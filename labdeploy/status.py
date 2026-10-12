"""Report evidence from CI steps; never infer individual checks from overall success."""
import datetime

CHECKS = {
    "tests": ("build", "Run unit tests"),
    "dependency_scan": ("build", "Audit Python dependencies"),
    "health": ("deploy", "Deploy, probe, promote; restore traffic on failure"),
    "target": ("deploy", "Validate artifact and deployment configuration"),
}
PASSED = {"passed", "passed_in_earlier_release", "skipped_by_config"}  # reused image: proved earlier; tests: none: declared, and warned about
RELEASE_STEP = CHECKS["health"][1]
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "completed_unverified", "dispatch_lost"}
# Plain next actions keyed by a substring of the failed step's name, in the order the steps run.
STEP_ADVICE = [
    ("Validate dispatch inputs", "The dispatched commit is no longer the tip of main, or the inputs were malformed; create a new plan."),
    ("Validate reviewed specification", "platform.yaml on main no longer matches the plan; create a new plan."),
    ("Run unit tests", "Unit tests failed on main; fix them and deploy again. The job log names the failing tests."),
    ("Audit Python dependencies", "The dependency audit found known vulnerabilities; upgrade the packages named in the job log, then deploy again."),
    ("Build without cloud credentials", "The container image did not build; check the Dockerfile and the job log, then deploy again."),
    ("Validate artifact and deployment configuration", "The staging environment variables differ from the reviewed target; compare them with the Terraform outputs."),
    ("google-github-actions/auth", "Cloud sign-in failed; check the staging environment's variables and the identity federation in Terraform."),
    ("Recover any interrupted previous release", "A previous release left an unresolved checkpoint; run the recovery workflow and inspect the checkpoint before releasing again."),
    ("Push image and resolve immutable digest", "Pushing the image failed; check the deployer's Artifact Registry permission and the registry name."),
    ("Attest build provenance", "Signing the image's provenance failed; check the workflow's attestations permission."),
    ("Verify build provenance", "Provenance verification failed closed; confirm the runner can read the registry through docker-credential-gcloud."),
    ("Summarize image vulnerabilities", "The vulnerability policy blocked the release; see vulnerability-summary.json, fix or accept the findings, then deploy again."),
]


