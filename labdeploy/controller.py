import contextlib
import datetime
import json
import re
import sqlite3
import time
import uuid

from .analyzer import FILES, analyze
from .config import Principal
from .cost import estimate, findings
from .github import explain
from .infra import BOOTSTRAP_IMAGE, app_root
from .models import enforce, parse_spec, spec_hash
from .rightsize import SETTINGS, apply_settings
from .status import TERMINAL, advice, release_phases, summarize
from .workflows import TEMPLATES, pinned_commit, render

DISPATCHED = {"dispatched", "dispatch_unknown", "dispatching"}
WAIT_LIMIT = 45  # seconds; keeps a waiting status call inside the client's tool timeout


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (at REAL, owner TEXT, plan_id TEXT, event TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS events (at REAL NOT NULL, identity TEXT, client TEXT, kind TEXT NOT NULL, name TEXT, application TEXT, "
                       "outcome TEXT, duration_ms INTEGER, detail TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS events_at ON events (at)")

    @contextlib.contextmanager
    def connect(self):
        # Commit or roll back exactly like sqlite3's own context manager, then always close the connection.
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, owner, plan, state="planned"):
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?, ?, ?, ?)", (plan["id"], owner, state, json.dumps(plan)))
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))

    def mark(self, plan_id, **fields):
        """Annotate a stored plan without changing its state, for example when a later rollback supersedes it."""
        with self.connect() as db:
            row = db.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                return
            payload = {**json.loads(row[0]), **fields}
            db.execute("UPDATE plans SET payload=? WHERE id=?", (json.dumps(payload), plan_id))

    def get(self, owner, plan_id):
        with self.connect() as db:
            row = db.execute("SELECT state,payload FROM plans WHERE id=? AND owner=?", (plan_id, owner)).fetchone()
        if row is None:
            raise ValueError("Plan not found for this identity")
        return {**json.loads(row[1]), "state": row[0]}

    def fetch(self, plan_id):
        """A plan by id regardless of who created it, with its owner; None when unknown."""
        with self.connect() as db:
            row = db.execute("SELECT owner, state, payload FROM plans WHERE id=?", (plan_id,)).fetchone()
        return (row[0], {**json.loads(row[2]), "state": row[1]}) if row else None

    def claim(self, owner, plan_id):
        with self.connect() as db:
            changed = db.execute("UPDATE plans SET state='dispatching' WHERE id=? AND owner=? AND state='planned'", (plan_id, owner)).rowcount
            if changed:
                db.execute("INSERT INTO audit VALUES (?, ?, ?, 'dispatching')", (time.time(), owner, plan_id))
        return bool(changed)

    def finish(self, owner, plan, state):
        with self.connect() as db:
            before = db.execute("SELECT state FROM plans WHERE id=? AND owner=?", (plan["id"], owner)).fetchone()
            db.execute("UPDATE plans SET state=?,payload=? WHERE id=? AND owner=?", (state, json.dumps(plan), plan["id"], owner))
            if before is None or before[0] != state:  # re-saving a record with more detail is not a new event
                db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))

    def plans(self, owner=None, applications=None, limit=100):
        """Plan summaries, newest first: one identity's own plans, or every plan of the given applications."""
        with self.connect() as db:
            if applications is not None:
                marks = ",".join("?" for _ in applications) or "''"  # only placeholders are interpolated; every value is bound
                query = f"SELECT owner, state, payload FROM plans WHERE json_extract(payload, '$.application') IN ({marks}) ORDER BY rowid DESC LIMIT ?"  # noqa: S608
                rows = db.execute(query, (*applications, limit)).fetchall()
            else:
                rows = db.execute("SELECT owner, state, payload FROM plans WHERE owner=? ORDER BY rowid DESC LIMIT ?", (owner, limit)).fetchall()
        summaries = []
        for plan_owner, state, payload in rows:
            plan = json.loads(payload)
            receipt = plan.get("receipt") or {}
            summaries.append({"id": plan["id"], "application": plan["application"], "sha": plan["sha"], "expires_at": plan["expires_at"], "state": state,
                              "owner": plan_owner, "kind": plan.get("kind") or "release",
                              "summary": (plan.get("commit") or {}).get("message"), "completed_at": (plan.get("outcome") or {}).get("completed_at"),
                              "revision": receipt.get("revision") if state == "succeeded" else None})
        return summaries

    def latest_plan(self, application, sha=None):
        """The newest plan of any state for an application, optionally for one commit; rollback records excluded."""
        query = "SELECT state, payload FROM plans WHERE json_extract(payload, '$.application')=? AND json_extract(payload, '$.kind') IS NULL"
        params = [application]
        if sha:
            query += " AND json_extract(payload, '$.sha')=?"
            params.append(sha)
        with self.connect() as db:
            row = db.execute(query + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
        return {**json.loads(row[1]), "state": row[0]} if row else None

    def latest_success(self, application, sha=None, include_rolled_back=False):
        """The newest release that succeeded for an application, by default excluding ones rolled back since (optionally for one commit)."""
        query = "SELECT payload FROM plans WHERE state='succeeded' AND json_extract(payload, '$.application')=? AND json_extract(payload, '$.kind') IS NULL"
        params = [application]
        if not include_rolled_back:
            query += " AND json_extract(payload, '$.rolled_back') IS NULL"
        if sha:
            query += " AND json_extract(payload, '$.sha')=?"
            params.append(sha)
        with self.connect() as db:
            row = db.execute(query + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, owner, plan_id):
        with self.connect() as db:
            rows = db.execute("SELECT at, event FROM audit WHERE owner=? AND plan_id=? ORDER BY at, rowid", (owner, plan_id)).fetchall()
        return [{"at": at, "event": event} for at, event in rows]

    def record_event(self, kind, name=None, identity=None, client=None, application=None, outcome=None, duration_ms=None, detail=None):
        """One line of the audit trail: tool calls, REST calls, sign-ins, throttling, token reuse. Kept for a year."""
        with self.connect() as db:
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (time.time(), identity, client, kind, name, application, outcome, duration_ms, (detail or "")[:500] or None))
            db.execute("DELETE FROM events WHERE at < ?", (time.time() - 365 * 86_400,))

    def recent_events(self, limit=50, identity=None, application=None):
        query, params = "SELECT at, identity, client, kind, name, application, outcome, duration_ms, detail FROM events", []
        clauses = []
        if identity is not None:
            clauses.append("identity=?")
            params.append(identity)
        if application is not None:
            clauses.append("application=?")
            params.append(application)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*params, limit)).fetchall()
        keys = ("at", "identity", "client", "kind", "name", "application", "outcome", "duration_ms", "detail")
        return [dict(zip(keys, row, strict=True)) for row in rows]


