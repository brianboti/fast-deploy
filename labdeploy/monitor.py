"""The settings page's Monitor section: what each view shows, gathered from what the controller already keeps, plus the few
live reads it allows itself (labdeploy/monitor_page.py draws it).

Stored, read for free: the daily digest (one a day, kept 90 days, with each service's day and the cost line), every plan
with its outcome and its release report a day later, and the audit trail. Live: Cloud Monitoring through the observer
identity, one query per chart line, each answer kept five minutes per application, environment and range and shared by
every viewer; Cloud Logging only on one application's page and only when asked (entries.list allows 60 calls a minute per
project, shared with the release reports and the digest); never BigQuery. The weekly cost report comes from GitHub and is
kept until a newer run exists. Nothing here changes anything.

Every view is scoped as Claude's tools are: a principal sees the applications its token lists, "*" every one. One that
sees only some applications gets only their numbers, through the controller's *_for methods that the tools use too: never
the whole project's bill, another application's line of it, or the background jobs' results.
"""
import concurrent.futures
import datetime
import statistics
import threading
import time
from urllib.parse import quote

from . import aftercare
from .infra import DEFAULT_SETTINGS

LIVE_SECONDS = 300  # a live read is shared for five minutes
FAILED_SECONDS = 60  # a read that failed is not tried again for a minute
LOG_SECONDS = 60
LOG_LINES = 20
REPORT_CHECK_SECONDS = 900  # how often a kept weekly report asks GitHub whether a newer run exists
HISTORY_DAYS = 90
# Each range: its words, its length and its step (one bar or point per step).
RANGES = {"1h": ("the last hour", 3600, 120), "24h": ("the last 24 hours", 86_400, 3600),
          "7d": ("the last 7 days", 7 * 86_400, 6 * 3600), "30d": ("the last 30 days", 30 * 86_400, 86_400)}
RELEASE_KINDS = {"staging": None, "production": "promotion"}
ATTEMPT_FAILED = {"failed", "cancelled", "timed_out", "dispatch_unknown", "dispatch_lost"}
MEMORY_LINE = 0.8  # the memory alert's line (infra/gcp/modules/service/main.tf)
READERS = 4  # reads one view makes at the same time when nothing is cached: the services' traffic, the applications' reports


class ReadFailed(Exception):
    """A live read that failed, remembered for a minute so a missing permission is not asked again on every view."""


class LiveCache:
    """Answers kept in memory and shared by every viewer; one read at a time per key, so two people opening the same page at
    once cost one read."""

    def __init__(self, seconds=LIVE_SECONDS, failed_seconds=FAILED_SECONDS, clock=time.time):
        self.seconds, self.failed_seconds, self.clock = seconds, failed_seconds, clock
        self.lock, self.items, self.locks = threading.Lock(), {}, {}

    def fresh(self, key, keep):
        with self.lock:
            entry = self.items.get(key)
        if entry is None:
            return None
        return entry if self.clock() - entry["at"] < (self.failed_seconds if "error" in entry else keep) else None

    @staticmethod
    def answer(entry):
        if "error" in entry:
            raise ReadFailed(entry["error"])
        return entry["value"], entry["at"]

    def get(self, key, read, seconds=None):
        """(value, the moment it was read), reading it when it is missing or older than `seconds`."""
        keep = seconds or self.seconds
        entry = self.fresh(key, keep)
        if entry is None:
            with self.lock:
                lock = self.locks.setdefault(key, threading.Lock())
            with lock:
                entry = self.fresh(key, keep)
                if entry is None:
                    at = self.clock()
                    try:
                        entry = {"at": at, "value": read()}
                    except Exception as exc:  # noqa: BLE001 - a view says why a part is missing; it never fails for it
                        entry = {"at": at, "error": (str(exc) or type(exc).__name__)[:240]}
                    with self.lock:
                        self.items = {k: v for k, v in self.items.items() if at - v["at"] < max(self.seconds, keep, 3600)}
                        self.items[key] = entry
        return self.answer(entry)


def moment(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC)


def day_of(seconds):
    return moment(seconds).date().isoformat()


