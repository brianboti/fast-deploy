"""Read-only deployment observations. Provider reads never dispatch, approve, retry or promote.

Snapshots are shared across viewers and persisted separately from controller-owned plans.
Authorization is checked before every lookup, including cached observations and exports.
"""

import copy
import json
import re
import threading
import time
import types
from urllib.parse import urlparse

from . import aftercare, journey
from .onboarding import STEPS
from .status import TERMINAL, advice, release_phases, summarize

POLL_SECONDS = 10
HEALTH_SECONDS = 30
TYPICAL_SECONDS = 300
ATTENTION = {"failed", "timed_out", "awaiting_approval", "completed_unverified", "dispatch_lost"}
SETUP_LABELS = dict(
    zip(
        STEPS,
        (
            "Check the repository",
            "Prepare app settings",
            "Review app settings",
            "Register the app",
            "Prepare cloud resources",
            "Check the cloud changes",
            "Create cloud resources",
            "Connect the deployment account",
            "Prepare deployment workflow",
            "Review the workflow",
            "Start the first deployment",
            "Setup finished",
        ),
        strict=True,
    )
)
LABELS = {
    "planned": "Ready to deploy",
    "dispatching": "Starting deployment",
    "dispatched": "Starting deployment",
    "dispatch_unknown": "Checking whether deployment started",
    "awaiting_workflow": "Waiting for GitHub",
    "queued": "Waiting for a runner",
    "running": "Deployment in progress",
    "awaiting_approval": "Approval needed",
    "succeeded": "Deployment succeeded",
    "failed": "Deployment failed",
    "cancelled": "Deployment cancelled",
    "timed_out": "Deployment timed out",
    "completed_unverified": "Result needs verification",
    "dispatch_lost": "Deployment could not be located",
    "blocked": "Setup needs attention",
    "complete": "Setup finished",
    "rolled_back": "Previous version restored",
}


def clean(value):
    """Bound displayed evidence and remove common credential forms; never serialize raw provider objects."""
    text = str(value or "")[:12000]
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted private key]", text, flags=re.S)
    text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[redacted]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|ya29\.[\w.-]+|sk-[\w-]{12,})", "[redacted]", text)
    text = re.sub(r"(?i)((?:password|secret|token|api[_-]?key|authorization)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", text)
    return text


def url(value):
    """Only ordinary HTTPS links, never executable schemes, embedded credentials or secret queries."""
    parsed = urlparse(str(value or ""))
    return parsed._replace(query="", fragment="").geturl() if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None


def stamp(value):
    return aftercare.parse_moment(value) if isinstance(value, str) else value


def step_state(item):
    conclusion = item.get("conclusion")
    if conclusion:
        return {"success": "done", "skipped": "skipped", "failure": "failed", "cancelled": "cancelled", "timed_out": "failed"}.get(conclusion, "unknown")
    return "running" if item.get("status") == "in_progress" else "pending"


