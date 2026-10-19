"""Contracts the platform keeps (docs/ROADMAP.md D31 to D33): apps/<service>/platform.yaml in the platform repository, for
a repository taken as it is.

The contract names the application repository, the service's folder in it and the commit that runs (`source`), and holds
the rest of the specification as an in-repository platform.yaml would. Merging it into the platform repository's main is
its review (D32): the controller never writes `reviewed` and never merges an import. A release is a pull request that
moves `source.commit` and nothing else; the controller opens it and merges it itself only when the application's
auto_merge allows it, the identity that asked may merge its own requests, and the change is exactly that one line. Once a
contract on main names something that has not been released, the controller releases it (the merge is the release).
"""
import re
import threading
import time

from . import reader, scaffold
from .config import Principal
from .github import checks_verdict, explain
from .models import Source, contract_text, parse_contract, spec_hash

MERGEABLE = ("clean", "unstable", "has_hooks")
RELEASE_IDENTITY = "release-on-merge"


def contract_path(service):
    return f"apps/{service}/platform.yaml"


def lock_path(service):
    return f"apps/{service}/requirements.lock"


def test_lock_path(service):
    return f"apps/{service}/requirements-test.lock"


def node_lock_path(service):
    return f"apps/{service}/package-lock.json"


def release_branch(service):
    return f"lab-deploy/release-{service}-"


