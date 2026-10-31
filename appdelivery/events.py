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
import re
from logging.handlers import RotatingFileHandler

log = logging.getLogger("appdelivery")
release_var = contextvars.ContextVar("appdelivery_release", default=None)
identity_var = contextvars.ContextVar("appdelivery_identity", default=None)
SECRET_KEYS = frozenset({"password", "token", "access_token", "secret", "authorization", "private_key", "credentials", "env", "env_text", "value", "values"})
REDACTED = "[redacted]"


# Credentials by their shape, wherever they appear in a value (review of 2026-10-30, L6): a Google access token, a bearer
# header, a PEM block, and a long run of token characters that mixes cases and digits (a hex digest, all one case, stays).
CREDENTIAL_SHAPES = (re.compile(r"ya29\.[A-Za-z0-9._-]+"), re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
                     re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)"))
TOKEN_RUN = re.compile(r"[A-Za-z0-9+_-]{40,}={0,2}")


def redact_text(text: str) -> str:
    """A string with every credential-shaped part replaced."""
    for shape in CREDENTIAL_SHAPES:
        text = shape.sub(REDACTED, text)
    return TOKEN_RUN.sub(lambda found: REDACTED if _mixed(found.group(0)) else found.group(0), text)


def _mixed(run: str) -> bool:
    return any(c.islower() for c in run) and any(c.isupper() for c in run) and any(c.isdigit() for c in run)


def redact(value):
    """A copy with every value under a secret-looking key replaced, and credential-shaped text replaced in every other
    string; nested dicts and lists included."""
    if isinstance(value, dict):
        return {key: (REDACTED if str(key).lower() in SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
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
