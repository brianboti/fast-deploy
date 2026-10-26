import base64
import io
import json
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from labdeploy import scaffold
from labdeploy.resolution_protocol import ResolutionPending
from labdeploy.resolver import Resolver, Settings, command_kind, validate_output
from workers.resolver.build_context import generate
from workers.resolver.worker import validate_input

LOCK = '# uv pip compile\nexample==1.0 \\\n    --hash=sha256:' + 'a' * 64 + '\n'


@pytest.fixture
def remote(setup):
    controller, principal, _ = setup
    job = "projects/worker-project/locations/us-central1/jobs/resolve-python"
    image = "us-central1-docker.pkg.dev/worker-project/resolver/python@sha256:" + "a" * 64
    account = "dependency-worker@worker-project.iam.gserviceaccount.com"
    controller.config.resolver = Settings(bucket="worker-transfers", signer="signer@worker-project.iam.gserviceaccount.com", workers={
        "python-3.12-compile": {"job": job, "image": image, "service_account": account, "network": "workers", "subnet": "workers", "proxy": "http://10.89.0.10:3128"}})
    controller.config.durable_intents, controller.config.backup_bucket = True, "backup-bucket"
    controller.observer = SimpleNamespace(token=lambda: "test")
    state = {"launches": 0, "done": False, "timeout": False, "bad_output": False, "manifests": {}}
    def handler(request):
        if request.url.host == "iamcredentials.googleapis.com":
            return httpx.Response(200, json={"signedBlob": base64.b64encode(b"test-signature").decode()})
        if request.url.host == "storage.googleapis.com":
            if request.method == "POST":
                manifest = json.loads(request.content)
                if "input" in manifest:
                    state["manifests"][manifest["id"]] = manifest
                return httpx.Response(200, json={"generation": "1"})
            identifier = request.url.path.split("outputs/")[-1].removesuffix(".json")
            manifest = state["manifests"][identifier]
            return httpx.Response(200, json={"schema": 1, "id": identifier, "mode": manifest["mode"],
                                 "input_hash": manifest["input_hash"], "output": "evil @ https://evil.test/file\n" if state["bad_output"] else LOCK})
        if request.url.path.endswith("/jobs/resolve-python"):
            return httpx.Response(200, json={"etag": "v1", "template": {"taskCount": 1, "parallelism": 1, "template": {
                "serviceAccount": account, "maxRetries": 0, "vpcAccess": {"egress": "ALL_TRAFFIC", "networkInterfaces": [{"network": "workers", "subnetwork": "workers", "tags": ["resolver-worker"]}]},
                "containers": [{"image": image, "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}, "env": [
                    {"name": "HTTPS_PROXY", "value": "http://10.89.0.10:3128"}, {"name": "HTTP_PROXY", "value": "http://10.89.0.10:3128"}]}]}}})
        if request.url.path.endswith(":run"):
            state["launches"] += 1
            state["launch_body"] = json.loads(request.content)
            if state["timeout"]:
                raise httpx.ReadTimeout("lost response", request=request)
            return httpx.Response(200, json={"name": "projects/worker-project/locations/us-central1/operations/one"})
        if "/operations/" in request.url.path:
            return httpx.Response(200, json={"done": True, "response": {"name": job + "/executions/one"}})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"executions": []})
        if "/executions/" in request.url.path:
            return httpx.Response(200, json={"completionTime": "2026-10-26T00:00:00Z", "succeededCount": 1} if state["done"] else {})
        raise AssertionError(str(request.url))
    resolver = Resolver(controller, httpx.Client(transport=httpx.MockTransport(handler)))
    return resolver, state, principal.name


def submit(resolver, owner):
    with pytest.raises(ResolutionPending) as pending:
        resolver.resolve(owner, {"commit": "a" * 40}, scaffold.lock_container("3.12"), "example==1.0\n")
    return pending.value.identifier


