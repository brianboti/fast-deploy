"""GitHub App credentials stay server-side. No cloud credentials in this process."""
import base64
import datetime
import io
import json
import os
import time
import types
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from .bake import DEFAULT_BAKE_SECONDS

TOKEN_WARNING_DAYS = 14


def needed_permission(request):
    """The fine-grained token permission a refused request needs, read from its method and path."""
    method, path = request.method, request.url.path
    if "/environments/" in path and "/variables" in path:
        return "Environments: read and write"
    if "/environments/" in path:
        return "Administration: read and write (it creates the deployment environment)"
    if "/actions/variables" in path:
        return "Variables: read and write"
    if path.endswith("/dispatches") or ("/actions/runs/" in path and method != "GET"):
        return "Actions: read and write"
    if "/contents/.github/workflows/" in path and method != "GET":
        return "Workflows: read and write"
    if "/pulls" in path and method != "GET":
        return "Pull requests: read and write"
    if method != "GET" and ("/contents/" in path or "/git/" in path):
        return "Contents: read and write"
    return "Contents: read and Actions: read"


def explain(error, repository=None):
    """A plain sentence for a GitHub failure, or None when the error is not a GitHub transport error."""
    where = f" for {repository}" if repository else ""
    if isinstance(error, httpx.HTTPStatusError):
        status, headers = error.response.status_code, error.response.headers
        if status == 401:
            return "GitHub rejected the controller's credential: the token is expired or revoked. Renew it on the controller host and restart."
        if status == 403 and headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            when = datetime.datetime.fromtimestamp(int(reset), datetime.UTC).strftime("%H:%M UTC") if reset and reset.isdigit() else "shortly"
            return f"GitHub API rate limit reached; it resets at {when}. Retry then."
        if status == 403:
            return f"GitHub refused access{where}: the credential needs the permission {needed_permission(error.request)} for this request."
        if status == 404:
            return f"GitHub could not find the repository, branch or file{where}; check the registry entry and that the credential can read that repository."
        if status == 422:
            try:
                detail = error.response.json().get("message", "")
            except ValueError:
                detail = ""
            hint = " If this was a workflow dispatch, the app's workflow files may pin an older platform; regenerate them." if "input" in detail.lower() or "workflow" in detail.lower() else ""
            return f"GitHub refused the request{where}: {detail or 'unprocessable'}.{hint}"
        if status >= 500:
            return f"GitHub is unavailable right now (HTTP {status}); retry in a few minutes."
        return f"GitHub answered HTTP {status}{where}."
    if isinstance(error, httpx.TimeoutException):
        return "GitHub did not answer in time; retry in a moment."
    if isinstance(error, httpx.RequestError):
        return f"Could not reach GitHub ({type(error).__name__}); check the controller's network connection."
    return None


