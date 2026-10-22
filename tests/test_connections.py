"""Account selection, real provider protocols with mock HTTP, and enforced repository boundaries."""
import copy
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from starlette.datastructures import FormData
from test_settings import confirm, sign_in, submit, tab
from test_settings import site as settings_site

from labdeploy.config import Config, LiveConfig
from labdeploy.connections import GOOGLE_SCOPE, BrowserConnections, github_catalog
from labdeploy.github import AppInstallations, GitHub, StaticToken
from labdeploy.settings import change_connections

site = settings_site


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setenv("LAB_GOOGLE_CLIENT_SECRET", "server-secret")
    config = Config(applications={}, principals=[], allowed_hosts=["deploy.example.com"], oauth={"public_url": "https://deploy.example.com"},
                    connections={"google_client_id": "123.apps.googleusercontent.com"})
    calls = []

    def google(request):
        calls.append(request)
        path = request.url.path
        if path == "/token":
            body = parse_qs(request.content.decode())
            assert body["client_secret"] == ["server-secret"]
            assert len(body["code_verifier"][0]) >= 43
            assert body["redirect_uri"] == ["https://deploy.example.com/settings/connections/google/callback"]
            return httpx.Response(200, json={"access_token": "google-secret", "scope": GOOGLE_SCOPE, "expires_in": 3600})
        if path == "/v1/userinfo":
            return httpx.Response(200, json={"email": "person@example.com", "sub": "google-sub", "email_verified": True})
        if path.endswith("projects:search"):
            return httpx.Response(200, json={"projects": [
                {"projectId": "target-project", "name": "projects/123456", "displayName": "Research", "state": "ACTIVE"},
                {"projectId": "closed-project", "name": "projects/987654", "state": "DELETE_REQUESTED"},
            ]})
        if path.endswith("billingInfo"):
            assert request.headers["Authorization"] == "Bearer google-secret"
            return httpx.Response(200, json={"billingEnabled": True})
        assert request.headers["Authorization"] == "Bearer server-identity"
        return httpx.Response(200, json={})

    service = BrowserConnections(config, None, httpx.Client(transport=httpx.MockTransport(google)), foundation_token=lambda: "server-identity")
    return service, calls


def connect(browser, session="session"):
    url = browser.start_google(session)
    params = parse_qs(urlparse(url).query)
    return browser.finish_google(session, params["state"][0], "code")


def test_google_code_flow_uses_state_pkce_session_and_read_only_scopes(browser):
    service, calls = browser
    url = service.start_google("one")
    params = parse_qs(urlparse(url).query)
    assert params["prompt"] == ["select_account"]
    assert params["access_type"] == ["online"]
    assert params["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/cloud-platform" not in params["scope"][0].split()
    with pytest.raises(ValueError, match="another browser"):
        service.finish_google("two", params["state"][0], "code")
    assert not calls
    key = service.finish_google("one", params["state"][0], "code")
    with pytest.raises(ValueError, match="expired"):
        service.finish_google("one", params["state"][0], "code")
    with pytest.raises(ValueError, match="expired"):
        service.projects("two", key)
    snapshot = service.snapshot("one")
    assert snapshot["accounts"][0]["email"] == "person@example.com"
    assert "google-secret" not in json.dumps(snapshot)
    assert service.snapshot("two")["accounts"] == []


def test_projects_are_live_and_destination_checks_billing_and_foundation(browser):
    service, calls = browser
    key = connect(service)
    projects = service.projects("session", key)
    assert [p["id"] for p in projects] == ["target-project"]
    with pytest.raises(ValueError, match="can access"):
        service.destination("session", key, "forged-project")
    result = service.destination("session", key, "target-project")
    assert result == {"id": "target-project", "number": "123456", "name": "Research", "email": "person@example.com"}
    assert any("lab-deploy-infra-apply" in str(call.url) for call in calls)
    assert any("target-project-tfstate" in str(call.url) for call in calls)
    service.forget("session")
    assert not service.accounts and not service.states


def test_expired_state_and_accounts_are_removed(browser):
    service, calls = browser
    key = connect(service)
    pending = parse_qs(urlparse(service.start_google("session")).query)["state"][0]
    now = service.clock()
    service.clock = lambda: now + 4000
    with pytest.raises(ValueError, match="expired"):
        service.finish_google("session", pending, "code")
    with pytest.raises(ValueError, match="expired"):
        service.account("session", key)
    assert not service.accounts and not service.states


@pytest.mark.parametrize("failure", ["declined", "scope", "email"])
def test_google_partial_consent_and_unverified_identity_fail_closed(browser, failure):
    service, calls = browser
    original = service.client

    def response(request):
        if failure == "scope" and request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "sensitive", "expires_in": 3600, "scope": "openid email"})
        if failure == "email" and request.url.path.endswith("userinfo"):
            return httpx.Response(200, json={"email": "someone@example.com", "sub": "id", "email_verified": False})
        return original.send(request)

    service.client = httpx.Client(transport=httpx.MockTransport(response))
    params = parse_qs(urlparse(service.start_google("session")).query)
    with pytest.raises(ValueError):
        service.finish_google("session", params["state"][0], "code", "access_denied" if failure == "declined" else None)
    assert not service.accounts