def test_async_worker_result_is_bound_validated_and_reused(remote):
    resolver, state, owner = remote
    identifier = submit(resolver, owner)
    assert submit(resolver, owner) == identifier
    assert state["launches"] == 1
    assert "args" not in state["launch_body"]["overrides"]["containerOverrides"][0]
    assert resolver.status(owner, identifier)["state"] == "running"
    state["done"] = True
    assert resolver.status(owner, identifier)["state"] == "succeeded"
    assert resolver.resolve(owner, {"commit": "a" * 40}, scaffold.lock_container("3.12"), "example==1.0\n") == LOCK
    with pytest.raises(ValueError, match="not found"):
        resolver.status("another-owner", identifier)
    with pytest.raises(ResolutionPending) as changed:
        resolver.resolve(owner, {"commit": "b" * 40}, scaffold.lock_container("3.12"), "example==1.0\n")
    assert changed.value.identifier != identifier


def test_lost_launch_response_does_not_send_again(remote):
    resolver, state, owner = remote
    state["timeout"] = True
    identifier = submit(resolver, owner)
    assert resolver.status(owner, identifier)["state"] == "unknown"
    assert submit(resolver, owner) == identifier
    assert state["launches"] == 1


def test_successful_worker_cannot_publish_an_unsafe_lock(remote):
    resolver, state, owner = remote
    identifier = submit(resolver, owner)
    state.update(done=True, bad_output=True)
    assert resolver.status(owner, identifier)["state"] == "failed"  # final, so the slot is free again
    with resolver.store.connect() as db:
        assert db.execute("SELECT result FROM resolutions WHERE id=?", (identifier,)).fetchone()[0] == "rejected worker output: ValueError"
    with pytest.raises(ValueError, match="Remote resolution failed"):
        submit(resolver, owner)


def test_allowlist_never_accepts_a_shell_supplied_by_the_caller(tmp_path):
    assert command_kind(scaffold.export_container("3.12", True)) == "python-3.12-export-dev"
    with pytest.raises(ValueError, match="allowlist"):
        command_kind(["sh", "-c", "caller code"])
    with pytest.raises(ValueError, match="disabled"):
        scaffold.docker_runner(["anything"], "")
    generate("python-3.12-compile", tmp_path / "build")
    dockerfile = (tmp_path / "build/Dockerfile").read_text()
    assert scaffold.base_image("3.12") in dockerfile and "USER 65532:65532" in dockerfile
    assert "COPY labdeploy" not in dockerfile


@pytest.mark.parametrize("value", ["--index-url https://evil.example\nexample", "example @ https://evil.example/archive", "-r /etc/passwd", "git+https://evil.example/repo"])
def test_worker_rejects_unapproved_python_sources(value):
    with pytest.raises(ValueError):
        validate_input("python-3.12-compile", value)


def test_worker_rejects_export_archive_paths_and_npm_urls():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("../../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="exactly two"):
        validate_input("python-3.12-export", base64.b64encode(archive.getvalue()).decode())
    with pytest.raises(ValueError):
        validate_input("node-24-lock", json.dumps({"dependencies": {"evil": "https://evil.example/package.tgz"}}))
    with pytest.raises(ValueError):
        validate_output("python-3.12-compile", "example==1.0\n")
    validate_input("python-3.12-compile", "example>=1.0\n")


def test_a_worker_that_never_started_does_not_hold_a_slot(remote):
    resolver, state, owner = remote
    resolver.c.config.durable_intents = False
    with pytest.raises(ValueError, match="off-host"):  # refused before anything is reserved
        submit(resolver, owner)
    resolver.c.config.durable_intents = True
    handler = resolver.client._transport.handler
    def signing_down(request):
        return httpx.Response(503) if request.url.host == "iamcredentials.googleapis.com" else handler(request)
    resolver.client._transport.handler = signing_down
    for _ in range(3):
        with pytest.raises(ValueError, match="could not be started"):
            submit(resolver, owner)
    resolver.client._transport.handler = handler
    assert resolver.status(owner, submit(resolver, owner))["state"] == "running"
    assert state["launches"] == 1


def test_the_local_fallback_is_an_explicit_switch(setup):
    controller, principal, _ = setup
    assert controller.config.resolver is None and controller.config.resolver_local_fallback is False
    with pytest.raises(ValueError, match="Local execution is disabled"):
        controller.resolver.runner(principal.name, {"commit": "a" * 40})(scaffold.lock_container("3.12"), "example==1.0\n")
    controller.config.resolver_local_fallback = True
    assert controller.resolver.runner(principal.name, {"commit": "a" * 40}) is scaffold.local_docker_runner