def parse_expiration(header):
    """GitHub sends 'YYYY-MM-DD HH:MM:SS UTC' for fine-grained tokens."""
    text = (header or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
    except ValueError:
        return None



def pipeline_of(app):
    """The repository whose workflows release an application: the platform's for a contract it keeps, else the app's own."""
    return getattr(app, "pipeline_repo", None) or app.repository


def central_of(app):
    """Whether the platform repository's own pipeline releases this application (docs/ROADMAP.md D33)."""
    return bool(getattr(app, "central", False))

class GitHub:
    def __init__(self, client=None):
        self.client = client or httpx.Client(base_url="https://api.github.com", timeout=30, follow_redirects=False)
        self.cached_token = None
        self.token_until = 0

    def token(self):
        # A fine-grained token is supported for development; prefer App installation tokens.
        if os.getenv("GITHUB_TOKEN"):
            return os.environ["GITHUB_TOKEN"]
        if self.cached_token and time.time() < self.token_until:
            return self.cached_token
        app_id = os.getenv("GITHUB_APP_ID")
        installation_id = os.getenv("GITHUB_INSTALLATION_ID")
        key_path = os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")
        if not app_id or not installation_id or not key_path or not installation_id.isdigit():
            raise ValueError("Configure GitHub App credentials or a scoped GITHUB_TOKEN on the server")
        now = int(time.time())
        signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, Path(key_path).read_text(), algorithm="RS256")
        response = self.client.post(f"/app/installations/{installation_id}/access_tokens", headers=self.headers(signed))
        response.raise_for_status()
        self.cached_token = response.json()["token"]
        self.token_until = now + 3000
        return self.cached_token

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, headers=self.headers(self.token()), **kwargs)
        response.raise_for_status()
        return response

    def head(self, app):
        return self.commit(app)["sha"]

    def commit(self, app, ref=None):
        """The commit at a ref with the facts people recognize it by: first line of the message, author, date."""
        data = self.request("GET", f"/repos/{app.repository}/commits/{quote(ref or app.branch, safe='')}").json()
        details = data.get("commit") or {}
        message = (details.get("message") or "").strip().splitlines()
        return {"sha": data["sha"], "message": message[0][:200] if message else "", "author": (details.get("author") or {}).get("name"),
                "date": (details.get("author") or {}).get("date"), "url": data.get("html_url")}

    def compare(self, app, base_sha, head=None):
        """Commits and files between base_sha and the branch (or another ref): how far main has moved past the live release."""
        return self.compare_repository(app.repository, base_sha, head or app.branch)

    def compare_repository(self, repository, base, head):
        data = self.request("GET", f"/repos/{repository}/compare/{base}...{quote(head, safe='')}").json()
        commits = [{"sha": c.get("sha"), "message": ((c.get("commit") or {}).get("message") or "").strip().splitlines()[0][:200] if c.get("commit") else ""}
                   for c in data.get("commits", [])]
        files = [{"path": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                 for f in data.get("files", [])][:50]
        return {"status": data.get("status"), "ahead_by": data.get("ahead_by", 0), "behind_by": data.get("behind_by", 0), "commits": commits[-10:], "files": files}

    def changed_files(self, repository, base, head):
        """The paths a comparison touches (both names of a rename), and whether that list is complete: GitHub lists at most
        300 files, and a base that is not an ancestor (a rewritten branch) says nothing reliable."""
        data = self.request("GET", f"/repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}").json()
        listed = data.get("files", [])
        files = sorted({name for f in listed for name in (f.get("filename"), f.get("previous_filename")) if name})
        return files, data.get("status") in ("ahead", "identical") and len(listed) < 300

    def schedule_health(self, app, workflow):
        """Whether GitHub still runs a scheduled workflow and when it last did; GitHub pauses schedules in idle repositories."""
        state = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}").json().get("state")
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs", params={"event": "schedule", "per_page": 1}).json()["workflow_runs"]
        last = runs[0] if runs else None
        return {"workflow": workflow, "state": state, "last_scheduled_run": last and last.get("created_at"), "last_conclusion": last and last.get("conclusion")}

    def cancel_run(self, app, run_id):
        """Ask GitHub to cancel a run; the recovery workflow handles whatever the runner had started."""
        self.request("POST", f"/repos/{pipeline_of(app)}/actions/runs/{int(run_id)}/cancel")
        return {"cancel_requested": True, "workflow_run_id": int(run_id)}

    def dispatch_workflow(self, app, workflow, inputs):
        """Start one of the application's workflows: its own, or the platform repository's for an application whose contract
        the platform keeps, which also learn the service they act for."""
        if central_of(app):
            inputs = {**inputs, "application": app.service}
        response = self.request("POST", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/dispatches",
                                json={"ref": "main" if central_of(app) else app.branch, "inputs": inputs})
        return response.json() if response.content else {}

    def artifact_json(self, app, run_id, name, filenames, limit=1_048_576):
        """Parsed JSON files from one named artifact of a run, or None when the run has no such artifact."""
        artifacts = self.request("GET", f"/repos/{pipeline_of(app)}/actions/runs/{int(run_id)}/artifacts", params={"per_page": 100}).json()["artifacts"]
        artifact = next((a for a in artifacts if a.get("name") == name and not a.get("expired")), None)
        if artifact is None:
            return None
        if artifact.get("size_in_bytes", 0) > limit:
            raise ValueError(f"Artifact {name} exceeds 1 MiB")
        response = self.client.request("GET", f"/repos/{pipeline_of(app)}/actions/artifacts/{int(artifact['id'])}/zip", headers=self.headers(self.token()))
        if response.status_code == 302:
            # The signed download URL belongs to another host; the GitHub credential is not forwarded.
            response = self.client.get(response.headers["location"])
        response.raise_for_status()
        found = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for filename in filenames:
                try:
                    info = archive.getinfo(filename)
                except KeyError:
                    continue
                if info.file_size > limit:
                    raise ValueError(f"{filename} exceeds 1 MiB")
                found[filename] = json.loads(archive.read(info))
        return found

    def release_receipt(self, app, run_id):
        """The deploy job's receipt and vulnerability summary for a finished run, or None when nothing was uploaded."""
        files = self.artifact_json(app, run_id, "release-receipt", ("release-receipt.json", "vulnerability-summary.json"))
        if not files:
            return None
        return {"release": files.get("release-receipt.json"), "vulnerabilities": files.get("vulnerability-summary.json")}

    def recovery_after(self, app, run, workflow="recover-gcp.yml", name="recovery-receipt"):
        """The newest recovery run started after a deploy run, with its receipt when uploaded; None while nothing has run yet.
        The platform repository's recovery uploads one receipt per service, named recovery-receipt-<service>."""
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs", params={"branch": "main", "per_page": 10}).json()["workflow_runs"]
        since = run.get("created_at") or ""
        candidates = [r for r in runs if (r.get("created_at") or "") >= since and r.get("id") != run.get("id")]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r.get("created_at") or "")
        result = {"run_id": latest.get("id"), "status": latest.get("status"), "conclusion": latest.get("conclusion"), "html_url": latest.get("html_url"),
                  "created_at": latest.get("created_at"), "receipt": None}
        if latest.get("status") == "completed":
            files = self.artifact_json(app, latest["id"], name, ("recovery-receipt.json",))
            result["receipt"] = (files or {}).get("recovery-receipt.json")
        return result

    def credential_status(self, now=None):
        """When the server-side credential expires, from GitHub's own response header; App tokens renew themselves."""
        response = self.request("GET", "/rate_limit")
        kind = "fine_grained_token" if os.getenv("GITHUB_TOKEN") else "app_installation"
        expires = parse_expiration(response.headers.get("github-authentication-token-expiration"))
        if expires is None:
            return {"kind": kind, "expires_at": None, "days_left": None, "warning": None}
        days = (expires - (now or datetime.datetime.now(datetime.UTC))).days
        warning = None
        if days < 0:
            warning = "The GitHub credential has expired; every GitHub call will fail until it is renewed."
        elif days < TOKEN_WARNING_DAYS:
            warning = f"The GitHub credential expires in {days} day(s); create a new one and update the controller host before then."
        return {"kind": kind, "expires_at": expires.isoformat(timespec="seconds"), "days_left": days, "warning": warning}

    def file(self, app, sha, filename):
        response = self.request("GET", f"/repos/{app.repository}/contents/{quote(filename, safe='/')}", params={"ref": sha})
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > 65_536:
            raise ValueError("Repository input exceeds 64 KiB")
        return base64.b64decode(data["content"], validate=False).decode("utf-8")

    def contents(self, repository, ref, path, limit=65_536):
        """A regular file from any repository the credential can read, at a ref; None when it does not exist. Lock files may be
        read up to GitHub's 1 MB contents limit (`limit`); every other input stays within 64 KiB."""
        try:
            response = self.request("GET", f"/repos/{repository}/contents/{quote(path, safe='/')}", params={"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > min(limit, 1_000_000):
            raise ValueError(f"Repository input exceeds {min(limit, 1_000_000) // 1024} KiB")
        return {"sha": data.get("sha"), "text": base64.b64decode(data["content"], validate=False).decode("utf-8")}

    # ---- onboarding: read a repository's shape, follow its pull requests and runs, prepare its environment
    def repository(self, repository):
        data = self.request("GET", f"/repos/{repository}").json()
        return {"repository_id": str(data["id"]), "owner_id": str(data["owner"]["id"]), "default_branch": data.get("default_branch"),
                "private": bool(data.get("private")), "url": data.get("html_url"), "full_name": data.get("full_name")}

    def tree(self, repository, ref, sizes=False):
        """Every path in the repository at a ref (files and directories), for the contract's checks; with `sizes`, a mapping
        of each file to its size in bytes as well, for the reader's refusal of files over 100 MiB."""
        data = self.request("GET", f"/repos/{repository}/git/trees/{quote(ref, safe='')}", params={"recursive": "1"}).json()
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") in ("blob", "tree")]
        if not sizes:
            return paths
        return paths, {item["path"]: int(item.get("size") or 0) for item in data.get("tree", []) if item.get("type") == "blob"}

    def pull_request(self, repository, number):
        data = self.request("GET", f"/repos/{repository}/pulls/{number}").json()
        return {"number": number, "state": data["state"], "merged": bool(data.get("merged")), "mergeable_state": data.get("mergeable_state"),
                "head_sha": data["head"]["sha"], "merge_commit_sha": data.get("merge_commit_sha"), "url": data["html_url"],
                "merged_by": (data.get("merged_by") or {}).get("login"), "head_ref": data["head"].get("ref"), "base_sha": data["base"].get("sha")}

    def open_pull_requests(self, repository, prefix):
        """The open pull requests whose branch starts with `prefix`: the controller's own, found by its branch names."""
        pulls = self.request("GET", f"/repos/{repository}/pulls", params={"state": "open", "per_page": 100}).json()
        return [{"number": item["number"], "url": item["html_url"], "branch": item["head"]["ref"], "head_sha": item["head"]["sha"]}
                for item in pulls if str(item["head"].get("ref", "")).startswith(prefix)]

    def close_pull_request(self, repository, number, comment):
        """Close a pull request with a comment saying why; it can be reopened, and its branch is kept."""
        self.request("POST", f"/repos/{repository}/issues/{int(number)}/comments", json={"body": comment})
        self.request("PATCH", f"/repos/{repository}/pulls/{int(number)}", json={"state": "closed"})
        return {"number": number, "closed": True}

    def pull_request_files(self, repository, number):
        return [item["filename"] for item in self.request("GET", f"/repos/{repository}/pulls/{number}/files", params={"per_page": 100}).json()]

    def merge_pull_request(self, repository, number, title):
        """Squash-merge; a refusal (checks pending, protection, conflicts) is reported, never forced."""
        try:
            data = self.request("PUT", f"/repos/{repository}/pulls/{number}/merge", json={"merge_method": "squash", "commit_title": title}).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (405, 409, 422):
                try:
                    reason = exc.response.json().get("message", "")
                except ValueError:
                    reason = exc.response.text[:200]
                return {"merged": False, "sha": None, "reason": reason}
            raise
        return {"merged": bool(data.get("merged")), "sha": data.get("sha"), "reason": data.get("message")}

    def issue_comments(self, repository, number):
        return [item.get("body", "") for item in self.request("GET", f"/repos/{repository}/issues/{number}/comments", params={"per_page": 100}).json()]

    def recent_runs(self, repository, workflow, limit=20):
        """The newest completed runs of a workflow, on any commit."""
        params = {"per_page": limit, "status": "completed"}
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs", params=params).json().get("workflow_runs", [])
        return [{"id": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "url": run["html_url"], "head_sha": run["head_sha"]} for run in runs]

    def job_conclusions(self, repository, run_id):
        """Each job of a run's latest attempt by name, with its conclusion."""
        jobs = self.request("GET", f"/repos/{repository}/actions/runs/{int(run_id)}/jobs", params={"per_page": 100}).json().get("jobs", [])
        return {job["name"]: job.get("conclusion") for job in jobs}

    def contains(self, repository, ancestor, ref):
        """Whether `ref` already contains commit `ancestor` (the same commit, or one after it)."""
        data = self.request("GET", f"/repos/{repository}/compare/{quote(ancestor, safe='')}...{quote(ref, safe='')}").json()
        return data.get("status") in ("ahead", "identical")

    def workflow_runs(self, repository, workflow, head_sha=None, limit=5):
        params = {"per_page": limit}
        if head_sha:
            params["head_sha"] = head_sha
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs", params=params).json().get("workflow_runs", [])
        return [{"id": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "url": run["html_url"], "head_sha": run["head_sha"]} for run in runs]

    def ensure_environment(self, repository, name, branch):
        """The deployment environment the workflows run in, restricted to one branch; idempotent."""
        path = f"/repos/{repository}/environments/{quote(name, safe='')}"
        self.request("PUT", path, json={"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
        try:
            self.request("POST", path + "/deployment-branch-policies", json={"name": branch, "type": "branch"})
        except httpx.HTTPStatusError as exc:
            # The policy for this branch already exists: GitHub answers 303 See Other (pointing at it), and once answered 422.
            # A repository onboarded again (Sophia's copy from backend/, 2026-10-16) meets it.
            if exc.response.status_code not in (303, 422):
                raise

    def set_environment_variables(self, repository, name, variables):
        path = f"/repos/{repository}/environments/{quote(name, safe='')}/variables"
        for key, value in variables.items():
            try:
                self.request("POST", path, json={"name": key, "value": value})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                self.request("PATCH", f"{path}/{key}", json={"name": key, "value": value})

    def repository_ids(self, repository):
        """The immutable numeric ids GitHub's OIDC tokens carry; Workload Identity Federation trusts these, not names."""
        data = self.request("GET", f"/repos/{repository}").json()
        return {"repository_id": str(data["id"]), "owner_id": str(data["owner"]["id"])}

    def open_platform_pull_request(self, repository, base_branch, branch, files, title, body, message):
        """A pull request on the platform repository itself (infrastructure roots), with the same no-merge rules."""
        return self.open_pull_request(types.SimpleNamespace(repository=repository, branch=base_branch), branch, files, title, body, message)

    def branch_head(self, repository, branch="main"):
        return self.request("GET", f"/repos/{repository}/commits/{quote(branch, safe='')}").json()["sha"]

    def open_pull_request(self, app, branch, files, title, body, message, draft=False, create_only=False):
        """Open one pull request with the given files on a branch; never merges, never force-pushes, reuses an open one.
        `create_only`: a file that already exists on the branch is left alone (and named), never overwritten."""
        owner = app.repository.split("/")[0]
        open_prs = self.request("GET", f"/repos/{app.repository}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}).json()
        if open_prs:
            return {"number": open_prs[0]["number"], "url": open_prs[0]["html_url"], "branch": branch, "changed": [],
                    "note": "A pull request on this branch is already open and was left as it is (it may hold a person's edits, and it may predate "
                            "this request); close it to have the platform open a fresh one."}
        base = self.head(app)
        if not self.fresh_branch(app.repository, branch, base):
            # The branch is left over from an earlier pull request (after a squash merge its history no longer matches the
            # base): a pull request from it would conflict, and GitHub runs no workflow for a conflicting one. Use a new
            # branch named after the base commit; nothing is deleted or force-pushed.
            branch = f"{branch}-{base[:7]}"
            if not self.fresh_branch(app.repository, branch, base):
                raise ValueError(f"Branches {branch} already exist and differ from {app.branch}; delete the old ones and try again")
        changed, kept = [], []
        for path, text in files.items():
            current = self.contents(app.repository, branch, path)
            if current is not None and current["text"] == text:
                continue
            if current is not None and create_only:
                kept.append(path)
                continue
            payload = {"message": message, "branch": branch, "content": base64.b64encode(text.encode()).decode()}
            if current is not None:
                payload["sha"] = current["sha"]
            self.request("PUT", f"/repos/{app.repository}/contents/{quote(path, safe='/')}", json=payload)
            changed.append(path)
        if not changed:
            return {"number": None, "url": None, "branch": branch, "changed": [], "kept": kept, "note": "The files already have this content."}
        response = self.request("POST", f"/repos/{app.repository}/pulls", json={"title": title, "base": app.branch, "head": branch, "draft": draft, "body": body})
        result = {"number": response.json()["number"], "url": response.json()["html_url"], "branch": branch, "changed": changed}
        if kept:
            result["kept"] = kept
        return result

    def fresh_branch(self, repository, branch, base):
        """Create the branch at `base`, or accept an existing one only when it is exactly `base`; False when it has drifted."""
        try:
            self.request("POST", f"/repos/{repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base})
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:  # 422: the branch already exists
                raise
        return self.compare_repository(repository, base, branch)["status"] == "identical"

    def update_workflows(self, app, platform_sha, files):
        """Open the pull request that pins the app's workflow files to a platform commit; never merges, never force-pushes."""
        body = (f"Regenerated from the platform templates at commit `{platform_sha}`.\n\nFiles: " + ", ".join(f"`{p}`" for p in files)
                + "\n\nReview and merge through the normal process; the controller refuses to deploy an app whose pin it does not support. "
                "Nothing is merged or deployed by this pull request itself.")
        result = self.open_pull_request(app, f"lab-deploy/platform-{platform_sha[:12]}{getattr(app, 'suffix', '')}", files,
                                        f"Update Fast Deploy workflows to platform {platform_sha[:12]}" + (f" ({app.service})" if getattr(app, "suffix", "") else ""), body,
                                        f"Pin Fast Deploy workflows to platform {platform_sha[:12]}")
        if result.get("note") == "The files already have this content.":
            result["note"] = "The workflows already match this platform commit."
        elif result.get("note"):
            result["note"] = "A pull request for this platform commit is already open."
        return result

    def dispatch(self, app, plan):
        inputs = {"commit_sha": plan["sha"], "spec_hash": plan["spec_hash"], "plan_id": plan["id"], "target_json": json.dumps(plan["target"], sort_keys=True)}
        if plan.get("reuse_image"):
            inputs["image_digest"] = plan["reuse_image"]
        if plan.get("provenance") == "unavailable":
            # Sent only for the exception: a workflow pinned before 1.8.6 has no such input and GitHub refuses unknown ones.
            inputs["provenance"] = "unavailable"
        if plan.get("bake_seconds", DEFAULT_BAKE_SECONDS) != DEFAULT_BAKE_SECONDS:
            # Sent only when the operator changed it, for the same reason: a workflow pinned before 1.9.0 has no such input.
            inputs["bake_seconds"] = str(int(plan["bake_seconds"]))
        if central_of(app):
            # The platform repository's pipeline (docs/ROADMAP.md D33): which application, where its source is, and the
            # platform commit whose apps/<id>/platform.yaml the plan was made from.
            inputs.update(application=app.service, repository=app.repository, app_path=app.path, contract_commit=(plan.get("contract") or {})["commit"])
        response = self.request("POST", f"/repos/{pipeline_of(app)}/actions/workflows/{app.workflow}/dispatches", json={"ref": "main" if central_of(app) else app.branch, "inputs": inputs})
        # Current API returns 200 with run ID; older deployments may still return 204.
        return response.json() if response.content else {}

    def status(self, app, plan):
        result = plan.get("dispatch_result", {})
        run_id = result.get("workflow_run_id")
        rollback = plan.get("kind") == "rollback"
        title = f"rollback-{plan['from_plan']}" if rollback else f"deploy-{plan['id']}"
        workflow = app.workflows["recover"] if rollback else app.workflow
        if run_id:
            run = self.request("GET", f"/repos/{pipeline_of(app)}/actions/runs/{int(run_id)}").json()
        else:
            runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs", params={"event": "workflow_dispatch", "per_page": 100}).json()["workflow_runs"]
            run = next((r for r in runs if r.get("display_title") == title), None)
        if run is None:
            return {"status": "not_visible_yet", "note": "Do not redispatch; inspect GitHub Actions if this persists."}
        # The unique plan id in the run title is the identity. The head SHA is the branch tip the workflow ran on, which
        # only equals the plan's commit for a plan of the tip itself: rollbacks and plans pinned to older commits differ.
        # A platform-pipeline run's head is the platform's main, never the application's commit.
        tip_only = not rollback and not plan.get("pinned_commit") and not central_of(app)
        if (tip_only and run.get("head_sha") != plan["sha"]) or run.get("display_title") != title:
            # Observed live: for a second or two after dispatch the run exists but its title and commit are not
            # populated yet. Only a run that has started or finished must prove its identity.
            if run_id and run.get("status") in {None, "", "requested", "queued", "pending", "waiting"}:
                return {"status": "not_visible_yet", "note": "GitHub is still creating the run; its identity is checked once it starts."}
            raise ValueError("Workflow identity does not match this deployment plan")
        jobs = []
        page = 1
        while True:
            batch = self.request("GET", f"/repos/{pipeline_of(app)}/actions/runs/{int(run['id'])}/attempts/{int(run.get('run_attempt', 1))}/jobs",
                                 params={"per_page": 100, "page": page}).json()["jobs"]
            jobs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return {**{key: run.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt", "created_at", "run_started_at", "updated_at")}, "jobs": jobs}

    def drift_report(self, repository, workflow="infra-drift.yml", name="drift-report"):
        """The newest report of the platform repository's weekly drift check, or None when it has not produced one yet."""
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs",
                            params={"branch": "main", "status": "completed", "per_page": 5}).json()["workflow_runs"]
        holder = types.SimpleNamespace(repository=repository)
        for run in runs:
            files = self.artifact_json(holder, run["id"], name, ("drift-report.json",))
            if files and "drift-report.json" in files:
                return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "conclusion": run.get("conclusion"),
                        "created_at": run.get("created_at"), "report": files["drift-report.json"]}
        return None

    def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        """The newest cost-report artifact from a successful rightsizing run, or an explicit 'none yet'."""
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs",
                            params={"status": "success", "branch": "main", "per_page": 5}).json()["workflow_runs"]
        for run in runs:
            files = self.artifact_json(app, run["id"], name, ("cost-report.json",), limit)
            if files and "cost-report.json" in files:
                return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "generated_at": run.get("updated_at"), "report": files["cost-report.json"]}
        return {"report": None, "note": "No successful rightsizing run with a cost-report artifact yet; the workflow runs weekly or on manual dispatch."}

