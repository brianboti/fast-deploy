"""The service's own state, in one SQLite file (WAL, one writer, one process): releases, their target machines, and audit
events. A release keeps its request as sent (bindings by name; a literal is not a credential, the contract refuses
those) and never a secret's value."""
import contextlib
import fcntl
import json
import os
import secrets
import sqlite3

from .events import now

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS releases (release_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, grp TEXT NOT NULL, environment TEXT NOT NULL, "
    "infra_owner TEXT NOT NULL, requested_by TEXT NOT NULL, approved_by TEXT, status TEXT NOT NULL, image TEXT NOT NULL, previous TEXT, "
    "can_roll_back INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL, plan_json TEXT NOT NULL, error_json TEXT, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS releases_target ON releases (request_id, grp)",
    "CREATE TABLE IF NOT EXISTS targets (release_id TEXT NOT NULL, position INTEGER NOT NULL, resource_id TEXT NOT NULL, name TEXT NOT NULL, zone TEXT NOT NULL, "
    "status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_json TEXT, log TEXT, PRIMARY KEY (release_id, resource_id))",
    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, release_id TEXT, identity TEXT, client TEXT, kind TEXT NOT NULL, "
    "name TEXT, outcome TEXT, duration_ms INTEGER, detail TEXT)",
    "CREATE INDEX IF NOT EXISTS events_at ON events (at)",
)
# A release whose machines a worker is changing: at most one per request and group at a time.
MOVING = ("ROLLING_OUT", "ROLLING_BACK", "REMOVING")
PENDING = ("AWAITING_APPROVAL",)


def new_release_id() -> str:
    return f"rel-{secrets.token_hex(6)}"


