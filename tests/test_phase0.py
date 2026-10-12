"""Phase 0: cost overheads, stale-price warning, plain GitHub errors, controller status, pin sync, recovery schedule."""
import datetime
import hashlib
import tomllib
from pathlib import Path

import httpx
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import __version__
from labdeploy.cli import sync_pins
from labdeploy.config import Config
from labdeploy.cost import PRICING, estimate
from labdeploy.github import GitHub, explain, parse_expiration
from labdeploy.models import AppSpec
from labdeploy.server import build_app

MCP = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
NEW_CHECKOUT = "3d3c42e5aac5ba805825da76410c181273ba90b1"
OLD_CHECKOUT = "11d5960a326750d5838078e36cf38b85af677262"


def config(tmp_path):
    return Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "database": str(tmp_path / "db"),
                                  "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})


def call(client, name, **arguments):
    return client.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]


def failure(status, headers=None, json=None):
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, headers=headers, json=json, request=request))


def test_estimate_lists_overheads_and_warns_when_prices_are_stale():
    spec = AppSpec(name="demo", reviewed=True)
    verified = datetime.date.fromisoformat(PRICING["verified"])
    fresh = estimate(spec, today=verified + datetime.timedelta(days=10))
    assert fresh["release_overheads"]["image_scan_usd_per_release"] == 0.26 and "0.26" in fresh["release_overheads"]["image_scan"]
    assert "private repositories" in fresh["release_overheads"]["actions_minutes"] and fresh["warnings"] == []
    stale = estimate(spec, today=verified + datetime.timedelta(days=120))
    assert len(stale["warnings"]) == 1 and "120 days" in stale["warnings"][0] and PRICING["source"] in stale["warnings"][0]


def test_github_failures_are_explained_in_plain_words():
    assert "expired or revoked" in explain(failure(401))
    assert "resets at" in explain(failure(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}))
    assert "permission" in explain(failure(403))
    assert "lab/demo" in explain(failure(404), "lab/demo")
    assert "regenerate" in explain(failure(422, json={"message": "Unexpected inputs provided: plan_id"}))
    assert "unavailable" in explain(failure(502))
    assert "did not answer" in explain(httpx.ReadTimeout("slow"))
    assert "Could not reach" in explain(httpx.ConnectError("down"))
    assert explain(ValueError("other")) is None


def test_tools_and_rest_report_github_failures_plainly(tmp_path):
    class Rejected(FakeGitHub):
        def commit(self, app, ref=None):
            raise failure(401)
    with TestClient(build_app(config(tmp_path), Rejected())) as client:
        result = call(client, "create_deployment_plan", application="demo")
        assert result["isError"] and "expired or revoked" in result["content"][0]["text"]
        rest = client.post("/applications/demo/plans", headers={"Authorization": "Bearer test-token"})
        assert rest.status_code == 502 and "expired or revoked" in rest.json()["error"]


def test_missing_platform_yaml_explains_what_to_do(tmp_path):
    class NoConfig(FakeGitHub):
        def file(self, app, sha, filename):
            raise failure(404)
    with TestClient(build_app(config(tmp_path), NoConfig())) as client:
        for tool in ("create_deployment_plan", "estimate_cost"):
            result = call(client, tool, application="demo")
            assert result["isError"] and "propose_configuration" in result["content"][0]["text"] and "lab/demo" in result["content"][0]["text"]


def test_controller_status_and_handshake_report_the_platform_version(tmp_path):
    class Expiring(FakeGitHub):
        def credential_status(self):
            return {"kind": "fine_grained_token", "expires_at": "2026-10-20T00:00:00+00:00", "days_left": 7, "warning": "The GitHub credential expires in 7 day(s)"}
    with TestClient(build_app(config(tmp_path), Expiring())) as client:
        params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        init = client.post("/mcp", headers=MCP, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}).json()["result"]["serverInfo"]
        assert init["name"] == "Lab Deploy" and init["version"] == __version__
        status = call(client, "get_controller_status")["structuredContent"]
        assert status["version"] == __version__ and status["applications"] == ["demo"] and status["github_credential"]["days_left"] == 7
        assert "expires in 7" in status["github_credential"]["warning"] and status["pricing_verified"] == PRICING["verified"]
        assert status["live_dispatch"] is False and status["oauth_sign_in"] is False and status["uptime_seconds"] >= 0
    with TestClient(build_app(config(tmp_path), FakeGitHub())) as client:  # adapters without the check still answer
        assert call(client, "get_controller_status")["structuredContent"]["github_credential"]["kind"] == "unknown"


def test_version_matches_pyproject():
    assert tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"] == __version__


def test_credential_status_reads_githubs_expiration_header(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")

    def handler(request):
        assert request.url.path == "/rate_limit" and request.headers["Authorization"] == "Bearer github_pat_test"
        return httpx.Response(200, headers={"github-authentication-token-expiration": "2026-10-20 12:00:00 UTC"}, json={})
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    soon = github.credential_status(now=datetime.datetime(2026, 10, 10, tzinfo=datetime.UTC))
    assert soon["kind"] == "fine_grained_token" and soon["days_left"] == 10 and "expires in 10" in soon["warning"]
    later = github.credential_status(now=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC))
    assert later["days_left"] == 80 and later["warning"] is None
    expired = github.credential_status(now=datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC))
    assert "has expired" in expired["warning"]
    assert parse_expiration("garbage") is None and parse_expiration("") is None

    def no_header(request):
        return httpx.Response(200, json={})
    assert GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(no_header))).credential_status()["expires_at"] is None


def test_sync_pins_copies_ci_pins_into_templates(tmp_path):
    ci = tmp_path / "ci.yml"
    ci.write_text(f"jobs:\n  t:\n    steps:\n      - uses: actions/checkout@{NEW_CHECKOUT} # v7.0.1\n      - uses: other/action@{'c' * 40}\n")
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "deploy.yml").write_text(f"steps:\n  - uses: actions/checkout@{OLD_CHECKOUT} # v4, verified 2026-10-12\n    with:\n      path: app\n"
                                          f"  - uses: actions/checkout@{OLD_CHECKOUT}\n  - uses: google/auth@{'a' * 40} # v3\n")
    changed = sync_pins(ci, templates)
    assert changed == [f"deploy.yml: actions/checkout {OLD_CHECKOUT[:12]} -> {NEW_CHECKOUT[:12]}"] * 2
    text = (templates / "deploy.yml").read_text()
    assert text.count(f"actions/checkout@{NEW_CHECKOUT} # v7.0.1\n") == 2 and OLD_CHECKOUT not in text
    assert f"google/auth@{'a' * 40} # v3" in text and "      path: app" in text  # untouched lines keep their pins and indentation
    assert sync_pins(ci, templates) == []


def test_recovery_schedule_is_a_safety_net_not_a_heartbeat():
    assert "cron: '7 */6 * * *'" in Path("templates/recover-gcp.yml").read_text()
