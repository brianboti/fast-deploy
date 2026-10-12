"""Runs only on the deployment runner, never inside Claude or the MCP server."""
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .models import enforce, parse_spec, spec_hash


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=600).stdout.strip()


def describe_error(error):
    """Short, log-safe description of a failure: a command's ERROR line when there is one, else its last stderr line."""
    lines = [line.strip() for line in (getattr(error, "stderr", "") or "").splitlines() if line.strip()]
    chosen = next((line for line in lines if line.startswith("ERROR")), lines[-1] if lines else f"{type(error).__name__}: {error}")
    return chosen[:300]


class CloudRun:
    def __init__(self, project, region, service, probe_account, runner=run):
        self.project, self.region, self.service = project, region, service
        self.probe_account, self.run = probe_account, runner

    def command(self, *args):
        return ["gcloud", "run", *args, "--project", self.project, "--region", self.region, "--quiet"]

    def describe(self):
        return json.loads(self.run(self.command("services", "describe", self.service, "--format=json")))

    def require_private_service(self):
        policy = json.loads(self.run(self.command("services", "get-iam-policy", self.service, "--format=json")))
        for binding in policy.get("bindings", []):
            if any(member in {"allUsers", "allAuthenticatedUsers"} for member in binding.get("members", [])):
                raise ValueError("Remove public IAM grants before deploying this authenticated-only workload")
        annotations = self.describe().get("metadata", {}).get("annotations", {})
        if annotations.get("run.googleapis.com/invoker-iam-disabled") == "true":
            raise ValueError("Cloud Run invoker IAM check must remain enabled")

    def traffic(self, allocations):
        if not allocations or sum(allocations.values()) != 100:
            raise ValueError("Expected explicit revision traffic totaling 100%")
        try:
            self.run(self.command("services", "update-traffic", self.service, "--to-revisions", ",".join(f"{k}={v}" for k, v in sorted(allocations.items()))))
        except subprocess.CalledProcessError as error:
            # Observed live: gcloud applies the split and then exits non-zero because the service's newest revision,
            # a candidate that never became ready, is not Ready. The routing state is the truth, so verify it.
            try:
                observed = traffic_snapshot(self.describe()["status"])
            except (ValueError, KeyError):
                observed = None
            if observed != allocations:
                raise RuntimeError(f"Traffic update failed: {describe_error(error)}") from None

    def remove_candidate(self):
        try:
            self.run(self.command("services", "update-traffic", self.service, "--remove-tags=candidate"))
        except subprocess.CalledProcessError as error:
            description = self.describe()
            entries = list(description.get("spec", {}).get("traffic") or []) + list(description.get("status", {}).get("traffic") or [])
            if any(entry.get("tag") == "candidate" for entry in entries):
                raise RuntimeError(f"Candidate tag removal failed: {describe_error(error)}") from None

    def ready(self):
        conditions = self.describe().get("status", {}).get("conditions", [])
        return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)

    def deploy(self, image, spec, suffix, labels=None):
        # Cloud Run gates traffic on this HTTP startup probe (up to 60 s of grace) before the platform's own probes run.
        probe = (f"httpGet.path={spec.health_path},httpGet.port={spec.port},"
                 "initialDelaySeconds=0,periodSeconds=5,timeoutSeconds=3,failureThreshold=12")
        extra = []
        if labels:
            for key, value in labels.items():
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", key) or not re.fullmatch(r"[a-z0-9_-]{0,63}", value):
                    raise ValueError("Revision labels must be lowercase letters, digits, dashes or underscores")
            extra.append("--labels=" + ",".join(f"{key}={value}" for key, value in sorted(labels.items())))
        self.run(self.command("deploy", self.service, "--image", image,
            "--revision-suffix", suffix, "--no-traffic", "--tag=candidate", "--startup-probe", probe,
            "--ingress=all", "--port", str(spec.port),
            "--cpu", str(spec.resources.cpu), "--memory", f"{spec.resources.memory_mib}Mi",
            "--min-instances", str(spec.scaling.minimum), "--max-instances", str(spec.scaling.maximum),
            "--concurrency", str(spec.concurrency), "--timeout=60", "--cpu-throttling", *extra))

    def revision_logs(self, revision, limit=50):
        """The last log lines a revision wrote, oldest first: the answer to 'why did the candidate not start?'."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", revision):
            raise ValueError("Invalid revision name")
        query = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{self.service}" AND resource.labels.revision_name="{revision}"')
        raw = self.run(["gcloud", "logging", "read", query, "--project", self.project, "--limit", str(limit), "--freshness=2h", "--order=desc", "--format=json"])
        lines = []
        for entry in reversed(json.loads(raw or "[]")):
            payload = entry.get("textPayload")
            if payload is None and isinstance(entry.get("jsonPayload"), dict):
                payload = entry["jsonPayload"].get("message") or json.dumps(entry["jsonPayload"])
            if payload is None and isinstance(entry.get("httpRequest"), dict):
                request = entry["httpRequest"]
                payload = f"{request.get('status')} {request.get('requestMethod')} {request.get('requestUrl')}"
            if payload is None and isinstance(entry.get("protoPayload"), dict):
                payload = (entry["protoPayload"].get("status") or {}).get("message")
            lines.append({"at": entry.get("timestamp"), "severity": entry.get("severity"), "message": str(payload or "")[:500]})
        return lines

    def revision_image(self, revision):
        return self.run(self.command("revisions", "describe", revision, "--format=value(status.imageDigest)"))

    def health(self, url, audience, path):
        # Both URLs come from the Cloud Run API, not the application or a tool argument.
        for value in (url, audience):
            if not re.fullmatch(r"https://[a-zA-Z0-9.-]+\.run\.app", value):
                raise ValueError("Health URL must be an HTTPS Cloud Run URL")
        access = self.run(["gcloud", "auth", "print-access-token"])
        req = urllib.request.Request(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{self.probe_account}:generateIdToken",
            data=json.dumps({"audience": audience, "includeEmail": True}).encode(),
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed https URL built above
            token = json.load(response)["token"]
        # Do not follow redirects with an identity token.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        for attempt in range(6):
            try:
                req = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})  # noqa: S310 - https://*.run.app only
                with opener.open(req, timeout=15) as response:
                    if response.status == 200:
                        return True
            except (OSError, ValueError):
                pass
            if attempt < 5:
                time.sleep(5)
        return False


TERMINAL = {"succeeded", "traffic_restored", "recovered"}
IMAGE_DIGEST = re.compile(r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+@sha256:[a-f0-9]{64}")


def now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def reconcile_serving_tags(adapter, image_path, runner=run):
    """Tag each serving revision's image digest `serving-<revision>` and drop such tags for revisions that no longer serve.

    Registry cleanup keeps every image with a `serving-` tag, so an image that still serves traffic, including one a
    rollback returned to, is never deleted. Called after each successful release and by the weekly right-sizing job.
    """
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+", image_path):
        raise ValueError("Unexpected image path")
    traffic = adapter.describe().get("status", {}).get("traffic", [])
    serving = sorted({item["revisionName"] for item in traffic if item.get("percent") and isinstance(item.get("revisionName"), str)})
    listed = json.loads(runner(["gcloud", "artifacts", "docker", "tags", "list", image_path, "--format=json"]) or "[]")
    current = {entry["tag"].rsplit("/", 1)[-1] for entry in listed if isinstance(entry.get("tag"), str) and entry["tag"].rsplit("/", 1)[-1].startswith("serving-")}
    wanted = {}
    for revision in serving:
        digest = adapter.revision_image(revision)
        if IMAGE_DIGEST.fullmatch(digest):
            wanted[f"serving-{revision}"] = digest
    added, removed = [], []
    for tag, digest in wanted.items():
        if tag not in current:
            runner(["gcloud", "artifacts", "docker", "tags", "add", digest, f"{image_path}:{tag}"])
            added.append(tag)
    for tag in sorted(current - set(wanted)):
        runner(["gcloud", "artifacts", "docker", "tags", "delete", f"{image_path}:{tag}", "--quiet"])
        removed.append(tag)
    return {"serving": sorted(wanted), "added": added, "removed": removed}


def traffic_snapshot(status):
    allocations = {}
    for item in status.get("traffic", []):
        percent = item.get("percent", 0)
        if percent:
            revision = item.get("revisionName")
            if not isinstance(revision, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", revision):
                raise ValueError("Cannot snapshot concrete previous traffic revisions")
            if type(percent) is not int or not 0 < percent <= 100:
                raise ValueError("Invalid traffic percentage")
            allocations[revision] = allocations.get(revision, 0) + percent
    if sum(allocations.values()) != 100:
        raise ValueError("Bootstrap a healthy service before releasing; rollback target required")
    return allocations


def recover(adapter, journal, target):
    """Called under the same GitHub concurrency group as releases, on a separate runner."""
    saved = journal.read()
    if saved is None:
        return {"state": "no_recovery_needed"}
    if saved.get("target") != target:
        raise ValueError("Recovery journal target differs from configured destination")
    if saved.get("state") in TERMINAL:
        return {"state": "no_recovery_needed", "last_release": saved["state"]}
    if saved.get("state") not in {"preparing", "deploying", "promoting", "rolling_back", "rollback_failed_manual_recovery_required"}:
        raise ValueError("Unknown recovery journal state; manual review required")
    plan_id = saved.get("plan_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Invalid recovery plan ID")
    old = saved["previous_traffic"]
    verified = traffic_snapshot({"traffic": [{"revisionName": k, "percent": v} for k, v in old.items()]})
    if any(not r.startswith(adapter.service + "-") for r in verified):
        raise ValueError("Recovery revision belongs to another service")
    candidate = adapter.service + "-r" + plan_id[:16]
    current = traffic_snapshot(adapter.describe()["status"])
    if set(current) - (set(verified) | {candidate}):
        raise ValueError("Traffic changed outside this release; automatic recovery refused")
    adapter.traffic(verified)
    saved["state"] = "recovered"
    journal.write(saved)
    retire_candidate(adapter, saved)
    journal.write(saved)
    return saved


def retire_candidate(adapter, record):
    """After traffic is safely off the candidate, drop its tag and record whether the service reports Ready.

    Cloud Run never allows deleting a service's newest revision, so a failed candidate stays in place with no
    traffic; the service reports not Ready until the next successful release while the previous revision serves.
    """
    try:
        adapter.remove_candidate()
        record.pop("cleanup_warning", None)
    except Exception as error:
        record["cleanup_warning"] = f"Candidate tag cleanup failed: {describe_error(error)}"
    try:
        record["service_ready"] = adapter.ready()
    except Exception:
        record["service_ready"] = None
    if record.get("service_ready") is False:
        record["note"] = ("The failed candidate remains the newest revision with no traffic, so Cloud Run reports the service as "
                          "not ready until the next successful release; requests are served by the previous revision.")


def rollout(adapter, image, spec, plan_id, receipt_path, journal=None, target=None, labels=None, image_path=None):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Invalid plan ID")
    if not IMAGE_DIGEST.fullmatch(image):
        raise ValueError("An immutable Artifact Registry image digest is required")
    enforce(spec)
    previous = adapter.describe()
    status = previous["status"]
    allocations = traffic_snapshot(status)
    suffix = "r" + plan_id[:16]
    revision = f"{adapter.service}-{suffix}"
    receipt = {"plan_id": plan_id, "image": image, "previous_traffic": allocations, "state": "preparing", "target": target, "phases": []}
    def save():
        # Each state change is timestamped so the controller can show how long every phase took.
        if not receipt["phases"] or receipt["phases"][-1]["state"] != receipt["state"]:
            receipt["phases"].append({"state": receipt["state"], "at": now_iso()})
        if journal is not None:
            journal.write(receipt)
        Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n")
    # Fail before any cloud mutation if the recovery checkpoint cannot be saved.
    save()
    try:
        # Freeze any LATEST allocation to concrete revisions before creating a new revision.
        adapter.traffic(allocations)
        receipt["state"] = "deploying"
        save()
        adapter.deploy(image, spec, suffix, labels)
        candidate = adapter.describe()["status"]
        entry = next((t for t in candidate.get("traffic", []) if t.get("tag") == "candidate" and t.get("revisionName") == revision), None)
        if not entry or not adapter.health(entry["url"], candidate["url"], spec.health_path):
            raise RuntimeError("Candidate health check failed")
        receipt["state"] = "promoting"
        save()
        adapter.traffic({revision: 100})
        if not adapter.health(candidate["url"], candidate["url"], spec.health_path):
            raise RuntimeError("Post-promotion health check failed")
        receipt.update(state="succeeded", revision=revision, url=candidate["url"])
        save()
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        # Graceful termination gets an immediate recovery attempt. A killed runner is
        # handled by the durable journal and separate recovery workflow instead.
        receipt["error"] = describe_error(error)
        print(f"Release step failed: {receipt['error']}", file=sys.stderr)
        try:
            adapter.traffic(allocations)
            receipt["state"] = "traffic_restored"
        except Exception as restore_error:
            receipt["state"] = "rollback_failed_manual_recovery_required"
            receipt["restore_error"] = describe_error(restore_error)
            print(f"Traffic restoration failed: {receipt['restore_error']}", file=sys.stderr)
        if receipt["state"] == "traffic_restored":
            retire_candidate(adapter, receipt)
        try:
            # Best effort: the candidate's own last lines explain most startup failures.
            receipt["candidate_logs"] = adapter.revision_logs(revision)
        except Exception as log_error:
            receipt["log_warning"] = f"Candidate logs unavailable: {describe_error(log_error)}"
        try:
            save()
        except Exception:
            Path(receipt_path).write_text(json.dumps({**receipt, "journal_warning": "Recovery checkpoint update failed"}, indent=2))
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(f"Release failed: {receipt['state']}; inspect the receipt and Cloud Run") from None
    retire_candidate(adapter, receipt)
    if image_path:
        try:
            receipt["serving_tags"] = reconcile_serving_tags(adapter, image_path, adapter.run)
        except Exception as tag_error:
            receipt["tag_warning"] = f"Serving-tag update failed: {describe_error(tag_error)}"
    save()
    return receipt


def main():
    from .journal import GCSJournal
    from .target import environment_target, verify_target
    target = environment_target()
    recovery_only = os.environ.get("RECOVERY_ONLY") == "true"
    if not recovery_only:
        verify_target(os.environ["TARGET_JSON"], target)
        prefix = f"{target.region}-docker.pkg.dev/{target.project_id}/{target.registry}/{target.service}@sha256:"
        if not os.environ["IMAGE_DIGEST"].startswith(prefix):
            raise ValueError("Image does not belong to the reviewed artifact destination")
    adapter = CloudRun(target.project_id, target.region, target.service, target.probe_service_account)
    adapter.require_private_service()
    journal = GCSJournal(target.recovery_bucket, target.service, lambda: run(["gcloud", "auth", "print-access-token"]))
    recovered = recover(adapter, journal, target.model_dump())
    if recovery_only:
        Path("recovery-receipt.json").write_text(json.dumps(recovered, indent=2))
        return
    spec = parse_spec(Path(os.environ["SPEC_FILE"]).read_text())
    if spec_hash(spec) != os.environ["SPEC_HASH"] or spec.name != os.environ["SERVICE"]:
        raise ValueError("Spec differs from reviewed plan or configured service")
    def terminate(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    labels = {"lab-deploy-plan": os.environ["PLAN_ID"]}
    if re.fullmatch(r"[a-f0-9]{40}", os.environ.get("COMMIT_SHA", "")):
        labels["lab-deploy-commit"] = os.environ["COMMIT_SHA"]
    image_path = f"{target.region}-docker.pkg.dev/{target.project_id}/{target.registry}/{target.service}"
    rollout(adapter, os.environ["IMAGE_DIGEST"], spec, os.environ["PLAN_ID"], "release-receipt.json", journal, target.model_dump(), labels, image_path)


if __name__ == "__main__":
    main()

