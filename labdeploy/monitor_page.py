"""How the settings page's Monitor section looks (labdeploy/monitor.py gathers what it shows).

Five views inside the settings page's frame: Overview, Health, Releases, Cost and Activity. Server-rendered HTML with the
charts of labdeploy/charts.py, under the same headers as every screen of the page: no script, no image file, nothing
loaded from elsewhere. Every view says how fresh it is: live numbers "a few minutes behind", spend "through" a day, the
records "as of" the moment the page was drawn. Nothing here changes anything; where a change makes sense, the page says
what to ask Claude.
"""
import datetime
import re
from urllib.parse import quote, urlencode

from . import charts
from .chat import money, plural
from .monitor import LIVE_SECONDS, RANGES
from .settings_page import PREFIX, ago, callout, card, chip, esc, head, icon, shell

LIVE_WORDS = ("Live, a few minutes behind: Cloud Monitoring samples each minute and shows a sample up to three minutes later, and this page keeps "
              f"a reading {LIVE_SECONDS // 60} minutes for everyone who opens it.")
LEVELS = {"ok": ("Healthy", "ok"), "warn": ("Needs a look", "warn"), "bad": ("Needs you", "bad"), "idle": ("Not released yet", "plain")}
VERDICTS = {"better": ("Better than the version before", "ok"), "same": ("About the same as the version before", "plain"),
            "worse": ("Worse than the version before", "bad"), "too_little_traffic": ("Too little traffic to compare", "plain"),
            "first": ("First release: nothing to compare with", "plain"), "replaced": ("Replaced within the hour", "plain"),
            "rolled_back": ("Rolled back by a person", "warn"), "unreadable": ("The report could not be read", "plain")}
OUTCOMES = {"live": "Reached users", "restored": "Went back on its own", "stopped": "Stopped before reaching anyone",
            "rolled_back": "Rolled back by a person", "in_progress": "On its way", "rollback": "Rollback"}
KINDS = {"tool": "Claude's tool call", "rest": "REST call", "sign_in": "Sign-in", "configuration": "Configuration change",
         "auto_deploy": "Auto-deploy", "watchdog": "Watchdog check", "backup": "Nightly backup", "digest": "Daily digest",
         "release_report": "Release report", "promotion": "Promotion", "production_rollback": "Production rollback",
         "production": "Production set-up", "release_tag": "Release tag", "recheck": "Recheck", "drill": "Drill", "restore_drill": "Restore drill",
         "auth_burst": "Failed sign-ins slowed", "token_reuse": "Token reuse", "session_revoked": "Session ended", "client_registered": "Client registered",
         "onboarding": "Onboarding", "contract_release": "Contract release", "propose_configuration": "Draft pull request"}
TAB_WORDS = {"health": "Open its health", "releases": "Open releases", "cost": "Open cost", "activity": "Open activity"}


# ---- small pieces

def href(tab, **params):
    return f"{PREFIX}?" + urlencode({"tab": tab, **{key: value for key, value in params.items() if value not in (None, "", False)}})


def when(value):
    """Seconds since 1970 or ISO text as an aware moment in UTC; None for anything else."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.UTC)
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)


def seconds_of(value):
    moment = when(value)
    return moment.timestamp() if moment else None


def clock(value):
    moment = when(value)
    return f"{moment:%H:%M} UTC" if moment else "unknown"


def day_words(value):
    moment = when(value)
    return f"{moment.day} {moment:%b}" if moment else "unknown"


def stamp(value):
    moment = when(value)
    return f"{moment.day} {moment:%b} {moment:%H:%M}" if moment else "unknown"


def age(value, now):
    """How long ago, as the page's own lines say it: just now, 12 min ago, 5 h ago, yesterday, 3 days ago, then the day."""
    at = seconds_of(value)
    if at is None:
        return "unknown"
    return ago(at, now) if now - at < 7 * 86_400 else f"on {day_words(at)}"


def span(seconds):
    """A duration as people say it: 45 s, 7 min, 3.5 h, 2 days."""
    if seconds is None:
        return "–"
    if seconds < 90:
        return f"{round(seconds)} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 2 * 86_400:
        return f"{seconds / 3600:.1f} h".replace(".0 h", " h")
    return f"{seconds / 86_400:.1f} days".replace(".0 days", " days")


def percent(share):
    """A share as a percentage with no needless zeros: 100%, 99.95%, 98.2%."""
    return f"{share * 100:.2f}".rstrip("0").rstrip(".") + "%"


def milliseconds(value):
    return "–" if value is None else f"{round(value):,} ms"


def short(sha):
    return (sha or "")[:7] or "unknown"


def commit(sha, message, limit=60):
    first = " ".join(str(message or "").split())
    first = first if len(first) <= limit else first[: limit - 1].rstrip() + "…"
    return f'<span class="mono">{esc(short(sha))}</span>' + (f" {esc(first)}" if first else "")


def coded(text):
    """Plain text whose `backticked` words (the watchdog writes for Markdown) show as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(text))


def level_chip(level):
    words, kind = LEVELS[level]
    return chip(words, kind)


def verdict_chip(report, released_at=None, now=None):
    verdict = (report or {}).get("verdict")
    if verdict in VERDICTS:
        words, kind = VERDICTS[verdict]
        return chip(words, kind)
    young = released_at and (now or 0) - (seconds_of(released_at) or 0) < 86_400
    return chip("Judged a day after its release" if young else "No report", "plain")


def toolbar(*parts):
    return f'<div class="toolbar">{"".join(part for part in parts if part)}</div>'


def segment(label, options, current):
    links = "".join(f'<a href="{esc(url)}"' + (' aria-current="page"' if value == current else "") + f">{esc(words)}</a>" for value, words, url in options)
    return f'<nav class="seg" aria-label="{esc(label)}">{links}</nav>'


def live_note(at, observer):
    if not observer:
        return '<span class="fresh"><span class="dot off"></span>Live views are off</span>'
    if at is None:
        return '<span class="fresh"><span class="dot off"></span>No live reading</span>'
    return f'<span class="fresh" title="{esc(LIVE_WORDS)}"><span class="dot"></span>Live, a few minutes behind · as of {esc(clock(at))}</span>'


def records_note(at):
    return f'<span class="fresh"><span class="dot off"></span>From the controller\'s records · as of {esc(clock(at))}</span>'


def refresh(url):
    return f'<a class="btn ghost refresh" href="{esc(url)}">{icon("refresh")}Refresh</a>'


def figure(title, chart, sub="", legend="", note="", numbers=""):
    return (f'<figure class="viz"><figcaption class="viz-head"><h3>{esc(title)}</h3>' + (f'<span class="sub">{sub}</span>' if sub else "")
            + f"</figcaption>{legend}{chart}" + (f'<p class="viz-note">{note}</p>' if note else "") + f"{numbers}</figure>")


def section(title, body, sub=""):
    return (f'<section class="card"><div class="card-head"><div><h2>{esc(title)}</h2>' + (f'<p class="sub">{sub}</p>' if sub else "")
            + f'</div></div><div class="card-body">{body}</div></section>')


def empty(words):
    return f'<p class="empty">{words}</p>'


def no_observer():
    return callout("gray", "Live views are off.", "<p>This controller has no observer identity (<code>observer.service_account</code>), so nothing is "
                                                  "read from Cloud Monitoring; what is shown comes from the controller's records. docs/SETUP.md says how "
                                                  "to add one.</p>")


def digest_words(digest):
    if digest.get("off"):
        return "The daily digest is off (<code>digest_hour_utc</code> is empty on the Platform tab), so there is no day-by-day record."
    if not digest.get("kept"):
        return (f"The daily digest has not run yet: it is written once a day at {digest.get('hour'):02d}:00 UTC, and the day-by-day record starts "
                "with the first one.")
    return None


def digest_note(digest):
    if digest.get("off"):
        return '<span class="fresh">Daily digest off</span>'
    if not digest.get("kept"):
        return f'<span class="fresh">No daily digest yet (written at {digest.get("hour"):02d}:00 UTC)</span>'
    return f'<span class="fresh">Daily record: the digest of {esc(day_words(digest.get("day")))}</span>'


def step_words(start, step, index):
    """The time a step covers, in UTC."""
    first, last = when(start + index * step), when(start + (index + 1) * step)
    if step >= 86_400:
        return f"{first.day} {first:%b %H:%M}–{last.day} {last:%b %H:%M}"
    return f"{first.day} {first:%b %H:%M}–{last:%H:%M}"


def axis(window):
    """Three time labels under a chart: where it starts, its middle, and now."""
    start, end = window["start"], window["end"]
    middle = (start + end) / 2
    if end - start <= 86_400:
        return [(0, clock(start)), (0.5, clock(middle).replace(" UTC", "")), (1, "now")]
    return [(0, day_words(start)), (0.5, day_words(middle)), (1, "now")]


def hours_value(hours):
    """A tile's duration: hours below two days, days above."""
    if hours < 48:
        return f"{hours:g}<small> h</small>"
    return f"{hours / 24:.1f}".rstrip("0").rstrip(".") + "<small> days</small>"


