"""How the settings page looks (labdeploy/settings.py holds what it does).

Server-rendered HTML under the sign-in page's Content-Security-Policy (default-src 'none'; style-src 'unsafe-inline'): no
script, no web font, no image file. The system font stack, ui-monospace for code and inline SVG icons. Every function here
turns data the routes already checked into markup; nothing here reads or writes the configuration.
"""
import copy
import dataclasses
import datetime
import hashlib
import html
import time
from urllib.parse import quote, unquote

import pydantic
from starlette.responses import HTMLResponse

from . import theme
from .config import Application, Config, PlatformUpdates, Principal, ProductionSettings, with_settings
from .infra import DEFAULT_SETTINGS, SETTING_RANGES
from .oauth import PAGE_HEADERS

PREFIX = "/settings"
# The sidebar's two groups: Monitor (labdeploy/monitor_page.py), open to every signed-in principal for the applications it
# may see, and Configure, the controller's own file, for an identity that sees every application and may deploy.
MONITOR = {"overview": "Overview", "deployments": "Deployments", "health": "Health", "releases": "Releases", "cost": "Cost", "activity": "Activity"}
TABS = {"connections": "Connections", "applications": "Applications", "platform": "Platform", "people": "People", "production": "Production", "advanced": "Advanced"}
KILL_SWITCH = "all applications"  # what the operator types to turn the controller-wide pause on or off
GITHUB = "https://github.com/"
MISSING = object()

INTRO = {
    "connections": "Connect your accounts, choose repositories and set the destination for new applications.",
    "applications": "Each application's release switches, cost ceiling, viewers and alerts. Its contract, infrastructure and secrets change "
                    "elsewhere; each application's page links to them.",
    "platform": "The controller itself: updates after a platform release, the auto-deploy check, backups, the watchdog's webhook and the "
                "spend caps you recorded.",
    "people": "Who may use Fast Deploy, through Claude and through this page. Each person signs in with a token of their own.",
    "production": "Who approves a promotion to production, and how visitors move to the new version.",
    "advanced": "The whole configuration file as YAML. For account and repository setup, use Connections.",
}

# Each infrastructure setting in words: label, help, unit.
INFRA = {
    "image_versions_to_keep": ("Image versions to keep", "The newest images the registry always keeps.", "versions"),
    "image_retention_days": ("Delete older images after", "Images beyond the kept versions go once they are this old.", "days"),
    "registry_cleanup_dry_run": ("Clean-up only reports", "On: the registry clean-up lists what it would delete and deletes nothing.", ""),
    "runtime_alerts": ("Runtime alerts", "Off: none of the alerts below is created.", ""),
    "error_alert_count": ("Alert on server errors", "More than this many 5xx answers in five minutes.", "errors"),
    "latency_alert_ms": ("Alert on slow answers", "When p95 latency stays above this for five minutes.", "ms"),
    "instance_alert_count": ("Alert on instances", "When this many instances or more run for five minutes.", "instances"),
}
LABELS = {
    "connections": "connections", "repositories": "allowed repositories", "google_client_id": "Google sign-in client",
    "google_account": "account used to select the destination", "project_id": "Google Cloud project", "project_number": "Google Cloud project number",
    "cost_ceiling_usd": "cost ceiling", "auto_deploy": "auto-deploy", "auto_merge": "auto-merge", "bake_seconds": "watch after release",
    "viewers": "viewers", "paused": "state", "provenance": "provenance", "infra": "infrastructure settings", "applications": "applications",
    "can_deploy": "may deploy", "may_merge_own_requests": "self-merge", "token_sha256": "token", "name": "name", "machine": "the machine installs",
    "workflows": "re-pin pull requests", "delay_minutes": "delay", "quiet_minutes": "quiet time", "reviewers": "reviewers", "rollout": "rollout",
    "auto_deploy_interval_seconds": "auto-deploy check", "backup_keep_days": "backups kept", "notify_webhook_url": "webhook",
    "cloud_run_spend_caps": "spend caps", "monthly_budgets_usd": "budgets", "cost_anomaly_detection": "Cost Anomaly Detection",
    "digest_hour_utc": "daily digest", "platform_updates": "automatic updates", "application_settings": "onboarded applications' switches",
    "confirm": "confirmation", "production": "production",
}


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def masked(data):
    """A copy for showing on screen: token hashes and the webhook address shortened to a fingerprint."""
    shown_ = copy.deepcopy(data)
    for principal in shown_.get("principals") or []:
        if isinstance(principal, dict) and isinstance(principal.get("token_sha256"), str):
            principal["token_sha256"] = f"(hash {principal['token_sha256'][:8]}...)"
    if isinstance(shown_.get("notify_webhook_url"), str):
        shown_["notify_webhook_url"] = f"(hidden, fingerprint {hashlib.sha256(shown_['notify_webhook_url'].encode()).hexdigest()[:8]})"
    return shown_


# ---- icons: small inline SVG, stroked with the text colour

PATHS = {
    "connections": '<path d="m10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
    "applications": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>'
                    '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
    "platform": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14.8c1.6.8 2.6 2.5 3 5.2"/>',
    "production": '<path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.2 7.5 9.5 4.4-1.3 7.5-4.9 7.5-9.5V6L12 3z"/><path d="m9 12 2 2 4-4"/>',
    "advanced": '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>',
    "signout": '<path d="M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3M16 16l4-4-4-4M20 12H10"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.5 2.5L16 9.5"/>',
    "alert": '<path d="M10.3 4.2 2.6 17.5a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4M12 17h.01"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5h.01"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "back": '<path d="m15 6-6 6 6 6"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    "overview": '<path d="M4.5 16a7.5 7.5 0 1 1 15 0"/><path d="m12 16 3.5-4.5"/><path d="M4 20h16"/>',
    "health": '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
    "releases": '<circle cx="12" cy="12" r="3.5"/><path d="M3 12h5.5M15.5 12H21"/>',
    "cost": '<circle cx="12" cy="12" r="9"/><path d="M14.8 9.4c-.5-.9-1.5-1.4-2.8-1.4-1.6 0-2.8.8-2.8 2 0 2.9 5.6 1.5 5.6 4.3 0 1.2-1.1 2.1-2.8 2.1-1.3 0-2.4-.5-2.9-1.4M12 6v2M12 16v2"/>',
    "activity": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" stroke-width="2.6"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v5h-5"/>',
}
MARK = theme.MARK


