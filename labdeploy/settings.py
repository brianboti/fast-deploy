"""The settings page (https://HOST/settings): reports in its Monitor section, and the controller's own configuration file,
changed in the browser, in its Configure section.

Server-rendered HTML without JavaScript, behind the same headers as the OAuth sign-in page. A person signs in with an
operator token (the same lookup and failed-login budget as /login). Any signed-in principal may open Monitor
(labdeploy/monitor.py), scoped to the applications its token may see as Claude's tools are; only an identity with
applications: ["*"] and can_deploy may open Configure. Every change goes the same way: the form becomes a candidate
configuration, the candidate is validated exactly as the controller validates its file (the loader, Config, and the
overlay of application_settings on the applications onboarding registered), the unified diff is shown, and only the confirm
step writes the file: atomically, mode 0600, the previous version kept under history/, a copy in the backup bucket as the
nightly backup makes, one audit event "configuration" naming the keys (never their values). LiveConfig then reloads the
file by itself.

What it deliberately does not change: contracts (platform.yaml) and infrastructure, which change only through pull requests
(docs/ROADMAP.md D32), and secrets, which the operator sets with gcloud (D5). The page links to where those live.
"""
import copy
import dataclasses
import datetime
import difflib
import hashlib
import hmac
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import jwt
import pydantic
import yaml
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__, deployment_page
from . import monitor_page as pages
from . import settings_page as view
from .config import Application, Config, PlatformUpdates, ProductionSettings, with_settings
from .connections import BrowserConnections
from .controller import copy_to_bucket
from .deployments import Deployments
from .infra import DEFAULT_SETTINGS
from .models import UniqueLoader
from .monitor import Monitor
from .oauth import find_principal, principal_for
from .settings_page import KILL_SWITCH, MONITOR, PREFIX, TABS, effective, masked

COOKIE = "__Host-fast-deploy-settings"  # __Host-: only over HTTPS, for this host and path / only
PENDING_SECONDS = 600  # a reviewed change must be confirmed within ten minutes
HISTORY_KEEP = 20
HEADER = "# The controller's configuration, last written by its settings page (/settings); earlier versions are in history/ beside it.\n"


def is_settings_path(path):
    return path == PREFIX or path.startswith(PREFIX + "/")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def qualifies(principal):
    """Who may change the controller's configuration: an identity that sees every application and may deploy."""
    return "*" in principal.applications and principal.can_deploy


