"""Durable operation identities and a conservative transactional outbox.

An expired sending claim is UNKNOWN, never permission to repeat a provider mutation.
The plans table remains the public projection; both records change in one transaction.
"""
import hashlib
import json
import re
import time
from urllib.parse import quote

import httpx

SCHEMA_VERSION = 1
REQUEST_FIELDS = ("id", "kind", "application", "repository", "branch", "sha", "spec_hash", "cloud", "environment",
                  "target", "layout", "source", "contract", "from_plan", "to_traffic", "reuse_image", "provenance",
                  "rollout", "bake_seconds", "base", "pipeline")
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "rolled_back", "recovered", "traffic_restored", "dispatch_lost", "completed_unverified"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def request_of(plan):
    return {key: plan[key] for key in REQUEST_FIELDS if key in plan}


def migrate(db):
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied REAL NOT NULL)")
    newest = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if newest is not None and newest > SCHEMA_VERSION:
        raise ValueError("Controller database requires a newer platform version")
    db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, "
               "application TEXT NOT NULL, environment TEXT NOT NULL, request_hash TEXT NOT NULL, request TEXT NOT NULL, "
               "state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS operation_events (operation_id TEXT NOT NULL REFERENCES operations(id), sequence INTEGER NOT NULL, "
               "at REAL NOT NULL, state TEXT NOT NULL, PRIMARY KEY(operation_id, sequence))")
    db.execute("CREATE TABLE IF NOT EXISTS outbox (operation_id TEXT PRIMARY KEY REFERENCES operations(id), state TEXT NOT NULL, "
               "attempts INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, provider_ref TEXT, error TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS execution_scans (id TEXT PRIMARY KEY REFERENCES operations(id), payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS operation_checks (id TEXT PRIMARY KEY REFERENCES operations(id), checked REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS schedules (name TEXT PRIMARY KEY, next_run REAL NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS operation_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if "version" not in {row[1] for row in db.execute("PRAGMA table_info(plans)")}:
        db.execute("ALTER TABLE plans ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
    # Import history, never enqueue historic operations during a migration.
    for identifier, owner, state, payload in db.execute("SELECT id, owner, state, payload FROM plans WHERE id NOT IN (SELECT id FROM operations)"):
        record(db, owner, json.loads(payload), state, identifier=identifier, queue=False)
    db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", (SCHEMA_VERSION, time.time()))


def record(db, owner, plan, state, identifier=None, queue=True):
    identifier = identifier or plan["id"]
    request = request_of(plan)
    now = time.time()
    db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
               (identifier, owner, (plan.get("kind") or "deployment"), plan.get("application", ""), plan.get("environment", ""),
                digest(request), canonical(request), state, now, now))
    db.execute("INSERT INTO operation_events VALUES (?, 0, ?, ?)", (identifier, now, state))
    if state == "dispatching" and queue:
        enqueue(db, identifier)


def enqueue(db, identifier):
    if db.execute("SELECT value FROM operation_settings WHERE name='recovery_mode'").fetchone() == ("true",):
        raise ValueError("Controller is reconciling restored state; new mutations are paused")
    db.execute("INSERT OR IGNORE INTO outbox(operation_id,state,updated) VALUES (?, 'prepared', ?)", (identifier, time.time()))


def transition(db, identifier, state):
    row = db.execute("SELECT state, revision FROM operations WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise ValueError("Operation identity missing")
    if row[0] == state:
        return
    revision, now = row[1] + 1, time.time()
    db.execute("UPDATE operations SET state=?, revision=?, updated=? WHERE id=?", (state, revision, now, identifier))
    db.execute("INSERT INTO operation_events VALUES (?, ?, ?, ?)", (identifier, revision, now, state))
    if state == "dispatching":
        enqueue(db, identifier)
    elif state == "dispatch_unknown":
        db.execute("UPDATE outbox SET state='unknown', updated=? WHERE operation_id=?", (now, identifier))
    elif state == "dispatched":
        db.execute("UPDATE outbox SET state='submitted', updated=? WHERE operation_id=?", (now, identifier))
    elif state in TERMINAL:
        db.execute("UPDATE outbox SET state='completed', updated=? WHERE operation_id=?", (now, identifier))


class Intents:
    """Immutable off-host witnesses. Only a confirmed create or identical object permits sending."""

    def __init__(self, bucket, token, prefix="intents/v1", client=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
            raise ValueError("Invalid intent bucket")
        if not re.fullmatch(r"[a-zA-Z0-9/_-]{1,120}", prefix):
            raise ValueError("Invalid intent prefix")
        self.bucket, self.token, self.prefix = bucket, token, prefix
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def write(self, identifier, payload):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid operation identity")
        body = canonical(payload)
        if len(body.encode()) > 64 * 1024:
            raise ValueError("Mutation intent is too large")
        key = f"{self.prefix}/{identifier}.json"
        headers = {"Authorization": "Bearer " + self.token()}
        response = self.client.post(f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o", headers=headers,
                                    params={"uploadType": "media", "name": key, "ifGenerationMatch": "0"}, content=body.encode())
        if response.status_code == 412:
            response = self.client.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(key, safe='')}",
                                       headers=headers, params={"alt": "media"})
            response.raise_for_status()
            if canonical(response.json()) != body:
                raise ValueError("Off-host operation identity already names a different request")
        else:
            response.raise_for_status()
        return key

    def list(self):
        """Read all retained witnesses; a partial listing must never be treated as a complete restore."""
        token, result = None, []
        headers = {"Authorization": "Bearer " + self.token()}
        for _ in range(1000):
            response = self.client.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o", headers=headers,
                                       params={"prefix": self.prefix + "/", "maxResults": 1000, **({"pageToken": token} if token else {})})
            response.raise_for_status()
            page = response.json()
            for item in page.get("items", []):
                found = self.client.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(item['name'], safe='')}",
                                        headers=headers, params={"alt": "media", "generation": item["generation"]})
                found.raise_for_status()
                if len(found.content) > 64 * 1024:
                    raise ValueError("Oversized off-host intent")
                result.append(found.json())
            token = page.get("nextPageToken")
            if not token:
                return result
        raise ValueError("Intent listing incomplete; recovery remains paused")


