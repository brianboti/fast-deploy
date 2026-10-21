"""Report evidence from CI steps; never infer individual checks from overall success."""
import datetime
import re

from .bake import summary as bake_summary

# A check that a Python and a Node.js service prove in different steps names both; the one that ran is the evidence.
CHECKS = {
    "tests": ("build", ("Run unit tests", "Run unit tests (Node.js)")),
    "dependency_scan": ("build", ("Audit Python dependencies", "Audit Node.js dependencies")),
    "health": ("deploy", "Deploy, probe, promote; restore traffic on failure"),
    "target": ("deploy", "Validate artifact and deployment configuration"),
}
# A promotion (docs/ROADMAP.md D37) proves itself in the promote job of the platform repository's promote.yml.
PROMOTION_CHECKS = {
    "approval": ("promote", "Refuse to run without a reviewer's approval"),
    "provenance": ("promote", "Verify the staging image's provenance"),
    "copy": ("promote", "Copy the image into production by digest"),
    "health": ("promote", "Deploy, probe, promote; restore traffic on failure"),
}
PASSED = {"passed", "passed_in_earlier_release", "skipped_by_config", "reported"}  # reused image: proved earlier; tests: none: declared, and warned about
RELEASE_STEP = CHECKS["health"][1]
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "completed_unverified", "dispatch_lost"}
# Plain next actions keyed by a substring of the failed step's name, in the order the steps run.
STEP_ADVICE = [
    ("Validate dispatch inputs", "The dispatched commit is no longer the tip of main, or the inputs were malformed; create a new plan."),
    ("Validate reviewed specification", "platform.yaml on main no longer matches the plan; create a new plan."),
    ("Run unit tests", "Unit tests failed on main; fix them and deploy again. The job log names the failing tests."),
    ("Audit Python dependencies", "The dependency audit found known vulnerabilities; upgrade the packages named in the job log, then deploy again."),
    ("Audit Node.js dependencies", "The dependency audit stopped the release: a known malicious package, a critical or high finding with a fix, a "
     "package from outside the npm registry, or an audit that did not finish (dependency-audit.json in the bundle says which); update the "
     "packages it names, commit the lockfile, then deploy again."),
    ("Install Node.js dependencies", "npm ci failed: package.json and the lockfile disagree, or a package could not be fetched; run npm install, "
     "commit both files, then deploy again."),
    ("Lint the Dockerfile", "The Dockerfile lint found error-level problems, which stop a release; fix the lines the run annotates "
     "(or download hadolint's findings from the run), then deploy again. The operator can turn blocking off for one repository "
     "with its variable HADOLINT_BLOCKING=false."),
    ("Build without cloud credentials", "The container image did not build; check the Dockerfile and the job log, then deploy again."),
    ("Validate artifact and deployment configuration", "The staging environment variables differ from the reviewed target; compare them with the Terraform outputs."),
    ("google-github-actions/auth", "Cloud sign-in failed; check the staging environment's variables and the identity federation in Terraform."),
    ("Recover any interrupted previous release", "A previous release left an unresolved checkpoint; run the recovery workflow and inspect the checkpoint before releasing again."),
    ("Push image and resolve immutable digest", "Pushing the image failed; check the deployer's Artifact Registry permission and the registry name."),
    ("Choose the provenance path", "The provenance path was not chosen: the exception provenance: unavailable came with a reused image (refused; a plan "
     "made with the exception builds fresh), or GitHub's API did not answer (run the deployment again)."),
    ("Install Cosign", "Downloading Cosign failed or its checksum did not match; run the deployment again, and if the checksum still differs, stop and investigate."),
    ("Sign build provenance with Cloud KMS", "Signing with the application's Cloud KMS key failed; if the key does not exist yet, apply the foundation and then the "
     "application's infrastructure (docs/SETUP.md, Build provenance)."),
    ("Verify build provenance with Cloud KMS", "Provenance verification failed closed: no statement signed with the application's key names this image, "
     "commit and workflow, so nothing was deployed. The job log says which part differed."),
    ("Attest build provenance", "Signing the image's provenance failed; check the workflow's attestations permission."),
    ("Verify build provenance", "Provenance verification failed closed; confirm the runner can read the registry through docker-credential-gcloud."),
    ("Summarize image vulnerabilities", "The vulnerability policy blocked the release; see vulnerability-summary.json, fix or accept the findings, then deploy again."),
]