class Deployments:
    def __init__(self, controller, clock=time.time):
        self.c, self.clock = controller, clock
        self.lock = threading.RLock()  # the read budget and the per-plan locks only, never held across a provider call
        self.attempts, self.locks, self.typicals = {}, {}, {}
        with controller.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS deployment_observations (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def allowed(self, principal, application):
        return "*" in principal.applications or application in principal.applications

    def plan(self, principal, plan_id):
        found = self.c.store.fetch(plan_id)
        if found is None or not self.allowed(principal, found[1]["application"]):
            raise ValueError("Deployment not found or not available to your account.")
        return found

    def saved(self, key):
        with self.c.store.connect() as db:
            row = db.execute("SELECT payload FROM deployment_observations WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def saved_many(self, keys):
        """Every saved observation of these plans in one query, by plan id."""
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)  # only placeholders are interpolated; every value is bound
        with self.c.store.connect() as db:
            rows = db.execute(f"SELECT id, payload FROM deployment_observations WHERE id IN ({marks})", tuple(keys)).fetchall()  # noqa: S608
        return {key: json.loads(payload) for key, payload in rows}

    def save(self, key, value):
        with self.c.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO deployment_observations VALUES (?, ?)", (key, json.dumps(value)))

    def due(self, key, now):
        """Whether this plan's shared 10-second read budget allows a read now; taken when it does."""
        with self.lock:
            if now - self.attempts.get(key, 0) < POLL_SECONDS:
                return False
            self.attempts[key] = now
            # Bound the in-memory maps; persisted evidence is retained with deployment history.
            if len(self.attempts) > 2000:
                self.attempts = {k: v for k, v in self.attempts.items() if now - v < 300}
                self.locks = {k: v for k, v in self.locks.items() if k in self.attempts or v.locked()}
            return True

    def observe(self, plan):
        """A shared 10-second read budget; failures retain the last successful evidence and its age. One plan's reads never
        wait on another's: only that plan's own lock is held across GitHub and Cloud Run."""
        key = plan["id"]
        with self.lock:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            now = self.clock()
            saved = self.saved(key)
            if not self.due(key, now):
                return saved
            before = json.dumps(saved, sort_keys=True)
            if plan["state"] == "planned" or plan["state"] in TERMINAL:
                # Nothing to read on GitHub: the controller recorded the run's final evidence with the plan (Controller.refresh),
                # and it wins over an older running snapshot.
                saved.pop("workflow", None)
                saved.pop("error", None)
            else:
                try:
                    app = self.c.config.applications.get(plan["application"])
                    if app is None:
                        raise ValueError("Application is no longer configured")
                    workflow = self.c.github.status(app, plan)
                    summary = summarize(workflow, plan.get("checks") or {}, plan.get("kind"))
                    current = next((s.get("step") for s in summary.get("timeline") or [] if s.get("status") == "in_progress"), None)
                    signature = [summary["state"], current]
                    if signature != saved.get("signature"):
                        saved["transitions"] = (saved.get("transitions", []) + [{"at": now, "text": clean(current or LABELS.get(summary["state"], summary["state"]))}])[-100:]
                    saved.update(signature=signature, state=summary["state"])
                    saved.update(workflow=self.workflow(workflow), observed_at=now)
                    saved.pop("error", None)
                except Exception:  # noqa: BLE001 - provider outages are status, never a reason to lose the last observation
                    saved["error"] = "GitHub could not be reached. The last known progress is shown; deployment may still be running."
            target = plan.get("target") or {}
            # While a run goes on, Cloud Run is read as often as GitHub: the switch of visitors is what people watch for.
            cadence = POLL_SECONDS if plan["state"] not in TERMINAL else HEALTH_SECONDS
            if self.c.observer is not None and target and now - saved.get("health_attempt_at", 0) >= cadence:
                saved["health_attempt_at"] = now
                try:
                    service = self.c.observer.service(types.SimpleNamespace(**target))
                    saved["health"] = {
                        "ready": service.get("ready"),
                        "url": url(service.get("url")),
                        "at": now,
                        "updated_at": stamp(service.get("updated_at")),
                        "traffic": [{"revision": clean(t.get("revision")), "percent": t.get("percent"), "plan_id": clean(t.get("plan_id"))}
                                    for t in service.get("traffic") or []],
                    }
                    if plan["state"] not in TERMINAL:
                        saved["cloud"] = journey.cloud_evidence(saved.get("cloud"), key, saved["health"], now)
                    saved.pop("health_error", None)
                except Exception:  # noqa: BLE001 - health has its own independent freshness and failure state
                    saved["health_error"] = "Current Cloud Run status is unavailable."
            if json.dumps(saved, sort_keys=True) != before:
                self.save(key, saved)
            return saved

    @staticmethod
    def workflow(workflow):
        result = {k: workflow.get(k) for k in ("id", "status", "conclusion", "created_at", "run_started_at", "updated_at", "run_attempt")}
        result["html_url"] = url(workflow.get("html_url"))
        result["jobs"] = [
            {
                "name": clean(job.get("name")),
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "html_url": url(job.get("html_url")),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
                "steps": [{k: clean(s.get(k)) if k == "name" else s.get(k) for k in ("name", "status", "conclusion", "started_at", "completed_at")} for s in job.get("steps") or []],
            }
            for job in workflow.get("jobs") or []
        ]
        result["pending"] = [{"environment": clean(p.get("environment")), "reviewers": [clean(r) for r in p.get("reviewers") or []]} for p in workflow.get("pending") or []]
        return result

    def detail(self, principal, plan_id=None, application=None):
        setup = None
        if application:
            if not self.allowed(principal, application):
                raise ValueError("Deployment not found or not available to your account.")
            setup = self.c.store.onboarding(application)
            if setup is None:
                raise ValueError("No setup record was found for this app.")
            plan_id = setup.get("plan_id")
            if not plan_id:
                return self.setup(setup)
        owner, plan = self.plan(principal, plan_id)
        observed = self.observe(plan)
        if plan["state"] in TERMINAL:
            observed = {k: v for k, v in observed.items() if k not in {"workflow", "error"}}
        workflow = observed.get("workflow") or plan.get("workflow") or {}
        state = plan["state"]
        evidence = {"checks": plan.get("checks") or {}, "timeline": plan.get("timeline") or []}
        if observed.get("workflow"):
            evidence = summarize(workflow, evidence["checks"], plan.get("kind"))
            state = evidence["state"]
        receipt = plan.get("receipt") or {}
        if plan.get("rolled_back"):
            state = "rolled_back"
        now = self.clock()
        start = stamp(workflow.get("run_started_at") or workflow.get("created_at")) or aftercare.created(plan)
        finished = stamp((plan.get("outcome") or {}).get("completed_at"))
        if workflow.get("status") == "completed":
            finished = stamp(workflow.get("updated_at")) or finished
        groups = []
        for job in workflow.get("jobs") or []:
            steps = [{"name": clean(s.get("name")), "state": step_state(s), "started_at": stamp(s.get("started_at")), "finished_at": stamp(s.get("completed_at"))} for s in job.get("steps") or []]
            groups.append({"name": clean(job.get("name")), "state": step_state(job), "steps": steps, "url": url(job.get("html_url"))})
        if not groups and evidence.get("timeline"):
            by_job = {}
            for step in evidence["timeline"]:
                name = clean(step.get("job") or "Recorded workflow steps")
                by_job.setdefault(name, []).append({"name": clean(step.get("step")), "state": step_state(step), "seconds": step.get("seconds")})
            for name, items in by_job.items():
                states = {item["state"] for item in items}
                status = "failed" if "failed" in states else "done" if states <= {"done", "skipped"} else "unknown"
                groups.append({"name": name, "state": status, "url": None, "steps": items})
        current = next((s["name"] for g in groups for s in g["steps"] if s["state"] == "running"), None)
        kind = plan.get("kind") or "release"
        phases = safe_tree(release_phases(receipt))
        bake = int(plan.get("bake_seconds") if plan.get("bake_seconds") is not None else 300)
        stages = journey.stages(groups, observed.get("cloud"), phases, bake, state, now) if kind == "release" else None
        target = plan.get("target") or {}
        events = [{"at": e["at"], "text": LABELS.get(e["event"], clean(e["event"]))} for e in self.c.store.events(owner, plan["id"])]
        events = sorted(events + observed.get("transitions", []), key=lambda item: item["at"])[-100:]
        result = {
            "id": plan["id"],
            "application": plan["application"],
            "state": state,
            "label": LABELS.get(state, clean(state)),
            "kind": kind,
            "repository": plan.get("repository"),
            "sha": plan.get("sha"),
            "message": clean((plan.get("commit") or {}).get("message")),
            "owner": owner,
            "project": target.get("project_id"),
            "region": target.get("region"),
            "environment": "Production" if plan.get("kind") in {"promotion", "production_rollback"} else "Test environment",
            "started_at": start,
            "finished_at": finished,
            "now": now,
            "observed_at": max(observed.get("observed_at") or 0, (plan.get("outcome") or {}).get("recorded_at") or 0) or None,
            "error": observed.get("error"),
            "current": current,
            "groups": groups,
            "checks": evidence["checks"],
            "run_url": url(workflow.get("html_url") or (plan.get("outcome") or {}).get("run_url")),
            "app_url": url(receipt.get("url")),
            "health": observed.get("health"),
            "health_error": observed.get("health_error"),
            "observer": self.c.observer is not None,
            "pending": workflow.get("pending") or [],
            "events": events,
            "next_action": clean(advice({**plan, **evidence, "state": state})),
            "phases": phases,
            "stages": stages,
            "cloud": observed.get("cloud") or {},
            "bake_seconds": bake,
            "typical": self.typical(plan["application"]) if stages else None,
            "report": safe_tree(plan.get("release_report")),
            "recovery": safe_tree(plan.get("recovery_run")),
            # Ended once the controller has recorded it: a run GitHub reports finished is still followed until the receipt and the
            # checks are in (the follow job records them within a minute), so the page never stops on "confirming".
            "terminal": plan["state"] in TERMINAL - {"completed_unverified"} or bool(plan.get("rolled_back")),
            "setup": self.setup(setup) if setup else None,
        }
        return result

    def typical(self, application):
        """How long this application's successful releases usually take, from GitHub's start to the recorded end; worked out
        again every five minutes at most, from at most its eight newest."""
        with self.lock:
            cached = self.typicals.get(application)
            if cached and self.clock() - cached[0] < TYPICAL_SECONDS:
                return cached[1]
        durations = []
        for summary in self.c.store.plans(applications=[application], limit=60):
            if summary["kind"] != "release" or summary["state"] != "succeeded":
                continue
            found = self.c.store.fetch(summary["id"])
            plan = found[1] if found else {}
            workflow = plan.get("workflow") or {}
            began = stamp(workflow.get("run_started_at") or workflow.get("created_at"))
            ended = stamp((plan.get("outcome") or {}).get("completed_at"))
            if began and ended and ended > began:
                durations.append(ended - began)
            if len(durations) == 8:
                break
        value = journey.typical(durations)
        with self.lock:
            self.typicals[application] = (self.clock(), value)
        return value

    def setup(self, record):
        state = record["state"]
        current = record.get("blocked_at") if state == "blocked" else state
        position = STEPS.index(current) if current in STEPS else 0
        steps = [
            {"name": SETUP_LABELS[s], "state": "done" if index < position else "failed" if index == position and state == "blocked" else "running" if index == position else "pending"}
            for index, s in enumerate(STEPS[:-1])
        ]
        if state == "complete":
            for step in steps:
                step["state"] = "done"
        group = "failed" if state == "blocked" else "done" if state == "complete" else "running"
        links = [{"name": SETUP_LABELS.get(key, key), "url": url(value.get("url"))} for key, value in record.get("steps", {}).items() if isinstance(value, dict) and url(value.get("url"))]
        waiting = (record.get("steps", {}).get(current) or {}).get("waiting")
        return {
            "application": record["application"],
            "id": None,
            "state": state,
            "label": LABELS.get(state, "Setting up your app"),
            "kind": "setup",
            "repository": record.get("repository"),
            "owner": record.get("principal"),
            "environment": "Test environment",
            "started_at": record.get("created_at"),
            "finished_at": record.get("completed_at"),
            "now": self.clock(),
            "observed_at": record.get("updated_at"),
            "current": SETUP_LABELS.get(current),
            "error": None,
            "next_action": clean(record.get("error") or waiting or "Setup continues in the background. Status is recorded about once a minute."),
            "groups": [{"name": "First-time setup", "state": group, "steps": steps}],
            "links": links,
            "terminal": state in {"blocked", "complete"},
            "checks": {},
            "events": [],
            "observer": False,
        }

    def listing(self, principal):
        plans = self.c.store.plans(applications=self.c.visible_apps(principal), limit=100)
        observations = self.saved_many([plan["id"] for plan in plans])
        now = self.clock()
        rows = []
        for plan in plans:
            saved = observations.get(plan["id"], {})
            state = plan["state"]
            if state not in TERMINAL and saved.get("state"):
                state = saved["state"]
            rows.append(
                {**plan, "label": LABELS.get(state, state), "state": state, "summary": clean(plan.get("summary")), "error": saved.get("error"), "href": f"/settings?tab=deployments&plan={plan['id']}"}
            )
        setups = [self.setup(r) for r in self.c.store.onboardings() if self.allowed(principal, r["application"]) and not r.get("plan_id")]
        # In progress: runs not ended, plans not yet expired, setups still going. A run that ended unverified, a failed one or
        # a blocked setup needs attention instead; a run waiting for approval is both.
        active = sum(r["state"] not in TERMINAL and not (r["state"] == "planned" and r["expires_at"] < now) for r in rows)
        active += sum(s["state"] not in {"blocked", "complete"} for s in setups)
        attention = sum(r["state"] in ATTENTION for r in rows) + sum(s["state"] == "blocked" for s in setups)
        return {"rows": rows, "setups": setups, "now": now, "counts": {"active": active, "attention": attention}}


def safe_tree(value):
    """Only receipt/report evidence selected above, with strings bounded/redacted recursively."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(k): "[redacted]" if re.search(r"(?i)token|secret|password|credential", k) else safe_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_tree(v) for v in value[:100]]
    return copy.deepcopy(value)
