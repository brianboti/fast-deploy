import contextlib
import json
import re
import sqlite3
import time
import uuid

from .analyzer import FILES, analyze
from .cost import estimate, findings
from .models import enforce, parse_spec, spec_hash
from .status import TERMINAL, advice, release_phases, summarize

DISPATCHED = {"dispatched", "dispatch_unknown", "dispatching"}
WAIT_LIMIT = 45  # seconds; keeps a waiting status call inside the client's tool timeout


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (at REAL, owner TEXT, plan_id TEXT, event TEXT)")

    @contextlib.contextmanager
    def connect(self):
        # Commit or roll back exactly like sqlite3's own context manager, then always close the connection.
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, owner, plan):
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?, ?, 'planned', ?)", (plan["id"], owner, json.dumps(plan)))
            db.execute("INSERT INTO audit VALUES (?, ?, ?, 'planned')", (time.time(), owner, plan["id"]))

    def get(self, owner, plan_id):
        with self.connect() as db:
            row = db.execute("SELECT state,payload FROM plans WHERE id=? AND owner=?", (plan_id, owner)).fetchone()
        if row is None:
            raise ValueError("Plan not found for this identity")
        return {**json.loads(row[1]), "state": row[0]}

    def claim(self, owner, plan_id):
        with self.connect() as db:
            changed = db.execute("UPDATE plans SET state='dispatching' WHERE id=? AND owner=? AND state='planned'", (plan_id, owner)).rowcount
            if changed:
                db.execute("INSERT INTO audit VALUES (?, ?, ?, 'dispatching')", (time.time(), owner, plan_id))
        return bool(changed)

    def finish(self, owner, plan, state):
        with self.connect() as db:
            db.execute("UPDATE plans SET state=?,payload=? WHERE id=? AND owner=?", (state, json.dumps(plan), plan["id"], owner))
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))

    def plans(self, owner, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT state, payload FROM plans WHERE owner=? ORDER BY rowid DESC LIMIT ?", (owner, limit)).fetchall()
        summaries = []
        for state, payload in rows:
            plan = json.loads(payload)
            receipt = plan.get("receipt") or {}
            summaries.append({"id": plan["id"], "application": plan["application"], "sha": plan["sha"], "expires_at": plan["expires_at"], "state": state,
                              "summary": (plan.get("commit") or {}).get("message"), "completed_at": (plan.get("outcome") or {}).get("completed_at"),
                              "revision": receipt.get("revision") if state == "succeeded" else None})
        return summaries

    def latest_success(self, application):
        """The newest plan whose release succeeded for an application, across every operator; the controller's view of what is live."""
        with self.connect() as db:
            row = db.execute("SELECT payload FROM plans WHERE state='succeeded' AND json_extract(payload, '$.application')=? ORDER BY rowid DESC LIMIT 1",
                             (application,)).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, owner, plan_id):
        with self.connect() as db:
            rows = db.execute("SELECT at, event FROM audit WHERE owner=? AND plan_id=? ORDER BY at, rowid", (owner, plan_id)).fetchall()
        return [{"at": at, "event": event} for at, event in rows]


