"""jobs-demo: the service of an application shaped like CodeSurgeon (docs/DEVELOPER-CONTRACT.md section 4c).

    GET  /health   200 when the service can serve
    GET  /         the runs as JSON: the request in progress and the last ones done
    POST /work     queue a request (any caller Cloud Run let in): the stand-in for a new GitHub issue
    POST /tick     the platform's clock (the caller must be the <service>-wake identity): move the oldest request one step

A request goes through two jobs the service starts itself: the worker (it writes its result to the bucket), then the
checker (built on another application's test image; its verdict is its exit code, read from the execution, never from a
file it wrote). Everything the service remembers lives in the bucket, since an instance's disk is wiped. Standard library
only: the token comes from the metadata server, and the Cloud Run and Cloud Storage APIs are called over HTTPS.
"""
import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
RUN_API = "https://run.googleapis.com/v2"
STORAGE = "https://storage.googleapis.com"
STATE = "state.json"
KEPT = 20


def token():
    request = urllib.request.Request(METADATA, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - the fixed metadata address
        return json.load(response)["access_token"]


def call(method, url, body=None, raw=None, content_type="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Authorization": "Bearer " + token()}
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - Google's APIs only, built here
        text = response.read()
    return json.loads(text) if text else {}


# ---- the bucket: everything the service remembers
def bucket():
    return os.environ["BUCKET_RUNS"]


def read(name, default=None):
    url = f"{STORAGE}/storage/v1/b/{bucket()}/o/{urllib.parse.quote(name, safe='')}?alt=media"
    try:
        return call("GET", url)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return default
        raise


def write(name, value):
    url = f"{STORAGE}/upload/storage/v1/b/{bucket()}/o?uploadType=media&name={urllib.parse.quote(name, safe='')}"
    call("POST", url, raw=json.dumps(value, indent=2).encode())


def queued():
    url = f"{STORAGE}/storage/v1/b/{bucket()}/o?prefix={urllib.parse.quote('queue/')}"
    return sorted(item["name"] for item in call("GET", url).get("items") or [])


def remove(name):
    try:
        call("DELETE", f"{STORAGE}/storage/v1/b/{bucket()}/o/{urllib.parse.quote(name, safe='')}")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


# ---- the jobs: started with per-run arguments, read from their executions
def start(job, run_id):
    """Start one of the service's jobs (JOB_WORKER, JOB_CHECKER) for one request; returns the execution's name."""
    name = os.environ[f"JOB_{job.upper()}"]
    operation = call("POST", f"{RUN_API}/{name}:run", {"overrides": {"containerOverrides": [{"env": [{"name": "RUN_ID", "value": run_id}]}]}})
    execution = (operation.get("metadata") or {}).get("name")
    if execution:
        return execution
    newest = call("GET", f"{RUN_API}/{name}/executions?pageSize=1").get("executions") or []
    return newest[0]["name"] if newest else None


def outcome(execution):
    """None while it runs; else whether it succeeded and the exit code of its task (the verdict)."""
    found = call("GET", f"{RUN_API}/{execution}")
    if not found.get("completionTime"):
        return None
    tasks = call("GET", f"{RUN_API}/{execution}/tasks").get("tasks") or []
    result = (tasks[0].get("lastAttemptResult") or {}) if tasks else {}
    return {"succeeded": bool(found.get("succeededCount")), "exit_code": result.get("exitCode"),
            "cancelled": bool(found.get("cancelledCount")), "completed": found.get("completionTime")}


def tick():
    """One step of the oldest request: start the worker; when it ended, start the checker; when that ended, record both."""
    state = read(STATE, {"current": None, "done": []})
    current = state.get("current")
    if current is None:
        waiting = queued()
        if not waiting:
            return {"step": "idle"}
        request = read(waiting[0], {})
        run_id = request.get("id") or waiting[0].rsplit("/", 1)[-1].removesuffix(".json")
        write(f"runs/{run_id}/request.json", request)
        remove(waiting[0])
        current = {"id": run_id, "stage": "worker", "worker": start("worker", run_id), "started": time.time()}
        state["current"] = current
        write(STATE, state)
        return {"step": "worker started", "run": run_id}
    found = outcome(current[current["stage"]])
    if found is None:
        return {"step": f"{current['stage']} running", "run": current["id"]}
    current[current["stage"] + "_result"] = found
    if current["stage"] == "worker" and found["succeeded"]:
        current.update(stage="checker", checker=start("checker", current["id"]))
        write(STATE, state)
        return {"step": "checker started", "run": current["id"]}
    current["verdict"] = "passed" if current["stage"] == "checker" and found["exit_code"] == 0 else "failed"
    state["done"] = ([current] + state.get("done", []))[:KEPT]
    state["current"] = None
    write(STATE, state)
    return {"step": "done", "run": current["id"], "verdict": current["verdict"]}


# ---- who is calling
def caller(headers):
    """The email of the identity Cloud Run (or IAP) let in. Cloud Run checked the token's signature already."""
    through_iap = headers.get("X-Goog-Authenticated-User-Email") or ""
    if through_iap:
        return through_iap.split(":", 1)[-1]
    words = (headers.get("Authorization") or "").split()
    if len(words) != 2 or words[0].lower() != "bearer" or words[1].count(".") != 2:
        return None
    payload = words[1].split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")
    except ValueError:
        return None


def wake_identity():
    service = os.environ.get("K_SERVICE", "jobs-demo")
    project = os.environ["JOB_WORKER"].split("/")[1]
    return f"{service}-wake@{project}.iam.gserviceaccount.com"


class Handler(BaseHTTPRequestHandler):
    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            return self.answer(200, {"status": "ok"})
        if self.path == "/":
            return self.answer(200, read(STATE, {"current": None, "done": []}))
        return self.answer(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/tick":
                if caller(self.headers) != wake_identity():
                    return self.answer(403, {"error": "only the platform's clock may call /tick"})
                return self.answer(200, tick())
            if self.path == "/work":
                run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{os.getpid() % 1000:03d}"
                write(f"queue/{run_id}.json", {"id": run_id, "asked_by": caller(self.headers), "at": time.time()})
                return self.answer(202, {"queued": run_id})
            return self.answer(404, {"error": "not found"})
        except urllib.error.HTTPError as error:
            # One JSON line per event on stdout, with a severity the platform's views read.
            print(json.dumps({"severity": "ERROR", "message": f"{self.path}: Google API answered {error.code}"}), flush=True)
            return self.answer(502, {"error": f"a Google API answered {error.code}"})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)  # noqa: S104 - Cloud Run's contract
    # SIGTERM ends the loop; closing the server then waits for the requests in flight, so none is cut off.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
