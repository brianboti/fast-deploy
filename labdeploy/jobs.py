"""The platform's clock and the guard of the jobs an application starts itself (docs/ROADMAP.md D66, D70, 27.6).

Both run in the controller's scheduler and read what is released, never a branch: the newest successful staging release
of each application, and its newest promotion for production, with the contract (`spec`) and the target that release
recorded.

The clock: every `wake.every_minutes`, POST `wake.path` to the service as its <service>-wake identity (an identity token the
controller may mint, for the run.app address or, behind IAP, for the project's IAP client), each call on its own thread
so a slow application never holds the scheduler. A service whose ingress the spending cap closed is not woken.

The guard: every minute, every job that carries the platform's labels in each application's project, whatever its
release says; a run that has more than one task, has run past its `timeout_minutes`, or is beyond the day's
`runs_per_day` (UTC) is cancelled, and so is any run of a job the released contract does not declare (a job its root
created before a release named it, or after a rollback to one that does not). Each cancel lands in the audit trail.
Cloud Run lets one permission start a job with any task count and timeout (docs/RESEARCH-2026-10-21-jobs.md, section 1),
so the contract's limits hold because the guard holds them. A run that ends within the minute between two looks cannot be
cancelled: it still counts towards the day, and a day over its runs is recorded once (`job_over_limit`) for the digest.
"""
import datetime
import threading
import time

import httpx

from .models import AppSpec

RUN_API = "https://run.googleapis.com/v2"
IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts"
GRACE_SECONDS = 300  # a run's own timeout stops it first; the guard cancels what is still going five minutes after it
MAX_WAKE_SECONDS = 300
CLOSED = "INGRESS_TRAFFIC_INTERNAL_ONLY"


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def started_today(executions, now):
    """The executions created on `now`'s UTC day, oldest first."""
    today = datetime.datetime.fromtimestamp(now, datetime.UTC).date()
    return sorted((e for e in executions if (parse_time(e.get("createTime")) or 0) and
                   datetime.datetime.fromtimestamp(parse_time(e["createTime"]), datetime.UTC).date() == today),
                  key=lambda e: (parse_time(e.get("createTime")) or 0, e.get("name", "")))


def judge(executions, job, now):
    """Which of a job's executions the guard cancels, each with its reason (D70). `executions` as the Cloud Run API lists them;
    `job` the released contract's entry, or None for a job the released contract does not declare: every run of it goes."""
    if job is None:
        return [(e.get("name", ""), "the application's released contract declares no such job")
                for e in executions if not e.get("completionTime") and not e.get("cancelledCount")]
    today = datetime.datetime.fromtimestamp(now, datetime.UTC).date()
    started_today = sorted((e for e in executions if (parse_time(e.get("createTime")) or 0) and
                            datetime.datetime.fromtimestamp(parse_time(e["createTime"]), datetime.UTC).date() == today),
                           key=lambda e: (parse_time(e.get("createTime")) or 0, e.get("name", "")))
    beyond = {e.get("name") for e in started_today[job.runs_per_day:]}
    verdicts = []
    for execution in executions:
        if execution.get("completionTime") or execution.get("cancelledCount"):
            continue  # finished or already cancelled
        name = execution.get("name", "")
        started = parse_time(execution.get("startTime") or execution.get("createTime"))
        tasks = int(execution.get("taskCount") or 1)
        if tasks > 1:
            verdicts.append((name, f"it was started with {tasks} tasks; the contract allows one"))
        elif started is not None and now - started > job.timeout_minutes * 60 + GRACE_SECONDS:
            verdicts.append((name, f"it has run {round((now - started) / 60)} minutes, past its {job.timeout_minutes}"))
        elif name in beyond:
            verdicts.append((name, f"it is run {[e.get('name') for e in started_today].index(name) + 1} today, beyond the {job.runs_per_day} the contract allows"))
    return verdicts