def _seconds(step):
    try:
        start = datetime.datetime.fromisoformat(step["started_at"].replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(step["completed_at"].replace("Z", "+00:00"))
        return round((end - start).total_seconds(), 1)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def timeline(workflow):
    """Every step of every job in order, with its outcome and duration when GitHub reports them."""
    items = []
    for job in workflow.get("jobs", []):
        for step in job.get("steps", []):
            items.append({"job": job.get("name"), "step": step.get("name"), "status": step.get("status"),
                          "conclusion": step.get("conclusion"), "seconds": _seconds(step)})
    return items


def failed_step(items):
    return next(({"job": item["job"], "step": item["step"]} for item in items if item["conclusion"] in {"failure", "timed_out"}), None)


def summarize(workflow, prior, kind=None):
    state = workflow.get("status")
    if state == "not_visible_yet":
        return {"state": "awaiting_workflow", "checks": prior, "timeline": [], "failed_step": None}
    terminal = state == "completed"
    if kind == "rollback":
        conclusion = workflow.get("conclusion")
        items = timeline(workflow)
        result = ({"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "completed_unverified")
                  if terminal else ("running" if state == "in_progress" else "queued"))
        return {"state": result, "checks": {}, "timeline": items, "failed_step": failed_step(items), "recovery": "not_requested"}
    checks = dict(prior)
    for name, (job_name, step_name) in CHECKS.items():
        job = next((j for j in workflow.get("jobs", []) if j.get("name") == job_name), None)
        step = next((s for s in (job or {}).get("steps", []) if s.get("name") == step_name), None)
        if step is None:
            if prior.get(name) == "passed_in_earlier_release":
                continue
            checks[name] = "unconfirmed" if terminal else "pending"
        elif step.get("status") != "completed":
            checks[name] = "interrupted" if terminal else step.get("status", "pending")
        else:
            conclusion = step.get("conclusion")
            checks[name] = {"success": "passed", "failure": "failed", "skipped": "not_run", "cancelled": "cancelled"}.get(conclusion, "unconfirmed")
            if conclusion == "skipped" and prior.get(name) == "skipped_by_config":
                checks[name] = "skipped_by_config"
            if name == "health" and conclusion == "failure":
                checks[name] = "release_failed_health_unconfirmed"
    conclusion = workflow.get("conclusion")
    if terminal:
        result = {"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "completed_unverified")
        if result == "succeeded" and any(checks.get(k) not in PASSED for k in CHECKS):
            result = "completed_unverified"
    else:
        result = "running" if state == "in_progress" else "queued"
    items = timeline(workflow)
    return {"state": result, "checks": checks, "timeline": items, "failed_step": failed_step(items),
            "recovery": "inspect_recovery_workflow_and_journal" if result in {"failed", "cancelled", "timed_out"} else "not_requested"}


def release_phases(receipt):
    """The release step's own phases from its receipt: what happened inside the one workflow step Claude otherwise sees as a block."""
    if not receipt:
        return None
    phases = {"state": receipt.get("state"), "revision": receipt.get("revision"), "url": receipt.get("url"), "error": receipt.get("error"),
              "restore_error": receipt.get("restore_error"), "service_ready": receipt.get("service_ready"), "note": receipt.get("note"),
              "previous_traffic": receipt.get("previous_traffic"), "cleanup_warning": receipt.get("cleanup_warning"),
              "timestamps": receipt.get("phases"), "candidate_logs": receipt.get("candidate_logs"), "candidate_logs_note": receipt.get("candidate_logs_note"),
              "migration": receipt.get("migration")}
    return {key: value for key, value in phases.items() if value is not None}


def advice(plan):
    """One plain sentence on what to do next, derived from the state, the failed step, the receipt and any recovery run."""
    state = plan.get("state")
    receipt = plan.get("receipt") or {}
    if plan.get("kind") == "rollback":
        if state == "succeeded" and receipt.get("state") == "rolled_back":
            return f"Traffic is back on {receipt.get('restored_traffic')}; revision {receipt.get('rolled_back_from')} no longer serves. Deploy a fixed commit when ready."
        if state == "succeeded":
            return f"The rollback run finished but reports '{receipt.get('state', 'no receipt')}'; confirm the traffic split in Cloud Run."
        if state in {"failed", "cancelled", "timed_out"}:
            return "The rollback run did not complete; traffic was not changed. Open the run log, then retry or deploy the wanted commit."
        if state in {"dispatched", "queued", "running", "awaiting_workflow"}:
            return "The rollback is running; traffic moves back within about a minute. Ask again shortly."
    recovery = plan.get("recovery_run") or {}
    recovered = f" The recovery run that followed concluded '{recovery.get('conclusion')}'" + (f": {recovery['receipt'].get('state')}." if recovery.get("receipt") else ".") if recovery else ""
    if state == "planned":
        return "Call deploy_staging with this plan id within 15 minutes of planning, or create a new plan."
    if state in {"dispatching", "dispatch_unknown"}:
        return "The dispatch outcome is unknown; inspect GitHub Actions for a run titled with this plan id before dispatching again."
    if state == "dispatch_lost":
        return "GitHub never showed a run for this plan, so the dispatch was lost; nothing was deployed. Create a new plan and deploy again."
    if state in {"awaiting_workflow", "queued", "running"}:
        return "The release is in progress; ask again, or call get_deployment_status with wait_seconds to wait for a change."
    if state == "succeeded":
        if receipt.get("revision"):
            return f"Revision {receipt['revision']} serves all traffic at {receipt.get('url', 'the service URL')}. Nothing to do."
        return "The workflow succeeded; the release receipt was not available, so confirm the serving revision in Cloud Run."
    if state == "completed_unverified":
        return "The workflow finished but not every check reported a result; inspect the run before trusting this release."
    failed = plan.get("failed_step") or {}
    step = failed.get("step") or ""
    if step == RELEASE_STEP or (state in {"cancelled", "timed_out"} and receipt):
        if receipt.get("state") == "traffic_restored":
            return (f"The candidate failed ({receipt.get('error', 'see the receipt')}); traffic was restored and the previous revision kept serving. "
                    "Fix the application and deploy again." + (" The candidate's last log lines are in the receipt." if receipt.get("candidate_logs") else "") + recovered)
        if receipt.get("state") == "rollback_failed_manual_recovery_required":
            return (f"Traffic restoration failed ({receipt.get('restore_error', 'see the receipt')}). Run the recovery workflow now and inspect Cloud Run; "
                    f"the previous split was {receipt.get('previous_traffic')}.{recovered}")
        if receipt.get("state") in {"preparing", "migrating", "deploying", "promoting"}:
            note = " The migration job may have run; check its log before releasing again." if receipt.get("state") == "migrating" else ""
            return f"The runner stopped mid-release at '{receipt['state']}'; the recovery workflow restores traffic from the checkpoint.{note}{recovered}"
        if receipt.get("state") == "succeeded":
            return f"The release itself completed before the run ended; revision {receipt.get('revision')} serves traffic.{recovered}"
    for needle, text in STEP_ADVICE:
        if needle in step:
            return text
    if state == "cancelled":
        return f"The run was cancelled before the release step changed anything, or no receipt was uploaded.{recovered or ' Check the recovery workflow.'}"
    if state == "timed_out":
        return f"The run timed out.{recovered or ' Check the recovery workflow and Cloud Run.'}"
    if state == "failed":
        return f"The run failed at '{step or 'an unnamed step'}'; open the run log.{recovered}"
    return "Inspect the run in GitHub Actions."
