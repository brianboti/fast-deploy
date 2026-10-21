"""The settings page (https://HOST/settings): the controller's own configuration file, changed in the browser.

Server-rendered HTML without JavaScript, behind the same headers as the OAuth sign-in page. The operator signs in with an
operator token (the same lookup and failed-login budget as /login); only an identity with applications: ["*"] and
can_deploy may open it. Every change goes the same way: the form becomes a candidate configuration, the candidate is
validated exactly as the controller validates its file (the loader, Config, and the overlay of application_settings on
the applications onboarding registered), the unified diff is shown, and only the confirm step writes the file: atomically,
mode 0600, the previous version kept under history/, a copy in the backup bucket as the nightly backup makes, one audit
event "configuration" naming the keys (never their values). LiveConfig then reloads the file by itself.

What it deliberately does not change: contracts (platform.yaml) and infrastructure, which change only through pull requests
(docs/ROADMAP.md D32), and secrets, which the operator sets with gcloud (D5). The page links to where those live.
"""
import copy
import dataclasses
import datetime
import difflib
import hashlib
import hmac
import html
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pydantic
import yaml
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .config import Application, Config, PlatformUpdates, ProductionSettings, with_settings
from .controller import copy_to_bucket
from .infra import DEFAULT_SETTINGS, SETTING_RANGES
from .models import UniqueLoader
from .oauth import PAGE_HEADERS, find_principal, principal_for

PREFIX = "/settings"
COOKIE = "__Host-fast-deploy-settings"  # __Host-: only over HTTPS, for this host and path / only
PENDING_SECONDS = 600  # a reviewed change must be confirmed within ten minutes
HISTORY_KEEP = 20
TABS = {"applications": "Applications", "platform": "Platform", "people": "People", "production": "Production", "advanced": "Advanced"}
KILL_SWITCH = "all applications"  # what the operator types to turn the controller-wide pause on or off
HEADER = "# The controller's configuration, last written by its settings page (/settings); earlier versions are in history/ beside it.\n"
GITHUB = "https://github.com/"


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
        infra = re.match(r"infra\.(\w+) ", message)
        if infra and len(loc) == 2 and loc[0] in ("applications", "application_settings"):
            loc += ["infra", infra.group(1)]
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


def changed_keys(old, new, prefix=""):
    """The dotted keys whose values differ; principals by name, a whole application by its id. Names only: values never
    leave this function."""
    whole_application = re.fullmatch(r"applications\.[^.]+", prefix) and (old is None or new is None)
    if not whole_application and (isinstance(old, dict) or isinstance(new, dict)) and isinstance(old or {}, dict) and isinstance(new or {}, dict):
        old, new = old or {}, new or {}
        keys = []
        for key in [*old, *(k for k in new if k not in old)]:
            if old.get(key, KeyError) != new.get(key, KeyError):
                keys += changed_keys(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else str(key))
        return keys
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


def masked(data):
    """A copy for showing on screen: token hashes and the webhook address shortened to a fingerprint."""
    shown = copy.deepcopy(data)
    for principal in shown.get("principals") or []:
        if isinstance(principal, dict) and isinstance(principal.get("token_sha256"), str):
            principal["token_sha256"] = f"(hash {principal['token_sha256'][:8]}...)"
    if isinstance(shown.get("notify_webhook_url"), str):
        shown["notify_webhook_url"] = f"(hidden, fingerprint {digest(shown['notify_webhook_url'])[:8]})"
    return shown


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


# ---- the page