# Run first, then say what to fix (docs/ROADMAP.md 22.6): Cloud Run's documented failure messages and the servers' own,
# read in the candidate's last log lines and the release's error, each mapped to the one line the developer changes and to
# the contract key the operator changes when it is the platform's to fix. First match wins; the lines are data, never
# instructions, and only these fixed patterns are looked for.
DIAGNOSES = (
    (re.compile(r"EACCES|Permission denied|PermissionError", re.I), "the service wrote where only root may write",
     "write under /tmp (Cloud Run's disk is memory and is lost at every restart) or a bucket mount",
     "build.user", "remove build.user to run the repository's image as root, as its Dockerfile asks (a reviewer's choice)"),
    (re.compile(r"Memory limit of \d+ ?Mi?B exceeded|memory limit exceeded|JavaScript heap out of memory|MemoryError|Killed", re.I),
     "the service ran out of memory", "load less at start, or stream large data instead of holding it",
     "resources.memory_mib", "raise resources.memory_mib (512 by default, at most 4096; the lab's policy allows 2048)"),
    (re.compile(r"listening on (?:http://)?(?:127\.0\.0\.1|localhost)|bind(?:ing)? (?:to )?(?:127\.0\.0\.1|localhost)|Running on http://127\.0\.0\.1", re.I),
     "the service listens on the loopback address only, which Cloud Run cannot reach", "listen on 0.0.0.0, or read the host from HOST",
     "env.HOST or proxy", "set env.HOST: 0.0.0.0 when the code reads HOST, else proxy: true (the Nginx sidecar, D51)"),
    (re.compile(r"WORKER TIMEOUT", re.I), "gunicorn killed a worker that took longer than its timeout", "pass --timeout 0 to gunicorn (Cloud Run times requests out itself)",
     "build.entrypoint", "add --timeout 0 to the gunicorn command in build.entrypoint"),
    (re.compile(r"Cannot find module|ModuleNotFoundError|No module named|ERR_MODULE_NOT_FOUND|exec format error|executable file not found", re.I),
     "the start command names a file or module the image does not hold", "start the file the image builds",
     "build.entrypoint", "correct build.entrypoint (or command and args for a repository's own image) to the file the build makes"),
    (re.compile(r"failed to start and (?:then )?listen on the port|did not listen on the port|container failed to start", re.I),
     "the service did not listen on the port Cloud Run probed in time", "listen on the port Cloud Run sets in PORT, on 0.0.0.0",
     "port", "set port to the port the code listens on (Cloud Run's --port sets PORT alike), or proxy: true if it binds the loopback"),
    (re.compile(r"(?:Startup|Liveness) (?:HTTP )?probe failed|probe failed", re.I), "Cloud Run's startup probe did not get an answer in time",
     "answer the health path with 200 quickly, without calling other services", "health_path",
     "set health_path: null to use the TCP probe, or to a path that answers at once"),
    # A service that passed its release and fails later (docs/ROADMAP.md 23.3): Google's documented messages for it.
    (re.compile(r"no available instance", re.I), "every instance was busy and Cloud Run could not start another one (the instance limit)",
     "answer faster, or serve several requests at once in one instance", "scaling.maximum",
     "raise scaling.maximum (the cost bound rises with it) or concurrency"),
    (re.compile(r"maximum request timeout|upstream request timeout", re.I), "a request took longer than the service's timeout",
     "answer within the timeout, or move long work to a job and answer at once", "timeout_seconds",
     "raise timeout_seconds (60 by default, up to 3600 for a service that streams)"),
)


