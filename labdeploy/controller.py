import contextlib
import datetime
import json
import re
import sqlite3
import threading
import time
import types
import uuid

import httpx

from . import aftercare, operations
from .analyzer import COMPOSE_FILES, FILES, analyze
from .bake import MAX_BAKE_SECONDS
from .config import Application, Principal, Production
from .contract import Contracts, contract_path
from .cost import domain_floor, estimate, findings
from .github import explain
from .infra import BOOTSTRAP_IMAGE, PRODUCTION_ROOTS, PRODUCTION_WORKFLOWS, app_root, root_jobs
from .jobbroker import Broker
from .jobimages import base_of
from .jobs import Keeper
from .models import AppSpec, containment, enforce, job_egress, needs_env, parse_contract, parse_spec, spec_hash
from .resolution_protocol import ResolutionPending
from .resolver import Resolver
from .rightsize import SETTINGS, apply_settings
from .status import TERMINAL, advice, diagnose, release_phases, summarize
from .updates import PlatformUpdates
from .workflows import TEMPLATES, pinned_commit, render

DISPATCHED = {"dispatched", "dispatch_unknown", "dispatching"}
DISPATCH_GRACE_SECONDS = 120  # a dispatch being sent this recently is not lost merely because GitHub shows no run yet
# How long a run that succeeded with a check not reported yet is read again (found on 2026-10-19: GitHub marked a
# promotion's run completed while its job and steps still read in progress, and the promotion stayed unverified).
RECHECK_SECONDS = 6 * 3600
WAIT_LIMIT = 45  # seconds; keeps a waiting status call inside the client's tool timeout
# What a weekly cost report says about the application itself: its service's settings, usage and bounds, the proposals and
# the drift from its contract. An identity that sees only some applications is given these; the rest of the report (the
# projects' bill, Google's recommender for every service of the project) is shown to an identity that sees every application.
OWN_REPORT = ("generated_at", "service", "project_id", "window_days", "live_settings", "estimate", "findings", "metrics", "rightsizing",
              "drift", "onboarding", "serving_tags")
HIDDEN_COSTS = ("Of the bill, only the lines labelled with this token's applications: the whole project's bill (by Google service, its "
                "totals, the month so far and its budget) and other applications' spend are shown to an identity that sees every application.")
HIDDEN_STATUS = ("Other applications, and the background jobs' results and errors (which can name any application), are shown to an "
                 "identity that sees every application.")
HIDDEN_EVENTS = ("Events about the applications this token may see, and its own: the rest of the audit trail is shown to an identity "
                 "that may deploy every application.")


def own_spend(actual, labels):
    """A report's actual spend reduced to the lines labelled with these applications (the lab-deploy-app label is the
    service's name), each with its line of the window before, and the application's own share. The bill by Google service,
    the projects' totals and names, and every other label's line go; a warning that says why there is no bill stays."""
    def own(rows):
        return [row for row in rows if row.get("app") in labels]
    found = {key: actual[key] for key in ("window_days", "data_through", "warning") if key in actual}
    if actual.get("by_app") is not None:
        found["by_app"] = own(actual["by_app"])
        found["note"] = ("Net of credits, from Google's billing export; up to a day behind. Firestore and Cloud Run request fees carry no "
                         "label, so they are not in these lines.")
    previous = actual.get("previous")
    if isinstance(previous, dict) and previous.get("by_app") is not None:
        found["previous"] = {"window_days": previous.get("window_days"), "by_app": own(previous["by_app"])}
    movers = (actual.get("movers") or {}).get("by_app")
    if movers is not None:
        found["movers"] = {"by_app": own(movers)}
    if isinstance(actual.get("app"), dict) and actual["app"].get("name") in labels:
        found["app"] = actual["app"]
    return found