def moved(text, commit):
    """The contract's text with source.commit replaced and nothing else, so comments and layout survive the release."""
    if not re.fullmatch(r"[a-f0-9]{40}", commit or ""):
        raise ValueError("A release names a full 40-character commit")
    out, inside, count = [], False, 0
    for line in text.splitlines(keepends=True):
        if re.match(r"^source:\s*(#.*)?$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            inside = False
        match = re.match(r"^(\s+)commit:\s*[\"']?[a-f0-9]{40}[\"']?\s*(#.*)?$", line) if inside else None
        if match:
            line = f"{match.group(1)}commit: {commit}\n"
            count += 1
        out.append(line)
    if count != 1:
        raise ValueError("The contract's source block does not hold exactly one commit line; move it by hand")
    return "".join(out)


def only_commit_moved(before_text, after_text):
    """(True, None) when a contract change moves source.commit and nothing else (D32), else (False, why)."""
    before_spec, before = parse_contract(before_text)
    after_spec, after = parse_contract(after_text)
    if spec_hash(before_spec) != spec_hash(after_spec):
        return False, "the change touches the specification, not only source.commit"
    if (before.repository.lower(), before.path) != (after.repository.lower(), after.path):
        return False, "the change touches source.repository or source.path"
    if before.commit == after.commit:
        return False, "source.commit does not move"
    return True, None


class Contracts:
    """The controller's side of contracts kept in the platform repository."""

    def __init__(self, controller):
        self.c = controller
        self.lock = threading.Lock()  # a release from a request and one from the scheduler never plan the same contract twice
        self._visibility = None
        self.runner = scaffold.docker_runner  # the containers that make locks (D27); tests replace it

    @property
    def platform(self):
        platform = self.c.config.platform
        if platform is None:
            raise ValueError("Set platform.repository in the controller configuration before keeping contracts")
        return platform

    def platform_public(self):
        """Whether the platform repository is public; its workflow logs and artifacts then are too."""
        if self._visibility is None:
            self._visibility = not self.c.github.repository(self.platform.repository)["private"]
        return self._visibility

    # ---- reading
    def read(self, service, ref=None):
        """The contract on the platform repository's main (or at `ref`): {"spec", "source", "commit", "text", "path"}, or None."""
        platform = self.platform
        ref = ref or self.c.github.branch_head(platform.repository, platform.branch)
        found = self.c.github.contents(platform.repository, ref, contract_path(service))
        if found is None:
            return None
        spec, source = parse_contract(found["text"])
        if spec.name != service:
            raise ValueError(f"{contract_path(service)} names service {spec.name}; the folder and the name must be the same")
        return {"spec": spec, "source": source, "commit": ref, "text": found["text"], "path": contract_path(service)}

    def require(self, app, ref=None):
        """The application's contract on main, which must point at the repository and folder the registry names."""
        found = self.read(app.service, ref)
        if found is None:
            raise ValueError(f"The platform keeps no contract for {app.service} on main yet ({contract_path(app.service)}): "
                             "import it with import_contract and merge that pull request")
        source = found["source"]
        if source.repository.lower() != app.repository.lower() or source.path != app.path:
            raise ValueError(f"{found['path']} points at {source.repository}" + (f" ({source.path})" if source.path else "")
                             + f", but the registry has {app.repository}" + (f" ({app.path})" if app.path else "") + "; correct one of them")
        return found

    # ---- the reader over GitHub
    def inspect(self, repository, path="", name=None):
        """The reader's answer for one service of a repository, read-only, with the repository's facts. Nothing is created."""
        github = self.c.github
        info = github.repository(repository)
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", info.get("full_name") or "") and info["full_name"].lower() == repository.lower():
            repository = info["full_name"]
        if (info.get("default_branch") or "main") != "main":
            raise ValueError(f"The default branch is {info.get('default_branch')}; the platform releases only from main")
        head = github.branch_head(repository, "main")
        paths, sizes = github.tree(repository, head, sizes=True)
        if path and path not in paths:
            raise ValueError(f"{repository} has no folder {path} on main")
        texts, skipped = {}, []
        wanted = reader.files_to_read(paths, path)
        for _ in range(4):  # a Node.js service's entry file and its imports are read in up to three more passes
            for item in wanted:
                try:
                    found = github.contents(repository, head, item, limit=1_000_000 if reader.large_files(item) else 65_536)
                except ValueError:
                    skipped.append(item)
                    continue
                if found is not None:
                    texts[item] = found["text"]
            wanted = [item for item in reader.more_files_to_read(paths, texts, path) if item not in skipped]
            if not wanted:
                break
        result = reader.read(paths, texts, path, name=name, fallback_name=repository.split("/")[1], sizes=sizes)
        if skipped:
            result["notes"].append("Not read, over the size limit: " + ", ".join(skipped[:8]))
        if info.get("private") and self.platform_public():
            result["refusals"].append({"file": repository, "problem": "is private, and the platform repository that would build it is public: its "
                                                                     "build logs and the image it uploads would be readable by anyone",
                                       "fix": "keep this repository's own workflows (follow docs/DEVELOPER-CONTRACT.md), or make the platform "
                                              "repository private first"})
            result["spec"] = None
        return {"repository": repository, "path": path, "head": head, "private": bool(info.get("private")),
                "repository_id": info.get("repository_id"), "owner_id": info.get("owner_id"), "read": result,
                "services": reader.services(paths, texts) if not path else []}

    # ---- importing
    def lock_files(self, result, runner=None):
        """{path: text} of the locks the platform makes for a service: its dependencies (unless the repository has a lock of
        its own) and, when it has tests, the service with its test tools. Raises ValueError naming what failed."""
        spec, lock, tests = result["spec"], result["lock"], result["tests"]
        files, app_lock, runner = {}, None, runner or self.runner
        if spec.runtime == "node-http":
            # D42: a repository without an npm lockfile is locked once here; its tests install from the same lock (npm ci
            # includes the development dependencies), so there is no second lock.
            if lock["action"] != "lock":
                return files
            made = scaffold.node_lock(lock["package"], spec.node, runner)
            if made["error"]:
                raise ValueError(f"The dependencies could not be locked: {made['error']}. Run `{made['command']}` with npm 11.10 or later by hand to see why.")
            return {node_lock_path(spec.name): made["text"]}
        if lock["action"] == "export":
            exported = scaffold.export_uv_lock(lock["pyproject"], lock["lock"], spec.python, runner)
            if exported["error"]:
                raise ValueError(f"uv.lock could not be exported: {exported['error']}")
            app_lock = exported["text"]
            if lock.get("server"):
                lock = {"action": "compile", "input": "\n".join(reader.pins_of(app_lock)) + "\n", "server": lock["server"]}
        if lock["action"] == "convert":
            app_lock = lock["text"]
        if lock["action"] == "compile":
            compiled = scaffold.compile_lock(lock["input"] + (lock["server"] + "\n" if lock.get("server") else ""), spec.python, runner)
            if compiled["error"]:
                raise ValueError(f"The dependencies could not be locked: {compiled['error']}. Run `{compiled['command']}` by hand to see why.")
            app_lock = compiled["text"]
        if app_lock is not None:
            files[lock_path(spec.name)] = app_lock if app_lock.endswith("\n") else app_lock + "\n"
        if tests:
            base = reader.pins_of(app_lock if app_lock is not None else lock.get("text", ""))
            compiled = scaffold.compile_lock("\n".join(base + tests["tools"]) + "\n", spec.python, runner)
            if compiled["error"]:
                raise ValueError(f"The test tools could not be locked with the service: {compiled['error']}")
            files[test_lock_path(spec.name)] = compiled["text"]
        return files

    def body(self, view, files):
        """The import pull request's text: what the reader chose and from where, what it set aside, what to check."""
        result = view["read"]
        where = f"`{view['repository']}`" + (f", folder `{view['path']}`" if view["path"] else "")
        lines = [f"Fast Deploy read {where} at `{view['head'][:12]}` as it is and drafted its contract. Nothing was added to that repository.",
                 "", "**Merging this pull request is the review** (docs/ROADMAP.md D32): the controller never merges it, and the contract says "
                 "nothing about `reviewed`. After the merge, the platform's own pipeline builds and releases exactly what it says.", "",
                 "| What | Chosen | From |", "|---|---|---|"]
        lines += [f"| {item['what']} | {str(item['value']).replace('|', '/')} | `{item['from']}` |" for item in result["choices"]]
        if result["ignored"]:
            lines += ["", "Set aside:"] + [f"- `{item['file']}`: {item['why']}" for item in result["ignored"]]
        if result["notes"]:
            lines += ["", "Please check:"] + [f"- {note}" for note in result["notes"]]
        lines += ["", "Files: " + ", ".join(f"`{name}`" for name in sorted(files)), ""]
        if result.get("runtime") == "node-http":
            lines.append("A lock, when the repository has none, is made by the platform with npm 11.10 or later in a container (`npm install "
                         f"{scaffold.NODE_LOCK_OPTIONS}`): every install script off, no version younger than three days, and every entry "
                         "checked for its integrity hash and a registry address. Installs run with every script off; only `build.scripts` "
                         "runs its own afterwards.")
        else:
            lines.append("Locks are made by the platform in a container of the service's own Python with uv, every package pinned with its hashes.")
        return "\n".join(lines) + "\n"

    def propose(self, principal, repository, path="", name=None, runner=None):
        """import_contract: read the service as it is, make its locks, and open the pull request that adds its contract (and
        locks) under apps/<service>/ in the platform repository. Returns the reader's answer and the pull request."""
        if "*" not in principal.applications or not principal.can_deploy:
            raise ValueError("Importing a contract needs an identity that may onboard: applications: ['*'] with can_deploy")
        view = self.inspect(repository, path, name)
        result = view["read"]
        if result["refusals"] or result["spec"] is None:
            return {**view, "pull_request": None}
        spec = result["spec"]
        existing = self.read(spec.name)
        if existing is not None:
            source = existing["source"]
            if source.repository.lower() != view["repository"].lower() or source.path != view["path"]:
                raise ValueError(f"{existing['path']} already holds {spec.name} for {source.repository}" + (f" ({source.path})" if source.path else "")
                                 + "; give this service another name (application)")
            return {**view, "pull_request": None, "existing": existing["path"],
                    "note": f"The platform already keeps {existing['path']} for this service; nothing was imported again."}
        files = self.lock_files(result, runner)
        source = Source(repository=view["repository"], path=view["path"], commit=view["head"])
        header = (f"# Imported by Fast Deploy from {view['repository']}" + (f" ({view['path']})" if view["path"] else "")
                  + f" at {view['head'][:12]} on {time.strftime('%Y-%m-%d', time.gmtime())}, read as it is: the pull request that added\n"
                  "# this file lists every choice and where it came from. Merging is the review (docs/ROADMAP.md D32); a release moves source.commit.\n")
        text = contract_text(spec, source, header)
        checked, _ = parse_contract(text)
        if spec_hash(checked) != spec_hash(spec):
            raise ValueError("The drafted contract does not read back as the same specification; nothing was opened")
        files[contract_path(spec.name)] = text
        if not self.c.config.live_dispatch:
            return {**view, "files": sorted(files), "pull_request": "disabled", "contract": text}
        platform = self.platform
        pull_request = self.c.github.open_platform_pull_request(
            platform.repository, platform.branch, f"lab-deploy/contract-{spec.name}", files,
            f"Contract for {spec.name}, taken as it is from {view['repository']}", self.body(view, files),
            f"Import the contract of {spec.name} from {view['repository']}")
        return {**view, "service": spec.name, "files": sorted(files), "pull_request": pull_request, "contract": text}

    # ---- releasing: a pull request that moves source.commit (D32)
    def merge_gate(self, app_id, app, record):
        """None when the controller may merge this release pull request itself, else what it waits for."""
        onboarding = self.c.config.onboarding
        allowed = app.auto_merge if app.auto_merge is not None else bool(onboarding and onboarding.auto_merge)
        if not allowed:
            return f"a person to merge {record['url']}: merging it is the release (auto_merge is off for {app_id})"
        requester = record.get("requested_by", "")
        if requester in (self.c.AUTO_DEPLOY_IDENTITY, RELEASE_IDENTITY):
            return None
        person = next((p for p in self.c.config.principals if p.name == requester.removeprefix("onboarding:")), None)
        if person is None or not person.may_merge_own_requests:
            return f"another operator to review and merge {record['url']}: {requester} asked for this release and may not approve their own request"
        return None

    def request_release(self, principal, app_id, app, commit=None):
        """Open (or find) the pull request that moves source.commit to `commit`, the tip of the application's main by default,
        and merge it when the gate allows. Returns the request record; state current when the contract already names it."""
        github, platform = self.c.github, self.platform
        found = self.require(app)
        target = commit or github.head(app)
        if found["source"].commit == target:
            return {"application": app_id, "state": "current", "target": target, "contract_commit": found["commit"]}
        self.c.ancestor_of_branch(app, target)
        text = moved(found["text"], target)
        ok, why = only_commit_moved(found["text"], text)
        if not ok:
            raise ValueError(f"The release would not be a plain move of source.commit: {why}")
        branch = release_branch(app.service) + target[:12]
        superseded = []
        for item in github.open_pull_requests(platform.repository, release_branch(app.service)):
            if item["branch"] != branch:
                github.close_pull_request(platform.repository, item["number"], f"Superseded: Fast Deploy now releases `{target[:12]}` instead (a newer request).")
                superseded.append(item["number"])
        body = (f"Releases `{app.service}` at `{target[:12]}` of {app.repository}: this pull request moves `source.commit` in "
                f"`{found['path']}` from `{found['source'].commit[:12]}` and changes nothing else (docs/ROADMAP.md D32).\n\n"
                f"Requested by `{principal.name}` (written by the controller, not by Claude). Merging it is the release: the controller "
                "builds, tests and releases that commit within a minute, with every guardrail of a release.\n")
        pull_request = github.open_platform_pull_request(platform.repository, platform.branch, branch, {found["path"]: text},
                                                          f"Release {app.service} at {target[:12]}", body, f"Release {app.service} at {target[:12]}")
        if pull_request.get("number") is None:
            raise ValueError(f"The release pull request could not be opened: {pull_request.get('note')}")
        record = {"application": app_id, "service": app.service, "pull_request": pull_request["number"], "url": pull_request["url"],
                  "branch": pull_request["branch"], "target": target, "from": found["source"].commit, "requested_by": principal.name,
                  "requested_at": time.time(), "state": "open", "superseded": superseded}
        self.c.store.save_release_request(app_id, record)
        return self.advance_request(app_id, app, record)

    def advance_request(self, app_id, app, record):
        """Look at a release pull request again: merged or closed by someone, or mergeable by the controller now."""
        github, platform = self.c.github, self.platform
        number, path = record["pull_request"], contract_path(app.service)
        pr = github.pull_request(platform.repository, number)
        if pr["merged"]:
            record.update(state="merged", merged_by=pr.get("merged_by"), merge_sha=pr.get("merge_commit_sha"), waiting=None)
        elif pr["state"] == "closed":
            record.update(state="closed", waiting=None)
        else:
            waiting = self.merge_gate(app_id, app, record)
            if waiting is None:
                files = github.pull_request_files(platform.repository, number)
                head = github.contents(platform.repository, pr["head_sha"], path)
                current = self.read(app.service)
                ok, why = (False, "the contract is not on main") if current is None or head is None else only_commit_moved(current["text"], head["text"])
                if files != [path]:
                    waiting = f"a person: the pull request changes more than {path} ({', '.join(files[:4])})"
                elif not ok:
                    waiting = f"a person: {why}"
                elif pr["mergeable_state"] not in MERGEABLE:
                    waiting = f"the platform's checks on the pull request (GitHub says {pr['mergeable_state']})"
                elif (verdict := checks_verdict(github.head_checks(platform.repository, pr["head_sha"]), pr.get("created_at")))[0] != "passed":
                    waiting = f"the pull request's checks: {verdict[1]}" if verdict[0] == "wait" else f"a person: {verdict[1]}"
                else:
                    self.c.ancestor_of_branch(app, parse_contract(head["text"])[1].commit)
                    merged = github.merge_pull_request(platform.repository, number, f"Release {app.service} at {record['target'][:12]}")
                    if merged.get("merged"):
                        record.update(state="merged", merged_by="the controller", merge_sha=merged.get("sha"), waiting=None)
                    else:
                        waiting = f"merge: {merged.get('reason') or 'not mergeable yet'}"
            if record["state"] == "open":
                record["waiting"] = waiting
        self.c.store.save_release_request(app_id, record)
        return record

    def release_now(self, principal, app_id):
        """Plan and dispatch what the application's contract on main says, unless that was released or tried already."""
        with self.lock:
            app = self.c.config.applications[app_id]
            found = self.require(app)
            attempted = self.c.store.latest_plan(app_id, found["source"].commit)
            if attempted and attempted.get("spec_hash") == spec_hash(found["spec"]) and not self.c.retryable(attempted):
                return {**attempted, "note": "This contract was already released or tried; its plan is above."}
            plan = self.c.plan(principal, app_id)
            return self.c.deploy(principal, plan["id"])

    def tick(self):
        """One pass of the scheduler: advance open release pull requests, then release every contract on main that names a
        commit or a specification not released yet (merging was the release)."""
        if self.c.config.paused:
            return {"paused": True}
        central = {app_id: app for app_id, app in self.c.config.applications.items() if app.central}
        if not central:
            return {}
        results = {}
        for app_id, app in central.items():
            record = self.c.store.release_request(app_id)
            if record and record.get("state") == "open":
                try:
                    self.advance_request(app_id, app, record)
                except Exception as exc:  # noqa: BLE001 - one application's trouble must not stop the others
                    results[f"{app_id}:request"] = f"error: {(explain(exc) or str(exc))[:160]}"
        head = self.c.github.branch_head(self.platform.repository, self.platform.branch)
        for app_id, app in central.items():
            if app.paused:
                results[app_id] = "paused"
                continue
            onboarding = self.c.store.onboarding(app_id)
            if onboarding and onboarding.get("state") != "complete":
                results[app_id] = f"onboarding ({onboarding.get('state')})"
                continue
            try:
                found = self.require(app, head)
                attempted = self.c.store.latest_plan(app_id, found["source"].commit)
                if attempted and attempted.get("spec_hash") == spec_hash(found["spec"]) and not self.c.retryable(attempted):
                    results[app_id] = f"up to date: {found['source'].commit[:12]} ({attempted['state']})"
                    continue
                principal = Principal(name=RELEASE_IDENTITY, token_sha256="0" * 64, applications=[app_id], can_deploy=True)
                outcome = self.release_now(principal, app_id)
                state = "dispatch disabled" if outcome.get("dispatch") == "disabled" else outcome.get("state")
                self.c.store.record_event("contract_release", name=app_id, identity=RELEASE_IDENTITY, application=app_id, outcome=state,
                                          detail=f"{found['source'].commit[:12]} contract {head[:12]} plan {str(outcome.get('id'))[:8]}")
                results[app_id] = f"{state}: plan {outcome.get('id')} for {found['source'].commit[:12]}"
            except Exception as exc:  # noqa: BLE001 - recorded, and the next pass looks again
                message = explain(exc) or str(exc)
                self.c.store.record_event("contract_release", name=app_id, identity=RELEASE_IDENTITY, application=app_id, outcome="error", detail=message)
                results[app_id] = f"error: {message[:160]}"
        return results
