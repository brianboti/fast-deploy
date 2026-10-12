"""Report evidence from CI steps; never infer individual checks from overall success."""
CHECKS = {
    "tests": ("build", "Run unit tests"),
    "dependency_scan": ("build", "Audit Python dependencies"),
    "health": ("deploy", "Deploy, probe, promote; restore traffic on failure"),
    "target": ("deploy", "Validate artifact and deployment configuration"),
}


def summarize(workflow, prior):
    state = workflow.get("status")
    if state == "not_visible_yet":
        return {"state": "awaiting_workflow", "checks": prior}
    terminal = state == "completed"
    checks = dict(prior)
    for name, (job_name, step_name) in CHECKS.items():
        job = next((j for j in workflow.get("jobs", []) if j.get("name") == job_name), None)
        step = next((s for s in (job or {}).get("steps", []) if s.get("name") == step_name), None)
        if step is None:
            checks[name] = "unconfirmed" if terminal else "pending"
        elif step.get("status") != "completed":
            checks[name] = "interrupted" if terminal else step.get("status", "pending")
        else:
            conclusion = step.get("conclusion")
            checks[name] = {"success": "passed", "failure": "failed", "skipped": "not_run", "cancelled": "cancelled"}.get(conclusion, "unconfirmed")
            if name == "health" and conclusion == "failure":
                checks[name] = "release_failed_health_unconfirmed"
    conclusion = workflow.get("conclusion")
    if terminal:
        result = {"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed_out"}.get(conclusion, "completed_unverified")
        if result == "succeeded" and any(checks.get(k) != "passed" for k in CHECKS):
            result = "completed_unverified"
    else:
        result = "running" if state == "in_progress" else "queued"
    return {"state": result, "checks": checks,
            "recovery": "inspect_recovery_workflow_and_journal" if result in {"failed", "cancelled", "timed_out"} else "not_requested"}