class Controller:
    def __init__(self, config, github, store):
        self.config, self.github, self.store = config, github, store

    def app(self, principal, app_id, write=False):
        if app_id not in principal.applications or app_id not in self.config.applications:
            raise ValueError("Application is not authorized for this identity")
        if write and not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        return self.config.applications[app_id]

    def inspect(self, principal, app_id):
        import httpx
        app = self.app(principal, app_id)
        sha = self.github.head(app)
        files = {}
        for filename in FILES:
            try:
                files[filename] = self.github.file(app, sha, filename)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        return {"commit_sha": sha, **analyze(files, app.service)}

    def spec_at(self, app, sha):
        """The reviewed specification at a commit, with a plain explanation when the app has none yet."""
        import httpx
        try:
            text = self.github.file(app, sha, "platform.yaml")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            raise ValueError(f"{app.repository} has no platform.yaml on {app.branch}; run propose_configuration to draft one, review it, "
                             "set reviewed: true and merge it through the normal process") from None
        return parse_spec(text)

    def describe_head(self, app):
        """Commit facts for the branch tip; adapters without commit metadata still give the SHA."""
        describe = getattr(self.github, "commit", None)
        return describe(app) if callable(describe) else {"sha": self.github.head(app)}

    def plan(self, principal, app_id, cloud="gcp", environment="staging"):
        app = self.app(principal, app_id)
        commit = self.describe_head(app)
        sha = commit["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("Expected full Git commit SHA")
        spec = self.spec_at(app, sha)
        enforce(spec, cloud, environment)
        if spec.name != app.service:
            raise ValueError("Spec service name does not match operator registry")
        cost = self.cost_policy(app, spec)
        plan = {
            "id": uuid.uuid4().hex, "application": app_id, "repository": app.repository,
            "branch": app.branch, "sha": sha, "commit": commit, "spec_hash": spec_hash(spec),
            "spec": spec.model_dump(), "cloud": cloud, "environment": environment,
            "target": app.target.model_dump(),
            "expires_at": time.time() + 900,
            "cost": cost,
            "checks": {"config_policy": "passed", "tests": "pending_in_ci", "dependency_scan": "pending_in_ci", "health": "pending_after_deployment"},
        }
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    def propose_config(self, principal, app_id):
        import httpx
        app = self.app(principal, app_id, write=True)
        report = self.inspect(principal, app_id)
        try:
            self.github.file(app, report["commit_sha"], "platform.yaml")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        else:
            raise ValueError("platform.yaml already exists; edit it through a normal reviewed PR")
        if not report["supported_candidate"]:
            raise ValueError("No supported Python manifest found")
        if not self.config.live_dispatch:
            return {**report, "pull_request": "disabled"}
        return self.github.propose_config(app, report["commit_sha"], report["draft_spec"], report["review_required"])

    def plans(self, principal):
        return {"plans": self.store.plans(principal.name)}

    def applications(self, principal):
        """Registered apps this identity may see, each with the live release this controller recorded and what main holds beyond it."""
        result = {}
        for app_id in principal.applications:
            app = self.config.applications[app_id]
            entry = app.model_dump()
            live = self.store.latest_success(app_id)
            if live is None:
                entry["live_release"] = None
                entry["undeployed_commits"] = None
                entry["note"] = "No release recorded by this controller yet."
            else:
                receipt = live.get("receipt") or {}
                entry["live_release"] = {"plan_id": live["id"], "sha": live["sha"], "commit": live.get("commit"), "revision": receipt.get("revision"),
                                         "url": receipt.get("url"), "completed_at": (live.get("outcome") or {}).get("completed_at")}
                compare = getattr(self.github, "compare", None)
                entry["undeployed_commits"] = compare(app, live["sha"]) if callable(compare) else None
            result[app_id] = entry
        return result

    def cost_summary(self, app, spec, requests_per_month=None, average_request_seconds=None):
        """Bounds and findings from list prices and the reviewed spec, with the operator's ceiling applied."""
        summary = estimate(spec, requests_per_month, average_request_seconds)
        summary["findings"] = findings(spec)
        limit = app.cost_ceiling_usd
        over = limit is not None and summary["compute_ceiling_usd_month"] > limit
        summary["ceiling_policy"] = {"limit_usd_month": limit, "status": "not_configured" if limit is None else ("exceeds_limit" if over else "within_limit")}
        summary["note"] = ("Bounds from list prices and the reviewed configuration, not a bill forecast. "
                           "Measured right-sizing comes from the rightsizing workflow report (get_cost_report).")
        return summary

    def cost_policy(self, app, spec):
        summary = self.cost_summary(app, spec)
        if summary["ceiling_policy"]["status"] == "exceeds_limit":
            raise ValueError(f"Cost policy: worst-case compute of ${summary['compute_ceiling_usd_month']:.2f}/month exceeds the operator ceiling "
                             f"of ${app.cost_ceiling_usd:.2f}/month; lower scaling.maximum or resources, or raise cost_ceiling_usd in configuration")
        return summary

    def cost(self, principal, app_id, requests_per_month=None, average_request_seconds=None):
        app = self.app(principal, app_id)
        sha = self.github.head(app)
        spec = self.spec_at(app, sha)
        return {"commit_sha": sha, "spec_hash": spec_hash(spec), "reviewed": spec.reviewed,
                **self.cost_summary(app, spec, requests_per_month, average_request_seconds)}

    def cost_report(self, principal, app_id):
        app = self.app(principal, app_id)
        return self.github.cost_report(app)

    def deploy(self, principal, plan_id):
        plan = self.store.get(principal.name, plan_id)
        app = self.app(principal, plan["application"], write=True)
        if plan["state"] != "planned":
            return plan
        if not self.config.live_dispatch:
            return {**plan, "dispatch": "disabled", "note": "Operator must configure GitHub/GCP and enable live_dispatch."}
        if plan["expires_at"] < time.time():
            raise ValueError("Plan expired; create a new plan")
        if plan["repository"] != app.repository or plan["branch"] != app.branch or plan.get("target") != app.target.model_dump():
            raise ValueError("Registry changed; create a new plan")
        if self.github.head(app) != plan["sha"]:
            raise ValueError("Branch moved; create a new plan")
        spec = self.spec_at(app, plan["sha"])
        enforce(spec, plan["cloud"], plan["environment"])
        if spec_hash(spec) != plan["spec_hash"] or spec.name != app.service:
            raise ValueError("Plan mismatch")
        self.cost_policy(app, spec)
        if not self.store.claim(principal.name, plan_id):
            return self.store.get(principal.name, plan_id)
        try:
            result = self.github.dispatch(app, plan)
        except Exception:
            # Network failure can occur after GitHub accepted the dispatch: no blind retry.
            self.store.finish(principal.name, plan, "dispatch_unknown")
            raise ValueError("Dispatch outcome unknown. Inspect GitHub before requesting another deployment.") from None
        plan["dispatch_result"] = result
        self.store.finish(principal.name, plan, "dispatched")
        return self.store.get(principal.name, plan_id)

    def status(self, principal, plan_id, wait_seconds=0):
        """Current state of a plan; with wait_seconds it polls until the state changes, the run ends, or the wait is over."""
        wait = min(max(int(wait_seconds or 0), 0), WAIT_LIMIT)
        deadline = time.time() + wait
        first = None
        while True:
            plan = self.refresh(principal, plan_id)
            first = first if first is not None else plan["state"]
            if plan["state"] in TERMINAL or plan["state"] != first or time.time() >= deadline:
                return plan
            time.sleep(min(5, max(0.0, deadline - time.time())))

    def refresh(self, principal, plan_id):
        plan = self.store.get(principal.name, plan_id)
        app = self.app(principal, plan["application"])
        if plan["state"] in DISPATCHED:
            workflow = self.github.status(app, plan)
            plan["dispatch_state"] = plan["state"]
            plan["workflow"] = workflow
            plan.update(summarize(workflow, plan["checks"]))
            if plan["state"] in TERMINAL:
                self.record_outcome(principal, app, plan, workflow)
        elif plan["state"] in {"failed", "cancelled", "timed_out"} and not (plan.get("recovery_run") or {}).get("conclusion"):
            self.record_recovery(principal, app, plan)
        plan["phases"] = release_phases(plan.get("receipt"))
        plan["events"] = self.store.events(principal.name, plan_id)
        plan["next_action"] = advice(plan)
        return plan

    def record_outcome(self, principal, app, plan, workflow):
        """A finished run is stored once with its receipt, so history survives GitHub's artifact retention."""
        fetch = getattr(self.github, "release_receipt", None)
        receipts = fetch(app, workflow["id"]) if callable(fetch) and workflow.get("id") else None
        plan["receipt"] = (receipts or {}).get("release")
        plan["vulnerabilities"] = (receipts or {}).get("vulnerabilities")
        plan["outcome"] = {"state": plan["state"], "conclusion": workflow.get("conclusion"), "run_url": workflow.get("html_url"),
                           "completed_at": workflow.get("updated_at"), "recorded_at": time.time()}
        plan["workflow"] = {key: workflow.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt", "created_at", "updated_at")}
        if plan["state"] in {"failed", "cancelled", "timed_out"}:
            self.record_recovery(principal, app, plan, persist=False)
        self.persist(principal, plan)

    def record_recovery(self, principal, app, plan, persist=True):
        lookup = getattr(self.github, "recovery_after", None)
        if not callable(lookup) or not (plan.get("workflow") or {}).get("id"):
            return
        plan["recovery_run"] = lookup(app, plan["workflow"])
        if persist and (plan["recovery_run"] or {}).get("conclusion"):
            self.persist(principal, plan)

    def persist(self, principal, plan):
        record = {key: value for key, value in plan.items() if key not in {"events", "phases", "next_action"}}
        self.store.finish(principal.name, record, plan["state"])

