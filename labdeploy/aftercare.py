"""After a release (docs/ROADMAP.md Phase 23): the report a day later, the trust numbers, and the daily digest's facts.

Everything here is computed by code, from the controller's own records and what the observer reads; the words are
labdeploy/chat.py's. Nothing here acts. A "worse" report names a rollback as the next step, and a person decides.

The release report (D57) compares the new revision's first day with what the previous revisions did in the week before
the switch, from Cloud Run's request log with the health path left out: Cloud Run's request metric cannot tell the
uptime check's calls from a person's, and on a quiet application they are nearly all of it. The trust numbers (D61) use
DORA's definitions on the plans the controller recorded. The digest (D60) gathers what a person should know once a day.
"""
import datetime
import math
import statistics
import time

REPORT_AFTER_SECONDS = 86_400  # the new revision's first day
BASELINE_SECONDS = 7 * 86_400  # the previous revisions' week before the switch
MIN_REQUESTS = 100  # on each side, besides the health path, before shares and latencies are compared
ERROR_FLOOR = 0.01  # a 5xx share at or below this is never called worse
ERROR_FACTOR = 2.0
LATENCY_FACTOR = 2.0
LATENCY_BETTER = 2 / 3
UPTIME_FLOOR = 0.99
MIN_WINDOW_SECONDS = 3600  # replaced within the hour: nothing worth comparing
HORIZON_SECONDS = 8 * 86_400  # a release recorded longer ago gets no report (an upgrade does not reach back a month)
MAX_ATTEMPTS = 3  # reads that keep failing end in "could not be read" instead of a retry every fifteen minutes
RELEASE_KINDS = (None, "promotion")
ROLLBACK_KINDS = ("rollback", "production_rollback")


def parse_moment(value):
    """Seconds since 1970 from ISO text or a number; None for anything else."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)).timestamp()


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def created(plan):
    """When a plan was made: it expires fifteen minutes after that."""
    return float(plan.get("expires_at") or 0) - 900


def recorded(plan):
    """When the controller recorded a plan's outcome, else when the plan was made."""
    return float((plan.get("outcome") or {}).get("recorded_at") or 0) or created(plan)


# ---- the release report (D57)
def switched_at(plan):
    """The moment the new revision took all the traffic: the start of its last bake, else its last move of traffic, else
    the run's end. None when the receipt says none of it."""
    receipt = plan.get("receipt") or {}
    since = parse_moment((receipt.get("bake") or {}).get("since"))
    if since:
        return since
    moves = [parse_moment(phase.get("at")) for phase in receipt.get("phases") or [] if phase.get("state") == "promoting"]
    moves = [moment for moment in moves if moment]
    if moves:
        return max(moves)
    return parse_moment((plan.get("outcome") or {}).get("completed_at"))


def previous_revisions(plan):
    receipt = plan.get("receipt") or {}
    new = receipt.get("revision")
    return sorted(name for name in (receipt.get("previous_traffic") or {}) if name != new)


def p95(values):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def window_stats(read, seconds):
    """Counts over request log lines: requests, server errors, their share and p95 latency; `truncated` when the window held
    more lines than were read (the newest ones count)."""
    requests = (read or {}).get("requests") or []
    errors = sum(1 for item in requests if isinstance(item.get("status"), int) and item["status"] >= 500)
    latency = p95([item["latency_ms"] for item in requests if isinstance(item.get("latency_ms"), (int, float))])
    return {"requests": len(requests), "errors_5xx": errors, "error_share": round(errors / len(requests), 4) if requests else None,
            "p95_ms": None if latency is None else round(latency, 1), "hours": round(seconds / 3600, 1), "truncated": bool((read or {}).get("truncated"))}


def judge(new, baseline, uptime):
    """The verdict and its reasons, from the two windows and the uptime check (D57): better, same, worse, too_little_traffic
    or first (no previous version to compare with)."""
    worse = []
    if uptime is not None and uptime < UPTIME_FLOOR:
        worse.append({"rule": "uptime", "new": uptime})
    if baseline is None:
        return {"verdict": "worse" if worse else "first", "reasons": worse}
    enough = new["requests"] >= MIN_REQUESTS and baseline["requests"] >= MIN_REQUESTS
    if enough:
        share, before = new["error_share"] or 0.0, baseline["error_share"] or 0.0
        if share > ERROR_FLOOR and share > ERROR_FACTOR * before:
            worse.append({"rule": "errors", "new": share, "before": before})
        if new["p95_ms"] and baseline["p95_ms"] and new["p95_ms"] > LATENCY_FACTOR * baseline["p95_ms"]:
            worse.append({"rule": "latency", "new": new["p95_ms"], "before": baseline["p95_ms"]})
    if worse:
        return {"verdict": "worse", "reasons": worse}
    if not enough:
        return {"verdict": "too_little_traffic", "reasons": []}
    share, before = new["error_share"] or 0.0, baseline["error_share"] or 0.0
    better = []
    if before > ERROR_FLOOR and share <= before / 2:
        better.append({"rule": "errors", "new": share, "before": before})
    if new["p95_ms"] and baseline["p95_ms"] and new["p95_ms"] <= LATENCY_BETTER * baseline["p95_ms"] and share <= before:
        better.append({"rule": "latency", "new": new["p95_ms"], "before": baseline["p95_ms"]})
    return {"verdict": "better" if better else "same", "reasons": better}


