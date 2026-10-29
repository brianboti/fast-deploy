"""Structured logging: one JSON line per event, carrying the release id and the identity, secret-looking fields redacted.

Events: release_received, release_planned, release_awaiting_approval, release_approved, rollout_started,
machine_deploying, machine_healthy, machine_unhealthy, machine_failed, machine_restored, rollout_completed,
rollout_failed, rollback_started, rollback_completed, rollback_failed, remove_started, remove_completed, remove_failed,
release_recovered. A secret's value never reaches one: values are read only toward a machine, and a container's log is
cleaned of every secret value the release read before it is kept.
"""
import contextlib
import contextvars
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler

log = logging.getLogger("appdelivery")
release_var = contextvars.ContextVar("appdelivery_release", default=None)
identity_var = contextvars.ContextVar("appdelivery_identity", default=None)
SECRET_KEYS = frozenset({"password", "token", "access_token", "secret", "authorization", "private_key", "credentials", "env", "env_text", "value", "values"})
REDACTED = "[redacted]"


def redact(value):
    if isinstance(value, dict):
        return {key: (REDACTED if str(key).lower() in SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def event(_event: str, _level: int = logging.INFO, **fields) -> None:
    record = {"event": _event, "at": now(), "release_id": release_var.get(), "identity": identity_var.get()}
    record.update(redact(fields))
    log.log(_level, json.dumps({key: value for key, value in record.items() if value is not None}, sort_keys=True, default=str))


@contextlib.contextmanager
def bound(release_id=None, identity=None):
    tokens = [(var, var.set(value)) for var, value in ((release_var, release_id), (identity_var, identity)) if value is not None]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def configure(level: int = logging.INFO, path: str | None = None) -> None:
    """Console always; a rotating file when the operator names one. Idempotent."""
    if log.level == logging.NOTSET or log.level > level:
        log.setLevel(level)
    if not any(isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler) for handler in log.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(stream)
    if path and not any(getattr(handler, "baseFilename", None) == path for handler in log.handlers):
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
