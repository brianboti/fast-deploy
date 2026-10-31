"""Structured logging: one JSON line per event, each carrying the request and operation ids, secrets redacted.

The events the service emits: request_received, request_validated, provider_selected, resources_requested,
provisioning_started, resource_created, provisioning_completed, provisioning_failed, cleanup_started, cleanup_completed,
destruction_started, resource_deleted, destruction_completed, destruction_failed, and the provider calls behind them.
"""
import contextlib
import contextvars
import datetime
import json
import logging
import re
from logging.handlers import RotatingFileHandler

log = logging.getLogger("cloudinfra")
request_id_var = contextvars.ContextVar("cloudinfra_request_id", default=None)
operation_id_var = contextvars.ContextVar("cloudinfra_operation_id", default=None)
identity_var = contextvars.ContextVar("cloudinfra_identity", default=None)
SECRET_KEYS = frozenset({"password", "root_password", "rootpassword", "database_password", "token", "access_token", "refresh_token", "secret",
                         "authorization", "private_key", "credentials", "client_secret", "api_key"})
REDACTED = "[redacted]"


# Credentials by their shape, wherever they appear in a value (review of 2026-10-30, L6): a Google access token, a bearer
# header, a PEM block, and a long run of token characters (base64's / included) that mixes cases and digits, or that is
# letters and digits alone beyond hex. A hex digest stays, and so does a name or a path, which hyphens or slashes split.
CREDENTIAL_SHAPES = (re.compile(r"ya29\.[A-Za-z0-9._-]+"), re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
                     re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?(?:-----END [A-Z ]+-----|$)"))
TOKEN_RUN = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
HEX = re.compile(r"^[a-f0-9]+$")
ALPHANUMERIC = re.compile(r"^[A-Za-z0-9]+$")


def redact_text(text: str) -> str:
    """A string with every credential-shaped part replaced."""
    for shape in CREDENTIAL_SHAPES:
        text = shape.sub(REDACTED, text)
    return TOKEN_RUN.sub(lambda found: REDACTED if _secret_like(found.group(0)) else found.group(0), text)


def _secret_like(run: str) -> bool:
    run = run.rstrip("=")
    if any(c.islower() for c in run) and any(c.isupper() for c in run) and any(c.isdigit() for c in run):
        return True
    # one case of letters and digits, unbroken: an API key, not a digest (hex) and not a name (it would carry hyphens)
    return bool(ALPHANUMERIC.fullmatch(run)) and not HEX.fullmatch(run.lower()) and any(c.isdigit() for c in run) and any(c.isalpha() for c in run)


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


def event(_event: str, _level: int = logging.INFO, **fields):
    """Emit one event. The ids bound to the current context are added; fields are redacted before they are written.
    The first two parameters are named so that no field (name, kind, level) can collide with them."""
    record = {"event": _event, "at": now(), "request_id": request_id_var.get(), "operation_id": operation_id_var.get(), "identity": identity_var.get()}
    record.update(redact(fields))
    log.log(_level, json.dumps({key: value for key, value in record.items() if value is not None}, sort_keys=True, default=str))


@contextlib.contextmanager
def bound(request_id=None, operation_id=None, identity=None):
    """Bind ids to the current context for the events emitted inside the block."""
    tokens = []
    for var, value in ((request_id_var, request_id), (operation_id_var, operation_id), (identity_var, identity)):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def configure(level: int = logging.INFO, path: str | None = None):
    """Console output always; a rotating file when the operator names one. Idempotent across factory calls."""
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
