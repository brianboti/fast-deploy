import contextlib
import datetime
import json
import re
import sqlite3
import threading
import time
import uuid

from .analyzer import COMPOSE_FILES, FILES, analyze
from .config import Application, Principal, Production
from .contract import Contracts, contract_path
from .cost import estimate, findings
from .github import explain
from .infra import BOOTSTRAP_IMAGE, PRODUCTION_ROOTS, PRODUCTION_WORKFLOWS, app_root
from .models import containment, enforce, needs_env, parse_contract, parse_spec, spec_hash
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
            db.execute("CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")
            # Each application's production (docs/ROADMAP.md D37), for an application of the file as for a registered one.
            db.execute("CREATE TABLE IF NOT EXISTS productions (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS onboardings (id TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)")
            # The newest release pull request of each application whose contract the platform keeps (docs/ROADMAP.md D32).
            db.execute("CREATE TABLE IF NOT EXISTS release_requests (id TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("PRAGMA journal_mode=WAL")  # readers never block the scheduler's and the request handlers' writes

    # ---- applications registered by onboarding, and the onboarding records themselves
    def register_application(self, app_id, entry):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO applications VALUES (?, ?, ?)", (app_id, time.time(), json.dumps(entry)))

    def unregister_application(self, app_id):
        """Forget an onboarded application and its onboarding record; its plans and audit events stay as history."""
        with self.connect() as db:
            removed = db.execute("DELETE FROM applications WHERE id=?", (app_id,)).rowcount
            db.execute("DELETE FROM onboardings WHERE id=?", (app_id,))
        return bool(removed)

    def registered_applications(self):
        with self.connect() as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id, payload FROM applications ORDER BY created").fetchall()}

    def register_production(self, app_id, entry):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO productions VALUES (?, ?, ?)", (app_id, time.time(), json.dumps(entry)))

    def productions(self):
        with self.connect() as db:
            return {row[0]: json.loads(row[1]) for row in db.execute("SELECT id, payload FROM productions ORDER BY created").fetchall()}

    def onboarding(self, app_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM onboardings WHERE id=?", (app_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_onboarding(self, app_id, record):
        record["updated_at"] = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO onboardings VALUES (?, ?, ?)", (app_id, record["updated_at"], json.dumps(record)))

    def onboardings(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM onboardings ORDER BY updated").fetchall()]

    def release_request(self, app_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM release_requests WHERE id=?", (app_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_release_request(self, app_id, record):
        record["updated_at"] = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO release_requests VALUES (?, ?, ?)", (app_id, record["updated_at"], json.dumps(record)))

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
            raise ValueError("Plan not found for this identity; list_plans shows the plans it may see, or create a new plan")
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
        """Store a plan's new state; a concurrent writer that got there first wins, so an outcome is recorded once."""
        with self.connect() as db:
            before = db.execute("SELECT state FROM plans WHERE id=? AND owner=?", (plan["id"], owner)).fetchone()
            if before is None:
                return False
            changed = db.execute("UPDATE plans SET state=?,payload=? WHERE id=? AND owner=? AND state=?", (state, json.dumps(plan), plan["id"], owner, before[0])).rowcount
            if changed and before[0] != state:  # re-saving a record with more detail is not a new event
                db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))
        return bool(changed)

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

    def in_flight(self, limit=10):
        """Plans whose run has not been seen to end, newest first, with their application: the background follow-up asks
        GitHub about them, so an outcome is recorded even when nobody asks."""
        with self.connect() as db:
            rows = db.execute("SELECT id, json_extract(payload, '$.application') FROM plans WHERE state IN ('dispatching', 'dispatched', 'dispatch_unknown') "
                              "ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [(row[0], row[1]) for row in rows]

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

    def latest_success(self, application, sha=None, include_rolled_back=False, signed=False):
        """The newest release that succeeded for an application, by default excluding ones rolled back since (optionally for one commit).
        `signed`: only releases whose image carries signed provenance, so not one released under the operator's exception."""
        query = "SELECT payload FROM plans WHERE state='succeeded' AND json_extract(payload, '$.application')=? AND json_extract(payload, '$.kind') IS NULL"
        params = [application]
        if not include_rolled_back:
            query += " AND json_extract(payload, '$.rolled_back') IS NULL"
        if signed:
            query += " AND COALESCE(json_extract(payload, '$.receipt.provenance'), '') != 'unavailable'"
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

    def newest_event(self, kind, outcome=None):
        """The newest audit event of one kind, optionally with one outcome; None when there is none."""
        query, params = "SELECT at, identity, name, outcome, detail FROM events WHERE kind=?", [kind]
        if outcome is not None:
            query += " AND outcome=?"
            params.append(outcome)
        with self.connect() as db:
            row = db.execute(query + " ORDER BY at DESC, rowid DESC LIMIT 1", params).fetchone()
        return dict(zip(("at", "identity", "name", "outcome", "detail"), row, strict=True)) if row else None

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


def same_image_inputs(recorded, spec):
    """Whether an earlier release's image was made the way this specification would make it (its build section and Python),
    comparing what was set rather than raw dumps, so a field a later version added with its default changes nothing (found
    live: 1.16.0's build.network made every first release after the upgrade build again)."""
    from .models import Build
    if not isinstance(recorded, dict):
        return False
    try:
        earlier = Build.model_validate(recorded.get("build") or {}).model_dump(exclude_defaults=True)
    except ValueError:
        return False
    return earlier == spec.build.model_dump(exclude_defaults=True) and recorded.get("python", "3.12") == spec.python


IMAGE_RESCAN_DAYS = 30  # Artifact Analysis stops analysing an image this long after its push (docs/ROADMAP.md 14.4)


def recent(release, now=None):
    """Whether a release's image is young enough to reuse: Artifact Analysis analyses an image continuously for 30 days only,
    so an older one is built and scanned again rather than redeployed."""
    recorded = ((release or {}).get("outcome") or {}).get("recorded_at")
    if not isinstance(recorded, (int, float)):
        return True  # releases recorded before 1.16.0 carry no time; the scan still runs on the reused image
    return (now or time.time()) - recorded < IMAGE_RESCAN_DAYS * 86_400


def release_approval(plan, requested_by=None):
    """The fixed approval block of a release plan (docs/ROADMAP.md 12.1), built by code from the plan's own fields and never
    from Claude's words: the commit, where it goes, what it may cost, who may reach it, what it needs, how far a rollback
    undoes it, and the identifiers that tie the request to the plan and the run. The same plan always gives the same block."""
    spec, cost, target = plan.get("spec") or {}, plan.get("cost") or {}, plan.get("target") or {}
    needs = spec.get("needs") or {}
    lines = ((plan.get("commit") or {}).get("message") or "").strip().splitlines()
    migrates = bool(needs.get("migrate"))
    reversibility = ({"tier": "data-bearing", "detail": "the migration changes the database; a rollback restores traffic only"} if migrates
                     else {"tier": "reversible", "detail": "traffic rollback in seconds"})
    requester = requested_by or ""
    block = {
        "commit": {"sha": plan.get("sha"), "message": lines[0][:100] if lines else None},
        "spec_hash": plan.get("spec_hash"),
        "target": {"project_id": target.get("project_id"), "region": target.get("region"), "service": target.get("service"),
                   "layout": plan.get("layout")},
        "cost": {"idle_floor_usd_month": cost.get("idle_floor_usd_month"), "compute_ceiling_usd_month": cost.get("compute_ceiling_usd_month"),
                 "needs_floor_usd_month": cost.get("needs_floor_usd_month"), "ceiling_policy": (cost.get("ceiling_policy") or {}).get("status")},
        "exposure": spec.get("exposure", "authenticated"),
        "needs": {"buckets": list(needs.get("buckets") or []), "topics": list(needs.get("topics") or []), "database": needs.get("database", "none"),
                  "migrate": migrates},
        "warnings": len(plan.get("warnings") or []),
        "reversibility": reversibility,
        "identifiers": {"plan": plan.get("id"), "commit": plan.get("sha"), "requested_by": requested_by,
                        "onboarding": plan.get("application") if requester.startswith("onboarding:") else None, "run": None, "run_url": None},
    }
    if plan.get("containment"):
        held = plan["containment"]
        block["containment"] = {key: held.get(key) for key in ("trust", "egress", "execution_environment", "vulnerability_gate")}
    if plan.get("contract"):
        # An application taken as it is (docs/ROADMAP.md 13.1) also names its source and the platform commit whose contract describes it.
        contract = plan["contract"]
        block["contract"] = {"repository": contract["repository"], "commit": contract["commit"], "path": contract["path"],
                             "source": {"repository": plan.get("repository"), "commit": plan.get("sha")}}
    return block


def link_run(plan):
    """Add the workflow run to a release plan's approval identifiers once GitHub has one, so the chain reads request, plan, run."""
    approval, workflow, dispatched = plan.get("approval"), plan.get("workflow") or {}, plan.get("dispatch_result") or {}
    run = workflow.get("id") or dispatched.get("workflow_run_id")
    if isinstance(approval, dict) and run:
        approval.setdefault("identifiers", {}).update(run=run, run_url=workflow.get("html_url") or dispatched.get("html_url"))
    return plan


class Controller:
    def __init__(self, config, github, store, observer=None):
        self._cache = {}
        self._onboarding_lock = threading.Lock()
        self.config, self.github, self.store, self.observer = config, github, store, observer
        self.contracts = Contracts(self)
        self.refresh_registry()

    def refresh_registry(self):
        """Applications onboarding registered live in the database; the live configuration shows them next to the file's."""
        if hasattr(self.config, "registry"):
            self.config.registry = {app_id: Application.model_validate(entry) for app_id, entry in self.store.registered_applications().items()}
        if hasattr(self.config, "productions"):
            self.config.productions = self.store.productions()

    def visible_apps(self, principal):
        """The application ids this identity may see: its list, or every registered application for a `*` identity."""
        if "*" in principal.applications:
            return list(self.config.applications)
        return [app_id for app_id in principal.applications if app_id in self.config.applications]

    # ---- one sentence: a repository that follows the contract becomes a running service
    def onboard(self, principal, repository, app_id=None, auto_deploy=False, path=None, as_is=False):
        from .onboarding import Onboarder
        with self._onboarding_lock:
            return Onboarder(self).start(principal, repository, app_id, auto_deploy, path, as_is=as_is)

    def import_contract(self, principal, repository, path=None, name=None):
        """Read one service of a repository as it is and open the pull request that adds its contract to the platform
        repository (docs/ROADMAP.md 13.1); nothing is added to the repository itself, and merging is the review."""
        from .onboarding import normalize_path, normalize_repository
        result = self.contracts.propose(principal, normalize_repository(repository), normalize_path(path), name)
        view = {key: result.get(key) for key in ("repository", "path", "head", "service", "files", "pull_request", "existing", "note")}
        read = result["read"]
        view.update(service=view.get("service") or read["service"], choices=read["choices"], refusals=read["refusals"], ignored=read["ignored"],
                    notes=read["notes"])
        if read["refusals"]:
            view["next_action"] = "Nothing was opened: fix what each refusal names in the repository, then ask again."
        elif result.get("pull_request") == "disabled":
            view["next_action"] = "Writes are disabled (live_dispatch: false): the contract above was drafted and not proposed."
        elif result.get("pull_request"):
            view["next_action"] = (f"A person reviews and merges the pull request: merging it is the review. Then onboard_application with {view['repository']}"
                                   + (f" and path {view['path']}" if view["path"] else "") + " as it is, or it continues by itself when onboarding opened it.")
        else:
            view["next_action"] = view.get("note") or "Nothing to import."
        return view

    def contract_tick(self):
        """The scheduler's pass over contracts the platform keeps: release pull requests, and merged contracts not released."""
        return self.contracts.tick()

    def check_repository(self, principal, repository, path=None):
        """The contract's checklist for a repository, or one service folder of it, before anything is registered or created:
        each item, whether it holds, why it matters, and whether the platform can draft it (docs/ROADMAP.md 11.1)."""
        from .onboarding import Blocked, contract_check, missing_message, normalize_path, normalize_repository
        if "*" not in principal.applications:
            raise ValueError("Checking a repository that is not registered needs an identity that may see every application: applications: ['*']")
        repository, path = normalize_repository(repository), normalize_path(path)
        try:
            result = contract_check(self.github, repository, path)
        except Blocked as exc:
            return {"repository": repository, "path": path, "ready": False, "error": str(exc),
                    "next_action": "Fix what the error names; nothing was created."}
        view = {key: result[key] for key in ("repository", "path", "branch", "ready", "checklist", "recommended")}
        writable = [entry["item"] for entry in result["checklist"] + result["recommended"] if entry["platform_writes"]]
        view["platform_can_draft"] = writable
        # As it is (docs/ROADMAP.md 13.5): what the reader would choose without changing the repository, and what it refuses.
        try:
            seen = self.contracts.inspect(repository, path)
            read = seen["read"]
            view["as_it_is"] = {"ready": not read["refusals"] and read["spec"] is not None, "service": read["service"], "choices": read["choices"],
                                "refusals": read["refusals"], "ignored": read["ignored"], "notes": read["notes"], "services": seen["services"]}
        except Exception as exc:  # noqa: BLE001 - the checklist stands on its own
            view["as_it_is"] = {"ready": False, "error": (explain(exc) or str(exc))[:300]}
        if result["ready"]:
            view["next_action"] = (f"Ready to deploy: onboard_application with {repository}" + (f" and path {path}" if path else "")
                                   + (". The platform can still draft the recommended " + ", ".join(writable) + " with propose_configuration." if writable else "."))
        elif view["as_it_is"].get("ready"):
            view["next_action"] = (missing_message(result["checklist"]) + " Or deploy it as it is, adding nothing to the repository: onboard_application "
                                   "with as_is true (its contract is kept in the platform repository).")
        else:
            view["next_action"] = missing_message(result["checklist"])
        return view

    def advance_onboarding(self, app_id):
        from .onboarding import Onboarder
        with self._onboarding_lock:
            record = self.store.onboarding(app_id)
            if record is None:
                raise ValueError("No onboarding record for this application")
            return Onboarder(self).advance(record)

    def onboarding_tick(self):
        """One pass over every onboarding that waits on GitHub or the pipeline; the scheduler calls it every minute. It also
        re-reads the registered applications, so `lab-deploy unregister` reaches a running controller within a minute."""
        from .onboarding import ACTIVE
        self.refresh_registry()
        results = {}
        for record in self.store.onboardings():
            if record.get("state") in ACTIVE:
                try:
                    results[record["application"]] = self.advance_onboarding(record["application"])["state"]
                except Exception as exc:  # noqa: BLE001 - one stuck onboarding must not stop the others
                    results[record["application"]] = f"error: {str(exc)[:120]}"
        return results

    def onboarding_status(self, principal, app_id):
        from .onboarding import describe
        record = self.store.onboarding(app_id)
        if record is None:
            if app_id in self.visible_apps(principal):
                return self.registered_by_hand(app_id)
            raise ValueError("No onboarding record for this application; call onboard_application with the repository link")
        if "*" not in principal.applications and app_id not in principal.applications:
            raise ValueError("Application is not authorized for this identity; list_applications shows the ones it may use")
        return describe(record)

    def registered_by_hand(self, app_id):
        """An application in the configuration without an onboarding record (found live: `demo`, registered by hand and live,
        was told to onboard itself): what it is and where its live release stands, so the answer points at the right tools."""
        app = self.config.applications[app_id]
        live = self.store.latest_success(app_id)
        return {"application": app_id, "state": "registered_by_hand", "repository": app.repository, "path": app.path, "as_is": app.central,
                "live_release": live and {"plan_id": live["id"], "sha": live["sha"], "commit": live.get("commit"),
                                          "completed_at": (live.get("outcome") or {}).get("completed_at")},
                "next_action": f"{app_id} was registered by hand, not onboarded from chat, so there is no onboarding to follow; "
                               "list_applications shows its live release and get_service_health how it is doing."}

    AUTO_DEPLOY_IDENTITY = "auto-deploy"

    @staticmethod
    def retryable(attempt, now=None):
        """A plan that never became a run (left planned, or its dispatch was lost) is tried again half an hour later."""
        now = now or time.time()
        return attempt["state"] in {"planned", "dispatch_lost"} and now > attempt["expires_at"] - 900 + 1800

    def cached(self, key, seconds, fn):
        """Remember one GitHub answer per application for a few minutes; the first tool Claude calls asks for all of them."""
        entry = self._cache.get(key)
        if entry and time.monotonic() - entry[0] < seconds:
            return entry[1]
        value = fn()
        self._cache[key] = (time.monotonic(), value)
        return value

    def touched_since(self, app, base, head):
        """Whether a commit range concerns a service in a folder of a shared repository: a file under its folder or one of
        its own workflows changed, or its build reads the whole repository. When in doubt, it does."""
        if self.spec_at(app, head).build.context == "repository":
            return True
        files, complete = self.github.changed_files(app.repository, base, head)
        own = set() if app.central else {f".github/workflows/{name}" for name in app.workflows.values()}
        return not complete or any(name.startswith(f"{app.path}/") or name in own for name in files)

    FOLLOW_IDENTITY = "follow-up"

    def follow_tick(self, limit=10):
        """One pass: refresh every release still on its way, so its outcome is recorded even when nobody asks (an auto-deploy,
        an onboarding's first release, a release whose status Claude did not follow). The application list, the watchdog
        and the chat's summaries then never show a finished release as still dispatched (found live on 2026-10-16: demo's
        auto-deploy of the 15th had succeeded, and the list still showed the release before it)."""
        principal = Principal(name=self.FOLLOW_IDENTITY, token_sha256="0" * 64, applications=["*"], can_deploy=True)
        results = {}
        for plan_id, app_id in self.store.in_flight(limit):
            if app_id not in self.config.applications:
                continue  # an application unregistered since: its plans stay as history
            try:
                results[plan_id[:8]] = self.refresh(principal, plan_id)["state"]
            except Exception as exc:  # noqa: BLE001 - one plan's trouble must not stop the others; the next pass asks again
                results[plan_id[:8]] = f"error: {(explain(exc) or str(exc))[:120]}"
        return results

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
                if app.central:
                    # docs/ROADMAP.md D32: the release of a new commit is the pull request that moves source.commit; its merge
                    # (by a person, or by the controller under auto_merge) releases it through contract_tick.
                    current = self.contracts.require(app)["source"].commit
                    if current == head or (app.path and not self.touched_since(app, current, head)):
                        results[app_id] = "up to date: the contract names the tip of main" if current == head else f"up to date: nothing under {app.path} changed"
                        continue
                    pending = self.store.release_request(app_id)
                    if pending and pending.get("target") == head and pending.get("state") in ("open", "merged"):
                        results[app_id] = f"release pull request #{pending['pull_request']} for {head[:12]} is {pending['state']}"
                        continue
                    principal = Principal(name=self.AUTO_DEPLOY_IDENTITY, token_sha256="0" * 64, applications=[app_id], can_deploy=True)
                    record = self.contracts.request_release(principal, app_id, app, head)
                    self.store.record_event("auto_deploy", name=app_id, identity=self.AUTO_DEPLOY_IDENTITY, application=app_id, outcome=record["state"],
                                            detail=f"{head[:12]} release pull request #{record.get('pull_request')}")
                    results[app_id] = f"release pull request #{record.get('pull_request')} for {head[:12]}: {record['state']}"
                    continue
                live = self.store.latest_success(app_id)
                if live and live["sha"] == head:
                    results[app_id] = "up to date"
                    continue
                if live and app.path and not self.touched_since(app, live["sha"], head):
                    results[app_id] = f"up to date: nothing under {app.path} changed since {live['sha'][:12]}"
                    continue
                attempted = self.store.latest_plan(app_id, head)
                if attempted and not self.retryable(attempted):
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
            onboarding = self.store.onboarding(app_id)
            if onboarding and onboarding.get("state") != "complete":
                # Its workflows may not exist yet, so the other checks would only fail; say what is actually wrong.
                problems.append(f"{app_id}: onboarding is not finished ({onboarding.get('state')} at {onboarding.get('blocked_at') or onboarding.get('state')}); "
                                f"call onboard_application again, or remove it with `lab-deploy unregister {app_id}`")
                continue
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
        drift = self.drift_status()
        if drift.get("status") in ("drift", "clean"):
            for app_id in drift.get("serious", []):
                problems.append(f"{app_id}: infrastructure drift in {', '.join(drift['drifted'].get(app_id, []))}: something changed outside the pipeline; "
                                f"see {drift.get('run_url')} and merge or re-apply the root to put the code's version back")
            for app_id in drift.get("failed", []):
                problems.append(f"{app_id}: the weekly drift plan did not finish; see {drift.get('run_url')}")
            if drift.get("stale"):
                problems.append(f"the weekly drift check last reported {drift.get('checked_at')}; its schedule may have stopped (GitHub pauses schedules "
                                "after sixty days without activity)")
        reminder = self.restore_drill_reminder()
        if reminder:
            problems.append(reminder)
        replication = self.replication_problem()
        if replication:
            problems.append(replication)
        report = {"checked_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "problems": problems, "notified": False}
        self.store.record_event("watchdog", outcome="problems" if problems else "ok", detail="; ".join(problems) or None)
        if problems and self.config.notify_webhook_url:
            import httpx
            text = "Fast Deploy watchdog found " + str(len(problems)) + " problem(s):\n- " + "\n- ".join(problems)
            try:
                httpx.post(self.config.notify_webhook_url, json={"text": text, "content": text}, timeout=20).raise_for_status()
                report["notified"] = True
            except Exception as exc:  # noqa: BLE001
                report["notify_error"] = str(exc)[:200]
        return report

    RESTORE_DRILL_DAYS = 92  # a quarter and a day: the restore drill is due when none has passed for longer than this

    def restore_drill_reminder(self, now=None):
        """The watchdog's line when the quarterly restore drill is due (docs/RUNBOOK.md, "Quarterly restore drill"), else None.
        Only with a backup bucket: the drill restores the copy that survives losing this machine."""
        if not getattr(self.config, "backup_bucket", None):
            return None
        now = now or time.time()
        passed = self.store.newest_event("restore_drill", "ok")
        if passed and now - passed["at"] <= self.RESTORE_DRILL_DAYS * 86_400:
            return None
        when = "no drill has passed yet" if passed is None else f"the last one passed on {datetime.datetime.fromtimestamp(passed['at'], datetime.UTC):%Y-%m-%d}"
        latest = self.store.newest_event("restore_drill")
        if latest and latest["outcome"] != "ok":
            when += f"; the latest attempt failed: {(latest['detail'] or 'no detail')[:160]}"
        how = "scripts/machine.sh drill (from the laptop)" if getattr(self.config, "litestream_replica", None) else "python -m labdeploy.cli restore-drill"
        return f"quarterly restore drill due ({when}): run {how}"

    def replication_problem(self, run=None):
        """The watchdog's line when the machine's live copy of the database (Litestream, docs/ROADMAP.md 15.3) is not
        running, else None. Only where `litestream_replica` is set: the machine's configuration."""
        if not getattr(self.config, "litestream_replica", None):
            return None
        import subprocess
        run = run or subprocess.run
        try:
            state = run(["systemctl", "is-active", "litestream"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"the live copy of the database (Litestream) could not be checked: {str(exc)[:120]}"
        if state != "active":
            return (f"the live copy of the database (Litestream) is {state or 'not running'}: until it runs again the database is only copied "
                    "nightly (scripts/machine.sh logs litestream)")
        return None

    DRIFT_STALE_DAYS = 8  # the drift check runs weekly; a report older than this means the schedule stopped

    def drift_status(self):
        """The weekly drift check's newest result for the platform repository (remembered for an hour), or why there is none."""
        platform = getattr(self.config, "platform", None)
        reader = getattr(self.github, "drift_report", None)
        if platform is None or not callable(reader):
            return {"status": "not_configured", "note": "platform.repository is not set in the controller configuration, so the drift report cannot be read"}
        try:
            found = self.cached(("drift", platform.repository), 3600, lambda: reader(platform.repository))
        except Exception as exc:  # noqa: BLE001 - the drift view is informative; its absence is reported, never fatal
            return {"status": "unavailable", "note": explain(exc) or str(exc)[:200]}
        if not found:
            # A first report can appear any minute (a manual run); ask again next time instead of remembering "none" for an hour.
            self._cache.pop(("drift", platform.repository), None)
            return {"status": "not_run", "note": "The weekly drift check (.github/workflows/infra-drift.yml) has produced no report yet; it needs the "
                                                 "foundation applied with the 1.9.0 plan identity, and runs on Mondays or on demand."}
        report = found.get("report") or {}
        try:
            checked = datetime.datetime.fromisoformat(str(report.get("checked_at")).replace("Z", "+00:00"))
            age_days = round((datetime.datetime.now(datetime.UTC) - checked).total_seconds() / 86400, 1)
        except ValueError:
            age_days = None
        details = {record["app"]: record.get("categories", []) for record in report.get("applications", []) if record.get("drifted")}
        return {"status": "drift" if report.get("drifted") else "clean", "checked_at": report.get("checked_at"), "age_days": age_days,
                "stale": age_days is None or age_days > self.DRIFT_STALE_DAYS, "run_url": found.get("run_url"),
                "drifted": details, "serious": report.get("serious", []), "failed": report.get("failed", [])}

    def backup_tick(self, config_path=None):
        """A consistent, compact copy of the SQLite database next to it (or in backup_dir), made with VACUUM INTO while the
        controller keeps writing; copies older than backup_keep_days go.

        With `backup_bucket` set, the copy and the configuration file (`config_path`, else the live configuration's path)
        are uploaded to the bucket's `backup_prefix` folder (controller/ for a laptop, machine/ for the machine) with this
        host's own gcloud sign-in: the operator's on a laptop, the machine's identity on the machine.
        """
        from pathlib import Path
        folder = Path(self.config.backup_dir or f"{self.config.database}.backups")
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"deployments-{datetime.datetime.now(datetime.UTC):%Y%m%d-%H%M%S}.db"
        source = sqlite3.connect(self.config.database, timeout=30)
        try:
            source.execute("VACUUM INTO ?", (str(destination),))
        finally:
            source.close()
        cutoff = time.time() - self.config.backup_keep_days * 86_400
        removed = []
        for old in folder.glob("deployments-*.db"):
            if old != destination and old.stat().st_mtime < cutoff:
                old.unlink()
                removed.append(old.name)
        uploaded = None
        bucket = getattr(self.config, "backup_bucket", None)
        if bucket:
            # The copy and the configuration (principal hashes, targets, settings; never the raw operator token) go to a
            # private, versioned bucket so a lost laptop loses nothing but its tunnel.
            import subprocess
            config_path = config_path or getattr(self.config, "path", None)
            prefix = getattr(self.config, "backup_prefix", None) or "controller"
            targets = [(str(destination), f"gs://{bucket}/{prefix}/{destination.name}")]
            if config_path and Path(config_path).is_file():
                targets.append((str(config_path), f"gs://{bucket}/{prefix}/config.yaml"))
            for source_path, target in targets:
                subprocess.run(["gcloud", "storage", "cp", source_path, target], check=True, capture_output=True, text=True, timeout=120)
            uploaded = [target for _, target in targets]
        self.store.record_event("backup", outcome="ok", detail=str(destination) + (f" + {len(uploaded)} uploaded" if uploaded else ""))
        return {"backup": str(destination), "removed": removed, "kept_days": self.config.backup_keep_days, "uploaded": uploaded}

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
        passed = metrics.get("uptime_checks_passed")
        if passed is not None and passed < 1:
            notes.append(f"Google's uptime check reached the health path in only {passed:.0%} of its probes in the last {metrics['window_minutes']} minutes, "
                         "although the service's own metrics may look normal: requests refused before they reach the container are not counted there. "
                         "When every app fails at once, check the native spend cap (docs/RUNBOOK.md, \"The native spend cap tripped\").")
        needs = []
        try:
            spec = self.spec_at(app, (live or {}).get("sha") or self.github.head(app))
            declared = spec.needs.model_dump()
            if declared["buckets"] or declared["topics"] or declared["database"] != "none":
                needs = observer.needs(app.target, declared)
        except ValueError as exc:  # no reviewed spec yet, or the observer may not look: say so instead of hiding the view
            notes.append(f"Needs not checked: {str(exc)[:160]}")
        for item in needs:
            if item["exists"] is None:
                notes.append(f"Need {item['need']} {item['name']} not checked: {item.get('unreadable', 'not readable')}")
            elif not item["exists"]:
                notes.append(f"Declared need {item['need']} {item['name']} does not exist; ask for propose_infrastructure and merge the pull request")
            elif item.get("running") is False:
                notes.append(f"Database {item['name']} is not running ({item.get('state')}): the spending cap stops databases at the budget line; "
                             "docs/RUNBOOK.md says how to lift it")
        access = {"exposure": None, "iap_enabled": service.get("iap_enabled"), "viewers": self.viewers(app)}
        try:
            access["exposure"] = self.spec_at(app, (live or {}).get("sha") or self.github.head(app)).exposure
        except ValueError:
            pass
        if access["exposure"] == "lab-members" and not access["iap_enabled"]:
            notes.append("platform.yaml says lab-members but Identity-Aware Proxy is not enabled on the service; render and merge the infrastructure")
        if access["exposure"] == "authenticated" and access["iap_enabled"]:
            notes.append("Identity-Aware Proxy is enabled although platform.yaml says authenticated; render and merge the infrastructure")
        return {"application": app_id, "service": service, "metrics": metrics, "recorded_live_release": live and {"plan_id": live["id"], "sha": live["sha"], "revision": recorded},
                "agreement": agreement, "needs": needs, "access": access, "notes": notes,
                "console_url": f"https://console.cloud.google.com/run/detail/{app.target.region}/{app.target.service}/metrics?project={app.target.project_id}"}

    def recent_logs(self, principal, app_id, minutes=30, severity="ERROR", limit=50):
        app = self.app(principal, app_id)
        lines = self.require_observer().logs(app.target, minutes, severity, limit)
        return {"application": app_id, "minutes": minutes, "severity": severity, "count": len(lines), "lines": lines}

    def app(self, principal, app_id, write=False):
        if app_id not in self.visible_apps(principal):
            raise ValueError("Application is not authorized for this identity; list_applications shows the ones it may use")
        if write and not principal.can_deploy:
            raise ValueError("Identity has read-only access; reads still work, and a deploying identity (can_deploy) is the operator's to grant")
        return self.config.applications[app_id]

    def require_active(self, app_id):
        """Plans and dispatches stop while the operator has paused everything or this app; reads keep working."""
        if self.config.paused:
            raise ValueError("Deployments are paused by the operator (paused: true in the controller configuration); reads still work")
        if self.config.applications[app_id].paused:
            raise ValueError(f"Deployments of {app_id} are paused by the operator (paused: true on the application); reads still work")

    SCHEDULES = {"recover": 2 * 86_400, "rightsize": 8 * 86_400}  # staleness: six-hourly and weekly runs, with slack

    def schedule_health(self, app):
        """The two scheduled workflows per app: paused by GitHub, stale, or fine. GitHub disables schedules after sixty idle days."""
        check = getattr(self.github, "schedule_health", None)
        if not callable(check):
            return None
        result = {}
        for kind, limit in self.SCHEDULES.items():
            workflow = app.workflows[kind]
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
        if app.central:
            return {"commit": None, "supported": True, "note": "Released by the platform repository's own pipeline (deploy.yml); there is no pin."}
        pin = self.pinned_platform(app, [platform.repository, *platform.former_repositories])
        result = {"commit": pin, "supported": None, "note": None}
        if pin is None:
            result.update(supported=False, note=f"The app has no {app.workflow} pinned to this platform; run update_app_workflows.")
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

    @staticmethod
    def service_paths(app, paths):
        """A repository's file list as the service sees it: only its folder's files, relative to that folder."""
        if paths is None or not app.path:
            return paths
        prefix = app.path + "/"
        return [path[len(prefix):] for path in paths if path.startswith(prefix)]

    def inspect(self, principal, app_id):
        import httpx
        app = self.app(principal, app_id)
        sha = self.github.head(app)
        files = {}
        for filename in FILES:
            try:
                files[filename] = self.github.file(app, sha, app.repo_path(filename))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        for filename in COMPOSE_FILES:  # a Compose file usually sits at the repository's root, beside the services it names
            for candidate in dict.fromkeys((app.repo_path(filename), filename)):
                if "compose" in files:
                    break
                try:
                    files["compose"] = self.github.file(app, sha, candidate)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
        try:  # the file list lets the analyzer see tests and a lock file; without it the advice is only more cautious
            paths = self.github.tree(app.repository, sha) if hasattr(self.github, "tree") else None
        except Exception:  # noqa: BLE001
            paths = None
        return {"commit_sha": sha, "path": app.path, **analyze(files, app.service, self.service_paths(app, paths))}

    def spec_at(self, app, sha):
        """The reviewed specification at a commit, with a plain explanation when the app has none yet. For an application taken
        as it is, the contract on the platform repository's main, whatever the commit."""
        import httpx
        if app.central:
            return self.contracts.require(app)["spec"]
        try:
            text = self.github.file(app, sha, app.repo_path("platform.yaml"))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            raise ValueError(f"{app.repository} has no {app.repo_path('platform.yaml')} on {app.branch}; run propose_configuration to draft one, review it, "
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
        if app.central:
            return self.plan_contract(principal, app_id, app, cloud, environment, commit_sha)
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
            "provenance": app.provenance,
            "bake_seconds": app.bake_seconds,
            "layout": {"path": app.path, "workflow": app.workflow, "github_environment": app.environment},
            "warnings": [],
        }
        if not app.bake_seconds:
            plan["warnings"].append("bake_seconds is 0 for this application: nothing watches the new revision after its final probe.")
        if spec.tests == "none":
            plan["checks"]["tests"] = "skipped_by_config"
            plan["warnings"].append("platform.yaml declares tests: none, so nothing checks this app's behaviour before a release; only the health probe guards it.")
        if spec.secrets:
            plan["warnings"].append("References secrets " + ", ".join(f"{app.service}-{name}" for name in spec.secrets) + "; each must exist in Secret Manager with an enabled version.")
        derived = needs_env(spec, app.target.project_id, app.target.region)
        if derived:
            plan["needs"] = {"buckets": list(spec.needs.buckets), "topics": list(spec.needs.topics), "database": spec.needs.database, "env": derived}
            plan["warnings"].append("Declares needs (" + ", ".join(sorted(derived)) + "); the release stops before deploying unless the pipeline has applied them "
                                    "(propose_infrastructure, then merge).")
        # A rolled-back release's image is still the attested image of that commit, so it counts for reuse.
        # An image released under the provenance exception carries no signed provenance, so it is never reused (the reuse
        # path verifies the reused image's provenance again and would stop); an earlier signed image of the commit still is.
        prior = self.store.latest_success(app_id, sha, include_rolled_back=True, signed=True)
        receipt = (prior or {}).get("receipt") or {}
        image = receipt.get("image") if receipt.get("provenance") != "unavailable" else None
        prefix = f"{app.target.region}-docker.pkg.dev/{app.target.project_id}/{app.target.registry}/{app.target.service}@sha256:"
        if app.provenance == "unavailable":
            plan["warnings"].append("The operator's exception provenance: unavailable is set for this application: the image is built fresh and released "
                                    "without signed provenance. Remove it from the controller configuration once the cause is fixed.")
        elif image and image.startswith(prefix) and recent(prior):
            # The same commit was released before: reuse its attested image instead of building and testing again.
            plan["reuse_image"] = image
            plan["reused_from_plan"] = prior["id"]
            plan["checks"].update(tests="passed_in_earlier_release", dependency_scan="passed_in_earlier_release")
        if image and image.startswith(prefix) and not recent(prior):
            plan["stale_image"] = True
        self.contain(plan, spec, app)
        plan["approval"] = release_approval(plan, principal.name)
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    @staticmethod
    def contain(plan, spec, app):
        """How the release will treat the code (docs/ROADMAP.md D35), in the plan, its warnings and its approval block."""
        held = containment(spec, "unknown" if app.central else "reviewed")
        plan["containment"] = held
        if held["trust"] == "unknown":
            plan["warnings"].append("Code nobody has reviewed: a working key in it stops the build, fixable critical and high vulnerabilities stop the "
                                    "release, and it runs in the second-generation environment.")
        if held["egress"] == "private":
            plan["warnings"].append("No internet: the service reaches Google's APIs and the lab's private network only; `egress: internet` in the "
                                    "contract opens it, through a reviewed pull request.")
        reused = plan.get("reuse_image")
        if not reused and plan.get("stale_image"):
            plan["warnings"].append("The commit's earlier image is over 30 days old, so it is built again and scanned again.")
        return held

    def plan_contract(self, principal, app_id, app, cloud, environment, commit_sha=None):
        """A plan for an application taken as it is: exactly the commit and specification its contract on the platform
        repository's main names, bound to that contract commit (docs/ROADMAP.md 13.1)."""
        found = self.contracts.require(app)
        sha = found["source"].commit
        if commit_sha and commit_sha != sha:
            raise ValueError(f"The contract {found['path']} runs {sha[:12]}; another commit is released by the pull request that moves "
                             "source.commit: deploy_latest opens it for the tip of main")
        behind = self.ancestor_of_branch(app, sha)
        describe = getattr(self.github, "commit", None)
        commit = describe(app, sha) if callable(describe) else {"sha": sha}
        spec = found["spec"]
        enforce(spec, cloud, environment)
        if spec.name != app.service:
            raise ValueError("Spec service name does not match operator registry")
        cost = self.cost_policy(app, spec)
        platform = self.config.platform
        plan = {
            "id": uuid.uuid4().hex, "application": app_id, "repository": app.repository,
            "branch": app.branch, "sha": sha, "commit": commit, "spec_hash": spec_hash(spec),
            "spec": spec.model_dump(), "cloud": cloud, "environment": environment,
            "target": app.target.model_dump(), "expires_at": time.time() + 900, "cost": cost,
            "checks": {"config_policy": "passed", "tests": "pending_in_ci", "dependency_scan": "pending_in_ci", "health": "pending_after_deployment"},
            "pinned_commit": True, "behind_branch_by": behind, "provenance": app.provenance, "bake_seconds": app.bake_seconds,
            "layout": {"path": app.path, "workflow": app.workflow, "github_environment": app.environment},
            "contract": {"repository": platform.repository, "commit": found["commit"], "path": found["path"]},
            "warnings": [],
        }
        if behind:
            plan["warnings"].append(f"The contract runs a commit {behind} behind the tip of main; deploy_latest opens the pull request that moves it.")
        if not app.bake_seconds:
            plan["warnings"].append("bake_seconds is 0 for this application: nothing watches the new revision after its final probe.")
        if spec.tests == "none":
            plan["checks"]["tests"] = "skipped_by_config"
            plan["warnings"].append("The contract declares tests: none, so nothing checks this app's behaviour before a release; only the health probe guards it.")
        if spec.health_path is None:
            plan["warnings"].append("No health route: Cloud Run's TCP startup probe gates the start, and the HTTP health probe is off.")
        if spec.secrets:
            plan["warnings"].append("References secrets " + ", ".join(f"{app.service}-{name}" for name in spec.secrets) + "; each must exist in Secret Manager with an enabled version.")
        derived = needs_env(spec, app.target.project_id, app.target.region)
        if derived:
            plan["needs"] = {"buckets": list(spec.needs.buckets), "topics": list(spec.needs.topics), "database": spec.needs.database, "env": derived}
        prior = self.store.latest_success(app_id, sha, include_rolled_back=True, signed=True)
        receipt = (prior or {}).get("receipt") or {}
        image = receipt.get("image") if receipt.get("provenance") != "unavailable" else None
        prefix = f"{app.target.region}-docker.pkg.dev/{app.target.project_id}/{app.target.registry}/{app.target.service}@sha256:"
        same_build = same_image_inputs((prior or {}).get("spec"), spec)
        if app.provenance == "unavailable":
            plan["warnings"].append("The operator's exception provenance: unavailable is set for this application: the image is built fresh and released "
                                    "without signed provenance. Remove it from the controller configuration once the cause is fixed.")
        elif image and image.startswith(prefix) and same_build and recent(prior):
            # The same commit built the same way was released before: reuse its attested image.
            plan["reuse_image"] = image
            plan["reused_from_plan"] = prior["id"]
            plan["checks"].update(tests="passed_in_earlier_release", dependency_scan="passed_in_earlier_release")
        if image and image.startswith(prefix) and not recent(prior):
            plan["stale_image"] = True
        self.contain(plan, spec, app)
        plan["approval"] = release_approval(plan, principal.name)
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    def propose_config(self, principal, app_id=None, repository=None, path=None):
        """D26: one draft pull request with the files docs/DEVELOPER-CONTRACT.md asks for and the service lacks, never one that
        exists, for a registered application or, before registration, for a repository link and folder. The lock is compiled
        here in a container of the app's own Python when Docker is available (D27). The platform never merges it."""
        from types import SimpleNamespace

        from . import scaffold
        from .onboarding import normalize_path, normalize_repository, slug
        if app_id:
            app = self.app(principal, app_id, write=True)
            self.require_active(app_id)
            if app.central:
                raise ValueError(f"{app_id} is taken as it is: nothing is added to its repository, and its contract is "
                                 f"{contract_path(app.service)} in the platform repository")
            target, folder, name = SimpleNamespace(repository=app.repository, branch=app.branch), app.path, app.service
        else:
            if not repository:
                raise ValueError("Give an application, or a repository link (and the service's folder when it is not the root)")
            if "*" not in principal.applications or not principal.can_deploy:
                raise ValueError("Drafting files for a repository that is not registered needs an identity that may onboard: "
                                 "applications: ['*'] with can_deploy")
            target, folder, name = SimpleNamespace(repository=normalize_repository(repository), branch="main"), normalize_path(path), None
        sha = self.github.head(target)
        paths = self.github.tree(target.repository, sha)
        if folder and folder not in paths:
            raise ValueError(f"{target.repository} has no folder {folder} on {target.branch}")
        texts, too_large = {}, []
        for item in scaffold.files_to_read(paths, folder):
            try:
                found = self.github.contents(target.repository, sha, item)
            except ValueError as exc:
                # A real project has large files among the ones read for hints (a 241 KB module, a 190 KB test, a hashed lock):
                # one of them is skipped and named, it never stops the draft.
                if "exceeds 64 KiB" not in str(exc):
                    raise
                too_large.append(item)
                continue
            if found is not None:
                texts[item] = found["text"]
        requirements, locked = (f"{folder}/requirements.txt", f"{folder}/requirements.lock") if folder else ("requirements.txt", "requirements.lock")
        lock = {"text": None, "command": None, "error": None}
        if requirements in texts and locked not in paths and self.config.live_dispatch:
            lock = scaffold.compile_lock(texts[requirements], scaffold.detect(paths, texts, folder)["python"], path=folder)
        # Before registration the repository's own name is the last resort for the service's name (after the folder and
        # pyproject.toml), so the draft is not left saying my-service.
        result = scaffold.draft_files(paths, texts, folder, name, lock_text=lock["text"], fallback_name=target.repository.split("/")[1])
        if too_large:
            result["review"].append("Not read, over 64 KiB each, so the draft could not use them as hints: " + ", ".join(too_large) + ".")
        if lock["error"]:
            result["review"].append(f"requirements.lock was not compiled on the controller's machine ({lock['error']}): run `{lock['command']}` "
                                    "and commit the file.")
        view = {"repository": target.repository, "path": folder, "commit_sha": sha,
                "files": {target_path: item["purpose"] for target_path, item in sorted(result["files"].items())},
                "existing": result["existing"], "review": result["review"], "detected": result["detected"]}
        if not result["files"]:
            view["pull_request"] = None
            view["note"] = "Nothing to draft: the files the contract asks for already exist. Edit them through a normal reviewed pull request."
            return view
        if not self.config.live_dispatch:
            view["pull_request"] = "disabled"
            return view
        branch = "lab-deploy/contract-files" + (f"-{slug(folder.replace('/', '-'))}" if folder else "")
        view["pull_request"] = self.github.open_pull_request(
            target, branch, {target_path: item["content"] for target_path, item in result["files"].items()},
            "Add the files Fast Deploy needs" + (f" for {folder}" if folder else ""), scaffold.pull_request_body(result),
            "Add the files the Fast Deploy contract asks for (drafted; platform.yaml says reviewed: false)", draft=True, create_only=True)
        self.store.record_event("propose_configuration", name=target.repository, identity=principal.name, application=app_id,
                                outcome="opened", detail=f"{len(result['files'])} files" + (f" in {folder}" if folder else ""))
        return view

    def plans(self, principal):
        """Deployers see every plan of their applications, including auto-deploy's; read-only identities see their own."""
        if principal.can_deploy:
            return {"plans": self.store.plans(applications=self.visible_apps(principal))}
        return {"plans": self.store.plans(principal.name)}

    def load(self, principal, plan_id):
        """A plan this identity may see, with the identity that created it: deployers may see any plan of their applications."""
        if principal.can_deploy:
            found = self.store.fetch(plan_id)
            if found is None or found[1].get("application") not in self.visible_apps(principal):
                raise ValueError("Plan not found for this identity; list_plans shows the plans it may see, or create a new plan")
            return found
        return principal.name, self.store.get(principal.name, plan_id)

    def pinned_platform(self, app, repositories):
        """The platform commit the app's deploy workflow pins today (under the platform's name or a former one), or None
        when the workflow is absent."""
        found = self.github.contents(app.repository, app.branch, f".github/workflows/{app.workflow}")
        return pinned_commit(found["text"], repositories) if found else None

    def update_workflows(self, principal, app_id, platform_sha=None, allow_writes=False):
        """Regenerate the app's three workflows against a platform commit and open one pull request. Review and merge stay human."""
        app = self.app(principal, app_id, write=True)
        platform = self.config.platform
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before updating app workflows")
        if app.central:
            return {"application": app_id, "platform_commit": None, "current_pin": None, "pull_request": None,
                    "note": "This application is taken as it is: the platform repository's own pipeline releases it, so it has no workflows to update."}
        if not allow_writes and not self.config.live_dispatch:
            raise ValueError("Writes are disabled (live_dispatch: false); enable them, or run the command on the controller host")
        sha = platform_sha or platform.commit or self.github.branch_head(platform.repository, platform.branch)
        if not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
            raise ValueError("The platform commit must be a full 40-character SHA")
        current = self.pinned_platform(app, [platform.repository, *platform.former_repositories])
        # Pinned to this commit under the platform's current name: nothing to do. Under a former name, the files are rewritten
        # so the app follows the rename.
        if current == sha and self.pinned_platform(app, [platform.repository]) == sha:
            return {"application": app_id, "platform_commit": sha, "current_pin": current, "pull_request": None, "note": "Already pinned to this platform commit."}
        files = {}
        for name, kind in zip(TEMPLATES, ("deploy", "recover", "rightsize"), strict=True):
            template = self.github.contents(platform.repository, sha, f"templates/{name}")
            if template is None:
                raise ValueError(f"templates/{name} does not exist at platform commit {sha[:12]}")
            files[f".github/workflows/{app.workflows[kind]}"] = render(template["text"], platform.repository, sha, app)
        return {"application": app_id, "platform_commit": sha, "current_pin": current, "pull_request": self.github.update_workflows(app, sha, files)}

    def render_infrastructure(self, principal, app_id, state_bucket=None, bootstrap_image=None, settings=None, environment="staging"):
        """The app's Terraform root as the pipeline will plan it: literal inputs from the configuration, the reviewed spec and GitHub's ids.
        environment: staging (the default) or production, the app's production root under infra/gcp/apps-production (D37)."""
        app = self.app(principal, app_id, write=True)
        if environment not in ("staging", "production"):
            raise ValueError("environment is staging or production")
        target = app.target
        # The deploy identity trusts the workflows that release the app: its own repository's, or the platform repository's
        # pipeline in the environment staging-<service> for an application taken as it is (docs/ROADMAP.md D33).
        trusted = self.config.platform.repository if app.central else app.repository
        layout = {"github_environment": app.environment, "workflow_files": list(app.workflows.values()), "pipeline": "platform" if app.central else "repository"}
        directory = f"infra/gcp/apps/{target.service}"
        if environment == "production":
            production = self.config.production
            if production is None or app.production is None:
                raise ValueError(f"{app_id} has no production yet: set production in the controller configuration and call add_production")
            # Promotions run the platform repository's promote.yml in production-<service>, whatever released staging.
            target, trusted = app.production.target, self.config.platform.repository
            layout = {"github_environment": app.production.environment, "workflow_files": list(PRODUCTION_WORKFLOWS), "pipeline": "platform"}
            directory = f"{PRODUCTION_ROOTS}/{target.service}"
            state_bucket = state_bucket or production.state_bucket
        ids = self.github.repository_ids(trusted)
        note, needs, exposure = None, None, "authenticated"
        try:
            spec = self.spec_at(app, self.github.head(app))
            health_path, needs, exposure = spec.health_path, spec.needs.model_dump(), spec.exposure
        except ValueError as exc:
            health_path, note = "/health", f"Defaults used until the app has a reviewed platform.yaml: {exc}"
        files = app_root(target.service, project_id=target.project_id, region=target.region, state_bucket=state_bucket or f"{target.project_id}-tfstate",
                         github_repository=trusted, github_repository_id=ids["repository_id"], github_owner_id=ids["owner_id"],
                         bootstrap_image=bootstrap_image or BOOTSTRAP_IMAGE, health_path=health_path, settings={**app.infra, **(settings or {})}, needs=needs,
                         exposure=exposure, viewers=self.viewers(app), environment=environment, **layout)
        return {"application": app_id, "service": target.service, "environment": environment, "directory": directory, "files": files, "note": note}

    def add_production(self, principal, app_id):
        """Give an application its production (docs/ROADMAP.md D37), one step at a time and safe to repeat: its production
        target registered by convention in the production project; its production root proposed as a pull request that a
        person merges (production infrastructure is never merged by the controller), which the pipeline plans and applies
        with the production identities; then its environment production-<service> in the platform repository, whose
        required reviewers approve every promotion, with the target's variables. Returns where it stands and what is next."""
        app = self.app(principal, app_id, write=True)
        if not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        production, platform = self.config.production, self.config.platform
        if production is None:
            raise ValueError("Set production in the controller configuration first: the production project and its reviewers (docs/SETUP.md 7n)")
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before adding production")
        service, steps = app.service, {}
        if app.production is None:
            entry = {"environment": f"production-{service}", "target": {
                "project_id": production.project_id, "region": production.region, "service": service, "registry": service,
                "deploy_service_account": f"{service}-deploy@{production.project_id}.iam.gserviceaccount.com",
                "probe_service_account": f"{service}-probe@{production.project_id}.iam.gserviceaccount.com",
                "wif_provider": f"projects/{production.project_number}/locations/global/workloadIdentityPools/{service}-gh/providers/github",
                "recovery_bucket": f"{production.project_id}-{service}-recovery"}}
            Production.model_validate(entry)
            self.store.register_production(app_id, entry)
            self.refresh_registry()
            app = self.config.applications[app_id]
            self.store.record_event("production", name=app_id, identity=principal.name, application=app_id, outcome="registered", detail=production.project_id)
            steps["registered"] = entry["target"]
        rendered = self.render_infrastructure(principal, app_id, environment="production")
        on_main = self.github.contents(platform.repository, platform.branch, f"{rendered['directory']}/main.tf")
        base = {"application": app_id, "environment": app.production.environment, "project_id": app.production.target.project_id, "steps": steps}
        if on_main is None or on_main["text"] != rendered["files"]["main.tf"]:
            proposed = self.propose_infrastructure(principal, app_id, allow_writes=True, through="add_production", environment="production")
            pull_request = proposed["pull_request"]
            return {**base, "state": "infrastructure", "pull_request": pull_request,
                    "next_action": f"A person merges {pull_request.get('url') or 'the production infrastructure pull request'} once its plan has run "
                                   "(production infrastructure is never merged by the controller), then approves the apply in the environment "
                                   "infra-production. Ask again after the apply for the environment and its approvers."}
        target = app.production.target
        variables = {"GCP_PROJECT_ID": target.project_id, "GCP_REGION": target.region, "CLOUD_RUN_SERVICE": target.service,
                     "ARTIFACT_REPOSITORY": target.registry, "WIF_PROVIDER": target.wif_provider, "DEPLOY_SERVICE_ACCOUNT": target.deploy_service_account,
                     "PROBE_SERVICE_ACCOUNT": target.probe_service_account, "RECOVERY_BUCKET": target.recovery_bucket}
        self.github.ensure_protected_environment(platform.repository, app.production.environment, platform.branch, production.reviewers)
        self.github.set_environment_variables(platform.repository, app.production.environment, variables)
        self.store.record_event("production", name=app_id, identity=principal.name, application=app_id, outcome="ready",
                                detail=f"{app.production.environment}: reviewers {', '.join(production.reviewers)}")
        return {**base, "state": "ready", "reviewers": list(production.reviewers), "variables": sorted(variables),
                "next_action": f"Production is ready for {app_id}: its root is on main, and every promotion into {app.production.environment} waits "
                               f"for {' or '.join(production.reviewers)} to approve it on GitHub."}

    def viewers(self, app):
        """Who may open the app's pages: the lab-wide list plus the app's own, as the root grants them."""
        onboarding = self.config.onboarding
        return sorted(set((onboarding.default_viewers if onboarding else []) + list(app.viewers)))

    def propose_infrastructure(self, principal, app_id, allow_writes=False, through=None, **render):
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
        production = rendered.get("environment") == "production"
        where = "production" if production else "staging"
        body = (f"Terraform root for `{rendered['service']}` in {where}, rendered by the controller from the configuration, the app's reviewed "
                "platform.yaml and GitHub's immutable ids.\n\nThe infrastructure plan workflow posts what an apply would do; "
                "merging applies exactly that with the pipeline identity and publishes the outputs to the app's GitHub environment. "
                "Nothing is applied by this pull request itself.\n\n"
                f"Requested by `{principal.name}`{' through ' + through if through else ''} (written by the controller, not by Claude); "
                "with `may_merge_own_requests: false` someone else merges it (docs/ROADMAP.md D29).")
        branch = f"lab-deploy/infra-{rendered['service']}" + ("-production" if production else "")
        title = ("Production infrastructure for " if production else "Infrastructure for ") + rendered["service"]
        pull_request = self.github.open_platform_pull_request(platform.repository, platform.branch, branch, paths, title, body,
                                                              f"Render the {where} infrastructure root for {rendered['service']}")
        return {"application": app_id, "service": rendered["service"], "environment": rendered.get("environment", "staging"), "files": sorted(paths),
                "pull_request": pull_request, "note": rendered["note"]}

    def applications(self, principal):
        """Registered apps this identity may see, each with the live release this controller recorded and what main holds beyond it."""
        result = {}
        for app_id in self.visible_apps(principal):
            app = self.config.applications[app_id]
            entry = app.model_dump()
            entry["paused"] = bool(self.config.paused or app.paused)
            try:
                entry["platform_pin"] = self.cached(("pin", app_id), 300, lambda app=app: self.platform_pin(app))
            except Exception as exc:  # noqa: BLE001 - the pin is informational here; dispatch enforces it
                entry["platform_pin"] = {"commit": None, "supported": None, "note": f"Could not read the app's workflow: {str(exc)[:120]}"}
            try:
                entry["schedules"] = self.cached(("schedules", app_id), 300, lambda app=app: self.schedule_health(app))
            except Exception as exc:  # noqa: BLE001 - a GitHub hiccup must not hide the application list
                entry["schedules"] = {"warning": f"Could not read the scheduled workflows: {str(exc)[:120]}"}
            record = self.store.onboarding(app_id)
            if record is not None and record.get("state") == "complete":
                from .onboarding import summary
                entry["onboarding"] = {"minutes": (record.get("measures") or {}).get("minutes"), "blocked": (record.get("measures") or {}).get("blocked", 0),
                                       "interventions": (record.get("measures") or {}).get("interventions", 0), "summary": summary(record)}
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

    def spending_guards(self, app):
        """The layers that stop spending in this application's project, as the operator recorded them (docs/SETUP.md 7i)."""
        project = app.target.project_id
        amount = (getattr(self.config, "cloud_run_spend_caps", None) or {}).get(project)
        if amount:
            native = {"status": "recorded", "amount_usd_month": amount,
                      "covers": "Cloud Run only: every service in the project, counted on gross estimated cost before credits",
                      "when_reached": "every Cloud Run service in the project answers 5xx until the cap is lifted in Cloud Billing (up to an hour to "
                                      "recover) or the month ends; Cloud SQL, buckets and topics keep running and billing"}
        else:
            native = {"status": "not_recorded",
                      "advice": f"No native Cloud Run spend cap is recorded for project {project}. Create one in Cloud Billing (docs/SETUP.md 7i) and "
                                "record it under cloud_run_spend_caps in the controller configuration; it is the backstop that needs nothing of this platform to work."}
        return {"project_id": project, "cloud_run_spend_cap": native,
                "platform_cap": "Once the foundation's budget and cap workflow are applied, the budget's notifications close ingress on every managed "
                                "service and stop every managed Cloud SQL instance at the budget line (docs/RUNBOOK.md, spending cap)"}

    def cost_summary(self, app, spec, requests_per_month=None, average_request_seconds=None):
        """Bounds and findings from list prices and the reviewed spec, with the operator's ceiling applied."""
        summary = estimate(spec, requests_per_month, average_request_seconds)
        summary["findings"] = findings(spec)
        summary["spending_guards"] = self.spending_guards(app)
        if summary["spending_guards"]["cloud_run_spend_cap"]["status"] == "not_recorded":
            summary["findings"].insert(0, {"id": "no_native_spend_cap", "severity": "warning", "setting": "cloud_run_spend_caps (controller configuration)",
                                           "current": None, "message": summary["spending_guards"]["cloud_run_spend_cap"]["advice"]})
        limit = app.cost_ceiling_usd
        compared = round(summary["compute_ceiling_usd_month"] + summary["needs_floor_usd_month"], 2)
        over = limit is not None and compared > limit
        summary["ceiling_policy"] = {"limit_usd_month": limit, "compared_usd_month": compared,
                                     "status": "not_configured" if limit is None else ("exceeds_limit" if over else "within_limit")}
        summary["note"] = ("Bounds from list prices and the reviewed configuration, not a bill forecast: the ceiling policy compares the compute bound "
                           "plus the needs' floor with the operator's limit; request fees, egress and the per-image scan are extra and unbounded by it. "
                           "Measured right-sizing comes from the rightsizing workflow report (get_cost_report).")
        return summary

    def cost_policy(self, app, spec):
        summary = self.cost_summary(app, spec)
        if summary["ceiling_policy"]["status"] == "exceeds_limit":
            raise ValueError(f"Cost policy: worst-case compute plus the needs' floor, ${summary['ceiling_policy']['compared_usd_month']:.2f}/month, exceeds the "
                             f"operator ceiling of ${app.cost_ceiling_usd:.2f}/month; lower scaling.maximum or resources, drop a need, or raise cost_ceiling_usd")
        return summary

    def cost(self, principal, app_id, requests_per_month=None, average_request_seconds=None):
        app = self.app(principal, app_id)
        sha = self.github.head(app)
        spec = self.spec_at(app, sha)
        return {"commit_sha": sha, "spec_hash": spec_hash(spec), "reviewed": spec.reviewed,
                **self.cost_summary(app, spec, requests_per_month, average_request_seconds)}

    @staticmethod
    def artifact_name(app, name):
        """An artifact of the application's runs: the platform repository's pipeline names one per service."""
        return f"{name}-{app.service}" if app.central else name

    @classmethod
    def artifact_names(cls, app, name):
        """The artifact's name as a keyword, given only when it differs from the default an application's own workflows use."""
        return {"name": cls.artifact_name(app, name)} if app.central else {}

    def cost_report(self, principal, app_id):
        app = self.app(principal, app_id)
        result = self.github.cost_report(app, workflow=app.workflows["rightsize"], **self.artifact_names(app, "cost-report"))
        if isinstance(result, dict):
            result["spending_guards"] = self.spending_guards(app)
        return result

    def propose_rightsizing(self, principal, app_id):
        """Turn the latest measured proposals into a pull request against platform.yaml; review and merge stay human."""
        app = self.app(principal, app_id, write=True)
        self.require_active(app_id)
        if not self.config.live_dispatch:
            raise ValueError("Writes are disabled (live_dispatch: false); enable them before proposing changes")
        latest = self.github.cost_report(app, workflow=app.workflows["rightsize"], **self.artifact_names(app, "cost-report"))
        report = latest.get("report")
        if not report:
            raise ValueError("No cost report yet; the right-sizing workflow runs weekly, or dispatch it in GitHub Actions")
        proposals = [p for p in (report.get("rightsizing") or {}).get("recommendations") or []
                     if p.get("setting") in SETTINGS and isinstance(p.get("recommended"), int) and not isinstance(p.get("recommended"), bool)]
        if not proposals:
            return {"application": app_id, "pull_request": None, "changes": [], "note": "The latest report proposes no concrete setting change."}
        if app.central:
            # The contract the platform keeps (its source block survives a regeneration: the file is read and written as data).
            text = self.contracts.require(app)["text"]
            new_text, applied, regenerated = apply_settings(text, {p["setting"]: p["recommended"] for p in proposals})
        else:
            head = self.describe_head(app)
            text = self.github.file(app, head["sha"], app.repo_path("platform.yaml"))
            new_text, applied, regenerated = apply_settings(text, {p["setting"]: p["recommended"] for p in proposals})
        if new_text == text:
            return {"application": app_id, "pull_request": None, "changes": [], "note": "The contract already matches the proposals."}
        spec = parse_contract(new_text)[0] if app.central else parse_spec(new_text)
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
        if app.central:
            # The contract lives in the platform repository: the proposal is a pull request there, and its merge releases it.
            platform = self.config.platform
            pull_request = self.github.open_platform_pull_request(platform.repository, platform.branch, f"{branch}-{app.service}", {contract_path(app.service): new_text},
                                                                  f"Right-size {app.service} from measured usage", body, f"Apply measured right-sizing proposals to {app.service}")
        else:
            pull_request = self.github.open_pull_request(app, branch + app.suffix, {app.repo_path("platform.yaml"): new_text}, "Right-size platform.yaml from measured usage", body,
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
        layout = {"path": app.path, "workflow": app.workflow, "github_environment": app.environment}
        if plan.get("layout", {"path": "", "workflow": "deploy-gcp.yml", "github_environment": "staging"}) != layout:
            raise ValueError("The application's folder or workflow names changed since this plan; create a new plan")
        if plan.get("provenance", "required") != app.provenance:
            raise ValueError("The application's provenance setting changed since this plan; create a new plan")
        if bool(plan.get("contract")) != app.central:
            raise ValueError("The application's contract moved between its repository and the platform's since this plan; create a new plan")
        if plan.get("pinned_commit"):
            self.ancestor_of_branch(app, plan["sha"])
        elif self.github.head(app) != plan["sha"]:
            raise ValueError("Branch moved; create a new plan")
        if app.central and self.contracts.require(app)["source"].commit != plan["sha"]:
            raise ValueError("The contract moved to another commit since this plan; create a new plan")
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
        link_run(plan)
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
        """Plan the tip of main and dispatch it in one step when every policy passes. For an application taken as it is, the tip
        is released through the pull request that moves source.commit (docs/ROADMAP.md D32): merged at once when the gate
        allows and the checks have passed, else it waits for them or for a person, and the merge releases it."""
        app = self.app(principal, app_id, write=True)
        if not app.central:
            plan = self.plan(principal, app_id)
            return self.deploy(principal, plan["id"])
        self.require_active(app_id)
        if not self.config.live_dispatch:
            raise ValueError("Dispatch is disabled (live_dispatch: false); nothing can be released from here")
        request = self.contracts.request_release(principal, app_id, app)
        if request["state"] in ("current", "merged"):
            result = self.contracts.release_now(principal, app_id)
            if request["state"] == "merged":
                result = {**result, "release_request": request}
            return result
        return {"application": app_id, "state": "release_requested", "release_request": request, "sha": request["target"],
                "next_action": (f"Release pull request #{request['pull_request']} moves {app.service} to {request['target'][:12]}: "
                                + (f"waiting for {request['waiting']}." if request.get("waiting") else "it merges once the checks pass.")
                                + " Merging it starts the release within a minute; ask for list_plans or the application's status then.")}

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
            "previous": self.previous_release(app_id, receipt["previous_traffic"]),
        }
        try:
            record["dispatch_result"] = self.github.dispatch_workflow(app, app.workflows["recover"], {"rollback_plan_id": live["id"]})
        except Exception:
            self.store.add(principal.name, record, "dispatch_unknown")
            raise ValueError("Rollback dispatch outcome unknown. Inspect GitHub before requesting it again.") from None
        self.store.add(principal.name, record, "dispatched")
        return {**record, "state": "dispatched",
                "next_action": f"Rollback dispatched: traffic returns to {record['to_traffic']} within about a minute. Ask for status with this id."}

    def previous_release(self, app_id, split):
        """The release whose revision held the most traffic before the live one, so a rollback can name a commit rather than a
        revision (found live: `lab-notebook-r9111fd55b0d94066`); only the revision when this controller did not release it."""
        if not isinstance(split, dict) or not split:
            return None
        revision = max(split, key=lambda name: split[name] or 0)
        for item in self.store.plans(applications=[app_id]):
            if item.get("revision") == revision and item.get("sha"):
                return {"revision": revision, "sha": item["sha"], "message": item.get("summary")}
        return {"revision": revision}

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
            link_run(plan)
            plan.update(summarize(workflow, plan["checks"], plan.get("kind")))
            if plan["state"] == "awaiting_workflow" and self.dispatch_lost(plan):
                plan["state"] = "dispatch_lost"
                self.store.finish(owner, plan, "dispatch_lost")
            elif plan["state"] in TERMINAL:
                self.record_outcome(owner, app, plan, workflow)
        elif plan["state"] in {"failed", "cancelled", "timed_out"} and not (plan.get("recovery_run") or {}).get("conclusion"):
            self.record_recovery(owner, app, plan)
        plan["owner"] = owner
        plan["phases"] = release_phases(plan.get("receipt"))
        plan["events"] = self.store.events(owner, plan_id)
        plan["next_action"] = advice(plan)
        return plan

    @staticmethod
    def dispatch_lost(plan, now=None):
        """No run is visible: lost when the dispatch itself never completed and two minutes passed, or the plan has expired."""
        now = now or time.time()
        created = plan["expires_at"] - 900
        if plan.get("dispatch_state") in {"dispatching", "dispatch_unknown"}:
            return now > created + 120
        return now > plan["expires_at"]

    def record_outcome(self, owner, app, plan, workflow):
        """A finished run is stored once with its receipt, so history survives GitHub's artifact retention."""
        if plan.get("kind") == "rollback":
            fetch = getattr(self.github, "artifact_json", None)
            files = fetch(app, workflow["id"], self.artifact_name(app, "recovery-receipt"), ("recovery-receipt.json",)) if callable(fetch) and workflow.get("id") else None
            plan["receipt"] = (files or {}).get("recovery-receipt.json")
            if plan["state"] == "succeeded" and (plan["receipt"] or {}).get("state") == "rolled_back":
                self.store.mark(plan["from_plan"], rolled_back=True, rolled_back_by=plan["id"])
            plan["vulnerabilities"] = None
        else:
            fetch = getattr(self.github, "release_receipt", None)
            receipts = fetch(app, workflow["id"]) if callable(fetch) and workflow.get("id") else None
            plan["receipt"] = (receipts or {}).get("release")
            plan["vulnerabilities"] = (receipts or {}).get("vulnerabilities")
            if plan["state"] in {"failed", "cancelled", "timed_out"} and (plan["receipt"] or {}).get("state") == "succeeded":
                # The release finished before the run ended (a cancel that came too late, a later step that failed): its
                # receipt, not the run's conclusion, says what serves traffic, so it is the live release.
                plan["run_ended"] = plan["state"]
                plan["state"] = "succeeded"
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
        plan["recovery_run"] = lookup(app, plan["workflow"], workflow=app.workflows["recover"], **self.artifact_names(app, "recovery-receipt"))
        if persist and (plan["recovery_run"] or {}).get("conclusion"):
            self.persist(owner, plan)

    def persist(self, owner, plan):
        record = {key: value for key, value in plan.items() if key not in {"events", "phases", "next_action", "owner"}}
        self.store.finish(owner, record, plan["state"])