def diagnose(receipt):
    """{"cause", "developer", "contract_key", "operator", "evidence"} for a failed candidate, from its last log lines and the
    release's error, or None when nothing known matches (docs/ROADMAP.md 22.6)."""
    lines = [str(item.get("message") or "") for item in (receipt or {}).get("candidate_logs") or [] if isinstance(item, dict)]
    lines += [str((receipt or {}).get("error") or "")]
    for pattern, cause, developer, key, operator in DIAGNOSES:
        for line in reversed(lines):
            match = pattern.search(line)
            if match:
                return {"cause": cause, "developer": developer, "contract_key": key, "operator": operator, "evidence": line.strip()[:200]}
    return None


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
    if kind in ("rollback", "production_rollback"):
        conclusion = workflow.get("conclusion")
        items = timeline(workflow)
        waiting = "awaiting_approval" if state == "waiting" else ("running" if state == "in_progress" else "queued")
        result = ({"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "completed_unverified")
                  if terminal else waiting)
        return {"state": result, "checks": {}, "timeline": items, "failed_step": failed_step(items), "recovery": "not_requested"}
    table = PROMOTION_CHECKS if kind == "promotion" else CHECKS
    checks = dict(prior)
    for name, (job_name, step_names) in table.items():
        job = next((j for j in workflow.get("jobs", []) if j.get("name") == job_name), None)
        names = (step_names,) if isinstance(step_names, str) else step_names
        found = [s for s in (job or {}).get("steps", []) if s.get("name") in names]
        step = next((s for s in found if s.get("conclusion") != "skipped"), found[0] if found else None)
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
            if conclusion == "success" and prior.get(name) == "reported_by_config":
                checks[name] = "reported"  # tests: report (D54): the suite ran; a failure would have been reported, not a stop
            if name == "health" and conclusion == "failure":
                checks[name] = "release_failed_health_unconfirmed"
    conclusion = workflow.get("conclusion")
    if terminal:
        result = {"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "completed_unverified")
        if result == "succeeded" and any(checks.get(k) not in PASSED for k in table):
            result = "completed_unverified"
    elif state == "waiting":
        result = "awaiting_approval"  # held by an environment's required reviewers: nothing runs until one approves
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
              "migration": receipt.get("migration"), "bake": receipt.get("bake"), "bake_summary": bake_summary(receipt.get("bake")),
              "dockerfile_lint": receipt.get("dockerfile_lint"), "failed_phase": receipt.get("failed_phase"),
              "diagnosis": diagnose(receipt) if receipt.get("failed_phase") in ("deploying", "promoting", "baking") else None}
    return {key: value for key, value in phases.items() if value is not None}


def bake_note(receipt):
    """What the bake could not judge, said plainly after a successful release; empty when it passed or did not run."""
    bake = receipt.get("bake") or {}
    if bake.get("result") == "inconclusive":
        return " The bake saw too little traffic to judge errors or latency, so only the probes vouch for this release."
    if bake.get("result") == "unobserved":
        return " The bake could not read the new revision's logs, so nothing watched it after its final probe; check the deployer's Logging viewer role."
    if bake.get("result") == "skipped":
        return " No bake ran (bake_seconds: 0 for this application)."
    return ""


PROMOTION_STEP_ADVICE = [
    ("Check the dispatch inputs", "The promotion's inputs were refused before anything ran (the run's log says which); nothing changed in production."),
    ("The commits are on main", "The commit, or the contract it came through, is no longer on main, so it is not promoted; nothing changed in production."),
    ("Read the reviewed specification", "The reviewed specification no longer matches the staging release, or production's policy refuses it; nothing "
     "changed in production."),
    ("Refuse to run without a reviewer's approval", "The promote job started without a recorded approval, so it stopped before touching production: "
     "ask for add_production, which puts the environment's required reviewers back, then promote again."),
    ("Validate the production target", "The environment's variables differ from the registered production target; ask for add_production, which sets "
     "them again, then promote again. Nothing changed in production."),
    ("google-github-actions/auth", "Signing in to production failed: the production root may not be applied yet, or the environment's variables are "
     "wrong (add_production repairs them). Nothing changed in production."),
    ("Recover any interrupted previous release", "An earlier promotion left a checkpoint that could not be restored automatically; inspect production's "
     "traffic in Cloud Run (docs/RUNBOOK.md, Production) before promoting again."),
    ("Install Cosign", "Downloading Cosign failed or its checksum did not match; promote again, and if the checksum still differs, stop and investigate."),
    ("Verify the staging image's provenance", "The staging image's provenance could not be verified, so nothing was copied or deployed; the run's log says "
     "which part differed."),
    ("Copy the image into production by digest", "Copying the image into production failed: the production deployer may not read staging's images yet "
     "(add_production's access step), or the copy came out with another digest. Nothing was deployed."),
    ("Sign the promotion with production's key", "Signing the promotion with production's key failed: the application's production root must be "
     "applied, since it creates the key. Nothing was deployed."),
]


def promotion_advice(plan):
    """The next step for a promotion, where it differs from a staging release's; None to use the release's words."""
    state, receipt = plan.get("state"), plan.get("receipt") or {}
    reviewers = " or ".join(plan.get("reviewers") or []) or "a production reviewer"
    run = (plan.get("workflow") or {}).get("html_url")
    where = f" ({run})" if run else ""
    if state == "awaiting_approval":
        return (f"Waiting for {reviewers} to approve the promotion on GitHub{where}: open the run, choose Review deployments, read the approval "
                "block and approve or reject it. Nothing changes in production until then.")
    if state in {"dispatched", "awaiting_workflow", "queued", "running"}:
        return "The promotion is under way; it waits for a reviewer's approval before anything reaches production. Ask again shortly."
    if state == "succeeded" and receipt.get("revision"):
        approved = ", ".join((receipt.get("promotion") or {}).get("approved_by") or []) or "a reviewer"
        return (f"Revision {receipt['revision']} serves all production traffic at {receipt.get('url', 'the service URL')}: the exact image staging release "
                f"{((plan.get('source') or {}).get('plan') or '')[:8]} deployed, approved by {approved}. Nothing to do.{bake_note(receipt)}")
    step = (plan.get("failed_step") or {}).get("step") or ""
    for needle, text in PROMOTION_STEP_ADVICE:
        if needle in step:
            return text
    if state in {"failed", "cancelled", "timed_out"} and not step and not receipt:
        return ("Nothing ran in production: a reviewer rejected the promotion, it was cancelled while it waited, or it waited too long for approval. "
                "Promote again when ready.")
    if receipt.get("state") in {"preparing", "migrating", "deploying", "promoting", "baking"} and state in {"failed", "cancelled", "timed_out"}:
        return (f"The runner stopped mid-release at '{receipt['state']}'. The next promotion restores production's traffic from the checkpoint before "
                "anything else; to restore it now, follow docs/RUNBOOK.md, Production.")
    step_share = receipt.get("step")
    if receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") in ("promoting", "baking") and step_share and step_share < 100:
        why = "it was cancelled" if receipt.get("cancelled") else receipt.get("error", "see the receipt")
        return (f"The new revision stopped with {step_share} percent of production's traffic ({why}); the earlier split serves everyone again. "
                "Fix the application, release it to staging, then promote again.")
    return None


def production_rollback_advice(plan):
    """The next step for a production rollback (promote.yml's rollback)."""
    state, receipt = plan.get("state"), plan.get("receipt") or {}
    reviewers = " or ".join(plan.get("reviewers") or []) or "a production reviewer"
    run = (plan.get("workflow") or {}).get("html_url")
    where = f" ({run})" if run else ""
    if state == "awaiting_approval":
        return (f"Waiting for {reviewers} to approve the rollback on GitHub{where}: Review deployments, then approve. Production's traffic stays "
                "where it is until then.")
    if state in {"dispatched", "dispatching", "awaiting_workflow", "queued", "running"}:
        return "The production rollback is under way; it waits for a reviewer's approval, then moves the traffic within a minute. Ask again shortly."
    if state == "succeeded" and receipt.get("state") == "rolled_back":
        return (f"Production's traffic is back on {receipt.get('restored_traffic')}; revision {receipt.get('rolled_back_from')} no longer serves. "
                "Promote a fixed staging release when ready.")
    if state == "succeeded":
        return f"The rollback run finished but reports '{receipt.get('state', 'no receipt')}'; confirm production's traffic split in Cloud Run."
    if state in {"failed", "cancelled", "timed_out"}:
        return ("The rollback did not run or did not finish, so production's traffic was not changed (a reviewer may have rejected it, or traffic "
                "moved since the promotion and the runner refused). Open the run, then try again or promote the wanted release.")
    return None


def advice(plan):
    """One plain sentence on what to do next, derived from the state, the failed step, the receipt and any recovery run."""
    state = plan.get("state")
    receipt = plan.get("receipt") or {}
    if plan.get("kind") == "promotion":
        text = promotion_advice(plan)
        if text:
            return text
    if plan.get("kind") == "production_rollback":
        return production_rollback_advice(plan) or "Inspect the rollback run in the platform repository's Actions."
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
    if not recovery:
        recovered = ""
    elif recovery.get("conclusion") is None:  # it starts after every failed release; asked too early, it has no result yet
        link = f": {recovery['html_url']}" if recovery.get("html_url") else ""
        recovered = f" The recovery check that started after it is still {recovery.get('status') or 'running'}{link}."
    else:
        recovered = f" The recovery run that followed concluded '{recovery.get('conclusion')}'" + (f": {recovery['receipt'].get('state')}." if recovery.get("receipt") else ".")
    if state == "planned":
        return "Call deploy_staging with this plan id within 15 minutes of planning, or create a new plan."
    if state in {"dispatching", "dispatch_unknown"}:
        return "The dispatch outcome is unknown; inspect GitHub Actions for a run titled with this plan id before dispatching again."
    if state == "dispatch_lost":
        return "GitHub never showed a run for this plan, so the dispatch was lost; nothing was deployed. Create a new plan and deploy again."
    if state in {"awaiting_workflow", "queued", "running"}:
        return "The release is in progress; ask again, or call get_deployment_status with wait_seconds to wait for a change."
    if state == "succeeded":
        if receipt.get("revision") and plan.get("run_ended"):
            return (f"Revision {receipt['revision']} serves all traffic at {receipt.get('url', 'the service URL')}: the release completed before "
                    f"the run ended ({plan['run_ended']}), so this is the live release. Roll back if it should not serve.{bake_note(receipt)}")
        if receipt.get("revision"):
            return f"Revision {receipt['revision']} serves all traffic at {receipt.get('url', 'the service URL')}. Nothing to do.{bake_note(receipt)}"
        return "The workflow succeeded; the release receipt was not available, so confirm the serving revision in Cloud Run."
    if state == "completed_unverified":
        return ("The workflow finished but not every check reported a result. GitHub can be late: the controller reads the run again "
                "for six hours and records it once every check is in. Until then, inspect the run before trusting this release.")
    failed = plan.get("failed_step") or {}
    step = failed.get("step") or ""
    if step == RELEASE_STEP or (state in {"cancelled", "timed_out"} and receipt):
        if receipt.get("state") == "traffic_restored" and receipt.get("cancelled"):
            phase = receipt.get("failed_phase") or "the release"
            job = (" The migration job runs on in Google Cloud once started and may still finish; check its execution before "
                   "releasing again." if phase == "migrating" else "")
            if phase == "baking":
                return ("The release was cancelled while baking: the new revision had served all traffic since its promotion, and the previous "
                        "traffic split is restored now. Deploy again when ready." + recovered)
            return (f"The release was cancelled while {phase}; nothing new serves and the previous revision kept serving.{job} "
                    "Deploy again when ready." + recovered)
        if receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") == "preparing":
            return (f"The release stopped before deploying anything ({receipt.get('error', 'see the receipt')}); the previous revision kept "
                    "serving and nothing changed. Fix that and deploy again." + recovered)
        if receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") == "migrating":
            return (f"The database migration failed, so nothing new was deployed and the previous revision kept serving ({receipt.get('error', 'see the receipt')}). "
                    "Fix the migration and deploy again; check its log lines in the receipt first, since a tool that does not run the "
                    "migration in one transaction may have applied part of it." + recovered)
        if receipt.get("state") == "traffic_restored" and receipt.get("failed_phase") == "baking":
            return (f"The new revision served all traffic but failed its bake after promotion ({receipt.get('error', 'see the receipt')}); the previous "
                    "traffic split was restored, so the previous revision serves again. The new revision's last log lines and the bake's numbers are in "
                    "the receipt; fix the application and deploy again." + recovered)
        if receipt.get("state") == "traffic_restored":
            found = diagnose(receipt)
            if found:
                return (f"The candidate failed ({receipt.get('error', 'see the receipt')}): {found['cause']} (its log: \"{found['evidence'][:120]}\"). Traffic was restored and the previous revision "
                        f"kept serving. In the code: {found['developer']}. In the contract: {found['operator']} ({found['contract_key']}); then deploy "
                        "again. The candidate's last log lines are in the receipt." + recovered)
            return (f"The candidate failed ({receipt.get('error', 'see the receipt')}); traffic was restored and the previous revision kept serving. "
                    "Fix the application and deploy again." + (" The candidate's last log lines are in the receipt." if receipt.get("candidate_logs") else "") + recovered)
        if receipt.get("state") == "rollback_failed_manual_recovery_required":
            return (f"Traffic restoration failed ({receipt.get('restore_error', 'see the receipt')}). Run the recovery workflow now and inspect Cloud Run; "
                    f"the previous split was {receipt.get('previous_traffic')}.{recovered}")
        if receipt.get("state") in {"preparing", "migrating", "deploying", "promoting", "baking"}:
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