def restore_intents(store, witnesses):
    """Import witnesses after restoring a consistent backup. Leave admission fenced for operator verification.

    No witness is proof of successful completion. Every missing or pre-send local record becomes unknown.
    The broker must initialize its schema before this function is called.
    """
    store.recovery_mode(True)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        count = 0
        for witness in witnesses:
            if witness.get("schema") != 1 or witness.get("kind") not in {"release", "job", "resolver"}:
                raise ValueError("Unsupported off-host intent; reconciliation remains paused")
            job = witness["kind"] == "job"
            request = ({"id": witness["id"], "kind": "job", "application": witness["application"], "environment": witness["environment"],
                        "target": {"job": witness["name"]}, "spec_hash": witness["spec_hash"]} if job else witness["request"])
            identifier = request["id"]
            if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                raise ValueError("Invalid witnessed operation identity")
            if not job and digest(request) != witness["request_hash"]:
                raise ValueError("Intent request hash mismatch")
            owner = "machine:" + witness["subject"] if job else witness["owner"]
            existing = db.execute("SELECT request_hash,owner,state FROM operations WHERE id=?", (identifier,)).fetchone()
            if existing and (existing[0] != digest(request) or existing[1] != owner):
                raise ValueError("Restored operation conflicts with its off-host intent")
            if not existing:
                record(db, owner, request, "dispatch_unknown", queue=False)
                if witness["kind"] == "release" and request.get("kind") != "cancel":
                    payload = {**request, "expires_at": 0, "state": "dispatch_unknown", "restored_from_intent": True}
                    db.execute("INSERT INTO plans(id,owner,state,payload,version) VALUES (?,?,'dispatch_unknown',?,0)",
                               (identifier, owner, canonical(payload)))
            elif existing[2] not in TERMINAL:
                transition(db, identifier, "dispatch_unknown")
                db.execute("UPDATE plans SET state='dispatch_unknown',version=version+1 WHERE id=?", (identifier,))
            if not existing or existing[2] not in TERMINAL:
                db.execute("INSERT OR REPLACE INTO outbox(operation_id,state,attempts,updated) VALUES (?,'unknown',1,?)", (identifier, time.time()))
            if witness["kind"] == "resolver":
                binding = request["source"]
                db.execute("INSERT OR IGNORE INTO resolutions(id,owner,binding,state,payload,created) VALUES (?,?,?,'unknown',?,?)",
                           (identifier, owner, digest(binding), canonical(binding), time.time()))
            if job:
                # Arguments are deliberately absent from the off-host witness. Recovery can observe, never relaunch.
                payload = {"name": witness["name"], "image": witness["image"], "spec_hash": witness["spec_hash"]}
                found = db.execute("SELECT request_hash FROM job_admissions WHERE id=?", (identifier,)).fetchone()
                if found and found[0] != witness["request_hash"]:
                    raise ValueError("Restored quota reservation conflicts with its intent")
                db.execute("INSERT OR IGNORE INTO job_admissions(id,subject,application,environment,job,key,request_hash,day,state,payload,created) "
                           "VALUES (?,?,?,?,?,?,?,?,'unknown',?,?)",
                           (identifier, witness["subject"], witness["application"], witness["environment"], witness["job"], witness["key"],
                            witness["request_hash"], witness["day"], canonical(payload), time.time()))
            count += 1
    return {"witnesses": count, "recovery_mode": True, "next_action": "Reconcile provider state and verify quota before explicitly resuming admission."}


def scan_execution(store, client, headers, name, identifier):
    """One bounded page per observation; retain the cursor so large histories do not monopolize safety lanes."""
    with store.connect() as db:
        row = db.execute("SELECT payload FROM execution_scans WHERE id=?", (identifier,)).fetchone()
    scan = json.loads(row[0]) if row else {"token": None, "matches": []}
    response = client.get("https://run.googleapis.com/v2/" + name + "/executions", headers=headers,
                          params={"pageSize": 100, **({"pageToken": scan["token"]} if scan["token"] else {})})
    response.raise_for_status()
    page = response.json()
    matches = set(scan["matches"])
    for item in page.get("executions", []):
        env = [value for container in (item.get("template") or {}).get("containers", []) for value in container.get("env", [])]
        if any(value.get("name") == "FAST_DEPLOY_OPERATION_ID" and value.get("value") == identifier for value in env):
            matches.add(item["name"])
    token = page.get("nextPageToken")
    if len(matches) > 1:
        raise ValueError("More than one provider execution matches this operation; manual review required")
    with store.connect() as db:
        value = canonical({"token": token, "matches": sorted(matches) if token else []})
        if row:
            db.execute("UPDATE execution_scans SET payload=? WHERE id=? AND payload=?", (value, identifier, row[0]))
        else:
            db.execute("INSERT OR IGNORE INTO execution_scans VALUES (?,?)", (identifier, value))
    return next(iter(matches)) if matches and not token else None