def test_github_installation_discovery_lists_actual_grants_and_revokes_inventory_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    seen = []

    def github(request):
        seen.append(request)
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"slug": "fast-deploy"})
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": 123, "account": {"login": "team", "type": "Organization"}, "repository_selection": "selected"}])
        if path == "/app/installations/123/access_tokens":
            assert json.loads(request.content) == {"permissions": {"metadata": "read"}}
            return httpx.Response(201, json={"token": "inventory-token"})
        if path == "/installation/repositories":
            assert request.headers["authorization"] == "Bearer inventory-token"
            return httpx.Response(200, json={"repositories": [{"full_name": "team/app", "private": True}]})
        assert path == "/installation/token" and request.method == "DELETE"
        return httpx.Response(204)

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(github))
    credential = AppInstallations("123", lambda: "unused", client)
    monkeypatch.setattr(credential, "assertion", lambda: "app-jwt")
    adapter = GitHub(client, credential)
    catalog = github_catalog(adapter)
    assert catalog["repositories"][0]["name"] == "team/app"
    assert catalog["installations"][0]["url"] == "https://github.com/organizations/team/settings/installations/123"
    assert catalog["install_url"] == "https://github.com/apps/fast-deploy/installations/new"
    assert "inventory-token" not in json.dumps(catalog)
    assert seen[-1].method == "DELETE"


def test_github_catalog_paginates_without_following_remote_links(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    pages = []

    def github(request):
        pages.append(request.url.params["page"])
        assert request.url.host == "api.github.com"
        count = 100 if len(pages) == 1 else 1
        return httpx.Response(200, json=[{"full_name": f"owner/p{len(pages)}-{i}", "private": True, "owner": {"login": "owner"}} for i in range(count)],
                              headers={"Link": '<https://untrusted.example/steal>; rel="next"'})

    adapter = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(github)), StaticToken("secret"))
    assert len(github_catalog(adapter)["repositories"]) == 101
    assert pages == ["1", "2"]


def test_restrictions_apply_before_network_and_change_without_restart(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []
    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={})))
    adapter = GitHub(client, StaticToken("secret"))
    config = LiveConfig(Config(applications={}, principals=[], platform={"repository": "team/platform"}, connections={"repositories": ["Team/Allowed"]}))
    adapter.access_config = config
    with pytest.raises(ValueError, match="disabled in Connections"):
        adapter.request("GET", "/repos/team/refused/contents/app.py")
    assert not calls
    adapter.request("GET", "/repos/team/allowed")
    adapter.request("GET", "/repos/team/platform")
    config.current.connections.repositories = []
    with pytest.raises(ValueError):
        adapter.request("POST", "/repos/team/allowed/actions/workflows/deploy.yml/dispatches")
    assert len(calls) == 2


def test_existing_central_application_cannot_bypass_repo_selection(setup):
    controller, principal, github = setup
    controller.config.connections.repositories = []
    with pytest.raises(ValueError, match="disabled in Connections"):
        controller.require_active("demo")
    assert github.calls == 0


def test_connections_page_permissions_and_forgery_protection(site):
    client, path, store = site()
    sign_in(client, "watcher-token")
    assert client.get("/settings?tab=connections").status_code == 403
    sign_in(client)
    page = tab(client, "connections")
    assert "Google sign-in is not set up yet" in page.text and "Choose your Google account" in page.text
    assert "Connection setup" in page.text and "No destination selected" in page.text
    response = client.post("/settings/connections/action", data={"action": "google-connect"})
    assert response.status_code == 403
    response = submit(client, page, {}, action="google-connect") if 'value="google-connect"' in page.text else None
    assert response is None
    assert client.get("/settings/connections/google/callback?state=forged&code=secret").status_code == 401


