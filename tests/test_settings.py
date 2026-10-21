"""The settings page (labdeploy/settings.py): the controller's own configuration file, changed in the browser."""
import contextlib
import hashlib
import html
import os
import re
import sqlite3
import stat
import subprocess
from html.parser import HTMLParser

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import __version__, settings
from labdeploy.config import Config, load_config
from labdeploy.controller import Controller, Store
from labdeploy.oauth import PAGE_HEADERS
from labdeploy.server import build_app


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


OTHER_TARGET = {**TARGET, "service": "other", "registry": "other", "deploy_service_account": "other-deploy@test-project.iam.gserviceaccount.com",
                "probe_service_account": "other-probe@test-project.iam.gserviceaccount.com",
                "wif_provider": "projects/123456789/locations/global/workloadIdentityPools/other-gh/providers/github",
                "recovery_bucket": "test-project-other-recovery"}
ONBOARDED = {"repository": "lab/other", "service": "other", "target": OTHER_TARGET, "auto_deploy": True}


def base_config(tmp_path, **extra):
    return {
        "database": str(tmp_path / "db"),
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "cost_ceiling_usd": 40}},
        "principals": [
            {"name": "operator", "token_sha256": sha("operator-token"), "applications": ["*"], "can_deploy": True},
            {"name": "second", "token_sha256": sha("second-token"), "applications": ["*"], "can_deploy": True},
            {"name": "viewer", "token_sha256": sha("viewer-token"), "applications": ["demo"], "can_deploy": True},
            {"name": "watcher", "token_sha256": sha("watcher-token"), "applications": ["*"]},
        ],
        "platform": {"repository": "lab/fast-deploy"},
        "auth_failures_per_minute": 3,
        **extra,
    }


@pytest.fixture
def site(tmp_path):
    """make(**extra) -> (signed-out client, configuration path, store); more clients of the same controller with client()."""
    stack = contextlib.ExitStack()
    made = {}

    def make(**extra):
        path = tmp_path / "config.yaml"
        path.write_text("# the operator's own comment\n" + yaml.safe_dump(base_config(tmp_path, **extra), sort_keys=False))
        os.chmod(path, 0o600)
        store = Store(str(tmp_path / "db"))
        store.register_application("other", ONBOARDED)
        made["app"] = build_app(load_config(str(path)), FakeGitHub(), config_path=str(path))
        return client(), path, store

    def client(base_url="https://testserver"):
        """The first client of an app runs its startup; later ones share it (the MCP session manager starts once)."""
        if made.get("started") is made["app"]:
            return TestClient(made["app"], base_url=base_url)
        made["started"] = made["app"]
        return stack.enter_context(TestClient(made["app"], base_url=base_url))

    make.client = client
    with stack:
        yield make


class Forms(HTMLParser):
    """Every form of a page, with the values a browser would send for it."""

    def __init__(self, text):
        super().__init__()
        self.forms, self.form, self.select, self.textarea = [], None, None, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.form = {"action": a.get("action"), "fields": []}
            self.forms.append(self.form)
        elif self.form is None:
            return
        elif tag == "input" and a.get("name") and (a.get("type") != "checkbox" or "checked" in a):
            self.form["fields"].append((a["name"], a.get("value", "")))
        elif tag == "select":
            self.select = [a["name"], None, None]
        elif tag == "option" and self.select is not None:
            self.select[1] = a.get("value", "") if self.select[1] is None else self.select[1]
            self.select[2] = a.get("value", "") if "selected" in a else self.select[2]
        elif tag == "textarea":
            self.textarea = [a["name"], ""]

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "select" and self.select is not None:
            self.form["fields"].append((self.select[0], self.select[2] if self.select[2] is not None else self.select[1]))
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            self.form["fields"].append(tuple(self.textarea))
            self.textarea = None
        elif tag == "form":
            self.form = None

    def find(self, **hidden):
        for form in self.forms:
            if all((name, value) in form["fields"] for name, value in hidden.items()):
                return form
        raise AssertionError(f"no form with {hidden}")


