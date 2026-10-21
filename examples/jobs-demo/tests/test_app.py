"""jobs-demo's own tests: the steps a request goes through, and who may call /tick. No cloud: the bucket and the jobs are fakes."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

JOB = "projects/lab-proj-123/locations/us-central1/jobs/jobs-demo-{}"


class Cloud:
    """The bucket as a dict, and each job's executions as the Cloud Run API would describe them."""

    def __init__(self):
        self.objects, self.started, self.finished = {}, [], {}

    def read(self, name, default=None):
        return json.loads(json.dumps(self.objects[name])) if name in self.objects else default

    def write(self, name, value):
        self.objects[name] = json.loads(json.dumps(value))

    def queued(self):
        return sorted(name for name in self.objects if name.startswith("queue/"))

    def remove(self, name):
        self.objects.pop(name, None)

    def start(self, job, run_id):
        execution = f"{JOB.format(job)}/executions/{job}-{len(self.started)}"
        self.started.append((job, run_id))
        return execution

    def outcome(self, execution):
        return self.finished.get(execution)


def install(monkeypatch):
    cloud = Cloud()
    for name in ("read", "write", "queued", "remove", "start", "outcome"):
        monkeypatch.setattr(app, name, getattr(cloud, name))
    monkeypatch.setenv("JOB_WORKER", JOB.format("worker"))
    monkeypatch.setenv("K_SERVICE", "jobs-demo")
    return cloud


def test_a_request_goes_through_the_worker_then_the_checker_and_the_verdict_is_the_checkers_exit_code(monkeypatch):
    cloud = install(monkeypatch)
    assert app.tick() == {"step": "idle"}
    cloud.objects["queue/r1.json"] = {"id": "r1"}
    assert app.tick() == {"step": "worker started", "run": "r1"}
    assert cloud.started == [("worker", "r1")] and "queue/r1.json" not in cloud.objects and cloud.objects["runs/r1/request.json"] == {"id": "r1"}
    assert app.tick() == {"step": "worker running", "run": "r1"}
    worker = cloud.objects["state.json"]["current"]["worker"]
    cloud.finished[worker] = {"succeeded": True, "exit_code": 0, "cancelled": False, "completed": "t"}
    assert app.tick() == {"step": "checker started", "run": "r1"} and cloud.started[-1] == ("checker", "r1")
    checker = cloud.objects["state.json"]["current"]["checker"]
    cloud.finished[checker] = {"succeeded": False, "exit_code": 3, "cancelled": False, "completed": "t"}
    assert app.tick() == {"step": "done", "run": "r1", "verdict": "failed"}
    state = cloud.objects["state.json"]
    assert state["current"] is None and state["done"][0]["checker_result"]["exit_code"] == 3


def test_a_failed_worker_ends_the_request_without_a_checker(monkeypatch):
    cloud = install(monkeypatch)
    cloud.objects["queue/r2.json"] = {"id": "r2"}
    app.tick()
    cloud.finished[cloud.objects["state.json"]["current"]["worker"]] = {"succeeded": False, "exit_code": 1, "cancelled": True, "completed": "t"}
    assert app.tick() == {"step": "done", "run": "r2", "verdict": "failed"} and [job for job, _ in cloud.started] == ["worker"]


def token(email):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"Bearer {part({'alg': 'RS256'})}.{part({'email': email})}.signature"


def test_only_the_wake_identity_may_call_tick(monkeypatch):
    install(monkeypatch)
    assert app.wake_identity() == "jobs-demo-wake@lab-proj-123.iam.gserviceaccount.com"
    assert app.caller({"Authorization": token("jobs-demo-wake@lab-proj-123.iam.gserviceaccount.com")}) == app.wake_identity()
    assert app.caller({"Authorization": token("someone@example.org")}) != app.wake_identity()
    assert app.caller({"X-Goog-Authenticated-User-Email": "accounts.google.com:jobs-demo-wake@lab-proj-123.iam.gserviceaccount.com"}) == app.wake_identity()
    assert app.caller({}) is None and app.caller({"Authorization": "Bearer not-a-token"}) is None
