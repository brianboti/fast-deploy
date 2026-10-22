"""One sentence: from a repository that follows docs/DEVELOPER-CONTRACT.md to a running service, as a resumable state machine.
With `as_is`, from a repository taken as it is (docs/ROADMAP.md D31 to D33): the reader drafts its contract, a pull request
adds it to the platform repository, a person's merge is the review, and the platform repository's own pipeline releases it;
nothing is added to the repository itself.

Every step looks at what already exists and continues from there, so a second call finishes an interrupted first one.
Steps that wait on GitHub or the pipeline (a plan, an apply, a build) end the call; the scheduler's tick or the next call
carries on. The controller merges only pull requests it opened itself, only on the application's own files, only when
the policy and the checks have passed, and only when the configuration allows it (`onboarding.auto_merge`, or the
application's own `auto_merge`) and the identity that asked may merge its own requests; otherwise it waits for a person.
"""
import re
import time

import httpx
import yaml

from .analyzer import TEST_PATH
from .config import SERVICE_PATH, Application, Principal
from .github import checks_verdict, explain
from .models import AppSpec
from .reader import GENERIC_FOLDERS, slug_of
from .scaffold import SERVICE_NAME

STEPS = ("validating", "contract", "merging_contract", "registering", "infrastructure", "planning", "applying", "environment", "workflows",
         "merging_workflows", "releasing", "complete")
WAITING = {"merging_contract", "planning", "applying", "merging_workflows"}
ACTIVE = set(STEPS) - {"complete"}
REQUIRED_FILES = (("Dockerfile", "the build recipe"), ("platform.yaml", "the application's contract"), ("requirements.txt", "pinned runtime dependencies"))
# A Node.js service (docs/ROADMAP.md D41, D42): package.json and its npm lockfile instead of requirements.txt.
NODE_REQUIRED_FILES = (("Dockerfile", "the build recipe"), ("platform.yaml", "the application's contract"), ("package.json", "the dependencies"),
                       ("package-lock.json", "every dependency locked with its integrity hash, installed with npm ci"))
# What the contract recommends and why; found() below says where each may live, with the scaffold's own rules (any CI
# workflow counts, Renovate counts for Dependabot), so the checker never asks for a file the scaffold would not draft.
RECOMMENDED_FILES = (
    ("requirements.lock", "hash-locked installs: the image matches the audited versions"),
    (".dockerignore", "keeps tests, caches and local files out of the image"),
    ("README.md", "what the service is and how to run it"),
    ("LICENSE", "lets the lab reuse the code"),
    ("SECURITY.md", "how to report a vulnerability privately"),
    (".github/workflows/ci.yml", "runs the tests on every pull request, so main stays releasable"),
    (".github/dependabot.yml", "keeps the pins current through reviewable pull requests"),
)


def recommended_found(name, everything, tree):
    """Whether a recommended file exists: `tree` is the service's folder (relative paths), `everything` the repository."""
    from .scaffold import DEPENDABOT, PLATFORM_WORKFLOW, RENOVATE, SECURITY_POLICY, WORKFLOW
    lowered = {entry.lower() for entry in everything} | {entry.lower() for entry in tree}
    if name == "requirements.lock":
        return "requirements.lock" in tree
    if name == ".dockerignore":  # a Dockerfile-specific ignore file counts, and is the one a whole-repository build reads
        return ".dockerignore" in tree or "Dockerfile.dockerignore" in tree
    if name in ("README.md", "LICENSE"):
        stem = name.split(".")[0].lower()
        return any(entry.split("/")[-1].startswith((stem, "licence" if stem == "license" else stem)) and entry.count("/") == 0 for entry in lowered)
    if name == "SECURITY.md":
        return any(candidate in lowered for candidate in SECURITY_POLICY)
    if name == ".github/workflows/ci.yml":
        return any(WORKFLOW.fullmatch(entry) and not PLATFORM_WORKFLOW.fullmatch(entry) for entry in everything)
    if name == ".github/dependabot.yml":
        return any(candidate in everything for candidate in (*DEPENDABOT, *RENOVATE))
    return name in tree
