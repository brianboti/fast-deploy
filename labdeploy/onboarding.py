"""One sentence: from a repository that follows docs/DEVELOPER-CONTRACT.md to a running service, as a resumable state machine.

Every step looks at what already exists and continues from there, so a second call finishes an interrupted first one.
Steps that wait on GitHub or the pipeline (a plan, an apply, a build) end the call; the scheduler's tick or the next call
carries on. The controller merges only pull requests it opened itself, only on the application's own files, only when
the policy and the checks have passed, and only when the configuration allows it (`onboarding.auto_merge`).
"""
import re
import time

import yaml

from .config import Application, Principal
from .models import AppSpec

STEPS = ("validating", "registering", "infrastructure", "planning", "applying", "environment", "workflows", "merging_workflows", "releasing", "complete")
WAITING = {"planning", "applying", "merging_workflows"}
ACTIVE = set(STEPS) - {"complete"}
REQUIRED_FILES = (("Dockerfile", "the build recipe"), ("platform.yaml", "the application's contract"), ("requirements.txt", "pinned runtime dependencies"))
TEST_PATH = re.compile(r"(^|/)tests?(/|$)|(^|/)test_[^/]+\.py$|_test\.py$")
WHAT_NEXT = {
    "validating": "checking the repository against the contract",
    "registering": "registering the application with the controller",
    "infrastructure": "rendering the Terraform root and opening its pull request on the platform repository",
    "planning": "waiting for the infrastructure plan and its policy verdict, then merging",
    "applying": "waiting for the pipeline to apply the infrastructure",
    "environment": "creating the GitHub environment and its variables",
    "workflows": "opening the pull request that adds the three platform workflows",
    "merging_workflows": "waiting for that pull request to be mergeable, then merging",
    "releasing": "planning and dispatching the first release",
}


class Blocked(Exception):
    """The onboarding cannot continue until a person changes something; the message says what."""


def normalize_repository(text):
    """owner/name from a GitHub link or an owner/name string."""
    value = text.strip()
    match = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return value
    raise ValueError("Give a GitHub repository as https://github.com/OWNER/NAME or OWNER/NAME")


def slug(name):
    """An application id from a repository name: lowercase letters, digits and dashes."""
    value = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30]
    if not value or not value[0].isalpha():
        raise ValueError("Give an application id (lowercase letters, digits and dashes, starting with a letter)")
    return value


def describe(record):
    """The record as the operator sees it: the step, what it waits for or what blocked it, and what exists so far."""
    state = record["state"]
    view = {k: record.get(k) for k in ("application", "repository", "service", "state", "plan_id", "auto_deploy", "created_at", "updated_at")}
    view["steps"] = record.get("steps", {})
    if state == "complete":
        view["next_action"] = (f"Onboarding is complete; the first release is plan {record.get('plan_id')}. Ask for get_deployment_status with that id.")
    elif state == "blocked":
        view["blocked_at"] = record.get("blocked_at")
        view["error"] = record.get("error")
        view["next_action"] = "Fix what the error names, then call onboard_application again with the same repository; it continues from this step."
    else:
        view["next_action"] = f"In progress: {WHAT_NEXT[state]}. The controller continues on its own every minute; ask for get_onboarding_status to follow it."
    return view