def release_report(observer, target, plan, replaced_at=None, now=None):
    """The report of one release that succeeded at least a day ago (or was replaced since): its window against the week
    before, the uptime check, memory, and the verdict. Raises when the observer cannot read; the caller retries."""
    now = now or time.time()
    receipt, spec = plan.get("receipt") or {}, plan.get("spec") or {}
    start = switched_at(plan)
    revision = receipt.get("revision")
    base = {"generated_at": iso(now), "revision": revision, "environment": "production" if plan.get("kind") == "promotion" else "staging"}
    if start is None or not revision:
        return {**base, "verdict": "unreadable", "note": "the release receipt names no switch time or revision"}
    end = min(start + REPORT_AFTER_SECONDS, replaced_at or math.inf, now)
    base["window"] = {"from": iso(start), "to": iso(end)}
    if end - start < MIN_WINDOW_SECONDS:
        return {**base, "verdict": "replaced", "note": "a newer release replaced it within the hour"}
    as_moment = lambda seconds: datetime.datetime.fromtimestamp(seconds, datetime.UTC)  # noqa: E731
    health = spec.get("health_path")
    new = window_stats(observer.revision_requests(target, [revision], as_moment(start), as_moment(end), health), end - start)
    previous = previous_revisions(plan)
    baseline = None
    if previous:
        baseline = window_stats(observer.revision_requests(target, previous, as_moment(start - BASELINE_SECONDS), as_moment(start), health), BASELINE_SECONDS)
    uptime = observer.uptime_pass_rate(target, as_moment(start), as_moment(end))
    memory = {"new": observer.memory_p95(target, [revision], as_moment(start), as_moment(end)),
              "before": observer.memory_p95(target, previous, as_moment(start - BASELINE_SECONDS), as_moment(start)) if previous else None}
    verdict = judge(new, baseline, uptime)
    report = {**base, **verdict, "new": new, "baseline": baseline, "uptime": uptime, "memory": memory,
              "rules": {"min_requests": MIN_REQUESTS, "error_floor": ERROR_FLOOR, "error_factor": ERROR_FACTOR, "latency_factor": LATENCY_FACTOR,
                        "uptime_floor": UPTIME_FLOOR, "baseline_days": BASELINE_SECONDS // 86_400}}
    if replaced_at and replaced_at < start + REPORT_AFTER_SECONDS:
        report["note"] = f"a newer release replaced it after {round((end - start) / 3600, 1):g} hours, so the window is that long"
    if not health:
        report["health_note"] = "the service has no health route, so the uptime check's calls are counted among its requests"
    return report


def awaiting_report(plans, now=None):
    """Releases and promotions that succeeded, are at least a day old or replaced since, have no report, and were recorded
    in the last eight days; each with the moment a newer release of the same application and kind replaced it (or None)."""
    now = now or time.time()
    found = []
    ordered = sorted(plans, key=created)
    for index, plan in enumerate(ordered):
        if plan.get("state") != "succeeded" or plan.get("kind") not in RELEASE_KINDS or plan.get("release_report"):
            continue
        if now - recorded(plan) > HORIZON_SECONDS:
            continue
        later = [switched_at(other) for other in ordered[index + 1:] if other.get("state") == "succeeded"
                 and other.get("application") == plan.get("application") and other.get("kind") == plan.get("kind")]
        replaced = min((moment for moment in later if moment), default=None)
        start = switched_at(plan)
        due = plan.get("rolled_back") or replaced is not None or (start is not None and now - start >= REPORT_AFTER_SECONDS) or start is None
        if due:
            found.append((plan, replaced))
    return found


# ---- the trust numbers (D61)
def trust(plans, now=None, days=30):
    """DORA's numbers from the controller's own records over the last `days`, overall and per application. A failure is a
    release that reached traffic and was then restored automatically (its post-switch probe or its bake) or rolled back by
    a person; recovery is the time from that failure to the next release of the same application that succeeded."""
    now = now or time.time()
    since = now - days * 86_400
    rolled_back = {}
    for plan in plans:
        if plan.get("kind") in ROLLBACK_KINDS and plan.get("state") == "succeeded" and (plan.get("receipt") or {}).get("state") == "rolled_back":
            rolled_back[plan.get("from_plan")] = recorded(plan)
    releases = sorted((plan for plan in plans if plan.get("kind") in RELEASE_KINDS and plan.get("state") not in ("planned", "expired")
                       and created(plan) >= since), key=created)
    every = sorted((plan for plan in plans if plan.get("kind") in RELEASE_KINDS), key=created)

    def numbers(chosen):
        reached = stopped = restored = person = inconclusive = bakes = 0
        failures, suspects = [], []
        for plan in chosen:
            receipt = plan.get("receipt") or {}
            moved = receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") in ("promoting", "baking") and not receipt.get("cancelled")
            succeeded = plan.get("state") == "succeeded"
            bake = (receipt.get("bake") or {}).get("result")
            if bake in ("passed", "failed", "inconclusive", "unobserved"):
                bakes += 1
                inconclusive += bake == "inconclusive"
            if succeeded or moved:
                reached += 1
            elif plan.get("state") in ("failed", "cancelled", "timed_out"):
                stopped += 1
            if moved:
                restored += 1
                failures.append((plan, recorded(plan)))
                again = [other for other in every if other.get("application") == plan.get("application") and other.get("kind") == plan.get("kind")
                         and other.get("sha") == plan.get("sha") and other.get("state") == "succeeded"
                         and 0 < created(other) - recorded(plan) <= 86_400 and ((other.get("receipt") or {}).get("bake") or {}).get("result") == "passed"]
                if again:
                    suspects.append({"application": plan.get("application"), "plan": plan["id"][:8], "sha": (plan.get("sha") or "")[:12],
                                     "passed_in": again[0]["id"][:8]})
            elif succeeded and plan["id"] in rolled_back:
                person += 1
                failures.append((plan, rolled_back[plan["id"]]))
        recoveries = []
        for plan, failed_at in failures:
            nxt = [recorded(other) for other in every if other.get("application") == plan.get("application") and other.get("kind") == plan.get("kind")
                   and other.get("state") == "succeeded" and other["id"] not in rolled_back and recorded(other) > failed_at]
            if nxt:
                recoveries.append(min(nxt) - failed_at)
        return {"releases": len(chosen), "reached_traffic": reached, "stopped_before_traffic": stopped, "restored_automatically": restored,
                "rolled_back_by_a_person": person, "change_failure_rate": round((restored + person) / reached, 3) if reached else None,
                "recovery_hours_median": round(statistics.median(recoveries) / 3600, 1) if recoveries else None,
                "bakes": bakes, "inconclusive_share": round(inconclusive / bakes, 3) if bakes else None, "suspected_false_restores": suspects}
    per_app = {}
    for plan in releases:
        per_app.setdefault(plan.get("application"), []).append(plan)
    return {"days": days, "since": iso(since), "overall": numbers(releases), "applications": {app: numbers(chosen) for app, chosen in sorted(per_app.items())}}


# ---- the digest's facts (D60)
def release_words(plan):
    """What became of a release, as the digest says it: live, restored, stopped, rolled back, or still on its way."""
    receipt = plan.get("receipt") or {}
    if plan.get("rolled_back"):
        return "rolled_back"
    if plan.get("state") == "succeeded":
        return "live"
    if receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") in ("promoting", "baking") and not receipt.get("cancelled"):
        return "restored"
    if plan.get("state") in ("failed", "cancelled", "timed_out", "dispatch_lost"):
        return "stopped"
    return "in_progress"


def recent_releases(plans, now=None, hours=24):
    """Releases, promotions and rollbacks recorded in the last `hours`, newest first."""
    now = now or time.time()
    found = []
    for plan in plans:
        at = (plan.get("outcome") or {}).get("recorded_at")
        if plan.get("state") in ("planned", "expired") or not at or now - float(at) > hours * 3600:
            continue
        found.append({"application": plan.get("application"), "kind": plan.get("kind") or "release", "plan": plan["id"][:8],
                      "sha": plan.get("sha"), "message": (plan.get("commit") or {}).get("message"), "state": plan.get("state"),
                      "result": release_words(plan) if plan.get("kind") in RELEASE_KINDS else plan.get("state"),
                      "failed_phase": (plan.get("receipt") or {}).get("failed_phase"), "at": iso(float(at))})
    return sorted(found, key=lambda item: item["at"], reverse=True)


def recent_reports(plans, now=None, hours=24):
    """Release reports written in the last `hours`, newest first."""
    now = now or time.time()
    found = []
    for plan in plans:
        report = plan.get("release_report") or {}
        at = parse_moment(report.get("generated_at"))
        if at and now - at <= hours * 3600:
            found.append({"application": plan.get("application"), "environment": report.get("environment"), "plan": plan["id"][:8],
                          "sha": plan.get("sha"), "message": (plan.get("commit") or {}).get("message"),
                          "released_at": (plan.get("outcome") or {}).get("completed_at"), **report})
    return sorted(found, key=lambda item: item.get("generated_at") or "", reverse=True)


ERROR_LINES_CAP = 200  # error log lines read per application a day: enough to say "200 or more"


def service_day(observer, target, application, environment):
    """One application's last day as the digest counts it: server errors, the uptime check, and error log lines."""
    metrics = observer.metrics(target, 1440)
    lines = observer.logs(target, 1440, "ERROR", ERROR_LINES_CAP)
    return {"application": application, "environment": environment, "server_errors": int(metrics.get("server_errors") or 0),
            "uptime": metrics.get("uptime_checks_passed"), "error_lines": len(lines), "error_lines_capped": len(lines) >= ERROR_LINES_CAP,
            "requests": metrics.get("requests")}


def troubled(day):
    """Whether one application's day needs a person to look."""
    return bool(day.get("server_errors") or day.get("error_lines") or (day.get("uptime") is not None and day["uptime"] < UPTIME_FLOOR))