def test_guided_steps_preserve_review_and_allow_returning_to_account_selection(site, monkeypatch):
    from labdeploy.controller import Controller
    monkeypatch.setattr(Controller, "check_repository", lambda *args: {"as_it_is": {"ready": True}})
    client, path, store = site(live_dispatch=True, onboarding={"project_id": "test-project", "project_number": "123456789"})
    sign_in(client)
    initial = tab(client, "connections")
    assert '<html lang="en">' in initial.text
    assert 'id="github" open' in initial.text
    review = submit(client, initial, {"repository": "lab/demo-new"}, action="check-repository")
    assert 'id="application-setup" open' in review.text
    account = client.get('/settings?tab=connections&step=google')
    assert 'id="google" open' in account.text and 'id="application-setup" open' not in account.text
    assert 'value="start-application"' in account.text
    assert "default-src 'none'" in account.headers['content-security-policy']
    assert '<script' not in account.text


def test_google_client_setup_is_reviewed_and_persisted_without_a_secret(site):
    client, path, store = site()
    sign_in(client)
    review = submit(client, tab(client, "connections"), {"connections.google_client_id": "123.apps.googleusercontent.com"}, form="google-setup")
    assert "123.apps.googleusercontent.com" in review.text
    assert "connections" not in yaml.safe_load(path.read_text())
    assert confirm(client, review).status_code == 200
    assert yaml.safe_load(path.read_text())["connections"]["google_client_id"] == "123.apps.googleusercontent.com"


def test_repository_selection_requires_provider_access_and_confirmation(site, monkeypatch):
    catalog = {"kind": "github_app", "installations": [], "install_url": "https://github.com/apps/fast-deploy/installations/new",
               "repositories": [{"name": "lab/fast-deploy", "private": True}, {"name": "lab/demo", "private": True}]}
    monkeypatch.setattr("labdeploy.connections.github_catalog", lambda github: copy.deepcopy(catalog))
    client, path, store = site()
    sign_in(client)
    page = submit(client, tab(client, "connections"), {}, action="github-refresh")
    assert "lab/demo" in page.text
    refused = submit(client, page, {"repositories": "stranger/private"}, form="repositories")
    assert refused.status_code == 400 and "no longer visible" in refused.text
    review = submit(client, page, {"repositories": "lab/demo"}, form="repositories")
    confirm(client, review)
    assert yaml.safe_load(path.read_text())["connections"]["repositories"] == ["lab/demo"]
    assert store.newest_event("configuration")["name"] == "connections"


def test_google_callback_session_binding_and_signout(site, browser, monkeypatch):
    service, calls = browser
    monkeypatch.setattr("labdeploy.settings.BrowserConnections", lambda config, github: service)
    client, path, store = site()
    sign_in(client)
    start = submit(client, tab(client, "connections"), {}, action="google-connect")
    assert start.status_code == 303
    assert "SameSite=Lax" in start.headers["set-cookie"] and "HttpOnly" in start.headers["set-cookie"]
    params = parse_qs(urlparse(start.headers["location"]).query)
    url = "/settings/connections/google/callback?" + f'state={params["state"][0]}&code=secret-code'
    other = site.client()
    sign_in(other, "second-token")
    assert other.get(url).status_code == 401
    result = client.get(url)
    assert result.status_code == 200 and "Next: choose a project" in result.text
    assert "google-secret" not in result.text and "secret-code" not in result.text
    assert "person@example.com" in tab(client, "connections").text
    client.post("/settings/sign-out")
    assert not service.accounts


def test_provider_failure_does_not_leak_error_body_or_token(site, browser, monkeypatch):
    service, calls = browser
    monkeypatch.setattr("labdeploy.settings.BrowserConnections", lambda config, github: service)

    def error(session):
        response = httpx.Response(403, text="sensitive-provider-body", request=httpx.Request("GET", "https://api.github.com/?token=secret"))
        response.raise_for_status()

    monkeypatch.setattr(service, "refresh_github", error)
    client, path, store = site()
    sign_in(client)
    page = submit(client, tab(client, "connections"), {}, action="github-refresh")
    assert page.status_code == 400 and "Access was refused" in page.text
    assert "sensitive-provider-body" not in page.text and "?token=secret" not in page.text