STYLE = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#222;line-height:1.4}
h1{margin-bottom:.25rem}h2{margin-top:2rem}nav a{margin-right:1rem}nav a[aria-current]{font-weight:bold;text-decoration:none;color:#222}
fieldset{border:1px solid #ccc;margin:1rem 0;padding:.5rem 1rem}legend{font-weight:bold}label{display:block;margin:.5rem 0 .15rem}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;box-sizing:border-box;padding:.35rem;font-size:1rem}
input[type=checkbox]{margin-right:.4rem}label.inline{display:block;margin:.6rem 0 .2rem}.hint{display:block;color:#555;font-size:.9rem;margin-top:.15rem}
button{padding:.45rem 1rem;font-size:1rem;margin-top:.75rem}.error{color:#b00020}.notice{background:#f4f4f4;padding:.5rem 1rem;border-left:4px solid #888}
.danger{border-color:#b00020}.danger legend{color:#b00020}.danger form{margin-bottom:1rem}pre .hint{display:inline}
table{border-collapse:collapse;width:100%;font-size:.9rem}td,th{border:1px solid #ddd;padding:.3rem;text-align:left;vertical-align:top}
pre{background:#f7f7f7;padding:.75rem;overflow-x:auto}.add{color:#0a6b0a}.del{color:#b00020}.token{font-family:monospace;font-size:1.1rem;background:#fff3c4;padding:.5rem}
header .who form{display:inline;margin-left:.5rem}header button{margin:0;padding:.2rem .6rem}"""


def document(title, body, status=200, headers=None):
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>")
    return HTMLResponse(page, status_code=status, headers={**PAGE_HEADERS, **(headers or {})})


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


class Problems:
    """Validation errors by key; each field claims its own (and those under it), the rest are shown at the top of the form."""

    def __init__(self, found=None):
        self.found = dict(found or {})

    def at(self, key):
        claimed = [self.found.pop(k) for k in list(self.found) if k == key or k.startswith(key + ".")]
        return "".join(f'<span class="error">{esc(message)}</span><br>' for message in claimed)

    def rest(self):
        items = "".join(f"<li>{esc(key + ': ' if key else '')}{esc(message)}</li>" for key, message in self.found.items())
        self.found = {}
        return f'<div class="error"><p>Nothing was changed:</p><ul>{items}</ul></div>' if items else ""


class Form:
    """Values for one form's inputs: what was submitted when that form comes back with errors, else the file's."""

    def __init__(self, submitted=None):
        self.submitted = submitted

    @staticmethod
    def picker(submitted):
        """Form(...) for a form by its name: the submitted values for the one that was sent, the file's for the others."""
        sent = submitted.get("form") if submitted is not None else None
        return lambda name: Form(submitted if sent == name else None)

    def value(self, key, current):
        return self.submitted.get(key, "") if self.submitted is not None else ("" if current is None else current)

    def checked(self, key, current):
        return (key in self.submitted) if self.submitted is not None else bool(current)


def text_input(form, bag, key, label, current, hint="", kind="text", attributes=""):
    return (f'<label for="{esc(key)}">{esc(label)}</label>{bag.at(key)}'
            f'<input id="{esc(key)}" name="{esc(key)}" type="{kind}" value="{esc(form.value(key, current))}" {attributes}>'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else ""))


def number_input(form, bag, key, label, current, low=None, high=None, hint="", whole=True, required=False):
    attributes = " ".join(part for part in (f'min="{low}"' if low is not None else "", f'max="{high}"' if high is not None else "",
                                            'step="1"' if whole else 'step="any"', "required" if required else "") if part)
    return text_input(form, bag, key, label, current, hint, "number", attributes)


def area(form, bag, key, label, current, hint="", rows=3):
    value = form.value(key, current)
    return (f'<label for="{esc(key)}">{esc(label)}</label>{bag.at(key)}<textarea id="{esc(key)}" name="{esc(key)}" rows="{rows}">{esc(value)}</textarea>'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else ""))


def checkbox(form, bag, key, label, current, hint=""):
    checked = " checked" if form.checked(key, current) else ""
    return (f'{bag.at(key)}<label class="inline"><input type="checkbox" name="{esc(key)}" value="on"{checked}>{esc(label)}</label>'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else ""))


def select(form, bag, key, label, current, options, hint=""):
    chosen = form.value(key, current)
    rendered = "".join(f'<option value="{esc(value)}"{" selected" if str(chosen) == value else ""}>{esc(text)}</option>' for value, text in options)
    return (f'<label for="{esc(key)}">{esc(label)}</label>{bag.at(key)}<select id="{esc(key)}" name="{esc(key)}">{rendered}</select>'
            + (f'<span class="hint">{esc(hint)}</span>' if hint else ""))


def tristate(value):
    return "" if value is None else ("true" if value else "false")


def form_open(visit, tab, which, action=f"{PREFIX}/change"):
    return (f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{esc(visit.csrf)}">'
            f'<input type="hidden" name="tab" value="{esc(tab)}"><input type="hidden" name="form" value="{esc(which)}">')


def confirm_box(key, word, bag):
    return (f'<label for="{esc(key)}">Type <strong>{esc(word)}</strong> to confirm</label>{bag.at(key)}'
            f'<input id="{esc(key)}" name="confirm" type="text" autocomplete="off" required>')


def links_panel(config, registry, productions):
    """What the page does not change, and where each of those lives."""
    platform = config.platform
    applications = {**{app_id: app for app_id, app in registry.items() if app_id not in config.applications}, **config.applications}

    def platform_link(path, text=None, tree=False):
        if platform is None:
            return f"<code>{esc(path)}</code> in the platform repository"
        return f'<a href="{esc(GITHUB + platform.repository + ("/tree/" if tree else "/blob/") + platform.branch + "/" + path)}">{esc(text or path)}</a>'

    rows = []
    for app_id in sorted(applications):
        app = applications[app_id]
        service = app.target.service
        if app.central:
            contract = platform_link(f"apps/{service}/platform.yaml")
        else:
            path = app.repo_path("platform.yaml")
            contract = f'<a href="{esc(f"{GITHUB}{app.repository}/blob/{app.branch}/{path}")}">{esc(app.repository)}/{esc(path)}</a>'
        roots = platform_link(f"infra/gcp/apps/{service}", tree=True)
        environments = esc(app.environment)
        if app_id in productions:
            roots += "<br>" + platform_link(f"infra/gcp/apps-production/{service}", tree=True)
            environments += f"<br>{esc((productions[app_id] or {}).get('environment') or 'production-' + service)}"
        environment = f'<a href="{esc(f"{GITHUB}{app.pipeline_repo}/settings/environments")}">{environments}</a>'
        secrets_link = f'<a href="{esc("https://console.cloud.google.com/security/secret-manager?project=" + app.target.project_id)}">{esc(service)}-NAME in {esc(app.target.project_id)}</a>'
        rows.append(f"<tr><td>{esc(app_id)}</td><td>{contract}</td><td>{roots}</td><td>{environment}</td><td>{secrets_link}</td></tr>")
    table = ("<table><tr><th>Application</th><th>Contract (pull request)</th><th>Terraform root (pull request)</th><th>GitHub environment</th>"
             f"<th>Secrets (gcloud)</th></tr>{''.join(rows)}</table>") if rows else "<p>No application is registered yet.</p>"
    return (f'<section class="notice"><p><strong>What this page does not change.</strong> Each application\'s contract and its '
            "infrastructure change only through a reviewed pull request (docs/ROADMAP.md D32); secrets are set by the operator with gcloud, never "
            f"through the platform (D5); Google's native spend cap is made with {platform_link('scripts/spend-cap.sh')} (docs/SETUP.md 7i) and only "
            "recorded on the Platform tab.</p><details><summary>Where each application's contract, infrastructure and secrets live</summary>"
            f"{table}</details></section>")


def applications_tab(visit, state, registry, pick, bag):
    config, form = state.config, pick("main")
    onboarding = "on" if (config.onboarding.auto_merge if config.onboarding else True) else "off"
    blocks, danger = [], []
    ids = [*config.applications, *(app_id for app_id in registry if app_id not in config.applications)]
    for app_id in ids:
        in_file = app_id in config.applications
        prefix = f"applications.{app_id}" if in_file else f"application_settings.{app_id}"
        app = config.applications[app_id] if in_file else effective(config, registry, app_id)
        where = "an entry of this file" if in_file else "registered by onboarding; switches go under application_settings"
        infra = []
        for key, default in DEFAULT_SETTINGS.items():
            name = f"{prefix}.infra.{key}"
            if isinstance(default, bool):
                infra.append(select(form, bag, name, key, tristate(app.infra.get(key)),
                                    [("", f"default ({'on' if default else 'off'})"), ("true", "on"), ("false", "off")]))
            else:
                low, high = SETTING_RANGES.get(key, (None, None))
                infra.append(number_input(form, bag, name, key, app.infra.get(key), low, high, f"empty: the default, {default}; {low} to {high}"))
        fields = "".join([
            number_input(form, bag, f"{prefix}.cost_ceiling_usd", "Cost ceiling, USD a month", app.cost_ceiling_usd, whole=False,
                         hint="refuse plans whose worst case is higher; empty: no ceiling"),
            checkbox(form, bag, f"{prefix}.auto_deploy", "Release the tip of main on its own (auto_deploy)", app.auto_deploy),
            select(form, bag, f"{prefix}.auto_merge", "Merge its own pull requests (auto_merge)", tristate(app.auto_merge),
                   [("", f"follow onboarding.auto_merge ({onboarding})"), ("true", "yes"), ("false", "no")]),
            number_input(form, bag, f"{prefix}.bake_seconds", "Bake after promotion, seconds", app.bake_seconds, 0, 900, "0 turns the bake off", required=True),
            area(form, bag, f"{prefix}.viewers", "Viewers, one per line", "\n".join(app.viewers), "user:EMAIL, group:EMAIL or domain:DOMAIN"),
            "<details><summary>Infrastructure settings (they reach its Terraform root with the next propose_infrastructure)</summary>", *infra, "</details>",
        ])
        blocks.append(f'<fieldset><legend>{esc(app_id)}</legend><input type="hidden" name="blocks" value="{esc(app_id)}">'
                      f'<p class="hint">{esc(app.repository)}, {where}</p>{bag.at(prefix)}{fields}</fieldset>')  # fields first claimed theirs
        state_words = "paused" if app.paused else "running"
        pause = (form_open(visit, "applications", "pause") + f'<input type="hidden" name="application" value="{esc(app_id)}">'
                 f'<input type="hidden" name="value" value="{"false" if app.paused else "true"}"><p>{esc(app_id)} is <strong>{state_words}</strong>.</p>'
                 + confirm_box(f"danger.{app_id}.paused", app_id, bag)
                 + f'<button type="submit">{"Resume" if app.paused else "Pause"} {esc(app_id)}</button></form>')
        relaxed = app.provenance == "unavailable"
        provenance = (form_open(visit, "applications", "provenance") + f'<input type="hidden" name="application" value="{esc(app_id)}">'
                      f'<input type="hidden" name="value" value="{"required" if relaxed else "unavailable"}"><p>Provenance for {esc(app_id)}: '
                      f'<strong>{esc(app.provenance)}</strong>.</p>' + confirm_box(f"danger.{app_id}.provenance", app_id, bag)
                      + f'<button type="submit">{"Require signed provenance again" if relaxed else "Release without signed provenance (unavailable)"}</button></form>')
        danger.append(f"<h3>{esc(app_id)}</h3>{pause}{provenance}")
    if not ids:
        return "<p>No application is registered yet: onboarding adds them, or the Advanced tab.</p>"
    main = form_open(visit, "applications", "main") + "".join(blocks) + '<button type="submit">Review the change</button></form>'
    return (f"<h2>Applications</h2>{bag.rest()}{main}<fieldset class=\"danger\"><legend>Danger zone</legend><p>Pausing stops new plans and releases of "
            "one application (reads keep working). Provenance unavailable lets it release without a signed build record: an exception for staging or "
            f"an emergency.</p>{''.join(danger)}</fieldset>")


def effective(config, registry, app_id):
    """An onboarded application as the controller sees it; the registered entry alone when its settings do not apply."""
    try:
        return with_settings(registry[app_id], config.application_settings.get(app_id))
    except pydantic.ValidationError:
        return registry[app_id]


def platform_tab(visit, state, pick, bag):
    config, form = state.config, pick("main")
    updates = config.platform_updates or PlatformUpdates()
    caps = "\n".join(f"{project}: {amount}" for project, amount in config.cloud_run_spend_caps.items())
    webhook = "set" if config.notify_webhook_url else "not set"
    fields = "".join([
        number_input(form, bag, "auto_deploy_interval_seconds", "Auto-deploy check, every N seconds", config.auto_deploy_interval_seconds, 60, 3600, required=True),
        number_input(form, bag, "backup_keep_days", "Keep nightly backups for N days", config.backup_keep_days, 1, 365, required=True),
        text_input(Form(), bag, "notify_webhook_url", f"Watchdog webhook (Slack or Discord), now {webhook}", "",
                   "type a new address to replace it; it is never shown", "password", 'autocomplete="off"'),
        checkbox(form, bag, "notify_webhook_url.remove", "Remove the webhook", False),
        area(form, bag, "cloud_run_spend_caps", "Native Cloud Run spend caps you created, one per line: project: USD a month", caps,
             "a record for plans and the status; the cap itself lives in Cloud Billing"),
        "<fieldset><legend>After a release (platform_updates)</legend>",
        checkbox(form, bag, "platform_updates.on", "On: the machine installs main by itself and applications are re-pinned", config.platform_updates is not None),
        checkbox(form, bag, "platform_updates.machine", "The machine installs main", updates.machine),
        checkbox(form, bag, "platform_updates.workflows", "Applications get the re-pin pull request", updates.workflows),
        number_input(form, bag, "platform_updates.delay_minutes", "Wait this many minutes after the checks pass", updates.delay_minutes, 0, 10_080, required=True),
        number_input(form, bag, "platform_updates.quiet_minutes", "Restart only after this many quiet minutes", updates.quiet_minutes, 0, 240, required=True),
        bag.at("platform_updates"), "</fieldset>",
    ])
    main = form_open(visit, "platform", "main") + fields + '<button type="submit">Review the change</button></form>'
    paused = config.paused
    kill = (form_open(visit, "platform", "kill") + f'<input type="hidden" name="value" value="{"false" if paused else "true"}">'
            f"<p>The controller is <strong>{'paused for every application' if paused else 'not paused'}</strong>.</p>"
            + confirm_box("danger.paused", KILL_SWITCH, bag)
            + f'<button type="submit">{"Resume every application" if paused else "Pause every application (the kill switch)"}</button></form>')
    return (f"<h2>Platform</h2>{bag.rest()}{main}<fieldset class=\"danger\"><legend>Danger zone</legend><p>The kill switch stops new plans and releases "
            f"of every application; reads keep working.</p>{kill}</fieldset>")


def people_tab(visit, state, pick, bag):
    config, form = state.config, pick("main")
    rows = []
    for principal in config.principals:
        prefix = f"principals.{principal.name}"
        fields = "".join([
            text_input(form, bag, f"{prefix}.applications", "Applications (ids, or * for every one)", ", ".join(principal.applications)),
            checkbox(form, bag, f"{prefix}.can_deploy", "May deploy (can_deploy)", principal.can_deploy),
            checkbox(form, bag, f"{prefix}.may_merge_own_requests", "Its own requests merge without a second operator", principal.may_merge_own_requests)])
        rows.append(f'<fieldset><legend>{esc(principal.name)}</legend><input type="hidden" name="people" value="{esc(principal.name)}">'
                    f"{bag.at(prefix)}{fields}</fieldset>")
    main = form_open(visit, "people", "main") + "".join(rows) + '<button type="submit">Review the change</button></form>'
    blank = pick("add")
    add = (form_open(visit, "people", "add") + "<fieldset><legend>A new person (a new token)</legend>"
           + text_input(blank, bag, "new.name", "Name", "", "how events name them")
           + text_input(blank, bag, "new.applications", "Applications (ids, or *)", "")
           + checkbox(blank, bag, "new.can_deploy", "May deploy (can_deploy)", False)
           + checkbox(blank, bag, "new.may_merge_own_requests", "Its own requests merge without a second operator", True)
           + '<p class="hint">The token is made on this server and shown once, after you confirm; only its SHA-256 is kept.</p>'
           + '<button type="submit">Review the change</button></fieldset></form>')
    danger = []
    for principal in config.principals:
        danger.append(f"<h3>{esc(principal.name)}</h3>")
        for which, words_ in (("rotate", f"Rotate the token of {principal.name}"), ("remove", f"Remove {principal.name}")):
            danger.append(form_open(visit, "people", which) + f'<input type="hidden" name="principal" value="{esc(principal.name)}">'
                          + confirm_box(f"danger.{principal.name}.{which}", principal.name, bag) + f'<button type="submit">{esc(words_)}</button></form>')
    return (f"<h2>People</h2>{bag.rest()}{main}{add}<fieldset class=\"danger\"><legend>Danger zone</legend><p>Rotating a token or removing a person ends "
            f"every session signed in with the old token: Claude's and this page's.</p>{''.join(danger)}</fieldset>")


def production_tab(visit, state, pick, bag):
    production, form = state.config.production, pick("main")
    if production is None:
        return ("<h2>Production</h2><p>This controller has no production block. docs/SETUP.md 7n says how to add one; the first time, "
                "add it on the Advanced tab.</p>")
    fields = "".join([
        area(form, bag, "production.reviewers", "Reviewers who approve every promotion, one GitHub user name per line", "\n".join(production.reviewers), "one to six"),
        text_input(form, bag, "production.rollout", "Rollout: the share of traffic at each step", ", ".join(str(step) for step in production.rollout),
                   "a rising list that ends at 100, such as 10, 50, 100"),
        number_input(form, bag, "production.bake_seconds", "Watch each step for this many seconds", production.bake_seconds, 0, 1800, required=True),
    ])
    return (f"<h2>Production</h2><p class=\"hint\">Project {esc(production.project_id)}. The rollout and the watch apply to the next promotion. "
            "Reviewers reach each production-&lt;service&gt; environment on GitHub when add_production runs: after changing them, ask for "
            "add_production again for each application (docs/RUNBOOK.md, Production).</p>"
            + bag.at("production") + bag.rest() + form_open(visit, "production", "main") + fields + '<button type="submit">Review the change</button></form>')


def advanced_tab(visit, state, pick, bag):
    form = pick("main")
    text = form.submitted.get("text", "") if form.submitted is not None else state.text
    return ("<h2>Advanced: the whole file</h2><p>The only place for <code>database</code>, <code>oauth</code> and <code>github_app</code>; "
            "those need a restart (<code>scripts/machine.sh restart</code>). The file is checked like the controller checks it. Comments and "
            "formatting are not kept: the page writes the file as the controller reads it, and keeps the previous one in history/.</p>"
            + bag.rest() + form_open(visit, "advanced", "main") + f'<textarea name="text" rows="30" spellcheck="false">{esc(text)}</textarea>'
            + '<button type="submit">Review the change</button></form>')


def tab_body(visit, state, registry, tab, submitted=None, found=None):
    """One tab's forms; after a refused change, the form that was sent keeps its values and each problem sits by its field."""
    bag, pick = Problems(found), Form.picker(submitted)
    if tab == "advanced":
        return advanced_tab(visit, state, pick, bag)
    if state.config is None:
        return (f'<p class="error">The file on disk does not load, so only the Advanced tab can change it: {esc(state.error)}</p>'
                "<p>Until it loads, the controller keeps the configuration it read last.</p>")
    if tab == "platform":
        return platform_tab(visit, state, pick, bag)
    if tab == "people":
        return people_tab(visit, state, pick, bag)
    if tab == "production":
        return production_tab(visit, state, pick, bag)
    return applications_tab(visit, state, registry, pick, bag)


def header(visit, tab):
    nav = "".join(f'<a href="{PREFIX}?tab={key}"{" aria-current=page" if key == tab else ""}>{label}</a>' for key, label in TABS.items())
    return (f'<header><h1>Fast Deploy settings</h1><div class="who">Signed in as <strong>{esc(visit.principal.name)}</strong>.'
            f'<form method="post" action="{PREFIX}/sign-out"><input type="hidden" name="csrf" value="{esc(visit.csrf)}">'
            f'<button type="submit">Sign out</button></form></div></header><nav>{nav}</nav>')


SIGN_IN = """<h1>Fast Deploy settings</h1><p>This page changes the controller's own configuration file. Sign in with an operator token
that may see every application and deploy.</p>__ERROR__<form method="post" action="/settings/sign-in">
<label for="token">Operator token</label><input id="token" name="token" type="password" autocomplete="off" required autofocus>
<button type="submit">Sign in</button></form><p class="hint">The token is checked on this server only; the page keeps a sign-in for at most
__MINUTES__ minutes, and a restart of the controller ends it.</p>"""


def diff_class(line):
    if line.startswith(("---", "+++", "@@")):
        return "hint"
    return "add" if line.startswith("+") else "del" if line.startswith("-") else ""


# ---- the routes

def routes(config, store, failures):
    """The settings page's routes. `config` is the controller's LiveConfig; `failures` the failed-login budget /login shares."""
    sessions, pending, write_lock = Sessions(store), Pending(), threading.Lock()

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

    def sign_in_page(error="", status=200, headers=None):
        minutes = config.settings_session_seconds // 60
        body = SIGN_IN.replace("__ERROR__", f'<p class="error">{esc(error)}</p>' if error else "").replace("__MINUTES__", str(minutes))
        return document("Fast Deploy settings: sign in", body, status, headers)

    def refusal(principal, status=403):
        return document("Fast Deploy settings", f"<h1>Fast Deploy settings</h1><p>{esc(principal.name)} may not change the controller's configuration: "
                        "this page needs an identity with applications: [\"*\"] and can_deploy. Ask the operator, or use scripts/machine.sh "
                        f'config-get and config-put.</p><form method="post" action="{PREFIX}/sign-out"><button type="submit">Sign out</button></form>',
                        status, {"Set-Cookie": clear_cookie()})

    def no_file():
        return document("Fast Deploy settings", "<h1>Fast Deploy settings</h1><p>This controller was started without a configuration file "
                        "(LAB_CONFIG), so there is nothing here to change.</p>", 404)

    def cookie(value):
        return f"{COOKIE}={value}; Max-Age={config.settings_session_seconds}; Path=/; Secure; HttpOnly; SameSite=Strict"

    def clear_cookie():
        return f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"

    def visitor(request):
        """(Visit, None) for a live sign-in, else (None, why it ended)."""
        found = sessions.find(request.cookies.get(COOKIE))
        if found is None:
            return None, ("Your sign-in ended: it expired, or the controller restarted. Sign in again." if request.cookies.get(COOKIE) else "")
        session_id, subject, credential = found
        principal = principal_for(config, subject, credential)
        if principal is None:
            sessions.close(session_id)
            return None, "Your sign-in ended: its token was rotated or its principal removed. Sign in again."
        return Visit(principal, session_id, sessions.sign("csrf:" + session_id)), None

    def guard(request):
        if not host_allowed(request):
            return document("Fast Deploy settings", "<p>This host is not one of the controller's (allowed_hosts).</p>", 400)
        if not getattr(config, "path", None):
            return no_file()
        return None

    def page(visit, tab, body, status=200, state=None):
        state = state or read(config.path)
        return document("Fast Deploy settings", header(visit, tab) + (links_panel(state.config, registry(), store.productions()) if state.config else "")
                        + f"<main>{body}</main>", status)

    async def show(request):
        refused = guard(request)
        if refused:
            return refused
        visit, ended = visitor(request)
        if visit is None:
            return sign_in_page(ended, headers={"Set-Cookie": clear_cookie()} if ended else None)
        if not qualifies(visit.principal):
            return refusal(visit.principal)
        tab = request.query_params.get("tab", "applications")
        tab = tab if tab in TABS else "applications"
        state = await run_in_threadpool(read, config.path)
        return page(visit, tab, tab_body(visit, state, registry(), tab), state=state)

    async def sign_in(request):
        refused = guard(request)
        if refused:
            return refused
        if not same_origin(request):
            return document("Fast Deploy settings", "<p>This form must be sent from the settings page itself.</p>", 403)
        data = await request.form()
        principal = find_principal(config, str(data.get("token", "")).strip())  # pasted tokens often carry a stray newline
        host = client(request)
        if principal is None:
            allowed, retry_after = failures.allow(host)
            if not allowed:
                store.record_event("sign_in", name="settings page", client=host, outcome="throttled")
                return document("Fast Deploy settings", "<p>Too many failed sign-in attempts from this address. Wait a minute and try again.</p>",
                                429, {"Retry-After": str(retry_after)})
            store.record_event("sign_in", name="settings page", client=host, outcome="rejected")
            return sign_in_page("That token was not accepted.", 401)
        if not qualifies(principal):
            store.record_event("sign_in", name="settings page", identity=principal.name, client=host, outcome="refused")
            return refusal(principal)
        store.record_event("sign_in", name="settings page", identity=principal.name, client=host, outcome="ok")
        value = sessions.open(principal, config.settings_session_seconds)
        return RedirectResponse(PREFIX, status_code=303, headers={"Cache-Control": "no-store", "Set-Cookie": cookie(value)})

    async def posted(request):
        """(Visit, form) for a POST from a signed-in operator with a valid form token, else (None, the response)."""
        refused = guard(request)
        if refused:
            return None, refused
        if not same_origin(request):
            return None, document("Fast Deploy settings", "<p>This form must be sent from the settings page itself.</p>", 403)
        visit, ended = visitor(request)
        if visit is None:
            return None, sign_in_page(ended or "Sign in first.", 401)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), visit.csrf.encode()):
            return None, document("Fast Deploy settings", f'<p>This form is stale. <a href="{PREFIX}">Open the page again</a>.</p>', 403)
        if not qualifies(visit.principal):
            return None, refusal(visit.principal)
        return visit, form

    async def sign_out(request):
        refused = guard(request)
        if refused:
            return refused
        found = sessions.find(request.cookies.get(COOKIE))
        if found is not None:  # signing out is the one change a forged form could make, and SameSite=Strict stops that too
            sessions.close(found[0])
        return RedirectResponse(PREFIX, status_code=303, headers={"Cache-Control": "no-store", "Set-Cookie": clear_cookie()})

    async def change(request):
        visit, form = await posted(request)
        if visit is None:
            return form
        tab = str(form.get("tab", ""))
        if tab not in TABS:
            return page(visit, "applications", '<p class="error">Unknown tab.</p>', 400)
        state = await run_in_threadpool(read, config.path)
        apps = registry()
        if state.config is None and tab != "advanced":
            return page(visit, tab, tab_body(visit, state, apps, tab), 409, state)
        result = prepare(visit, state, apps, tab, form)
        if isinstance(result, dict):  # problems: the tab again, with the submitted values and each problem next to its field
            return page(visit, tab, tab_body(visit, state, apps, tab, form, result), 400, state)
        return page(visit, tab, review(visit, tab, state, *result), state=state)

    def prepare(visit, state, apps, tab, form):
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
        text = HEADER + dump(new)
        return old, new, Change(visit.session_id, tab, state.sha, text, keys, ends, token, time.time() + PENDING_SECONDS)

    def review(visit, tab, state, old, new, change_):
        if change_ is None:
            return (f'<h2>Review the change</h2><p><strong>Nothing would change.</strong> The file already says this.</p>'
                    f'<p><a href="{PREFIX}?tab={tab}">Back to {TABS[tab]}</a></p>')
        if old is not None:
            lines_ = unified(old, new)
        else:  # the file on disk did not load: compare its text
            lines_ = list(difflib.unified_diff(state.text.splitlines(), dump(masked(new)).splitlines(), "configuration now", "after this change", lineterm=""))
        shown = "\n".join(f'<span class="{diff_class(line)}">{esc(line)}</span>' for line in lines_)
        change_id = pending.add(change_)
        warnings = []
        if change_.token:
            warnings.append(f"A new token for {change_.token[0]} is shown once, after you confirm.")
        for name in change_.ends:
            warnings.append(f"Every session of {name} ends: Claude's and this page's.")
        if visit.principal.name in change_.ends:
            warnings.append("That includes yours: after the new token is shown, sign in again.")
        if tab == "advanced":
            warnings.append("database, oauth and github_app changes need a restart: scripts/machine.sh restart.")
        notes = "".join(f"<li>{esc(warning)}</li>" for warning in warnings)
        return (f"<h2>Review the change</h2><p>Settings that change: {esc(', '.join(change_.keys))}.</p>"
                + (f'<ul class="error">{notes}</ul>' if notes else "")
                + f'<pre>{shown}</pre><p class="hint">Token hashes and the webhook address are shortened here; the file holds them in full.</p>'
                + f'<form method="post" action="{PREFIX}/confirm"><input type="hidden" name="csrf" value="{esc(visit.csrf)}">'
                f'<input type="hidden" name="change" value="{esc(change_id)}"><button type="submit">Write the file</button></form>'
                f'<p><a href="{PREFIX}?tab={tab}">Cancel</a> (nothing is written until you confirm; this review lasts {PENDING_SECONDS // 60} minutes)</p>')

    async def confirm(request):
        visit, form = await posted(request)
        if visit is None:
            return form
        change_ = pending.take(visit.session_id, str(form.get("change", "")))
        if change_ is None:
            return page(visit, "applications", "<p>This change expired or was already written; nothing was written now. Prepare it again.</p>", 410)
        outcome = await run_in_threadpool(write, visit, change_, client(request))
        if outcome is None:
            return page(visit, change_.tab, "<p class=\"error\">The file changed after this change was prepared (another save, or config-put), so nothing "
                        f'was written. <a href="{PREFIX}?tab={change_.tab}">Open the tab again</a> and make the change on the new file.</p>', 409)
        return page(visit, change_.tab, outcome) if visit.principal.name not in change_.ends else document(
            "Fast Deploy settings", f"<h1>Fast Deploy settings</h1>{outcome}<p><a href=\"{PREFIX}\">Sign in again</a></p>", headers={"Set-Cookie": clear_cookie()})

    def write(visit, change_, host):
        """Write a confirmed change; None when the file moved on since it was prepared."""
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
        parts = [f"<h2>Written</h2><p>Settings changed: {esc(', '.join(change_.keys))}. The previous file is kept as {esc(kept.name)} in "
                 f"{esc(str(kept.parent))}.</p>"]
        if reloaded and config.error:
            parts.append(f'<p class="notice"><strong>The controller applied the rest at once; {esc(config.error)}:</strong> scripts/machine.sh restart '
                         "(on a laptop, stop and start scripts/start.sh).</p>")
        elif reloaded:
            parts.append('<p class="notice">The controller applied it at once.</p>')
        else:
            parts.append(f'<p class="error">The controller did not take the new file: {esc(config.error)}</p>')
        if copied:
            parts.append(f"<p>Copied to {esc(copied[0])}.</p>")
        elif failed:
            parts.append(f'<p class="error">The copy to the backup bucket failed ({esc(failed)}); the nightly backup copies it again.</p>')
        if change_.token:
            name, token = change_.token
            parts.append(f'<p>The new token for <strong>{esc(name)}</strong>. Copy it now into a password manager: it is not shown again, and the '
                         f'controller keeps only its SHA-256.</p><p class="token">{esc(token)}</p>')
        parts.append(f'<p><a href="{PREFIX}?tab={change_.tab}">Back to {TABS[change_.tab]}</a></p>')
        return "".join(parts)

    return [Route(PREFIX, show, methods=["GET"]), Route(f"{PREFIX}/sign-in", sign_in, methods=["POST"]),
            Route(f"{PREFIX}/sign-out", sign_out, methods=["POST"]), Route(f"{PREFIX}/change", change, methods=["POST"]),
            Route(f"{PREFIX}/confirm", confirm, methods=["POST"])]


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
    caps = {}
    for line in lines(form.get("cloud_run_spend_caps")):
        project, _, amount = line.replace("=", ":").partition(":")
        value = number({"amount": amount}, "amount", found, whole=False)
        if not amount.strip() or value is INVALID or value is None:
            found.pop("amount", None)
            found["cloud_run_spend_caps"] = f"{line!r} is not project: amount"
            continue
        caps[project.strip()] = value
    put(candidate, "cloud_run_spend_caps", caps, config.cloud_run_spend_caps, {})
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
