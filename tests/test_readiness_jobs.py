import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from labdeploy import operations
from labdeploy.config import JobBroker
from labdeploy.jobbroker import Broker
from labdeploy.models import AppSpec

SUBJECT = "123456789012345678901"
IMAGE = "us-central1-docker.pkg.dev/test-project/demo/demo-task@sha256:" + "a" * 64


@pytest.fixture
def broker(setup):
    c, _, _ = setup
    c.config.job_broker = JobBroker(audience="https://platform.example/job-broker", subjects={SUBJECT: {"application": "demo", "environment": "staging"}})
    c.config.durable_intents, c.config.backup_bucket = True, "test-backup"
    c.observer = SimpleNamespace(token=lambda: "test")
    spec = AppSpec(name="demo", reviewed=True, jobs={"task": {"dockerfile": "jobs/Dockerfile", "runs_per_day": 2}})
    target = c.config.applications["demo"].target
    c.jobs.targets = lambda: [("demo", "staging", target, spec, "https://demo.run.app")]
    c.store.latest_success = lambda *a, **k: {"receipt": {"jobs": {"task": {"image": IMAGE}}}}
    cloud = {"launches": 0, "completed": False, "timeout": False, "closed": False, "witnesses": [], "execution": None}
    name = "projects/test-project/locations/us-central1/jobs/demo-task"
    def handler(request):
        path = request.url.path
        if "storage.googleapis.com" in str(request.url):
            cloud["witnesses"].append(json.loads(request.content))
            return httpx.Response(200, json={"generation": "1"})
        if path.endswith("/services/demo"):
            return httpx.Response(200, json={"ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY" if cloud["closed"] else "INGRESS_TRAFFIC_ALL"})
        if path.endswith("/jobs/demo-task"):
            return httpx.Response(200, json={"etag": "v1", "template": {"template": {"maxRetries": 0,
                                 "serviceAccount": "demo-task@test-project.iam.gserviceaccount.com",
                                 "containers": [{"image": IMAGE, "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]}}})
        if path.endswith(":run"):
            cloud["launches"] += 1
            cloud["body"] = json.loads(request.content)
            cloud["identifier"] = cloud["body"]["overrides"]["containerOverrides"][0]["env"][0]["value"]
            if cloud["timeout"]:
                raise httpx.ReadTimeout("lost response", request=request)
            return httpx.Response(200, json={"name": "projects/test-project/locations/us-central1/operations/one"})
        if "/operations/" in path:
            return httpx.Response(200, json={"done": True, "response": {"name": name + "/executions/one"}})
        if path.endswith("/executions"):
            return httpx.Response(200, json={"executions": [{"name": name + "/executions/one", "template": {"containers": [{"env": [
                {"name": "FAST_DEPLOY_OPERATION_ID", "value": cloud["identifier"]}]}]}}]})
        if "/executions/" in path:
            return httpx.Response(200, json={"completionTime": "2026-10-26T00:00:00Z", "succeededCount": 1} if cloud["completed"] else {})
        raise AssertionError(str(request.url))
    instance = Broker(c, httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: 1792972800)
    return instance, cloud


def test_hundred_concurrent_requests_create_one_execution(broker):
    broker, cloud = broker
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: broker.submit(SUBJECT, {"job": "task", "idempotency_key": "same-key"}), range(100)))
    assert len({item["id"] for item in results}) == 1
    assert cloud["launches"] == 1
    assert cloud["witnesses"][0]["id"] == results[0]["id"]
    assert cloud["body"]["overrides"]["taskCount"] == 1
    with pytest.raises(ValueError, match="different arguments"):
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "same-key", "arguments": ["changed"]})


def test_creation_operation_is_not_execution_completion_and_quota_never_refunds(broker):
    broker, cloud = broker
    first = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key"})
    assert broker.status(SUBJECT, first["id"])["state"] == "running"
    with pytest.raises(ValueError, match="limit"):
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "second-key"})
    cloud["completed"] = True
    assert broker.status(SUBJECT, first["id"])["state"] == "succeeded"
    second = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "second-key"})
    assert broker.status(SUBJECT, second["id"])["state"] == "succeeded"
    with pytest.raises(ValueError, match="limit"):
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "third-key"})
    assert cloud["launches"] == 2