def test_new_destination_does_not_carry_old_project_settings(browser):
    service, calls = browser
    key = connect(service)
    config = service.config.model_copy(update={"onboarding": None})
    config = Config.model_validate({**config.model_dump(), "platform": {"repository": "lab/platform"},
                                   "onboarding": {"project_id": "previous-project", "project_number": "999999", "state_bucket": "previous-state",
                                                  "billing_export_table": "previous.billing.table", "iap_client_id": "123-previous.apps.googleusercontent.com"}})
    service.github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"value":
            ("projects/123456/locations/global/workloadIdentityPools/lab-deploy-platform/providers/github-"
             if "WIF" in request.url.path else "lab-deploy-infra-") + ("apply" if "APPLY" in request.url.path else "plan")
            + ("" if "WIF" in request.url.path else "@target-project.iam.gserviceaccount.com")}))), StaticToken("test"))
    candidate = config.model_dump()
    change_connections(candidate, config, FormData({"account": key, "project": "target-project"}), "destination", service, "session")
    validated = Config.model_validate(candidate)
    assert validated.onboarding.project_id == "target-project" and validated.onboarding.project_number == "123456"
    assert validated.connections.google_account == "person@example.com"
    assert validated.onboarding.state_bucket is None and validated.onboarding.billing_export_table is None and validated.onboarding.iap_client_id is None


@pytest.mark.parametrize("mode,until", [("as-is", "contract"), ("prepared", "infrastructure")])
def test_ui_checks_then_starts_once_with_reviewed_values(site, monkeypatch, mode, until):
    from labdeploy.controller import Controller
    started = []
    monkeypatch.setattr(Controller, "check_repository", lambda self, principal, repository, path: {
        "repository": repository, "path": path, "ready": True, "checklist": [], "as_it_is": {"ready": True, "refusals": []}})

    def start(self, principal, repository, app_id, auto_deploy, path, **kwargs):
        started.append((repository, app_id, auto_deploy, path, kwargs))
        return {"application": "new-app"}

    monkeypatch.setattr(Controller, "onboard", start)
    client, path, store = site(live_dispatch=True, onboarding={"project_id": "test-project", "project_number": "123456789"})
    sign_in(client)
    review = submit(client, tab(client, "connections"), {"repository": "https://github.com/lab/new-app", "mode": mode,
                                                        "auto_deploy": "on", "path": "services/api"}, action="check-repository")
    assert review.status_code == 200 and "Your app is ready to set up" in review.text and "test-project" in review.text
    assert started == []
    result = submit(client, review, {"repository": "forged/private", "project": "forged-project"}, action="start-application")
    assert result.status_code == 303 and "onboarding=new-app" in result.headers["location"]
    expected_name = "new-app-api" if mode == "as-is" else "api"
    assert f'<dt>Application</dt><dd>{expected_name}' in review.text
    assert started == [("lab/new-app", expected_name, True, "services/api", {"as_is": mode == "as-is", "until": until})]
    duplicate = submit(client, review, {}, action="start-application")
    assert duplicate.status_code == 400 and "already used" in duplicate.text and len(started) == 1


def test_setup_review_cannot_outlive_a_configuration_change(site, monkeypatch):
    from labdeploy.controller import Controller
    monkeypatch.setattr(Controller, "check_repository", lambda *args: {"as_it_is": {"ready": True}})
    client, path, store = site(live_dispatch=True, onboarding={"project_id": "test-project", "project_number": "123456789"})
    sign_in(client)
    review = submit(client, tab(client, "connections"), {"repository": "lab/new-app"}, action="check-repository")
    config = yaml.safe_load(path.read_text())
    config["onboarding"]["project_id"] = "other-project"
    path.write_text(yaml.safe_dump(config))
    refreshed = tab(client, "connections")
    assert "Settings changed" in refreshed.text and 'value="start-application"' not in refreshed.text
    result = submit(client, review, {}, action="start-application")
    assert result.status_code == 400 and "Settings changed" in result.text
    assert store.onboardings() == []


def test_failed_recheck_invalidates_previous_start_and_preserves_input(site, monkeypatch):
    from labdeploy.controller import Controller

    def check(self, principal, repository, path):
        if repository == "lab/broken":
            raise ValueError("Repository could not be checked")
        return {"as_it_is": {"ready": True}}

    monkeypatch.setattr(Controller, "check_repository", check)
    client, path, store = site(live_dispatch=True, onboarding={"project_id": "test-project", "project_number": "123456789"})
    sign_in(client)
    review = submit(client, tab(client, "connections"), {"repository": "lab/first"}, action="check-repository")
    failed = submit(client, review, {"repository": "lab/broken", "application": "second"}, action="check-repository")
    assert failed.status_code == 400 and 'value="lab/broken"' in failed.text and 'value="second"' in failed.text
    assert 'value="start-application"' not in failed.text
    assert submit(client, review, {}, action="start-application").status_code == 400
    assert not store.onboardings()


