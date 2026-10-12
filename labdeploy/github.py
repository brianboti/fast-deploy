"""GitHub App credentials stay server-side. No cloud credentials in this process."""
import base64
import io
import json
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt


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
        return self.request("GET", f"/repos/{app.repository}/commits/{quote(app.branch, safe='')}").json()["sha"]

    def file(self, app, sha, filename):
        response = self.request("GET", f"/repos/{app.repository}/contents/{quote(filename, safe='/')}", params={"ref": sha})
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > 65_536:
            raise ValueError("Repository input exceeds 64 KiB")
        return base64.b64decode(data["content"], validate=False).decode("utf-8")

    def dispatch(self, app, plan):
        response = self.request("POST", f"/repos/{app.repository}/actions/workflows/{app.workflow}/dispatches", json={
            "ref": app.branch,
            "inputs": {"commit_sha": plan["sha"], "spec_hash": plan["spec_hash"], "plan_id": plan["id"],
                       "target_json": json.dumps(plan["target"], sort_keys=True)},
        })
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
        if run_id:
            run = self.request("GET", f"/repos/{app.repository}/actions/runs/{int(run_id)}").json()
        else:
            runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{app.workflow}/runs", params={"event": "workflow_dispatch", "per_page": 100}).json()["workflow_runs"]
            run = next((r for r in runs if r.get("display_title") == f"deploy-{plan['id']}"), None)
        if run is None:
            return {"status": "not_visible_yet", "note": "Do not redispatch; inspect GitHub Actions if this persists."}
        if run.get("head_sha") != plan["sha"] or run.get("display_title") != f"deploy-{plan['id']}":
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
        return {**{key: run.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt")}, "jobs": jobs}

    def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        """The newest cost-report artifact from a successful rightsizing run, or an explicit 'none yet'."""
        runs = self.request("GET", f"/repos/{app.repository}/actions/workflows/{workflow}/runs",
                            params={"status": "success", "branch": app.branch, "per_page": 5}).json()["workflow_runs"]
        for run in runs:
            artifacts = self.request("GET", f"/repos/{app.repository}/actions/runs/{int(run['id'])}/artifacts", params={"per_page": 100}).json()["artifacts"]
            artifact = next((a for a in artifacts if a.get("name") == name and not a.get("expired")), None)
            if artifact is None:
                continue
            if artifact.get("size_in_bytes", 0) > limit:
                raise ValueError("Cost report artifact exceeds 1 MiB")
            response = self.client.request("GET", f"/repos/{app.repository}/actions/artifacts/{int(artifact['id'])}/zip", headers=self.headers(self.token()))
            if response.status_code == 302:
                # The signed download URL belongs to another host; the GitHub credential is not forwarded.
                response = self.client.get(response.headers["location"])
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                info = archive.getinfo("cost-report.json")
                if info.file_size > limit:
                    raise ValueError("Cost report exceeds 1 MiB")
                report = json.loads(archive.read(info))
            return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "generated_at": run.get("updated_at"), "report": report}
        return {"report": None, "note": "No successful rightsizing run with a cost-report artifact yet; the workflow runs weekly or on manual dispatch."}