def tile(label, value, note="", chart="", wide=False):
    return (f'<div class="tile{" wide" if wide else ""}"><p class="label">{esc(label)}</p><p class="value">{value}</p>'
            + (f'<p class="note">{note}</p>' if note else "") + chart + "</div>")


# ---- Overview

def overview(frame, data):
    parts = [head("Overview", "How the applications your token may see are doing: what needs you first, then the numbers."),
             toolbar(live_note(data["live_at"], data["observer"]), digest_note(data["digest"]), refresh(href("overview")))]
    if not data["cards"]:
        parts.append(callout("gray", "This token sees no application.", "<p>The operator lists the applications a token may see under "
                                                                        "<code>principals</code> in the controller's configuration.</p>"))
        return shell(frame, "overview", "".join(parts))
    if not data["observer"]:
        parts.append(no_observer())
    parts.append(attention(data))
    parts.append(f'<div class="tiles">{tiles(data)}</div>')
    cards = "".join(app_card(item, data["as_of"]) for item in data["cards"])
    parts.append('<div class="section-head"><h2>Applications</h2><p class="sub">Each card: the last 24 hours of requests, the live release and how '
                 f'it did a day later. Open one for its health.</p></div><div class="apps">{cards}</div>')
    return shell(frame, "overview", "".join(parts))


def quiet_words(data):
    checked = data.get("checked") or {}
    said = ["no release did worse a day later"]
    if checked.get("live"):
        said.append("no service had server errors in the last 24 hours")
    if checked.get("spend"):
        said.append("no forecast is over its budget")
    if checked.get("drift"):
        said.append("the cloud setup matches the code")
    if checked.get("watchdog"):
        said.append("the watchdog found nothing")
    return "Nothing needs you: " + ", ".join(said[:-1]) + (" and " if len(said) > 1 else "") + said[-1] + "."


def attention(data):
    items = data["attention"]
    if not items:
        return card("Needs attention", f'<ul class="attention"><li class="ok">{icon("check")}<span>{esc(quiet_words(data))}</span></li></ul>', "All quiet.")
    rows = []
    for item in items:
        target = href(item["tab"], app=item.get("app"), env="production" if item.get("environment") == "production" else None)
        rows.append(f'<li class="{item["level"]}">{icon("error" if item["level"] == "bad" else "alert")}<span>{coded(item["text"])}</span>'
                    f'<a href="{esc(target)}">{TAB_WORDS[item["tab"]]}</a></li>')
    return card("Needs attention", f'<ul class="attention">{"".join(rows)}</ul>',
                f"{plural(len(items), 'thing')}, the worst first. Nothing here changes anything; each line says where to look.")