# The files the platform's checker may draft for a developer, in a draft pull request it never merges (docs/ROADMAP.md D26).
PLATFORM_WRITES = frozenset({"Dockerfile", ".dockerignore", "requirements-dev.txt", "requirements.lock", "package-lock.json", "tests", "platform.yaml",
                             ".github/workflows/ci.yml", "SECURITY.md", ".github/dependabot.yml"})
WHAT_NEXT = {
    "validating": "checking the repository against the contract",
    "contract": "opening the pull request that adds the contract to the platform repository",
    "merging_contract": "waiting for a person to review and merge the contract pull request (merging it is the review)",
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


def contract_check(github, repository, folder=""):
    """The contract's checklist for one service of a repository, read-only (docs/DEVELOPER-CONTRACT.md): every required item
    and every recommended one, whether it holds, why it matters, and whether the platform's checker can draft it. Raises
    Blocked when the repository cannot be checked at all (another default branch, a folder that does not exist)."""
    repo = github.repository(repository)
    # The name exactly as GitHub spells it: the registry, the workflows and the identities all use this one.
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo.get("full_name") or "") and repo["full_name"].lower() == repository.lower():
        repository = repo["full_name"]
    branch = repo["default_branch"] or "main"
    if branch != "main":
        raise Blocked(f"The default branch is {branch}; the platform's own workflows release only from main. Deploy it as it is instead "
                      "(onboard_application with as_is true): the platform then keeps the contract and reads the repository's default branch")
    everything = github.tree(repository, branch)
    tree = everything
    if folder:
        if folder not in everything:
            raise Blocked(f"The repository has no folder {folder} on {branch}; give the folder that holds the service's Dockerfile and platform.yaml")
        tree = [entry[len(folder) + 1:] for entry in everything if entry.startswith(folder + "/")]
    checklist, recommended, files = [], [], {}

    def item(key, name, ok, why=None, detail=None, into=checklist):
        entry = {"item": name, "ok": bool(ok), "platform_writes": key in PLATFORM_WRITES and not ok}
        if why:
            entry["why"] = why
        if detail:
            entry["detail"] = detail
        into.append(entry)

    python_manifests = ("requirements.txt", "requirements.lock", "pyproject.toml", "Pipfile", "uv.lock", "poetry.lock", "environment.yml")
    node = "package.json" in tree and not any(name in tree for name in python_manifests)
    for name, why in NODE_REQUIRED_FILES if node else REQUIRED_FILES:
        where = f"{folder}/{name}" if folder else name
        if name == "package-lock.json":
            # Its presence is what counts here (the reader checks every entry); a lockfile can be far larger than 64 KiB.
            shrinkwrap = "npm-shrinkwrap.json" in tree
            item(name, (f"{folder}/npm-shrinkwrap.json" if folder else "npm-shrinkwrap.json") if shrinkwrap else where,
                 shrinkwrap or name in tree, why)
            continue
        files[name] = github.contents(repository, branch, where)
        if name == "Dockerfile" and files[name] is None and "Procfile" in tree:
            # Built with Google's buildpacks instead (docs/ROADMAP.md D28); the images are several times larger.
            item(name, where, True, why, detail="no Dockerfile: Google's buildpacks build the image from the Procfile")
            continue
        item(name, where, files[name] is not None, why)
    spec = None
    if files["platform.yaml"] is not None:
        try:
            spec = AppSpec.model_validate(yaml.safe_load(files["platform.yaml"]["text"]))
            item("valid", "platform.yaml is valid", True)
        except Exception as exc:  # noqa: BLE001 - any parse or validation problem is a checklist line
            item("valid", "platform.yaml is valid", False, detail=str(exc)[:300])
    if spec is not None:
        item("reviewed", "platform.yaml says reviewed: true", spec.reviewed, "a person has read it")
        if spec.tests == "required" and spec.runtime == "node-http":
            from .node import package_data, tests_of
            how, _, _ = tests_of(package_data((files.get("package.json") or {}).get("text")) or {}, tree, spec.node)
            item("tests", "tests (a test script, or files node --test finds)", how is not None, "run before every build")
        elif spec.tests == "required":
            item("tests", "tests (pytest files or a tests/ directory)", any(TEST_PATH.search(entry) for entry in tree), "run before every build")
            item("requirements-dev.txt", "requirements-dev.txt", "requirements-dev.txt" in tree, "installs pytest for the test step")
        dockerfile = (files["Dockerfile"] or {}).get("text", "")
        if dockerfile:
            item("port", f"Dockerfile exposes port {spec.port}", re.search(rf"^\s*EXPOSE\s+{spec.port}(/tcp)?\s*$", dockerfile, re.M) is not None,
                 "the port in platform.yaml is the one Cloud Run sends requests to")
    for name, why in RECOMMENDED_FILES:
        if node and name == "requirements.lock":
            continue  # a Node.js service's lockfile is required above
        in_service = name in ("requirements.lock", ".dockerignore")
        item(name, f"{folder}/{name}" if folder and in_service else name, recommended_found(name, everything, tree), why, into=recommended)
    return {"repository": repository, "path": folder, "branch": branch, "repository_id": repo["repository_id"], "owner_id": repo["owner_id"],
            "checklist": checklist, "recommended": recommended, "spec": spec, "ready": all(entry["ok"] for entry in checklist)}