class Onboarder:
    def __init__(self, controller):
        self.c = controller

    @property
    def settings(self):
        settings = self.c.config.onboarding
        if settings is None:
            raise ValueError("Set the `onboarding` block (project_id, project_number, region) in the controller configuration before onboarding repositories")
        return settings

    def principal(self, record):
        return Principal(name=f"onboarding:{record['principal']}", token_sha256="0" * 64, applications=["*"], can_deploy=True)

    # ---- entry points
    def start(self, principal, repository, app_id=None, auto_deploy=False):
        if "*" not in principal.applications:
            raise ValueError("Onboarding needs an identity that may see every application: applications: ['*'] in the controller configuration")
        if not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        _ = self.settings  # fail early when the block is missing
        repository = normalize_repository(repository)
        app_id = slug(app_id or repository.split("/")[1])
        record = self.c.store.onboarding(app_id)
        if record is None:
            record = {"application": app_id, "repository": repository, "principal": principal.name, "auto_deploy": bool(auto_deploy),
                      "state": "validating", "steps": {}, "created_at": time.time()}
            self.c.store.record_event("onboarding", name=app_id, identity=principal.name, application=app_id, outcome="started", detail=repository)
        elif record["repository"] != repository:
            raise ValueError(f"Application {app_id} is being onboarded from {record['repository']}; give another application id for {repository}")
        elif record["state"] == "complete":
            return describe(record)
        elif record["state"] == "blocked":
            record["state"], record["error"] = record.pop("blocked_at", "validating"), None
        return self.advance(record)

    def advance(self, record):
        """Run every step that can finish now; stop at the first that waits, blocks or completes."""
        for _ in range(len(STEPS)):
            state = record["state"]
            if state not in ACTIVE:
                break
            try:
                getattr(self, "step_" + state)(record)
            except Blocked as exc:
                record["blocked_at"], record["state"], record["error"] = state, "blocked", str(exc)[:600]
            except Exception as exc:  # noqa: BLE001 - a GitHub or cloud error is reported the same way, and the next call retries the step
                record["blocked_at"], record["state"], record["error"] = state, "blocked", f"{type(exc).__name__}: {str(exc)[:500]}"
            self.c.store.save_onboarding(record["application"], record)
            if record["state"] != state:
                self.c.store.record_event("onboarding", name=record["application"], application=record["application"], outcome=record["state"],
                                          detail=record.get("error") if record["state"] == "blocked" else state)
            if record["state"] == state or record["state"] in ("blocked", "complete"):
                break
        return describe(record)

    # ---- the steps
    def step_validating(self, record):
        github = self.c.github
        repo = github.repository(record["repository"])
        branch = repo["default_branch"] or "main"
        checklist = []
        if branch != "main":
            raise Blocked(f"The default branch is {branch}; the platform releases only from main")
        files = {}
        for name, why in REQUIRED_FILES:
            files[name] = github.contents(record["repository"], branch, name)
            checklist.append({"item": name, "ok": files[name] is not None, "why": why})
        tree = github.tree(record["repository"], branch)
        spec = None
        if files["platform.yaml"] is not None:
            try:
                spec = AppSpec.model_validate(yaml.safe_load(files["platform.yaml"]["text"]))
                checklist.append({"item": "platform.yaml is valid", "ok": True})
            except Exception as exc:  # noqa: BLE001 - any parse or validation problem is a checklist line
                checklist.append({"item": "platform.yaml is valid", "ok": False, "detail": str(exc)[:300]})
        if spec is not None:
            checklist.append({"item": "platform.yaml says reviewed: true", "ok": spec.reviewed, "why": "a person has read it"})
            if spec.tests == "required":
                checklist.append({"item": "tests (pytest files or a tests/ directory)", "ok": any(TEST_PATH.search(path) for path in tree), "why": "run before every build"})
                checklist.append({"item": "requirements-dev.txt", "ok": "requirements-dev.txt" in tree, "why": "installs pytest for the test step"})
            dockerfile = (files["Dockerfile"] or {}).get("text", "")
            if dockerfile:
                checklist.append({"item": f"Dockerfile exposes port {spec.port}", "ok": re.search(rf"^\s*EXPOSE\s+{spec.port}(/tcp)?\s*$", dockerfile, re.M) is not None,
                                  "why": "the port in platform.yaml is the one Cloud Run sends requests to"})
        record["steps"]["validating"] = {"checklist": checklist, "repository_id": repo["repository_id"], "owner_id": repo["owner_id"]}
        missing = [item for item in checklist if not item["ok"]]
        if missing:
            raise Blocked("The repository does not follow the contract yet: " + "; ".join(item["item"] + (f" ({item['detail']})" if item.get("detail") else "") for item in missing)
                          + ". See docs/DEVELOPER-CONTRACT.md.")
        record["service"] = spec.name
        record["needs"] = spec.needs.model_dump()
        record["state"] = "registering"

    def step_registering(self, record):
        app_id, service = record["application"], record["service"]
        if app_id in self.c.config.applications:
            app = self.c.config.applications[app_id]
            if app.repository != record["repository"] or app.service != service:
                raise Blocked(f"Application {app_id} is already registered for {app.repository} as service {app.service}; give another application id")
            record["steps"]["registering"] = {"note": "already registered"}
        else:
            settings = self.settings
            project, region = settings.project_id, settings.region
            entry = {"repository": record["repository"], "branch": "main", "service": service, "auto_deploy": record["auto_deploy"],
                     "target": {"project_id": project, "region": region, "service": service, "registry": service,
                                "deploy_service_account": f"{service}-deploy@{project}.iam.gserviceaccount.com",
                                "probe_service_account": f"{service}-probe@{project}.iam.gserviceaccount.com",
                                "wif_provider": f"projects/{settings.project_number}/locations/global/workloadIdentityPools/{service}-gh/providers/github",
                                "recovery_bucket": f"{project}-{service}-recovery"}}
            Application.model_validate(entry)
            self.c.store.register_application(app_id, entry)
            self.c.refresh_registry()
            record["steps"]["registering"] = {"target": entry["target"]}
        record["state"] = "infrastructure"

    def step_infrastructure(self, record):
        platform = self.c.config.platform
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before onboarding repositories")
        settings, app_id, service = self.settings, record["application"], record["service"]
        render = {"state_bucket": settings.state_bucket or f"{settings.project_id}-tfstate", "bootstrap_image": settings.bootstrap_image}
        rendered = self.c.render_infrastructure(self.principal(record), app_id, **render)
        existing = self.c.github.contents(platform.repository, platform.branch, f"infra/gcp/apps/{service}/main.tf")
        if existing is not None and existing["text"] == rendered["files"]["main.tf"]:
            record["steps"]["infrastructure"] = {"note": "the root is already on main"}
            record["state"] = "environment"
            return
        result = self.c.propose_infrastructure(self.principal(record), app_id, allow_writes=True, **render)
        pull_request = result["pull_request"]
        if pull_request.get("number") is None:
            raise Blocked(f"The infrastructure pull request could not be opened: {pull_request.get('note')}")
        record["steps"]["infrastructure"] = {"pull_request": pull_request["number"], "url": pull_request["url"]}
        record["state"] = "planning"

    def step_planning(self, record):
        platform, service = self.c.config.platform, record["service"]
        number = record["steps"]["infrastructure"]["pull_request"]
        pr = self.c.github.pull_request(platform.repository, number)
        step = record["steps"].setdefault("planning", {})
        if pr["merged"]:
            step.update(merge_sha=pr["merge_commit_sha"], waiting=None)
            record["state"] = "applying"
            return
        runs = self.c.github.workflow_runs(platform.repository, "infra-plan.yml", head_sha=pr["head_sha"])
        run = runs[0] if runs else None
        if run is None or run["status"] != "completed":
            step.update(waiting="the infrastructure plan run", run=run and run["url"])
            return
        comments = [body for body in self.c.github.issue_comments(platform.repository, number) if f"<!-- lab-deploy-infra:{service} -->" in body]
        verdict = comments[-1] if comments else ""
        if run["conclusion"] != "success" or "**Policy: passed.**" not in verdict:
            problems = [line for line in verdict.splitlines() if line.startswith("- ")]
            raise Blocked("The infrastructure plan did not pass: " + ("; ".join(problems) if problems else f"run {run['url']} concluded {run['conclusion']}")
                          + f". Pull request {pr['url']}.")
        if not self.settings.auto_merge:
            step.update(waiting=f"a person to merge {pr['url']}")
            return
        files = self.c.github.pull_request_files(platform.repository, number)
        outside = [path for path in files if not path.startswith(f"infra/gcp/apps/{service}/")]
        if outside:
            raise Blocked(f"The pull request touches files outside the application's root ({', '.join(outside[:5])}); review and merge it by hand: {pr['url']}")
        merged = self.c.github.merge_pull_request(platform.repository, number, f"Infrastructure for {service}")
        if not merged["merged"]:
            step.update(waiting=f"merge: {merged.get('reason') or 'not mergeable yet'}")
            return
        step.update(merge_sha=merged["sha"], waiting=None)
        record["state"] = "applying"

    def step_applying(self, record):
        platform = self.c.config.platform
        merge_sha = record["steps"]["planning"]["merge_sha"]
        step = record["steps"].setdefault("applying", {})
        runs = self.c.github.workflow_runs(platform.repository, "infra-apply.yml", head_sha=merge_sha)
        run = runs[0] if runs else None
        if run is None or run["status"] != "completed":
            step.update(waiting="the infrastructure apply run", run=run and run["url"])
            return
        if run["conclusion"] != "success":
            raise Blocked(f"The infrastructure apply failed: {run['url']}. Fix the cause and dispatch the apply workflow for this application, then call again.")
        step.update(run=run["url"], waiting=None)
        record["state"] = "environment"

    def step_environment(self, record):
        settings = self.settings
        app = self.c.config.applications[record["application"]]
        target = app.target
        variables = {"GCP_PROJECT_ID": target.project_id, "GCP_REGION": target.region, "CLOUD_RUN_SERVICE": target.service, "ARTIFACT_REPOSITORY": target.registry,
                     "WIF_PROVIDER": target.wif_provider, "DEPLOY_SERVICE_ACCOUNT": target.deploy_service_account, "PROBE_SERVICE_ACCOUNT": target.probe_service_account,
                     "RECOVERY_BUCKET": target.recovery_bucket}
        if settings.billing_export_table:
            variables["BILLING_EXPORT_TABLE"] = settings.billing_export_table
        self.c.github.ensure_environment(app.repository, settings.environment, app.branch)
        self.c.github.set_environment_variables(app.repository, settings.environment, variables)
        record["steps"]["environment"] = {"environment": settings.environment, "variables": sorted(variables)}
        record["state"] = "workflows"

    def step_workflows(self, record):
        platform = self.c.config.platform
        sha = platform.commit or self.c.github.branch_head(platform.repository, platform.branch)
        result = self.c.update_workflows(self.principal(record), record["application"], sha, allow_writes=True)
        if result["pull_request"] is None:
            record["steps"]["workflows"] = {"note": "already pinned", "platform_commit": sha}
            record["state"] = "releasing"
            return
        pull_request = result["pull_request"]
        if pull_request.get("number") is None:
            raise Blocked(f"The workflows pull request could not be opened: {pull_request.get('note')}")
        record["steps"]["workflows"] = {"pull_request": pull_request["number"], "url": pull_request["url"], "platform_commit": sha}
        record["state"] = "merging_workflows"

    def step_merging_workflows(self, record):
        app = self.c.config.applications[record["application"]]
        number = record["steps"]["workflows"]["pull_request"]
        pr = self.c.github.pull_request(app.repository, number)
        step = record["steps"].setdefault("merging_workflows", {})
        if pr["merged"]:
            step.update(merge_sha=pr["merge_commit_sha"], waiting=None)
            record["state"] = "releasing"
            return
        if not self.settings.auto_merge:
            step.update(waiting=f"a person to merge {pr['url']}")
            return
        if pr["mergeable_state"] == "dirty":
            raise Blocked(f"The workflows pull request has conflicts; resolve them by hand: {pr['url']}")
        if pr["mergeable_state"] not in ("clean", "unstable", "has_hooks"):
            step.update(waiting=f"the pull request to become mergeable (GitHub says {pr['mergeable_state']}): {pr['url']}")
            return
        merged = self.c.github.merge_pull_request(app.repository, number, "Add the Lab Deploy workflows")
        if not merged["merged"]:
            step.update(waiting=f"merge: {merged.get('reason') or 'not mergeable yet'}")
            return
        step.update(merge_sha=merged["sha"], waiting=None)
        record["state"] = "releasing"

    def step_releasing(self, record):
        app_id = record["application"]
        self.c._cache.pop(("pin", app_id), None)  # the pin just changed; do not trust the cached answer
        result = self.c.deploy_latest(self.principal(record), app_id)
        record["plan_id"] = result["id"]
        record["steps"]["releasing"] = {"plan_id": result["id"], "state": result["state"], "sha": result["sha"]}
        record["state"] = "complete"