class ProcessLock:
    """One service process per state file: a second would see the first's work in flight and take it for interrupted."""

    def __init__(self, database: str):
        self.path = f"{database}.lock"
        self.handle = None

    def acquire(self) -> None:
        self.handle = open(self.path, "a")  # noqa: SIM115 - held open for the life of the process
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"Another appdelivery process holds {self.path}: one process per state file") from None

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class StateManager:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:" and os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if path == ":memory:" else None
        with self.connect() as db:
            for statement in SCHEMA:
                db.execute(statement)
            if self._memory is None:
                db.execute("PRAGMA journal_mode=WAL")

    @contextlib.contextmanager
    def connect(self):
        if self._memory is not None:  # tests: one shared in-memory database
            self._memory.row_factory = sqlite3.Row
            with self._memory:
                yield self._memory
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    # ---- releases
    def create_release(self, release_id, request_id, group, environment, infra_owner, requested_by, status, image, previous, request: dict, plan: dict,
                       machines: list[dict]) -> dict:
        moment = now()
        with self.connect() as db:
            db.execute("INSERT INTO releases (release_id, request_id, grp, environment, infra_owner, requested_by, approved_by, status, image, previous, can_roll_back, "
                       "request_json, plan_json, error_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, ?, ?, NULL, ?, ?)",
                       (release_id, request_id, group, environment, infra_owner, requested_by, status, image, previous, json.dumps(request), json.dumps(plan), moment, moment))
            for position, item in enumerate(machines):
                db.execute("INSERT INTO targets (release_id, position, resource_id, name, zone, status) VALUES (?, ?, ?, ?, ?, 'PENDING')",
                           (release_id, position, item["resource"], item["name"], item["zone"]))
        return self.get_release(release_id)

    def get_release(self, release_id) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM releases WHERE release_id = ?", (release_id,)).fetchone()
            targets = db.execute("SELECT * FROM targets WHERE release_id = ? ORDER BY position", (release_id,)).fetchall()
        return self._release(row, targets) if row else None

    def list_releases(self, infra_owner, request_id=None, environment=None, limit=50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT release_id FROM releases WHERE infra_owner = ? AND (? IS NULL OR request_id = ?) AND (? IS NULL OR environment = ?) "
                              "ORDER BY created_at DESC, rowid DESC LIMIT ?", (infra_owner, request_id, request_id, environment, environment, limit)).fetchall()
        return [self.get_release(row["release_id"]) for row in rows]

    def releases_in(self, statuses) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        with self.connect() as db:
            rows = db.execute(f"SELECT release_id FROM releases WHERE status IN ({marks}) ORDER BY rowid", tuple(statuses)).fetchall()  # noqa: S608 - placeholders only
        return [self.get_release(row["release_id"]) for row in rows]

    def busy_release(self, request_id, group) -> dict | None:
        """The release of this request and group a worker is changing, or one waiting for approval."""
        statuses = (*MOVING, *PENDING)
        marks = ", ".join("?" for _ in statuses)
        with self.connect() as db:
            row = db.execute(f"SELECT release_id FROM releases WHERE request_id = ? AND grp = ? AND status IN ({marks}) ORDER BY rowid DESC LIMIT 1",  # noqa: S608
                             (request_id, group, *statuses)).fetchone()
        return self.get_release(row["release_id"]) if row else None

    def running_release(self, request_id, group) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT release_id FROM releases WHERE request_id = ? AND grp = ? AND status = 'RUNNING' ORDER BY rowid DESC LIMIT 1",
                             (request_id, group)).fetchone()
        return self.get_release(row["release_id"]) if row else None

    def running_releases(self, request_id) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT release_id FROM releases WHERE request_id = ? AND status = 'RUNNING' ORDER BY grp", (request_id,)).fetchall()
        return [self.get_release(row["release_id"]) for row in rows]

    def transition(self, release_id, expected: tuple, status: str, error: dict | None = None, **fields) -> bool:
        """Move a release from one of the expected statuses to status, with the fields given; False if it was not in one."""
        allowed = {"approved_by", "can_roll_back"}
        assert set(fields) <= allowed  # noqa: S101 - a programming error, not input
        sets = ", ".join([f"{name} = ?" for name in fields] + ["status = ?", "error_json = ?", "updated_at = ?"])
        marks = ", ".join("?" for _ in expected)
        values = (*[int(value) if isinstance(value, bool) else value for value in fields.values()], status, json.dumps(error) if error else None, now(), release_id, *expected)
        with self.connect() as db:
            done = db.execute(f"UPDATE releases SET {sets} WHERE release_id = ? AND status IN ({marks})", values).rowcount  # noqa: S608 - names from a fixed set
        return done == 1

    def set_target(self, release_id, resource_id, status, error: dict | None = None, log: str | None = None, started: bool = False, finished: bool = False) -> None:
        moment = now()
        with self.connect() as db:
            db.execute("UPDATE targets SET status = ?, error_json = COALESCE(?, error_json), log = COALESCE(?, log), started_at = CASE WHEN ? THEN ? ELSE started_at END, "
                       "finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE release_id = ? AND resource_id = ?",
                       (status, json.dumps(error) if error else None, log, started, moment, finished, moment, release_id, resource_id))

    @staticmethod
    def _release(row, targets) -> dict:
        record = dict(row)
        record["group"] = record.pop("grp")
        record["can_roll_back"] = bool(record["can_roll_back"])
        record["request"] = json.loads(record.pop("request_json"))
        record["plan"] = json.loads(record.pop("plan_json"))
        record["error"] = json.loads(record.pop("error_json")) if record.get("error_json") else None
        record["targets"] = [{**dict(item), "error": json.loads(item["error_json"]) if item["error_json"] else None} for item in targets]
        for item in record["targets"]:
            item.pop("error_json", None)
            item.pop("release_id", None)
        return record

    # ---- audit
    def record_event(self, kind, name=None, release_id=None, identity=None, client=None, outcome=None, duration_ms=None, detail=None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events (at, release_id, identity, client, kind, name, outcome, duration_ms, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (now(), release_id, identity, client, kind, name, outcome, duration_ms, detail))