class Sessions:
    """Sign-ins to the settings page. The cookie carries a random id signed with a key that lives only in this process
    (a restart signs everyone out); the database keeps the id's SHA-256, the principal and the token hash it signed in with,
    so rotating or removing that principal ends the session at its next request."""

    def __init__(self, store):
        self.connect = store.connect
        self.key = secrets.token_bytes(32)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings_sessions (digest TEXT PRIMARY KEY, subject TEXT NOT NULL, credential TEXT NOT NULL, "
                       "created REAL NOT NULL, expires REAL NOT NULL)")

    def sign(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def open(self, principal, seconds):
        session_id, now = secrets.token_urlsafe(32), time.time()
        with self.connect() as db:
            db.execute("DELETE FROM settings_sessions WHERE expires < ?", (now,))
            db.execute("INSERT INTO settings_sessions VALUES (?, ?, ?, ?, ?)", (digest(session_id), principal.name, principal.token_sha256, now, now + seconds))
        return f"{session_id}.{self.sign(session_id)}"

    def find(self, cookie):
        """(session id, subject, token hash) behind a signed, live cookie, else None."""
        session_id, _, signature = (cookie or "").partition(".")
        if not session_id or not hmac.compare_digest(signature.encode(), self.sign(session_id).encode()):
            return None
        with self.connect() as db:
            row = db.execute("SELECT subject, credential FROM settings_sessions WHERE digest=? AND expires > ?", (digest(session_id), time.time())).fetchone()
        return (session_id, row[0], row[1]) if row else None

    def close(self, session_id):
        with self.connect() as db:
            db.execute("DELETE FROM settings_sessions WHERE digest=?", (digest(session_id),))

    def end(self, subject):
        with self.connect() as db:
            db.execute("DELETE FROM settings_sessions WHERE subject=?", (subject,))


@dataclasses.dataclass
class Visit:
    principal: object
    session_id: str
    csrf: str


@dataclasses.dataclass
class Change:
    """A validated change waiting for its confirm step. Kept in this process only, so a new token never touches the disk."""
    session_id: str
    tab: str
    base: str  # SHA-256 of the file the change was made from; the confirm step refuses when the file moved on
    text: str  # the file to write
    keys: list
    ends: list  # principals whose sessions end when it is written
    token: tuple | None  # (principal name, new token), shown once on the result page
    expires: float
    back: str  # where Cancel and Back return: the tab, or the application's own page


class Pending:
    def __init__(self):
        self.lock, self.items = threading.Lock(), {}

    def add(self, change):
        change_id = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.items = {key: item for key, item in self.items.items() if item.expires > now}
            self.items[change_id] = change
        return change_id

    def take(self, session_id, change_id):
        with self.lock:
            change = self.items.get(change_id)
            if change is None or change.session_id != session_id or change.expires < time.time():
                return None
            return self.items.pop(change_id)


# ---- the file: read, resolve, validate, diff, write

@dataclasses.dataclass
class Current:
    raw: bytes
    text: str
    data: dict | None
    config: Config | None
    error: str | None

    @property
    def sha(self):
        return hashlib.sha256(self.raw).hexdigest()


def parse(text):
    data = yaml.load(text, Loader=UniqueLoader)  # noqa: S506 - SafeLoader subclass, the controller's own loader
    if not isinstance(data, dict):
        raise ValueError("The file must hold the settings as a mapping (key: value lines) at its top level")
    return data


def read(path):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    try:
        data = parse(text)
        return Current(raw, text, data, Config.model_validate(data), None)
    except pydantic.ValidationError as exc:
        return Current(raw, text, None, None, "; ".join(f"{key or 'the file'}: {message}" for key, message in problems(exc, {}).items()))
    except (yaml.YAMLError, ValueError) as exc:
        return Current(raw, text, None, None, yaml_problem(exc))


def yaml_problem(exc):
    mark = getattr(exc, "problem_mark", None)
    where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
    return f"The YAML does not parse: {where}{getattr(exc, 'problem', None) or exc}"


def resolved(config):
    """The configuration as the controller reads it, in the shape the file gave it: only the keys the file sets, with the
    values validation resolved. Both sides of the diff, and the file the page writes, are this."""
    return config.model_dump(mode="json", exclude_unset=True)


def dump(data):
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def plain(item):
    """One pydantic error in words a person can act on."""
    kind, context = item.get("type", ""), item.get("ctx") or {}
    words = {
        "greater_than_equal": f"must be at least {context.get('ge')}", "less_than_equal": f"must be at most {context.get('le')}",
        "greater_than": f"must be more than {context.get('gt')}", "less_than": f"must be less than {context.get('lt')}",
        "missing": "is required", "extra_forbidden": "is not a setting the controller knows (check the spelling)",
        "int_type": "must be a whole number", "bool_type": "must be true or false", "string_type": "must be text",
        "float_type": "must be a number", "list_type": "must be a list", "dict_type": "must be a mapping (key: value lines)",
        "string_pattern_mismatch": "does not have the form this setting needs", "literal_error": f"must be one of {context.get('expected')}",
        "too_short": f"needs at least {context.get('min_length')} item(s)", "too_long": f"takes at most {context.get('max_length')} item(s)",
        "model_type": "must be a mapping (key: value lines)", "model_attributes_type": "must be a mapping (key: value lines)",
    }
    if kind in words:
        return words[kind]
    message = str(item.get("msg", ""))
    return message.removeprefix("Value error, ")


def problems(exc, candidate, prefix=()):
    """Every validation error by the dotted key of the setting it concerns ("" for the whole file); principals by name."""
    found = {}
    for item in exc.errors():
        loc = [*prefix, *item.get("loc", ())]
        if len(loc) >= 2 and loc[0] == "principals" and isinstance(loc[1], int):
            try:
                loc[1] = candidate["principals"][loc[1]]["name"]
            except (KeyError, IndexError, TypeError):
                pass
        message = plain(item)
        first = message.split(" ", 1)[0]
        infra = re.match(r"infra\.(\w+) ", message)
        if len(loc) == 2 and loc[0] in ("applications", "application_settings"):  # an application's own check names the field it is about
            if infra:
                loc += ["infra", infra.group(1)]
            elif message.startswith("Viewer "):
                loc.append("viewers")
            elif first in Application.model_fields:
                loc.append(first)
        elif loc == ["production"] and first.startswith("production.") and first[len("production."):] in ProductionSettings.model_fields:
            loc = first.split(".")
        key = ".".join(str(part) for part in loc)
        found[key] = f"{found[key]}; {message}" if key in found and message not in found[key] else message
    return found


def validate(candidate, registry):
    """(Config, {}) for a candidate the controller would accept, else (None, problems by key). The same checks the controller
    makes: the loader's Config, then every onboarded application with its application_settings laid over it."""
    try:
        config = Config.model_validate(candidate)
    except pydantic.ValidationError as exc:
        return None, problems(exc, candidate)
    found = {}
    for app_id, application in registry.items():
        if app_id not in config.applications:
            try:
                with_settings(application, config.application_settings.get(app_id))
            except pydantic.ValidationError as exc:
                found.update(problems(exc, candidate, ("application_settings", app_id)))
    if not any(qualifies(p) for p in config.principals):
        found[""] = ("Nobody could open this page afterwards: keep at least one principal with applications: [\"*\"] and can_deploy. "
                     "To do it anyway, use scripts/machine.sh config-get and config-put.")
    return (None, found) if found else (config, {})


BLOCKS = {"platform", "platform_updates", "observer", "github_app", "oauth", "onboarding", "production"}  # the file's optional sections


def changed_keys(old, new, prefix=""):
    """The dotted keys whose values differ; principals by name, a whole application by its id. Names only: values never
    leave this function."""
    # A whole application, or a block such as platform_updates or production, that appears or goes is one change, not one per field.
    whole = (re.fullmatch(r"applications\.[^.]+", prefix) or prefix in BLOCKS) and (old is None or new is None)
    if not whole and (isinstance(old, dict) or isinstance(new, dict)) and isinstance(old or {}, dict) and isinstance(new or {}, dict):
        before, after = old or {}, new or {}
        keys = []
        for key in [*before, *(k for k in after if k not in before)]:
            if before.get(key, KeyError) != after.get(key, KeyError):
                keys += changed_keys(before.get(key), after.get(key), f"{prefix}.{key}" if prefix else str(key))
        return keys or ([prefix] if prefix and old != new else [])
    if prefix == "principals" and isinstance(old, list) and isinstance(new, list):
        before = {p.get("name"): p for p in old if isinstance(p, dict)}
        after = {p.get("name"): p for p in new if isinstance(p, dict)}
        keys = []
        for name in [*before, *(n for n in after if n not in before)]:
            if name not in before or name not in after:
                keys.append(f"principals.{name}")
            elif before[name] != after[name]:
                keys += changed_keys(before[name], after[name], f"principals.{name}")
        return keys or ["principals"]
    return [prefix]


def unified(old, new):
    return list(difflib.unified_diff(dump(masked(old)).splitlines(), dump(masked(new)).splitlines(), "configuration now", "after this change", lineterm=""))


def keep_history(path, raw, keep=HISTORY_KEEP):
    """The previous file under <its folder>/history/config-<UTC time>.yaml, mode 0600; only the newest `keep` stay."""
    folder = Path(path).parent / "history"
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = f"{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%S%fZ}"
    for attempt in range(100):
        target = folder / f"config-{stamp}{f'-{attempt}' if attempt else ''}.yaml"
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"Could not name a new file in {folder}")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
    for old in sorted(folder.glob("config-*.yaml"))[:-keep]:
        old.unlink()
    return target