def copy_to_bucket(config, files, run=None):
    """Upload files to the backup bucket's folder (`backup_bucket`, `backup_prefix`: controller/ for a laptop, machine/ for the
    machine) with this host's own gcloud sign-in, as the nightly backup and the settings page do. `files` pairs a local path
    with its name in the bucket. Returns the gs:// targets written, or [] when no bucket is configured."""
    import subprocess
    bucket = getattr(config, "backup_bucket", None)
    if not bucket:
        return []
    prefix = getattr(config, "backup_prefix", None) or "controller"
    targets = [(str(path), f"gs://{bucket}/{prefix}/{name}") for path, name in files]
    for source_path, target in targets:
        (run or subprocess.run)(["gcloud", "storage", "cp", source_path, target], check=True, capture_output=True, text=True, timeout=120)
    return [target for _, target in targets]


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
            # What follows a platform release (labdeploy/updates.py): the machine's record under `machine`, each application's
            # re-pin under `workflows:<id>`.
            db.execute("CREATE TABLE IF NOT EXISTS platform_updates (id TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)")
            # The daily digest (docs/ROADMAP.md D60), one per UTC day, with where it was sent.
            db.execute("CREATE TABLE IF NOT EXISTS digests (day TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")
            # The issue the digest and the alerts are posted to, per platform repository, kept from the moment it is opened.
            db.execute("CREATE TABLE IF NOT EXISTS digest_issues (repository TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("PRAGMA journal_mode=WAL")  # readers never block the scheduler's and the request handlers' writes
            db.execute("BEGIN IMMEDIATE")
            operations.migrate(db)

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

    def forget_onboarding(self, app_id):
        """Drop an onboarding record alone (`lab-deploy forget-onboarding`, which first makes sure it registered nothing)."""
        with self.connect() as db:
            return bool(db.execute("DELETE FROM onboardings WHERE id=?", (app_id,)).rowcount)

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

    def platform_update(self, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM platform_updates WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_platform_update(self, key, record):
        record["updated_at"] = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO platform_updates VALUES (?, ?, ?)", (key, record["updated_at"], json.dumps(record)))

    def platform_updates(self, prefix=""):
        with self.connect() as db:
            rows = db.execute("SELECT id, payload FROM platform_updates WHERE substr(id, 1, ?) = ? ORDER BY id", (len(prefix), prefix)).fetchall()
        return {row[0]: json.loads(row[1]) for row in rows}

    def save_digest(self, day, record):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, ?)", (day, time.time(), json.dumps(record)))
            db.execute("DELETE FROM digests WHERE created < ?", (time.time() - 90 * 86_400,))

    def digest_issue(self, repository):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM digest_issues WHERE repository=?", (repository.lower(),)).fetchone()
        return json.loads(row[0]) if row else None

    def save_digest_issue(self, repository, issue):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO digest_issues VALUES (?, ?, ?)", (repository.lower(), time.time(), json.dumps(issue)))

    def digest(self, day=None):
        """The digest of one UTC day, or the newest one; None when there is none."""
        with self.connect() as db:
            row = (db.execute("SELECT payload FROM digests WHERE day=?", (day,)) if day
                   else db.execute("SELECT payload FROM digests ORDER BY day DESC LIMIT 1")).fetchone()
        return json.loads(row[0]) if row else None

    def digests(self, since_day=""):
        """Every digest kept (90 days), oldest first; from one UTC day (YYYY-MM-DD) on when given. The settings page draws
        the month's spend from their cost lines."""
        with self.connect() as db:
            rows = db.execute("SELECT day, payload FROM digests WHERE day >= ? ORDER BY day", (since_day,)).fetchall()
        return [{**json.loads(payload), "day": day} for day, payload in rows]

    def history(self, since=None):
        """Every plan and rollback record with its state, oldest first; with `since`, only those made after that moment.
        For the release reports and the trust numbers (docs/ROADMAP.md D57, D61)."""
        query, params = "SELECT owner, state, payload FROM plans", ()
        if since is not None:
            query += " WHERE json_extract(payload, '$.expires_at') >= ?"
            params = (since + 900,)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY rowid", params).fetchall()
        return [{**json.loads(payload), "state": state, "owner": owner} for owner, state, payload in rows]

    @contextlib.contextmanager
    def connect(self):
        # Commit or roll back exactly like sqlite3's own context manager, then always close the connection.
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, owner, plan, state="planned"):
        with self.connect() as db:
            db.execute("INSERT INTO plans(id,owner,state,payload) VALUES (?, ?, ?, ?)", (plan["id"], owner, state, json.dumps(plan)))
            db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))
            operations.record(db, owner, plan, state)

    def mark(self, plan_id, **fields):
        """Annotate a stored plan without changing its state, for example when a later rollback supersedes it."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                return
            payload = {**json.loads(row[0]), **fields}
            db.execute("UPDATE plans SET payload=?,version=version+1 WHERE id=?", (json.dumps(payload), plan_id))

    def get(self, owner, plan_id):
        with self.connect() as db:
            row = db.execute("SELECT state,payload,version FROM plans WHERE id=? AND owner=?", (plan_id, owner)).fetchone()
        if row is None:
            raise ValueError("Plan not found for this identity; list_plans shows the plans it may see, or create a new plan")
        return {**json.loads(row[1]), "state": row[0], "_store_revision": row[2]}

    def fetch(self, plan_id):
        """A plan by id regardless of who created it, with its owner; None when unknown."""
        with self.connect() as db:
            row = db.execute("SELECT owner, state, payload, version FROM plans WHERE id=?", (plan_id,)).fetchone()
        return (row[0], {**json.loads(row[2]), "state": row[1], "_store_revision": row[3]}) if row else None

    def claim(self, owner, plan_id):
        with self.connect() as db:
            changed = db.execute("UPDATE plans SET state='dispatching',version=version+1 WHERE id=? AND owner=? AND state='planned'", (plan_id, owner)).rowcount
            if changed:
                db.execute("INSERT INTO audit VALUES (?, ?, ?, 'dispatching')", (time.time(), owner, plan_id))
                operations.transition(db, plan_id, "dispatching")
        return bool(changed)

    def finish(self, owner, plan, state):
        """Store a plan's new state; a concurrent writer that got there first wins, so an outcome is recorded once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT state, version FROM plans WHERE id=? AND owner=?", (plan["id"], owner)).fetchone()
            if before is None:
                return False
            revision = plan.get("_store_revision", before[1])
            payload = {key: value for key, value in plan.items() if key != "_store_revision"}
            changed = db.execute("UPDATE plans SET state=?,payload=?,version=version+1 WHERE id=? AND owner=? AND version=?",
                                 (state, json.dumps(payload), plan["id"], owner, revision)).rowcount
            if changed and before[0] != state:  # re-saving a record with more detail is not a new event
                db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan["id"], state))
            if changed:
                operations.transition(db, plan["id"], state)
                plan["_store_revision"] = revision + 1
        return bool(changed)

    def release_claim(self, owner, plan_id, state="planned"):
        """Nothing was sent (the intent or a check failed first): give the claim back, or close a record that has no
        planned state, so the queue holds no prepared action that will never be sent."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = db.execute("SELECT state FROM outbox WHERE operation_id=?", (plan_id,)).fetchone()
            if action is not None and action[0] != "prepared":
                return  # it may have been sent: reconciliation decides, never a give-back
            changed = db.execute("UPDATE plans SET state=?,version=version+1 WHERE id=? AND owner=? AND state='dispatching'",
                                 (state, plan_id, owner)).rowcount
            if changed:
                db.execute("DELETE FROM outbox WHERE operation_id=? AND state='prepared'", (plan_id,))
                operations.transition(db, plan_id, state)
                db.execute("INSERT INTO audit VALUES (?, ?, ?, ?)", (time.time(), owner, plan_id, state))

    def recently_sent(self, plan_id, now=None, grace=DISPATCH_GRACE_SECONDS):
        """True while a dispatch is still being prepared or sent: no run is visible yet, and that is not a loss."""
        with self.connect() as db:
            row = db.execute("SELECT updated FROM outbox WHERE operation_id=? AND state IN ('prepared','sending')", (plan_id,)).fetchone()
        return row is not None and (now or time.time()) - row[0] < grace

    def start_action(self, identifier):
        """One sender only. An interrupted sending action is reconciled, never reclaimed for retry."""
        with self.connect() as db:
            changed = db.execute("UPDATE outbox SET state='sending',attempts=attempts+1,updated=? WHERE operation_id=? AND state='prepared'",
                                 (time.time(), identifier)).rowcount
        if not changed:
            raise ValueError("Operation already sent or uncertain; reconcile before another mutation")

    def recovery_mode(self, enabled=None):
        with self.connect() as db:
            if enabled is not None:
                db.execute("INSERT OR REPLACE INTO operation_settings VALUES ('recovery_mode', ?)", ("true" if enabled else "false",))
            return db.execute("SELECT value FROM operation_settings WHERE name='recovery_mode'").fetchone() == ("true",)

    def operation_status(self):
        with self.connect() as db:
            rows = db.execute("SELECT state, COUNT(*), MIN(updated) FROM outbox WHERE state!='completed' GROUP BY state").fetchall()
        return {"recovery_mode": self.recovery_mode(), "outbox": [{"state": state, "count": count, "oldest_at": oldest} for state, count, oldest in rows]}

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

    def in_flight(self, limit=10, now=None):
        """Plans whose run has not been seen to end, newest first, with their application: the background follow-up asks
        GitHub about them, so an outcome is recorded even when nobody asks. A run that succeeded while some check had not
        reported yet (completed_unverified) is asked about again for RECHECK_SECONDS: GitHub can mark a run completed before
        its steps are."""
        since = (now or time.time()) - RECHECK_SECONDS
        with self.connect() as db:
            rows = db.execute("SELECT id, json_extract(payload, '$.application') FROM plans WHERE state IN ('dispatching', 'dispatched', 'dispatch_unknown') "
                              "OR (state = 'completed_unverified' AND json_extract(payload, '$.outcome.conclusion') = 'success' "
                              "AND json_extract(payload, '$.outcome.recorded_at') > ?) "
                              "ORDER BY COALESCE((SELECT checked FROM operation_checks WHERE id=plans.id),0),rowid LIMIT ?", (since, limit)).fetchall()
            db.executemany("INSERT OR REPLACE INTO operation_checks VALUES (?,?)", [(row[0], now or time.time()) for row in rows])
        return [(row[0], row[1]) for row in rows]

    def dispatching(self, now=None):
        """How many plans are between their claim and GitHub's answer, the moment a restart would leave a dispatch unknown.
        Only plans that have not expired count: one left behind by a process that was killed is not on its way any more."""
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM plans WHERE state='dispatching' AND json_extract(payload, '$.expires_at') > ?",
                              (now or time.time(),)).fetchone()[0]

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

    def latest_success(self, application, sha=None, include_rolled_back=False, signed=False, kind=None):
        """The newest release that succeeded for an application, by default excluding ones rolled back since (optionally for one commit).
        `signed`: only releases whose image carries signed provenance, so not one released under the operator's exception.
        `kind`: None for staging releases, "promotion" for what was promoted to production (docs/ROADMAP.md D37)."""
        query = "SELECT payload FROM plans WHERE state='succeeded' AND json_extract(payload, '$.application')=?"
        params = [application]
        if kind is None:
            query += " AND json_extract(payload, '$.kind') IS NULL"
        else:
            query += " AND json_extract(payload, '$.kind')=?"
            params.append(kind)
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

    def newest_event(self, kind, outcome=None, name=None):
        """The newest audit event of one kind, optionally with one outcome and one name; None when there is none."""
        query, params = "SELECT at, identity, name, outcome, detail FROM events WHERE kind=?", [kind]
        if outcome is not None:
            query += " AND outcome=?"
            params.append(outcome)
        if name is not None:
            query += " AND name=?"
            params.append(name)
        with self.connect() as db:
            row = db.execute(query + " ORDER BY at DESC, rowid DESC LIMIT 1", params).fetchone()
        return dict(zip(("at", "identity", "name", "outcome", "detail"), row, strict=True)) if row else None

    def recent_events(self, limit=50, application=None, scope=None):
        """The newest events first (list_events, GET /events): one application's when named, and of them those `scope` may see
        (scope_clause; None for every event)."""
        query, params = "SELECT at, identity, client, kind, name, application, outcome, duration_ms, detail FROM events", []
        clauses = []
        if application is not None:
            clauses.append("application=?")
            params.append(application)
        if scope is not None:
            clause, values = self.scope_clause(scope)
            clauses.append(clause)
            params += values
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*params, limit)).fetchall()
        keys = ("at", "identity", "client", "kind", "name", "application", "outcome", "duration_ms", "detail")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # ---- the audit trail as the settings page's Activity view filters it
    @staticmethod
    def scope_clause(scope):
        """The condition for the events `scope` may see (Controller.event_scope): with (an identity, the applications it may
        see), the events about those applications or made by that identity; with (an identity, None), that identity's own."""
        who, visible = scope
        if visible is None:
            return "identity=?", [who]
        marks = ",".join("?" for _ in visible) or "''"  # only placeholders are interpolated; every value is bound
        return f"(application IN ({marks}) OR identity=?)", [*visible, who]

    @staticmethod
    def event_filter(kind=None, identity=None, application=None, scope=None, since=None):
        """A WHERE clause and its values: one kind, identity or application, and `scope` (scope_clause; None for every
        event)."""
        clauses, params = [], []
        for column, value in (("kind", kind), ("identity", identity), ("application", application)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if since is not None:
            clauses.append("at >= ?")
            params.append(since)
        if scope is not None:
            clause, values = Store.scope_clause(scope)
            clauses.append(clause)
            params += values
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def find_events(self, limit=100, kind=None, identity=None, application=None, scope=None, since=None):
        where, params = self.event_filter(kind, identity, application, scope, since)
        with self.connect() as db:
            rows = db.execute("SELECT at, identity, client, kind, name, application, outcome, duration_ms, detail FROM events"  # noqa: S608
                              + where + " ORDER BY at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
        keys = ("at", "identity", "client", "kind", "name", "application", "outcome", "duration_ms", "detail")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def event_days(self, since, kind=None, identity=None, application=None, scope=None):
        """{UTC day: (events, configuration changes)} from `since` on, under the same filter."""
        where, params = self.event_filter(kind, identity, application, scope, since)
        with self.connect() as db:
            rows = db.execute("SELECT strftime('%Y-%m-%d', at, 'unixepoch'), COUNT(*), SUM(kind='configuration') FROM events"  # noqa: S608
                              + where + " GROUP BY 1", params).fetchall()
        return {day: (count, configured or 0) for day, count, configured in rows}

    def event_values(self, column, scope=None, since=None):
        """The distinct kinds, identities or applications of the events a scope may see: the Activity view's filter choices."""
        if column not in ("kind", "identity", "application"):
            raise ValueError("column is kind, identity or application")
        where, params = self.event_filter(scope=scope, since=since)
        with self.connect() as db:
            rows = db.execute(f"SELECT DISTINCT {column} FROM events" + where + f" ORDER BY {column} LIMIT 200", params).fetchall()  # noqa: S608
        return [row[0] for row in rows if row[0]]


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
    if plan.get("jobs"):
        # The jobs the service starts (docs/ROADMAP.md D64): their limits and worst case, and the test image they build on.
        block["jobs"] = plan["jobs"]
        block["cost"]["jobs_ceiling_usd_month"] = cost.get("jobs_ceiling_usd_month")
        if plan.get("base"):
            block["base"] = plan["base"]
    if plan.get("contract"):
        # An application taken as it is (docs/ROADMAP.md 13.1) also names its source and the platform commit whose contract describes it.
        contract = plan["contract"]
        block["contract"] = {"repository": contract["repository"], "commit": contract["commit"], "path": contract["path"],
                             "source": {"repository": plan.get("repository"), "commit": plan.get("sha")}}
    return block


def promotion_approval(plan, requested_by=None):
    """The fixed approval block of a promotion (docs/ROADMAP.md D37), built by code from the plan's fields: which staging
    release and exact image, where it goes, what it may cost in each environment, who approves it on GitHub, how far a
    rollback undoes it, and the identifiers that tie the request to the plan and the run."""
    spec, cost, target, source = plan.get("spec") or {}, plan.get("cost") or {}, plan.get("target") or {}, plan.get("source") or {}
    needs = spec.get("needs") or {}
    lines = ((plan.get("commit") or {}).get("message") or "").strip().splitlines()
    migrates = bool(needs.get("migrate"))
    reversibility = ({"tier": "data-bearing", "detail": "the migration changes production's database; a rollback restores traffic only"} if migrates
                     else {"tier": "reversible", "detail": "traffic returns to the previous production revision if a check fails"})

    def bounds(numbers):
        numbers = numbers or {}
        return {"idle_floor_usd_month": numbers.get("idle_floor_usd_month"), "compute_ceiling_usd_month": numbers.get("compute_ceiling_usd_month"),
                "needs_floor_usd_month": numbers.get("needs_floor_usd_month")}
    return {
        "commit": {"sha": plan.get("sha"), "message": lines[0][:100] if lines else None},
        "image": {"digest": source.get("digest"), "from": source.get("image"), "staging_release": source.get("plan"), "provenance": source.get("provenance"),
                  "released_at": source.get("released_at")},
        "spec_hash": plan.get("spec_hash"),
        "target": {"project_id": target.get("project_id"), "region": target.get("region"), "service": target.get("service"),
                   "environment": (plan.get("layout") or {}).get("github_environment")},
        "cost": {"production": {**bounds(cost), "ceiling_policy": (cost.get("ceiling_policy") or {}).get("status")}, "staging": bounds(source.get("cost"))},
        "exposure": spec.get("exposure", "authenticated"),
        "needs": {"buckets": list(needs.get("buckets") or []), "topics": list(needs.get("topics") or []), "database": needs.get("database", "none"),
                  "migrate": migrates},
        "approvers": list(plan.get("reviewers") or []),
        "readiness": plan.get("readiness"),
        "warnings": len(plan.get("warnings") or []),
        "reversibility": reversibility,
        "identifiers": {"plan": plan.get("id"), "commit": plan.get("sha"), "requested_by": requested_by, "staging_release": source.get("plan"),
                        "run": None, "run_url": None},
    }


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
        self._issue_lock = threading.Lock()  # the digest and an alert at the same moment open one issue, not two
        self.config, self.github, self.store, self.observer = config, github, store, observer
        self.started = time.time()
        self.resolver = Resolver(self)
        self.contracts = Contracts(self)
        self.updates = PlatformUpdates(self)
        self.jobs = Keeper(self, observer, config)  # the clock and the guard of the jobs applications start (docs/ROADMAP.md D66, D70)
        self.broker = Broker(self)
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

    # ---- the controller's shared state as an identity may see it: a `*` identity everything, as it is; one that sees only
    # some applications the numbers and names of its own, never the whole project's or another application's.
    def cost_report_for(self, principal, result):
        """A weekly cost report (read_cost_report) as this identity may see it: what was measured on the application itself
        and, of the bill, the lines labelled with its own applications (own_spend)."""
        if "*" in principal.applications or not isinstance(result, dict):
            return result
        found = {key: value for key, value in result.items() if key != "budgets"}  # a project's budget goes with its month so far
        if isinstance(result.get("report"), dict):
            whole = result["report"]
            report = {key: value for key, value in whole.items() if key in OWN_REPORT}
            if isinstance(whole.get("release_tolls"), dict):  # the image scans are its own; a repository's Actions minutes may be other services'
                report["release_tolls"] = {key: value for key, value in whole["release_tolls"].items() if key != "actions"}
            if isinstance(whole.get("actual_spend"), dict):
                report["actual_spend"] = own_spend(whole["actual_spend"], {self.config.applications[app_id].service for app_id in self.visible_apps(principal)})
            found["report"] = report
        found["hidden"] = HIDDEN_COSTS
        return found

    def background_for(self, principal, jobs):
        """The background jobs' state (Scheduler.snapshot) as this identity may see it: for one that sees only some
        applications, each job's last and next run, never its result or error, which may name any application."""
        if not isinstance(jobs, dict) or "*" in principal.applications:
            return jobs
        return {name: {"last_run": entry.get("last_run"), "next_run": entry.get("next_run")} for name, entry in jobs.items()}

    def jobs_for(self, principal):
        """The clock's wakes and the guard's newest cancels (docs/ROADMAP.md D66, D70) for the applications this identity may see."""
        snapshot = self.jobs.snapshot()
        if "*" not in principal.applications:
            visible = set(self.visible_apps(principal))
            snapshot = {"wakes": {key: value for key, value in snapshot["wakes"].items() if key.split(" (", 1)[0] in visible},
                        "cancels": [item for item in snapshot["cancels"] if item.get("application") in visible]}
        return snapshot if snapshot["wakes"] or snapshot["cancels"] else None

    def wake_tick(self):
        """The platform's clock (D66): POST wake.path to each released application whose interval has passed."""
        return self.jobs.wake_tick()

    def job_guard_tick(self):
        """The guard (D70): cancel any job run beyond its contract's limits."""
        result = self.jobs.guard_tick()
        if self.config.job_broker is not None:
            result["admissions"] = self.broker.tick()
        return result

    def drift_for(self, principal):
        """The weekly drift check's result (drift_status) as this identity may see it: for one that sees only some
        applications, theirs alone, and drift or clean by theirs."""
        drift = self.drift_status()
        if "*" in principal.applications or drift.get("status") not in ("drift", "clean"):
            return drift
        visible = set(self.visible_apps(principal))
        drifted = {key: value for key, value in (drift.get("drifted") or {}).items() if key in visible}
        return {**drift, "status": "drift" if drifted else "clean", "drifted": drifted,
                "serious": [key for key in drift.get("serious") or [] if key in visible], "failed": [key for key in drift.get("failed") or [] if key in visible]}

    def updates_for(self, principal):
        """What follows a platform release (PlatformUpdates.status) as this identity may see it: for one that sees only some
        applications, the re-pins of theirs alone, without the re-pin step's own error, which may name any application."""
        view = self.updates.status()
        if "*" in principal.applications or "applications" not in view:
            return view
        visible = set(self.visible_apps(principal))
        return {**view, "applications": {key: value for key, value in view["applications"].items() if key in visible},
                "errors": {part: message for part, message in (view.get("errors") or {}).items() if part != "workflows"}}

    def event_scope(self, principal):
        """Which audit events this identity may see (Store.scope_clause): a read-only identity its own; a deployer of some
        applications the events about them and its own; a deployer of every application all of them (None)."""
        if not principal.can_deploy:
            return (principal.name, None)
        return None if "*" in principal.applications else (principal.name, sorted(self.visible_apps(principal)))

    def events_for(self, principal, limit=50, application=None):
        """The audit trail, newest first, as list_events and GET /events give it: the events the Activity view gives this
        identity (event_scope). A deployer of some applications is told in one line what it does not see."""
        found = {"events": self.store.recent_events(min(max(int(limit), 1), 500), application, self.event_scope(principal))}
        if principal.can_deploy and "*" not in principal.applications:
            found["hidden"] = HIDDEN_EVENTS
        return found

    # ---- one sentence: a repository that follows the contract becomes a running service
    def onboard(self, principal, repository, app_id=None, auto_deploy=False, path=None, as_is=False, until=None):
        from .onboarding import Onboarder
        with self._onboarding_lock:
            return Onboarder(self).start(principal, repository, app_id, auto_deploy, path, as_is=as_is, until=until)

    def onboard_all(self, principal, repository, auto_deploy=False):
        """"Deploy everything in REPO" (docs/ROADMAP.md D56, 20.5): one onboarding as it is per service the repository holds,
        each named after its folder (the root's after the repository), through check_repository's list of services. Each
        opens its own contract pull request; a person's merge of each is the review."""
        from .onboarding import Onboarder, normalize_repository
        if "*" not in principal.applications or not principal.can_deploy:
            raise ValueError("Onboarding every service of a repository needs an identity that may onboard: applications: ['*'] with can_deploy")
        self.require_selected(normalize_repository(repository))
        seen = self.contracts.inspect(normalize_repository(repository), "")
        found = [entry["path"] for entry in seen["services"]]
        view = {"repository": seen["repository"], "services": found, "onboardings": []}
        if not found:
            view["next_action"] = ("No service was found: neither the root nor a folder holds a dependency manifest beside an entry file. "
                                   "Name the folder with check_repository and its path.")
            return view
        with self._onboarding_lock:
            for path in found:
                try:
                    # 1.22.2: each service is read now; its lock compiles and its contract pull request follow within a minute, from
                    # the onboarding tick (live on 2026-10-20: four lock compiles in one call outlived the tool call that asked).
                    view["onboardings"].append(Onboarder(self).start(principal, seen["repository"], None, auto_deploy, path or None, as_is=True, until="contract"))
                except ValueError as exc:
                    view["onboardings"].append({"repository": seen["repository"], "path": path, "state": "refused", "error": str(exc)[:300]})
        states = [str(item.get("state")) for item in view["onboardings"]]
        opening = ("; the controller compiles each one's lock and opens its contract pull request within a minute" if "contract" in states else "")
        view["next_action"] = (f"{len(found)} service{'s' if len(found) != 1 else ''} found ({', '.join(folder or 'the root' for folder in found[:6])}); "
                               f"each is onboarded as it is on its own ({', '.join(sorted(set(states)))}){opening}. A person reviews and merges each "
                               "contract pull request; get_onboarding_status with each application's name follows it.")
        return view

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

    def platform_updates_tick(self):
        """The scheduler's pass over what follows a platform release on main: the machine's update, then the re-pins
        (labdeploy/updates.py; off unless `platform_updates` is configured)."""
        return self.updates.tick()

    def check_repository(self, principal, repository, path=None):
        """The contract's checklist for a repository, or one service folder of it, before anything is registered or created:
        each item, whether it holds, why it matters, and whether the platform can draft it (docs/ROADMAP.md 11.1)."""
        from .onboarding import Blocked, contract_check, missing_message, normalize_path, normalize_repository
        if "*" not in principal.applications:
            raise ValueError("Checking a repository that is not registered needs an identity that may see every application: applications: ['*']")
        repository, path = normalize_repository(repository), normalize_path(path)
        try:
            result, blocked = contract_check(self.github, repository, path), None
        except Blocked as exc:
            # 1.22.1: the checklist for the platform's own workflows is blocked (another default branch, a folder that is not
            # there), but the repository may still be taken as it is, so the reader's answer below is given all the same.
            result, blocked = None, str(exc)
        if result is None:
            view = {"repository": repository, "path": path, "branch": None, "ready": False, "checklist": [], "recommended": [], "error": blocked}
            writable = []
        else:
            view = {key: result[key] for key in ("repository", "path", "branch", "ready", "checklist", "recommended")}
            writable = [entry["item"] for entry in result["checklist"] + result["recommended"] if entry["platform_writes"]]
            view["platform_can_draft"] = writable
        # As it is (docs/ROADMAP.md 13.5): what the reader would choose without changing the repository, and what it refuses.
        try:
            seen = self.contracts.inspect(repository, path)
            read = seen["read"]
            view["as_it_is"] = {"ready": not read["refusals"] and read["spec"] is not None, "service": read["service"], "choices": read["choices"],
                                "refusals": read["refusals"], "ignored": read["ignored"], "notes": read["notes"], "services": seen["services"]}
            view["branch"] = view["branch"] or seen.get("branch")
        except Exception as exc:  # noqa: BLE001 - the checklist stands on its own
            view["as_it_is"] = {"ready": False, "error": (explain(exc) or str(exc))[:300]}
        # A root that holds no service of its own (its only refusal is the missing manifest) while its folders do: the
        # answer names the folders rather than offering a Dockerfile or a manifest for the root (live 2026-10-19).
        empty_root = ("no dependency manifest", "has Node.js code but no package.json")
        refusals = view["as_it_is"].get("refusals") or []
        folders = [entry["path"] for entry in view["as_it_is"].get("services") or [] if entry.get("path")]
        if folders and refusals and all(str(item.get("problem", "")).startswith(empty_root) for item in refusals):
            view["as_it_is"]["folders"] = folders
        else:
            folders = []
        if result is not None and result["ready"]:
            view["next_action"] = (f"Ready to deploy: onboard_application with {repository}" + (f" and path {path}" if path else "")
                                   + (". The platform can still draft the recommended " + ", ".join(writable) + " with propose_configuration." if writable else "."))
        elif folders:
            view["next_action"] = (f"The services live in folders ({', '.join(folders[:6])}" + (f" and {len(folders) - 6} more" if len(folders) > 6 else "")
                                   + f"): check one with check_repository and its path, such as path {folders[0]}.")
        elif view["as_it_is"].get("ready") and result is None:
            view["next_action"] = blocked.rstrip(".") + "."  # it already says to deploy it as it is (found live: said twice, 1.22.4)
        elif view["as_it_is"].get("ready"):
            view["next_action"] = (missing_message(result["checklist"]) + " Or deploy it as it is, adding nothing to the repository: onboard_application "
                                   "with as_is true (its contract is kept in the platform repository).")
        elif result is None:
            view["next_action"] = "Fix what the error names; nothing was created."
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

    def resolver_tick(self):
        self.resolver.tick()
        return {"enabled": self.config.resolver is not None}

    def follow_tick(self, limit=10):
        """One pass: refresh every release still on its way, so its outcome is recorded even when nobody asks (an auto-deploy,
        an onboarding's first release, a release whose status Claude did not follow). The application list, the watchdog
        and the chat's summaries then never show a finished release as still dispatched (found live on 2026-10-16: demo's
        auto-deploy of the 15th had succeeded, and the list still showed the release before it)."""
        principal = Principal(name=self.FOLLOW_IDENTITY, token_sha256="0" * 64, applications=["*"], can_deploy=True)
        results = {}
        with self.store.connect() as db:
            cancellations = db.execute("SELECT id,request FROM operations WHERE kind='cancel' AND state IN ('dispatching','dispatch_unknown') LIMIT 10").fetchall()
        for identifier, payload in cancellations:
            previous = self.store.fetch(json.loads(payload)["from_plan"])
            if previous and previous[1]["state"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                with self.store.connect() as db:
                    operations.transition(db, identifier, "succeeded")
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

    def base_tick(self):
        """Jobs built on another application's test image follow it (docs/ROADMAP.md D69): when that application's newest staging
        release changed its npm lockfile, its Node.js line or its build.scripts since the release the jobs were built on, the
        application is released again at the commit it runs (with auto_deploy), else the trail and the status say it is behind."""
        if self.config.paused:
            return {"paused": True}
        results = {}
        for app_id, app in self.config.applications.items():
            live = self.store.latest_success(app_id)
            built_on = (live or {}).get("base")
            if not live or not built_on:
                continue
            found = next(((other_id, other) for other_id, other in self.config.applications.items() if other.service == built_on["service"]), None)
            newest = self.store.latest_success(found[0]) if found else None
            if newest is None or newest["sha"] == built_on["commit"]:
                results[app_id] = "up to date"
                continue
            try:
                changed = self.packages_changed(found[1], built_on["commit"], newest)
            except Exception as exc:  # noqa: BLE001 - unreadable means changed: building the test image again is the safe side
                changed = f"could not compare ({explain(exc) or type(exc).__name__})"
            if not changed:
                results[app_id] = f"up to date: {built_on['service']}'s packages are the same at {newest['sha'][:12]}"
                continue
            attempted = self.store.latest_plan(app_id)
            if attempted and attempted.get("id") != live.get("id") and not attempted.get("base"):
                results[app_id] = f"its newest plan {attempted['id'][:8]} builds on no application's test image; nothing to follow"
                continue
            if attempted and (attempted.get("base") or {}).get("commit") == newest["sha"] and not self.retryable(attempted):
                results[app_id] = f"plan {attempted['id'][:8]} already builds on {newest['sha'][:12]} ({attempted['state']})"
                continue
            if not app.auto_deploy or app.paused:
                results[app_id] = f"behind: {built_on['service']} {changed} at {newest['sha'][:12]}; release {app_id} again to rebuild its jobs"
                self.store.record_event("jobs_base", name=app_id, application=app_id, outcome="behind", detail=results[app_id])
                continue
            try:
                principal = Principal(name=self.AUTO_DEPLOY_IDENTITY, token_sha256="0" * 64, applications=[app_id], can_deploy=True)
                plan = self.plan(principal, app_id, commit_sha=None if app.central else live["sha"])
                outcome = self.deploy(principal, plan["id"])
                state = "dispatch disabled" if outcome.get("dispatch") == "disabled" else outcome.get("state")
                results[app_id] = f"{state}: plan {plan['id'][:8]} rebuilds its jobs on {built_on['service']} {newest['sha'][:12]} ({changed})"
                self.store.record_event("jobs_base", name=app_id, identity=self.AUTO_DEPLOY_IDENTITY, application=app_id, outcome=state, detail=results[app_id])
            except Exception as exc:  # noqa: BLE001 - one app's trouble must not stop the others
                results[app_id] = f"error: {explain(exc) or exc}"
                self.store.record_event("jobs_base", name=app_id, application=app_id, outcome="error", detail=results[app_id])
        return results

    def packages_changed(self, base_app, old_sha, newest):
        """What changed in a base application's packages between two of its commits: its lockfile, its Node.js line or its
        build.scripts (the spec its newest release recorded against the one at the old commit); an empty string for nothing."""
        old = self.store.latest_success(next(app_id for app_id, app in self.config.applications.items() if app is base_app), old_sha)
        before, after = (old or {}).get("spec") or {}, newest.get("spec") or {}
        if before.get("node") != after.get("node"):
            return f"moved Node.js from {before.get('node')} to {after.get('node')}"
        if (before.get("build") or {}).get("scripts") != (after.get("build") or {}).get("scripts"):
            return "changed its build.scripts"
        for name in ("npm-shrinkwrap.json", "package-lock.json"):
            path = base_app.repo_path(name)
            try:
                then, now = self.github.file(base_app, old_sha, path), self.github.file(base_app, newest["sha"], path)
            except Exception:  # noqa: BLE001, S112 - a lockfile the repository does not have at both commits is compared by the next name
                continue
            return "" if then == now else f"changed {name}"
        return "changed (no lockfile of its own to compare)"

    def watchdog_problems(self, now=None):
        """The daily look around: configuration, credential, each app's schedules, pin and newest attempt, drift, drills, the
        database's live copy and the daily digest itself. Shared by the watchdog and the digest (docs/ROADMAP.md D60)."""
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
                if onboarding.get("repository", "").lower() != app.repository.lower() or onboarding.get("path", "") != app.path:
                    # Another repository's onboarding stopped under this name (a misnamed call on 2026-10-22): it registered
                    # nothing, so unregistering would retire the wrong application, and this one's own checks still run.
                    problems.append(f"{app_id}: an onboarding of {onboarding.get('repository')} stopped under this name ({onboarding.get('state')} at "
                                    f"{onboarding.get('blocked_at') or onboarding.get('state')}) and registered nothing; drop it with "
                                    f"`lab-deploy forget-onboarding {app_id}`")
                else:
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
        problems += self.drill_reminders(now)
        digest = self.digest_problem(now)
        if digest:
            problems.append(digest)
        return problems

    def watchdog_tick(self):
        """The daily look around (watchdog_problems), recorded; posted to the webhook only while the daily digest is off,
        since the digest carries the same lines."""
        problems = self.watchdog_problems()
        report = {"checked_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "problems": problems, "notified": False}
        self.store.record_event("watchdog", outcome="problems" if problems else "ok", detail="; ".join(problems) or None)
        if problems and self.config.notify_webhook_url and getattr(self.config, "digest_hour_utc", None) is None:
            text = "Fast Deploy watchdog found " + str(len(problems)) + " problem(s):\n- " + "\n- ".join(problems)
            sent = self.post_webhook(text)
            report["notified"] = sent["ok"]
            if not sent["ok"]:
                report["notify_error"] = sent["error"]
        return report

    # ---- after a release (docs/ROADMAP.md Phase 23): drills, the release report, the digest and the trust numbers
    # How often each drill is due, in days; the rollback and cap-fire drills only where a production exists.
    DRILLS = {"alerts": (31, "the monthly proof that alerts reach you", "scripts/alert-drill.sh"),
              "rollback": (183, "the rollback drill", "docs/RUNBOOK.md, \"The rollback drill\""),
              "cap_fire": (183, "the cap-fire drill", "scripts/cap-drill.sh PRODUCTION_PROJECT")}

    def drill_reminders(self, now=None):
        """One line per drill that is due: none passed within its interval (docs/ROADMAP.md 23.6). A production rollback that
        succeeded counts as the rollback path proven; the restore drill keeps its own reminder."""
        now = now or time.time()
        production = getattr(self.config, "production", None) is not None or any(app.production for app in self.config.applications.values())
        lines = []
        for name, (days, words, how) in self.DRILLS.items():
            # The alert proof needs a cloud with alerts (the observer says there is one); the other two need a production.
            if (name == "alerts" and getattr(self.config, "observer", None) is None) or (name != "alerts" and not production):
                continue
            passed = self.store.newest_event("drill", "ok", name)
            last = passed["at"] if passed else None
            if name == "rollback":
                rollbacks = [self.store.latest_success(app_id, kind="production_rollback") for app_id, app in self.config.applications.items() if app.production]
                moments = [float((item.get("outcome") or {}).get("recorded_at") or 0) for item in rollbacks if item]
                last = max([moment for moment in [last, *moments] if moment], default=None)
            if last and now - last <= days * 86_400:
                continue
            since = "none recorded yet" if not last else f"the last passed on {datetime.datetime.fromtimestamp(last, datetime.UTC):%Y-%m-%d}"
            lines.append(f"{words} is due ({since}): {how}, then `scripts/machine.sh record-drill {name} ok`")
        return lines

    DIGEST_STALE_SECONDS = 2 * 86_400 + 3600

    def digest_problem(self, now=None):
        """The watchdog's line when the daily digest stopped or reached no one, else None."""
        if getattr(self.config, "digest_hour_utc", None) is None:
            return None
        now = now or time.time()
        newest = self.store.digest()
        if newest is None:
            return None if now - self.started < self.DIGEST_STALE_SECONDS else "the daily digest has never been written; the controller's background jobs may be stuck"
        made = aftercare.parse_moment(newest.get("generated_at")) or 0
        if now - made > self.DIGEST_STALE_SECONDS and now - self.started > self.DIGEST_STALE_SECONDS:
            return f"the daily digest was last written on {newest.get('day')}; the controller's background jobs may be stuck"
        delivered = newest.get("delivered") or []
        if delivered and not any(item.get("ok") for item in delivered):
            reasons = "; ".join(f"{item.get('channel')}: {item.get('error')}" for item in delivered)[:300]
            return f"the daily digest of {newest.get('day')} reached no one ({reasons})"
        return None

    def post_webhook(self, text):
        """Post one message to notify_webhook_url (Slack or Discord); {"channel", "ok", "error"}."""
        url = getattr(self.config, "notify_webhook_url", None)
        if not url:
            return {"channel": "webhook", "ok": False, "error": "notify_webhook_url is not set"}
        import httpx
        short = text if len(text) <= 1900 else text[:1880].rstrip() + "\n…"
        try:
            httpx.post(url, json={"text": short, "content": short}, timeout=20).raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a channel that fails is reported, never fatal
            return {"channel": "webhook", "ok": False, "error": str(exc)[:200]}
        return {"channel": "webhook", "ok": True}

    DIGEST_ISSUE = "Fast Deploy: today"
    DIGEST_ISSUE_BODY = ("Fast Deploy's daily digest: once a day the controller adds a comment here with what shipped, how each release did a "
                         "day later, what needs a person, and \"all quiet\" when nothing does (docs/ROADMAP.md D60). Watch this issue to get it "
                         "by email. A release that did worse than the one before is posted at once. Written by code; nothing here acts.")

    def post_issue(self, text):
        """A comment on the platform repository's digest issue, opened the first time; {"channel", "ok", "error" or "url"}.
        A GitHub App whose installation may not write issues opens nothing. Found on 2026-10-22: the App opened the issue,
        GitHub refused the comment and never listed the issue back, so each message left one more empty issue (six)."""
        platform = getattr(self.config, "platform", None)
        find, create, comment = (getattr(self.github, name, None) for name in ("find_issue", "create_issue", "comment_issue"))
        if platform is None:
            return {"channel": "issue", "ok": False, "error": "platform.repository is not set in the controller configuration"}
        if not all(callable(item) for item in (find, create, comment)):
            return {"channel": "issue", "ok": False, "error": "this GitHub adapter cannot write issues"}
        repository = platform.repository
        try:
            reader = getattr(self.github, "permissions", None)
            granted = reader(repository) if callable(reader) else None
            if granted is not None and granted.get("issues") != "write":
                return {"channel": "issue", "ok": False, "error": (
                    f"the GitHub App's installation may not write issues on {repository} (Issues: {granted.get('issues') or 'no access'}). "
                    "Add Issues: read and write in the App's settings (Permissions & events), then accept the new permission on the "
                    "installation (Settings, Applications, Configure); docs/SETUP.md section 4")}
            with self._issue_lock:
                issue = self.digest_issue(repository, find, create)
            made = comment(repository, issue["number"], text)
        except Exception as exc:  # noqa: BLE001 - a channel that fails is reported, never fatal
            note = explain(exc, repository) or str(exc)[:200]
            return {"channel": "issue", "ok": False, "error": note[:300]}
        return {"channel": "issue", "ok": True, "issue": issue["number"], "url": made.get("url") or issue.get("url")}

    def digest_issue(self, repository, find, create):
        """The open "Fast Deploy: today" issue: the one GitHub lists, else the one the controller opened or found before while
        it is open, else a new one. Its number is kept before anything is written to it, so a comment GitHub refuses never
        leads to a second issue."""
        issue = find(repository, self.DIGEST_ISSUE)
        if issue is None:
            kept, read = self.store.digest_issue(repository), getattr(self.github, "issue", None)
            if kept and callable(read):
                again = read(repository, kept["number"])
                issue = again if again and again.get("open") else None
            elif kept:
                issue = kept  # an adapter that cannot read one issue: the kept number stands
        if issue is None:
            issue = create(repository, self.DIGEST_ISSUE, self.DIGEST_ISSUE_BODY)
        if (self.store.digest_issue(repository) or {}).get("number") != issue["number"]:
            self.store.save_digest_issue(repository, {"number": issue["number"], "url": issue.get("url")})
        return issue

    def announce(self, text):
        """Say something now, on every channel there is: the digest issue and the webhook (a release that did worse)."""
        sent = [self.post_issue(text)]
        if getattr(self.config, "notify_webhook_url", None):
            sent.append(self.post_webhook(text))
        return sent

    def release_report_tick(self, limit=5, now=None):
        """One pass: the report of each release that succeeded at least a day ago and has none (docs/ROADMAP.md D57), at most
        `limit` a pass; a "worse" verdict is announced at once. Reads only; nothing here changes the cloud."""
        if self.observer is None:
            return {"skipped": "no observer identity: live views are off"}
        from . import chat
        now = now or time.time()
        results = {}
        plans = self.store.history(since=now - aftercare.HORIZON_SECONDS - aftercare.BASELINE_SECONDS)
        for plan, replaced in aftercare.awaiting_report(plans, now)[:limit]:
            short = plan["id"][:8]
            if plan.get("rolled_back"):
                report = {"generated_at": aftercare.iso(now), "verdict": "rolled_back", "note": "a person rolled it back; nothing to compare",
                          "environment": "production" if plan.get("kind") == "promotion" else "staging"}
            elif plan.get("application") not in self.config.applications:
                report = {"generated_at": aftercare.iso(now), "verdict": "unreadable", "note": "the application is no longer registered"}
            else:
                try:
                    report = aftercare.release_report(self.observer, types.SimpleNamespace(**plan["target"]), plan, replaced, now)
                except Exception as exc:  # noqa: BLE001 - a read that fails is tried again at the next pass, a few times
                    attempts = int(plan.get("release_report_attempts") or 0) + 1
                    if attempts < aftercare.MAX_ATTEMPTS:
                        self.store.mark(plan["id"], release_report_attempts=attempts)
                        results[short] = f"not read yet ({attempts}): {str(exc)[:120]}"
                        continue
                    report = {"generated_at": aftercare.iso(now), "verdict": "unreadable", "note": f"the observer could not read it: {str(exc)[:200]}"}
            self.store.mark(plan["id"], release_report=report)
            self.store.record_event("release_report", name=plan["id"], identity="follow-up", application=plan.get("application"),
                                    outcome=report["verdict"], detail="; ".join(str(item.get("rule")) for item in report.get("reasons") or []) or None)
            results[short] = report["verdict"]
            if report["verdict"] == "worse":
                report["announced"] = self.announce(chat.release_report_alert({**plan, "release_report": report}))
                self.store.mark(plan["id"], release_report=report)
        return results

    def digest_tick(self, now=None):
        """Once a UTC day, from digest_hour_utc on: build the digest, send it, keep it (docs/ROADMAP.md D60)."""
        hour = getattr(self.config, "digest_hour_utc", None)
        if hour is None:
            return {"off": "digest_hour_utc is null"}
        now = now or time.time()
        moment = datetime.datetime.fromtimestamp(now, datetime.UTC)
        day = moment.date().isoformat()
        if moment.hour < hour:
            return {"waiting": f"until {hour:02d}:00 UTC"}
        if self.store.digest(day):
            return {"done": day}
        record = self.build_digest(now)
        self.store.save_digest(day, record)  # kept before it is sent: a channel that hangs never makes a second digest
        from . import chat
        record["delivered"] = self.publish_digest(chat.digest(record))
        self.store.save_digest(day, record)
        ok = [item["channel"] for item in record["delivered"] if item.get("ok")]
        self.store.record_event("digest", name=day, identity="follow-up", outcome="sent" if ok else "kept",
                                detail=("sent by " + ", ".join(ok)) if ok else "; ".join(f"{item['channel']}: {item.get('error')}" for item in record["delivered"]))
        return {"day": day, "attention": record["attention"], "sent_by": ok}

    def publish_digest(self, text):
        sent = [self.post_issue(text)]
        if getattr(self.config, "notify_webhook_url", None):
            sent.append(self.post_webhook(text))
        return sent

    def build_digest(self, now=None, applications=None):
        """The facts of the day, all from records and reads, none from a model: what shipped, how releases did a day later,
        which applications had server errors, error lines or missed uptime checks, drift, waiting re-pins and drills, the
        watchdog's problems, and on Mondays the trust numbers. `applications`: only these (an identity's own)."""
        now = now or time.time()
        moment = datetime.datetime.fromtimestamp(now, datetime.UTC)
        visible = set(self.config.applications) if applications is None else set(applications)
        plans = [plan for plan in self.store.history(since=now - 100 * 86_400) if plan.get("application") in visible]
        record = {"day": moment.date().isoformat(), "generated_at": aftercare.iso(now), "hour_utc": getattr(self.config, "digest_hour_utc", None),
                  "releases": aftercare.recent_releases(plans, now), "reports": aftercare.recent_reports(plans, now), "services": [], "notes": []}
        if self.observer is None:
            record["notes"].append("live views are off (no observer identity), so errors and uptime checks are not counted")
        else:
            for app_id in sorted(visible):
                app = self.config.applications[app_id]
                for environment, target in (("staging", app.target), ("production", app.production.target if app.production else None)):
                    if target is None:
                        continue
                    try:
                        record["services"].append(aftercare.service_day(self.observer, target, app_id, environment, now))
                    except Exception as exc:  # noqa: BLE001 - one application not read is said, the rest still counted
                        record["notes"].append(f"{app_id} ({environment}) was not read: {str(exc)[:120]}")
        record["troubled"] = [day for day in record["services"] if aftercare.troubled(day)]
        record["slow_starts"] = aftercare.slow_starts(record["services"])
        # Google's side and the quotas, once per project (docs/ROADMAP.md 25.4, 25.5).
        record["google"], record["quotas"] = [], []
        if self.observer is not None and applications is None:
            for project, environment in self.projects().items():
                if callable(getattr(self.observer, "google_incidents", None)):
                    try:
                        for incident in self.observer.google_incidents(project, 1440):
                            record["google"].append({"project": project, "environment": environment, **incident})
                    except Exception as exc:  # noqa: BLE001 - informative; the note says what was not read
                        record["notes"].append(f"Google's incidents for {project} were not read: {str(exc)[:120]}")
                if callable(getattr(self.observer, "quota_pressure", None)):
                    try:
                        for quota in self.observer.quota_pressure(project):
                            record["quotas"].append({"project": project, "environment": environment, **quota})
                    except Exception as exc:  # noqa: BLE001
                        record["notes"].append(f"The quotas of {project} were not read: {str(exc)[:120]}")
        drift = self.drift_status()
        record["drift"] = {key: drift.get(key) for key in ("status", "checked_at", "drifted", "serious", "failed", "stale", "run_url")}
        try:
            updates = self.updates.status()
        except Exception:  # noqa: BLE001 - informative only
            updates = {}
        record["updates"] = updates
        record["problems"] = self.watchdog_problems(now) if applications is None else []
        if moment.weekday() == 0 or applications is not None:
            record["trust"] = aftercare.trust(plans, now, 30)
        # The jobs applications start (docs/ROADMAP.md D66, D70): each run the guard cancelled in the last day, and each
        # application the clock could not wake there.
        cancels = [event for event in self.store.find_events(limit=50, kind="job_guard", since=now - 86_400) if event.get("application") in visible]
        failed = {}
        for event in self.store.find_events(limit=500, kind="wake", since=now - 86_400):
            if event.get("application") in visible and not str(event.get("outcome") or "").startswith("2"):
                failed.setdefault(event["application"], event)
        if cancels or failed:
            record["jobs"] = {"cancels": [{"application": e["application"], "job": e.get("name"), "detail": e.get("detail")} for e in cancels],
                              "wake_failures": [{"application": app, "detail": e.get("detail")} for app, e in sorted(failed.items())]}
        record["guards"] = {"missing_native_caps": sorted({app.target.project_id for app_id, app in self.config.applications.items() if app_id in visible}
                                                          - set(getattr(self.config, "cloud_run_spend_caps", None) or {}))}
        if applications is None:
            record["cost"] = self.cost_position(now)
            if moment.weekday() == 0 and self.observer is not None:
                record["alert_slots"] = self.alert_slots()
                record["idle"] = self.idle_services(plans)
                if callable(getattr(self.observer, "serving_images", None)):
                    record["serving_images"] = self.serving_image_check(now)
        record["attention"] = bool(
            record["google"] or record["quotas"]
            or any(item.get("new_critical_fixable") or item.get("malicious") for item in (record.get("serving_images") or {}).get("images") or [])
            or any(item.get("over_budget") for item in (record.get("cost") or {}).get("projects") or [])
            or any(item["result"] in ("restored", "stopped", "rolled_back") for item in record["releases"])
            or any(item.get("verdict") == "worse" for item in record["reports"]) or record["troubled"]
            or (record["drift"].get("drifted") or record["drift"].get("failed")) or record["problems"] or (record.get("jobs") or {}).get("cancels"))
        return record

    def projects(self):
        """Every project the platform releases into, staging first, each with its environment."""
        found = {}
        for app in self.config.applications.values():
            found.setdefault(app.target.project_id, "staging")
            if app.production:
                found.setdefault(app.production.target.project_id, "production")
        production = getattr(self.config, "production", None)
        if production is not None:
            found.setdefault(production.project_id, "production")
        return found

    def cost_position(self, now=None):
        """The digest's cost line (docs/ROADMAP.md 24.2, D63): each project's spend this month from the billing export, its
        forecast, its recorded budget and its native cap, and whether Google's anomaly detection is recorded as on."""
        budgets = getattr(self.config, "monthly_budgets_usd", None) or {}
        caps = getattr(self.config, "cloud_run_spend_caps", None) or {}
        projects = self.projects()
        position = {"anomaly_detection": bool(getattr(self.config, "cost_anomaly_detection", False)), "projects": []}
        table = getattr(getattr(self.config, "onboarding", None), "billing_export_table", None)
        spent = {}
        if not table:
            position["warning"] = "no billing export is recorded (onboarding.billing_export_table), so this month's spend is not read"
        elif self.observer is None or not callable(getattr(self.observer, "month_spend", None)):
            position["warning"] = "live views are off (no observer identity), so this month's spend is not read"
        else:
            try:
                month = self.observer.month_spend(table, list(projects), now)
                position.update(month=month["month"], days_elapsed=month["days_elapsed"], days_in_month=month["days_in_month"], rough=month["rough"])
                spent = {item["project"]: item for item in month["projects"]}
            except Exception as exc:  # noqa: BLE001 - the line says why it has no numbers; the digest goes on
                position["warning"] = str(exc)[:240]
        for project, environment in projects.items():
            item = spent.get(project) or {}
            budget = budgets.get(project)
            forecast = item.get("forecast_usd")
            position["projects"].append({"project": project, "environment": environment, "net_cost_usd": item.get("net_cost_usd"),
                                         "forecast_usd": forecast, "budget_usd": budget, "native_cap_usd": caps.get(project),
                                         "over_budget": bool(budget and forecast is not None and forecast > budget)})
        return position

    def alert_slots(self):
        """Monday's count (docs/ROADMAP.md 24.4): each project's alert policies and uptime executions, against the free allotments."""
        found = []
        for project, environment in self.projects().items():
            try:
                found.append({"project": project, "environment": environment, **self.observer.policies(project)})
            except Exception as exc:  # noqa: BLE001 - informative only
                found.append({"project": project, "environment": environment, "warning": str(exc)[:160]})
        return found

    def serving_image_check(self, now=None):
        """Monday's look at the images that serve (docs/ROADMAP.md 26.3): what Artifact Analysis holds for each now, set
        beside last Monday's for the same image, then a pull of each, so Google keeps analysing it. It analyses only images
        pulled within 30 days, and Cloud Run never pulls a serving image again after its deploy: without the pull, a finding
        published a month after a release would never reach an image that has served since. Reads and in-region pulls
        cost nothing."""
        now = now or time.time()
        before = {}
        for back in range(7, 14):
            previous = self.store.digest(datetime.datetime.fromtimestamp(now - back * 86_400, datetime.UTC).date().isoformat())
            if previous and previous.get("serving_images"):
                before = {item["image"]: item for item in previous["serving_images"].get("images") or [] if item.get("image")}
                break
        images, notes = [], []
        for app_id, app in sorted(self.config.applications.items()):
            for environment, target in (("staging", app.target), ("production", app.production.target if app.production else None)):
                if target is None:
                    continue
                try:
                    serving = self.observer.serving_images(target)
                except Exception as exc:  # noqa: BLE001 - one application not read is said, the rest still read
                    notes.append(f"{app_id} ({environment}): its serving images were not read: {str(exc)[:120]}")
                    continue
                for image in serving:
                    try:
                        found = self.observer.image_findings(target.project_id, image)
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"{app_id} ({environment}): its image's findings were not read: {str(exc)[:120]}")
                        continue
                    last = before.get(image) or {}
                    found["new_critical_fixable"] = max(0, found["critical_fixable"] - last["critical_fixable"]) if last else None
                    found["new_high_fixable"] = max(0, found["high_fixable"] - last["high_fixable"]) if last else None
                    images.append({"application": app_id, "environment": environment, **found})
        for item in images:
            try:
                item["pulled_bytes"] = self.observer.refresh_analysis(item["image"])
            except Exception as exc:  # noqa: BLE001 - the findings above stand; next Monday says whether analysis went on
                item["pull_error"] = str(exc)[:160]
        return {"images": images, "notes": notes}

    def idle_services(self, plans):
        """Monday's list (docs/ROADMAP.md D63): staging services nobody but the health checks called in 30 days, candidates to
        retire. Only services with a live release; a person decides."""
        idle = []
        for app_id, app in sorted(self.config.applications.items()):
            live = next((plan for plan in reversed(plans) if plan.get("application") == app_id and plan.get("state") == "succeeded"
                         and plan.get("kind") is None), None)
            if live is None:
                continue
            try:
                quiet = not self.observer.had_user_requests(app.target, 30, (live.get("spec") or {}).get("health_path"))
            except Exception:  # noqa: BLE001 - informative only; an unread service is not called idle
                quiet = False
            if quiet:
                idle.append(app_id)
        return idle

    def digest_view(self, principal, fresh=False):
        """get_digest: the newest digest kept, or, with `fresh` or none kept today, one built now and not sent. An identity
        that sees only some applications gets a digest of those."""
        everything = "*" in principal.applications
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        kept = self.store.digest() if everything and not fresh else None
        if kept and kept.get("day") == today:
            return {**kept, "kept": True}
        built = self.build_digest(applications=None if everything else self.visible_apps(principal))
        return {**built, "kept": False, "previous": ({"day": kept.get("day"), "delivered": kept.get("delivered")} if kept else None)}

    def trust_numbers(self, principal, days=30):
        """The trust numbers (docs/ROADMAP.md D61) over the applications this identity may see."""
        if not 1 <= int(days) <= 365:
            raise ValueError("days is between 1 and 365")
        visible = set(self.visible_apps(principal))
        now = time.time()
        plans = [plan for plan in self.store.history(since=now - int(days) * 86_400 - 86_400) if plan.get("application") in visible]
        return aftercare.trust(plans, now, int(days))

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
        # The copy and the configuration (principal hashes, targets, settings; never the raw operator token) go to a
        # private, versioned bucket so a lost laptop loses nothing but its tunnel.
        config_path = config_path or getattr(self.config, "path", None)
        files = [(destination, destination.name)]
        if config_path and Path(config_path).is_file():
            files.append((config_path, "config.yaml"))
        uploaded = copy_to_bucket(self.config, files) or None
        self.store.record_event("backup", outcome="ok", detail=str(destination) + (f" + {len(uploaded)} uploaded" if uploaded else ""))
        return {"backup": str(destination), "removed": removed, "kept_days": self.config.backup_keep_days, "uploaded": uploaded}

    def require_observer(self):
        if self.observer is None:
            raise ValueError("Live cloud views need observer.service_account in the controller configuration and the Terraform observer identity")
        return self.observer

    def service_health(self, principal, app_id, minutes=60, environment="staging"):
        """What serves right now and how it behaves, from Cloud Run and Cloud Monitoring, next to what this controller recorded.
        environment: staging, or production (its project, its live promotion and its error budget, docs/ROADMAP.md D39)."""
        import httpx
        app = self.app(principal, app_id)
        observer = self.require_observer()
        if environment not in ("staging", "production"):
            raise ValueError("environment is staging or production")
        if environment == "production" and app.production is None:
            raise ValueError(f"{app_id} has no production yet: ask for add_production first")
        target = app.production.target if environment == "production" else app.target
        service = observer.service(target)
        metrics = observer.metrics(target, max(5, min(int(minutes), 1440)))
        live = self.store.latest_success(app_id, kind="promotion" if environment == "production" else None)
        budget = None
        if environment == "production" and callable(getattr(observer, "error_budget", None)):
            try:
                budget = observer.error_budget(target, f"{target.service}-production")
            except (ValueError, httpx.HTTPError) as exc:  # the budget is one part of the view; the rest still answers
                budget = {"unreadable": str(exc)[:160]}
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
        if callable(getattr(observer, "refused_requests", None)):
            # Requests refused with 429 (docs/ROADMAP.md 25.3): Cloud Run's request metric leaves out those refused at the limit.
            try:
                refused = observer.refused_requests(target, max(5, min(int(minutes), 1440)))
            except (ValueError, httpx.HTTPError):
                refused = 0
            if refused:
                metrics["refused"] = refused
                notes.append(f"{refused} request(s) answered 429 in the last {metrics['window_minutes']} minutes: when the log says \"no available "
                             "instance\", every allowed instance was busy and people were turned away (raise scaling.maximum, or concurrency)")
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
                needs = observer.needs(target, declared)
        except ValueError as exc:  # no reviewed spec yet, or the observer may not look: say so instead of hiding the view
            notes.append(f"Needs not checked: {str(exc)[:160]}")
        if environment == "production" and budget is None:
            notes.append("No objective yet in production: ask for add_production, which proposes the production root with its SLO")
        elif budget and budget.get("budget_remaining") is not None and budget["budget_remaining"] < 0.25:
            left = budget["budget_remaining"]
            # Found live on 2026-10-19: a quiet application's budget went below zero after the rollback drill, and "only -28%
            # is left" read as nonsense. A spent budget says so, and by how much.
            amount = (f"is spent, {-left:.0%} beyond it" if left < 0 else "is spent" if left == 0 else f"has only {left:.0%} left")
            notes.append(f"The {budget['days']}-day error budget {amount}: keep risky changes out of production until older errors leave the window")
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
        uptime_30 = None
        if environment == "production" and callable(getattr(observer, "uptime_pass_rate", None)):
            # The uptime check's own line (docs/ROADMAP.md D58): its calls no longer count in the users' objective.
            try:
                end = datetime.datetime.now(datetime.UTC)
                uptime_30 = observer.uptime_pass_rate(target, end - datetime.timedelta(days=30), end)
            except (ValueError, httpx.HTTPError):
                uptime_30 = None
        diagnosis = None
        if (metrics.get("server_errors") or not service["ready"]) and callable(getattr(observer, "logs", None)):
            # What the newest error lines say, against the same table a failed release is read with (docs/ROADMAP.md 23.3).
            try:
                diagnosis = diagnose({"candidate_logs": observer.logs(target, max(5, min(int(minutes), 1440)), "WARNING", 30),
                                      "error": service.get("ready_message")})
            except (ValueError, httpx.HTTPError):
                diagnosis = None
        return {"application": app_id, "environment": environment, "service": service, "metrics": metrics,
                "recorded_live_release": live and {"plan_id": live["id"], "sha": live["sha"], "revision": recorded,
                                                   "released_at": (live.get("outcome") or {}).get("completed_at")},
                "release_report": (live or {}).get("release_report"), "uptime_30_days": uptime_30, "diagnosis": diagnosis,
                "agreement": agreement, "needs": needs, "access": access, "notes": notes, "error_budget": budget,
                "console_url": f"https://console.cloud.google.com/run/detail/{target.region}/{target.service}/metrics?project={target.project_id}"}

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

    def require_selected(self, repository):
        """New onboarding, plans and dispatches need a repository chosen in Connections (docs/CONNECTIONS.md)."""
        platform = self.config.platform.repository if self.config.platform else None
        if not self.config.connections.permits(repository, platform):
            raise ValueError(f"Access to {repository} is disabled in Connections. Select it there before deploying; reads and rollbacks still work.")

    def require_active(self, app_id, rollback=False):
        """Plans and dispatches stop while the operator has paused everything or this app; reads keep working. A rollback
        also works for an application whose repository is no longer chosen in Connections: it only moves traffic back."""
        if not rollback:
            self.require_selected(self.config.applications[app_id].repository)
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
        if cloud != "gcp" or environment != "staging":
            # Production takes only what staging released, by promotion (docs/ROADMAP.md D37); nothing is built for it.
            raise ValueError("A plan releases to GCP staging only; production is reached by promote_to_production after a staging release")
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
        if spec.tests == "report":
            plan["checks"]["tests"] = "reported_by_config"
            plan["warnings"].append("The contract declares tests: report: the suite runs and is counted, and a failure is reported in the run, never a stop.")
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
        self.plan_jobs(plan, spec)
        plan["approval"] = release_approval(plan, principal.name)
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    def plan_jobs(self, plan, spec):
        """The jobs a release points at their images (docs/ROADMAP.md D64, 27.3): built every time, never an earlier run's, and
        built on the test image of the application they name at the commit its last staging release deployed (D69)."""
        if not spec.jobs:
            return
        if plan.pop("reuse_image", None):
            plan.pop("reused_from_plan", None)
            plan["checks"].update(tests="pending_in_ci", dependency_scan="pending_in_ci")
        costs = {item["job"]: item for item in (plan.get("cost") or {}).get("jobs") or []}
        plan["jobs"] = {name: {"cpu": job.resources.cpu, "memory_mib": job.resources.memory_mib, "timeout_minutes": job.timeout_minutes,
                               "runs_per_day": job.runs_per_day, "egress": job_egress(spec, name), "base": job.base,
                               "worst_month_usd": (costs.get(name) or {}).get("worst_month_usd")} for name, job in spec.jobs.items()}
        worst = (plan.get("cost") or {}).get("jobs_ceiling_usd_month")
        plan["warnings"].append(f"Starts its own jobs ({', '.join(sorted(spec.jobs))}): one task each, never retried; the guard cancels a run past its "
                                f"timeout or beyond its runs a day. If every run of every day ran to its timeout: ${worst:,.2f} a month of compute, "
                                "plus any model tokens.")
        base = base_of(spec)
        if base is None:
            return
        found = next(((app_id, item) for app_id, item in self.config.applications.items() if item.service == base and app_id != plan["application"]), None)
        if found is None:
            raise ValueError(f"The jobs build on {base}'s test image, and no application with the service {base} is registered: onboard it first")
        base_id, base_app = found
        release = self.store.latest_success(base_id)
        if release is None:
            raise ValueError(f"The jobs build on {base}'s test image, and {base} has no successful staging release yet: release it first, then this")
        if (release.get("spec") or {}).get("runtime") != "node-http":
            raise ValueError(f"The jobs build on {base}'s test image, and {base} is not a Node.js web service: a test image is made for node-http only")
        plan["base"] = {"service": base, "repository": base_app.repository, "path": base_app.path or "", "commit": release["sha"]}
        plan["warnings"].append(f"The jobs build on the test image of {base} at {release['sha'][:12]}, the commit its last staging release deployed.")

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
        if spec.tests == "report":
            plan["checks"]["tests"] = "reported_by_config"
            plan["warnings"].append("The contract declares tests: report: the suite runs and is counted, and a failure is reported in the run, never a stop.")
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
        built_with = ((prior or {}).get("contract") or {}).get("commit")
        if image and built_with and built_with != found["commit"]:
            # The image's signed provenance names the contract commit it was built from, and the pipeline verifies a reused
            # image against the plan's; the platform repository's main moves with every merge (live 2026-10-19: "made from a
            # different contract"). When nothing under apps/<service>/ changed since (the contract and the locks the platform
            # made), the plan keeps that commit, an ancestor of main the pipeline accepts; otherwise the image is built again.
            folder = found["path"].rsplit("/", 1)[0] + "/"
            try:
                files, complete = self.github.changed_files(platform.repository, built_with, found["commit"])
            except Exception:  # noqa: BLE001 - a comparison that cannot be read rules nothing out: build again
                files, complete = [], False
            if complete and not any(name.startswith(folder) for name in files):
                plan["contract"]["commit"] = built_with
            else:
                image = None
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
        self.plan_jobs(plan, spec)
        plan["approval"] = release_approval(plan, principal.name)
        self.store.add(principal.name, plan)
        return {**plan, "state": "planned", "live_dispatch_enabled": self.config.live_dispatch, "next_action": advice({"state": "planned"})}

    def propose_config(self, principal, app_id=None, repository=None, path=None):
        """D26: one draft pull request with the files docs/DEVELOPER-CONTRACT.md asks for and the service lacks, never one that
        exists, for a registered application or, before registration, for a repository link and folder. The lock is compiled
        here in a container of the app's own Python when Docker is available (D27). The platform never merges it."""
        from types import SimpleNamespace

        from . import reader, scaffold
        from .models import default_node
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
        wanted = scaffold.files_to_read(paths, folder)
        for _ in range(4):  # a Node.js service's entry file and its imports are read in up to three more passes
            for item in wanted:
                try:
                    # An npm lockfile is read up to GitHub's 1 MB contents limit; every other input stays within 64 KiB.
                    big = {"limit": 1_000_000} if item.rsplit("/", 1)[-1] in ("package-lock.json", "npm-shrinkwrap.json") else {}
                    found = self.github.contents(target.repository, sha, item, **big)
                except ValueError as exc:
                    # A real project has large files among the ones read for hints (a 241 KB module, a 190 KB test, a hashed lock):
                    # one of them is skipped and named, it never stops the draft.
                    if "exceeds" not in str(exc):
                        raise
                    too_large.append(item)
                    continue
                if found is not None:
                    texts[item] = found["text"]
            wanted = [item for item in reader.more_files_to_read(paths, texts, folder) if item not in too_large]
            if not wanted:
                break
        requirements, locked = (f"{folder}/requirements.txt", f"{folder}/requirements.lock") if folder else ("requirements.txt", "requirements.lock")
        lock = {"text": None, "command": None, "error": None}
        package = f"{folder}/package.json" if folder else "package.json"
        node_service = reader.runtime_of(scaffold.Repository(paths, texts, folder), set()) == "node-http"
        if node_service and package in texts and not any((f"{folder}/{name}" if folder else name) in paths for name in ("package-lock.json", "npm-shrinkwrap.json")) \
                and self.config.live_dispatch:
            read = reader.read(paths, texts, folder)
            try:
                lock = scaffold.node_lock(texts[package], read["spec"].node if read["spec"] else default_node(), path=folder,
                                          runner=self.resolver.runner(principal.name, {"repository": target.repository, "commit": sha, "path": folder}))
            except ResolutionPending as pending:
                return pending.view()
        elif requirements in texts and locked not in paths and self.config.live_dispatch:
            try:
                lock = scaffold.compile_lock(texts[requirements], scaffold.detect(paths, texts, folder)["python"], path=folder,
                                             runner=self.resolver.runner(principal.name, {"repository": target.repository, "commit": sha, "path": folder}))
            except ResolutionPending as pending:
                return pending.view()
        # Before registration the repository's own name is the last resort for the service's name (after the folder and
        # pyproject.toml), so the draft is not left saying my-service.
        result = scaffold.draft_files(paths, texts, folder, name, lock_text=lock["text"], fallback_name=target.repository.split("/")[1])
        if too_large:
            result["review"].append("Not read, over 64 KiB each, so the draft could not use them as hints: " + ", ".join(too_large) + ".")
        if lock["error"]:
            made = "package-lock.json was not made" if node_service else "requirements.lock was not compiled"
            result["review"].append(f"{made} on the controller's machine ({lock['error']}): run `{lock['command']}` and commit the file.")
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
        self.require_selected(app.repository)  # a pull request in its repository
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
        files = self.rendered_workflows(app, sha)
        return {"application": app_id, "platform_commit": sha, "current_pin": current, "pull_request": self.github.update_workflows(app, sha, files)}

    def rendered_workflows(self, app, sha):
        """The application's three workflow files rendered from the platform's templates at one commit, by their path in its repository."""
        platform = self.config.platform
        files = {}
        for name, kind in zip(TEMPLATES, ("deploy", "recover", "rightsize"), strict=True):
            template = self.github.contents(platform.repository, sha, f"templates/{name}")
            if template is None:
                raise ValueError(f"templates/{name} does not exist at platform commit {sha[:12]}")
            files[f".github/workflows/{app.workflows[kind]}"] = render(template["text"], platform.repository, sha, app)
        return files

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
        domain, maximum, jobs, wake = None, None, {}, False
        try:
            spec = self.spec_at(app, self.github.head(app))
            health_path, needs, exposure = spec.health_path, spec.needs.model_dump(), spec.exposure
            jobs, wake = root_jobs(spec), spec.wake is not None  # docs/ROADMAP.md D64, D66
            domain = spec.domain if environment == "production" else None  # staging keeps its run.app address (16.6)
            maximum = spec.scaling.maximum
            onboarding = getattr(self.config, "onboarding", None)
            if environment == "staging" and spec.project == "own" and onboarding is not None and target.project_id == onboarding.project_id:
                # 14.9: a contract that asks for a project of its own is never rendered into the shared one.
                raise PermissionError(f"{app_id}'s contract asks for a project of its own (project: own), and it is registered in the shared "
                                      f"project {target.project_id}. Create its project and foundation (docs/SETUP.md 7s), then unregister "
                                      "and onboard it again, or remove project: own from the contract")
        except ValueError as exc:
            health_path, note = "/health", f"Defaults used until the app has a reviewed platform.yaml: {exc}"
        settings = {**app.infra, **(settings or {})}
        if maximum is not None and "instance_alert_count" not in settings:
            # "At maximum instances" at the application's own limit (docs/ROADMAP.md 25.3), not a fixed 3 that a default
            # application (at most 2) never reaches; an application limited to one instance is watched by the refused-requests
            # alert alone, since one busy instance is its ordinary state.
            settings["instance_alert_count"] = max(2, maximum)
        # A staging root lets the application's production deployer read what staging released, to promote it (16.2).
        readers = [app.production.target.deploy_service_account] if environment == "staging" and app.production is not None else []
        files = app_root(target.service, project_id=target.project_id, region=target.region, state_bucket=state_bucket or f"{target.project_id}-tfstate",
                         github_repository=trusted, github_repository_id=ids["repository_id"], github_owner_id=ids["owner_id"],
                         bootstrap_image=bootstrap_image or BOOTSTRAP_IMAGE, health_path=health_path, settings=settings, needs=needs,
                         exposure=exposure, viewers=self.viewers(app), environment=environment, promotion_readers=readers, domain=domain,
                         jobs=jobs, wake=wake, **layout)
        return {"application": app_id, "service": target.service, "environment": environment, "directory": directory, "files": files, "note": note}

    def add_production(self, principal, app_id):
        """Give an application its production (docs/ROADMAP.md D37), one step at a time and safe to repeat:
        1. its production target, registered by convention in the production project;
        2. its production root, proposed as a pull request that a person merges (production infrastructure is never merged
           by the controller), which the pipeline plans and applies with the production identities once a person approves
           the apply in the environment infra-production;
        3. its environment production-<service> in the platform repository, whose required reviewers approve every
           promotion, with the target's variables;
        4. read access for its production deployer to the images staging released, a change of its staging root that a
           person merges too.
        Returns where it stands and what is next."""
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
            return {**base, "state": "infrastructure", "step": 1, "pull_request": pull_request,
                    "next_action": f"A person merges {pull_request.get('url') or 'the production infrastructure pull request'} once its plan has run "
                                   "(production infrastructure is never merged by the controller), then approves the apply in the environment "
                                   "infra-production. Ask again after the apply for the environment and its approvers."}
        applied = self.root_applied(platform, rendered["directory"])
        if applied["state"] != "succeeded":
            run = f" ({applied['url']})" if applied.get("url") else ""
            waiting = {"waiting": "waits for a person to approve it in the environment infra-production",
                       "running": "is running", "none": "has not started yet",
                       "failed": f"ended {applied.get('conclusion')}: read its log, fix the cause and run Infrastructure apply again for production"}
            return {**base, "state": "applying", "step": 2, "apply_run": applied,
                    "next_action": f"The production root is on main and its apply{run} {waiting[applied['state']]}. Ask again once it has succeeded."}
        target = app.production.target
        variables = {"GCP_PROJECT_ID": target.project_id, "GCP_REGION": target.region, "CLOUD_RUN_SERVICE": target.service,
                     "ARTIFACT_REPOSITORY": target.registry, "WIF_PROVIDER": target.wif_provider, "DEPLOY_SERVICE_ACCOUNT": target.deploy_service_account,
                     "PROBE_SERVICE_ACCOUNT": target.probe_service_account, "RECOVERY_BUCKET": target.recovery_bucket}
        self.github.ensure_protected_environment(platform.repository, app.production.environment, platform.branch, production.reviewers)
        self.github.set_environment_variables(platform.repository, app.production.environment, variables)
        # The weekly cost report runs in staging and reads the billing account's one export, which holds both projects; this
        # names the application's production for it (docs/ROADMAP.md 16.7).
        self.github.set_environment_variables(app.pipeline_repo, app.environment, {"PRODUCTION_PROJECT_ID": target.project_id})
        steps["environment"] = {"name": app.production.environment, "reviewers": list(production.reviewers), "variables": sorted(variables)}
        if not self.promotion_access_on_main(app):
            proposed = self.propose_infrastructure(principal, app_id, allow_writes=True, through="add_production")
            pull_request = proposed["pull_request"]
            self.store.record_event("production", name=app_id, identity=principal.name, application=app_id, outcome="access_proposed",
                                    detail=pull_request.get("url"))
            return {**base, "state": "access", "step": 3, "pull_request": pull_request, "reviewers": list(production.reviewers),
                    "next_action": f"The environment {app.production.environment} is ready. A person merges {pull_request.get('url') or 'the pull request'}: "
                                   "it lets the production deployer read the images staging released, so a promotion can copy one. Ask again "
                                   "after its apply."}
        self.store.record_event("production", name=app_id, identity=principal.name, application=app_id, outcome="ready",
                                detail=f"{app.production.environment}: reviewers {', '.join(production.reviewers)}")
        return {**base, "state": "ready", "step": 4, "reviewers": list(production.reviewers), "variables": sorted(variables),
                "next_action": f"Production is ready for {app_id}: every promotion into {app.production.environment} waits for "
                               f"{' or '.join(production.reviewers)} to approve it on GitHub. Promote a staging release with promote_to_production."}

    def root_applied(self, platform, directory):
        """Whether the platform's apply ran for the newest commit that changed a root: succeeded, waiting (for a person's
        approval in the environment that applies it), running, failed, or none (no run yet)."""
        commit = self.github.last_commit(platform.repository, f"{directory}/main.tf", platform.branch)
        runs = self.github.workflow_runs(platform.repository, "infra-apply.yml", head_sha=commit) if commit else []
        if not runs:
            return {"state": "none", "commit": commit}
        run = runs[0]
        if run["status"] == "waiting":
            state = "waiting"
        elif run["status"] != "completed":
            state = "running"
        else:
            state = "succeeded" if run.get("conclusion") == "success" else "failed"
        return {"state": state, "commit": commit, "url": run.get("url"), "conclusion": run.get("conclusion")}

    def promotion_access_on_main(self, app):
        """Whether the application's staging root on main lets its production deployer read the images staging released."""
        platform = self.config.platform
        text = (self.github.contents(platform.repository, platform.branch, f"infra/gcp/apps/{app.target.service}/main.tf") or {}).get("text") or ""
        return "promotion_readers" in text and f'"{app.production.target.deploy_service_account}"' in text

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
                                         "url": receipt.get("url"), "completed_at": (live.get("outcome") or {}).get("completed_at"),
                                         "report": live.get("release_report")}
                compare = getattr(self.github, "compare", None)
                entry["undeployed_commits"] = compare(app, live["sha"]) if callable(compare) else None
            if app.production is not None:
                # What production serves: the newest promotion that succeeded (docs/ROADMAP.md D37).
                promoted = self.store.latest_success(app_id, kind="promotion")
                receipt = (promoted or {}).get("receipt") or {}
                entry["production_release"] = None if promoted is None else {
                    "plan_id": promoted["id"], "sha": promoted["sha"], "commit": promoted.get("commit"), "revision": receipt.get("revision"),
                    "url": receipt.get("url"), "completed_at": (promoted.get("outcome") or {}).get("completed_at"),
                    "approved_by": (receipt.get("promotion") or {}).get("approved_by"), "staging_release": (promoted.get("source") or {}).get("plan"),
                    "report": promoted.get("release_report")}
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

    def cost_summary(self, app, spec, requests_per_month=None, average_request_seconds=None, environment="staging"):
        """Bounds and findings from list prices and the reviewed spec, with the operator's ceiling applied."""
        summary = estimate(spec, requests_per_month, average_request_seconds, environment=environment)
        summary["findings"] = findings(spec, environment=environment)
        summary["spending_guards"] = self.spending_guards(app)
        if summary["spending_guards"]["cloud_run_spend_cap"]["status"] == "not_recorded":
            summary["findings"].insert(0, {"id": "no_native_spend_cap", "severity": "warning", "setting": "cloud_run_spend_caps (controller configuration)",
                                           "current": None, "message": summary["spending_guards"]["cloud_run_spend_cap"]["advice"]})
        limit = app.cost_ceiling_usd
        compared = round(summary["compute_ceiling_usd_month"] + summary["needs_floor_usd_month"] + summary.get("jobs_ceiling_usd_month", 0), 2)
        over = limit is not None and compared > limit
        summary["ceiling_policy"] = {"limit_usd_month": limit, "compared_usd_month": compared,
                                     "status": "not_configured" if limit is None else ("exceeds_limit" if over else "within_limit")}
        summary["note"] = ("Bounds from list prices and the reviewed configuration, not a bill forecast: the ceiling policy compares the compute bound "
                           "plus the needs' floor (and the jobs' worst case) with the operator's limit; request fees, egress and the per-image scan are extra and unbounded by it. "
                           "Measured right-sizing comes from the rightsizing workflow report (get_cost_report).")
        return summary

    def cost_policy(self, app, spec, environment="staging"):
        summary = self.cost_summary(app, spec, environment=environment)
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
        """get_cost_report: the application's newest weekly report, as this identity may see it (cost_report_for)."""
        return self.cost_report_for(principal, self.read_cost_report(self.app(principal, app_id)))

    def read_cost_report(self, app):
        """The newest weekly cost report as the workflow wrote it, with the guards and budgets recorded for its projects: the
        whole projects' bill, which reaches an identity only through cost_report_for."""
        result = self.github.cost_report(app, workflow=app.workflows["rightsize"], **self.artifact_names(app, "cost-report"))
        if isinstance(result, dict):
            result["spending_guards"] = self.spending_guards(app)
            budgets = getattr(self.config, "monthly_budgets_usd", None) or {}
            projects = [app.target.project_id] + ([app.production.target.project_id] if app.production else [])
            result["budgets"] = {project: budgets.get(project) for project in projects}
            result["cost_anomaly_detection"] = bool(getattr(self.config, "cost_anomaly_detection", False))
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

    def prepare_dispatch(self, owner, plan):
        if self.config.operations_recovery_mode or self.store.recovery_mode():
            raise ValueError("Controller is reconciling restored state; new mutations are paused")
        with self.store.connect() as db:
            identity = db.execute("SELECT owner,request_hash FROM operations WHERE id=?", (plan["id"],)).fetchone()
        if identity != (owner, operations.digest(operations.request_of(plan))):
            raise ValueError("Operation request changed after admission")
        if self.config.durable_intents:
            if not self.config.backup_bucket or self.observer is None:
                raise ValueError("Durable dispatch requires an intent bucket and observer identity")
            witness = {"schema": 1, "kind": "release", "owner": owner, "request": operations.request_of(plan),
                       "request_hash": operations.digest(operations.request_of(plan))}
            operations.Intents(self.config.backup_bucket, self.observer.token,
                               prefix=self.config.backup_prefix + "/intents/v1").write(plan["id"], witness)
        self.store.start_action(plan["id"])

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
        plan = self.store.get(principal.name, plan_id)
        try:
            self.prepare_dispatch(principal.name, plan)
        except Exception:
            self.store.release_claim(principal.name, plan_id)  # nothing was sent: the same plan may be deployed again
            raise
        try:
            result = self.github.dispatch(app, plan)
        except Exception:
            # Network failure can occur after GitHub accepted the dispatch: no blind retry.
            self.store.finish(principal.name, plan, "dispatch_unknown")
            raise ValueError("Dispatch outcome unknown. Inspect GitHub before requesting another deployment.") from None
        plan["dispatch_result"] = result
        link_run(plan)
        self.confirm_dispatched(principal.name, plan)
        return self.store.get(principal.name, plan_id)

    def confirm_dispatched(self, owner, plan):
        """GitHub accepted the dispatch. That beats a follow-up pass that found no run yet and called it lost; any other
        newer state (the run already reported) stays."""
        if self.store.finish(owner, plan, "dispatched"):
            return
        found = self.store.fetch(plan["id"])
        if found and found[1]["state"] == "dispatch_lost":
            self.store.finish(owner, {**plan, "_store_revision": found[1]["_store_revision"]}, "dispatched")

    def send_record(self, owner, record):
        """Store a promotion or rollback record and its intent before anything is sent; a failure before sending closes it."""
        self.store.add(owner, record, "dispatching")
        record = self.store.get(owner, record["id"])
        try:
            self.prepare_dispatch(owner, record)
        except Exception:
            self.store.release_claim(owner, record["id"], "dispatch_lost")
            raise
        return record

    def cancel(self, principal, plan_id):
        """Cancel the GitHub run of a dispatched plan; the recovery workflow restores traffic if the runner had started changing it."""
        owner, plan = self.load(principal, plan_id)
        app = self.app(principal, plan["application"], write=True)
        run_id = (plan.get("dispatch_result") or {}).get("workflow_run_id")
        if plan["state"] not in DISPATCHED or not run_id:
            raise ValueError(f"Plan is '{plan['state']}' and has no running workflow to cancel")
        production = plan.get("kind") in ("promotion", "production_rollback")
        target = {"repository": self.promotion_pipeline(plan).repository if production else app.pipeline_repo, "run": run_id}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            earlier = db.execute("SELECT id, state FROM operations WHERE kind='cancel' AND json_extract(request, '$.from_plan')=? "
                                 "ORDER BY created DESC, rowid DESC", (plan_id,)).fetchall()
            if earlier and earlier[0][1] in ("dispatching", "succeeded"):
                existing = earlier[0]
            else:
                # Cancelling a run is safe to repeat (GitHub answers 409 once it is cancelling), so an attempt that failed or
                # whose answer was lost does not block another; one in flight does.
                existing = None
                cancel_id = operations.digest({"plan": plan_id, "run": run_id, "action": "cancel", "attempt": len(earlier)})[:32]
                request = {"id": cancel_id, "kind": "cancel", "application": plan["application"], "environment": plan.get("environment", "staging"),
                           "from_plan": plan_id, "target": target}
                operations.record(db, owner, request, "dispatching")
        if existing:
            return {"id": plan_id, "cancel_operation": existing[0], "cancel_state": existing[1],
                    "next_action": "Cancellation was already requested. Check release status in a minute."}
        try:
            self.prepare_dispatch(owner, request)
        except Exception:
            with self.store.connect() as db:
                db.execute("DELETE FROM outbox WHERE operation_id=? AND state='prepared'", (cancel_id,))
                operations.transition(db, cancel_id, "failed")  # nothing was sent
            raise
        try:
            result = self.github.cancel_run(self.promotion_pipeline(plan) if production else app, run_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409:
                with self.store.connect() as db:
                    operations.transition(db, cancel_id, "failed")
                raise ValueError(f"GitHub refused the cancellation ({error.response.status_code}); ask again or cancel the run on GitHub") from None
            result = {"cancel_requested": True, "workflow_run_id": int(run_id), "note": "GitHub was already cancelling or had finished the run"}
        except Exception:
            with self.store.connect() as db:
                operations.transition(db, cancel_id, "dispatch_unknown")
            raise ValueError("Cancellation request outcome is unknown; ask again in a minute, a repeated cancellation is harmless") from None
        with self.store.connect() as db:
            operations.transition(db, cancel_id, "succeeded")
            db.execute("INSERT INTO audit VALUES (?, ?, ?, 'cancel_requested')", (time.time(), owner, plan_id))
        status = self.refresh(principal, plan_id)
        status["cancel"] = result
        status["next_action"] = ("Cancellation requested. GitHub cancellation can take several minutes; recovery proceeds after the runner stops, restoring traffic "
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

    def promote(self, principal, app_id, plan_id=None):
        """Promote a staging release to production (docs/ROADMAP.md D37, 16.2): the exact image it deployed, never a rebuild,
        through the platform repository's promote.yml. The run waits in production-<service> until one of production's
        reviewers approves it on GitHub; it then checks the image's provenance, copies it by digest into production's registry,
        signs the promotion with production's key and releases it there with the same runner as staging. `plan_id` names
        the staging release to promote; by default the newest that succeeded with signed provenance."""
        app = self.app(principal, app_id, write=True)
        if not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        self.require_active(app_id)
        production, platform = self.config.production, self.config.platform
        if production is None or platform is None or app.production is None:
            raise ValueError(f"{app_id} has no production yet: ask for add_production first")
        if not self.config.live_dispatch:
            raise ValueError("Dispatch is disabled (live_dispatch: false); nothing can be promoted from here")
        if plan_id:
            found = self.store.fetch(plan_id)
            source = found[1] if found else None
            if source is None or source.get("application") != app_id or source.get("kind") or source.get("state") != "succeeded":
                raise ValueError(f"{plan_id[:8]} is not a staging release of {app_id} that succeeded; list_plans shows them")
            if source.get("rolled_back"):
                raise ValueError("That staging release was rolled back since; promote one that still stands")
        else:
            source = self.store.latest_success(app_id, signed=True)
            if source is None:
                raise ValueError(f"No staging release of {app_id} with signed provenance yet: deploy it to staging first, then promote that release")
        receipt = source.get("receipt") or {}
        image, mode = receipt.get("image") or "", receipt.get("provenance")
        staging = app.target
        prefix = f"{staging.region}-docker.pkg.dev/{staging.project_id}/{staging.registry}/{staging.service}@sha256:"
        if mode not in ("github", "cosign-kms"):
            raise ValueError("That staging release carries no signed provenance (the operator's exception), so it is never promoted; release the commit "
                             "to staging again without the exception")
        if not image.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{64}", image[len(prefix):]):
            raise ValueError("The staging release's image is not in this application's staging registry; deploy to staging again")
        if not recent(source):
            raise ValueError(f"That staging release is over {IMAGE_RESCAN_DAYS} days old, so its image is no longer scanned; release the commit to staging "
                             "again and promote that release")
        spec = AppSpec.model_validate(source["spec"])
        if spec_hash(spec) != source.get("spec_hash") or spec.name != app.service:
            raise ValueError("The staging release's recorded specification does not match its plan; deploy to staging again")
        enforce(spec, "gcp", "production")
        missing = self.production_missing(app)
        if missing:
            raise ValueError(f"Production for {app_id} is not ready: {'; '.join(missing)}. Ask for add_production, which continues from where it stands.")
        cost = self.cost_policy(app.model_copy(update={"target": app.production.target}), spec, environment="production")
        staging_workflow = "deploy.yml" if source.get("contract") else (source.get("layout") or {}).get("workflow") or app.workflows["deploy"]
        # GitHub's attestation names the commit its build run started on (main's tip then), not an older commit it built;
        # a reused image was attested by the run that built it. The run's own commit is what the check compares.
        builder = source
        while mode == "github" and builder.get("reused_from_plan"):
            found = self.store.fetch(builder["reused_from_plan"])
            if found is None:
                break
            builder = found[1]
        attested = (builder.get("workflow") or {}).get("head_sha") if mode == "github" else None
        plan = {
            "id": uuid.uuid4().hex, "kind": "promotion", "application": app_id, "repository": app.repository, "branch": app.branch,
            "sha": source["sha"], "commit": source.get("commit"), "spec_hash": source["spec_hash"], "spec": source["spec"],
            "cloud": "gcp", "environment": "production", "target": app.production.target.model_dump(), "expires_at": time.time() + 900,
            "cost": cost, "pipeline": platform.repository, "pinned_commit": True,
            "checks": {"approval": "pending", "provenance": "pending", "copy": "pending", "health": "pending_after_deployment"},
            "source": {"plan": source["id"], "image": image, "digest": image.rsplit("@", 1)[1], "provenance": mode, "workflow": staging_workflow,
                       "attested_commit": attested if attested and re.fullmatch(r"[a-f0-9]{40}", attested) else source["sha"],
                       "revision": receipt.get("revision"), "released_at": (source.get("outcome") or {}).get("completed_at"), "cost": source.get("cost")},
            "bake_seconds": min(production.bake_seconds, MAX_BAKE_SECONDS), "rollout": list(production.rollout),
            "layout": {"path": app.path, "workflow": "promote.yml", "github_environment": app.production.environment},
            "reviewers": list(production.reviewers), "containment": source.get("containment"), "warnings": [],
        }
        if source.get("contract"):
            plan["contract"] = source["contract"]
        if spec.jobs:
            # 27.8: the jobs' images the staging release ran, copied with the service's (D68).
            jobs = {name: (entry or {}).get("image") for name, entry in (receipt.get("jobs") or {}).items()}
            if set(jobs) != set(spec.jobs) or not all(jobs.values()):
                raise ValueError("The staging release carries no image for every job its contract declares: release staging again, then promote that release")
            plan["source"]["jobs"] = jobs
            plan["jobs"] = source.get("jobs") or {}
            plan["warnings"].append(f"Copies the images of its jobs ({', '.join(sorted(jobs))}) as well, each verified against its staging signature and "
                                    "signed again with production's key; production's guard holds them to the same limits.")
        if spec.needs.migrate:
            plan["warnings"].append("Runs its migration against production's database before the switch; a rollback restores traffic, not data.")
        if spec.secrets:
            plan["warnings"].append("References secrets " + ", ".join(f"{app.service}-{name}" for name in spec.secrets) + f" that must exist in "
                                    f"{app.production.target.project_id} with an enabled version, or the release stops before deploying.")
        if spec.domain:
            # A domain (docs/ROADMAP.md 16.6) is built by production's root, which a person merges; a promotion does not wait for it.
            root = self.github.contents(platform.repository, platform.branch, f"{PRODUCTION_ROOTS}/{app.production.target.service}/main.tf")
            if root is None or f"domain = {json.dumps(spec.domain)}" not in " ".join(root["text"].split()):
                plan["warnings"].append(f"The contract names the domain {spec.domain}, which production's root on main does not build yet: ask for "
                                        "add_production to open its pull request. Until it is merged and applied, production answers at its run.app address only.")
            else:
                plan["warnings"].append(f"Also answers at https://{spec.domain} through a load balancer with Cloud Armor (about "
                                        f"${domain_floor():.2f} a month), once the domain's A record points at the root's domain_address and "
                                        "Google has issued the certificate; the run.app address keeps answering behind the same sign-in.")
        if len(plan["rollout"]) > 1:
            minutes = round(len(plan["rollout"]) * plan["bake_seconds"] / 60)
            plan["warnings"].append("Released in steps of " + ", ".join(f"{step}%" for step in plan["rollout"]) + f" of the traffic, each watched "
                                    f"{plan['bake_seconds']} seconds: it serves everyone about {minutes} minutes after the approval.")
        if not plan["bake_seconds"]:
            plan["warnings"].append("production.bake_seconds is 0: nothing watches the new revision after its final probe.")
        if self.store.latest_success(app_id, include_rolled_back=True, kind="promotion") is None:
            # The application's first promotion: the readiness checklist, rendered by code from what is true now (D39).
            plan["readiness"] = self.readiness(app, spec)
            plan["warnings"] += [f"Readiness: {item['detail']}" for item in plan["readiness"] if not item["ok"]]
        plan["approval"] = promotion_approval(plan, principal.name)
        plan = self.send_record(principal.name, plan)
        try:
            plan["dispatch_result"] = self.github.dispatch_promotion(platform.repository, plan)
        except Exception:
            self.store.finish(principal.name, plan, "dispatch_unknown")
            raise ValueError("Promotion dispatch outcome unknown: look for a Promote to production run titled with this plan id on the platform "
                             "repository before asking again.") from None
        link_run(plan)
        self.confirm_dispatched(principal.name, plan)
        self.store.record_event("promotion", name=app_id, identity=principal.name, application=app_id, outcome="dispatched",
                                detail=f"staging {source['id'][:8]} {plan['source']['digest'][:19]}")
        return {**plan, "state": "dispatched", "next_action": advice({**plan, "state": "dispatched"})}

    def rollback_production(self, principal, app_id):
        """Move production's traffic back to the split that served before its live promotion (docs/ROADMAP.md D38): traffic
        only, in seconds, no build. It runs as promote.yml's rollback, the one workflow production's deployer trusts, so it
        waits for a production reviewer's approval like every job in production-<service>. The runner refuses when the
        checkpoint belongs to another promotion or traffic moved since."""
        app = self.app(principal, app_id, write=True)
        if not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        self.require_active(app_id, rollback=True)
        production, platform = self.config.production, self.config.platform
        if production is None or platform is None or app.production is None:
            raise ValueError(f"{app_id} has no production yet: ask for add_production first")
        if not self.config.live_dispatch:
            raise ValueError("Dispatch is disabled (live_dispatch: false); nothing can be rolled back from here")
        live = self.store.latest_success(app_id, kind="promotion")
        if live is None:
            raise ValueError(f"Nothing promoted to production by this controller to roll back for {app_id}")
        receipt = live.get("receipt") or {}
        if not receipt.get("previous_traffic"):
            raise ValueError("The live promotion's receipt records no earlier split; promote the wanted staging release instead")
        gate = [item for item in self.production_missing(app) if item.startswith(("the environment", "administrators"))]
        if gate:
            raise ValueError(f"Production's approval gate for {app_id} is not as it must be: {'; '.join(gate)}. Ask for add_production first.")
        record = {
            "id": uuid.uuid4().hex, "kind": "production_rollback", "application": app_id, "repository": app.repository, "branch": app.branch,
            "sha": live["sha"], "commit": live.get("commit"), "spec_hash": live.get("spec_hash"), "cloud": "gcp", "environment": "production",
            "target": app.production.target.model_dump(), "pipeline": platform.repository, "expires_at": time.time() + 900, "cost": None, "checks": {},
            "from_plan": live["id"], "from_revision": receipt.get("revision"), "to_traffic": receipt["previous_traffic"],
            "previous": self.previous_release(app_id, receipt["previous_traffic"]), "reviewers": list(production.reviewers),
            "layout": {"path": app.path, "workflow": "promote.yml", "github_environment": app.production.environment},
        }
        record = self.send_record(principal.name, record)
        try:
            record["dispatch_result"] = self.github.dispatch_production_rollback(platform.repository, record)
        except Exception:
            self.store.finish(principal.name, record, "dispatch_unknown")
            raise ValueError("Rollback dispatch outcome unknown: look for a run titled rollback- with this id among the platform repository's Promote to "
                             "production runs before asking again.") from None
        self.confirm_dispatched(principal.name, record)
        self.store.record_event("production_rollback", name=app_id, identity=principal.name, application=app_id, outcome="dispatched",
                                detail=f"undoes {live['id'][:8]}")
        return {**record, "state": "dispatched", "next_action": advice({**record, "state": "dispatched"})}

    def readiness(self, app, spec):
        """Production readiness at an application's first promotion (docs/ROADMAP.md D39, roadmap item 16.4): each item what is
        true now, from the reviewed specification and the production root on main, with what to do where it falls short."""
        platform = self.config.platform
        root = (self.github.contents(platform.repository, platform.branch, f"{PRODUCTION_ROOTS}/{app.production.target.service}/main.tf") or {}).get("text") or ""
        database = spec.needs.database
        items = [
            {"item": "probes", "ok": spec.health_path is not None,
             "detail": f"the health route {spec.health_path} answers every probe and the uptime check" if spec.health_path
             else "no health route: only a TCP connection proves the service is up; add one to platform.yaml (health_path)"},
            {"item": "alerts", "ok": "slo_goal" in root,
             "detail": "the production root has its objective, its error-budget alerts and the minute uptime check" if "slo_goal" in root
             else "the production root on main predates the objective: ask for add_production to propose it again, then merge"},
            {"item": "runbook", "ok": True, "detail": "docs/RUNBOOK.md, Production: approving, undoing, a new secret version, the rollback drill"},
            {"item": "backup", "ok": True,
             "detail": {"postgres": f"Cloud SQL keeps a week of daily backups and, in production, a week of write-ahead log for recovery to any "
                                    f"moment ({spec.needs.database_tier} tier); scripts/data-drill.sh proves a restore",
                        "firestore": "Firestore keeps delete protection and, in production, a week of versions for recovery to any moment"}.get(
                 database, "nothing to back up: the application declares no database")},
            {"item": "capacity", "ok": spec.scaling.maximum >= 1,
             "detail": f"up to {spec.scaling.maximum} instance(s) of {spec.resources.cpu} CPU and {spec.resources.memory_mib} MiB, {spec.concurrency} "
                       "requests each at once" + ("; with no warm instance the first request after a quiet spell waits for one to start"
                                                  if spec.scaling.minimum == 0 else "")},
            {"item": "rollback", "ok": True,
             "detail": "rollback_production returns traffic to the split before this promotion in seconds; the first promotion's earlier split is "
                       "the placeholder image the infrastructure started with"},
        ]
        return items

    def production_missing(self, app):
        """What a promotion of this application still needs, in plain words; empty when production is ready: the protected
        environment, the production root on main, and the staging root's read access for the production deployer."""
        platform = self.config.platform
        missing = []
        environment = self.github.deployment_environment(platform.repository, app.production.environment)
        rules = (environment or {}).get("protection_rules") or []
        reviewed = [rule for rule in rules if rule.get("type") == "required_reviewers" and rule.get("reviewers")]
        if environment is None:
            missing.append(f"the environment {app.production.environment} does not exist on {platform.repository}")
        elif not any(rule.get("prevent_self_review") for rule in reviewed):
            missing.append(f"the environment {app.production.environment} has no required reviewers with self-review prevented")
        elif environment.get("can_admins_bypass") is not False:
            missing.append(f"administrators can skip the approval in {app.production.environment}")
        if self.github.contents(platform.repository, platform.branch, f"{PRODUCTION_ROOTS}/{app.production.target.service}/main.tf") is None:
            missing.append("its production root is not on main")
        if not self.promotion_access_on_main(app):
            missing.append("its staging root does not yet let the production deployer read the images staging released")
        return missing

    @staticmethod
    def promotion_pipeline(plan):
        """Where a promotion's run lives, in the shape the GitHub adapter reads: the platform repository's promote.yml."""
        return types.SimpleNamespace(repository=plan["pipeline"], pipeline_repo=plan["pipeline"], central=True, branch="main",
                                     service=plan["target"]["service"], workflow="promote.yml", workflows={"deploy": "promote.yml"})

    def rollback(self, principal, app_id):
        """Move traffic back to the split that served before the live release, through the recovery workflow. No build."""
        app = self.app(principal, app_id, write=True)
        self.require_active(app_id, rollback=True)
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
        record = self.send_record(principal.name, record)
        try:
            record["dispatch_result"] = self.github.dispatch_workflow(app, app.workflows["recover"], {"rollback_plan_id": live["id"]})
        except Exception:
            self.store.finish(principal.name, record, "dispatch_unknown")
            raise ValueError("Rollback dispatch outcome unknown. Inspect GitHub before requesting it again.") from None
        self.confirm_dispatched(principal.name, record)
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
        with self.store.connect() as db:
            resolution = db.execute("SELECT id FROM resolutions WHERE id=? AND owner=?", (plan_id, principal.name)).fetchone()
        if resolution:
            return self.resolver.status(principal.name, plan_id)
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
            if plan["state"] == "awaiting_workflow" and self.dispatch_lost(plan) and not self.store.recently_sent(plan_id):
                plan["state"] = "dispatch_lost"
                self.store.finish(owner, plan, "dispatch_lost")
            elif plan["state"] in TERMINAL:
                self.record_outcome(owner, app, plan, workflow)
        elif self.unverified_recently(plan):
            # The run succeeded while a check had not reported: read it again, and record the plan once more when every check
            # has come in (the receipt, the outcome, a promotion's release tag). Until then nothing is fetched or written.
            workflow = self.github.status(app, plan)
            again = summarize(workflow, plan["checks"], plan.get("kind"))
            if again["state"] == "succeeded":
                plan["workflow"] = workflow
                plan.update(again)
                self.store.record_event("recheck", name=plan_id, identity=owner, application=plan["application"], outcome="succeeded",
                                        detail="every check reported after the run had ended")
                self.record_outcome(owner, app, plan, workflow)
        elif plan["state"] in {"failed", "cancelled", "timed_out"} and not (plan.get("recovery_run") or {}).get("conclusion"):
            self.record_recovery(owner, app, plan)
        plan["owner"] = owner
        plan["phases"] = release_phases(plan.get("receipt"))
        plan["events"] = self.store.events(owner, plan_id)
        plan["next_action"] = advice(plan)
        return plan

    @staticmethod
    def unverified_recently(plan, now=None):
        """A run that succeeded while some check had not reported yet, recorded less than RECHECK_SECONDS ago."""
        outcome = plan.get("outcome") or {}
        return (plan.get("state") == "completed_unverified" and outcome.get("conclusion") == "success"
                and (now or time.time()) - float(outcome.get("recorded_at") or 0) < RECHECK_SECONDS)

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
        if plan.get("kind") in ("rollback", "production_rollback"):
            # A production rollback's receipt is uploaded by the platform repository's promote.yml, under its plain name.
            production = plan.get("kind") == "production_rollback"
            where, name = (self.promotion_pipeline(plan), "recovery-receipt") if production else (app, self.artifact_name(app, "recovery-receipt"))
            fetch = getattr(self.github, "artifact_json", None)
            files = fetch(where, workflow["id"], name, ("recovery-receipt.json",)) if callable(fetch) and workflow.get("id") else None
            plan["receipt"] = (files or {}).get("recovery-receipt.json")
            if plan["state"] == "succeeded" and (plan["receipt"] or {}).get("state") == "rolled_back":
                self.store.mark(plan["from_plan"], rolled_back=True, rolled_back_by=plan["id"])
            plan["vulnerabilities"] = None
        else:
            # A promotion's receipt is uploaded by the platform repository's promote.yml, a release's by its own pipeline.
            where = self.promotion_pipeline(plan) if plan.get("kind") == "promotion" else app
            fetch = getattr(self.github, "release_receipt", None)
            receipts = fetch(where, workflow["id"]) if callable(fetch) and workflow.get("id") else None
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
        if plan.get("kind") == "promotion" and plan["state"] == "succeeded" and not plan.get("release_tag"):
            plan["release_tag"] = self.release_tag(owner, plan)
        if plan["state"] in {"failed", "cancelled", "timed_out"}:
            self.record_recovery(owner, app, plan, persist=False)
        self.persist(owner, plan)

    def release_tag(self, owner, plan):
        """The release tag of a promotion that succeeded (docs/ROADMAP.md 16.7): production/<service>/<day>-<plan>, an annotated
        tag at the promoted commit in the application's repository that names the plan, the image and who approved it. Best
        effort: a tag that cannot be written (a repository the App is not installed on, a rule there) is recorded with why,
        and production is not affected. None when the GitHub adapter cannot write tags."""
        create = getattr(self.github, "create_tag", None)
        if not callable(create):
            return None
        receipt, source, outcome = plan.get("receipt") or {}, plan.get("source") or {}, plan.get("outcome") or {}
        day = str(outcome.get("completed_at") or "")[:10]
        day = day if re.fullmatch(r"\d{4}-\d\d-\d\d", day) else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        service, repository = plan["target"]["service"], plan["repository"]
        name = f"production/{service}/{day}-{plan['id'][:8]}"
        approved = ", ".join((receipt.get("promotion") or {}).get("approved_by") or []) or "a production reviewer"
        message = "\n".join([f"{service} promoted to production on {day} by Fast Deploy, approved by {approved}.", "",
                             f"Plan: {plan['id']}", f"Staging release: {source.get('plan')}", f"Image: {source.get('digest')}",
                             f"Project: {plan['target']['project_id']}", f"Revision: {receipt.get('revision')}", f"Run: {outcome.get('run_url')}"])
        record = {"name": name, "repository": repository, "sha": plan["sha"]}
        try:
            made = create(repository, name, plan["sha"], message)
        except Exception as exc:  # noqa: BLE001 - a tag records a release; it is never a reason to doubt one
            note = explain(exc) or str(exc)[:200]
            self.store.record_event("release_tag", name=name, identity=owner, application=plan["application"], outcome="failed", detail=note)
            return {**record, "created": False, "note": note[:300]}
        self.store.record_event("release_tag", name=name, identity=owner, application=plan["application"],
                                outcome="existed" if made.get("existed") else "created", detail=f"{repository}@{plan['sha'][:12]}")
        return {**record, "created": True, "url": made.get("url")}

    def record_recovery(self, owner, app, plan, persist=True):
        if plan.get("kind") in ("promotion", "production_rollback"):
            return  # a promotion restores production's traffic itself; the next promotion recovers a lost runner's checkpoint first
        lookup = getattr(self.github, "recovery_after", None)
        if not callable(lookup) or not (plan.get("workflow") or {}).get("id"):
            return
        plan["recovery_run"] = lookup(app, plan["workflow"], workflow=app.workflows["recover"], **self.artifact_names(app, "recovery-receipt"))
        if persist and (plan["recovery_run"] or {}).get("conclusion"):
            self.persist(owner, plan)

    def persist(self, owner, plan):
        record = {key: value for key, value in plan.items() if key not in {"events", "phases", "next_action", "owner"}}
        self.store.finish(owner, record, plan["state"])