def submit(client, page, changes=None, **hidden):
    """Send one form of a page as a browser would, with some fields changed (None unticks a box or drops the field)."""
    form = Forms(page.text).find(**hidden)
    data = {}
    for name, value in form["fields"]:
        data.setdefault(name, []).append(value)
    for name, value in (changes or {}).items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = [value]
    return client.post(form["action"], data=data, follow_redirects=False)


def sign_in(client, token="operator-token"):
    return client.post("/settings/sign-in", data={"token": token}, follow_redirects=False)


def tab(client, name, app=None):
    page = client.get(f"/settings?tab={name}" + (f"&app={app}" if app else ""))
    assert page.status_code == 200, page.text
    return page


def confirm(client, review):
    """The confirm step of a review page: its "Write the file" form."""
    assert review.status_code == 200, review.text
    return submit(client, review, {}, change=html.unescape(re.search(r'name="change" value="([^"]+)"', review.text).group(1)))


def text_of(page):
    return html.unescape(page.text)


def words_of(page):
    """The page as a person reads it: the text without its tags, one space between words."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page.text)))


def token_on(page):
    return html.unescape(re.search(r'class="token">([^<]+)<', page.text).group(1))


def written(path):
    return yaml.safe_load(path.read_text())


def test_sign_in_refusals_headers_and_the_cookie(site):
    client, _, store = site()
    page = client.get("/settings")
    assert page.status_code == 200 and "Operator token" in page.text and "<script" not in page.text
    for header, value in PAGE_HEADERS.items():
        assert page.headers[header] == value
    assert "Signed in" not in page.text
    viewer = sign_in(client, "viewer-token")  # may deploy, but not every application
    assert viewer.status_code == 403 and "viewer may not change" in viewer.text
    watcher = sign_in(client, "watcher-token")  # every application, but read-only
    assert watcher.status_code == 403 and "can_deploy" in watcher.text
    assert sign_in(client, "wrong").status_code == 401
    for _ in range(4):
        last = sign_in(client, "wrong")
    assert last.status_code == 429 and int(last.headers["Retry-After"]) >= 1  # the failed-login budget /login uses
    done = sign_in(client, " operator-token\n")  # a paste's whitespace is ignored, as on /login
    assert done.status_code == 303 and done.headers["location"] == "/settings"
    cookie = done.headers["set-cookie"]
    for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=3600"):
        assert attribute in cookie
    assert cookie.startswith("__Host-")
    page = client.get("/settings")
    assert "Signed in as operator" in words_of(page) and 'aria-current="page"' in page.text
    outcomes = [(e["identity"], e["outcome"]) for e in store.recent_events(20) if e["kind"] == "sign_in" and e["name"] == "settings page"]
    assert ("operator", "ok") in outcomes and ("viewer", "refused") in outcomes and (None, "rejected") in outcomes and (None, "throttled") in outcomes
    signed_out = client.post("/settings/sign-out", follow_redirects=False)
    assert signed_out.status_code == 303 and "Max-Age=0" in signed_out.headers["set-cookie"]
    assert "Operator token" in client.get("/settings").text


def test_the_session_is_stored_hashed_signed_and_short(site, tmp_path):
    client, _, _ = site(settings_session_seconds=600)
    assert "Max-Age=600" in sign_in(client).headers["set-cookie"]
    value = client.cookies.get(settings.COOKIE)
    session_id, _, signature = value.partition(".")
    with sqlite3.connect(tmp_path / "db") as db:
        rows = db.execute("SELECT digest, subject, expires - created FROM settings_sessions").fetchall()
    assert rows == [(sha(session_id), "operator", pytest.approx(600))] and session_id not in str(rows)
    forged = make_cookie_client(site, f"{session_id}.{'0' * len(signature)}")
    assert "Operator token" in forged.get("/settings").text  # a cookie whose signature does not match is nobody
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE settings_sessions SET expires = 0")
    page = client.get("/settings")
    assert "Your sign-in ended" in page.text and "Operator token" in page.text


def make_cookie_client(site, value):
    other = site.client()
    other.cookies.set(settings.COOKIE, value, domain="testserver")
    return other


def test_the_page_answers_only_on_the_controllers_hosts_and_its_own_forms(site):
    client, path, _ = site()
    stranger = site.client("https://evil.example")
    assert stranger.get("/settings").status_code == 400  # not in allowed_hosts (DNS rebinding)
    sign_in(client)
    before = path.read_text()
    page = tab(client, "platform")
    forged = submit(client, page, {"csrf": "not-the-form-token", "backup_keep_days": "30"}, form="main", tab="platform")
    assert forged.status_code == 403 and path.read_text() == before
    form = {name: values for name, values in [(n, [v]) for n, v in Forms(page.text).find(form="main", tab="platform")["fields"]]}
    cross = client.post("/settings/change", data=form, headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403 and path.read_text() == before
    assert client.get("/settingsx").status_code == 401  # only the page's own paths skip the bearer token


def test_applications_round_trip_for_the_file_and_an_onboarded_one(site, tmp_path):
    client, path, store = site()
    sign_in(client)
    overview = words_of(tab(client, "applications"))
    assert "demo In this file lab/demo Running $40 / month" in overview and "other Onboarded lab/other Running None" in overview
    page = tab(client, "applications", "demo")  # one application's page: one form, for it alone
    assert 'href="https://github.com/lab/demo/blob/main/platform.yaml"' in page.text  # what the page does not change, and where it lives
    assert 'href="https://github.com/lab/fast-deploy/tree/main/infra/gcp/apps/demo"' in page.text
    assert "secret-manager?project=test-project" in page.text and "scripts/spend-cap.sh" in tab(client, "platform").text
    review = submit(client, page, {"applications.demo.cost_ceiling_usd": "55.5", "applications.demo.viewers": "user:ada@example.edu\r\nuser:bob@example.edu",
                                   "applications.demo.infra.error_alert_count": "9"}, form="main", tab="applications")
    shown, said = text_of(review), words_of(review)
    assert review.status_code == 200 and "Review the change" in shown
    assert "-    cost_ceiling_usd: 40" in shown and "+    cost_ceiling_usd: 55.5" in shown and "+    - user:ada@example.edu" in shown
    # Plain words first: one line per setting, with its key and its old and new values.
    assert "demo, cost ceiling applications.demo.cost_ceiling_usd 40 to 55.5" in said
    assert "demo, alert on server errors applications.demo.infra.error_alert_count 5 to 9" in said
    assert "demo, viewers applications.demo.viewers none to user:ada@example.edu, user:bob@example.edu" in said
    assert written(path)["applications"]["demo"]["cost_ceiling_usd"] == 40  # nothing is written before the confirm step
    done = confirm(client, review)
    assert done.status_code == 200 and "Written" in done.text and "applied it at once" in done.text
    history = sorted((tmp_path / "history").glob("config-*.yaml"))
    assert len(history) == 1 and history[0].read_text().startswith("# the operator's own comment") and stat.S_IMODE(history[0].stat().st_mode) == 0o600
    review = submit(client, tab(client, "applications", "other"), {"application_settings.other.auto_deploy": None, "application_settings.other.bake_seconds": "120",
                                                                   "application_settings.other.infra.runtime_alerts": "false"}, form="main", tab="applications")
    assert "other, auto-deploy application_settings.other.auto_deploy as registered to off" in words_of(review)
    confirm(client, review)
    data = written(path)
    assert data["applications"]["demo"]["cost_ceiling_usd"] == 55.5 and data["applications"]["demo"]["viewers"] == ["user:ada@example.edu", "user:bob@example.edu"]
    assert data["applications"]["demo"]["infra"] == {"error_alert_count": 9}
    assert data["application_settings"] == {"other": {"auto_deploy": False, "bake_seconds": 120, "infra": {"runtime_alerts": False}}}
    load_config(str(path))  # the file the page writes is one the controller loads
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_text().startswith(settings.HEADER)
    assert not list(tmp_path.glob(".config-*"))  # no temporary file left behind
    events = [e for e in store.recent_events(10) if e["kind"] == "configuration"]
    assert [e["application"] for e in events] == ["other", "demo"] and all(e["identity"] == "operator" and e["name"] == "applications" for e in events)
    assert "applications.demo.cost_ceiling_usd" in events[1]["detail"] and "55.5" not in events[1]["detail"] and "ada@" not in events[1]["detail"]
    again = tab(client, "applications", "demo")  # the controller runs on the new file at once (LiveConfig)
    assert 'value="55.5"' in again.text and "Last written just now by operator" in again.text
    assert "Nothing would change" in submit(client, again, {}, form="main", tab="applications").text
    # Back to the registered entry's own value: the override goes, and so does the empty block.
    back = submit(client, tab(client, "applications", "other"), {"application_settings.other.auto_deploy": "on", "application_settings.other.bake_seconds": "300",
                                                                 "application_settings.other.infra.runtime_alerts": ""}, form="main", tab="applications")
    confirm(client, back)
    assert "application_settings" not in written(path)
    assert "There is no application named nowhere" in tab(client, "applications", "nowhere").text


def test_a_one_application_change_is_filed_under_that_application(site):
    client, _, store = site()
    sign_in(client)
    confirm(client, submit(client, tab(client, "applications", "demo"), {"applications.demo.bake_seconds": "60"}, form="main", tab="applications"))
    assert store.recent_events(5, application="demo")[0]["kind"] == "configuration"


def test_validation_errors_sit_next_to_their_field_and_nothing_is_written(site):
    client, path, _ = site()
    sign_in(client)
    before = path.read_text()

    def marked(page, key, message, value=None):
        """The field is outlined and described by its own message; the summary at the top names it too."""
        field = re.search(rf'<input id="{re.escape(key)}"[^>]*>', page.text).group(0)
        assert 'aria-invalid="true"' in field and f'aria-describedby="{key}-error"' in field and (value is None or f'value="{value}"' in field)
        assert re.search(rf'<p class="field-error" id="{re.escape(key)}-error">[^<]*{re.escape(message)}', page.text)

    refused = submit(client, tab(client, "applications", "demo"), {"applications.demo.bake_seconds": "5000", "applications.demo.cost_ceiling_usd": "lots"},
                     form="main", tab="applications")
    assert refused.status_code == 400 and path.read_text() == before
    marked(refused, "applications.demo.bake_seconds", "must be at most 900", "5000")
    marked(refused, "applications.demo.cost_ceiling_usd", "must be a number", "lots")
    said = words_of(refused)
    assert "Nothing was changed. 2 things need fixing:" in said and "demo, watch after release: must be at most 900" in said
    refused = submit(client, tab(client, "applications", "other"), {"application_settings.other.infra.error_alert_count": "0"}, form="main", tab="applications")
    # ApplicationSettings alone accepts any number; laid over the registered entry, as the controller does, it is refused.
    assert refused.status_code == 400
    marked(refused, "application_settings.other.infra.error_alert_count", "infra.error_alert_count must be between 1 and 10000")
    assert re.search(r"<details class=\"card\" open>", refused.text)  # the folded infrastructure card opens to show it
    refused = submit(client, tab(client, "applications", "demo"), {"applications.demo.viewers": "bob"}, form="main", tab="applications")
    assert refused.status_code == 400 and "Viewer &#x27;bob&#x27; must be user:EMAIL" in refused.text and path.read_text() == before
    assert 'id="applications.demo.viewers-error"' in refused.text


def test_danger_zone_needs_the_name_typed(site):
    client, path, _ = site()
    sign_in(client)
    page = tab(client, "applications", "demo")
    wrong = submit(client, page, {"confirm": "dmeo"}, form="pause", application="demo")
    assert wrong.status_code == 400 and "type demo exactly to confirm" in wrong.text and not written(path)["applications"]["demo"].get("paused")
    assert re.search(r'name="applications\.demo\.cost_ceiling_usd"[^>]*value="40"', wrong.text)  # the main form keeps the file's values
    review = submit(client, page, {"confirm": "demo"}, form="pause", application="demo")
    assert "demo, state applications.demo.paused running to paused" in words_of(review)
    confirm(client, review)
    assert written(path)["applications"]["demo"]["paused"] is True
    assert "1 paused" in tab(client, "applications").text
    review = submit(client, tab(client, "applications", "other"), {"confirm": "other"}, form="provenance", application="other")
    assert "+    provenance: unavailable" in text_of(review)
    confirm(client, review)
    assert written(path)["application_settings"] == {"other": {"provenance": "unavailable"}}
    platform = tab(client, "platform")
    assert submit(client, platform, {"confirm": "yes"}, form="kill").status_code == 400
    confirm(client, submit(client, platform, {"confirm": settings.KILL_SWITCH}, form="kill"))
    assert written(path)["paused"] is True
    assert "Kill switch on: every application paused" in tab(client, "people").text  # shown on every tab while it is on
    confirm(client, submit(client, tab(client, "platform"), {"confirm": settings.KILL_SWITCH}, form="kill"))
    assert "paused" not in written(path)


def test_platform_round_trip_never_shows_the_webhook(site, tmp_path):
    client, path, store = site()
    sign_in(client)
    hook = "https://hooks.slack.com/services/T000/B000/very-secret-part"
    review = submit(client, tab(client, "platform"), {
        "auto_deploy_interval_seconds": "600", "backup_keep_days": "30", "notify_webhook_url": hook, "cloud_run_spend_caps": "test-project: 20",
        "platform_updates.on": "on", "platform_updates.workflows": None, "platform_updates.delay_minutes": "30"}, form="main", tab="platform")
    assert review.status_code == 200 and "very-secret-part" not in review.text and "notify_webhook_url: (hidden, fingerprint" in text_of(review)
    said = words_of(review)
    assert "Webhook notify_webhook_url not set to set" in said and "Automatic updates platform_updates off to on (re-pin pull requests: off, delay: 30 minutes)" in said
    assert "Auto-deploy check auto_deploy_interval_seconds 300 to 600 seconds" in said and "spend caps, test-project cloud_run_spend_caps.test-project not set to 20" in said
    confirm(client, review)
    data = written(path)
    assert data["auto_deploy_interval_seconds"] == 600 and data["backup_keep_days"] == 30 and data["notify_webhook_url"] == hook
    assert data["cloud_run_spend_caps"] == {"test-project": 20} and data["platform_updates"] == {"workflows": False, "delay_minutes": 30}
    event = next(e for e in store.recent_events(10) if e["kind"] == "configuration")
    assert "notify_webhook_url" in event["detail"] and "hooks.slack.com" not in event["detail"]
    page = tab(client, "platform")
    assert "very-secret-part" not in page.text and "Webhook set" in page.text and "Automatic updates on" in page.text
    assert "Nothing would change" in submit(client, page, {}, form="main", tab="platform").text  # an empty webhook field keeps it
    confirm(client, submit(client, page, {"platform_updates.on": None, "notify_webhook_url.remove": "on"}, form="main", tab="platform"))
    data = written(path)
    assert "platform_updates" not in data and "notify_webhook_url" not in data
    refused = submit(client, tab(client, "platform"), {"cloud_run_spend_caps": "test-project twenty", "backup_keep_days": "0"}, form="main", tab="platform")
    assert refused.status_code == 400 and "is not project: amount" in text_of(refused) and "must be at least 1" in refused.text


def test_a_new_token_is_shown_once_after_the_confirm_step(site):
    client, path, _ = site()
    sign_in(client)
    review = submit(client, tab(client, "people"), {"new.name": "newcomer", "new.applications": "*", "new.can_deploy": "on"}, form="add")
    assert "A new token for newcomer is shown once, after you confirm." in text_of(review) and 'class="token"' not in review.text
    done = confirm(client, review)
    token = token_on(done)
    assert "user-select:all" in done.text
    entry = next(p for p in written(path)["principals"] if p["name"] == "newcomer")
    assert entry == {"name": "newcomer", "token_sha256": sha(token), "applications": ["*"], "can_deploy": True}
    assert token not in tab(client, "people").text and token not in path.read_text()
    newcomer = site.client()
    assert sign_in(newcomer, token).status_code == 303


def test_people_edits_rotation_and_removal_end_sessions(site):
    client, path, _ = site()
    other = site.client()
    sign_in(client)
    sign_in(other, "second-token")
    assert "Signed in as second" in words_of(other.get("/settings"))
    page = tab(client, "people")
    refused = submit(client, page, {"principals.viewer.applications": "demo, nowhere"}, form="main", tab="people", people="viewer")
    assert refused.status_code == 400 and "Principal references unknown application" in refused.text
    assert re.search(r'<details open><summary><span class="who-cell">viewer', refused.text)  # the person whose form was sent stays open
    confirm(client, submit(client, page, {"principals.viewer.can_deploy": None}, form="main", tab="people", people="viewer"))
    confirm(client, submit(client, tab(client, "people"), {"principals.watcher.may_merge_own_requests": None}, form="main", tab="people", people="watcher"))
    viewer, watcher = (next(p for p in written(path)["principals"] if p["name"] == name) for name in ("viewer", "watcher"))
    assert "can_deploy" not in viewer and watcher["may_merge_own_requests"] is False
    review = submit(client, tab(client, "people"), {"confirm": "second"}, form="rotate", principal="second")
    assert "Every session of second ends" in text_of(review) and "principals.second.token_sha256" in review.text
    done = confirm(client, review)
    token = token_on(done)
    ended = other.get("/settings")
    assert "Your sign-in ended" in ended.text  # the rotated principal's session is over at once
    assert sign_in(other, "second-token").status_code == 401 and sign_in(other, token).status_code == 303
    page = tab(client, "people")
    assert submit(client, page, {"confirm": "Viewer"}, form="remove", principal="viewer").status_code == 400
    confirm(client, submit(client, page, {"confirm": "viewer"}, form="remove", principal="viewer"))
    assert [p["name"] for p in written(path)["principals"]] == ["operator", "second", "watcher"]


def test_rotating_your_own_token_signs_you_out_after_showing_it(site):
    client, path, _ = site()
    sign_in(client)
    done = confirm(client, submit(client, tab(client, "people"), {"confirm": "operator"}, form="rotate", principal="operator"))
    assert 'class="token"' in done.text and "Max-Age=0" in done.headers["set-cookie"]
    assert "Operator token" in client.get("/settings").text


def test_the_page_keeps_someone_able_to_open_it(site):
    client, path, _ = site()
    sign_in(client)
    confirm(client, submit(client, tab(client, "people"), {"confirm": "second"}, form="remove", principal="second"))
    before = path.read_text()
    refused = submit(client, tab(client, "people"), {"principals.operator.applications": "demo"}, form="main", tab="people", people="operator")
    assert refused.status_code == 400 and "Nobody could open this page afterwards" in refused.text and path.read_text() == before


def test_advanced_whole_file_with_the_restart_note(site, tmp_path):
    client, path, _ = site()
    sign_in(client)
    page = tab(client, "advanced")
    current = Forms(page.text).find(form="main", tab="advanced")
    text = dict(current["fields"])["text"]
    assert "# the operator's own comment" in text  # the file as it is on disk
    broken = submit(client, page, {"text": text + "applications: [\n"}, form="main", tab="advanced")
    assert broken.status_code == 400 and "The YAML does not parse: line" in broken.text
    twice = submit(client, page, {"text": text + "paused: true\npaused: false\n"}, form="main", tab="advanced")
    assert twice.status_code == 400 and "unique string keys" in twice.text
    typo = submit(client, page, {"text": text + "pased: true\n"}, form="main", tab="advanced")
    assert typo.status_code == 400 and "pased: is not a setting the controller knows" in words_of(typo) and 'href="#pased"' in typo.text
    new_database = str(tmp_path / "moved.db")
    review = submit(client, page, {"text": text.replace(str(tmp_path / "db"), new_database) + "oauth:\n  public_url: https://testserver\n"}, form="main", tab="advanced")
    said = words_of(review)
    assert "2 settings would change" in said and f"Database database {tmp_path / 'db'} to {new_database}" in said and "need a restart" in review.text
    done = confirm(client, review)
    assert "database path changes need a restart; oauth changes need a restart" in done.text and "scripts/machine.sh restart" in done.text
    assert load_config(str(path)).oauth.public_url == "https://testserver"


def test_a_file_that_moved_on_is_not_overwritten(site):
    client, path, _ = site()
    sign_in(client)
    review = submit(client, tab(client, "platform"), {"backup_keep_days": "21"}, form="main", tab="platform")
    path.write_text(path.read_text() + "# config-put meanwhile\n")
    conflict = confirm(client, review)
    assert conflict.status_code == 409 and "The file changed after this change was prepared" in conflict.text
    assert path.read_text().endswith("# config-put meanwhile\n") and "backup_keep_days: 21" not in path.read_text()
    assert confirm(client, review).status_code == 410  # a change is confirmed once at most


def test_a_failed_write_leaves_the_file_and_no_temporary(site, tmp_path, monkeypatch):
    client, path, _ = site()
    sign_in(client)
    before = path.read_text()
    review = submit(client, tab(client, "platform"), {"backup_keep_days": "21"}, form="main", tab="platform")

    def refuse(*args):
        raise OSError("disk full")
    monkeypatch.setattr(settings.os, "replace", refuse)
    with pytest.raises(OSError):
        confirm(client, review)
    assert path.read_text() == before and not list(tmp_path.glob(".config-*"))


def test_history_keeps_the_newest_twenty(tmp_path):
    path = tmp_path / "config.yaml"
    for number in range(23):
        settings.keep_history(path, f"version {number}\n".encode())
    kept = sorted((tmp_path / "history").glob("config-*.yaml"))
    assert len(kept) == 20 and kept[-1].read_text() == "version 22\n" and kept[0].read_text() == "version 3\n"


def test_the_new_file_goes_to_the_backup_bucket_like_the_nightly_backup(site, monkeypatch, tmp_path):
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    client, path, _ = site(backup_bucket="lab-backups", backup_prefix="machine")
    sign_in(client)
    done = confirm(client, submit(client, tab(client, "platform"), {"backup_keep_days": "21"}, form="main", tab="platform"))
    assert calls == [["gcloud", "storage", "cp", str(path), "gs://lab-backups/machine/config.yaml"]] and "Copied to gs://lab-backups/machine/config.yaml" in words_of(done)
    config = Config.model_validate({**base_config(tmp_path), "backup_bucket": "lab-backups", "database": str(tmp_path / "db")})
    calls.clear()
    Controller(config, FakeGitHub(), Store(config.database)).backup_tick(config_path=str(path))  # the nightly backup, through the same code
    assert [call[-1].rsplit("/", 1)[-1] for call in calls][-1] == "config.yaml" and all(call[-1].startswith("gs://lab-backups/controller/") for call in calls)


def test_no_bucket_means_no_copy_and_a_failed_copy_is_reported(site, monkeypatch):
    def fail(arguments, **kwargs):
        raise subprocess.CalledProcessError(1, arguments, stderr="ERROR: permission denied")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no bucket is configured"))
    client, path, store = site()
    sign_in(client)
    done = confirm(client, submit(client, tab(client, "platform"), {"backup_keep_days": "21"}, form="main", tab="platform"))
    assert "Copied" not in done.text and "failed" not in done.text
    monkeypatch.setattr(subprocess, "run", fail)
    confirm(client, submit(client, tab(client, "advanced"), {"text": path.read_text() + "backup_bucket: lab-backups\n"}, form="main", tab="advanced"))
    done = confirm(client, submit(client, tab(client, "platform"), {"backup_keep_days": "22"}, form="main", tab="platform"))
    assert "The copy to the backup bucket failed (ERROR: permission denied)" in done.text and written(path)["backup_keep_days"] == 22
    assert "copy to the backup bucket failed" in store.recent_events(1)[0]["detail"]


def test_production_tab(site):
    client, _, _ = site()
    sign_in(client)
    assert "This controller has no production yet" in tab(client, "production").text
    client, path, _ = site(production={"project_id": "prod-project", "project_number": "123456789012", "reviewers": ["octocat"]})
    sign_in(client)
    page = tab(client, "production")
    refused = submit(client, page, {"production.rollout": "50, 10"}, form="main", tab="production")
    assert refused.status_code == 400 and "production.rollout is a rising list" in refused.text
    confirm(client, submit(client, page, {"production.rollout": "25, 100", "production.reviewers": "octocat\nhubot", "production.bake_seconds": "600"},
                           form="main", tab="production"))
    assert written(path)["production"] == {"project_id": "prod-project", "project_number": "123456789012", "reviewers": ["octocat", "hubot"],
                                           "rollout": [25, 100], "bake_seconds": 600}


def test_a_file_that_does_not_load_can_be_fixed_on_the_advanced_tab(site):
    client, path, _ = site()
    sign_in(client)
    good = path.read_text()
    path.write_text(good + "pased: true\n")
    page = tab(client, "applications")
    assert "The file on disk does not load" in page.text and "pased" in page.text
    review = submit(client, tab(client, "advanced"), {"text": good}, form="main", tab="advanced")
    assert "-pased: true" in text_of(review)
    confirm(client, review)
    assert "pased" not in path.read_text() and load_config(str(path))


def test_a_controller_without_a_file_has_nothing_to_change(tmp_path):
    config = Config.model_validate(base_config(tmp_path))
    with TestClient(build_app(config, FakeGitHub()), base_url="https://testserver") as client:
        page = client.get("/settings")
    assert page.status_code == 404 and "without a configuration file" in page.text


def test_every_screen_keeps_the_strict_headers_and_loads_nothing(site):
    """No script, no web font, no image file: inline SVG and CSS only, under the sign-in page's Content-Security-Policy."""
    client, _, _ = site()
    pages = [client.get("/settings")]
    sign_in(client)
    pages += [tab(client, name) for name in ("applications", "platform", "people", "production", "advanced")]
    pages.append(tab(client, "applications", "demo"))
    pages.append(submit(client, pages[-1], {"applications.demo.bake_seconds": "60"}, form="main", tab="applications"))
    pages.append(confirm(client, pages[-1]))
    for page in pages:
        for header, value in PAGE_HEADERS.items():
            assert page.headers[header] == value
        assert "<script" not in page.text and "url(" not in page.text and "<img" not in page.text and "@import" not in page.text
    assert f"Fast Deploy {__version__}" in words_of(pages[1]) and 'aria-current="page"' in pages[2].text