def missing_message(checklist):
    """Why a repository is not ready, and what the platform can draft for it."""
    missing = [entry for entry in checklist if not entry["ok"]]
    text = ("The repository does not follow the contract yet: " + "; ".join(entry["item"] + (f" ({entry['detail']})" if entry.get("detail") else "")
                                                                             for entry in missing) + ". See docs/DEVELOPER-CONTRACT.md.")
    writable = [entry["item"] for entry in missing if entry["platform_writes"]]
    if writable:
        text += (f" The platform can draft {', '.join(writable)} in a draft pull request (propose_configuration); a person reviews it, sets "
                 "reviewed: true and merges it.")
    return text


def plain(error):
    """A GitHub failure as a sentence that names the repository and, for a refused write, the permissions onboarding needs."""
    repository = None
    if isinstance(error, httpx.HTTPStatusError):
        match = re.search(r"/repos/([^/]+/[^/]+)", str(error.request.url.path))
        repository = match.group(1) if match else None
    text = explain(error, repository) or f"{type(error).__name__}: {str(error)[:500]}"
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (403, 404):
        text += " Onboarding writes to the application and the platform repositories: the credential needs the permissions in docs/SETUP.md, section 7g."
    return text


def normalize_repository(text):
    """owner/name from a GitHub link or an owner/name string."""
    value = text.strip()
    match = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return value
    raise ValueError("Give a GitHub repository as https://github.com/OWNER/NAME or OWNER/NAME")


def normalize_path(text):
    """A service's folder as the registry keeps it: '' for the repository's root, else a relative path such as services/api."""
    value = (text or "").strip().replace("\\", "/").strip("/")
    while value.startswith("./"):
        value = value[2:]
    if value in ("", "."):
        return ""
    if not SERVICE_PATH.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("Give the service's folder inside the repository, such as services/api (relative, no hidden or parent folders)")
    return value


def slug(name):
    """An application id from a repository name: lowercase letters, digits and dashes."""
    value = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30]
    if not value or not value[0].isalpha():
        raise ValueError("Give an application id (lowercase letters, digits and dashes, starting with a letter)")
    return value


def summary(record):
    """One line for people: when the onboarding finished, how long it took, and how often a person had to step in."""
    measures = record.get("measures", {})
    blocked, interventions = measures.get("blocked", 0), measures.get("interventions", 0)
    when = time.strftime("%Y-%m-%d", time.gmtime(record.get("completed_at") or time.time()))
    text = f"Onboarded {when} in {measures.get('minutes', 0):g} minutes"
    if not blocked:
        return text + ", with no step blocked."
    steps = ", ".join(measures.get("blocked_steps", []))
    return text + (f"; {blocked} block{'s' if blocked != 1 else ''} ({steps}), {interventions} resolved by the operator.")