class Keeper:
    """The clock and the guard over every released application with jobs or a clock (D66, D70)."""

    def __init__(self, controller, observer, config, client=None, clock=time.time, spawn=None):
        self.controller, self.observer, self.config = controller, observer, config
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.clock = clock
        self.spawn = spawn or (lambda work: threading.Thread(target=work, daemon=True, name="lab-deploy-wake").start())
        self.lock = threading.Lock()
        self.wakes = {}    # (application, environment) -> {"next": ..., "running": bool, "last_at", "last_status", "failures"}
        self.over = set()  # (application, environment, job, day) already recorded as over its runs a day
        self.cancels = []  # the newest cancels, for the status and the digest

    def targets(self):
        """(application, environment, target, spec, url) for each application and environment, with the contract its newest
        successful release recorded (None when there is none) and the address that release reported."""
        store = self.controller.store
        for app_id, app in self.config.applications.items():
            for environment, kind in (("staging", None), ("production", "promotion")):
                if environment == "production" and app.production is None:
                    continue
                plan = store.latest_success(app_id, kind=kind) or {}
                spec = None
                if plan.get("spec"):
                    try:
                        spec = AppSpec.model_validate(plan["spec"])
                    except ValueError:
                        spec = None
                target = (app.production.target if environment == "production" else app.target)
                yield app_id, environment, target, spec, (plan.get("receipt") or {}).get("url")

    def released(self):
        """The targets whose release carries jobs or a clock."""
        for app_id, environment, target, spec, url in self.targets():
            if spec is not None and (spec.jobs or spec.wake is not None):
                yield app_id, environment, target, spec, url

    def pages(self, url, key, limit=10):
        """Every item of a Cloud Run list, page by page (at most `limit` pages: a job keeps its newest 1,000 executions)."""
        items, token = [], None
        for _ in range(limit):
            found = self.observer.get(url, pageSize=100, **({"pageToken": token} if token else {}))
            items += found.get(key) or []
            token = found.get("nextPageToken")
            if not token:
                break
        return items

    # ---- the clock (D66)
    def wake_tick(self):
        if self.observer is None:
            return {"woken": 0, "note": "no observer identity: the controller cannot reach the cloud"}
        now, started = self.clock(), []
        for app_id, environment, target, spec, url in self.released():
            if spec.wake is None or not url:
                continue
            key = (app_id, environment)
            with self.lock:
                entry = self.wakes.setdefault(key, {"next": 0, "running": False, "last_at": None, "last_status": None, "failures": 0})
                if entry["running"] or now < entry["next"]:
                    continue
                entry["running"] = True
                # After three failures in a row the clock waits half an hour before it tries again.
                interval = 1800 if entry["failures"] >= 3 else spec.wake.every_minutes * 60
                entry["next"] = now + interval
            started.append(f"{app_id} ({environment})")
            self.spawn(lambda k=key, t=target, s=spec, u=url: self.wake(k, t, s, u))
        return {"woken": len(started), "applications": started}

    def wake(self, key, target, spec, url):
        status = "error"
        try:
            # One read of the service itself (its ingress), not of its revisions: the cap closes ingress (27.7).
            service = self.observer.get(f"{RUN_API}/projects/{target.project_id}/locations/{target.region}/services/{target.service}")
            if service.get("ingress") == CLOSED:
                status = "closed by the spending cap"
                return
            audience = url
            if spec.exposure == "lab-members":
                # Behind IAP the token is for the project's IAP client, as the release probe's (docs/ROADMAP.md D66).
                setting = "production.iap_client_id" if key[1] == "production" else "onboarding.iap_client_id"
                audience = getattr(getattr(self.config, setting.split(".")[0], None), "iap_client_id", None)
                if not audience:
                    status = f"no IAP client recorded ({setting}), so the clock cannot pass the proxy"
                    return
            account = f"{target.service}-wake@{target.project_id}.iam.gserviceaccount.com"
            minted = self.client.post(f"{IAM_CREDENTIALS}/{account}:generateIdToken", json={"audience": audience, "includeEmail": True},
                                      headers={"Authorization": "Bearer " + self.observer.token()})
            if minted.status_code != 200:
                status = f"could not mint the wake identity's token (HTTP {minted.status_code}): apply the application's root"
                return
            answer = self.client.post(url.rstrip("/") + spec.wake.path, headers={"Authorization": "Bearer " + minted.json()["token"]},
                                      timeout=min(spec.timeout_seconds, MAX_WAKE_SECONDS) + 5)
            status = str(answer.status_code)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            status = f"error: {type(exc).__name__}"
        finally:
            with self.lock:
                entry = self.wakes[key]
                entry.update(running=False, last_at=self.clock(), last_status=status)
                entry["failures"] = 0 if status.startswith("2") else entry["failures"] + 1
            self.controller.store.record_event("wake", name=spec.wake.path, application=key[0], outcome=status[:60], detail=f"{key[1]}: {status}")

    # ---- the guard (D70)
    def guard_tick(self):
        if self.observer is None:
            return {"checked": 0, "cancelled": 0, "note": "no observer identity: the controller cannot reach the cloud"}
        now, checked, cancelled, problems, listed = self.clock(), 0, [], [], {}
        day = datetime.datetime.fromtimestamp(now, datetime.UTC).date().isoformat()
        for app_id, environment, target, spec, _ in self.targets():
            where = (target.project_id, target.region)
            if where not in listed:
                try:
                    listed[where] = self.pages(f"{RUN_API}/projects/{target.project_id}/locations/{target.region}/jobs", "jobs")
                except (httpx.HTTPError, ValueError) as exc:
                    problems.append(f"jobs of {target.project_id}: {str(exc)[:120]}")
                    listed[where] = []
            # Every job the platform made for this service, whether or not the released contract names it.
            mine = [item for item in listed[where] if (item.get("labels") or {}).get("lab-deploy-app") == target.service
                    and (item.get("labels") or {}).get("lab-deploy-job")]
            for item in mine:
                name = item["labels"]["lab-deploy-job"]
                job = spec.jobs.get(name) if spec is not None else None
                try:
                    executions = self.pages(f"{RUN_API}/{item['name']}/executions", "executions")
                except (httpx.HTTPError, ValueError) as exc:
                    problems.append(f"{app_id} {name} ({environment}): {str(exc)[:120]}")
                    continue
                checked += 1
                if job is not None and len(started_today(executions, now)) > job.runs_per_day:
                    key = (app_id, environment, name, day)
                    with self.lock:
                        first = key not in self.over
                        self.over.add(key)
                    if first:
                        self.controller.store.record_event("job_over_limit", name=f"{target.service}-{name}", application=app_id, outcome="over",
                                                           detail=f"{environment}: more than {job.runs_per_day} runs started today")
                for execution, reason in judge(executions, job, now):
                    outcome = self.cancel(execution)
                    if outcome == "already finished":
                        continue
                    item = {"at": now, "application": app_id, "environment": environment, "job": name, "execution": execution.rsplit("/", 1)[-1],
                            "reason": reason, "outcome": outcome}
                    cancelled.append(item)
                    self.controller.store.record_event("job_guard", name=f"{target.service}-{name}", application=app_id, outcome=outcome,
                                                       detail=f"{environment}: cancelled {item['execution']}: {reason}")
        with self.lock:
            self.cancels = (cancelled + self.cancels)[:50]
        return {"checked": checked, "cancelled": len(cancelled), "problems": problems}

    def cancel(self, execution):
        if not execution.startswith("projects/") or "/jobs/" not in execution or "/executions/" not in execution:
            return "refused: not an execution name"
        try:
            response = self.client.post(f"{RUN_API}/{execution}:cancel", json={}, headers={"Authorization": "Bearer " + self.observer.token()})
        except httpx.HTTPError as exc:
            return f"error: {type(exc).__name__}"
        if response.status_code == 403:
            return "refused: the guard's role is not granted on this job (apply the foundation, then the application's root)"
        if response.status_code in (400, 409, 412):
            return "already finished"  # it ended between the listing and the cancel
        return "cancelled" if response.status_code < 300 else f"HTTP {response.status_code}"

    def snapshot(self):
        with self.lock:
            wakes = {f"{app} ({environment})": {key: value for key, value in entry.items() if key != "running"}
                     for (app, environment), entry in sorted(self.wakes.items())}
            return {"wakes": wakes, "cancels": list(self.cancels[:20])}
