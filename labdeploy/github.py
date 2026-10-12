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

TOKEN_WARNING_DAYS = 14


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
            return f"GitHub refused access{where}: the credential lacks a required permission (Contents read and Actions write for releases)."
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

    def schedule_health(self, app, workflow):
        """Whether GitHub still runs a scheduled workflow and when it last did; GitHub pauses schedules in idle repositories."""
        state = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}").json().get("state")
        runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}/runs", params={"event": "schedule", "per_page": 1}).json()["workflow_runs"]
        last = runs[0] if runs else None
        return {"workflow": workflow, "state": state, "last_scheduled_run": last and last.get("created_at"), "last_conclusion": last and last.get("conclusion")}

    def cancel_run(self, app, run_id):
        """Ask GitHub to cancel a run; the recovery workflow handles whatever the runner had started."""
        self.request("POST", f"/repos/{app.repository}/actions/runs/{int(run_id)}/cancel")
        return {"cancel_requested": True, "workflow_run_id": int(run_id)}

    def dispatch_workflow(self, app, workflow, inputs):
        response = self.request("POST", f"/repos/{app.repository}/actions/workflows/{workflow}/dispatches", json={"ref": app.branch, "inputs": inputs})
        return response.json() if response.content else {}

    def artifact_json(self, app, run_id, name, filenames, limit=1_048_576):
        """Parsed JSON files from one named artifact of a run, or None when the run has no such artifact."""
        artifacts = self.request("GET", f"/repos/{app.repository}/actions/runs/{int(run_id)}/artifacts", params={"per_page": 100}).json()["artifacts"]
        artifact = next((a for a in artifacts if a.get("name") == name and not a.get("expired")), None)
        if artifact is None:
            return None
        if artifact.get("size_in_bytes", 0) > limit:
            raise ValueError(f"Artifact {name} exceeds 1 MiB")
        response = self.client.request("GET", f"/repos/{app.repository}/actions/artifacts/{int(artifact['id'])}/zip", headers=self.headers(self.token()))
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

    def recovery_after(self, app, run, workflow="recover-gcp.yml"):
        """The newest recovery run started after a deploy run, with its receipt when uploaded; None while nothing has run yet."""
        runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}/runs", params={"branch": app.branch, "per_page": 10}).json()["workflow_runs"]
        since = run.get("created_at") or ""
        candidates = [r for r in runs if (r.get("created_at") or "") >= since and r.get("id") != run.get("id")]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r.get("created_at") or "")
        result = {"run_id": latest.get("id"), "status": latest.get("status"), "conclusion": latest.get("conclusion"), "html_url": latest.get("html_url"),
                  "created_at": latest.get("created_at"), "receipt": None}
        if latest.get("status") == "completed":
            files = self.artifact_json(app, latest["id"], "recovery-receipt", ("recovery-receipt.json",))
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

    def contents(self, repository, ref, path):
        """A regular file from any repository the credential can read, at a ref; None when it does not exist."""
        try:
            response = self.request("GET", f"/repos/{repository}/contents/{quote(path, safe='/')}", params={"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > 65_536:
            raise ValueError("Repository input exceeds 64 KiB")
        return {"sha": data.get("sha"), "text": base64.b64decode(data["content"], validate=False).decode("utf-8")}

    def repository_ids(self, repository):
        """The immutable numeric ids GitHub's OIDC tokens carry; Workload Identity Federation trusts these, not names."""
        data = self.request("GET", f"/repos/{repository}").json()
        return {"repository_id": str(data["id"]), "owner_id": str(data["owner"]["id"])}

    def open_platform_pull_request(self, repository, base_branch, branch, files, title, body, message):
        """A pull request on the platform repository itself (infrastructure roots), with the same no-merge rules."""
        return self.open_pull_request(types.SimpleNamespace(repository=repository, branch=base_branch), branch, files, title, body, message)

    def branch_head(self, repository, branch="main"):
        return self.request("GET", f"/repos/{repository}/commits/{quote(branch, safe='')}").json()["sha"]

    def open_pull_request(self, app, branch, files, title, body, message):
        """Open one pull request with the given files on a branch; never merges, never force-pushes, reuses an open one."""
        owner = app.repository.split("/")[0]
        open_prs = self.request("GET", f"/repos/{app.repository}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}).json()
        if open_prs:
            return {"number": open_prs[0]["number"], "url": open_prs[0]["html_url"], "branch": branch, "changed": [], "note": "A pull request on this branch is already open."}
        try:
            self.request("POST", f"/repos/{app.repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": self.head(app)})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:  # 422: the branch already exists from an earlier, closed attempt; reuse it
                raise
        changed = []
        for path, text in files.items():
            current = self.contents(app.repository, branch, path)
            if current is not None and current["text"] == text:
                continue
            payload = {"message": message, "branch": branch, "content": base64.b64encode(text.encode()).decode()}
            if current is not None:
                payload["sha"] = current["sha"]
            self.request("PUT", f"/repos/{app.repository}/contents/{quote(path, safe='/')}", json=payload)
            changed.append(path)
        if not changed:
            return {"number": None, "url": None, "branch": branch, "changed": [], "note": "The files already have this content."}
        response = self.request("POST", f"/repos/{app.repository}/pulls", json={"title": title, "base": app.branch, "head": branch, "draft": False, "body": body})
        return {"number": response.json()["number"], "url": response.json()["html_url"], "branch": branch, "changed": changed}

    def update_workflows(self, app, platform_sha, files):
        """Open the pull request that pins the app's workflow files to a platform commit; never merges, never force-pushes."""
        body = (f"Regenerated from the platform templates at commit `{platform_sha}`.\n\nFiles: " + ", ".join(f"`{p}`" for p in files)
                + "\n\nReview and merge through the normal process; the controller refuses to deploy an app whose pin it does not support. "
                "Nothing is merged or deployed by this pull request itself.")
        result = self.open_pull_request(app, f"lab-deploy/platform-{platform_sha[:12]}", files, f"Update Lab Deploy workflows to platform {platform_sha[:12]}", body,
                                        f"Pin Lab Deploy workflows to platform {platform_sha[:12]}")
        if result.get("note") == "The files already have this content.":
            result["note"] = "The workflows already match this platform commit."
        elif result.get("note"):
            result["note"] = "A pull request for this platform commit is already open."
        return result

    def dispatch(self, app, plan):
        inputs = {"commit_sha": plan["sha"], "spec_hash": plan["spec_hash"], "plan_id": plan["id"], "target_json": json.dumps(plan["target"], sort_keys=True)}
        if plan.get("reuse_image"):
            inputs["image_digest"] = plan["reuse_image"]
        response = self.request("POST", f"/repos/{app.repository}/actions/workflows/{app.workflow}/dispatches", json={"ref": app.branch, "inputs": inputs})
        # Current API returns 200 with run ID; older deployments may still return 204.
        return response.json() if response.content else {}

    def propose_config(self, app, sha, draft, assumptions):
        import uuid

        import yaml
        branch = "lab-deploy/onboard-" + uuid.uuid4().hex[:12]
        self.request("POST", f"/repos/{app.repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
        content = yaml.safe_dump(draft, sort_keys=False)
        self.request("PUT", f"/repos/{app.repository}/contents/platform.yaml", json={
            "message": "Add deployment specification for review", "branch": branch,
            "content": base64.b64encode(content.encode()).decode(),
        })
        response = self.request("POST", f"/repos/{app.repository}/pulls", json={
            "title": "Review generated deployment specification", "base": app.branch, "head": branch, "draft": True,
            "body": ("Generated from commit `" + sha + "`. Configuration is blocked from deployment until reviewed.\n\n"
                     + "\n".join("- " + x for x in assumptions)
                     + "\n\nAfter review, set `reviewed: true` and merge through normal branch protection. "
                     "No merge or cloud deployment is performed by this tool."),
        })
        return {"number": response.json()["number"], "url": response.json()["html_url"], "branch": branch}

    def status(self, app, plan):
        result = plan.get("dispatch_result", {})
        run_id = result.get("workflow_run_id")
        rollback = plan.get("kind") == "rollback"
        title = f"rollback-{plan['from_plan']}" if rollback else f"deploy-{plan['id']}"
        workflow = "recover-gcp.yml" if rollback else app.workflow
        if run_id:
            run = self.request("GET", f"/repos/{app.repository}/actions/runs/{int(run_id)}").json()
        else:
            runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}/runs", params={"event": "workflow_dispatch", "per_page": 100}).json()["workflow_runs"]
            run = next((r for r in runs if r.get("display_title") == title), None)
        if run is None:
            return {"status": "not_visible_yet", "note": "Do not redispatch; inspect GitHub Actions if this persists."}
        # The unique plan id in the run title is the identity. The head SHA is the branch tip the workflow ran on, which
        # only equals the plan's commit for a plan of the tip itself: rollbacks and plans pinned to older commits differ.
        tip_only = not rollback and not plan.get("pinned_commit")
        if (tip_only and run.get("head_sha") != plan["sha"]) or run.get("display_title") != title:
            # Observed live: for a second or two after dispatch the run exists but its title and commit are not
            # populated yet. Only a run that has started or finished must prove its identity.
            if run_id and run.get("status") in {None, "", "requested", "queued", "pending", "waiting"}:
                return {"status": "not_visible_yet", "note": "GitHub is still creating the run; its identity is checked once it starts."}
            raise ValueError("Workflow identity does not match this deployment plan")
        jobs = []
        page = 1
        while True:
            batch = self.request("GET", f"/repos/{app.repository}/actions/runs/{int(run['id'])}/attempts/{int(run.get('run_attempt', 1))}/jobs",
                                 params={"per_page": 100, "page": page}).json()["jobs"]
            jobs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return {**{key: run.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt", "created_at", "run_started_at", "updated_at")}, "jobs": jobs}

    def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        """The newest cost-report artifact from a successful rightsizing run, or an explicit 'none yet'."""
        runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}/runs",
                            params={"status": "success", "branch": app.branch, "per_page": 5}).json()["workflow_runs"]
        for run in runs:
            files = self.artifact_json(app, run["id"], name, ("cost-report.json",), limit)
            if files and "cost-report.json" in files:
                return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "generated_at": run.get("updated_at"), "report": files["cost-report.json"]}
        return {"report": None, "note": "No successful rightsizing run with a cost-report artifact yet; the workflow runs weekly or on manual dispatch."}

