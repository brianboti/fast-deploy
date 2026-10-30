"""Infrastructure state only: requests, the resources they created, operations and audit events, in SQLite.

No application state lives here, and no credential: a generated database password goes straight to a secret at the
provider. Every value is JSON in a text column next to the few fields the service filters on, so a provider can add detail
without a migration. One service process works on a state file at a time, which ProcessLock enforces.
"""
import contextlib
import datetime
import json
import os
import sqlite3
import uuid

try:
    import fcntl
except ImportError:  # not on Windows; the service runs on Linux and macOS
    fcntl = None

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
# Columns added after the first release; created on an older database by migrate().
REQUEST_COLUMNS = (("expires_at", "TEXT"), ("lifecycle_json", "TEXT"), ("access_json", "TEXT"))
LIVE = ("PROVISIONING", "READY", "STOPPING", "STOPPED", "STARTING", "FAILED", "DESTROYING", "DESTROY_FAILED")
INSERT_REQUEST = ("INSERT INTO requests (request_id, name, provider, region, zone, owner, status, request_json, labels_json, estimated_cost, error_json, created_at, updated_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
SELECT_REQUEST = "SELECT * FROM requests WHERE request_id = ?"
LIST_REQUESTS = ("SELECT * FROM requests WHERE (? IS NULL OR owner = ?) AND (? IS NULL OR status = ?) AND (? IS NULL OR provider = ?) "
                 "ORDER BY created_at DESC, request_id LIMIT ?")
COUNT_ACTIVE = ("SELECT COUNT(*) FROM requests WHERE owner = ? AND status IN "
                "('PROVISIONING', 'READY', 'STOPPING', 'STOPPED', 'STARTING', 'FAILED', 'DESTROYING', 'DESTROY_FAILED')")
SWEEPABLE = ("SELECT * FROM requests WHERE status IN ('READY', 'STOPPED') OR (status IN ('FAILED', 'DESTROY_FAILED') AND expires_at IS NOT NULL) "
             "ORDER BY created_at, request_id")
UPDATE_LIFECYCLE = "UPDATE requests SET expires_at = ?, lifecycle_json = ?, updated_at = ? WHERE request_id = ?"
UPDATE_ACCESS = "UPDATE requests SET access_json = ?, updated_at = ? WHERE request_id = ?"
UPDATE_REQUEST = "UPDATE requests SET status = ?, error_json = ?, updated_at = ? WHERE request_id = ?"
TRANSITION = "UPDATE requests SET status = ?, error_json = ?, updated_at = ? WHERE request_id = ? AND status IN ({})"
UPSERT_RESOURCE = ("INSERT INTO resources (resource_id, request_id, kind, name, status, resource_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT(resource_id) DO UPDATE SET status = excluded.status, resource_json = excluded.resource_json, updated_at = excluded.updated_at")
SELECT_RESOURCE = "SELECT resource_json FROM resources WHERE resource_id = ?"
RESOURCES_OF = "SELECT resource_json FROM resources WHERE request_id = ? ORDER BY rowid"
IN_NETWORK = "SELECT resource_json FROM resources WHERE json_extract(resource_json, '$.network') = ? AND status NOT IN ('DELETED', 'PENDING') ORDER BY rowid"
INSERT_OPERATION = "INSERT INTO operations (operation_id, request_id, kind, resource_id, status, identity, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
FINISH_OPERATION = "UPDATE operations SET status = ?, finished_at = ?, error_json = ?, detail = ? WHERE operation_id = ?"
SELECT_OPERATION = "SELECT * FROM operations WHERE operation_id = ?"
OPERATIONS_OF = "SELECT * FROM operations WHERE request_id = ? ORDER BY rowid"
RUNNING_OPERATIONS = "SELECT * FROM operations WHERE status = 'RUNNING' ORDER BY rowid"
RUNNING_OPERATIONS_OF = "SELECT * FROM operations WHERE request_id = ? AND status = 'RUNNING' ORDER BY rowid"
EXPIRING = "AND expires_at IS NOT NULL AND expires_at <= ? AND status IN ('READY', 'STOPPING', 'STOPPED', 'STARTING') ORDER BY"
INSERT_EVENT = "INSERT INTO events (at, request_id, operation_id, identity, client, kind, name, outcome, duration_ms, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
RECENT_EVENTS = "SELECT * FROM events WHERE (? IS NULL OR request_id = ?) AND (? IS NULL OR identity = ?) ORDER BY id DESC LIMIT ?"


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


class ProcessLock:
    """One service process per state file: an exclusive lock on <database>.lock from start to the end of shutdown. A second
    process refuses to start rather than recover, as interrupted, requests the first is still working on."""

    def __init__(self, database: str):
        self.path = None if database == ":memory:" or fcntl is None else f"{database}.lock"
        self._file = None

    def acquire(self) -> None:
        if self.path is None:
            return
        handle = open(self.path, "a+")  # noqa: SIM115 - held open for the life of the process
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError(f"Another process of this service is using its state file ({self.path} is locked); one process per state file") from None
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None


def private(path: str) -> None:
    """The state file readable by the service's user only: it holds the inventory, every caller's address and token hashes."""
    if path != ":memory:" and os.path.isfile(path):
        os.chmod(path, 0o600)


class StateManager:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as db:
            for statement in SCHEMA:
                db.execute(statement)
            self.migrate(db)
            db.execute("CREATE INDEX IF NOT EXISTS requests_expires ON requests (expires_at)")
            db.execute("PRAGMA journal_mode=WAL")
        private(path)

    @staticmethod
    def migrate(db) -> None:
        """Add the columns later releases need to a database an earlier one created; nothing is rewritten."""
        present = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
        for column, kind in REQUEST_COLUMNS:
            if column not in present:
                db.execute(f"ALTER TABLE requests ADD COLUMN {column} {kind}")

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

    def list_requests(self, owner=None, status=None, provider=None, limit=100, expires_before=None) -> list[dict]:
        with self.connect() as db:
            if expires_before is None:
                rows = db.execute(LIST_REQUESTS, (owner, owner, status, status, provider, provider, limit)).fetchall()
            else:
                # only requests that can still expire: running, stopping, stopped or starting
                rows = db.execute(LIST_REQUESTS.replace("ORDER BY", EXPIRING), (owner, owner, status, status, provider, provider, expires_before, limit)).fetchall()
        return [self._request(row) for row in rows]

    def sweepable_requests(self) -> list[dict]:
        """Requests the sweeper looks at: ready or stopped, and failed ones with a lifetime, which expiry destroys."""
        with self.connect() as db:
            rows = db.execute(SWEEPABLE).fetchall()
        return [self._request(row) for row in rows]

    def set_lifecycle(self, request_id, lifecycle: dict) -> None:
        with self.connect() as db:
            db.execute(UPDATE_LIFECYCLE, (lifecycle.get("expires_at"), json.dumps(lifecycle), now(), request_id))

    def set_access(self, request_id, access: list[dict]) -> None:
        with self.connect() as db:
            db.execute(UPDATE_ACCESS, (json.dumps(access), now(), request_id))

    def active_requests(self, owner) -> int:
        with self.connect() as db:
            return int(db.execute(COUNT_ACTIVE, (owner,)).fetchone()[0])

    def set_request_status(self, request_id, status, error=None) -> None:
        with self.connect() as db:
            db.execute(UPDATE_REQUEST, (str(status), json.dumps(error) if error else None, now(), request_id))

    def transition(self, request_id, expected, status, error=None) -> bool:
        """Move a request to a status only from one it is expected to be in, so that work finishing late never overwrites a
        status set meanwhile (a request destroyed while a stop was on its way). True when it moved."""
        states = [str(item) for item in expected]
        with self.connect() as db:
            moved = db.execute(TRANSITION.format(", ".join("?" * len(states))), (str(status), json.dumps(error) if error else None, now(), request_id, *states)).rowcount
        return moved == 1

    @staticmethod
    def _request(row) -> dict:
        found = dict(row)
        found["request"] = json.loads(found.pop("request_json"))
        found["labels"] = json.loads(found.pop("labels_json"))
        error = found.pop("error_json")
        found["error"] = json.loads(error) if error else None
        found["status"] = RequestStatus(found["status"])
        lifecycle, access = found.pop("lifecycle_json", None), found.pop("access_json", None)
        found["lifecycle"] = json.loads(lifecycle) if lifecycle else {}
        found["access"] = json.loads(access) if access else []
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

    def running_operations(self) -> list[OperationRecord]:
        with self.connect() as db:
            rows = db.execute(RUNNING_OPERATIONS).fetchall()
        return [self._operation(row) for row in rows]

    def running_operations_of(self, request_id) -> list[OperationRecord]:
        with self.connect() as db:
            rows = db.execute(RUNNING_OPERATIONS_OF, (request_id,)).fetchall()
        return [self._operation(row) for row in rows]

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