def test_resume_shows_original_preferences_and_existing_destination(site, monkeypatch):
    from labdeploy.controller import Controller
    monkeypatch.setattr(Controller, "check_repository", lambda *args: {"ready": True, "checklist": []})
    client, path, store = site(live_dispatch=True, onboarding={"project_id": "different-project", "project_number": "999999"})
    store.save_onboarding("demo", {"application": "demo", "repository": "lab/demo", "path": "", "as_is": False,
                                   "state": "blocked", "auto_deploy": True, "steps": {}})
    sign_in(client)
    page = submit(client, tab(client, "connections"), {"repository": "lab/demo", "mode": "prepared"}, action="check-repository")
    assert '<dt>Future releases</dt><dd>Automatic' in page.text
    assert '<dt>Google project</dt><dd>test-project' in page.text
    assert 'Continue setup' in page.text
    refreshed = tab(client, "connections")
    assert 'value="lab/demo"' in refreshed.text and 'value="prepared" selected' in refreshed.text
    failed = submit(client, refreshed, {"repository": "lab/different", "application": "demo"}, action="check-repository")
    assert failed.status_code == 400 and "already belongs" in failed.text


def test_connected_google_projects_load_automatically_even_when_retry_needed(site, browser, monkeypatch):
    service, calls = browser
    monkeypatch.setattr("labdeploy.settings.BrowserConnections", lambda config, github: service)
    client, path, store = site()
    sign_in(client)
    start = submit(client, tab(client, "connections"), {}, action="google-connect")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    assert client.get(f"/settings/connections/google/callback?state={state}&code=code").status_code == 200
    assert "Research (target-project)" in tab(client, "connections").text
    start = submit(client, tab(client, "connections"), {}, action="google-connect")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    def unavailable(*args):
        raise httpx.ConnectError("upstream details")

    monkeypatch.setattr(service, "projects", unavailable)
    page = client.get(f"/settings/connections/google/callback?state={state}&code=code")
    assert page.status_code == 200 and "project list did not load" in page.text and "upstream details" not in page.text
    assert "person@example.com" in tab(client, "connections").text


def test_ui_displays_contract_approval_link_and_does_not_claim_live_success(site):
    client, path, store = site()
    store.save_onboarding("new-app", {"application": "new-app", "repository": "lab/new-app", "state": "merging_contract", "principal": "operator",
                                      "steps": {"contract": {"pull_request": 7, "url": "https://github.com/lab/platform/pull/7"}}})
    sign_in(client)
    page = client.get("/settings?tab=connections&onboarding=new-app")
    assert page.status_code == 200 and "Waiting for your approval on GitHub" in page.text
    assert 'href="https://github.com/lab/platform/pull/7"' in page.text
    assert "Setup finished" not in page.text


def test_review_blocks_locally_unselected_repository_before_inspection(site, monkeypatch):
    from labdeploy.controller import Controller

    def forbidden(*args):
        raise AssertionError("A disabled repository must not be read")

    monkeypatch.setattr(Controller, "check_repository", forbidden)
    client, path, store = site(connections={"repositories": []})
    sign_in(client)
    page = submit(client, tab(client, "connections"), {"repository": "lab/not-selected"}, action="check-repository")
    assert page.status_code == 400 and "Select and save" in page.text


@pytest.mark.parametrize("billing,disabled,expected", [(False, False, "Enable billing"), (True, True, "identity is disabled")])
def test_destination_refuses_unready_foundation(browser, billing, disabled, expected):
    service, calls = browser
    key = connect(service)
    original = service.client

    def response(request):
        if request.url.path.endswith("billingInfo"):
            return httpx.Response(200, json={"billingEnabled": billing})
        if "serviceAccounts/" in request.url.path:
            return httpx.Response(200, json={"disabled": disabled})
        return original.send(request)

    service.client = httpx.Client(transport=httpx.MockTransport(response))
    with pytest.raises(ValueError, match=expected):
        service.destination("session", key, "target-project")
    assert service.config.onboarding is None
