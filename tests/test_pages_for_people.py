"""Phase 8: pages for people through Identity-Aware Proxy (exposure: lab-members, viewers, the runner's checks)."""
import json

import pytest
from conftest import TARGET

from labdeploy.config import Config, Onboarding
from labdeploy.infra import app_root, check_plan
from labdeploy.models import AppSpec
from labdeploy.release import CloudRun

ROOT = dict(project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/demo",
            github_repository_id="123", github_owner_id="45", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)


def test_exposure_accepts_lab_members_and_never_public():
    assert AppSpec.model_validate({"name": "demo", "exposure": "lab-members", "reviewed": True}).exposure == "lab-members"
    assert AppSpec.model_validate({"name": "demo"}).exposure == "authenticated"
    with pytest.raises(ValueError):
        AppSpec.model_validate({"name": "demo", "exposure": "public"})


def test_root_renders_the_proxy_and_its_viewers():
    files = app_root("demo", **ROOT, exposure="lab-members", viewers=["user:b@example.edu", "group:lab@example.edu", "user:b@example.edu"])
    assert 'exposure                     = "lab-members"' in files["main.tf"]
    assert 'viewers                      = ["group:lab@example.edu", "user:b@example.edu"]' in files["main.tf"]  # sorted, unique
    assert "iap_client_id" in files["main.tf"] and "data.terraform_remote_state.project.outputs.iap_client_id" in files["main.tf"]
    plain = app_root("demo", **ROOT)
    assert 'exposure                     = "authenticated"' in plain["main.tf"] and "viewers                      = []" in plain["main.tf"]
    with pytest.raises(ValueError, match="at least one viewer"):
        app_root("demo", **ROOT, exposure="lab-members")
    with pytest.raises(ValueError, match="public access is never an option"):
        app_root("demo", **ROOT, exposure="public", viewers=["user:b@example.edu"])
    with pytest.raises(ValueError, match="user:EMAIL"):
        app_root("demo", **ROOT, exposure="lab-members", viewers=["allUsers"])


def test_policy_allows_the_viewer_grant_and_nothing_wider():
    def grant(address, kind, role, member):
        return {"address": address, "type": kind, "change": {"actions": ["create"], "after": {"role": role, "member": member}, "after_unknown": {}}}
    accessor, viewer_grant = "roles/iap.httpsResourceAccessor", "google_iap_web_cloud_run_service_iam_member"
    plan = {"resource_changes": [
        grant("ok", viewer_grant, accessor, "user:b@example.edu"),
        grant("agent", "google_cloud_run_v2_service_iam_member", "roles/run.invoker", "serviceAccount:service-1@gcp-sa-iap.iam.gserviceaccount.com"),
        grant("bad", viewer_grant, accessor, "allAuthenticatedUsers"),
    ]}
    assert {v["address"]: v["rule"] for v in check_plan(plan)} == {"bad": "no_public_access"}


def test_viewers_are_validated_in_the_configuration():
    base = {"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "viewers": ["user:b@example.edu"]}}, "principals": []}
    config = Config.model_validate({**base, "onboarding": {"project_id": "test-project", "project_number": "123456789", "default_viewers": ["domain:example.edu"]}})
    assert config.applications["demo"].viewers == ["user:b@example.edu"] and config.onboarding.default_viewers == ["domain:example.edu"]
    with pytest.raises(ValueError, match="Viewer"):
        Config.model_validate({**base, "applications": {"demo": {**base["applications"]["demo"], "viewers": ["everyone"]}}})
    with pytest.raises(ValueError):
        Onboarding(project_id="test-project", project_number="1", iap_client_id="not-a-client")


def test_runner_checks_the_proxy_against_the_spec_and_probes_through_it(monkeypatch):
    calls, annotations = [], {}

    def recorder(args):
        calls.append(args)
        if "describe" in args and "services" in args:
            return json.dumps({"metadata": {"annotations": annotations}})
        if args[:3] == ["gcloud", "auth", "print-access-token"]:
            return "access"
        return ""
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    people = AppSpec.model_validate({"name": "demo", "exposure": "lab-members", "reviewed": True})
    programs = AppSpec.model_validate({"name": "demo", "reviewed": True})
    with pytest.raises(ValueError, match="not enabled"):
        adapter.require_exposure(people)
    adapter.require_exposure(programs)  # no proxy, programs only: fine
    annotations["run.googleapis.com/iap-enabled"] = "true"
    with pytest.raises(ValueError, match="says exposure: authenticated"):
        adapter.require_exposure(programs)
    with pytest.raises(ValueError, match="IAP_CLIENT_ID"):
        adapter.require_exposure(people)
    with pytest.raises(ValueError, match="OAuth client id"):
        CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder, iap_client_id="nope")
    behind = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder, iap_client_id="123-abc.apps.googleusercontent.com")
    behind.require_exposure(people)
    # the probe's token audience is the proxy's client, not the service URL
    import urllib.request
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"token": "id-token"}'

    def fake_urlopen(req, timeout=0):
        if "generateIdToken" in req.full_url:
            seen["audience"] = json.loads(req.data)["audience"]
            return Response()
        seen["probe"] = req.full_url
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, req, timeout=0: fake_urlopen(req))
    monkeypatch.setattr(json, "load", lambda response: {"token": "id-token"})
    assert behind.health("https://demo-abc-uc.a.run.app", "https://demo-abc-uc.a.run.app", "/health") is True
    assert seen["audience"] == "123-abc.apps.googleusercontent.com" and seen["probe"].endswith("/health")
