"""Infrastructure state only: requests, the resources they created, operations and audit events, in SQLite.

No application state lives here, and no credential: a database password is returned once and never written. Every value
is JSON in a text column next to the few fields the service filters on, so a provider can add detail without a migration.
"""
import contextlib
import datetime
import json
import sqlite3
import uuid

from ..models.resources import load_resource
from ..models.response import OperationRecord, OperationStatus, RequestStatus

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, region TEXT NOT NULL, zone TEXT, owner TEXT, "
    "status TEXT NOT NULL, request_json TEXT NOT NULL, labels_json TEXT NOT NULL, estimated_cost REAL, error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS resources (resource_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, "
    "resource_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS resources_request ON resources (request_id)",
    "CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL, resource_id TEXT, status TEXT NOT NULL, "
    "identity TEXT, started_at TEXT NOT NULL, finished_at TEXT, error_json TEXT, detail TEXT)",
    "CREATE INDEX IF NOT EXISTS operations_request ON operations (request_id)",
    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, request_id TEXT, operation_id TEXT, identity TEXT, client TEXT, "
    "kind TEXT NOT NULL, name TEXT, outcome TEXT, duration_ms INTEGER, detail TEXT)",
    "CREATE INDEX IF NOT EXISTS events_at ON events (at)",
)
INSERT_REQUEST = ("INSERT INTO requests (request_id, name, provider, region, zone, owner, status, request_json, labels_json, estimated_cost, error_json, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
SELECT_REQUEST = "SELECT * FROM requests WHERE request_id = ?"
LIST_REQUESTS = ("SELECT * FROM requests WHERE (? IS NULL OR owner = ?) AND (? IS NULL OR status = ?) AND (? IS NULL OR provider = ?) "
                 "ORDER BY created_at DESC, request_id LIMIT ?")
COUNT_ACTIVE = "SELECT COUNT(*) FROM requests WHERE owner = ? AND status IN ('PROVISIONING', 'READY', 'FAILED', 'DESTROYING', 'DESTROY_FAILED')"
UPDATE_REQUEST = "UPDATE requests SET status = ?, error_json = ?, updated_at = ? WHERE request_id = ?"
UPSERT_RESOURCE = ("INSERT INTO resources (resource_id, request_id, kind, name, status, resource_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(resource_id) DO UPDATE SET status = excluded.status, resource_json = excluded.resource_json, updated_at = excluded.updated_at")
SELECT_RESOURCE = "SELECT resource_json FROM resources WHERE resource_id = ?"
RESOURCES_OF = "SELECT resource_json FROM resources WHERE request_id = ? ORDER BY rowid"
IN_NETWORK = "SELECT resource_json FROM resources WHERE json_extract(resource_json, '$.network') = ? AND status NOT IN ('DELETED', 'PENDING') ORDER BY rowid"
INSERT_OPERATION = "INSERT INTO operations (operation_id, request_id, kind, resource_id, status, identity, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
FINISH_OPERATION = "UPDATE operations SET status = ?, finished_at = ?, error_json = ?, detail = ? WHERE operation_id = ?"
SELECT_OPERATION = "SELECT * FROM operations WHERE operation_id = ?"
OPERATIONS_OF = "SELECT * FROM operations WHERE request_id = ? ORDER BY rowid"
INSERT_EVENT = "INSERT INTO events (at, request_id, operation_id, identity, client, kind, name, outcome, duration_ms, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
RECENT_EVENTS = "SELECT * FROM events WHERE (? IS NULL OR request_id = ?) AND (? IS NULL OR identity = ?) ORDER BY id DESC LIMIT ?"


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


class StateManager:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA journal_mode=WAL")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    # ---- requests
    def create_request(self, request_id, name, provider, region, zone, owner, status, request: dict, labels: dict, estimated_cost=None) -> dict:
        moment = now()
        with self.connect() as db:
            db.execute(INSERT_REQUEST, (request_id, name, provider, region, zone, owner, str(status), json.dumps(request), json.dumps(labels), estimated_cost, None, moment, moment))
        return self.get_request(request_id)

    def get_request(self, request_id) -> dict | None:
        with self.connect() as db:
            row = db.execute(SELECT_REQUEST, (request_id,)).fetchone()
        return self._request(row) if row else None

    def list_requests(self, owner=None, status=None, provider=None, limit=100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(LIST_REQUESTS, (owner, owner, status, status, provider, provider, limit)).fetchall()
        return [self._request(row) for row in rows]

    def active_requests(self, owner) -> int:
        with self.connect() as db:
            return int(db.execute(COUNT_ACTIVE, (owner,)).fetchone()[0])

    def set_request_status(self, request_id, status, error=None) -> None:
        with self.connect() as db:
            db.execute(UPDATE_REQUEST, (str(status), json.dumps(error) if error else None, now(), request_id))

    @staticmethod
    def _request(row) -> dict:
        found = dict(row)
        found["request"] = json.loads(found.pop("request_json"))
        found["labels"] = json.loads(found.pop("labels_json"))
        error = found.pop("error_json")
        found["error"] = json.loads(error) if error else None
        found["status"] = RequestStatus(found["status"])
        return found

    # ---- resources
    def save_resource(self, resource) -> None:
        moment = now()
        data = resource.model_dump(mode="json")
        with self.connect() as db:
            db.execute(UPSERT_RESOURCE, (resource.id, resource.request_id, resource.kind.value, resource.name, resource.status.value, json.dumps(data), resource.created_at or moment, moment))

    def get_resource(self, resource_id):
        with self.connect() as db:
            row = db.execute(SELECT_RESOURCE, (resource_id,)).fetchone()
        return load_resource(json.loads(row[0])) if row else None

    def resources_of(self, request_id) -> list:
        with self.connect() as db:
            rows = db.execute(RESOURCES_OF, (request_id,)).fetchall()
        return [load_resource(json.loads(row[0])) for row in rows]

    def resources_in_network(self, network_id) -> list:
        """Machines and databases, of any request, that joined this network and still exist."""
        with self.connect() as db:
            rows = db.execute(IN_NETWORK, (network_id,)).fetchall()
        return [load_resource(json.loads(row[0])) for row in rows]

    # ---- operations
    def start_operation(self, request_id, kind, resource_id=None, identity=None) -> str:
        operation_id = new_id()
        with self.connect() as db:
            db.execute(INSERT_OPERATION, (operation_id, request_id, kind, resource_id, OperationStatus.RUNNING.value, identity, now()))
        return operation_id

    def finish_operation(self, operation_id, status, error=None, detail=None) -> None:
        with self.connect() as db:
            db.execute(FINISH_OPERATION, (OperationStatus(status).value, now(), json.dumps(error) if error else None, detail, operation_id))

    def get_operation(self, operation_id) -> OperationRecord | None:
        with self.connect() as db:
            row = db.execute(SELECT_OPERATION, (operation_id,)).fetchone()
        return self._operation(row) if row else None

    def operations_of(self, request_id) -> list[OperationRecord]:
        with self.connect() as db:
            rows = db.execute(OPERATIONS_OF, (request_id,)).fetchall()
        return [self._operation(row) for row in rows]

    @staticmethod
    def _operation(row) -> OperationRecord:
        found = dict(row)
        error = found.pop("error_json")
        return OperationRecord(**found, error=json.loads(error) if error else None)

    # ---- audit
    def record_event(self, kind, name=None, request_id=None, operation_id=None, identity=None, client=None, outcome=None, duration_ms=None, detail=None) -> None:
        with self.connect() as db:
            db.execute(INSERT_EVENT, (now(), request_id, operation_id, identity, client, kind, name, outcome, duration_ms, detail))

    def recent_events(self, limit=50, request_id=None, identity=None) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(RECENT_EVENTS, (request_id, request_id, identity, identity, limit)).fetchall()
        return [dict(row) for row in rows]