def write_atomically(path, text):
    """Replace the file in one step: a temporary file in the same folder (mode 0600), flushed to disk, then renamed over it."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


# ---- reading the forms

INVALID = object()


def number(form, key, found, whole=True, required=False):
    raw = str(form.get(key, "")).strip()
    if not raw:
        if required:
            found[key] = "is required"
            return INVALID
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    if whole:
        found[key] = "must be a whole number"
        return INVALID
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        found[key] = "must be a number"
        return INVALID
    return value


def lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def words(text):
    return [word for word in re.split(r"[,\s]+", str(text or "").strip()) if word]


def choice(form, key, options, found):
    raw = str(form.get(key, ""))
    if raw not in options:
        found[key] = f"must be one of {', '.join(repr(option) for option in options if option)}"
        return INVALID
    return options[raw]


def same(a, b):
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def put(entry, field, value, current, default):
    """Write one setting into a mapping of the file only when it changes: removed when it returns to its default, else set."""
    if value is INVALID or same(value, current):
        return
    if same(value, default):
        entry.pop(field, None)
    else:
        entry[field] = value


def field_default(model, field):
    return model.model_fields[field].get_default(call_default_factory=True)


def override(candidate, app_id, field, value, current, base):
    """An onboarded application's switch under application_settings: dropped when it matches the registered entry."""
    if value is INVALID or same(value, current):
        return
    table = candidate.get("application_settings")
    if same(value, base):
        entry = (table or {}).get(app_id)
        if isinstance(entry, dict) and field in entry:
            entry.pop(field)
            if not entry:
                table.pop(app_id)
            if not table:
                candidate.pop("application_settings", None)
        return
    if not isinstance(table, dict):
        table = candidate["application_settings"] = {}
    if not isinstance(table.get(app_id), dict):
        table[app_id] = {}
    table[app_id][field] = value


APP_FIELDS = ("cost_ceiling_usd", "auto_deploy", "auto_merge", "bake_seconds", "viewers", "infra")
TRISTATE = {"": None, "true": True, "false": False}


def application_values(form, prefix, found):
    values = {
        "cost_ceiling_usd": number(form, f"{prefix}.cost_ceiling_usd", found, whole=False),
        "auto_deploy": f"{prefix}.auto_deploy" in form,
        "auto_merge": choice(form, f"{prefix}.auto_merge", TRISTATE, found),
        "bake_seconds": number(form, f"{prefix}.bake_seconds", found, required=True),
        "viewers": lines(form.get(f"{prefix}.viewers")),
    }
    infra, bad = {}, False
    for key, default in DEFAULT_SETTINGS.items():
        name = f"{prefix}.infra.{key}"
        value = choice(form, name, TRISTATE, found) if isinstance(default, bool) else number(form, name, found)
        if value is INVALID:
            bad = True
        elif value is not None:
            infra[key] = value
    values["infra"] = INVALID if bad else infra
    return values


# ---- the routes