def place(start, step, count, points):
    """Values in their steps by end time: [value or None] * count. A point whose end is not exactly on a step's end (the
    service rounds periods its own way) goes to the nearest one."""
    found = [None] * count
    for at, value in points:
        when = aftercare.parse_moment(at)
        if when is None:
            continue
        index = round((when - start) / step) - 1
        if 0 <= index < count:
            found[index] = value
    return found


def plan_window(plan):
    """(dispatched, finished) of a plan's run, in seconds since 1970; None where unknown."""
    workflow = plan.get("workflow") or {}
    started = aftercare.parse_moment(workflow.get("created_at"))
    finished = aftercare.parse_moment(workflow.get("updated_at")) or aftercare.parse_moment((plan.get("outcome") or {}).get("completed_at"))
    return started or aftercare.created(plan), finished


def outcome_of(plan):
    """What became of a release, promotion or rollback, in the digest's words: live, restored, stopped, rolled_back,
    in_progress; a rollback record is "rollback" when it succeeded."""
    if plan.get("kind") in aftercare.ROLLBACK_KINDS:
        return "rollback" if plan.get("state") == "succeeded" else "stopped" if plan.get("state") in ATTEMPT_FAILED else "in_progress"
    return aftercare.release_words(plan)


def each(read, items):
    """read(item) for every item, up to READERS at a time, in the items' order: a view with a dozen applications waits for the
    slowest few reads, not for all of them in turn."""
    items = list(items)
    if len(items) < 2:
        return [read(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(READERS, len(items))) as pool:
        return list(pool.map(read, items))


def requester(plan):
    return ((plan.get("approval") or {}).get("identifiers") or {}).get("requested_by") or plan.get("owner")


def run_url(plan):
    return (plan.get("outcome") or {}).get("run_url") or (plan.get("workflow") or {}).get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")


class Monitor:
    def __init__(self, controller, jobs=None, clock=time.time):
        self.controller, self.store, self.config = controller, controller.store, controller.config
        self.jobs = jobs  # the background jobs' state (a callable), or None in a process that runs none
        self.clock = clock
        self.live = LiveCache(clock=clock)
        self.reports, self.reports_lock = {}, threading.Lock()

    @property
    def observer(self):
        return self.controller.observer

    # ---- scope
    def visible(self, principal):
        return sorted(self.controller.visible_apps(principal))

    @staticmethod
    def everything(principal):
        return "*" in principal.applications

    def services(self, app_ids):
        """(application, environment, target) for every service of these applications: staging, and production where it exists."""
        found = []
        for app_id in app_ids:
            app = self.config.applications[app_id]
            found.append((app_id, "staging", app.target))
            if app.production is not None:
                found.append((app_id, "production", app.production.target))
        return found

    def history(self, app_ids, now):
        visible = set(app_ids)
        return [plan for plan in self.store.history(since=now - HISTORY_DAYS * 86_400) if plan.get("application") in visible]

    # ---- live reads, each cached and shared
    def window(self, range_key, now=None):
        _, seconds, step = RANGES[range_key]
        end = int((now or self.clock()) // 60 * 60)
        return end - seconds, end, step

    def traffic(self, app_id, environment, target, range_key):
        """Requests by response class and the latency, per step of a range: one Monitoring call, kept five minutes."""
        def read():
            start, end, step = self.window(range_key)
            found = self.observer.traffic(target, moment(start), moment(end), step)
            return {"start": start, "end": end, "step": step, **found}
        return self.live.get(("traffic", app_id, environment, range_key), read)

    def saturation(self, app_id, environment, target, range_key):
        def read():
            start, end, step = self.window(range_key)
            return {"start": start, "end": end, "step": step, **self.observer.saturation(target, moment(start), moment(end), step)}
        return self.live.get(("saturation", app_id, environment, range_key), read)

    def uptime(self, app_id, environment, target):
        return self.live.get(("uptime", app_id, environment), lambda: self.observer.uptime_days(target, 30))

    def error_budget(self, app_id, target):
        return self.live.get(("budget", app_id), lambda: self.observer.error_budget(target, f"{target.service}-production"))

    def log_lines(self, app_id, environment, target, range_key):
        minutes = min(RANGES[range_key][1] // 60, 1440)
        return self.live.get(("logs", app_id, environment, range_key), lambda: self.observer.logs(target, minutes, "ERROR", LOG_LINES), LOG_SECONDS)

    @staticmethod
    def read_part(read):
        """{"value", "read_at"} or {"error"} for one cached read."""
        try:
            value, at = read()
        except ReadFailed as exc:
            return {"error": str(exc)}
        return {"value": value, "read_at": at}

    # ---- what the records say about one service
    def live_release(self, app_id, environment):
        return self.store.latest_success(app_id, kind=RELEASE_KINDS[environment])

    def service_state(self, app_id, environment, plans, digest, traffic, now):
        """How one service is doing: (level, reasons). Levels: bad, warn, ok, and idle for one with no release yet. Reasons
        come from the records (the newest attempt, the release report, the digest's day) and the overview's live read."""
        app = self.config.applications[app_id]
        kind = RELEASE_KINDS[environment]
        mine = [plan for plan in plans if plan.get("application") == app_id and plan.get("kind") == kind]
        live = self.live_release(app_id, environment)  # however old: a live release from before the 90 days still serves
        reasons = []
        if live is None:
            return "idle", ["no release recorded by this controller yet"]
        newest = mine[-1] if mine else live
        if newest.get("id") != live.get("id") and newest.get("state") in ATTEMPT_FAILED and aftercare.created(newest) > aftercare.created(live):
            words = outcome_of(newest)
            reasons.append(("warn", "the newest release went back to the previous version on its own" if words == "restored"
                            else f"the newest release attempt ended {newest['state'].replace('_', ' ')}"))
        report = live.get("release_report") or {}
        if report.get("verdict") == "worse":
            reasons.append(("bad", f"its live release {(live.get('sha') or '')[:7]} did worse than the version before, a day after its release"))
        if traffic and traffic.get("server_errors"):
            count = traffic["server_errors"]
            reasons.append(("warn", f"{count:,} server error{'s' if count != 1 else ''} in {traffic.get('words', 'the last 24 hours')}"))
        day = next((item for item in (digest or {}).get("services") or [] if item.get("application") == app_id and item.get("environment") == environment), None)
        if day is not None and now - (aftercare.parse_moment((digest or {}).get("generated_at")) or 0) < 2 * 86_400:
            if day.get("uptime") is not None and day["uptime"] < aftercare.UPTIME_FLOOR:
                reasons.append(("bad", f"Google's uptime check passed {day['uptime'] * 100:.1f}% of the time on the digest's day"))
            if day.get("error_lines"):
                count = "200 or more" if day.get("error_lines_capped") else f"{day['error_lines']}"
                reasons.append(("warn", f"{count} error line{'s' if day['error_lines'] != 1 else ''} in its log on the digest's day"))
        if app.paused or self.config.paused:
            reasons.append(("info", "paused: no new release starts"))
        levels = [level for level, _ in reasons]
        level = "bad" if "bad" in levels else "warn" if "warn" in levels else "ok"
        return level, [words for _, words in reasons]

    @staticmethod
    def day_summary(found):
        """The overview's numbers from one 24-hour traffic read: requests per hour, server errors, and p95 over the day."""
        value = found["value"]
        count = (value["end"] - value["start"]) // value["step"]
        steps = place(value["start"], value["step"], count, [(step["at"], step) for step in value.get("steps") or []])
        hourly = [sum((step or {}).get("classes", {}).values()) if step else 0 for step in steps]
        errors = sum(((step or {}).get("classes") or {}).get("5xx", 0) for step in steps)
        return {"hourly": hourly, "requests": sum(hourly), "server_errors": errors, "p95_ms": value.get("p95_ms"), "start": value["start"],
                "step": value["step"], "read_at": found["read_at"], "words": "the last 24 hours"}

    # ---- the views
    def overview(self, principal):
        now = self.clock()
        app_ids = self.visible(principal)
        plans = self.history(app_ids, now)
        digest = self.store.digest()
        cards, live_times, services_seen = [], [], []
        services = self.services(app_ids)
        found_live = {}
        if self.observer is not None:  # one read per service, a few at a time; each kept five minutes for everyone
            def read(service):
                app_id, environment, target = service
                return self.read_part(lambda: self.traffic(app_id, environment, target, "24h"))
            found_live = {(app_id, environment): found for (app_id, environment, _), found in zip(services, each(read, services), strict=True)}
        for app_id in app_ids:
            app = self.config.applications[app_id]
            card = {"application": app_id, "paused": bool(app.paused or self.config.paused), "services": []}
            for _, environment, _ in self.services([app_id]):
                entry = {"environment": environment}
                if self.observer is not None:
                    found = found_live[(app_id, environment)]
                    if "error" in found:
                        entry["live_error"] = found["error"]
                    else:
                        entry["live"] = self.day_summary(found)
                        live_times.append(found["read_at"])
                entry["level"], entry["reasons"] = self.service_state(app_id, environment, plans, digest, entry.get("live"), now)
                release = self.live_release(app_id, environment)
                entry["release"] = release and {"sha": release.get("sha"), "message": (release.get("commit") or {}).get("message"),
                                                "at": (release.get("outcome") or {}).get("completed_at"), "plan": release.get("id"),
                                                "report": release.get("release_report")}
                card["services"].append(entry)
                services_seen.append(entry)
            cards.append(card)
        released = [entry for entry in services_seen if entry["level"] != "idle"]
        spend = self.spend(digest) if self.everything(principal) else None
        tiles = {"services": {"total": len(released), "healthy": sum(1 for entry in released if entry["level"] == "ok"),
                              "bad": sum(1 for entry in released if entry["level"] == "bad")},
                 "releases": self.release_counts(plans, now), "trust": aftercare.trust(plans, now, 30)["overall"], "spend": spend}
        drift = self.controller.drift_status()
        # What "all quiet" may claim: only what was looked at.
        checked = {"live": self.observer is not None and bool(services_seen) and all("live" in entry for entry in services_seen),
                   "spend": bool(spend and not spend.get("missing") and any(item.get("net_cost_usd") is not None for item in spend.get("projects") or [])),
                   "drift": drift.get("status") == "clean", "watchdog": digest is not None or self.store.newest_event("watchdog") is not None}
        return {"as_of": now, "live_at": min(live_times) if live_times else None, "observer": self.observer is not None,
                "digest": self.digest_facts(digest), "cards": cards, "tiles": tiles, "checked": checked,
                "attention": self.attention(principal, app_ids, plans, digest, cards, drift, now)}

    def digest_facts(self, digest):
        hour = getattr(self.config, "digest_hour_utc", None)
        return {"day": (digest or {}).get("day"), "generated_at": (digest or {}).get("generated_at"), "hour": hour, "off": hour is None,
                "kept": digest is not None}

    @staticmethod
    def release_counts(plans, now):
        """Releases and promotions that reached users in the last 7 days and the 7 before, and per day over 14 days."""
        daily = [0] * 14
        for plan in plans:
            if plan.get("kind") not in aftercare.RELEASE_KINDS or outcome_of(plan) not in ("live", "restored", "rolled_back"):
                continue
            age = now - aftercare.recorded(plan)
            if 0 <= age < 14 * 86_400:
                daily[13 - int(age // 86_400)] += 1
        return {"week": sum(daily[7:]), "previous": sum(daily[:7]), "daily": daily}

    def spend(self, digest):
        """The newest digest's cost line, as stored: each project's month so far, forecast, budget and cap."""
        cost = (digest or {}).get("cost")
        if not cost:
            return {"missing": True}
        return {**cost, "day": digest.get("day"), "generated_at": digest.get("generated_at")}

    def attention(self, principal, app_ids, plans, digest, cards, drift, now):
        """What needs a person, worst first: releases that did worse, services in trouble, forecasts over budget, drift,
        and the watchdog's problems. Each item names where to look."""
        items = []
        live = {(card["application"], entry["environment"]): (entry.get("release") or {}).get("plan") for card in cards for entry in card["services"]}
        for plan in plans:
            report = plan.get("release_report") or {}
            made = aftercare.parse_moment(report.get("generated_at"))
            environment = "production" if plan.get("kind") == "promotion" else "staging"
            # A live release that did worse is its service's own line below; one replaced since is said here, for a week.
            if report.get("verdict") == "worse" and made and now - made < 7 * 86_400 and live.get((plan["application"], environment)) != plan["id"]:
                where = " in production" if environment == "production" else ""
                items.append({"level": "warn", "text": f"{plan['application']}{where}: the release of {(plan.get('sha') or '')[:7]} did worse than the version "
                                                        "before a day after its release; a newer release has replaced it", "tab": "releases", "app": None})
        for card in cards:
            for entry in card["services"]:
                if entry["level"] in ("bad", "warn"):
                    where = " (production)" if entry["environment"] == "production" else ""
                    words = "; ".join(reason for reason in entry["reasons"] if not reason.startswith("paused"))
                    items.append({"level": entry["level"], "text": f"{card['application']}{where}: {words}", "tab": "health", "app": card["application"],
                                  "environment": entry["environment"]})
        if self.everything(principal):
            for item in ((digest or {}).get("cost") or {}).get("projects") or []:
                if item.get("over_budget"):
                    items.append({"level": "warn", "text": f"{item['project']} is on course for ${item['forecast_usd']:,.2f} this month, above its "
                                                           f"${item['budget_usd']:,.2f} budget", "tab": "cost", "app": None})
        for app_id in drift.get("serious") or []:
            if app_id in app_ids:
                items.append({"level": "bad", "text": f"{app_id}: its cloud setup differs from the code (infrastructure drift)", "tab": "activity", "app": None})
        for app_id in drift.get("failed") or []:
            if app_id in app_ids:
                items.append({"level": "warn", "text": f"{app_id}: the weekly drift check did not finish", "tab": "activity", "app": None})
        said = {item["app"] for item in items if item["tab"] == "health" and "newest release" in item["text"]}
        for problem in self.problems(digest):
            owner = problem.split(":", 1)[0] if ":" in problem else None
            if "infrastructure drift" in problem or (owner in said and "newest release attempt" in problem):
                continue  # said above: from the drift check itself, or as the service's own line
            if owner in self.config.applications:
                if owner in app_ids:
                    items.append({"level": "warn", "text": problem, "tab": "activity", "app": None})
            elif self.everything(principal):
                items.append({"level": "warn", "text": problem, "tab": "activity", "app": None})
        order = {"bad": 0, "warn": 1}
        return sorted(items, key=lambda item: order.get(item["level"], 2))

    def problems(self, digest):
        """The watchdog's problems: the newest digest's list, or the watchdog's own newest check while the digest is off."""
        if digest and getattr(self.config, "digest_hour_utc", None) is not None:
            return list(digest.get("problems") or [])
        event = self.store.newest_event("watchdog")
        if event and event.get("outcome") == "problems" and event.get("detail"):
            return [event["detail"]]
        return []

    def health(self, principal, app_id, environment="staging", range_key="24h", logs=False):
        app = self.controller.app(principal, app_id)  # ValueError when this principal may not see it
        if environment not in RELEASE_KINDS:
            environment = "staging"
        if environment == "production" and app.production is None:
            raise ValueError(f"{app_id} has no production")
        range_key = range_key if range_key in RANGES else "24h"
        target = app.production.target if environment == "production" else app.target
        now = self.clock()
        plans = self.history([app_id], now)
        digest = self.store.digest()
        live = self.live_release(app_id, environment)
        spec = (live or {}).get("spec") or {}
        settings = {**DEFAULT_SETTINGS, **app.infra}
        result = {"application": app_id, "environment": environment, "environments": ["staging"] + (["production"] if app.production else []),
                  "range": range_key, "as_of": now, "observer": self.observer is not None, "service": target.service, "project": target.project_id,
                  "release": live and {"sha": live.get("sha"), "message": (live.get("commit") or {}).get("message"), "plan": live.get("id"),
                                       "at": (live.get("outcome") or {}).get("completed_at"), "report": live.get("release_report"),
                                       "revision": (live.get("receipt") or {}).get("revision")},
                  "maximum": (spec.get("scaling") or {}).get("maximum"), "health_path": spec.get("health_path"),
                  "alerts": {"on": bool(settings["runtime_alerts"]), "latency_ms": settings["latency_alert_ms"],
                             "errors_5min": settings["error_alert_count"], "instances": settings["instance_alert_count"], "memory": MEMORY_LINE},
                  "links": self.links(target)}
        start, end, step = self.window(range_key, now)
        result["window"] = {"start": start, "end": end, "step": step}
        if self.observer is None:
            result["level"], result["reasons"] = self.service_state(app_id, environment, plans, digest, None, now)
            return result
        traffic = self.read_part(lambda: self.traffic(app_id, environment, target, range_key))
        saturation = self.read_part(lambda: self.saturation(app_id, environment, target, range_key))
        uptime = self.read_part(lambda: self.uptime(app_id, environment, target))
        for found in (traffic, saturation):
            if "value" in found:  # the window of the read itself: a cached read keeps the moment it was made
                result["window"] = {key: found["value"][key] for key in ("start", "end", "step")}
                break
        window = result["window"]
        count = (window["end"] - window["start"]) // window["step"]
        if "value" in traffic:
            value = traffic["value"]
            traffic = {"steps": place(value["start"], value["step"], count, [(item["at"], item) for item in value.get("steps") or []]),
                       "p95_ms": value.get("p95_ms"), "read_at": traffic["read_at"]}
        if "value" in saturation:
            value = saturation["value"]
            saturation = {"instances": place(value["start"], value["step"], count, value.get("instances") or []),
                          "memory": place(value["start"], value["step"], count, value.get("memory") or []), "read_at": saturation["read_at"]}
        if "value" in uptime:
            uptime = {"days": uptime["value"], "read_at": uptime["read_at"]}
        result.update(traffic=traffic, saturation=saturation, uptime=uptime)
        errors = None
        if "steps" in traffic:  # the status says what this range's live numbers say, as the overview does for 24 hours
            errors = {"server_errors": sum(((step or {}).get("classes") or {}).get("5xx", 0) for step in traffic["steps"]), "words": RANGES[range_key][0]}
        result["level"], result["reasons"] = self.service_state(app_id, environment, plans, digest, errors, now)
        result["markers"] = self.markers(plans, app_id, environment, window)
        if environment == "production" and callable(getattr(self.observer, "error_budget", None)):
            budget = self.read_part(lambda: self.error_budget(app_id, target))
            result["budget"] = budget.get("value") if "value" in budget else budget
            if "value" in budget:
                result["budget_read_at"] = budget["read_at"]
        if logs:
            found = self.read_part(lambda: self.log_lines(app_id, environment, target, range_key))
            result["logs"] = {"lines": found["value"], "read_at": found["read_at"]} if "value" in found else found
        return result

    @staticmethod
    def links(target):
        project, service = target.project_id, target.service
        query = f'resource.type="cloud_run_revision"\nresource.labels.service_name="{service}"\nseverity>=WARNING'
        return {"run": f"https://console.cloud.google.com/run/detail/{target.region}/{service}/metrics?project={project}",
                "dashboards": f"https://console.cloud.google.com/monitoring/dashboards?project={project}",
                "logs": f"https://console.cloud.google.com/logs/query;query={quote(query, safe='')};duration=PT24H?project={project}"}

    @staticmethod
    def markers(plans, app_id, environment, window):
        """The releases inside a chart's window: where each switched, and what became of it."""
        found = []
        span = window["end"] - window["start"]
        for plan in plans:
            if plan.get("application") != app_id or plan.get("kind") != RELEASE_KINDS[environment] or plan.get("state") in ("planned", "expired"):
                continue
            at = aftercare.switched_at(plan) or plan_window(plan)[1]
            if at is None or not window["start"] <= at <= window["end"]:
                continue
            found.append({"at": (at - window["start"]) / span, "moment": at, "sha": plan.get("sha"), "message": (plan.get("commit") or {}).get("message"),
                          "outcome": outcome_of(plan), "verdict": (plan.get("release_report") or {}).get("verdict")})
        return found

    def releases(self, principal, days=30, weeks=8):
        now = self.clock()
        app_ids = self.visible(principal)
        plans = self.history(app_ids, now)
        rows = []
        for plan in reversed(plans):
            if plan.get("state") in ("planned", "expired"):
                continue
            started, finished = plan_window(plan)
            kind = plan.get("kind") or "release"
            rows.append({"application": plan.get("application"), "kind": kind,
                         "environment": "production" if kind in ("promotion", "production_rollback") else "staging",
                         "sha": plan.get("sha"), "message": (plan.get("commit") or {}).get("message"), "who": requester(plan),
                         "started": started, "finished": finished, "seconds": finished - started if started and finished and finished >= started else None,
                         "outcome": outcome_of(plan), "state": plan.get("state"), "verdict": (plan.get("release_report") or {}).get("verdict"),
                         "run_url": run_url(plan), "plan": plan.get("id")})
        rows.sort(key=lambda row: row["finished"] or row["started"] or 0, reverse=True)
        trust = aftercare.trust(plans, now, days)
        weekly = []
        for week in range(weeks - 1, -1, -1):
            end = now - week * 7 * 86_400
            numbers = aftercare.trust([plan for plan in plans if aftercare.created(plan) < end], end, 7)["overall"]
            weekly.append({"end": end, "reached": numbers["reached_traffic"], "failures": numbers["restored_automatically"] + numbers["rolled_back_by_a_person"],
                           "rate": numbers["change_failure_rate"]})
        leads = []
        for plan in plans:
            if plan.get("kind") in aftercare.RELEASE_KINDS and plan.get("state") == "succeeded" and now - aftercare.recorded(plan) <= days * 86_400:
                committed = aftercare.parse_moment((plan.get("commit") or {}).get("date"))
                finished = plan_window(plan)[1]
                if committed and finished and finished >= committed:
                    leads.append((finished - committed) / 3600)
        since = now - days * 86_400
        rows_shown = [row for row in rows if (row["finished"] or row["started"] or 0) >= since]
        services = sorted({(row["application"], row["environment"]) for row in rows_shown if row["kind"] in ("release", "promotion")})
        return {"as_of": now, "days": days, "since": since, "rows": rows, "trust": trust, "weekly": weekly, "services": services,
                "lead_hours": round(statistics.median(leads), 1) if leads else None, "leads": len(leads), "applications": app_ids}

    def cost(self, principal):
        now = self.clock()
        app_ids = self.visible(principal)
        everything = self.everything(principal)
        digest = self.store.digest()
        month = moment(now).strftime("%Y-%m")
        result = {"as_of": now, "everything": everything, "applications": app_ids, "digest": self.digest_facts(digest),
                  "billing_export": bool(getattr(getattr(self.config, "onboarding", None), "billing_export_table", None)),
                  "observer": self.observer is not None, "month": month}
        if everything:
            result["position"] = self.spend(digest)
            series = {}
            for record in self.store.digests(f"{month}-01"):
                made = aftercare.parse_moment(record.get("generated_at"))
                for item in (record.get("cost") or {}).get("projects") or []:
                    if made and item.get("net_cost_usd") is not None:
                        series.setdefault(item["project"], []).append((made, item["net_cost_usd"]))
            result["month_series"] = series
        result["reports"] = each(lambda app_id: {"application": app_id, **self.weekly_report(principal, app_id)}, app_ids)
        result["bounds"] = []
        for app_id in app_ids:
            app = self.config.applications[app_id]
            for environment in ("staging", "production"):
                if environment == "production" and app.production is None:
                    continue
                live = self.live_release(app_id, environment)
                cost = (live or {}).get("cost") or {}
                if not cost:
                    continue
                result["bounds"].append({"application": app_id, "environment": environment, "floor": (cost.get("idle_floor_usd_month") or 0) + (cost.get("needs_floor_usd_month") or 0),
                                         "ceiling": (cost.get("compute_ceiling_usd_month") or 0) + (cost.get("needs_floor_usd_month") or 0),
                                         "limit": app.cost_ceiling_usd, "sha": live.get("sha")})  # the ceiling set now, which the next plan meets
        return result

    def weekly_report(self, principal, app_id):
        """An application's newest weekly cost report as this identity may see it, as get_cost_report gives it
        (Controller.cost_report_for). {"result", "checked_at"} or {"error"}."""
        app = self.controller.app(principal, app_id)
        found = self.kept_report(app_id, app)
        return {**found, "result": self.controller.cost_report_for(principal, found["result"])} if "result" in found else found

    def kept_report(self, app_id, app):
        """The whole report, kept for everyone until GitHub has a newer run: at most one check every fifteen minutes, and a
        download only when the run changed."""
        now = self.clock()
        with self.reports_lock:
            kept = self.reports.get(app_id)
        if kept and "error" in kept:  # a download that failed is tried again five minutes later, not on every view
            if now - kept["checked"] < LIVE_SECONDS:
                return {"error": kept["error"]}
            kept = None
        if kept and now - kept["checked"] < REPORT_CHECK_SECONDS:
            return {"result": kept["result"], "checked_at": kept["checked"]}
        check = getattr(self.controller.github, "newest_run_id", None)
        newest = None
        if kept and callable(check):
            try:
                newest = check(app, app.workflows["rightsize"])
            except Exception as exc:  # noqa: BLE001 - the kept report still shows; the check says why it is old
                return {"result": kept["result"], "checked_at": kept["checked"], "warning": f"GitHub was not reached: {str(exc)[:160]}"}
            if newest is not None and newest == kept.get("newest"):
                with self.reports_lock:
                    kept["checked"] = now
                return {"result": kept["result"], "checked_at": now}
        elif kept and now - kept["checked"] < 4 * REPORT_CHECK_SECONDS:  # an adapter that cannot ask cheaply reads again hourly
            return {"result": kept["result"], "checked_at": kept["checked"]}
        try:
            result = self.controller.read_cost_report(app)
        except Exception as exc:  # noqa: BLE001 - one application's report missing is said; the others still show
            with self.reports_lock:
                self.reports[app_id] = {"error": str(exc)[:200] or type(exc).__name__, "checked": now}
            return {"error": self.reports[app_id]["error"]}
        if newest is None and callable(check):
            try:
                newest = check(app, app.workflows["rightsize"])
            except Exception:  # noqa: BLE001 - only the next check's shortcut is lost
                newest = None
        with self.reports_lock:
            self.reports[app_id] = {"result": result, "checked": now, "newest": newest}
        return {"result": result, "checked_at": now}

    def activity(self, principal, kind=None, identity=None, application=None, days=30, limit=50, page=1):
        now = self.clock()
        scope = self.controller.event_scope(principal)  # the same events as list_events gives this identity
        since = now - 365 * 86_400
        choices = {column: self.store.event_values(column, scope, since) for column in ("kind", "identity", "application")}
        kind = kind if kind in choices["kind"] else None
        identity = identity if identity in choices["identity"] else None
        application = application if application in choices["application"] else None
        start = int(now // 86_400 * 86_400) - (days - 1) * 86_400
        counted = self.store.event_days(start, kind, identity, application, scope)
        count = self.store.count_events(kind, identity, application, scope)
        page = min(max(1, page), max(1, -(-count // limit)))
        day_rows = []
        for offset in range(days):
            day = day_of(start + offset * 86_400)
            total, configured = counted.get(day, (0, 0))
            day_rows.append({"day": day, "events": total, "configuration": configured})
        result = {"as_of": now, "filters": {"kind": kind, "identity": identity, "application": application}, "choices": choices,
                  "events": self.store.find_events(limit, kind, identity, application, scope, offset=(page - 1) * limit), "days": day_rows,
                  "scope": "everything" if scope is None else "own" if scope[1] is None else "applications", "limit": limit,
                  "page": page, "total": count, "pages": max(1, -(-count // limit)), "everything": self.everything(principal)}
        # The jobs and the drift as get_controller_status gives them to this identity.
        result["jobs"] = self.controller.background_for(principal, self.jobs() if callable(self.jobs) else None)
        result["backup"] = self.store.newest_event("backup")
        result["restore_drill"] = self.store.newest_event("restore_drill")
        result["digest"] = self.digest_facts(self.store.digest())
        result["drift"] = self.controller.drift_for(principal)
        status = getattr(self.controller.github, "credential_status", None)
        if callable(status):
            try:
                result["credential"] = self.controller.cached(("credential",), 3600, status)
            except Exception as exc:  # noqa: BLE001 - informative; the line says why it is missing
                result["credential"] = {"error": str(exc)[:200]}
        else:
            result["credential"] = None
        return result