def tiles(data):
    found = data["tiles"]
    services = found["services"]
    if services["total"]:
        value = f'{services["healthy"]}<small> of {services["total"]}</small>'
        others = services["total"] - services["healthy"]
        note = "every service with a release" if not others else f"{plural(others, 'service')} to look at"
    else:
        value, note = "–", "no service has a release yet"
    parts = [tile("Services healthy", value, esc(note))]
    trust = found["trust"]
    if trust.get("change_failure_rate") is not None:
        failures = trust["restored_automatically"] + trust["rolled_back_by_a_person"]
        value, note = f"{trust['change_failure_rate'] * 100:.0f}%", f"{failures} of {plural(trust['reached_traffic'], 'release')} needed undoing"
    else:
        value, note = "–", "no release reached users in 30 days"
    parts.append(tile("Change failure rate, 30 days", value, esc(note)))
    counts = found["releases"]
    change = counts["week"] - counts["previous"]
    note = "as many as the week before" if not change else f"{abs(change)} {'more' if change > 0 else 'fewer'} than the week before"
    start = int(data["as_of"] // 86_400 * 86_400) - 13 * 86_400
    tips = [f"{day_words(start + index * 86_400)}: {plural(value, 'release')} reached users" for index, value in enumerate(counts["daily"])]
    chart = charts.minibars("tile-releases", "Releases that reached users on each of the last 14 days",
                            f"{counts['week']} in the last 7 days and {counts['previous']} in the 7 days before.", counts["daily"], tips,
                            {index: charts.QUIET for index in range(7)})
    parts.append(tile("Releases this week", str(counts["week"]), esc(note) + "; the week before in grey", chart, wide=True))
    parts.append(spend_tile(found["spend"]))
    return "".join(parts)


def spend_tile(spend):
    if spend is None:
        return tile("Spend this month", "–", "Shown to an identity that sees every application.", wide=True)
    if spend.get("missing"):
        return tile("Spend this month", "–", "The daily digest's cost line appears with the first digest.", wide=True)
    projects = [item for item in spend.get("projects") or [] if item.get("net_cost_usd") is not None]
    if not projects:
        return tile("Spend this month", "–", esc(spend.get("warning") or "No project's spend was read."), wide=True)
    total = sum(item["net_cost_usd"] for item in projects)
    rows = []
    for item in projects:
        forecast = item.get("forecast_usd")
        name = f"{item['environment']} · {money(item['net_cost_usd'])}" + (f", on course for {money(forecast)}" if forecast not in (None, item["net_cost_usd"]) else "")
        rows.append(f'<p class="bullet-name">{esc(name)}</p>'
                    + charts.bullet(f"tile-spend-{item['project']}", f"{item['project']}: spend this month",
                                    bullet_words(item), item["net_cost_usd"], forecast, item.get("budget_usd"), item.get("native_cap_usd"), money=money))
    note = f"through {esc(day_words(spend.get('generated_at')))}" + ("; early in the month, so the forecast is rough" if spend.get("rough") else "")
    return tile("Spend this month", money(total), note, "".join(rows), wide=True)


def bullet_words(item):
    parts = [f"{money(item['net_cost_usd'])} so far"]
    if item.get("forecast_usd") is not None:
        parts.append(f"on course for {money(item['forecast_usd'])}")
    parts.append(f"budget {money(item['budget_usd'])}" if item.get("budget_usd") else "no budget recorded")
    parts.append(f"cap {money(item['native_cap_usd'])}" if item.get("native_cap_usd") else "no cap recorded")
    return "; ".join(parts) + "."


def app_card(item, now):
    levels = [entry["level"] for entry in item["services"]]
    worst = "bad" if "bad" in levels else "warn" if "warn" in levels else "ok" if "ok" in levels else "idle"
    head_html = (f'<div class="app-head"><a href="{esc(href("health", app=item["application"]))}">{esc(item["application"])}</a>'
                 f'<span class="chips" style="margin:0">{level_chip(worst)}{chip("Paused", "warn") if item["paused"] else ""}</span></div>')
    services = []
    two = len(item["services"]) > 1
    for entry in item["services"]:
        parts = [f'<p class="env">{esc(entry["environment"])}</p>' if two else ""]
        live = entry.get("live")
        ident = f"spark-{item['application']}-{entry['environment']}"
        if live:
            tips = [f"{step_words(live['start'], live['step'], index)} UTC: {plural(value, 'request')}" for index, value in enumerate(live["hourly"])]
            parts.append(charts.sparkline(ident, f"{item['application']} ({entry['environment']}): requests in each of the last 24 hours",
                                          f"{live['requests']:,} requests in 24 hours, {live['server_errors']:,} of them server errors.", live["hourly"], tips))
            parts.append(f'<p class="stats"><span><strong>{live["requests"]:,}</strong> requests</span><span><strong>{live["server_errors"]:,}</strong> '
                         f'server errors</span><span>95% within <strong>{esc(milliseconds(live["p95_ms"]))}</strong></span></p>')
        elif entry.get("live_error"):
            parts.append(f'<p class="stats">Live numbers not read: {esc(entry["live_error"][:140])}</p>')
        release = entry.get("release")
        if release:
            parts.append(f'<p class="release-line">Live: {commit(release["sha"], release["message"], 48)} · {esc(age(release["at"], now))}</p>'
                         f'<p class="release-line">A day later: {verdict_chip(release.get("report"), release.get("at"), now)}</p>')
        # The verdict has its own line above; the paused state its chip.
        reasons = [reason for reason in entry["reasons"] if "did worse than the version before" not in reason and not reason.startswith("paused")]
        if reasons:
            parts.append('<ul class="reasons">' + "".join(f"<li>{esc(reason)}</li>" for reason in reasons) + "</ul>")
        services.append(f'<div class="svc">{"".join(parts)}</div>')
    return f'<article class="app">{head_html}{"".join(services)}</article>'


# ---- Health

def health_list(frame, data):
    parts = [head("Health", "Pick an application: its traffic, errors, latency and saturation, its uptime over 30 days and its newest error lines."),
             toolbar(live_note(data["live_at"], data["observer"]), refresh(href("health")))]
    if not data["cards"]:
        parts.append(callout("gray", "This token sees no application.", ""))
    else:
        parts.append(f'<div class="apps">{"".join(app_card(item, data["as_of"]) for item in data["cards"])}</div>')
    return shell(frame, "health", "".join(parts))


def markers_for(data):
    found = []
    for item in data.get("markers") or []:
        verdict = VERDICTS.get(item.get("verdict"), ("not judged yet", ""))[0].lower()
        words = f"Release {short(item['sha'])} at {stamp(item['moment'])} UTC: {OUTCOMES.get(item['outcome'], item['outcome']).lower()}; a day later: {verdict}"
        found.append({"at": item["at"], "tip": words, "color": charts.BAD if item.get("verdict") == "worse" else charts.INK})
    return found


def health(frame, data):
    app, environment, range_key = data["application"], data["environment"], data["range"]
    crumb = f'<a class="crumb" href="{esc(href("overview"))}">{icon("back")}Overview</a>'
    chips_html = level_chip(data["level"]) + chip(f"{environment.capitalize()} · {data['service']} in {data['project']}", "plain")
    parts = [head(app, f"Traffic, errors, latency and saturation of {esc(app)} in {esc(environment)}, with its releases marked.", chips_html, crumb)]
    options = [(key, key, href("health", app=app, env=environment if environment != "staging" else None, range=key)) for key in RANGES]
    envs = [(name, name.capitalize(), href("health", app=app, env=name if name != "staging" else None, range=range_key)) for name in data["environments"]]
    read = [part["read_at"] for part in (data.get("traffic"), data.get("saturation")) if isinstance(part, dict) and part.get("read_at")]
    parts.append(toolbar(segment("Environment", envs, environment) if len(envs) > 1 else "", segment("Range", options, range_key),
                         live_note(min(read) if read else None, data["observer"]),
                         refresh(href("health", app=app, env=environment if environment != "staging" else None, range=range_key))))
    if data["reasons"] and data["level"] in ("warn", "bad"):
        parts.append(callout("amber" if data["level"] == "warn" else "red", "What needs a look", "<ul>" + "".join(f"<li>{esc(reason)}</li>" for reason in data["reasons"]) + "</ul>"))
    parts.append(release_card(data))
    if not data["observer"]:
        parts.append(no_observer())
        parts.append(links_card(data))
        return shell(frame, "health", "".join(parts), title=f"{app} · Health · Fast Deploy")
    markers = markers_for(data)
    words = RANGES[range_key][0]
    parts.append(section("Traffic and errors", traffic_figures(data, markers, words), f"Requests that reached {esc(app)}, {esc(words)}; a flag marks each release."))
    parts.append(section("Latency", latency_figure(data, markers, words), "How long 95 of every 100 requests took to answer."))
    parts.append(section("Saturation", f'<div class="pair">{saturation_figures(data, markers, words)}</div>', "How close it runs to its limits."))
    parts.append(section("Uptime, 30 days", uptime_figure(data), "Google's uptime check calls the health path from outside every minute, from several "
                                                                 "places: it sees what the service's own numbers cannot, such as a spending cap closing it."))
    if environment == "production":
        parts.append(section("Error budget", budget_body(data), "Production's objective: the share of users' requests that must succeed over 30 days."))
    parts.append(logs_card(data))
    parts.append(links_card(data))
    return shell(frame, "health", "".join(parts), title=f"{app} · Health · Fast Deploy")


def release_card(data):
    release = data.get("release")
    if not release:
        return section("Live release", empty("No release recorded by this controller yet: nothing it released serves here."))
    rows = [("Commit", commit(release["sha"], release["message"])), ("Released", f"{esc(stamp(release['at']))} UTC · {esc(age(release['at'], data['as_of']))}"),
            ("Revision", f'<span class="mono">{esc(release.get("revision") or "unknown")}</span>'),
            ("A day later", verdict_chip(release.get("report"), release.get("at"), data["as_of"]))]
    facts = "".join(f"<div><dt>{esc(term)}</dt><dd>{value}</dd></div>" for term, value in rows)
    return f'<section class="card"><div class="card-body"><dl class="strip">{facts}</dl></div></section>'


def classes_of(step):
    found = (step or {}).get("classes") or {}
    return {"answered": sum(value for name, value in found.items() if name not in ("4xx", "5xx")), "4xx": found.get("4xx", 0), "5xx": found.get("5xx", 0)}


def unreadable(part, what):
    return empty(f"{esc(what)} could not be read: {esc(part.get('error', 'no answer'))}. The page tries again within a minute.")


def traffic_figures(data, markers, words):
    traffic, window = data.get("traffic") or {}, data["window"]
    if "error" in traffic:
        return unreadable(traffic, "Traffic")
    steps = [classes_of(step) for step in traffic.get("steps") or []]
    start, step = window["start"], window["step"]
    series = [("answered", "Answered (2xx, 3xx)", charts.QUIET), ("4xx", "Refused, the caller's mistake (4xx)", charts.WARN),
              ("5xx", "Server errors (5xx)", charts.BAD)]
    total = sum(sum(item.values()) for item in steps)
    errors = sum(item["5xx"] for item in steps)
    refused = sum(item["4xx"] for item in steps)
    tips = [f"{step_words(start, step, index)} UTC: {item['answered']:,} answered, {item['4xx']:,} refused, {plural(item['5xx'], 'server error')}"
            for index, item in enumerate(steps)]
    legend = charts.legend([(label, colour, "box") for _, label, colour in series] + ([("Release", charts.INK, "flag")] if markers else []))
    ident = f"traffic-{data['application']}-{data['environment']}"
    traffic_chart = charts.stacked_bars(ident, f"Requests to {data['application']} by answer, {words}",
                                        f"{total:,} requests: {total - errors - refused:,} answered, {refused:,} refused, {errors:,} server errors.",
                                        steps, series, "requests", tips, axis(window), markers)
    table = charts.numbers(["Period (UTC)", "Answered", "Refused (4xx)", "Server errors (5xx)"],
                           [(step_words(start, step, index), f"{item['answered']:,}", f"{item['4xx']:,}", f"{item['5xx']:,}") for index, item in enumerate(steps)],
                           numeric={1, 2, 3}, caption="Requests by answer")
    note = "No request reached it in this range." if not total else f"{total:,} requests; {share(errors, total)} were server errors."
    first = figure("Requests by answer", traffic_chart, f"{total:,} in {esc(words)}", legend, esc(note), table)
    error_values = [item["5xx"] for item in steps]
    alerts = data["alerts"]
    alert_words = (f"An alert fires when more than {alerts['errors_5min']:,} server errors come in five minutes." if alerts["on"]
                   else "Runtime alerts are off for this application, so no alert watches these.")
    error_chart = charts.bars(f"errors-{data['application']}-{data['environment']}", f"Server errors (5xx) of {data['application']}, {words}",
                              f"{errors:,} server errors in {words}.", error_values, "errors", charts.BAD,
                              [f"{step_words(start, step, index)} UTC: {plural(value, 'server error')}" for index, value in enumerate(error_values)],
                              axis(window), markers, height=130)
    table = charts.numbers(["Period (UTC)", "Server errors"], [(step_words(start, step, index), f"{value:,}") for index, value in enumerate(error_values)],
                           numeric={1}, caption="Server errors")
    second = figure("Server errors (5xx)", error_chart, f"{errors:,} in {esc(words)}", "", esc(alert_words), table)
    return first + second


def share(part, whole):
    if not whole:
        return "none"
    value = part / whole * 100
    return "none" if not part else f"{value:.2f}%".replace(".00%", "%") if value < 10 else f"{value:.0f}%"


def latency_figure(data, markers, words):
    traffic, window = data.get("traffic") or {}, data["window"]
    if "error" in traffic:
        return unreadable(traffic, "Latency")
    values = [(step or {}).get("p95_ms") for step in traffic.get("steps") or []]
    start, step = window["start"], window["step"]
    alerts = data["alerts"]
    thresholds = [(alerts["latency_ms"], f"alert at {alerts['latency_ms']:,} ms")] if alerts["on"] else []
    _, _, hidden = charts.scale(values, thresholds)
    tips = [f"{step_words(start, step, index)} UTC: " + (f"95% within {round(value):,} ms" if value is not None else "no requests")
            for index, value in enumerate(values)]
    legend = charts.legend([("95th percentile", charts.ACCENT, "line")] + ([("Alert line", charts.INK, "dash")] if thresholds and not hidden else [])
                           + ([("Release", charts.INK, "flag")] if markers else []))
    chart = charts.line(f"latency-{data['application']}-{data['environment']}", f"95th-percentile latency of {data['application']}, {words}",
                        f"Over the whole range 95% of requests answered within {milliseconds(traffic.get('p95_ms'))}.", values, "ms", charts.ACCENT,
                        tips, axis(window), markers, thresholds)
    notes = [f"Over the whole range, 95% answered within {milliseconds(traffic.get('p95_ms'))}."]
    if hidden:
        notes.append(f"The alert line, {alerts['latency_ms']:,} ms for five minutes, is far above every step, so it is not drawn.")
    elif thresholds:
        notes.append(f"The alert fires when the 95th percentile stays above {alerts['latency_ms']:,} ms for five minutes.")
    else:
        notes.append("Runtime alerts are off for this application.")
    table = charts.numbers(["Period (UTC)", "95% within"], [(step_words(start, step, index), milliseconds(value)) for index, value in enumerate(values)],
                           numeric={1}, caption="Latency")
    return figure("95th-percentile latency", chart, "", legend, esc(" ".join(notes)), table)


def saturation_figures(data, markers, words):
    saturation, window = data.get("saturation") or {}, data["window"]
    if "error" in saturation:
        return unreadable(saturation, "Saturation")
    start, step = window["start"], window["step"]
    alerts, maximum = data["alerts"], data.get("maximum")
    instances = saturation.get("instances") or []
    thresholds = []
    if maximum is not None:
        thresholds.append((maximum, f"at most {maximum}"))
    if alerts["on"] and alerts["instances"] != maximum:
        thresholds.append((alerts["instances"], f"alert at {alerts['instances']}"))
    floor = max([maximum or 0, *(value or 0 for value in instances)]) or 1
    chart = charts.line(f"instances-{data['application']}-{data['environment']}", f"Instances serving {data['application']}, {words}",
                        f"The most instances at once in each step; it may run at most {maximum if maximum is not None else 'an unknown number of'}.",
                        instances, "instances", charts.ACCENT,
                        [f"{step_words(start, step, index)} UTC: " + (f"at most {plural(round(value), 'instance')} at once" if value is not None else "none")
                         for index, value in enumerate(instances)],
                        axis(window), markers, thresholds, height=150, floor_top=floor)
    legend = charts.legend([("Most at once", charts.ACCENT, "line"), ("Its limit", charts.INK, "dash")])
    note = (f"scaling.maximum is {maximum}" if maximum is not None else "The live release's maximum is not recorded") + (
        f"; an alert fires at {alerts['instances']} or more for five minutes." if alerts["on"] else ".")
    table = charts.numbers(["Period (UTC)", "Instances"], [(step_words(start, step, index), "–" if value is None else f"{value:g}") for index, value in enumerate(instances)],
                           numeric={1}, caption="Instances")
    first = figure("Instances", chart, "", legend, esc(note), table)
    memory = [None if value is None else round(value * 100, 1) for value in saturation.get("memory") or []]
    chart = charts.line(f"memory-{data['application']}-{data['environment']}", f"Memory use of {data['application']}, {words}",
                        "The 95th percentile of memory use in each step, as a share of the limit; the alert line is 80 percent.", memory, "%", charts.ACCENT,
                        [f"{step_words(start, step, index)} UTC: " + (f"{value:g}% of the limit" if value is not None else "no instance") for index, value in enumerate(memory)],
                        axis(window), markers, [(80, "alert at 80%")] if alerts["on"] else [], height=150, floor_top=100)
    legend = charts.legend([("95th percentile", charts.ACCENT, "line")] + ([("Alert line", charts.INK, "dash")] if alerts["on"] else []))
    table = charts.numbers(["Period (UTC)", "Memory used"], [(step_words(start, step, index), "–" if value is None else f"{value:g}%") for index, value in enumerate(memory)],
                           numeric={1}, caption="Memory")
    second = figure("Memory, share of the limit", chart, "", legend, "Above 80% for five minutes is the memory alert; the right-sizing report proposes more.", table)
    return first + second


def uptime_figure(data):
    uptime = data.get("uptime") or {}
    if "error" in uptime:
        return unreadable(uptime, "The uptime check")
    days = uptime.get("days") or []
    if not days or all(day.get("passed") is None for day in days):
        return empty("No uptime check has reported for this service in 30 days: its infrastructure creates one with the runtime alerts.")
    today = days[-1]["day"]
    shown = []
    for day in days:
        label = "today so far" if day["day"] == today else day_words(day["day"])
        passed = day.get("passed")
        shown.append({**day, "tip": f"{label}: " + (f"{percent(passed)} of checks passed" if passed is not None else "nothing measured")})
    measured = [day["passed"] for day in days if day.get("passed") is not None]
    mean = sum(measured) / len(measured)
    trouble = [day for day in days if charts.uptime_level(day.get("passed")) in ("warning", "bad")]
    strip = charts.uptime_strip(f"uptime-{data['application']}-{data['environment']}", f"Uptime of {data['application']}, the last 30 days",
                                f"{percent(mean)} of checks passed on the days measured; {plural(len(trouble), 'day')} below 99.9%.", shown,
                                "30 days ago", "today")
    legend = charts.legend([("99.9% or more", charts.GOOD, "box"), ("99% to 99.9%", charts.WARN, "box"), ("Below 99%", charts.BAD, "box"),
                            ("Not measured", charts.EMPTY, "box")])
    words = f"{percent(mean)} of checks passed over the {plural(len(measured), 'day')} measured."
    if trouble:
        words += " Below 99.9% (marked with a caret): " + ", ".join(f"{day_words(day['day'])} ({percent(day['passed'])})" for day in trouble[:6]) + "."
    table = charts.numbers(["Day (UTC)", "Checks passed"], [(day_words(day["day"]), "not measured" if day.get("passed") is None else percent(day["passed"]))
                                                            for day in days], numeric={1}, caption="Uptime by day")
    return figure("Uptime check, by day", strip, "", legend, esc(words), table)


def budget_body(data):
    budget = data.get("budget")
    if budget is None:
        return empty("No objective in production yet: ask Claude for <code>add_production</code>, which proposes the production root with its objective.")
    if "error" in budget:
        return unreadable(budget, "The error budget")
    left = budget.get("budget_remaining")
    if left is None:
        return empty("The objective exists, but Cloud Monitoring has no figure for it yet.")
    colour = charts.GOOD if left >= 0.5 else charts.WARN if left >= 0.25 else charts.BAD
    words = (f"{left * 100:.0f}% of the {budget.get('days', 30)}-day error budget is left" if left > 0 else
             "The error budget is spent" + (f", {-left * 100:.0f}% beyond it" if left < 0 else ""))
    meter = charts.meter(f"budget-{data['application']}", words, f"Goal {budget.get('goal')}; compliance {budget.get('compliance')}.", left, colour)
    details = []
    if budget.get("requests_counted") is not None:
        details.append(f"{budget['requests_counted']:,} users' requests counted, {budget.get('errors_counted', 0):,} of them server errors")
    if budget.get("goal") is not None:
        details.append(f"the goal is {budget['goal'] * 100:g}% good")
    if budget.get("clears_by") and left < 0.25:
        details.append(f"its newest error leaves the window by {day_words(budget['clears_by'])} if no new one comes")
    note = esc(words + (": " + "; ".join(details) if details else "") + ".")
    return f'<div class="viz">{meter}<p class="viz-note">{note}</p></div>'


def logs_card(data):
    app, environment, range_key = data["application"], data["environment"], data["range"]
    logs = data.get("logs")
    reason = ("Read only when you ask: Cloud Logging allows 60 reads a minute per project, shared with the release reports and the digest. "
              "The newest 20 error lines of the range, kept a minute.")
    if logs is None:
        url = href("health", app=app, env=environment if environment != "staging" else None, range=range_key, logs=1) + "#logs"
        body = f'<p class="viz-note" style="margin:0 0 12px">{esc(reason)}</p><a class="btn secondary" href="{esc(url)}">Read the newest error lines</a><div style="height:16px"></div>'
    elif "error" in logs:
        body = unreadable(logs, "The log")
    elif not logs.get("lines"):
        body = empty(f"No error line in {esc(RANGES[range_key][0])}.")
    else:
        lines = "".join(f'<li><span class="muted nowrap">{esc(stamp(line.get("at")))}</span><span class="msg">{esc(line.get("message"))}</span></li>'
                        for line in logs["lines"])
        body = f'<ul class="loglines">{lines}</ul><p class="viz-note" style="margin-bottom:14px">Read at {esc(clock(logs.get("read_at")))}.</p>'
    return f'<div id="logs">{section("Newest error lines", body, esc(RANGES[range_key][0].capitalize()) + ", at ERROR and above.")}</div>'


def links_card(data):
    links = data["links"]
    items = [("Cloud Run metrics", links["run"]), ("Monitoring dashboards", links["dashboards"]), ("Logs Explorer", links["logs"])]
    body = '<div class="links">' + "".join(f'<a class="btn secondary" href="{esc(url)}">{esc(words)}{icon("external")}</a>' for words, url in items) + "</div>"
    return section("In the Google Cloud console", body, "The same service in Google's own tools, for anything this page does not show. Its dashboard "
                                                        f"is named “{esc(data['service'])} {esc(data['environment'])}”.")


# ---- Releases

STYLES = {"live-better": ("circle", charts.GOOD, "Reached users, better a day later"), "live-same": ("circle", charts.INK, "Reached users, about the same"),
          "live-worse": ("triangle", charts.BAD, "Reached users, worse a day later"), "live-none": ("ring", charts.INK, "Reached users, not compared"),
          "restored": ("diamond", charts.WARN, "Went back on its own"), "rolled_back": ("cross", charts.BAD, "Rolled back by a person"),
          "stopped": ("square", charts.QUIET, "Stopped before reaching anyone"), "in_progress": ("ring", charts.QUIET, "On its way")}


def style_of(row):
    if row["outcome"] == "live":
        return {"better": "live-better", "same": "live-same", "worse": "live-worse"}.get(row.get("verdict"), "live-none")
    return row["outcome"] if row["outcome"] in STYLES else "in_progress"


def releases(frame, data):
    trust = data["trust"]["overall"]
    parts = [head("Releases", f"Every release, promotion and rollback this controller recorded for the applications your token may see, and the "
                              f"four DORA numbers over {data['days']} days."), toolbar(records_note(data["as_of"]), refresh(href("releases")))]
    if not data["applications"]:
        parts.append(callout("gray", "This token sees no application.", ""))
        return shell(frame, "releases", "".join(parts))
    weekly = data["weekly"]
    labels = [f"the week to {day_words(item['end'])}" for item in weekly]
    reached = [item["reached"] for item in weekly]
    per_week = trust["reached_traffic"] / (data["days"] / 7)
    tiles_html = [tile("Releases that reached users", f"{per_week:.1f}<small> a week</small>", f"{trust['reached_traffic']} in {data['days']} days",
                       charts.minibars("dora-frequency", "Releases that reached users, each of the last 8 weeks", ", ".join(map(str, reached)) + ".", reached,
                                       [f"{label}: {plural(value, 'release')}" for label, value in zip(labels, reached, strict=True)],
                                       {index: charts.QUIET for index in range(len(reached) - 1)}))]
    rates = [item["rate"] for item in weekly]
    rate_value = "–" if trust["change_failure_rate"] is None else f"{trust['change_failure_rate'] * 100:.0f}%"
    failures = trust["restored_automatically"] + trust["rolled_back_by_a_person"]
    tiles_html.append(tile("Change failure rate", rate_value, f"{failures} needed undoing: {trust['restored_automatically']} on their own, "
                                                              f"{trust['rolled_back_by_a_person']} by a person",
                           charts.minibars("dora-failures", "The share of releases that needed undoing, each of the last 8 weeks",
                                           ", ".join("none" if rate is None else f"{rate * 100:.0f}%" for rate in rates) + ".",
                                           [round((rate or 0) * 100) for rate in rates],
                                           [f"{label}: " + ("no release" if rate is None else f"{rate * 100:.0f}% of {plural(item['reached'], 'release')}")
                                            for label, rate, item in zip(labels, rates, weekly, strict=True)],
                                           {index: charts.QUIET for index in range(len(rates) - 1)}, charts.BAD)))
    recovery = trust.get("recovery_hours_median")
    tiles_html.append(tile("Back to a good release", "–" if recovery is None else hours_value(recovery),
                           "median, after a release that needed undoing" if recovery is not None else "nothing needed undoing"))
    lead = data.get("lead_hours")
    tiles_html.append(tile("From commit to live", "–" if lead is None else hours_value(lead),
                           f"median of {plural(data['leads'], 'release')}" if lead is not None else "no release with a commit time"))
    parts.append(f'<div class="tiles">{"".join(tiles_html)}</div>')
    parts.append(section("Timeline", timeline_figure(data), f"The last {data['days']} days. Each shape is a release or promotion: its shape and colour "
                                                            "say what became of it."))
    parts.append(release_table(data))
    return shell(frame, "releases", "".join(parts))


def timeline_figure(data):
    services = data["services"]
    if not services:
        return empty(f"No release in the last {data['days']} days.")
    rows = [f"{app} · production" if environment == "production" else app for app, environment in services]
    index = {service: number for number, service in enumerate(services)}
    span_seconds = data["as_of"] - data["since"]
    marks, used = [], set()
    for row in data["rows"]:
        key = (row["application"], row["environment"])
        at = row["finished"] or row["started"]
        if row["kind"] not in ("release", "promotion") or key not in index or not at or at < data["since"]:
            continue
        name = style_of(row)
        used.add(name)
        shape, colour, words = STYLES[name]
        marks.append({"row": index[key], "at": (at - data["since"]) / span_seconds, "shape": shape, "color": colour,
                      "tip": f"{row['application']} {short(row['sha'])}, {stamp(at)} UTC: {words.lower()}"})
    legend = charts.legend([(STYLES[name][2], STYLES[name][1], STYLES[name][0]) for name in STYLES if name in used])
    chart = charts.timeline("release-timeline", f"Releases of the last {data['days']} days", f"{len(marks)} releases and promotions across {plural(len(rows), 'service')}.",
                            rows, marks, [(0, day_words(data["since"])), (0.5, day_words((data["since"] + data["as_of"]) / 2)), (1, "now")])
    table = charts.numbers(["When (UTC)", "Service", "Commit", "What became of it"],
                           [(stamp(row["finished"] or row["started"]), row["application"] + (" · production" if row["environment"] == "production" else ""),
                             short(row["sha"]), STYLES[style_of(row)][2]) for row in data["rows"]
                            if row["kind"] in ("release", "promotion") and (row["finished"] or row["started"] or 0) >= data["since"]], caption="Releases")
    return figure("Releases and promotions", chart, "", legend, "", table)


def release_table(data):
    rows = []
    for row in data["rows"][:60]:
        environment = " · production" if row["environment"] == "production" else ""
        what = {"release": "Release", "promotion": "Promotion", "rollback": "Rollback", "production_rollback": "Rollback"}.get(row["kind"], row["kind"])
        verdict = verdict_chip({"verdict": row["verdict"]} if row["verdict"] else None, row["finished"], data["as_of"]) if row["outcome"] == "live" else ""
        run = f'<a href="{esc(row["run_url"])}" aria-label="The GitHub run">Run{icon("external")}</a>' if row.get("run_url") else '<span class="muted">–</span>'
        rows.append(f'<tr><td class="nowrap" data-label="Finished (UTC)">{esc(stamp(row["finished"] or row["started"]))}</td>'
                    f'<td class="nowrap" data-label="Application">{esc(row["application"])}{environment}<span class="sub">{what}</span></td>'
                    f'<td class="wrap wide" data-label="Commit">{commit(row["sha"], row["message"], 70)}</td><td data-label="Who asked">{esc(row["who"] or "unknown")}</td>'
                    f'<td class="r num" data-label="Took">{esc(span(row["seconds"]))}</td><td class="wrap outcome wide" data-label="What became of it">'
                    f'{esc(OUTCOMES.get(row["outcome"], row["outcome"]))}{"<br>" + verdict if verdict else ""}</td><td data-label="GitHub">{run}</td></tr>')
    if not rows:
        return section("Every release", empty("No release recorded by this controller yet."))
    table = ('<div class="table-scroll"><table class="list stack"><thead><tr><th>Finished (UTC)</th><th>Application</th><th>Commit</th><th>Who asked</th>'
             '<th class="r">Took</th><th>What became of it</th><th>GitHub</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>')
    more = f" The newest 60 of {len(data['rows'])}." if len(data["rows"]) > 60 else ""
    return (f'<section class="card flush"><div class="card-head"><div><h2>Every release</h2><p class="sub">Newest first, the last 90 days.{more}</p></div></div>'
            f'<div class="card-body">{table}</div></section>')


# ---- Cost

def cost(frame, data):
    position = data.get("position") or {}
    read = any(item.get("net_cost_usd") is not None for item in position.get("projects") or [])
    through = position.get("generated_at") if read else None  # "cost through" only where spend was read
    note = (f'<span class="fresh" title="The daily digest reads the billing export once a day; Google\'s export lands hours to a day late.">'
            f'<span class="dot off"></span>Cost through {esc(day_words(through))}</span>') if through else ""
    parts = [head("Cost", "What the month costs so far against the budgets and caps you recorded, where the money goes, and what could cost less. "
                          "Nothing here changes anything."), toolbar(note, records_note(data["as_of"]), refresh(href("cost")))]
    if not data["everything"]:
        parts.append(callout("gray", "Each project's spend is shown to an identity that sees every application.",
                             "<p>Below: the weekly reports and the bounds of the applications this token may see.</p>"))
    else:
        parts.append(projects_section(data))
    parts.append(breakdown_section(data))
    parts.append(proposals_section(data))
    parts.append(bounds_section(data))
    return shell(frame, "cost", "".join(parts))


def projects_section(data):
    position = data.get("position") or {}
    digest = data["digest"]
    if position.get("missing"):
        return section("This month", empty(digest_words(digest) or "The newest digest holds no cost line."))
    projects = position.get("projects") or []
    if not any(item.get("net_cost_usd") is not None for item in projects):
        why = position.get("warning") or "no project's spend was read"
        if not data["billing_export"]:
            why = ("there is no billing export recorded (<code>onboarding.billing_export_table</code>), so spend is not read; docs/SETUP.md says how "
                   "to turn on the export")
        return section("This month", empty(f"No spend to show: {why}."))
    figures = []
    month_start = datetime.datetime.strptime(data["month"] + "-01", "%Y-%m-%d").replace(tzinfo=datetime.UTC).timestamp()
    days = position.get("days_in_month") or 30
    for item in projects:
        if item.get("net_cost_usd") is None:
            continue
        project = item["project"]
        points = [((at - month_start) / (days * 86_400), value, f"{stamp(at)} UTC: {money(value)} so far") for at, value in data.get("month_series", {}).get(project, [])]
        forecast = (1.0, item["forecast_usd"], f"On course for {money(item['forecast_usd'])} by the end of the month") if item.get("forecast_usd") is not None else None
        thresholds = []
        if item.get("budget_usd"):
            thresholds.append((item["budget_usd"], f"budget {money(item['budget_usd'])}"))
        if item.get("native_cap_usd"):
            thresholds.append((item["native_cap_usd"], f"cap {money(item['native_cap_usd'])}"))
        if len(thresholds) == 2 and thresholds[0][0] == thresholds[1][0]:
            thresholds = [(thresholds[0][0], f"budget and cap {money(thresholds[0][0])}")]
        bullet = charts.bullet(f"bullet-{project}", f"{project}: spend this month", bullet_words(item), item["net_cost_usd"], item.get("forecast_usd"),
                               item.get("budget_usd"), item.get("native_cap_usd"), money=money)
        line = charts.progress(f"month-{project}", f"{project}: spend so far this month, day by day", bullet_words(item), points, forecast, thresholds,
                               [(0, f"1 {datetime.datetime.fromtimestamp(month_start, datetime.UTC):%b}"), (1, "month end")], "USD")
        legend = charts.legend([("So far", charts.ACCENT, "line"), ("Forecast", charts.ACCENT, "dash"), ("Budget and cap", charts.INK, "dash")])
        table = charts.numbers(["Read at (UTC)", "Month so far"], [(stamp(at), money(value)) for at, value in data.get("month_series", {}).get(project, [])],
                               numeric={1}, caption=f"{project} spend so far")
        over = " It is on course to pass its budget." if item.get("over_budget") else ""
        words = (f"{money(item['net_cost_usd'])} so far, on course for {money(item.get('forecast_usd'))}." + over
                 + ("" if item.get("budget_usd") else " No budget is recorded for it (Platform tab, “Budgets you created”).")
                 + ("" if item.get("native_cap_usd") else " No native spend cap is recorded for it."))
        figures.append(figure(f"{project} · {item['environment']}", bullet + line, esc(money(item["net_cost_usd"])), legend, esc(words), table))
    rough = " Early in the month, so the forecasts are rough." if position.get("rough") else ""
    sub = (f"From the daily digest of {esc(day_words(position.get('day')))}: the month so far, net of credits, from Google's billing export, which lands "
           f"hours to a day late; the forecast scales it to the whole month.{rough}")
    return section("This month", "".join(figures), sub)


def newest_reports(data):
    """The weekly reports to draw the money from: per set of projects they cover, the newest, so a project shared by many
    applications is counted once."""
    chosen = {}
    for item in data["reports"]:
        report = ((item.get("result") or {}).get("report")) or {}
        actual = report.get("actual_spend") or {}
        if actual.get("by_service") is None:
            continue
        key = tuple(sorted(row.get("project") or "" for row in actual.get("by_project") or [])) or (report.get("project_id"),)
        if key not in chosen or str(report.get("generated_at")) > str(chosen[key][1].get("generated_at")):
            chosen[key] = (item["application"], report)
    return list(chosen.values())


def change_words(now, before):
    if before is None:
        return "new"
    change = now - before
    if abs(change) < 0.005:
        return "no change"
    arrow = "▲" if change > 0 else "▼"
    return f"{arrow} {money(abs(change))}"


def breakdown_section(data):
    chosen = newest_reports(data)
    if not chosen:
        reasons = [item.get("error") or ((item.get("result") or {}).get("note")) for item in data["reports"]]
        why = next((reason for reason in reasons if reason), "the right-sizing workflow runs on Mondays, or from GitHub Actions on demand")
        return section("Where the money goes", empty(f"No weekly cost report with the bill yet: {esc(why)}."))
    parts = []
    for application, report in chosen:
        actual = report["actual_spend"]
        before = actual.get("previous") or {}
        days = actual.get("window_days", 30)
        earlier = {row["service"]: row["net_cost_usd"] for row in before.get("by_service") or []}
        rows = [(row["service"], row["net_cost_usd"], f"{money(row['net_cost_usd'])} · {change_words(row['net_cost_usd'], earlier.get(row['service']))}",
                 f"{row['service']}: {money(row['net_cost_usd'])} in the last {days} days, {money(earlier.get(row['service'], 0))} in the {days} before")
                for row in (actual.get("by_service") or [])[:8]]
        visible = set(data["applications"]) if not data["everything"] else None
        earlier_apps = {row["app"]: row["net_cost_usd"] for row in before.get("by_app") or []}
        app_rows = [(row["app"], row["net_cost_usd"], f"{money(row['net_cost_usd'])} · {change_words(row['net_cost_usd'], earlier_apps.get(row['app']))}",
                     f"{row['app']}: {money(row['net_cost_usd'])} in the last {days} days, {money(earlier_apps.get(row['app'], 0))} in the {days} before")
                    for row in (actual.get("by_app") or []) if visible is None or row["app"] in visible][:8]
        projects = ", ".join(row.get("project") for row in actual.get("by_project") or []) or report.get("project_id")
        through = actual.get("data_through")
        sub = (f"{money(actual.get('total_net_cost_usd'))} in the last {days} days against {money(before.get('total_net_cost_usd'))} in the {days} before, "
               f"net of credits, in {projects}. From {application}'s weekly report of {day_words(report.get('generated_at'))}; "
               f"cost through {day_words(through)}.")
        parts.append(f'<p class="viz-note" style="margin:0 0 12px">{esc(sub)}</p>')
        service_chart = charts.hbars(f"by-service-{application}", f"Spend by Google service, the last {days} days", sub, rows)
        table = charts.numbers(["Google service", f"Last {days} days", f"The {days} before"],
                               [(row["service"], money(row["net_cost_usd"]), money(earlier.get(row["service"], 0))) for row in actual.get("by_service") or []],
                               numeric={1, 2}, caption="Spend by Google service")
        gone = [name for name in earlier if name not in {row["service"] for row in actual.get("by_service") or []} and earlier[name]]
        note = ("Gone since the window before: " + ", ".join(f"{name} ({money(earlier[name])})" for name in gone) + ".") if gone else ""
        parts.append(figure("By Google service", service_chart, "", "", esc(note), table))
        if app_rows:
            app_chart = charts.hbars(f"by-app-{application}", f"Spend by application label, the last {days} days", sub, app_rows)
            table = charts.numbers(["Application label", f"Last {days} days", f"The {days} before"],
                                   [(name, money(value), money(earlier_apps.get(name, 0))) for name, value, _, _ in app_rows], numeric={1, 2},
                                   caption="Spend by application")
            parts.append(figure("By application", app_chart, "", "", "Firestore and Cloud Run request fees carry no label and count as unlabelled.", table))
    return section("Where the money goes", "".join(parts), "From the weekly right-sizing report's bill: ▲ more and ▼ less than the window before.")


def proposals_section(data):
    rows = []
    for item in data["reports"]:
        report = ((item.get("result") or {}).get("report")) or {}
        for proposal in (report.get("rightsizing") or {}).get("recommendations") or []:
            saving = proposal.get("estimated_monthly_saving_usd")
            ask = f"Propose right-sizing for {item['application']}"
            rows.append(f'<tr><td data-label="Application">{esc(item["application"])}</td><td data-label="Setting"><code class="key">'
                        f'{esc(proposal.get("setting") or "note")}</code></td><td data-label="Now">{esc(proposal.get("current"))}</td>'
                        f'<td data-label="Proposed">{esc(proposal.get("recommended"))}</td><td class="wrap wide" data-label="Why">{esc(proposal.get("reason"))}</td>'
                        f'<td class="r num" data-label="Saves a month">{esc(money(saving) if saving is not None else "–")}</td>'
                        f'<td data-label="Next"><a href="https://claude.ai/new?q={quote(ask)}">Ask Claude{icon("external")}</a></td></tr>')
    if not rows:
        return section("Right-sizing proposals", empty("No proposal: the newest weekly reports found nothing to change, or none has run yet."))
    table = ('<div class="table-scroll"><table class="list stack"><thead><tr><th>Application</th><th>Setting</th><th>Now</th><th>Proposed</th><th>Why</th>'
             f'<th class="r">Saves a month</th><th></th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return (f'<section class="card flush"><div class="card-head"><div><h2>Right-sizing proposals</h2><p class="sub">From each application\'s newest weekly '
            "report, measured over 14 days. Asking Claude to “propose right-sizing for APP” opens a pull request a person reviews and merges; nothing "
            f'changes here.</p></div></div><div class="card-body">{table}</div></section>')


def bounds_section(data):
    bounds = data.get("bounds") or []
    if not bounds:
        return section("Each application's bounds", empty("No release recorded yet, so no bounds: each release records them from its reviewed configuration."))
    rows = []
    for item in bounds:
        name = item["application"] + (" · production" if item["environment"] == "production" else "")
        limit = item.get("limit")
        words = f"{money(item['floor'])} to {money(item['ceiling'])}" + (f" · ceiling {money(limit)}" if limit else "")
        tip = (f"{name}: {money(item['floor'])} a month with no visitors, at most {money(item['ceiling'])} with every allowed instance busy all month"
               + (f"; its cost ceiling is {money(limit)}" if limit else "; no cost ceiling"))
        rows.append((name, item["floor"], item["ceiling"], limit, words, tip))
    chart = charts.ranges("bounds", "Each application's monthly bounds against its cost ceiling", "From the live release's plan, at list prices.", rows)
    legend = charts.legend([("From no visitors to every instance busy", charts.ACCENT, "box"), ("Cost ceiling", charts.INK, "line")])
    table = charts.numbers(["Application", "No visitors", "Every instance busy", "Cost ceiling"],
                           [(row[0], money(row[1]), money(row[2]), money(row[3]) if row[3] else "none") for row in rows], numeric={1, 2, 3},
                           caption="Bounds")
    return section("Each application's bounds", figure("A month, from idle to busy", chart, "", legend,
                                                      "Bounds from list prices and the reviewed configuration, not a bill: a plan whose busy bound passes "
                                                      "the ceiling is refused.", table))


# ---- Activity

def activity(frame, data):
    filters = data["filters"]
    scope = {"everything": "Every event of the controller.", "applications": "Events about the applications your token may see, and your own.",
             "own": "Your own events: a read-only token sees only what it did."}[data["scope"]]
    parts = [head("Activity", f"The audit trail and the controller's own jobs. {scope}"), toolbar(records_note(data["as_of"]), refresh(href("activity", **filters)))]
    parts.append(filter_form(data))
    parts.append(section("Events per day", days_figure(data), "The last 30 days, under the filter above; configuration changes stand out."))
    parts.append(events_table(data))
    parts.append(controller_section(data))
    return shell(frame, "activity", "".join(parts))


def options(name, values, current, everyone, words=None):
    rendered = [f'<option value=""{" selected" if not current else ""}>{esc(everyone)}</option>']
    rendered += [f'<option value="{esc(value)}"{" selected" if value == current else ""}>{esc((words or {}).get(value, value))}</option>' for value in values]
    return f'<span class="select"><select id="filter-{name}" name="{name}">{"".join(rendered)}</select></span>'


def filter_form(data):
    choices, filters = data["choices"], data["filters"]
    fields = [("kind", "What", "Every kind", KINDS), ("identity", "Who", "Everyone", None), ("application", "Application", "Every application", None)]
    rendered = "".join(f'<label for="filter-{name}">{label}{options(name, choices[name], filters[name], everyone, words)}</label>'
                       for name, label, everyone, words in fields)
    clear = f'<a class="btn ghost" href="{esc(href("activity"))}">Clear</a>' if any(filters.values()) else ""
    return (f'<section class="card"><form class="filters" method="get" action="{PREFIX}"><input type="hidden" name="tab" value="activity">{rendered}'
            f'<span style="display:flex;gap:8px"><button class="btn secondary" type="submit">Show</button>{clear}</span></form></section>')


def days_figure(data):
    days = data["days"]
    steps = [{"other": day["events"] - day["configuration"], "configuration": day["configuration"]} for day in days]
    total = sum(day["events"] for day in days)
    if not total:
        return empty("No event in the last 30 days under this filter.")
    series = [("other", "Other events", charts.ACCENT), ("configuration", "Configuration changes", charts.SECOND)]
    tips = [f"{day_words(day['day'])}: {plural(day['events'], 'event')}" + (f", {day['configuration']} configuration change(s)" if day["configuration"] else "")
            for day in days]
    flags = [{"at": (index + 0.5) / len(days), "color": charts.SECOND, "line": False,
              "tip": f"{day_words(day['day'])}: {plural(day['configuration'], 'configuration change')}"}
             for index, day in enumerate(days) if day["configuration"]]
    chart = charts.stacked_bars("activity-days", "Events on each of the last 30 days", f"{total:,} events in 30 days.", steps, series, "events", tips,
                                [(0, day_words(days[0]["day"])), (0.5, day_words(days[len(days) // 2]["day"])), (1, "today")], flags, height=150)
    table = charts.numbers(["Day (UTC)", "Events", "Configuration changes"], [(day_words(day["day"]), day["events"], day["configuration"]) for day in days],
                           numeric={1, 2}, caption="Events per day")
    keys = [(label, colour, "box") for _, label, colour in series] + ([("A day with a configuration change", charts.SECOND, "flag")] if flags else [])
    return figure("Events", chart, f"{total:,} in 30 days", charts.legend(keys), "", table)


def event_what(item):
    kind, name = item.get("kind") or "", item.get("name") or ""
    words = KINDS.get(kind, kind.replace("_", " ").capitalize() or "Event")
    return words + (f": {name}" if name and kind in ("tool", "rest", "configuration", "drill") else "")


def events_table(data):
    rows = []
    for item in data["events"]:
        config = item.get("kind") == "configuration"
        detail = (item.get("detail") or "")[:300]
        rows.append(f'<tr{" class=\"config\"" if config else ""}><td class="nowrap" data-label="When (UTC)">{esc(stamp(item["at"]))}</td>'
                    f'<td data-label="What">{chip("Configuration", "warn") if config else ""}{esc(event_what(item)) if not config else " " + esc(item.get("name") or "")}</td>'
                    f'<td data-label="Who">{esc(item.get("identity") or "–")}</td><td data-label="Application">{esc(item.get("application") or "–")}</td>'
                    f'<td data-label="Outcome">{esc(item.get("outcome") or "–")}</td>'
                    f'<td class="wrap wide{"" if detail else " none"}" data-label="Detail">{esc(detail)}</td></tr>')
    if not rows:
        return section("Events", empty("No event under this filter."))
    table = ('<div class="table-scroll"><table class="list stack"><thead><tr><th>When (UTC)</th><th>What</th><th>Who</th><th>Application</th><th>Outcome</th>'
             f'<th>Detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    more = ""
    if len(rows) >= data["limit"] and data["limit"] < 200:
        more = f'<div class="links" style="padding:12px 20px 14px"><a class="btn secondary" href="{esc(href("activity", **data["filters"], more=1))}">Show the newest 200</a></div>'
    return (f'<section class="card flush"><div class="card-head"><div><h2>Events</h2><p class="sub">The newest {len(rows)}, newest first. Configuration '
            "changes name the keys that changed, never their values.</p></div></div>"
            f'<div class="card-body">{table}{more}</div></section>')


JOB_WORDS = {"auto_deploy": "Auto-deploy check", "watchdog": "Watchdog", "backup": "Nightly backup", "onboarding": "Onboarding",
             "follow": "Following releases", "contracts": "Kept contracts", "platform_updates": "Platform updates",
             "release_reports": "Release reports", "digest": "Daily digest"}


def result_words(result):
    """A job's last result as a line: its findings as "name: value", "ok" when it had nothing to say."""
    if result in (None, {}, [], ""):
        return "ok"
    if isinstance(result, dict):
        return "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in list(result.items())[:4])[:200]
    return str(result)[:200]


def controller_section(data):
    parts = []
    jobs = data.get("jobs")
    if jobs:
        rows = []
        for name, entry in jobs.items():
            if entry.get("last_error"):
                result = f'<span class="field-error">{esc(entry["last_error"][:160])}</span>'
            elif entry.get("last_run"):
                result = esc(result_words(entry.get("last_result")))
            else:
                result = '<span class="muted">not run yet</span>'
            rows.append(f'<tr><td data-label="Job"><strong>{esc(JOB_WORDS.get(name, name))}</strong></td><td class="nowrap" data-label="Last run (UTC)">'
                        f'{esc(stamp(entry.get("last_run")) if entry.get("last_run") else "–")}</td><td class="wrap wide" data-label="Result">{result}</td>'
                        f'<td class="nowrap" data-label="Next run">{esc(stamp(entry.get("next_run")))}</td></tr>')
        parts.append('<div class="table-scroll"><table class="list compact stack"><thead><tr><th>Job</th><th>Last run (UTC)</th><th>Result</th><th>Next run</th></tr></thead>'
                     f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        parts.append(empty("The background jobs do not run in this process (a controller started without them, such as a test)."))
    facts = []
    backup = data.get("backup")
    facts.append(("Nightly backup", f"{esc(stamp(backup['at']))} UTC, {esc(backup.get('outcome') or '')}: <span class=\"mono\">{esc((backup.get('detail') or '')[:160])}</span>"
                  if backup else "No backup recorded yet."))
    drill = data.get("restore_drill")
    facts.append(("Restore drill", f"{esc(stamp(drill['at']))} UTC, {esc(drill.get('outcome'))}" if drill else "None recorded yet (docs/RUNBOOK.md, quarterly)."))
    digest = data.get("digest") or {}
    facts.append(("Daily digest", digest_words(digest) or f"The newest is of {esc(day_words(digest.get('day')))}, written {esc(stamp(digest.get('generated_at')))} UTC."))
    drift = data.get("drift") or {}
    status = drift.get("status")
    if status in ("drift", "clean"):
        drifted = ", ".join(f"{name} ({', '.join(kinds)})" for name, kinds in (drift.get("drifted") or {}).items())
        words = (f"Drift in {esc(drifted)}" if drift.get("drifted") else "The cloud setup matches the code") + f", checked {esc(day_words(drift.get('checked_at')))}"
        if drift.get("stale"):
            words += "; the weekly check looks stopped"
        if drift.get("run_url"):
            words += f' · <a href="{esc(drift["run_url"])}">the run{icon("external")}</a>'
    else:
        words = esc(drift.get("note") or "Not read.")
    facts.append(("Infrastructure drift", words))
    credential = data.get("credential")
    if credential is None:
        facts.append(("GitHub credential", "Not checked by this adapter."))
    elif credential.get("error"):
        facts.append(("GitHub credential", f'<span class="field-error">Not checked: {esc(credential["error"])}</span>'))
    elif credential.get("kind") == "github_app":
        facts.append(("GitHub credential", "The GitHub App: its installation tokens renew every hour and never expire."))
    else:
        left = credential.get("days_left")
        expiry = f"expires {esc(day_words(credential.get('expires_at')))} ({plural(left, 'day')} left)" if left is not None else "no expiry reported"
        warning = f' <span class="field-error">{esc(credential["warning"])}</span>' if credential.get("warning") else ""
        facts.append(("GitHub credential", f"A fine-grained token: {expiry}.{warning}"))
    listed = "".join(f"<div><dt>{esc(term)}</dt><dd>{value}</dd></div>" for term, value in facts)
    return section("The controller", "".join(parts) + f'<dl class="facts tight" style="margin-top:8px">{listed}</dl>',
                   "Its background jobs, backups, drift and its GitHub credential, as get_controller_status reports them.")