def routes(config, store, failures, controller=None, jobs=None):
    """The settings page's routes. `config` is the controller's LiveConfig; `failures` the failed-login budget /login shares;
    `controller` the controller whose records and observer the Monitor section reads; `jobs` the background jobs' state."""
    sessions, pending, write_lock = Sessions(store), Pending(), threading.Lock()
    monitor = Monitor(controller, jobs) if controller is not None else None
    deployments = Deployments(controller) if controller is not None else None
    connections = BrowserConnections(config, controller.github if controller is not None else None)
    if controller is not None:
        controller.browser_connections = connections
    google_cookie = "__Host-fast-deploy-google"

    def callback_cookie(value="", seconds=0):
        return f"{google_cookie}={value}; Max-Age={seconds}; Path=/; Secure; HttpOnly; SameSite=Lax"

    def registry():
        return {app_id: Application.model_validate(entry) for app_id, entry in store.registered_applications().items()}

    def client(request):
        return request.client.host if request.client else "unknown"

    def host_allowed(request):
        """Requests must name one of the controller's own hosts (allowed_hosts), as the MCP transport requires."""
        host = request.headers.get("host", "")
        for entry in config.allowed_hosts:
            if entry.endswith(":*"):
                base = entry[:-2]
                if host == base or (host.startswith(base + ":") and host[len(base) + 1:].isdigit()):
                    return True
            elif host == entry:
                return True
        return False

    def same_origin(request):
        origin = request.headers.get("origin")
        return origin is None or urlparse(origin).netloc == request.headers.get("host", "")

    def minutes():
        return config.settings_session_seconds // 60

    def cookie(value):
        return f"{COOKIE}={value}; Max-Age={config.settings_session_seconds}; Path=/; Secure; HttpOnly; SameSite=Strict"

    def clear_cookie():
        return f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"

    def frame(visit):
        """The frame of a Monitor view, or of a Configure screen refused: Configure shows only for an identity that may use it."""
        allowed = qualifies(visit.principal)
        return view.Frame(visit, __version__, allowed, len(config.applications) if allowed else None)

    def visitor(request):
        """(Visit, None) for a live sign-in, else (None, why it ended)."""
        found = sessions.find(request.cookies.get(COOKIE))
        if found is None:
            return None, ("Your sign-in ended: it expired, or the controller restarted. Sign in again." if request.cookies.get(COOKIE) else "")
        session_id, subject, credential = found
        principal = principal_for(config, subject, credential)
        if principal is None:
            sessions.close(session_id)
            connections.forget(session_id)
            return None, "Your sign-in ended: its token was rotated or its principal removed. Sign in again."
        return Visit(principal, session_id, sessions.sign("csrf:" + session_id)), None

    def guard(request):
        if not host_allowed(request):
            return view.message_page("Unknown host", "This host is not one of the controller's (allowed_hosts).", 400)
        if not getattr(config, "path", None):
            return view.message_page("Nothing to change", "This controller was started without a configuration file (LAB_CONFIG), so there is "
                                     "nothing here to change.", 404)
        return None

    def context(visit, state):
        snapshot = connections.snapshot(visit.session_id)
        if snapshot.get("review") and snapshot["review"]["config_sha"] != state.sha:
            snapshot["review"] = None
            snapshot["notice"] = "Settings changed. Check the repository again to review the current destination and permissions."
        return view.Context(visit, state, registry(), store.productions(), last_written(store, config.path), __version__, snapshot)

    async def show(request):
        refused = guard(request)
        if refused:
            return refused
        visit, ended = visitor(request)
        if visit is None:
            return view.sign_in_page(minutes(), ended, headers={"Set-Cookie": clear_cookie()} if ended else None)
        tab = request.query_params.get("tab") or "overview"
        if tab not in TABS:
            return await run_in_threadpool(monitor_view, visit, tab if tab in MONITOR else "overview", request.query_params)
        if not qualifies(visit.principal):
            return view.refusal(frame(visit), tab)
        app = request.query_params.get("app") or None if tab == "applications" else None
        state = await run_in_threadpool(read, config.path)
        ctx = context(visit, state)
        if tab == "connections":
            ctx.connections["step"] = request.query_params.get("step")
        if tab == "connections" and request.query_params.get("onboarding") and controller:
            try:
                ctx.connections["onboarding"] = await run_in_threadpool(controller.onboarding_status, visit.principal, request.query_params["onboarding"])
            except ValueError as exc:
                ctx.connections["error"] = str(exc)
        return view.tab_page(ctx, tab, app)

    def monitor_view(visit, tab, params):
        """One Monitor view, scoped to the applications the signed-in token may see."""
        principal, shown = visit.principal, frame(visit)
        if monitor is None:
            return view.shell(shown, tab, view.head(MONITOR[tab], "") + view.callout("gray", "Monitor needs the controller itself.",
                                                                                      "<p>This page was mounted without one.</p>"))
        try:
            if tab == "deployments":
                query = deployment_query(params)
                data = deployments.detail(principal, **query) if query else deployments.listing(principal)
                public_query = {"onboarding" if k == "application" else "plan": v for k, v in query.items()}
                return deployment_page.page(shown, data, public_query)
            if tab == "health" and params.get("app"):
                data = monitor.health(principal, params["app"], params.get("env") or "staging", params.get("range") or "24h", params.get("logs") == "1")
                return pages.health(shown, data)
            if tab == "health":
                return pages.health_list(shown, monitor.overview(principal))
            if tab == "releases":
                return pages.releases(shown, monitor.releases(principal))
            if tab == "cost":
                return pages.cost(shown, monitor.cost(principal))
            if tab == "activity":
                return pages.activity(shown, monitor.activity(principal, params.get("kind"), params.get("identity"), params.get("application"),
                                                              limit=200 if params.get("more") else 50))
            return pages.overview(shown, monitor.overview(principal))
        except ValueError as exc:  # an application this token may not see, or one without a production
            text = (f"There is no application named {params.get('app')} among those your token may see." if "not authorized" in str(exc)
                    else str(exc))
            return view.shell(shown, tab, view.head(MONITOR[tab], "") + view.callout("gray", text, f'<p><a href="{PREFIX}?tab=overview">Open the '
                                                                                                    "overview</a>.</p>"), 404)

    def deployment_query(params):
        if params.get("plan"):
            return {"plan_id": params["plan"]}
        if params.get("onboarding"):
            return {"application": params["onboarding"]}
        return {}

    async def deployment_status(request):
        # This route observes only: no dispatch, approval, retry, or cloud mutation.
        refused = guard(request)
        if refused:
            return refused
        visit, _ = visitor(request)
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if visit is None:
            return JSONResponse({"error": "Sign in again."}, status_code=401, headers=headers)
        if deployments is None:
            return JSONResponse({"error": "Deployment monitoring is unavailable."}, status_code=503, headers=headers)
        query = deployment_query(request.query_params)
        try:
            data = await run_in_threadpool(deployments.detail, visit.principal, **query) if query else await run_in_threadpool(deployments.listing, visit.principal)
        except ValueError:
            return JSONResponse({"error": "Deployment not found or not available to your account."}, status_code=404, headers=headers)
        if request.url.path.endswith("/report"):
            return JSONResponse(data, headers={**headers, "Content-Disposition": 'attachment; filename="deployment-report.json"'})
        body = deployment_page.detail(data) if query else deployment_page.listing(data)
        return JSONResponse({"html": body, "state": data.get("state", "list"), "label": data.get("label", "Deployment list updated"),
                             "error": data.get("error")}, headers=headers)

    async def deployment_script(request):
        if not host_allowed(request):
            return Response(status_code=400)
        return Response(deployment_page.SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    async def sign_in(request):
        refused = guard(request)
        if refused:
            return refused
        if not same_origin(request):
            return view.message_page("Refused", "This form must be sent from the settings page itself.", 403)
        data = await request.form()
        principal = find_principal(config, str(data.get("token", "")).strip())  # pasted tokens often carry a stray newline
        host = client(request)
        if principal is None:
            allowed, retry_after = failures.allow(host)
            if not allowed:
                store.record_event("sign_in", name="settings page", client=host, outcome="throttled")
                return view.message_page("Too many attempts", "Too many failed sign-in attempts from this address. Wait a minute and try again.",
                                         429, {"Retry-After": str(retry_after)})
            store.record_event("sign_in", name="settings page", client=host, outcome="rejected")
            return view.sign_in_page(minutes(), "That token was not accepted.", 401)
        # Every principal may open Monitor, scoped to what its token may see; Configure checks again on every request.
        store.record_event("sign_in", name="settings page", identity=principal.name, client=host, outcome="ok")
        value = sessions.open(principal, config.settings_session_seconds)
        return RedirectResponse(PREFIX, status_code=303, headers={"Cache-Control": "no-store", "Set-Cookie": cookie(value)})

    async def posted(request):
        """(Visit, form) for a POST from a signed-in operator with a valid form token, else (None, the response)."""
        refused = guard(request)
        if refused:
            return None, refused
        if not same_origin(request):
            return None, view.message_page("Refused", "This form must be sent from the settings page itself.", 403)
        visit, ended = visitor(request)
        if visit is None:
            return None, view.sign_in_page(minutes(), ended or "Sign in first.", 401)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), visit.csrf.encode()):
            return None, view.message_page("This form is stale", f'Open <a href="{PREFIX}">the settings page</a> again and repeat the change.', 403)
        if not qualifies(visit.principal):
            tab = str(form.get("tab", ""))
            return None, view.refusal(frame(visit), tab if tab in TABS else "applications")
        return visit, form

    async def sign_out(request):
        refused = guard(request)
        if refused:
            return refused
        found = sessions.find(request.cookies.get(COOKIE))
        if found is not None:  # signing out is the one change a forged form could make, and SameSite=Strict stops that too
            sessions.close(found[0])
            connections.forget(found[0])
        return RedirectResponse(PREFIX, status_code=303, headers={"Cache-Control": "no-store", "Set-Cookie": clear_cookie()})

    async def change(request):
        visit, form = await posted(request)
        if visit is None:
            return form
        tab = str(form.get("tab", ""))
        state = await run_in_threadpool(read, config.path)
        apps = registry()
        ctx = context(visit, state)
        if tab not in TABS:
            return view.notice(ctx, "applications", "red", "Unknown tab.", "Open the page again.", PREFIX, 400)
        app = subject(tab, form)
        if state.config is None and tab != "advanced":
            return view.tab_page(ctx, tab, app, status=409)
        result = await run_in_threadpool(prepare, visit, state, apps, tab, app, form)
        if isinstance(result, dict):  # problems: the tab again, with the submitted values and each problem next to its field
            return view.tab_page(ctx, tab, app, form, result, 400)
        old, new, change_ = result
        if change_ is None:
            back = back_to(tab, app)
            if tab == "connections":
                back += "&step=" + ("google" if form.get("form") == "destination" else "github")
            return view.nothing_page(ctx, tab, back)
        if old is not None:
            lines_ = unified(old, new)
        else:  # the file on disk did not load: compare its text
            lines_ = list(difflib.unified_diff(state.text.splitlines(), dump(masked(new)).splitlines(), "configuration now", "after this change", lineterm=""))
        return view.review_page(ctx, tab, change_.back, change_, pending.add(change_), old, new, lines_, PENDING_SECONDS // 60)

    def prepare(visit, state, apps, tab, app, form):
        """The validated change a form asks for: (old, new, Change or None) on success, else the problems by key."""
        which = str(form.get("form", "main"))
        found, token, ends = {}, None, []
        if tab == "advanced":
            try:
                candidate = parse(str(form.get("text", "")))
            except (yaml.YAMLError, ValueError) as exc:
                return {"": yaml_problem(exc) if isinstance(exc, yaml.YAMLError) else str(exc)}
        else:
            candidate = copy.deepcopy(state.data)
            if tab == "connections":
                try:
                    change_connections(candidate, state.config, form, which, connections, visit.session_id)
                except (ValueError, httpx.HTTPError, KeyError, OSError, jwt.PyJWTError, subprocess.SubprocessError) as exc:
                    return {"": connection_error(exc)}
                outcome = None
            else:
                builder = {"applications": change_applications, "platform": change_platform, "people": change_people, "production": change_production}[tab]
                outcome = builder(candidate, state.config, apps, form, which, found)
            if outcome:
                token, ends = outcome
        new_config, invalid = validate(candidate, apps)  # even after a field that did not read, so every problem shows at once
        if found or invalid:
            return {**invalid, **found}
        old = resolved(state.config) if state.config is not None else None
        new = resolved(new_config)
        if old is not None and old == new:
            return old, new, None
        keys = changed_keys(old, new) if old is not None else ["the whole file"]
        back = back_to(tab, app)
        if tab == "connections":
            step = "github" if which == "repositories" else "google"
            back += "&step=" + step
        return old, new, Change(visit.session_id, tab, state.sha, HEADER + dump(new), keys, ends, token, time.time() + PENDING_SECONDS, back)

    async def confirm(request):
        visit, form = await posted(request)
        if visit is None:
            return form
        change_ = pending.take(visit.session_id, str(form.get("change", "")))
        if change_ is None:
            ctx = context(visit, await run_in_threadpool(read, config.path))
            return view.notice(ctx, "applications", "gray", "This change expired or was already written.", "Nothing was written now. Prepare it again.",
                               PREFIX, 410)
        outcome = await run_in_threadpool(write, visit, change_, client(request))
        if outcome is None:
            ctx = context(visit, await run_in_threadpool(read, config.path))
            return view.notice(ctx, change_.tab, "red", "The file changed after this change was prepared, so nothing was written.",
                               "Another save or a config-put changed it. Open the tab again and make the change on the new file.", change_.back, 409)
        if visit.principal.name in change_.ends:
            return view.signed_out_result(outcome, {"Set-Cookie": clear_cookie()})
        return view.result_page(context(visit, await run_in_threadpool(read, config.path)), change_.tab, change_.back, outcome)

    def write(visit, change_, host):
        """Write a confirmed change; None when the file moved on since it was prepared. Returns what happened, for the result page."""
        path = config.path
        with write_lock:
            raw = Path(path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != change_.base:
                return None
            kept = keep_history(path, raw)
            write_atomically(path, change_.text)
            config.mtime = None  # read it now, whatever the file system's clock resolution
            reloaded = config.refresh()
        for name in change_.ends:
            sessions.end(name)
        copied, failed = [], None
        try:
            copied = copy_to_bucket(config.current, [(path, "config.yaml")])
        except (OSError, subprocess.SubprocessError) as exc:
            failed = (getattr(exc, "stderr", None) or str(exc) or type(exc).__name__).strip()[:200]
        detail = ", ".join(change_.keys) + ("; the copy to the backup bucket failed" if failed else "")
        single = {key.split(".")[1] for key in change_.keys if key.startswith(("applications.", "application_settings."))}
        only_app = next(iter(single)) if len(single) == 1 and all(key.startswith(("applications.", "application_settings.")) for key in change_.keys) else None
        store.record_event("configuration", name=change_.tab, identity=visit.principal.name, client=host, application=only_app, outcome="ok", detail=detail)
        return {"keys": change_.keys, "kept": kept, "reloaded": reloaded, "notes": config.error, "copied": copied, "failed": failed, "token": change_.token}

    async def connection_action(request):
        visit, form = await posted(request)
        if visit is None:
            return form
        error, notice, redirect = None, None, None
        try:
            action = form.get("action")
            if action == "google-connect":
                redirect = connections.start_google(visit.session_id)
            elif action == "google-projects":
                await run_in_threadpool(connections.projects, visit.session_id, str(form.get("account", "")))
            elif action == "google-disconnect":
                key = str(form.get("account", ""))
                connections.account(visit.session_id, key)
                with connections.lock:
                    connections.accounts.pop(key, None)
                notice = "Account disconnected from this session. Saved destinations and running apps are unchanged."
            elif action == "github-refresh":
                await run_in_threadpool(connections.refresh_github, visit.session_id)
                notice = "GitHub access refreshed. Select repositories below and save your selection."
            elif action == "check-repository":
                state = await run_in_threadpool(read, config.path)
                if state.config is None or controller is None:
                    raise ValueError("Fix the controller configuration before checking an application.")
                await run_in_threadpool(connections.review_repository, controller, visit.principal, visit.session_id, form, state.sha)
            elif action == "start-application":
                state = await run_in_threadpool(read, config.path)
                if state.config is None or controller is None:
                    raise ValueError("Fix the controller configuration before starting an application.")
                result = await run_in_threadpool(connections.start_application, controller, visit.principal, visit.session_id,
                                                str(form.get("review", "")), state.sha)
                return RedirectResponse(f"{PREFIX}?" + urlencode({"tab": "deployments", "onboarding": result["application"]}), status_code=303)
            else:
                raise ValueError("Choose a connection action from this page.")
        except (ValueError, httpx.HTTPError, KeyError, OSError, jwt.PyJWTError, subprocess.SubprocessError) as exc:
            error = connection_error(exc)
        if redirect:
            return RedirectResponse(redirect, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                    "Set-Cookie": callback_cookie(f"{visit.session_id}.{sessions.sign(visit.session_id)}", 600)})
        ctx = context(visit, await run_in_threadpool(read, config.path))
        ctx.connections.update(error=error)
        if notice:
            ctx.connections["notice"] = notice
        return view.tab_page(ctx, "connections", submitted=form, status=400 if error else 200)

    async def google_callback(request):
        refused = guard(request)
        if refused:
            return refused
        # The normal session cookie is Strict. Only this short-lived Lax cookie crosses the provider redirect.
        signed = sessions.find(request.cookies.get(google_cookie))
        principal = principal_for(config, signed[1], signed[2]) if signed else None
        headers = {"Set-Cookie": callback_cookie(), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if not signed or principal is None or not qualifies(principal):
            return view.message_page("Sign in again", "Sign in to this app, then reconnect your Google account.", 401, headers)
        try:
            key = await run_in_threadpool(connections.finish_google, signed[0], request.query_params.get("state", ""),
                                          request.query_params.get("code", ""), request.query_params.get("error"))
        except (ValueError, httpx.HTTPError, KeyError, OSError, jwt.PyJWTError, subprocess.SubprocessError) as exc:
            from .connections_page import problem
            return view.message_page("Google connection did not finish", problem(connection_error(exc))
                                     + f'<p><a href="{PREFIX}?tab=connections&step=google">Back to account selection</a></p>', 400, headers)
        store.record_event("connection", name="google", identity=principal.name, outcome="connected")
        note = ""
        try:
            await run_in_threadpool(connections.projects, signed[0], key)
        except (ValueError, httpx.HTTPError, KeyError, OSError) as exc:
            from .connections_page import problem
            note = '<p>Account connected, but the project list did not load. Try refreshing it on the next screen.</p>' + problem(connection_error(exc))
        # A direct provider -> callback -> settings redirect can suppress the Strict session cookie for the whole
        # redirect chain. A same-site link on this page reliably resumes the original session without weakening it.
        return view.message_page("Google account connected", note + f'<p><a class="btn primary" href="{PREFIX}?tab=connections&step=google">Next: choose a project</a></p>',
                                 200, headers)

    return [Route(PREFIX, show, methods=["GET"]),
            Route(f"{PREFIX}/deployment/status", deployment_status, methods=["GET"]),
            Route(f"{PREFIX}/deployment/report", deployment_status, methods=["GET"]),
            Route(f"{PREFIX}/deployment/script.js", deployment_script, methods=["GET"]),
            Route(f"{PREFIX}/connections/action", connection_action, methods=["POST"]),
            Route(f"{PREFIX}/connections/google/callback", google_callback, methods=["GET"]), Route(f"{PREFIX}/sign-in", sign_in, methods=["POST"]),
            Route(f"{PREFIX}/sign-out", sign_out, methods=["POST"]), Route(f"{PREFIX}/change", change, methods=["POST"]),
            Route(f"{PREFIX}/confirm", confirm, methods=["POST"])]


def subject(tab, form):
    """The application a form of the Applications tab was about: its danger forms name it, its main form carries its block."""
    if tab != "applications":
        return None
    return str(form.get("application") or next(iter(form.getlist("blocks")), "")) or None


def back_to(tab, app):
    return view.app_link(app) if tab == "applications" and app else f"{PREFIX}?tab={tab}"


def last_written(store, path):
    """Who wrote the file last and when: the newest configuration event, else the newest file in history/."""
    event = store.newest_event("configuration")
    if event is not None:
        return {"at": event["at"], "identity": event["identity"]}
    kept = sorted((Path(path).parent / "history").glob("config-*.yaml")) if path else []
    return {"at": kept[-1].stat().st_mtime, "identity": None} if kept else None


# ---- what each tab's forms change in the file

def danger_word(form, key, word, found):
    if str(form.get("confirm", "")).strip() != word:
        found[key] = f"type {word} exactly to confirm"
        return False
    return True


def change_applications(candidate, config, registry, form, which, found):
    if which in ("pause", "provenance"):
        app_id = str(form.get("application", ""))
        field = "paused" if which == "pause" else "provenance"
        if app_id not in config.applications and app_id not in registry:
            found[""] = f"{app_id} is not a registered application"
            return None
        if not danger_word(form, f"danger.{app_id}.{field}", app_id, found):
            return None
        value = choice(form, "value", {"true": True, "false": False} if field == "paused" else {"required": "required", "unavailable": "unavailable"}, found)
        set_application(candidate, config, registry, app_id, {field: value})
        return None
    for app_id in form.getlist("blocks"):
        if app_id not in config.applications and app_id not in registry:
            found[""] = f"{app_id} is no longer registered; open the tab again"
            continue
        prefix = f"applications.{app_id}" if app_id in config.applications else f"application_settings.{app_id}"
        set_application(candidate, config, registry, app_id, application_values(form, prefix, found))
    return None


def set_application(candidate, config, registry, app_id, values):
    if app_id in config.applications:
        entry, current = candidate["applications"][app_id], config.applications[app_id]
        for field, value in values.items():
            put(entry, field, value, getattr(current, field), field_default(Application, field))
        return
    base, current = registry[app_id], effective(config, registry, app_id)
    for field, value in values.items():
        override(candidate, app_id, field, value, getattr(current, field), getattr(base, field))


def amounts(form, key, found):
    """`project: USD` lines of a text area as a mapping; a line that is not one is named next to the field."""
    found_amounts = {}
    for line in lines(form.get(key)):
        project, _, amount = line.replace("=", ":").partition(":")
        value = number({"amount": amount}, "amount", found, whole=False)
        if not amount.strip() or value is INVALID or value is None:
            found.pop("amount", None)
            found[key] = f"{line!r} is not project: amount"
            continue
        found_amounts[project.strip()] = value
    return found_amounts


def change_platform(candidate, config, registry, form, which, found):
    if which == "kill":
        if danger_word(form, "danger.paused", KILL_SWITCH, found):
            put(candidate, "paused", choice(form, "value", {"true": True, "false": False}, found), config.paused, False)
        return None
    for field in ("auto_deploy_interval_seconds", "backup_keep_days"):
        put(candidate, field, number(form, field, found, required=True), getattr(config, field), field_default(Config, field))
    webhook = str(form.get("notify_webhook_url", "")).strip()
    if webhook:
        put(candidate, "notify_webhook_url", webhook, config.notify_webhook_url, None)
    elif "notify_webhook_url.remove" in form:
        put(candidate, "notify_webhook_url", None, config.notify_webhook_url, None)
    put(candidate, "cloud_run_spend_caps", amounts(form, "cloud_run_spend_caps", found), config.cloud_run_spend_caps, {})
    # After a release and between Mondays (docs/ROADMAP.md D60, 24.2, 24.3): the digest's hour (empty turns it off), each
    # project's budget as created in Cloud Billing, and whether Google's anomaly detection is on.
    if "digest_hour_utc" in form:
        put(candidate, "digest_hour_utc", number(form, "digest_hour_utc", found), config.digest_hour_utc, field_default(Config, "digest_hour_utc"))
    if "monthly_budgets_usd" in form:
        put(candidate, "monthly_budgets_usd", amounts(form, "monthly_budgets_usd", found), config.monthly_budgets_usd, {})
        put(candidate, "cost_anomaly_detection", "cost_anomaly_detection" in form, config.cost_anomaly_detection, False)
    if "platform_updates.on" not in form:
        if config.platform_updates is not None:
            candidate.pop("platform_updates", None)
        return None
    current = config.platform_updates
    block = candidate.get("platform_updates") if isinstance(candidate.get("platform_updates"), dict) else {}
    values = {"machine": "platform_updates.machine" in form, "workflows": "platform_updates.workflows" in form,
              "delay_minutes": number(form, "platform_updates.delay_minutes", found, required=True),
              "quiet_minutes": number(form, "platform_updates.quiet_minutes", found, required=True)}
    for field, value in values.items():
        default = field_default(PlatformUpdates, field)
        put(block, field, value, getattr(current, field) if current is not None else default, default)
    candidate["platform_updates"] = block
    return None


def change_people(candidate, config, registry, form, which, found):
    principals = candidate.get("principals") or []
    if which in ("rotate", "remove"):
        name = str(form.get("principal", ""))
        entry = next((p for p in principals if isinstance(p, dict) and p.get("name") == name), None)
        if entry is None:
            found[""] = f"{name} is not a principal any more; open the tab again"
            return None
        if not danger_word(form, f"danger.{name}.{which}", name, found):
            return None
        if which == "remove":
            candidate["principals"] = [p for p in principals if p is not entry]
            return None, [name]
        token = secrets.token_urlsafe(32)  # as `lab-deploy token` makes one
        entry["token_sha256"] = digest(token)
        return (name, token), [name]
    if which == "add":
        name = str(form.get("new.name", "")).strip()
        if not name:
            found["new.name"] = "is required"
            return None
        if any(p.name == name for p in config.principals):
            found["new.name"] = f"{name} is already a principal"
            return None
        token = secrets.token_urlsafe(32)
        entry = {"name": name, "token_sha256": digest(token), "applications": words(form.get("new.applications"))}
        put(entry, "can_deploy", "new.can_deploy" in form, None, False)
        put(entry, "may_merge_own_requests", "new.may_merge_own_requests" in form, None, True)
        candidate["principals"] = [*principals, entry]
        return (name, token), []
    for name in form.getlist("people"):
        entry = next((p for p in principals if isinstance(p, dict) and p.get("name") == name), None)
        current = next((p for p in config.principals if p.name == name), None)
        if entry is None or current is None:
            found[""] = f"{name} is not a principal any more; open the tab again"
            continue
        prefix = f"principals.{name}"
        put(entry, "applications", words(form.get(f"{prefix}.applications")), current.applications, None)
        put(entry, "can_deploy", f"{prefix}.can_deploy" in form, current.can_deploy, False)
        put(entry, "may_merge_own_requests", f"{prefix}.may_merge_own_requests" in form, current.may_merge_own_requests, True)
    return None


def change_production(candidate, config, registry, form, which, found):
    current = config.production
    if current is None or not isinstance(candidate.get("production"), dict):
        found[""] = "There is no production block; add one on the Advanced tab"
        return None
    block = candidate["production"]
    put(block, "reviewers", lines(form.get("production.reviewers")), current.reviewers, None)
    rollout = []
    for step in words(form.get("production.rollout")):
        try:
            rollout.append(int(step))
        except ValueError:
            found["production.rollout"] = f"{step!r} is not a whole number"
    put(block, "rollout", rollout, current.rollout, field_default(ProductionSettings, "rollout"))
    put(block, "bake_seconds", number(form, "production.bake_seconds", found, required=True), current.bake_seconds, field_default(ProductionSettings, "bake_seconds"))
    return None


def connection_error(error):
    """Provider error bodies, request URLs and credentials must never appear in a settings page."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 401:
            return "The provider rejected this connection. Reconnect your account or ask the administrator to renew the server credential."
        if status == 403:
            return "Access was refused. Check account permissions, enabled Google APIs and the GitHub App grant, then refresh."
        if status == 404:
            return "A required resource was not found. Check repository access and ask the administrator to finish the Fast Deploy foundation in the selected project."
        return f"The provider could not complete the request (HTTP {status}). Try again shortly."
    if isinstance(error, httpx.HTTPError):
        return "The provider could not be reached. Your saved settings are unchanged; try again shortly."
    if isinstance(error, OSError | KeyError | jwt.PyJWTError | subprocess.SubprocessError):
        return "The connection could not be read. Ask the server administrator to check its provider setup."
    return str(error)


def change_connections(candidate, config, form, which, connections, session):
    preferences = candidate.setdefault("connections", {})
    if which == "google-setup":
        preferences["google_client_id"] = str(form.get("connections.google_client_id", "")).strip() or None
    elif which == "github-setup":
        candidate["github_app"] = {
            "app_id": str(form.get("github_app.app_id", "")).strip(),
            "private_key_secret": str(form.get("github_app.private_key_secret", "")).strip(),
        }
    elif which == "repositories":
        # A forged or stale checkbox cannot grant access to a repository the credential does not expose.
        catalog = connections.refresh_github(session)
        available = {repo["name"].lower() for repo in catalog["repositories"]}
        selected = {str(repo).lower() for repo in form.getlist("repositories")}
        if selected - available:
            raise ValueError("Some selected repositories are no longer visible on GitHub. Refresh access and select again.")
        if config.platform and config.platform.repository.lower() not in available:
            raise ValueError("The platform repository must be accessible on GitHub. Install the App on it, then refresh.")
        preferences["repositories"] = sorted(selected)
    elif which == "destination":
        project = connections.destination(session, str(form.get("account", "")), str(form.get("project", "")))
        if config.production and config.production.project_id == project["id"]:
            raise ValueError("This is the production project. Choose a different project for staging.")
        current = config.onboarding
        if current and current.project_id == project["id"]:
            candidate["onboarding"]["project_number"] = project["number"]
        else:
            # Never carry a previous project's buckets, image, billing table or proxy identity into a new project.
            # Shared infrastructure workflows must point at the same foundation before new applications can use it.
            if config.platform is None:
                raise ValueError("Set the platform repository before choosing a deployment destination.")
            for key, expected in (
                ("INFRA_APPLY_SERVICE_ACCOUNT", f'lab-deploy-infra-apply@{project["id"]}.iam.gserviceaccount.com'),
                ("INFRA_PLAN_SERVICE_ACCOUNT", f'lab-deploy-infra-plan@{project["id"]}.iam.gserviceaccount.com'),
                ("INFRA_APPLY_WIF_PROVIDER", f'projects/{project["number"]}/locations/global/workloadIdentityPools/lab-deploy-platform/providers/github-apply'),
                ("INFRA_PLAN_WIF_PROVIDER", f'projects/{project["number"]}/locations/global/workloadIdentityPools/lab-deploy-platform/providers/github-plan'),
            ):
                try:
                    result = connections.github.request("GET", f"/repos/{config.platform.repository}/actions/variables/{key}").json()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 403:
                        raise ValueError("The GitHub connection needs Variables: read on the platform repository to verify a new destination. "
                                         "Have the administrator grant it on GitHub, then refresh access.") from None
                    raise
                if result.get("value") != expected:
                    raise ValueError("The platform’s infrastructure workflow still points to another project. Ask the administrator to configure "
                                     "its foundation and GitHub infrastructure variables for this project, then try again.")
            candidate["onboarding"] = {"project_id": project["id"], "project_number": project["number"], "region": "us-central1"}
            if current:
                for key in ("auto_merge", "default_viewers", "own_projects"):
                    candidate["onboarding"][key] = getattr(current, key)
                candidate["onboarding"]["own_projects"] = {name: own.model_dump() for name, own in current.own_projects.items()}
        preferences["google_account"] = project["email"]
    else:
        raise ValueError("Choose a setup form from Connections.")