def icon(name, cls="icon"):
    return (f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>')


# ---- the look: labdeploy/theme.py holds the design system; these names stay for the modules that build on them

STYLE = theme.STYLE
MONITOR_STYLE = theme.MONITOR_STYLE


def document(title, body, status=200, headers=None):
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{esc(title)}</title><style>{STYLE}{MONITOR_STYLE}</style></head><body>{body}</body></html>')
    return HTMLResponse(page, status_code=status, headers={**PAGE_HEADERS, **(headers or {})})


# ---- what the page knows when it draws a tab

@dataclasses.dataclass
class Context:
    visit: object  # the signed-in principal and the form token
    state: object  # the file: text, data and Config, or why it does not load
    registry: dict  # applications onboarding registered (the database)
    productions: dict  # each application's production, registered by add_production
    last: dict | None  # who last wrote the file, and when
    version: str
    connections: dict | None = None

    configure = True  # only an identity that may change the configuration gets a Context

    @property
    def config(self):
        return self.state.config

    @property
    def count(self):
        return len(self.applications()) if self.config is not None else len(self.registry)

    def applications(self):
        """(id, application as the controller sees it, whether the file holds it) for every application."""
        if self.config is None:
            return []
        listed = [(app_id, app, True) for app_id, app in self.config.applications.items()]
        listed += [(app_id, effective(self.config, self.registry, app_id), False) for app_id in self.registry if app_id not in self.config.applications]
        return listed


@dataclasses.dataclass
class Frame:
    """What the frame around a Monitor view needs: who is signed in, the version, whether Configure is theirs to open and
    how many applications the Applications entry counts (None when it is not shown)."""
    visit: object
    version: str
    configure: bool
    count: int | None = None


def effective(config, registry, app_id):
    """An onboarded application as the controller sees it; the registered entry alone when its settings do not apply."""
    try:
        return with_settings(registry[app_id], config.application_settings.get(app_id))
    except pydantic.ValidationError:
        return registry[app_id]


def ago(at, now=None):
    seconds = max(0, (now or time.time()) - at)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86_400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86_400:
        return "yesterday"
    if seconds < 7 * 86_400:
        return f"{int(seconds // 86_400)} days ago"
    return f"on {datetime.datetime.fromtimestamp(at, datetime.UTC):%Y-%m-%d}"


def chip(text, kind=""):
    return f'<span class="chip {kind}">{esc(text)}</span>'


def callout(kind, title, body="", icon_name=None):
    name = icon_name or {"amber": "alert", "green": "check", "red": "error", "gray": "info"}[kind]
    inner = (f"<strong>{esc(title)}</strong>" if title else "") + body
    return f'<div class="callout {kind}" role="{"alert" if kind == "red" else "status"}">{icon(name)}<div class="body">{inner}</div></div>'


# ---- validation problems, by the field they belong to

class Problems:
    """Problems by dotted key. The summary at the top lists them all; each field also shows its own."""

    def __init__(self, found=None):
        self.all = dict(found or {})

    def take(self, key):
        return [message for k, message in self.all.items() if k == key or k.startswith(key + ".")]

    def summary(self):
        if not self.all:
            return ""
        items = "".join(f'<li>{f"<a href=\"#{esc(key)}\">{esc(describe(key))}:</a> " if key else ""}{esc(message)}</li>' for key, message in self.all.items())
        count = len(self.all)
        return callout("red", f"Nothing was changed. {count} {'thing needs' if count == 1 else 'things need'} fixing:", f"<ul>{items}</ul>")


class Form:
    """Values for one form's inputs: what was submitted when that form comes back with problems, else the file's."""

    def __init__(self, submitted=None):
        self.submitted = submitted

    @staticmethod
    def picker(submitted):
        sent = submitted.get("form") if submitted is not None else None
        return lambda name: Form(submitted if sent == name else None)

    def value(self, key, current):
        return self.submitted.get(key, "") if self.submitted is not None else ("" if current is None else current)

    def checked(self, key, current):
        return (key in self.submitted) if self.submitted is not None else bool(current)


def tristate(value):
    return "" if value is None else ("true" if value else "false")


# ---- fields: the settings-row pattern (label, key and help on the left, the control on the right)

def described(key, errors):
    return f' aria-invalid="true" aria-describedby="{esc(key)}-error"' if errors else ""


def error_text(key, errors):
    return f'<p class="field-error" id="{esc(key)}-error">{esc("; ".join(errors))}</p>' if errors else ""


def row(label, control, for_id=None, yaml=None, help_html="", errors=(), key=None, cell=""):
    label_html = f'<label class="label" for="{esc(for_id)}">{esc(label)}</label>' if for_id else f'<span class="label">{esc(label)}</span>'
    classes = "row" + (" invalid" if errors else "") + (" switch-row" if cell == "switch-cell" else "")
    return (f'<div class="{classes}"><div><div class="label-line">{label_html}'
            + (f'<code class="key">{esc(yaml)}</code>' if yaml else "") + "</div>"
            + (f'<p class="help">{help_html}</p>' if help_html else "")
            + f'</div><div class="control {cell}">{control}{error_text(key or for_id or "", errors)}</div></div>')


def text_control(form, key, current, errors=(), kind="text", attributes=""):
    return (f'<input id="{esc(key)}" name="{esc(key)}" type="{kind}" value="{esc(form.value(key, current))}"{described(key, errors)}'
            + (f" {attributes}" if attributes else "") + ">")


def number_control(form, key, current, unit, errors=(), low=None, high=None, whole=True, required=False, placeholder=None):
    attributes = " ".join(part for part in (f'min="{low}"' if low is not None else "", f'max="{high}"' if high is not None else "",
                                            'step="1"' if whole else 'step="any"', "required" if required else "", 'inputmode="decimal"',
                                            f'placeholder="{esc(placeholder)}"' if placeholder is not None else "") if part)
    field = text_control(form, key, current, errors, "number", attributes)
    return f'<div class="affix">{field}<span class="unit">{esc(unit)}</span></div>' if unit else field


def area_control(form, key, current, errors=(), rows=3, mono=False, placeholder=""):
    return (f'<textarea id="{esc(key)}" name="{esc(key)}" rows="{rows}"{" class=\"mono\"" if mono else ""} spellcheck="false"{described(key, errors)}'
            + (f' placeholder="{esc(placeholder)}"' if placeholder else "") + f">{esc(form.value(key, current))}</textarea>")


def switch_control(form, key, current):
    checked = " checked" if form.checked(key, current) else ""
    return f'<input class="switch" type="checkbox" role="switch" id="{esc(key)}" name="{esc(key)}" value="on"{checked}>'


def select_control(form, key, current, options, errors=()):
    chosen = str(form.value(key, current))
    rendered = "".join(f'<option value="{esc(value)}"{" selected" if chosen == value else ""}>{esc(text)}</option>' for value, text in options)
    return f'<span class="select"><select id="{esc(key)}" name="{esc(key)}"{described(key, errors)}>{rendered}</select></span>'


def number_row(form, bag, key, label, yaml, current, unit, help_html="", **options):
    errors = bag.take(key)
    return row(label, number_control(form, key, current, unit, errors, **options), key, yaml, help_html, errors)


def switch_row(form, bag, key, label, yaml, current, help_html=""):
    return row(label, switch_control(form, key, current), key, yaml, help_html, bag.take(key), cell="switch-cell")


def select_row(form, bag, key, label, yaml, current, options, help_html=""):
    errors = bag.take(key)
    return row(label, select_control(form, key, current, options, errors), key, yaml, help_html, errors)


def area_row(form, bag, key, label, yaml, current, help_html="", rows=3, mono=False, placeholder=""):
    errors = bag.take(key)
    return row(label, area_control(form, key, current, errors, rows, mono, placeholder), key, yaml, help_html, errors)


def text_row(form, bag, key, label, yaml, current, help_html="", kind="text", attributes=""):
    errors = bag.take(key)
    return row(label, text_control(form, key, current, errors, kind, attributes), key, yaml, help_html, errors)


def pager(link, page, total, per_page, what="rows"):
    """Where a long list is and the way to its other pages: `link(n)` is page n's address. Nothing for a list that fits on one."""
    pages = max(1, -(-total // per_page))
    if pages == 1:
        return ""
    page = min(max(1, page), pages)
    start, end = (page - 1) * per_page + 1, min(page * per_page, total)
    numbers = sorted({1, pages, *range(page - 2, page + 3)} & set(range(1, pages + 1)))
    items, previous = [], 0
    for number in numbers:
        if number - previous > 1:
            items.append('<span class="gap" aria-hidden="true">…</span>')
        items.append(f'<span aria-current="page">{number}</span>' if number == page else f'<a href="{esc(link(number))}" aria-label="Page {number}">{number}</a>')
        previous = number
    before = (f'<a class="btn ghost" href="{esc(link(page - 1))}" rel="prev">Previous</a>' if page > 1 else '<span class="btn ghost off">Previous</span>')
    after = (f'<a class="btn ghost" href="{esc(link(page + 1))}" rel="next">Next</a>' if page < pages else '<span class="btn ghost off">Next</span>')
    return (f'<nav class="pager" aria-label="Pages of {esc(what)}"><span class="num">{start:,}–{end:,} of {total:,} {esc(what)}</span>'
            f'<span class="pages">{before}{"".join(items)}{after}</span></nav>')


def card(title, body, sub="", cls=""):
    return (f'<section class="card {cls}"><div class="card-head"><div><h2>{esc(title)}</h2>'
            + (f'<p class="sub">{sub}</p>' if sub else "") + f'</div></div><div class="card-body">{body}</div></section>')


def form_open(visit, tab, which, extra=None):
    hidden = {"csrf": visit.csrf, "tab": tab, "form": which, **(extra or {})}
    return f'<form method="post" action="{PREFIX}/change">' + "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in hidden.items())


def submit_bar(text="Review the change", note="Nothing is written until you confirm on the next page."):
    return f'<div class="actions"><span class="note">{esc(note)}</span><button class="btn primary" type="submit">{esc(text)}</button></div>'


def danger_row(visit, tab, which, hidden, title, help_html, word, key, bag, button):
    """A danger-zone action: its own form, which asks for the name typed out before the change is even reviewed."""
    errors = bag.take(key)
    control = (f'<div class="confirm-line"><input id="{esc(key)}" name="confirm" type="text" autocomplete="off" spellcheck="false" required '
               f'aria-label="Type {esc(word)} to confirm"{described(key, errors)}>'
               f'<button class="btn danger" type="submit">{esc(button)}</button></div>')
    help_html += f' Type <strong class="mono nowrap">{esc(word)}</strong> to confirm.'
    return form_open(visit, tab, which, hidden) + row(title, control, key, None, help_html, errors, key) + "</form>"


# ---- the frame around every tab

def shell(ctx, tab, inner, status=200, headers=None, title=None):
    """The frame of every signed-in screen: the sidebar's Monitor group, its Configure group for an identity that may change
    the configuration, who is signed in, and the screen itself. The same words on every page."""
    groups = [("monitor", "Monitor", MONITOR)] + ([("configure", "Configure", TABS)] if ctx.configure else [])
    sections = []
    for key, label, entries in groups:
        links = []
        for name, words in entries.items():
            count = f'<span class="count">{ctx.count}</span>' if name == "applications" and ctx.count is not None else ""
            current = ' aria-current="page"' if name == tab else ""
            links.append(f'<a href="{PREFIX}?tab={name}"{current}>{icon("releases" if name == "deployments" else name)}<span>{words}</span>{count}</a>')
        sections.append(f'<div class="nav-section"><p class="nav-group" id="nav-{key}">{label}</p>'
                        f'<div class="nav-links" role="group" aria-labelledby="nav-{key}">{"".join(links)}</div></div>')
    name = ctx.visit.principal.name
    side = (f'<aside class="side"><a class="mark" href="{PREFIX}">{MARK}<span>Fast Deploy</span></a>'
            f'<nav class="nav" aria-label="Fast Deploy">{"".join(sections)}</nav>'
            f'<div class="who"><span class="avatar" aria-hidden="true">{esc(name[:1].upper())}</span><span class="as">Signed in as</span>'
            f'<span class="name">{esc(name)}</span><span class="version">Fast Deploy {esc(ctx.version)}</span>'
            f'<form method="post" action="{PREFIX}/sign-out"><input type="hidden" name="csrf" value="{esc(ctx.visit.csrf)}">'
            f'<button class="btn secondary" type="submit">{icon("signout")}Sign out</button></form></div></aside>')
    return document(title or f"{MONITOR.get(tab) or TABS.get(tab, 'Settings')} · Fast Deploy",
                    f'<div class="shell">{side}<main class="main"><div class="content">{inner}</div></main></div>', status, headers)


def head(title, lede, chips_html="", crumb=""):
    return (f'<header class="page-head">{crumb}<h1>{esc(title)}</h1>' + (f'<p class="lede">{lede}</p>' if lede else "")
            + (f'<div class="chips">{chips_html}</div>' if chips_html else "") + "</header>")


def last_written(ctx):
    if not ctx.last:
        return chip("Not changed on this page yet", "plain")
    who = f" by {ctx.last['identity']}" if ctx.last.get("identity") else ""
    return chip(f"Last written {ago(ctx.last['at'])}{who}", "plain")


def kill_chip(ctx, always=False):
    if ctx.config is not None and ctx.config.paused:
        return chip("Kill switch on: every application paused", "bad")
    return chip("Kill switch off", "ok") if always else ""


# ---- Applications

def app_link(app_id):
    return f"{PREFIX}?tab=applications&app={quote(app_id, safe='')}"


def money(value):
    if value is None:
        return '<span class="muted">None</span>'
    return f'<span class="num">${value:g}</span><span class="muted hide-sm"> / month</span>'


def applications_list(ctx, missing=None):
    listed = ctx.applications()
    paused = [app_id for app_id, app, _ in listed if app.paused]
    chips_html = (chip(f"{len(listed)} application{'s' if len(listed) != 1 else ''}", "plain")
                  + (chip(f"{len(paused)} paused", "warn") if paused else chip("All running", "ok")) + kill_chip(ctx, True) + last_written(ctx))
    body = head("Applications", esc(INTRO["applications"]), chips_html)
    if missing:
        body += callout("red", f"There is no application named {missing}.", "<p>Pick one from the list below.</p>")
    if not listed:
        return body + callout("gray", "No application is registered yet.", "<p>Onboarding adds them, or the Advanced tab.</p>")
    rows = []
    for app_id, app, in_file in listed:
        state = chip("Paused", "warn") if app.paused or ctx.config.paused else chip("Running", "ok")
        provenance = chip("Unavailable", "warn") if app.provenance == "unavailable" else '<span class="muted">Required</span>'
        deploys = "On" if app.auto_deploy else '<span class="muted">Off</span>'
        rows.append(f'<tr><td><a class="stretch" href="{esc(app_link(app_id))}">{esc(app_id)}</a>'
                    f'<span class="sub">{"In this file" if in_file else "Onboarded"}</span></td>'
                    f'<td class="hide-md repo">{esc(app.repository)}</td><td>{state}</td><td>{money(app.cost_ceiling_usd)}</td>'
                    f'<td class="hide-sm">{deploys}</td><td class="hide-md">{provenance}</td><td class="go hide-sm">{icon("chevron")}</td></tr>')
    table = ('<div class="table-scroll"><table class="list"><thead><tr><th>Application</th><th class="hide-md">Repository</th><th>Status</th>'
             '<th>Cost ceiling</th><th class="hide-sm">Auto-deploy</th><th class="hide-md">Provenance</th><th class="go hide-sm"><span class="sr">Open</span></th>'
             f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return body + f'<section class="card flush"><div class="card-body">{table}</div></section>'


def where_it_lives(ctx, app_id, app):
    platform = ctx.config.platform

    def platform_link(path, tree=False):
        if platform is None:
            return f'<code>{esc(path)}</code> <span class="muted">in the platform repository</span>'
        url = f"{GITHUB}{platform.repository}/{'tree' if tree else 'blob'}/{platform.branch}/{path}"
        return f'<a href="{esc(url)}">{esc(path)}</a>'

    service = app.target.service
    if app.central:
        contract = platform_link(f"apps/{service}/platform.yaml")
    else:
        path = app.repo_path("platform.yaml")
        contract = f'<a href="{esc(f"{GITHUB}{app.repository}/blob/{app.branch}/{path}")}">{esc(app.repository)}/{esc(path)}</a>'
    roots = platform_link(f"infra/gcp/apps/{service}", tree=True)
    environments = esc(app.environment)
    if app_id in ctx.productions:
        roots += "<br>" + platform_link(f"infra/gcp/apps-production/{service}", tree=True)
        environments += ", " + esc((ctx.productions[app_id] or {}).get("environment") or f"production-{service}")
    environment = f'<a href="{esc(f"{GITHUB}{app.pipeline_repo}/settings/environments")}">{environments}</a>'
    secrets = (f'<a href="{esc("https://console.cloud.google.com/security/secret-manager?project=" + app.target.project_id)}">'
               f'Secret Manager, {esc(app.target.project_id)}</a>')
    rows = [row("Contract", f"<div>{contract}</div>", help_html="platform.yaml. Changes through a reviewed pull request."),
            row("Infrastructure", f"<div>{roots}</div>", help_html="Its Terraform roots. Change through a pull request that the pipeline applies."),
            row("GitHub environment", f"<div>{environment}</div>", help_html="Where its release workflows run."),
            row("Secrets", f"<div>{secrets}</div>", help_html=f"Named <code>{esc(service)}-NAME</code>; set with gcloud, never through the platform.")]
    return card("Where it lives", "".join(rows), "What this page does not change, and where to change it.")


def application_detail(ctx, app_id, submitted=None, found=None):
    config, bag, pick = ctx.config, Problems(found), Form.picker(submitted)
    in_file = app_id in config.applications
    app = config.applications[app_id] if in_file else effective(config, ctx.registry, app_id)
    form = pick("main")
    prefix = f"applications.{app_id}" if in_file else f"application_settings.{app_id}"
    onboarding = "on" if (config.onboarding.auto_merge if config.onboarding else True) else "off"
    where = ("Its own entry in this file." if in_file else
             "Registered by onboarding. What you change here is kept under <code>application_settings</code> in this file.")
    paused = app.paused or config.paused
    chips_html = ((chip("Paused", "warn") if paused else chip("Running", "ok"))
                  + (chip("Provenance unavailable", "warn") if app.provenance == "unavailable" else chip("Provenance required", "plain"))
                  + chip("Auto-deploy on" if app.auto_deploy else "Auto-deploy off", "plain") + kill_chip(ctx) + last_written(ctx))
    crumb = f'<a class="crumb" href="{PREFIX}?tab=applications">{icon("back")}Applications</a>'
    body = head(app_id, f'<span class="mono">{esc(app.repository)}</span> · {where}', chips_html, crumb) + bag.summary()
    releases = "".join([
        switch_row(form, bag, f"{prefix}.auto_deploy", "Release new commits on its own", "auto_deploy", app.auto_deploy,
                   "When main moves past the live release, release it. Every guardrail still applies."),
        select_row(form, bag, f"{prefix}.auto_merge", "Merge its own pull requests", "auto_merge", tristate(app.auto_merge),
                   [("", f"Follow onboarding ({onboarding})"), ("true", "Yes"), ("false", "No")],
                   "Pull requests the platform opens for it merge once the policy and the checks pass."),
        number_row(form, bag, f"{prefix}.bake_seconds", "Watch after release", "bake_seconds", app.bake_seconds, "seconds",
                   "How long the new version's logs are watched before the release counts as done; a crash or a 5xx brings the previous one "
                   "back. 0 turns it off; at most 900.", low=0, high=900, required=True),
    ])
    cost = number_row(form, bag, f"{prefix}.cost_ceiling_usd", "Cost ceiling", "cost_ceiling_usd", app.cost_ceiling_usd, "USD / month",
                      "Plans whose worst case (every allowed instance busy all month) costs more are refused. Empty: no ceiling.",
                      whole=False, placeholder="No ceiling")
    access = area_row(form, bag, f"{prefix}.viewers", "Who may open its pages", "viewers", "\n".join(app.viewers),
                      "One per line: <code>user:EMAIL</code>, <code>group:EMAIL</code> or <code>domain:DOMAIN</code>. Applies when its "
                      "contract says <code>exposure: lab-members</code>, on top of the lab's default viewers.", rows=3, mono=True,
                      placeholder="user:someone@example.edu")
    infra, infra_errors = [], False
    for key, default in DEFAULT_SETTINGS.items():
        name = f"{prefix}.infra.{key}"
        label, help_text, unit = INFRA[key]
        errors = bag.take(name)
        infra_errors = infra_errors or bool(errors)
        if isinstance(default, bool):
            control = select_control(form, name, tristate(app.infra.get(key)),
                                     [("", f"Default ({'on' if default else 'off'})"), ("true", "On"), ("false", "Off")], errors)
        else:
            low, high = SETTING_RANGES.get(key, (None, None))
            control = number_control(form, name, app.infra.get(key), unit, errors, low, high, placeholder=f"{default}")
            help_text += f" Default {default}; {low} to {high}."
        infra.append(f'<div class="field{" invalid" if errors else ""}"><div class="label-line"><label class="label" for="{esc(name)}">{esc(label)}</label>'
                     f'<code class="key">{esc(key)}</code></div>{control}{error_text(name, errors)}<p class="help">{esc(help_text)}</p></div>')
    alerts = (f'<details class="card"{" open" if infra_errors else ""}><summary class="card-head"><div><h2>Alerts and infrastructure</h2>'
              '<p class="sub">Written into its Terraform root by the next <code>propose_infrastructure</code> pull request. Empty: the default.</p>'
              f'</div>{icon("chevron", "icon chev")}</summary><div class="card-body"><div class="grid2">{"".join(infra)}</div></div></details>')
    main = (form_open(ctx.visit, "applications", "main") + f'<input type="hidden" name="blocks" value="{esc(app_id)}">'
            + card("Releases", releases, "How new versions reach visitors.") + card("Cost", cost)
            + card("Access", access, "Who may open the application's pages behind the lab's sign-in.") + alerts + submit_bar() + "</form>")
    relaxed = app.provenance == "unavailable"
    hidden = {"application": app_id}
    danger = (danger_row(ctx.visit, "applications", "pause", {**hidden, "value": "false" if app.paused else "true"},
                         f"{'Resume' if app.paused else 'Pause'} {app_id}",
                         f"It is <strong>{'paused' if app.paused else 'running'}</strong>. Pausing stops new plans and releases of this application; "
                         "reads keep working.", app_id, f"danger.{app_id}.paused", bag, "Resume" if app.paused else "Pause")
              + danger_row(ctx.visit, "applications", "provenance", {**hidden, "value": "required" if relaxed else "unavailable"},
                           "Require signed provenance" if relaxed else "Release without signed provenance",
                           f"Now <strong>{esc(app.provenance)}</strong>. Unavailable lets it release without a signed build record: an exception "
                           "for staging or an emergency.", app_id, f"danger.{app_id}.provenance", bag, "Require it" if relaxed else "Allow it"))
    return body + main + where_it_lives(ctx, app_id, app) + card("Danger zone", danger, cls="danger-card")


# ---- Platform

def platform_tab(ctx, submitted=None, found=None):
    config, bag, pick = ctx.config, Problems(found), Form.picker(submitted)
    form = pick("main")
    updates = config.platform_updates or PlatformUpdates()
    chips_html = (kill_chip(ctx, True) + (chip("Automatic updates on", "ok") if config.platform_updates is not None else chip("Automatic updates off", "plain"))
                  + chip("Webhook set" if config.notify_webhook_url else "No webhook", "plain") + last_written(ctx))
    body = head("Platform", esc(INTRO["platform"]), chips_html) + bag.summary()
    automatic = "".join([
        switch_row(form, bag, "platform_updates.on", "Follow platform releases by themselves", "platform_updates", config.platform_updates is not None,
                   "Off: after a release on main, the machine's update and the re-pins stay manual. The settings below apply while this is on."),
        '<div class="dep">',
        switch_row(form, bag, "platform_updates.machine", "The machine installs main", "machine", updates.machine,
                   "Once main's checks have passed and the delay is over, with its health check and the way back."),
        switch_row(form, bag, "platform_updates.workflows", "Re-pin applications' workflows", "workflows", updates.workflows,
                   "Applications whose pinned runner is older get the re-pin pull request."),
        number_row(form, bag, "platform_updates.delay_minutes", "Wait after the checks pass", "delay_minutes", updates.delay_minutes, "minutes",
                   "The window in which a release can still be stopped. 0 to 10,080.", low=0, high=10_080, required=True),
        number_row(form, bag, "platform_updates.quiet_minutes", "Restart only when quiet for", "quiet_minutes", updates.quiet_minutes, "minutes",
                   "Nobody has used Claude's tools or the REST API for this long. 0 to 240.", low=0, high=240, required=True),
        "</div>",
    ])
    schedule = "".join([
        number_row(form, bag, "auto_deploy_interval_seconds", "Check for new commits every", "auto_deploy_interval_seconds",
                   config.auto_deploy_interval_seconds, "seconds", "For applications with auto-deploy on. 60 to 3,600.", low=60, high=3600, required=True),
        number_row(form, bag, "backup_keep_days", "Keep nightly backups for", "backup_keep_days", config.backup_keep_days, "days",
                   "Copies of the database next to it; older ones are deleted. 1 to 365.", low=1, high=365, required=True),
    ])
    hook_state = chip("Set", "ok") if config.notify_webhook_url else chip("Not set", "plain")
    notifications = "".join([
        row("Watchdog webhook", f'<div>{hook_state}</div>' + text_control(Form(), "notify_webhook_url", "", bag.take("notify_webhook_url"), "password",
                                                                           'autocomplete="off" placeholder="https://hooks.slack.com/services/..."'),
            "notify_webhook_url", "notify_webhook_url", "A Slack or Discord incoming webhook: the daily digest, a release that did worse a day "
            "later, and the watchdog's findings when the digest is off. Write-only: type a new address to replace it; the current one is never shown.",
            bag.take("notify_webhook_url")),
        switch_row(form, bag, "notify_webhook_url.remove", "Remove the webhook", None, False,
                   "The digest then goes only to its issue on the platform repository.")
        if config.notify_webhook_url else "",
        number_row(form, bag, "digest_hour_utc", "Daily digest at", "digest_hour_utc", config.digest_hour_utc, "o'clock UTC",
                   "Once a day, written by code: what shipped, how each release did a day later, what needs you, or all quiet. 13 UTC is 8 in the "
                   "morning in Texas in winter. 0 to 23; empty turns it off.", low=0, high=23),
    ])
    caps = "\n".join(f"{project}: {amount:g}" for project, amount in config.cloud_run_spend_caps.items())
    platform = config.platform
    script = (f'<a href="{esc(f"{GITHUB}{platform.repository}/blob/{platform.branch}/scripts/spend-cap.sh")}">scripts/spend-cap.sh</a>'
              if platform else "<code>scripts/spend-cap.sh</code>")
    spend = area_row(form, bag, "cloud_run_spend_caps", "Caps you created", "cloud_run_spend_caps", caps,
                     f"One per line, <code>project: USD a month</code>. A record for plans and the status: the cap itself lives in Cloud Billing and "
                     f"is made with {script} (docs/SETUP.md 7i).", rows=3, mono=True, placeholder="my-staging-project: 20")
    budgets = "\n".join(f"{project}: {amount:g}" for project, amount in config.monthly_budgets_usd.items())
    spend += "".join([
        area_row(form, bag, "monthly_budgets_usd", "Budgets you created", "monthly_budgets_usd", budgets,
                 "One per line, <code>project: USD a month</code>, as the budget says in Cloud Billing. The daily digest and the weekly cost report "
                 "compare the month's spend and its forecast with it.", rows=3, mono=True, placeholder="my-staging-project: 20"),
        switch_row(form, bag, "cost_anomaly_detection", "Cost Anomaly Detection is on", "cost_anomaly_detection", config.cost_anomaly_detection,
                   "Google's free detector of unusual daily spend, turned on in Cloud Billing (docs/SETUP.md 7q). A record: the digest stops reminding."),
    ])
    main = (form_open(ctx.visit, "platform", "main")
            + card("Automatic updates", automatic, "What follows a platform release on main.") + card("Auto-deploy and backups", schedule)
            + card("Notifications", notifications) + card("Spend caps and budgets", spend, "Google's native Cloud Run spend caps, the budgets and the "
                                                                     "anomaly detector, as you set them up in Cloud Billing.") + submit_bar() + "</form>")
    paused = config.paused
    kill = danger_row(ctx.visit, "platform", "kill", {"value": "false" if paused else "true"},
                      "Resume every application" if paused else "Pause every application",
                      f"The kill switch is <strong>{'on' if paused else 'off'}</strong>. On, it stops new plans and releases of every application; "
                      "reads keep working.", KILL_SWITCH, "danger.paused", bag, "Resume all" if paused else "Pause all")
    return body + main + card("Danger zone", kill, cls="danger-card")


# ---- People

def access_chips(principal):
    if "*" in principal.applications:
        access = chip("All applications", "plain")
    elif principal.applications:
        access = chip(", ".join(principal.applications), "plain")
    else:
        access = chip("No applications", "plain")
    return (access + (chip("Can deploy", "ok") if principal.can_deploy else chip("Read-only", "plain"))
            + (chip("Self-merge", "plain") if principal.may_merge_own_requests else chip("Needs a second operator", "warn")))


def people_tab(ctx, submitted=None, found=None):
    config, bag, pick = ctx.config, Problems(found), Form.picker(submitted)
    form = pick("main")
    sent = submitted.get("people") if submitted is not None and submitted.get("form") == "main" else None
    deployers = sum(1 for p in config.principals if p.can_deploy)
    chips_html = (chip(f"{len(config.principals)} {'person' if len(config.principals) == 1 else 'people'}", "plain")
                  + chip(f"{deployers} can deploy", "plain") + kill_chip(ctx) + last_written(ctx))
    body = head("People", esc(INTRO["people"]), chips_html) + bag.summary()
    rows = []
    for principal in config.principals:
        prefix = f"principals.{principal.name}"
        mine = Form(form.submitted) if sent == principal.name else Form()
        fields = "".join([
            text_row(mine, bag, f"{prefix}.applications", "Applications", "applications", ", ".join(principal.applications),
                     "Application ids separated by commas, or <code>*</code> for every one, including those onboarding adds later."),
            switch_row(mine, bag, f"{prefix}.can_deploy", "May deploy", "can_deploy", principal.can_deploy,
                       "Off: read-only. Plans, releases and onboarding need it."),
            switch_row(mine, bag, f"{prefix}.may_merge_own_requests", "Their requests merge without a second operator", "may_merge_own_requests",
                       principal.may_merge_own_requests, "Off: the pull requests opened for them wait for another operator to review and merge."),
        ])
        opened = " open" if sent == principal.name or bag.take(prefix) else ""
        you = ' <span class="muted">(you)</span>' if principal.name == ctx.visit.principal.name else ""
        rows.append(f'<details{opened}><summary><span class="who-cell">{esc(principal.name)}{you}</span><span class="chips">{access_chips(principal)}</span>'
                    f'{icon("chevron", "icon chev")}</summary><div class="edit">'
                    + form_open(ctx.visit, "people", "main", {"people": principal.name}) + fields
                    + submit_bar("Review the change", "") + "</form></div></details>")
    people = f'<section class="card flush"><div class="card-body people">{"".join(rows)}</div></section>'
    blank = pick("add")
    add = (form_open(ctx.visit, "people", "add") + card("Add a person", "".join([
        text_row(blank, bag, "new.name", "Name", "name", "", "How events and pull requests name them.", attributes='autocomplete="off"'),
        text_row(blank, bag, "new.applications", "Applications", "applications", "", "Ids separated by commas, or <code>*</code>."),
        switch_row(blank, bag, "new.can_deploy", "May deploy", "can_deploy", False),
        switch_row(blank, bag, "new.may_merge_own_requests", "Their requests merge without a second operator", "may_merge_own_requests", True),
    ]), "The token is made on this server and shown once, after you confirm. Only its SHA-256 is kept.")
        + submit_bar("Review the new person", "") + "</form>")
    danger = []
    for principal in config.principals:
        hidden = {"principal": principal.name}
        danger.append(danger_row(ctx.visit, "people", "rotate", hidden, f"Rotate the token of {principal.name}",
                                 "A new token is shown once; the old one stops working at once.", principal.name, f"danger.{principal.name}.rotate", bag,
                                 "Rotate"))
        danger.append(danger_row(ctx.visit, "people", "remove", hidden, f"Remove {principal.name}", "They can no longer sign in anywhere.",
                                 principal.name, f"danger.{principal.name}.remove", bag, "Remove"))
    return (body + people + add + card("Danger zone", "".join(danger), "Rotating a token or removing a person ends every session signed in with the "
                                       "old token: Claude's and this page's.", cls="danger-card"))


# ---- Production and Advanced

def production_tab(ctx, submitted=None, found=None):
    bag, pick = Problems(found), Form.picker(submitted)
    production, form = ctx.config.production, pick("main")
    if production is None:
        return (head("Production", esc(INTRO["production"]), chip("No production block", "plain") + kill_chip(ctx) + last_written(ctx))
                + callout("gray", "This controller has no production yet.", "<p>docs/SETUP.md 7n says how to add one; the first time, add the "
                                                                              "<code>production</code> block on the Advanced tab.</p>"))
    chips_html = (chip(f"Project {production.project_id}", "plain") + chip("Rollout " + ", ".join(f"{step}%" for step in production.rollout), "plain")
                  + kill_chip(ctx) + last_written(ctx))
    body = head("Production", esc(INTRO["production"]), chips_html) + bag.summary()
    approvals = area_row(form, bag, "production.reviewers", "Reviewers", "reviewers", "\n".join(production.reviewers),
                         "GitHub user names, one per line, one to six. Each promotion waits for one of them. They reach each "
                         "<code>production-&lt;service&gt;</code> environment when <code>add_production</code> runs: ask for it again for each "
                         "application after a change.", rows=3, mono=True)
    rollout = "".join([
        text_row(form, bag, "production.rollout", "Traffic at each step", "rollout", ", ".join(str(step) for step in production.rollout),
                 "Shares of visitors in percent, rising and ending at 100, such as 10, 50, 100."),
        number_row(form, bag, "production.bake_seconds", "Watch each step for", "bake_seconds", production.bake_seconds, "seconds",
                   "0 to 1,800. A crash or a 5xx at any step brings the previous version back.", low=0, high=1800, required=True),
    ])
    return (body + form_open(ctx.visit, "production", "main") + card("Approvals", approvals, "Who approves a promotion on GitHub.")
            + card("Rollout", rollout, "Applies to the next promotion.") + submit_bar() + "</form>")


def advanced_tab(ctx, submitted=None, found=None):
    bag, form = Problems(found), Form.picker(submitted)("main")
    text = form.submitted.get("text", "") if form.submitted is not None else ctx.state.text
    loads = chip("The file loads", "ok") if ctx.config is not None else chip("The file does not load", "bad")
    body = head("Advanced", esc(INTRO["advanced"]), loads + chip("database, oauth, github_app need a restart", "plain") + kill_chip(ctx) + last_written(ctx)) + bag.summary()
    note = callout("amber", "Read this before you save.", "<p>Changes to <code>database</code>, <code>oauth</code> and <code>github_app</code> "
                   "need a restart: <code>scripts/machine.sh restart</code>. The page writes the file as the controller reads it, so comments and "
                   "formatting are not kept; the previous file stays in <code>history/</code>.</p>")
    errors = bag.take("text")
    editor = (f'<div class="{"invalid" if bag.all else ""}"><div class="editor"><label class="sr" for="text">The configuration file</label>'
              f'<textarea id="text" name="text" rows="32" spellcheck="false" autocapitalize="off" autocomplete="off"{described("text", errors)}>'
              f"{esc(text)}</textarea></div>{error_text('text', errors)}</div>")
    return body + note + form_open(ctx.visit, "advanced", "main") + editor + submit_bar() + "</form>"


def broken(ctx, tab):
    return (head(TABS[tab], esc(INTRO[tab]), chip("The file does not load", "bad"))
            + callout("red", "The file on disk does not load, so only the Advanced tab can change it.",
                      f'<p class="mono" style="font-size:13px">{esc(ctx.state.error)}</p><p>Until it loads, the controller keeps the configuration it read '
                      f'last. <a href="{PREFIX}?tab=advanced">Open the Advanced tab</a>.</p>'))


def tab_page(ctx, tab, app=None, submitted=None, found=None, status=200):
    """One tab. After a refused change, the form that was sent keeps its values and each problem sits by its field."""
    if tab == "advanced":
        inner = advanced_tab(ctx, submitted, found)
    elif ctx.config is None:
        inner = broken(ctx, tab)
    elif tab == "connections":
        from .connections_page import page
        inner = page(ctx, submitted, found)
    elif tab == "platform":
        inner = platform_tab(ctx, submitted, found)
    elif tab == "people":
        inner = people_tab(ctx, submitted, found)
    elif tab == "production":
        inner = production_tab(ctx, submitted, found)
    elif app and app in {app_id for app_id, _, _ in ctx.applications()}:
        inner = application_detail(ctx, app, submitted, found)
    else:
        inner = applications_list(ctx, missing=app)
    return shell(ctx, tab, inner, status, title=f"{app} · Fast Deploy" if tab == "applications" and app else None)


def notice(ctx, tab, kind, title, text, back, status):
    """A one-message page inside the frame: an expired review, a file that moved on."""
    return shell(ctx, tab, head(TABS[tab], "") + callout(kind, title, f"<p>{text}</p>")
                 + f'<div class="actions" style="justify-content:flex-start"><a class="btn secondary" href="{esc(back)}">Back</a></div>', status)


# ---- the review: plain words first, then the file's diff

def lookup(data, key):
    value = data
    parts = key.split(".")
    index = 0
    while index < len(parts):
        part = parts[index]
        if isinstance(value, list) and index > 0 and parts[index - 1] == "principals":
            value = next((p for p in value if isinstance(p, dict) and p.get("name") == part), MISSING)
        elif isinstance(value, dict):
            value = value.get(part, MISSING)
        else:
            return MISSING
        if value is MISSING:
            return MISSING
        index += 1
    return value


def split(key):
    """(subject, the setting's own path) for a dotted key."""
    parts = key.split(".") if key else []
    if len(parts) >= 2 and parts[0] in ("applications", "application_settings", "principals"):
        return parts[1], parts[2:]
    if len(parts) >= 2 and parts[0] == "danger":
        return parts[1] if len(parts) == 3 else None, ["confirm"]
    if parts and parts[0] == "new":
        return "new person", parts[1:]
    if parts and parts[0] in ("production", "platform_updates", "cloud_run_spend_caps", "connections", "onboarding", "github_app") and len(parts) > 1:
        return LABELS.get(parts[0], {"connections": "Connections", "onboarding": "New applications", "github_app": "GitHub connection"}.get(parts[0])), parts[1:]
    return None, parts


def label_of(rest):
    if not rest:
        return ""
    if rest[0] == "infra" and len(rest) > 1 and rest[1] in INFRA:
        return INFRA[rest[1]][0].lower()
    return LABELS.get(rest[0], rest[0].replace("_", " "))


def describe(key, capital=False):
    subject, rest = split(key)
    label = label_of(rest)
    if subject and label:
        return f"{subject}, {label}"
    if capital and not subject and label:
        return label[:1].upper() + label[1:]
    return subject or label or key


MODELS = {"applications": Application, "principals": Principal, "production": ProductionSettings, "platform_updates": PlatformUpdates}


def default_of(key):
    """What a missing setting means: its default, or the registered entry's own value for an onboarded application."""
    parts = key.split(".")
    if parts[0] == "application_settings":
        return "as registered"
    if len(parts) == 4 and parts[2] == "infra" and parts[3] in DEFAULT_SETTINGS:
        return DEFAULT_SETTINGS[parts[3]]
    if len(parts) == 1:
        model, field = Config, parts[0]
    elif len(parts) == 3 and parts[0] in ("applications", "principals"):
        model, field = MODELS[parts[0]], parts[2]
    elif len(parts) == 2 and parts[0] in ("production", "platform_updates"):
        model, field = MODELS[parts[0]], parts[1]
    else:
        return MISSING
    return model.model_fields[field].get_default(call_default_factory=True) if field in model.model_fields else MISSING


def shown(value, field):
    if field == "repositories":
        if value is MISSING or value is None:
            return "GitHub access (no local restriction)"
        if value == []:
            return "platform repository only"
    if value is MISSING or value is None:
        return "not set" if field != "cost_ceiling_usd" else "no ceiling"
    if field == "paused" and isinstance(value, bool):
        return "paused" if value else "running"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "none"
    if isinstance(value, dict):
        return ", ".join(f"{INFRA[k][0].lower() if k in INFRA else LABELS.get(k, k)}: {shown(v, k)}{unit_after(v, k)}" for k, v in value.items()) or "defaults"
    return str(value)


def unit_after(value, field):
    unit = UNITS.get(field)
    return f" {unit}" if unit and isinstance(value, int | float) and not isinstance(value, bool) else ""


UNITS = {"cost_ceiling_usd": "USD / month", "bake_seconds": "seconds", "auto_deploy_interval_seconds": "seconds", "backup_keep_days": "days",
         "delay_minutes": "minutes", "quiet_minutes": "minutes", **{key: unit for key, (_, _, unit) in INFRA.items() if unit}}


def change_lines(old, new, keys):
    """One plain line per changed setting: who or what, the setting, and its old and new values (None for a single word). Secrets as
    the diff shows them."""
    if old is None:
        return [("The whole file", "", "replaced", None)]
    before, after = masked(old), masked(new)
    lines = []
    for key in keys:
        subject, rest = split(key)
        field = rest[-1] if rest else ""
        was, now = lookup(before, key), lookup(after, key)
        if key == "platform_updates":
            was = "off" if was is MISSING else "on" + (f" ({shown(was, '')})" if was else "")
            now = "off" if now is MISSING else "on" + (f" ({shown(now, '')})" if now else "")
        elif not rest and subject:  # a whole application or person
            if was is not MISSING:
                lines.append((describe(key, True), key, "removed", None))
            elif key.startswith("principals.") and isinstance(now, dict):
                access = "every application" if "*" in now.get("applications", []) else ", ".join(now.get("applications", [])) or "no application"
                rights = "may deploy" if now.get("can_deploy") else "read-only"
                lines.append((describe(key, True), key, f"added: {access}, {rights}", None))
            else:
                lines.append((describe(key, True), key, "added", None))
            continue
        else:
            if was is MISSING:
                was = default_of(key)
            if now is MISSING:
                now = default_of(key)
            if field == "token_sha256":
                lines.append((describe(key, True), key, "replaced with a new token", None))
                continue
            if field == "notify_webhook_url":
                was, now = ("set" if was not in (MISSING, None) else "not set"), ("set" if now not in (MISSING, None) else "not set")
                if was == now == "set":
                    lines.append((describe(key, True), key, "replaced", None))
                    continue
            unit = UNITS.get(field) if any(isinstance(v, int | float) and not isinstance(v, bool) for v in (was, now)) else None
            was, now = shown(was, field), shown(now, field) + (f" {unit}" if unit and now not in (MISSING, None) else "")
        lines.append((describe(key, True), key, was, now))
    return lines


def diff_class(line):
    if line.startswith(("---", "+++", "@@")):
        return "meta"
    return "add" if line.startswith("+") else "del" if line.startswith("-") else ""


def review_page(ctx, tab, back, change, change_id, old, new, diff_lines, minutes):
    if tab == "connections":
        from .connections_page import settings_review
        return settings_review(ctx, back, change, change_id, old, new, diff_lines)
    items = []
    for what, key, was, now in change_lines(old, new, change.keys):
        values = (f'<span class="new">{esc(was)}</span>' if now is None else
                  f'<span class="old">{esc(was)}</span> <span class="muted">to</span> <span class="new">{esc(now)}</span>')
        items.append(f'<li><span class="what"><strong>{esc(what)}</strong>' + (f'<code class="key">{esc(key)}</code>' if key else "")
                     + f'</span><span class="values">{values}</span></li>')
    warnings = []
    if change.token:
        warnings.append(f"A new token for {change.token[0]} is shown once, after you confirm.")
    for name in change.ends:
        warnings.append(f"Every session of {name} ends: Claude's and this page's.")
    if ctx.visit.principal.name in change.ends:
        warnings.append("That includes yours: after the new token is shown, sign in again.")
    if any(key.split(".")[0] in ("database", "oauth", "github_app") for key in change.keys):
        warnings.append("database, oauth and github_app changes need a restart: scripts/machine.sh restart.")
    count = len(change.keys)
    body = head("Review the change", f"{count} setting{'s' if count != 1 else ''} would change. Nothing is written until you confirm.",
                chip(TABS[tab], "plain") + chip(f"This review lasts {minutes} minutes", "plain"))
    if warnings:
        body += callout("amber", "Before you confirm", "<ul>" + "".join(f"<li>{esc(w)}</li>" for w in warnings) + "</ul>")
    body += card("What changes", f'<ul class="changes">{"".join(items)}</ul>')
    rendered = "".join(f'<span class="{diff_class(line)}">{esc(line)}</span>' for line in diff_lines)
    changed = sum(1 for line in diff_lines if line[:1] in "+-" and not line.startswith(("---", "+++")))
    body += (f'<details class="card diff"><summary>{icon("chevron", "icon chev")}The file, line by line <span class="muted">{changed} changed '
             f'line{"s" if changed != 1 else ""} · token hashes and the webhook shortened</span></summary><pre class="diff">{rendered}</pre></details>')
    body += (f'<form method="post" action="{PREFIX}/confirm"><input type="hidden" name="csrf" value="{esc(ctx.visit.csrf)}">'
             f'<input type="hidden" name="change" value="{esc(change_id)}"><div class="actions">'
             f'<a class="btn secondary" href="{esc(back)}">Cancel</a><button class="btn primary" type="submit">'
             + ('Save settings' if tab == 'connections' else 'Write the file') + '</button></div></form>')
    return shell(ctx, tab, body)


def nothing_page(ctx, tab, back):
    if tab == "connections":
        from .connections_page import next_step
        step = "github" if "step=google" in back else "application-setup"
        body = head("These choices are already saved", "You can continue without saving again.") + next_step(step, "Continue setup")
        return shell(ctx, tab, '<div class="connections">' + body + '</div>')
    body = (head("Review the change", "Nothing would change.") + callout("gray", "Nothing would change.", "<p>The file already says this.</p>")
            + f'<div class="actions" style="justify-content:flex-start"><a class="btn secondary" href="{esc(back)}">Back</a></div>')
    return shell(ctx, tab, body)


# ---- after the write

def outcome_body(outcome, framed=True):
    keys, kept = outcome["keys"], outcome["kept"]
    if outcome["reloaded"] and outcome["notes"]:
        top = callout("amber", f"Written. The controller applied the rest at once; {outcome['notes']}.",
                      "<p>Restart it: <code>scripts/machine.sh restart</code> (on a laptop, stop and start <code>scripts/start.sh</code>).</p>", "check")
    elif outcome["reloaded"]:
        top = callout("green", "Written. The controller applied it at once.")
    else:
        top = callout("red", f"Written, but the controller did not take the new file: {outcome['notes']}")
    facts = [("Settings changed", "<br>".join(f'{esc(describe(key, True))} <code class="key">{esc(key)}</code>' for key in keys)),
             ("Previous file", f'<code>{esc(kept.name)}</code><br><span class="muted">in {esc(str(kept.parent))}</span>')]
    if outcome["copied"]:
        facts.append(("Backup copy", f"Copied to <code>{esc(outcome['copied'][0])}</code>"))
    elif outcome["failed"]:
        facts.append(("Backup copy", f'<span class="field-error">The copy to the backup bucket failed ({esc(outcome["failed"])}); the nightly '
                                     "backup copies it again.</span>"))
    facts.append(("Audit trail", "A <code>configuration</code> event names who saved, the tab and the keys; never their values."))
    token = ""
    if outcome["token"]:
        name, value = outcome["token"]
        token = (f'<div class="token-box"><h2>The new token for {esc(name)}</h2><p>Copy it now into a password manager. It is not shown again, and '
                 f'the controller keeps only its SHA-256.</p><code class="token">{esc(value)}</code></div>')
    listed = f'<dl class="facts">{"".join(f"<div><dt>{esc(term)}</dt><dd>{value}</dd></div>" for term, value in facts)}</dl>'
    return top + token + (card("What happened", listed) if framed else listed)


def result_page(ctx, tab, back, outcome):
    if tab == "connections":
        from .connections_page import settings_result
        return settings_result(ctx, back, outcome)
    where = back.split("app=", 1)[1] if "app=" in back else TABS[tab]
    body = head("Written", "The configuration file has changed.") + outcome_body(outcome)
    body += (f'<div class="actions" style="justify-content:flex-start"><a class="btn secondary" href="{esc(back)}">{icon("back")}Back to '
             f'{esc(unquote(where))}</a></div>')
    return shell(ctx, tab, body)


def signed_out_result(outcome, headers):
    """After rotating or removing your own token: the outcome and the new token, outside the frame, and the cookie cleared."""
    body = (f'<div class="center"><div class="card wide">{MARK}<h1>Written</h1><p class="lede">Your own token changed, so this sign-in has ended.</p>'
            + outcome_body(outcome, framed=False) + f'<a class="btn primary block" href="{PREFIX}" style="margin-top:20px">Sign in again</a></div></div>')
    return document("Written · Fast Deploy", body, headers=headers)


# ---- sign-in, refusals and plain messages

def sign_in_page(minutes, error="", status=200, headers=None):
    problem = callout("red", error) if error else ""
    body = (f'<div class="center"><div class="card">{MARK}<h1>Sign in to Fast Deploy</h1>'
            '<p class="lede">Reports, live health and cost of the applications your token may see; for the operator, the controller\'s own '
            'configuration too.</p>' + problem
            + f'<form method="post" action="{PREFIX}/sign-in"><label for="token">Operator token</label>'
            '<input id="token" name="token" type="password" autocomplete="off" required autofocus>'
            '<button class="btn primary block" type="submit">Sign in</button></form>'
            '<p class="foot">Any token of this controller signs in, and Monitor shows the applications it may see. Configure needs a token that may '
            'see every application and deploy. The token is checked on this server only. A sign-in lasts at most '
            f"{minutes} minutes, and a restart of the controller ends it.</p></div></div>")
    return document("Sign in · Fast Deploy", body, status, headers)


def message_page(title, text, status=200, headers=None, action=""):
    body = (f'<div class="center"><div class="card">{MARK}<h1>{esc(title)}</h1><div class="lede">{text}</div>{action}</div></div>')
    return document(f"{title} · Fast Deploy", body, status, headers)


def refusal(frame, tab):
    """A Configure screen asked for by an identity that may not change the configuration: said inside the frame, whose sidebar
    shows only Monitor, and the sign-in stays."""
    name = frame.visit.principal.name
    body = (head(TABS[tab], "") + callout("gray", "Configure is the operator's.",
                                          f"<p>{esc(name)} may not change the controller's configuration: that needs an identity with "
                                          "<code>applications: [\"*\"]</code> and <code>can_deploy</code>. Monitor shows what this token may see. Ask "
                                          "the operator, or use <code>scripts/machine.sh config-get</code> and <code>config-put</code>.</p>")
            + f'<div class="actions" style="justify-content:flex-start"><a class="btn secondary" href="{PREFIX}?tab=overview">Open the overview</a></div>')
    return shell(frame, tab, body, 403)