def describe(record):
    """The record as the operator sees it: the step, what it waits for or what blocked it, and what exists so far."""
    state = record["state"]
    view = {k: record.get(k) for k in ("application", "repository", "path", "service", "state", "plan_id", "auto_deploy", "created_at", "updated_at",
                                       "completed_at", "measures")}
    view["as_is"] = bool(record.get("as_is"))
    view["requested_by"] = record.get("principal")
    view["steps"] = record.get("steps", {})
    if state == "complete":
        view["next_action"] = (f"Onboarding is complete; the first release is plan {record.get('plan_id')}. Ask for get_deployment_status with that id.")
    elif state == "blocked":
        view["blocked_at"] = record.get("blocked_at")
        view["error"] = record.get("error")
        view["next_action"] = "Fix what the error names, then call onboard_application again with the same repository; it continues from this step."
    else:
        waiting = (view["steps"].get(state) or {}).get("waiting")
        doing = f"waiting for {waiting}" if waiting else WHAT_NEXT[state]
        view["next_action"] = f"In progress: {doing.rstrip('.')}. The controller continues on its own every minute; ask for get_onboarding_status to follow it."
    return view


def application_name(repository, path="", app_id=None, as_is=False):
    """Resolve the name identically for the setup review and the actual onboarding."""
    folder = path.rsplit("/", 1)[-1] if path else None
    if not app_id and as_is and folder and folder.lower() in GENERIC_FOLDERS:
        app_id = slug_of(f"{repository.split('/')[1]}-{folder}") or folder
    return slug(app_id or folder or repository.split("/")[1])


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

    def merge_gate(self, record, pr):
        """None when onboarding may merge this pull request itself, else what it waits for (docs/ROADMAP.md D29).

        Auto-merge must be on for the application (its own `auto_merge`, else `onboarding.auto_merge`), and the identity that
        asked for the onboarding must be allowed to merge its own requests. Otherwise a person merges it, and the next pass
        sees the merge and continues; an identity no longer in the configuration cannot approve anything.
        """
        app = self.c.config.applications.get(record["application"])
        own = getattr(app, "auto_merge", None)
        if not (self.settings.auto_merge if own is None else own):
            return f"a person to merge {pr['url']}"
        requester = next((p for p in self.c.config.principals if p.name == record["principal"]), None)
        if requester is None or not requester.may_merge_own_requests:
            return (f"another operator to review and merge PR #{pr['number']} ({pr['url']}): {record['principal']} asked for this onboarding "
                    "and may not approve their own request")
        return None

    # ---- entry points
    def start(self, principal, repository, app_id=None, auto_deploy=False, path=None, as_is=False, until=None):
        if "*" not in principal.applications:
            raise ValueError("Onboarding needs an identity that may see every application: applications: ['*'] in the controller configuration")
        if not principal.can_deploy:
            raise ValueError("Identity has read-only access")
        _ = self.settings  # fail early when the block is missing
        repository = normalize_repository(repository)
        path = normalize_path(path)
        app_id = application_name(repository, path, app_id, as_is)
        record = self.c.store.onboarding(app_id)
        if record is None:
            record = {"application": app_id, "repository": repository, "path": path, "principal": principal.name, "auto_deploy": bool(auto_deploy),
                      "as_is": bool(as_is), "state": "validating", "steps": {}, "created_at": time.time()}
            self.c.store.record_event("onboarding", name=app_id, identity=principal.name, application=app_id, outcome="started",
                                      detail=repository + (f" ({path})" if path else ""))
        elif record["repository"].lower() != repository.lower() or record.get("path", "") != path:
            where = record["repository"] + (f" at {record['path']}" if record.get("path") else "")
            raise ValueError(f"Application {app_id} is being onboarded from {where}; give another application id for this repository or folder")
        elif bool(record.get("as_is")) != bool(as_is) and record["state"] != "complete":
            how = "as it is (its contract kept by the platform)" if record.get("as_is") else "with its own workflows (it follows the contract)"
            raise ValueError(f"Application {app_id} is being onboarded {how}; finish that, or give another application id")
        elif record["state"] == "complete":
            return describe(record)
        elif record["state"] == "blocked":
            # Someone fixed what blocked it and asked again: an intervention, counted for the onboarding measures.
            measures = record.setdefault("measures", {})
            measures["interventions"] = measures.get("interventions", 0) + 1
            record["state"], record["error"] = record.pop("blocked_at", "validating"), None
        return self.advance(record, until)

    def advance(self, record, until=None):
        """Run every step that can finish now; stop at the first that waits, blocks or completes, or on reaching `until`: a
        state left to the onboarding tick, which runs it within a minute (1.22.2: one call for several services returns
        once each is read, and the lock compiles and contract pull requests follow in the background)."""
        for _ in range(len(STEPS)):
            state = record["state"]
            if state not in ACTIVE or state == until:
                break
            try:
                getattr(self, "step_" + state)(record)
            except Blocked as exc:
                record["blocked_at"], record["state"], record["error"] = state, "blocked", str(exc)[:600]
            except Exception as exc:  # noqa: BLE001 - a GitHub or cloud error is reported the same way, and the next call retries the step
                record["blocked_at"], record["state"], record["error"] = state, "blocked", plain(exc)[:600]
            if record["state"] == "blocked":
                measures = record.setdefault("measures", {})
                measures["blocked"] = measures.get("blocked", 0) + 1
                measures["blocked_steps"] = sorted(set(measures.get("blocked_steps", [])) | {state})
            self.c.store.save_onboarding(record["application"], record)
            if record["state"] != state:
                self.c.store.record_event("onboarding", name=record["application"], application=record["application"], outcome=record["state"],
                                          detail=record.get("error") if record["state"] == "blocked" else state)
            if record["state"] == state or record["state"] in ("blocked", "complete"):
                break
        return describe(record)

    # ---- the steps
    def step_validating(self, record):
        if record.get("as_is"):
            return self.validate_as_is(record)
        result = contract_check(self.c.github, record["repository"], record.get("path", ""))
        record["repository"] = result["repository"]
        record["steps"]["validating"] = {"checklist": result["checklist"], "recommended": result["recommended"],
                                         "repository_id": result["repository_id"], "owner_id": result["owner_id"]}
        if not result["ready"]:
            raise Blocked(missing_message(result["checklist"]))
        record["service"] = result["spec"].name
        record["needs"] = result["spec"].needs.model_dump()
        record["project"] = result["spec"].project or "shared"
        record["state"] = "registering"

    def validate_as_is(self, record):
        """The reader instead of the checklist (docs/ROADMAP.md 13.5): its choices and refusals, and whether the platform
        already keeps a contract for this service."""
        contracts = self.c.contracts
        name = record["application"] if SERVICE_NAME.fullmatch(record["application"]) else None
        view = contracts.inspect(record["repository"], record.get("path", ""), name)
        read = view["read"]
        folders = [entry["path"] for entry in view["services"] if entry["path"]]
        if read["refusals"] and not record.get("path") and len(folders) == 1 and not any(entry["path"] == "" for entry in view["services"]):
            # D56: a root that holds one service in a folder takes that folder without asking.
            record["path"] = folders[0]
            record["steps"]["folder"] = {"path": folders[0], "why": "the root holds no service of its own; this folder does"}
            view = contracts.inspect(view["repository"], folders[0], name)
            read = view["read"]
        record["repository"] = view["repository"]
        record["steps"]["validating"] = {"as_is": True, "choices": read["choices"], "ignored": read["ignored"], "notes": read["notes"],
                                         "refusals": read["refusals"], "head": view["head"], "branch": view.get("branch") or "main",
                                         "repository_id": view["repository_id"], "owner_id": view["owner_id"]}
        if read["refusals"] or read["spec"] is None:
            raise Blocked("It cannot be taken as it is: " + "; ".join(f"{item['file']} {item['problem']} (fix: {item['fix']})" for item in read["refusals"][:6]))
        record["service"] = read["spec"].name
        record["needs"] = read["spec"].needs.model_dump()
        record["project"] = read["spec"].project or "shared"
        existing = contracts.read(record["service"])
        if existing is None:
            record["state"] = "contract"
            return
        self.adopt(record, existing)

    def adopt(self, record, found):
        """A contract on the platform's main for this service: it must point at this repository and folder."""
        source = found["source"]
        if source.repository.lower() != record["repository"].lower() or source.path != record.get("path", ""):
            raise Blocked(f"{found['path']} already holds {record['service']} for {source.repository}" + (f" ({source.path})" if source.path else "")
                          + "; give this service another application id")
        record["needs"] = found["spec"].needs.model_dump()
        record["project"] = found["spec"].project or "shared"
        record["steps"]["contract_on_main"] = {"path": found["path"], "commit": found["commit"], "source_commit": source.commit}
        record["state"] = "registering"

    def step_contract(self, record):
        result = self.c.contracts.propose(self.principal(record), record["repository"], record.get("path", ""), record["service"])
        read = result["read"]
        if read["refusals"] or read["spec"] is None:
            raise Blocked("It cannot be taken as it is: " + "; ".join(f"{item['file']} {item['problem']} (fix: {item['fix']})" for item in read["refusals"][:6]))
        if result.get("existing"):
            self.adopt(record, self.c.contracts.read(record["service"]))
            return
        pull_request = result.get("pull_request")
        if pull_request == "disabled":
            raise Blocked("Writes are disabled (live_dispatch: false), so the contract pull request was not opened")
        if not pull_request or pull_request.get("number") is None:
            raise Blocked(f"The contract pull request could not be opened: {(pull_request or {}).get('note')}")
        record["steps"]["contract"] = {"pull_request": pull_request["number"], "url": pull_request["url"], "files": result.get("files")}
        record["state"] = "merging_contract"

    def step_merging_contract(self, record):
        """Only a person merges a contract: the merge is the review (docs/ROADMAP.md D32). The controller waits for it."""
        platform = self.c.config.platform
        opened = record["steps"].get("contract") or {}
        if not opened.get("pull_request"):
            record["state"] = "contract"
            return
        pr = self.c.github.pull_request(platform.repository, opened["pull_request"])
        step = record["steps"].setdefault("merging_contract", {})
        if pr["merged"]:
            found = self.c.contracts.read(record["service"])
            if found is None:
                step.update(waiting="the merge to show on the platform repository's main")
                return
            step.update(merged_by=pr.get("merged_by"), merge_sha=pr.get("merge_commit_sha"), waiting=None)
            self.adopt(record, found)
            return
        if pr["state"] == "closed":
            record["steps"].pop("contract", None)
            raise Blocked(f"The contract pull request {pr['url']} was closed without merging; ask again to draft a fresh one")
        step.update(waiting=f"a person to review and merge {pr['url']}: merging it is the review, and the controller never merges a contract")

    def step_registering(self, record):
        app_id, service, folder = record["application"], record["service"], record.get("path", "")
        if app_id in self.c.config.applications:
            app = self.c.config.applications[app_id]
            if app.central != bool(record.get("as_is")):
                raise Blocked(f"Application {app_id} is already registered " + ("as it is" if app.central else "with its own workflows")
                              + "; give another application id, or unregister it first (lab-deploy unregister " + app_id + ")")
            if app.repository.lower() != record["repository"].lower() or app.service != service or app.path != folder:
                raise Blocked(f"Application {app_id} is already registered for {app.repository}" + (f" at {app.path}" if app.path else "")
                              + f" as service {app.service}; give another application id, or unregister it first (lab-deploy unregister {app_id})")
            record["steps"]["registering"] = {"note": "already registered"}
        else:
            settings = self.settings
            project, region, number = settings.project_id, settings.region, settings.project_number
            if record.get("project") == "own":
                # A project of its own (docs/ROADMAP.md 14.9): only one the operator created and recorded, never the shared one.
                own = settings.own_projects.get(service)
                if own is None:
                    raise Blocked(f"The contract asks for a project of its own (project: own), and none is recorded for {service}. The operator "
                                  "creates a project, links it to the billing account, applies its foundation (scripts/apply-foundation.sh "
                                  f"PROJECT own, docs/SETUP.md 7s) and adds it under onboarding.own_projects.{service}; then ask again")
                project, region, number = own.project_id, own.region, own.project_number
            # The first application of a repository keeps the plain names; a further one gets its own (D24), so the two never
            # share workflows, an environment's variables, a deploy identity's trust or a release queue.
            # GitHub's names ignore case, so lab/Mono and lab/mono are one repository with one set of workflow files.
            sibling = any(other.repository.lower() == record["repository"].lower() and not other.central for other in self.c.config.applications.values())
            suffix = f"-{service}" if sibling else ""
            layout = {"workflow": f"deploy-gcp{suffix}.yml", "environment": f"staging{suffix}"}
            if record.get("as_is"):
                # The platform repository's pipeline in its own environment staging-<service> (docs/ROADMAP.md D33).
                platform = self.c.config.platform
                if platform is None:
                    raise ValueError("Set platform.repository in the controller configuration before onboarding repositories")
                layout = {"workflow": "deploy.yml", "environment": f"staging-{service}", "contract": "platform", "pipeline_repository": platform.repository}
            # A repository taken as it is is released from its own default branch (1.22.1); one with its own workflows from main.
            branch = (record["steps"].get("validating") or {}).get("branch") or "main"
            entry = {"repository": record["repository"], "branch": branch, "service": service, "auto_deploy": record["auto_deploy"],
                     "path": folder, **layout,
                     "target": {"project_id": project, "region": region, "service": service, "registry": service,
                                "deploy_service_account": f"{service}-deploy@{project}.iam.gserviceaccount.com",
                                "probe_service_account": f"{service}-probe@{project}.iam.gserviceaccount.com",
                                "wif_provider": f"projects/{number}/locations/global/workloadIdentityPools/{service}-gh/providers/github",
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
        project = self.c.config.applications[app_id].target.project_id
        # The shared project's state bucket may be named in the settings; a project of its own keeps its state in its own.
        bucket = (settings.state_bucket or f"{settings.project_id}-tfstate") if project == settings.project_id else f"{project}-tfstate"
        render = {"state_bucket": bucket, "bootstrap_image": settings.bootstrap_image}
        rendered = self.c.render_infrastructure(self.principal(record), app_id, **render)
        existing = self.c.github.contents(platform.repository, platform.branch, f"infra/gcp/apps/{service}/main.tf")
        if existing is not None and existing["text"] == rendered["files"]["main.tf"]:
            record["steps"]["infrastructure"] = {"note": "the root is already on main"}
            record["state"] = "environment"
            return
        result = self.c.propose_infrastructure(self.principal(record), app_id, allow_writes=True, through=f"the onboarding of `{app_id}`", **render)
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
            # Who approved it, as GitHub records it (D29): the audit trail keeps the requester and the approver apart.
            step.update(merge_sha=pr["merge_commit_sha"], waiting=None, merged_by=pr.get("merged_by"))
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
        waiting = self.merge_gate(record, pr)
        if waiting:
            step.update(waiting=waiting)
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
            later = self.later_apply(platform.repository, record["service"], merge_sha)
            if later is None:
                raise Blocked(f"The infrastructure apply failed: {run['url']}. Fix the cause and dispatch the apply workflow for this application, then call again.")
            run = later
        step.update(run=run["url"], waiting=None)
        record["state"] = "environment"

    def later_apply(self, repository, service, merge_sha):
        """The recovery the blocked message names: the cause was fixed, often by a commit on main, and the apply was dispatched
        again. A successful apply of this application on any commit that contains the merge counts."""
        github = self.c.github
        if not all(hasattr(github, name) for name in ("recent_runs", "job_conclusions", "contains")):
            return None
        for run in github.recent_runs(repository, "infra-apply.yml"):
            if run["conclusion"] != "success" or run["head_sha"] == merge_sha:
                continue
            if github.job_conclusions(repository, run["id"]).get(f"apply ({service})") == "success" and github.contains(repository, merge_sha, run["head_sha"]):
                return run
        return None

    def step_environment(self, record):
        settings = self.settings
        app = self.c.config.applications[record["application"]]
        target = app.target
        variables = {"GCP_PROJECT_ID": target.project_id, "GCP_REGION": target.region, "CLOUD_RUN_SERVICE": target.service, "ARTIFACT_REPOSITORY": target.registry,
                     "WIF_PROVIDER": target.wif_provider, "DEPLOY_SERVICE_ACCOUNT": target.deploy_service_account, "PROBE_SERVICE_ACCOUNT": target.probe_service_account,
                     "RECOVERY_BUCKET": target.recovery_bucket}
        if settings.billing_export_table:
            variables["BILLING_EXPORT_TABLE"] = settings.billing_export_table
        if settings.iap_client_id:
            variables["IAP_CLIENT_ID"] = settings.iap_client_id
        # An application taken as it is runs in its own environment of the platform repository (docs/ROADMAP.md D33).
        repository, branch = (app.pipeline_repo, "main") if app.central else (app.repository, app.branch)
        self.c.github.ensure_environment(repository, app.environment, branch)
        self.c.github.set_environment_variables(repository, app.environment, variables)
        record["steps"]["environment"] = {"repository": repository, "environment": app.environment, "variables": sorted(variables)}
        record["state"] = "workflows"

    def step_workflows(self, record):
        if self.c.config.applications[record["application"]].central:
            record["steps"]["workflows"] = {"note": "none: the platform repository's own pipeline releases it"}
            record["state"] = "releasing"
            return
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
            step.update(merge_sha=pr["merge_commit_sha"], waiting=None, merged_by=pr.get("merged_by"))
            record["state"] = "releasing"
            return
        waiting = self.merge_gate(record, pr)
        if waiting:
            step.update(waiting=waiting)
            return
        verdict, why = checks_verdict(self.c.github.head_checks(app.repository, pr["head_sha"]), pr.get("created_at"))
        if verdict == "wait":
            step.update(waiting=f"{why}: {pr['url']}")
            return
        if verdict == "failed":
            raise Blocked(f"The workflows pull request's checks failed: {why}. Fix the repository so they pass, then ask again: {pr['url']}")
        if pr["mergeable_state"] == "dirty":
            raise Blocked(f"The workflows pull request has conflicts; resolve them by hand: {pr['url']}")
        if pr["mergeable_state"] not in ("clean", "unstable", "has_hooks"):
            step.update(waiting=f"the pull request to become mergeable (GitHub says {pr['mergeable_state']}): {pr['url']}")
            return
        merged = self.c.github.merge_pull_request(app.repository, number, "Add the Fast Deploy workflows")
        if not merged["merged"]:
            step.update(waiting=f"merge: {merged.get('reason') or 'not mergeable yet'}")
            return
        step.update(merge_sha=merged["sha"], waiting=None)
        record["state"] = "releasing"

    def step_releasing(self, record):
        app_id = record["application"]
        self.c._cache.pop(("pin", app_id), None)  # the pin just changed; do not trust the cached answer
        if self.c.config.applications[app_id].central:
            # Exactly what the merged contract names, not a newer commit (that would be a release pull request of its own).
            result = self.c.contracts.release_now(self.principal(record), app_id)
        else:
            result = self.c.deploy_latest(self.principal(record), app_id)
        record["plan_id"] = result["id"]
        record["steps"]["releasing"] = {"plan_id": result["id"], "state": result["state"], "sha": result["sha"]}
        record["completed_at"] = time.time()
        measures = record.setdefault("measures", {})
        measures["minutes"] = round((record["completed_at"] - record.get("created_at", record["completed_at"])) / 60, 1)
        record["state"] = "complete"
        # The weekly cost issue repeats it, so the lab sees how long onboarding took without asking (docs/ROADMAP.md 11.5).
        app = self.c.config.applications[app_id]
        try:
            self.c.github.set_environment_variables(app.pipeline_repo, app.environment, {"ONBOARDING_SUMMARY": summary(record)})
        except Exception as exc:  # noqa: BLE001 - a measure is never worth failing an onboarding that has finished
            record["steps"]["releasing"]["note"] = f"The onboarding summary was not written to the environment: {plain(exc)[:200]}"
