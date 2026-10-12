"""Runs only on the deployment runner, never inside Claude or the MCP server."""
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from .models import enforce, parse_spec, spec_hash


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=600).stdout.strip()


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
        self.run(self.command("services", "update-traffic", self.service, "--to-revisions", ",".join(f"{k}={v}" for k, v in sorted(allocations.items()))))

    def remove_candidate(self):
        self.run(self.command("services", "update-traffic", self.service, "--remove-tags=candidate"))

    def deploy(self, image, spec, suffix):
        # Cloud Run gates traffic on this HTTP startup probe (up to 60 s of grace) before the platform's own probes run.
        probe = (f"httpGet.path={spec.health_path},httpGet.port={spec.port},"
                 "initialDelaySeconds=0,periodSeconds=5,timeoutSeconds=3,failureThreshold=12")
        self.run(self.command("deploy", self.service, "--image", image,
            "--revision-suffix", suffix, "--no-traffic", "--tag=candidate", "--startup-probe", probe,
            "--ingress=all", "--port", str(spec.port),
            "--cpu", str(spec.resources.cpu), "--memory", f"{spec.resources.memory_mib}Mi",
            "--min-instances", str(spec.scaling.minimum), "--max-instances", str(spec.scaling.maximum),
            "--concurrency", str(spec.concurrency), "--timeout=60", "--cpu-throttling"))

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
    try:
        adapter.remove_candidate()
    except Exception:
        saved["cleanup_warning"] = "Candidate tag cleanup failed"
        journal.write(saved)
    return saved


def rollout(adapter, image, spec, plan_id, receipt_path, journal=None, target=None):
    if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Invalid plan ID")
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("An immutable Artifact Registry image digest is required")
    enforce(spec)
    previous = adapter.describe()
    status = previous["status"]
    allocations = traffic_snapshot(status)
    receipt = {"plan_id": plan_id, "image": image, "previous_traffic": allocations, "state": "preparing", "target": target}
    def save():
        if journal is not None:
            journal.write(receipt)
        Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n")
    # Fail before any cloud mutation if the recovery checkpoint cannot be saved.
    save()
    try:
        # Freeze any LATEST allocation to concrete revisions before creating a new revision.
        adapter.traffic(allocations)
        suffix = "r" + plan_id[:16]
        receipt["state"] = "deploying"
        save()
        adapter.deploy(image, spec, suffix)
        candidate = adapter.describe()["status"]
        revision = f"{adapter.service}-{suffix}"
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
        try:
            adapter.traffic(allocations)
            receipt["state"] = "traffic_restored"
        except Exception:
            receipt["state"] = "rollback_failed_manual_recovery_required"
        try:
            save()
        except Exception:
            Path(receipt_path).write_text(json.dumps({**receipt, "journal_warning": "Recovery checkpoint update failed"}, indent=2))
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(f"Release failed: {receipt['state']}; inspect the receipt and Cloud Run") from None
    finally:
        try:
            adapter.remove_candidate()
        except Exception:
            receipt["cleanup_warning"] = "Candidate tag cleanup failed; remove it manually"
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
    rollout(adapter, os.environ["IMAGE_DIGEST"], spec, os.environ["PLAN_ID"], "release-receipt.json", journal, target.model_dump())


if __name__ == "__main__":
    main()