def test_timeout_stays_charged_and_is_reconciled_without_resending(broker):
    broker, cloud = broker
    cloud["timeout"] = True
    first = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key", "arguments": ["private-argument"]})
    assert first["state"] == "unknown"
    assert "private-argument" not in json.dumps(cloud["witnesses"])
    assert broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key", "arguments": ["private-argument"]})["id"] == first["id"]
    with pytest.raises(ValueError, match="limit"):
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "second-key"})
    assert broker.status(SUBJECT, first["id"])["state"] == "running"
    assert cloud["launches"] == 1


def test_closed_application_and_unknown_identity_cannot_launch(broker):
    broker, cloud = broker
    cloud["closed"] = True
    with pytest.raises(ValueError, match="spending cap"):
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key"})
    with pytest.raises(ValueError, match="not registered"):
        broker.submit("unregistered", {"job": "task", "idempotency_key": "first-key"})
    assert cloud["launches"] == 0


def test_workload_jwt_binds_issuer_audience_subject_and_signature(broker):
    broker, _ = broker
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    broker.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key()))
    claims = {"iss": "https://accounts.google.com", "aud": broker.config.job_broker.audience, "sub": SUBJECT, "iat": int(time.time()), "exp": int(time.time()) + 300}
    assert broker.authenticate("Bearer " + jwt.encode(claims, key, algorithm="RS256")) == SUBJECT
    for changes in ({"aud": "https://other.example"}, {"iss": "https://evil.example"}, {"exp": 1}, {"sub": "unknown"}):
        with pytest.raises(ValueError):
            broker.authenticate("Bearer " + jwt.encode({**claims, **changes}, key, algorithm="RS256"))
    with pytest.raises(ValueError):
        broker.authenticate("Bearer " + jwt.encode(claims, "wrong" * 8, algorithm="HS256"))


def test_restore_recovers_quota_from_witness_without_private_arguments(broker, tmp_path):
    broker, cloud = broker
    first = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key", "arguments": ["private-argument"]})
    from labdeploy.controller import Store
    restored = Store(tmp_path / "restored.db")
    controller = SimpleNamespace(store=restored, config=broker.config)
    Broker(controller)
    operations.restore_intents(restored, cloud["witnesses"])
    with restored.connect() as db:
        row = db.execute("SELECT state,payload,day FROM job_admissions WHERE id=?", (first["id"],)).fetchone()
    assert row[0] == "unknown" and "private-argument" not in row[1]
    assert row[2] == first["charged_day"]
    assert restored.recovery_mode()


def test_a_launch_that_never_left_frees_its_slot_but_keeps_the_days_charge(broker):
    broker, cloud = broker
    witness_write = broker.client._transport.handler
    def failing(request):
        if "storage.googleapis.com" in str(request.url):
            return httpx.Response(503)  # the intent bucket is down: nothing is sent to Cloud Run
        return witness_write(request)
    broker.client._transport.handler = failing
    first = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key"})
    assert first["state"] == "not_executed" and cloud["launches"] == 0
    broker.client._transport.handler = witness_write
    second = broker.submit(SUBJECT, {"job": "task", "idempotency_key": "second-key"})
    assert second["state"] == "running" and cloud["launches"] == 1
    with pytest.raises(ValueError, match="limit"):  # runs_per_day is 2: the run that never started still counts
        broker.submit(SUBJECT, {"job": "task", "idempotency_key": "third-key"})


def test_an_operator_can_free_a_slot_only_with_evidence(broker, tmp_path, monkeypatch):
    from labdeploy import reconcile
    instance, cloud = broker
    cloud["timeout"] = True
    first = instance.submit(SUBJECT, {"job": "task", "idempotency_key": "first-key"})
    assert first["state"] == "unknown"
    monkeypatch.setattr(reconcile, "load_config", lambda path: instance.config)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"operator": "brian", "operation": first["id"], "no_execution_found": False}))
    with pytest.raises(ValueError, match="Evidence"):
        reconcile.main(["--config", "x", "release-job", "--operation", first["id"], "--evidence", str(evidence)])
    evidence.write_text(json.dumps({"operator": "brian", "operation": first["id"], "no_execution_found": True}))
    reconcile.main(["--config", "x", "release-job", "--operation", first["id"], "--evidence", str(evidence)])
    cloud["timeout"] = False
    assert instance.submit(SUBJECT, {"job": "task", "idempotency_key": "second-key"})["state"] == "running"