class Controller:
    def __init__(self, config, github, store, observer=None):
        self.config, self.github, self.store, self.observer = config, github, store, observer

    AUTO_DEPLOY_IDENTITY = "auto-deploy"

    def auto_deploy_tick(self):
        """One pass: for every app with auto_deploy, release the tip of main when it is neither live nor already attempted."""
        if self.config.paused:
            return {"paused": True}
        results = {}
        for app_id, app in self.config.applications.items():
            if not app.auto_deploy:
                continue
            if app.paused:
                results[app_id] = "paused"
                continue
            try:
                head = self.github.head(app)
                live = self.store.latest_success(app_id)
                if live and live["sha"] == head:
                    results[app_id] = "up to date"
                    continue
                attempted = self.store.latest_plan(app_id, head)
                if attempted:
                    results[app_id] = f"commit {head[:12]} already handled by plan {attempted['id'][:8]} ({attempted['state']})"
                    continue
                principal = Principal(name=self.AUTO_DEPLOY_IDENTITY, token_sha256="0" * 64, applications=[app_id], can_deploy=True)
                plan = self.plan(principal, app_id)
                outcome = self.deploy(principal, plan["id"])
                state = "dispatch disabled" if outcome.get("dispatch") == "disabled" else outcome.get("state")
                self.store.record_event("auto_deploy", name=app_id, identity=self.AUTO_DEPLOY_IDENTITY, application=app_id, outcome=state, detail=f"{head[:12]} plan {plan['id'][:8]}")
                results[app_id] = f"{state}: plan {plan['id']} for commit {head[:12]}"
            except Exception as exc:  # noqa: BLE001 - one app's trouble must not stop the others; the trail keeps the reason
                message = explain(exc) or str(exc)
                self.store.record_event("auto_deploy", name=app_id, identity=self.AUTO_DEPLOY_IDENTITY, application=app_id, outcome="error", detail=message)
                results[app_id] = f"error: {message}"
        return results

    def watchdog_tick(self):
        """The daily look around: configuration, credential, each app's schedules, pin and newest attempt. Posts findings to the webhook."""
        problems = []
        error = getattr(self.config, "error", None)
        if error:
            problems.append(f"configuration file could not be loaded, the previous one is in force: {error}")
        check = getattr(self.github, "credential_status", None)
        if callable(check):
            try:
                credential = check()
                if credential.get("warning"):
                    problems.append(credential["warning"])
            except Exception as exc:  # noqa: BLE001
                problems.append(f"GitHub credential check failed: {explain(exc) or exc}")
        for app_id, app in self.config.applications.items():
            try:
                for workflow, health in (self.schedule_health(app) or {}).items():
                    problems.extend(f"{app_id}: {workflow}: {problem}" for problem in health.get("problems", []))
                pin = self.platform_pin(app)
                if pin and pin.get("supported") is False:
                    problems.append(f"{app_id}: {pin['note']}")
                newest = self.store.latest_plan(app_id)
                if newest and newest.get("state") in {"failed", "cancelled", "timed_out", "dispatch_unknown"}:
                    problems.append(f"{app_id}: the newest release attempt is '{newest['state']}' (plan {newest['id'][:8]}); nothing newer has succeeded")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{app_id}: check failed: {explain(exc) or exc}")
        report = {"checked_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "problems": problems, "notified": False}
        self.store.record_event("watchdog", outcome="problems" if problems else "ok", detail="; ".join(problems) or None)
        if problems and self.config.notify_webhook_url:
            import httpx
            text = "Lab Deploy watchdog found " + str(len(problems)) + " problem(s):\n- " + "\n- ".join(problems)
            try:
                httpx.post(self.config.notify_webhook_url, json={"text": text, "content": text}, timeout=20).raise_for_status()
                report["notified"] = True
            except Exception as exc:  # noqa: BLE001
                report["notify_error"] = str(exc)[:200]
        return report

    def backup_tick(self):
        """A consistent copy of the SQLite database next to it (or in backup_dir); copies older than backup_keep_days go."""
        from pathlib import Path
        folder = Path(self.config.backup_dir or f"{self.config.database}.backups")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"deployments-{datetime.datetime.now(datetime.UTC):%Y%m%d-%H%M%S}.db"
        source, copy = sqlite3.connect(self.config.database), sqlite3.connect(destination)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        cutoff = time.time() - self.config.backup_keep_days * 86_400
        removed = []
        for old in folder.glob("deployments-*.db"):
            if old != destination and old.stat().st_mtime < cutoff:
                old.unlink()
                removed.append(old.name)
        self.store.record_event("backup", outcome="ok", detail=str(destination))
        return {"backup": str(destination), "removed": removed, "kept_days": self.config.backup_keep_days}

    def require_observer(self):
        if self.observer is None:
            raise ValueError("Live cloud views need observer.service_account in the controller configuration and the Terraform observer identity")
        return self.observer

    def service_health(self, principal, app_id, minutes=60):
        """What serves right now and how it behaves, from Cloud Run and Cloud Monitoring, next to what this controller recorded."""
        app = self.app(principal, app_id)
        observer = self.require_observer()
        service = observer.service(app.target)
        metrics = observer.metrics(app.target, max(5, min(int(minutes), 1440)))
        live = self.store.latest_success(app_id)
        serving = [t for t in service["traffic"] if t.get("percent")]
        recorded = ((live or {}).get("receipt") or {}).get("revision")
        agreement = None if live is None else any(t.get("revision") == recorded for t in serving)
        notes = []
        if not service["ready"]:
            notes.append(f"Cloud Run reports the service not ready: {service.get('ready_message') or 'see the console'}")
        if service.get("ingress") and service["ingress"] != "INGRESS_TRAFFIC_ALL":
            notes.append(f"Ingress is {service['ingress']}: the spending cap or an operator closed the service; reopen with gcloud run services update --ingress=all")
        if live is not None and not agreement:
            notes.append(f"The controller's live release is revision {recorded}, but Cloud Run serves {[t.get('revision') for t in serving]}; "
                         "someone changed traffic outside the pipeline, or a release is in progress")
        if metrics.get("server_errors"):
            notes.append(f"{metrics['server_errors']} server errors in the last {metrics['window_minutes']} minutes")
        return {"application": app_id, "service": service, "metrics": metrics, "recorded_live_release": live and {"plan_id": live["id"], "sha": live["sha"], "revision": recorded},
                "agreement": agreement, "notes": notes, "console_url": f"https://console.cloud.google.com/run/detail/{app.target.region}/{app.target.service}/metrics?project={app.target.project_id}"}

    def recent_logs(self, principal, app_id, minutes=30, severity="ERROR", limit=50):
        app = self.app(principal, app_id)
        lines = self.require_observer().logs(app.target, minutes, severity, limit)
        return {"application": app_id, "minutes": minutes, "severity": severity, "count": len(lines), "lines": lines}

    def app(self, principal, app_id, write=False):
        if app_id not in principal.applications or app_id not in self.config.applications:
            raise ValueError("Application is not authorized for this identity")
        if write and not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        return self.config.applications[app_id]

    def require_active(self, app_id):
        """Plans and dispatches stop while the operator has paused everything or this app; reads keep working."""
        if self.config.paused:
            raise ValueError("Deployments are paused by the operator (paused: true in the controller configuration); reads still work")
        if self.config.applications[app_id].paused:
            raise ValueError(f"Deployments of {app_id} are paused by the operator (paused: true on the application); reads still work")

    SCHEDULES = {"recover-gcp.yml": 2 * 86_400, "rightsize-gcp.yml": 8 * 86_400}  # staleness: six-hourly and weekly runs, with slack

    def schedule_health(self, app):
        """The two scheduled workflows per app: paused by GitHub, stale, or fine. GitHub disables schedules after sixty idle days."""
        check = getattr(self.github, "schedule_health", None)
        if not callable(check):
            return None
        result = {}
        for workflow, limit in self.SCHEDULES.items():
            health = check(app, workflow)
            last = health.get("last_scheduled_run")
            age = None
            if last:
                try:
                    age = time.time() - datetime.datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    age = None
            problems = []
            if health.get("state") and health["state"] != "active":
                problems.append(f"GitHub reports the workflow as {health['state']}; re-enable it in the Actions tab")
            if age is not None and age > limit:
                problems.append(f"last scheduled run was {int(age // 3600)} hours ago")
            if age is None:
                problems.append("no scheduled run recorded yet")
            if health.get("last_conclusion") not in (None, "success"):
                problems.append(f"last scheduled run concluded {health['last_conclusion']}")
            result[workflow] = {**health, "ok": not problems, "problems": problems}
        return result

    def platform_pin(self, app):
        """Which platform commit the app's workflows pin, and whether this controller supports it."""
        platform = self.config.platform
        if platform is None:
            return None
        pin = self.pinned_platform(app, platform.repository)
        result = {"commit": pin, "supported": None, "note": None}
        if pin is None:
            result.update(supported=False, note="The app has no deploy-gcp.yml pinned to this platform; run update_app_workflows.")
        elif platform.commit and pin == platform.commit:
            result.update(supported=True)
        elif platform.minimum_commit:
            if pin == platform.minimum_commit:
                result.update(supported=True)
            else:
                comparison = self.github.compare_repository(platform.repository, platform.minimum_commit, pin)
                result.update(supported=comparison.get("status") in {"ahead", "identical"})
                if not result["supported"]:
                    result["note"] = f"The app pins platform {pin[:12]}, older than the minimum {platform.minimum_commit[:12]} this controller supports; run update_app_workflows."
        else:
            result.update(note="No platform.commit or platform.minimum_commit is configured, so the pin is reported but not enforced.")
        return result

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

    def ancestor_of_branch(self, app, sha):
        """How far the branch tip is beyond a commit; raises when the commit is not part of the branch's history."""
        comparison = self.github.compare(app, sha)
        if comparison.get("status") not in {"ahead", "identical"}:
            raise ValueError(f"Commit {sha[:12]} is not part of {app.branch}'s history; only commits already merged to {app.branch} can be deployed")
        return comparison.get("ahead_by", 0)

    def plan(self, principal, app_id, cloud="gcp", environment="staging", commit_sha=None):
        app = self.app(principal, app_id)
        self.require_active(app_id)
        behind = 0
        if commit_sha:
            if not re.fullmatch(r"[a-f0-9]{40}", commit_sha):
                raise ValueError("Give the full 40-character commit SHA to deploy a specific commit")
            behind = self.ancestor_of_branch(app, commit_sha)
            describe = getattr(self.github, "commit", None)
            commit = describe(app, commit_sha) if callable(describe) else {"sha": commit_sha}
        else:
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
            "pinned_commit": bool(commit_sha), "behind_branch_by": behind,
            "warnings": [],
        }
        if spec.tests == "none":
            plan["checks"]["tests"] = "skipped_by_config"
            plan["warnings"].append("platform.yaml declares tests: none, so nothing checks this app's behaviour before a release; only the health probe guards it.")
        if spec.secrets:
            plan["warnings"].append("References secrets " + ", ".join(f"{app.service}-{name}" for name in spec.secrets) + "; each must exist in Secret Manager with an enabled version.")
        # A rolled-back release's image is still the attested image of that commit, so it counts for reuse.
        prior = self.store.latest_success(app_id, sha, include_rolled_back=True)
        image = ((prior or {}).get("receipt") or {}).get("image")
        prefix = f"{app.target.region}-docker.pkg.dev/{app.target.project_id}/{app.target.registry}/{app.target.service}@sha256:"
        if image and image.startswith(prefix):
            # The same commit was released before: reuse its attested image instead of building and testing again.
            plan["reuse_image"] = image
            plan["reused_from_plan"] = prior["id"]
            plan["checks"].update(tests="passed_in_earlier_release", dependency_scan="passed_in_earlier_release")
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    def propose_config(self, principal, app_id):
        import httpx
        app = self.app(principal, app_id, write=True)
        self.require_active(app_id)
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
        """Deployers see every plan of their applications, including auto-deploy's; read-only identities see their own."""
        if principal.can_deploy:
            return {"plans": self.store.plans(applications=list(principal.applications))}
        return {"plans": self.store.plans(principal.name)}

    def load(self, principal, plan_id):
        """A plan this identity may see, with the identity that created it: deployers may see any plan of their applications."""
        if principal.can_deploy:
            found = self.store.fetch(plan_id)
            if found is None or found[1].get("application") not in principal.applications:
                raise ValueError("Plan not found for this identity")
            return found
        return principal.name, self.store.get(principal.name, plan_id)

    def pinned_platform(self, app, repository):
        """The platform commit the app's deploy workflow pins today, or None when the workflow is absent."""
        found = self.github.contents(app.repository, app.branch, ".github/workflows/deploy-gcp.yml")
        return pinned_commit(found["text"], repository) if found else None

    def update_workflows(self, principal, app_id, platform_sha=None, allow_writes=False):
        """Regenerate the app's three workflows against a platform commit and open one pull request. Review and merge stay human."""
        app = self.app(principal, app_id, write=True)
        platform = self.config.platform
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before updating app workflows")
        if not allow_writes and not self.config.live_dispatch:
            raise ValueError("Writes are disabled (live_dispatch: false); enable them, or run the command on the controller host")
        sha = platform_sha or platform.commit or self.github.branch_head(platform.repository, platform.branch)
        if not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
            raise ValueError("The platform commit must be a full 40-character SHA")
        current = self.pinned_platform(app, platform.repository)
        if current == sha:
            return {"application": app_id, "platform_commit": sha, "current_pin": current, "pull_request": None, "note": "Already pinned to this platform commit."}
        files = {}
        for name in TEMPLATES:
            template = self.github.contents(platform.repository, sha, f"templates/{name}")
            if template is None:
                raise ValueError(f"templates/{name} does not exist at platform commit {sha[:12]}")
            files[f".github/workflows/{name}"] = render(template["text"], platform.repository, sha)
        return {"application": app_id, "platform_commit": sha, "current_pin": current, "pull_request": self.github.update_workflows(app, sha, files)}

    def render_infrastructure(self, principal, app_id, state_bucket=None, bootstrap_image=None, settings=None):
        """The app's Terraform root as the pipeline will plan it: literal inputs from the configuration, the reviewed spec and GitHub's ids."""
        app = self.app(principal, app_id, write=True)
        target = app.target
        ids = self.github.repository_ids(app.repository)
        note = None
        try:
            health_path = self.spec_at(app, self.github.head(app)).health_path
        except ValueError as exc:
            health_path, note = "/health", f"Defaults used until the app has a reviewed platform.yaml: {exc}"
        files = app_root(target.service, project_id=target.project_id, region=target.region, state_bucket=state_bucket or f"{target.project_id}-tfstate",
                         github_repository=app.repository, github_repository_id=ids["repository_id"], github_owner_id=ids["owner_id"],
                         bootstrap_image=bootstrap_image or BOOTSTRAP_IMAGE, health_path=health_path, settings=settings)
        return {"application": app_id, "service": target.service, "directory": f"infra/gcp/apps/{target.service}", "files": files, "note": note}

    def propose_infrastructure(self, principal, app_id, allow_writes=False, **render):
        """Open the pull request that adds or updates the app's root on the platform repository; the pipeline does the rest."""
        platform = self.config.platform
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before proposing infrastructure")
        if not allow_writes and not self.config.live_dispatch:
            raise ValueError("Writes are disabled (live_dispatch: false); enable them, or run `lab-deploy infra render` on the controller host")
        rendered = self.render_infrastructure(principal, app_id, **render)
        lock = self.github.contents(platform.repository, platform.branch, "infra/gcp/project/.terraform.lock.hcl")
        if lock is not None:  # every root pins the same provider build as the foundation
            rendered["files"][".terraform.lock.hcl"] = lock["text"]
        paths = {f"{rendered['directory']}/{name}": text for name, text in rendered["files"].items()}
        body = (f"Terraform root for `{rendered['service']}` rendered by the controller from the configuration, the app's reviewed "
                "platform.yaml and GitHub's immutable ids.\n\nThe infrastructure plan workflow posts what an apply would do; "
                "merging applies exactly that with the pipeline identity and publishes the outputs to the app's GitHub environment. "
                "Nothing is applied by this pull request itself.")
        pull_request = self.github.open_platform_pull_request(platform.repository, platform.branch, f"lab-deploy/infra-{rendered['service']}", paths,
                                                              f"Infrastructure for {rendered['service']}", body, f"Render the infrastructure root for {rendered['service']}")
        return {"application": app_id, "service": rendered["service"], "files": sorted(paths), "pull_request": pull_request, "note": rendered["note"]}

    def applications(self, principal):
        """Registered apps this identity may see, each with the live release this controller recorded and what main holds beyond it."""
        result = {}
        for app_id in principal.applications:
            app = self.config.applications[app_id]
            entry = app.model_dump()
            entry["paused"] = bool(self.config.paused or app.paused)
            try:
                entry["platform_pin"] = self.platform_pin(app)
            except Exception as exc:  # noqa: BLE001 - the pin is informational here; dispatch enforces it
                entry["platform_pin"] = {"commit": None, "supported": None, "note": f"Could not read the app's workflow: {str(exc)[:120]}"}
            try:
                entry["schedules"] = self.schedule_health(app)
            except Exception as exc:  # noqa: BLE001 - a GitHub hiccup must not hide the application list
                entry["schedules"] = {"warning": f"Could not read the scheduled workflows: {str(exc)[:120]}"}
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

    def propose_rightsizing(self, principal, app_id):
        """Turn the latest measured proposals into a pull request against platform.yaml; review and merge stay human."""
        app = self.app(principal, app_id, write=True)
        self.require_active(app_id)
        if not self.config.live_dispatch:
            raise ValueError("Writes are disabled (live_dispatch: false); enable them before proposing changes")
        latest = self.github.cost_report(app)
        report = latest.get("report")
        if not report:
            raise ValueError("No cost report yet; the right-sizing workflow runs weekly, or dispatch it in GitHub Actions")
        proposals = [p for p in (report.get("rightsizing") or {}).get("recommendations") or []
                     if p.get("setting") in SETTINGS and isinstance(p.get("recommended"), int) and not isinstance(p.get("recommended"), bool)]
        if not proposals:
            return {"application": app_id, "pull_request": None, "changes": [], "note": "The latest report proposes no concrete setting change."}
        head = self.describe_head(app)
        text = self.github.file(app, head["sha"], "platform.yaml")
        new_text, applied, regenerated = apply_settings(text, {p["setting"]: p["recommended"] for p in proposals})
        if new_text == text:
            return {"application": app_id, "pull_request": None, "changes": [], "note": "platform.yaml already matches the proposals."}
        spec = parse_spec(new_text)
        enforce(spec)  # a proposal can never take the app outside the staging policy
        before, after = self.cost_summary(app, parse_spec(text)), self.cost_summary(app, spec)
        if after["ceiling_policy"]["status"] == "exceeds_limit":
            raise ValueError("The proposed settings would exceed the application's cost ceiling; adjust the ceiling or the proposal first")
        savings = sum(p.get("estimated_monthly_saving_usd") or 0 for p in proposals)
        def money(value):
            return "" if value is None else f"${value:.2f}"
        rows = "\n".join(f"| `{p['setting']}` | {p.get('current')} | {p['recommended']} | {p.get('reason')} | {money(p.get('estimated_monthly_saving_usd'))} |"
                         for p in proposals)
        body = (f"Measured right-sizing from the report generated {report.get('generated_at')} ({latest.get('run_url', 'see Actions')}).\n\n"
                "| Setting | Current | Proposed | Why | Monthly saving |\n|---|---|---|---|---|\n" + rows +
                f"\n\nEstimated saving about ${savings:.2f} per month at list prices. Worst-case compute bound ${before['compute_ceiling_usd_month']:.2f} "
                f"-> ${after['compute_ceiling_usd_month']:.2f} per month; idle floor ${before['idle_floor_usd_month']:.2f} -> ${after['idle_floor_usd_month']:.2f}.\n\n"
                + ("The file was regenerated from the model because a proposed key was missing, so comments were not kept.\n\n" if regenerated else "")
                + "Review, adjust and merge through the normal process; the next release applies it. Nothing is merged or deployed by this pull request itself.")
        branch = f"lab-deploy/rightsize-{(report.get('generated_at') or '')[:10].replace('-', '') or uuid.uuid4().hex[:8]}"
        pull_request = self.github.open_pull_request(app, branch, {"platform.yaml": new_text}, "Right-size platform.yaml from measured usage", body,
                                                     "Apply measured right-sizing proposals to platform.yaml")
        return {"application": app_id, "changes": applied, "estimated_monthly_saving_usd": round(savings, 4),
                "cost": {"worst_case_before_usd_month": before["compute_ceiling_usd_month"], "worst_case_after_usd_month": after["compute_ceiling_usd_month"],
                         "idle_floor_before_usd_month": before["idle_floor_usd_month"], "idle_floor_after_usd_month": after["idle_floor_usd_month"]},
                "pull_request": pull_request}

    def deploy(self, principal, plan_id):
        plan = self.store.get(principal.name, plan_id)
        app = self.app(principal, plan["application"], write=True)
        if plan["state"] != "planned":
            return plan
        self.require_active(plan["application"])
        if not self.config.live_dispatch:
            return {**plan, "dispatch": "disabled", "note": "Operator must configure GitHub/GCP and enable live_dispatch."}
        pin = self.platform_pin(app)
        if pin is not None and pin["supported"] is False:
            raise ValueError(pin["note"])
        if plan["expires_at"] < time.time():
            raise ValueError("Plan expired; create a new plan")
        if plan["repository"] != app.repository or plan["branch"] != app.branch or plan.get("target") != app.target.model_dump():
            raise ValueError("Registry changed; create a new plan")
        if plan.get("pinned_commit"):
            self.ancestor_of_branch(app, plan["sha"])
        elif self.github.head(app) != plan["sha"]:
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

    def cancel(self, principal, plan_id):
        """Cancel the GitHub run of a dispatched plan; the recovery workflow restores traffic if the runner had started changing it."""
        owner, plan = self.load(principal, plan_id)
        app = self.app(principal, plan["application"], write=True)
        run_id = (plan.get("dispatch_result") or {}).get("workflow_run_id")
        if plan["state"] not in DISPATCHED or not run_id:
            raise ValueError(f"Plan is '{plan['state']}' and has no running workflow to cancel")
        result = self.github.cancel_run(app, run_id)
        with self.store.connect() as db:
            db.execute("INSERT INTO audit VALUES (?, ?, ?, 'cancel_requested')", (time.time(), owner, plan_id))
        status = self.refresh(principal, plan_id)
        status["cancel"] = result
        status["next_action"] = ("Cancellation requested. GitHub stops the runner within a minute; the recovery workflow then restores traffic "
                                 "if the release had started changing it. Ask for status again in a minute.")
        return status

    def deploy_latest(self, principal, app_id):
        """Plan the tip of main and dispatch it in one step when every policy passes."""
        plan = self.plan(principal, app_id)
        return self.deploy(principal, plan["id"])

    def rollback(self, principal, app_id):
        """Move traffic back to the split that served before the live release, through the recovery workflow. No build."""
        app = self.app(principal, app_id, write=True)
        self.require_active(app_id)
        if not self.config.live_dispatch:
            raise ValueError("Dispatch is disabled (live_dispatch: false); nothing can be rolled back from here")
        live = self.store.latest_success(app_id)
        if live is None:
            raise ValueError("No release recorded by this controller to roll back from; deploy the wanted commit instead")
        receipt = live.get("receipt") or {}
        if not receipt.get("previous_traffic"):
            raise ValueError("The live release's receipt records no previous traffic split; deploy the wanted commit instead")
        record = {
            "id": uuid.uuid4().hex, "kind": "rollback", "application": app_id, "repository": app.repository, "branch": app.branch,
            "sha": live["sha"], "commit": live.get("commit"), "spec_hash": live.get("spec_hash"), "cloud": "gcp", "environment": "staging",
            "target": live.get("target"), "expires_at": time.time() + 900, "cost": None, "checks": {},
            "from_plan": live["id"], "from_revision": receipt.get("revision"), "to_traffic": receipt["previous_traffic"],
        }
        try:
            record["dispatch_result"] = self.github.dispatch_workflow(app, "recover-gcp.yml", {"rollback_plan_id": live["id"]})
        except Exception:
            self.store.add(principal.name, record, "dispatch_unknown")
            raise ValueError("Rollback dispatch outcome unknown. Inspect GitHub before requesting it again.") from None
        self.store.add(principal.name, record, "dispatched")
        return {**record, "state": "dispatched",
                "next_action": f"Rollback dispatched: traffic returns to {record['to_traffic']} within about a minute. Ask for status with this id."}

    def compare_with_live(self, principal, app_id):
        """What main holds beyond the live release: commits, files, the kinds of change, and the cost bounds before and after."""
        app = self.app(principal, app_id)
        live = self.store.latest_success(app_id)
        head = self.describe_head(app)
        if live is None:
            return {"application": app_id, "live_release": None, "head": head, "note": "No release recorded by this controller yet, so there is nothing to compare against."}
        comparison = self.github.compare(app, live["sha"])
        paths = [f["path"] for f in comparison.get("files", [])]
        kinds = {
            "dockerfile": any(p == "Dockerfile" or p.endswith("/Dockerfile") for p in paths),
            "dependencies": any(p.endswith(("requirements.txt", "requirements-dev.txt", "requirements.lock", "pyproject.toml")) for p in paths),
            "platform_config": "platform.yaml" in paths,
            "workflows": any(p.startswith(".github/workflows/") for p in paths),
            "application_code": any(p.endswith(".py") and not p.startswith(".github/") for p in paths),
        }
        result = {"application": app_id, "live_release": {"plan_id": live["id"], "sha": live["sha"], "commit": live.get("commit")},
                  "head": head, "ahead_by": comparison.get("ahead_by"), "behind_by": comparison.get("behind_by"), "commits": comparison.get("commits"),
                  "files": comparison.get("files"), "changes": kinds, "cost": None}
        if kinds["platform_config"]:
            before = self.cost_summary(app, self.spec_at(app, live["sha"]))
            after = self.cost_summary(app, self.spec_at(app, head["sha"]))
            result["cost"] = {"worst_case_before_usd_month": before["compute_ceiling_usd_month"], "worst_case_after_usd_month": after["compute_ceiling_usd_month"],
                              "idle_floor_before_usd_month": before["idle_floor_usd_month"], "idle_floor_after_usd_month": after["idle_floor_usd_month"],
                              "ceiling_policy_after": after["ceiling_policy"]}
        if comparison.get("ahead_by") == 0:
            result["note"] = "Main is exactly the live release; nothing is waiting to be deployed."
        return result

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
        owner, plan = self.load(principal, plan_id)
        app = self.app(principal, plan["application"])
        if plan["state"] in DISPATCHED:
            workflow = self.github.status(app, plan)
            plan["dispatch_state"] = plan["state"]
            plan["workflow"] = workflow
            plan.update(summarize(workflow, plan["checks"], plan.get("kind")))
            if plan["state"] in TERMINAL:
                self.record_outcome(owner, app, plan, workflow)
        elif plan["state"] in {"failed", "cancelled", "timed_out"} and not (plan.get("recovery_run") or {}).get("conclusion"):
            self.record_recovery(owner, app, plan)
        plan["owner"] = owner
        plan["phases"] = release_phases(plan.get("receipt"))
        plan["events"] = self.store.events(owner, plan_id)
        plan["next_action"] = advice(plan)
        return plan

    def record_outcome(self, owner, app, plan, workflow):
        """A finished run is stored once with its receipt, so history survives GitHub's artifact retention."""
        if plan.get("kind") == "rollback":
            fetch = getattr(self.github, "artifact_json", None)
            files = fetch(app, workflow["id"], "recovery-receipt", ("recovery-receipt.json",)) if callable(fetch) and workflow.get("id") else None
            plan["receipt"] = (files or {}).get("recovery-receipt.json")
            if plan["state"] == "succeeded" and (plan["receipt"] or {}).get("state") == "rolled_back":
                self.store.mark(plan["from_plan"], rolled_back=True, rolled_back_by=plan["id"])
            plan["vulnerabilities"] = None
        else:
            fetch = getattr(self.github, "release_receipt", None)
            receipts = fetch(app, workflow["id"]) if callable(fetch) and workflow.get("id") else None
            plan["receipt"] = (receipts or {}).get("release")
            plan["vulnerabilities"] = (receipts or {}).get("vulnerabilities")
        plan["outcome"] = {"state": plan["state"], "conclusion": workflow.get("conclusion"), "run_url": workflow.get("html_url"),
                           "completed_at": workflow.get("updated_at"), "recorded_at": time.time()}
        plan["workflow"] = {key: workflow.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt", "created_at", "updated_at")}
        if plan["state"] in {"failed", "cancelled", "timed_out"}:
            self.record_recovery(owner, app, plan, persist=False)
        self.persist(owner, plan)

    def record_recovery(self, owner, app, plan, persist=True):
        lookup = getattr(self.github, "recovery_after", None)
        if not callable(lookup) or not (plan.get("workflow") or {}).get("id"):
            return
        plan["recovery_run"] = lookup(app, plan["workflow"])
        if persist and (plan["recovery_run"] or {}).get("conclusion"):
            self.persist(owner, plan)

    def persist(self, owner, plan):
        record = {key: value for key, value in plan.items() if key not in {"events", "phases", "next_action", "owner"}}
        self.store.finish(owner, record, plan["state"])

