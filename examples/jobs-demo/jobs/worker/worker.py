"""The worker job of jobs-demo: reads its request from the bucket, does its piece of work, writes its result there.

RUN_ID arrives as the run's override; BUCKET_RUNS is the one bucket the job was given (write). It also tries the internet,
which a job with private egress cannot reach, and says so in its result.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
STORAGE = "https://storage.googleapis.com"


def token():
    request = urllib.request.Request(METADATA, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - the fixed metadata address
        return json.load(response)["access_token"]


def storage(method, path, data=None):
    request = urllib.request.Request(STORAGE + path, data=data, method=method, headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - Cloud Storage only
        return response.read()


def internet():
    try:
        socket.create_connection(("example.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def main():
    run_id, bucket = os.environ["RUN_ID"], os.environ["BUCKET_RUNS"]
    name = urllib.parse.quote(f"runs/{run_id}/request.json", safe="")
    request = json.loads(storage("GET", f"/storage/v1/b/{bucket}/o/{name}?alt=media"))
    result = {"run": run_id, "request": request, "finished": time.time(), "internet_reachable": internet()}
    target = urllib.parse.quote(f"runs/{run_id}/worker.json", safe="")
    storage("POST", f"/upload/storage/v1/b/{bucket}/o?uploadType=media&name={target}", json.dumps(result, indent=2).encode())
    print(json.dumps({"severity": "INFO", "message": f"run {run_id}: worker done, internet reachable: {result['internet_reachable']}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
