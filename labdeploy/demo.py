"""Local demonstration only: no GitHub credential, no cloud project, no dispatch.

``python -m labdeploy.demo serve`` starts the real controller and MCP server on
127.0.0.1, but the application is read from a directory on this machine instead
of GitHub. Every tool can be exercised end to end (list, analyze, plan, deploy,
status, refusals). Workflow dispatch is permanently disabled here and the local
repository adapter refuses every write, so nothing can reach GitHub or a cloud.

The production entry point, ``labdeploy.server:create_app``, never imports this
module. The running service does not substitute local or fake GitHub data.
"""
import argparse
import hashlib
import itertools
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import httpx

from .analyzer import FILES, local_paths
from .config import Config


def demo_target(service="lab-demo"):
    return {
        "project_id": "demo-staging-project", "region": "us-central1", "service": service, "registry": service,
        "deploy_service_account": f"{service}-deploy@demo-staging-project.iam.gserviceaccount.com",
        "probe_service_account": f"{service}-probe@demo-staging-project.iam.gserviceaccount.com",
        "wif_provider": f"projects/000000000000/locations/global/workloadIdentityPools/{service}-gh/providers/github",
        "recovery_bucket": f"demo-staging-project-{service}-recovery",
    }


DEMO_TARGET = demo_target()


def service_name(root):
    """The service name the application's own platform.yaml declares, so any application directory works."""
    import yaml

    spec = Path(root) / "platform.yaml"
    if spec.is_file():
        name = (yaml.safe_load(spec.read_text()) or {}).get("name")
        if isinstance(name, str) and name:
            return name
    return "lab-demo"


class LocalRepository:
    """Read-only stand-in for the GitHub adapter.

    A content hash of the manifest files replaces the commit SHA, so editing the
    application on disk invalidates existing plans exactly as a new commit would.
    """

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def head(self, app):
        digest = hashlib.sha256()
        for name in FILES:
            path = self.root / name
            if path.is_file() and not path.is_symlink():
                digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
        return digest.hexdigest()[:40]

    def commit(self, app, ref=None):
        return {"sha": self.head(app), "message": "Local working copy (demo mode)", "author": None, "date": None, "url": None}

    def tree(self, repository, ref):
        return local_paths(self.root)

    def file(self, app, sha, filename):
        if sha != self.head(app):
            raise ValueError("Demo application changed on disk; create a new plan")
        path = self.root / filename
        if path.is_symlink() or not path.is_file():
            # The controller treats a 404 from GitHub as "file absent"; mirror that contract.
            request = httpx.Request("GET", f"http://demo.invalid/{filename}")
            raise httpx.HTTPStatusError("File not found in demo repository", request=request, response=httpx.Response(404, request=request))
        if path.stat().st_size > 65_536:
            raise ValueError(f"{filename} exceeds 64 KiB")
        return path.read_text()

    def dispatch(self, app, plan):
        raise ValueError("Demo mode never dispatches a workflow")

    def contents(self, repository, ref, path):
        """A file of the local repository, as GitHub's contents API gives it; None when it does not exist."""
        try:
            return {"sha": "local", "text": self.file(None, ref, path)}
        except httpx.HTTPStatusError:
            return None

    def open_pull_request(self, app, branch, files, title, body, message, draft=False):
        raise ValueError("Demo mode never creates a pull request")

    def status(self, app, plan):
        raise ValueError("Demo mode has no workflow runs; plans stay planned")

    def cost_report(self, app, workflow="rightsize-gcp.yml"):
        raise ValueError("Demo mode has no telemetry; the rightsizing workflow produces measured cost reports in a real project")


def demo_config(database, token_sha256, service="lab-demo"):
    return Config.model_validate({
        "live_dispatch": False,
        "database": database,
        "allowed_hosts": ["127.0.0.1:*", "localhost:*", "testserver"],
        "applications": {"demo": {"repository": f"local/{service}", "service": service, "target": demo_target(service)}},
        "principals": [{"name": "demo-operator", "token_sha256": token_sha256, "applications": ["demo"], "can_deploy": True}],
    })


def serve(args):
    import uvicorn

    from .server import build_app

    repository = LocalRepository(args.path)
    token = secrets.token_urlsafe(32)
    database = args.database or str(Path(tempfile.mkdtemp(prefix="lab-deploy-demo-")) / "demo.db")
    service = service_name(repository.root)
    app = build_app(demo_config(database, hashlib.sha256(token.encode()).hexdigest(), service), repository)
    url = f"http://127.0.0.1:{args.port}"
    line = "=" * 76
    print(f"""{line}
 Fast Deploy DEMO MODE: local files, no GitHub, no cloud, dispatch disabled
{line}
 Application 'demo' ({service}) is read from: {repository.root}
 Plans database:                  {database}

 Demo operator token (valid only while this process runs). In another terminal:
   export LAB_DEMO_TOKEN={token}

 Walk through every tool:
   python -m labdeploy.demo walkthrough --port {args.port}

 Or let Claude Code drive the tools itself:
   claude mcp add --transport http lab-deploy {url}/mcp --header "Authorization: Bearer $LAB_DEMO_TOKEN"

 Press Ctrl+C to stop.
{line}""", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def walkthrough(args):
    token = os.environ.get("LAB_DEMO_TOKEN")
    if not token:
        sys.exit("Export LAB_DEMO_TOKEN from the demo server banner first")
    sys.stdout.reconfigure(line_buffering=True)  # keep step output ordered when piped to a file
    client = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    ids = itertools.count(1)
    steps = itertools.count(1)

    def step(title, explanation):
        print(f"\n{'=' * 76}\n STEP {next(steps)}: {title}\n {explanation}\n{'-' * 76}")

    def show(payload):
        try:
            print(json.dumps(json.loads(payload) if isinstance(payload, str) else payload, indent=2))
        except ValueError:
            print(payload)

    def rpc(method, params=None):
        body = {"jsonrpc": "2.0", "id": next(ids), "method": method}
        if params is not None:
            body["params"] = params
        response = client.post("/mcp", headers=headers, json=body)
        response.raise_for_status()
        return response.json()["result"]

    def tool(name, **arguments):
        result = rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(item.get("text", "") for item in result.get("content", []))
        return bool(result.get("isError")), text

    try:
        step("Public health check", "Only this endpoint answers without a token.")
        show(client.get("/healthz").json())

        step("A request without a token is refused", "Every other endpoint needs an operator token.")
        response = client.get("/applications")
        print(f"HTTP {response.status_code}: {response.text}")

        step("MCP handshake", "This is how Claude connects. The server also sends usage instructions for the model.")
        init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "walkthrough", "version": "1"}})
        print("server:", init["serverInfo"]["name"])
        print("instructions:", init.get("instructions", ""))

        step("The tools Claude is allowed to use", "There is no shell, no arbitrary repository, and no production tool. "
                                                   "Each tool declares whether it is read-only or changes something, so the client can ask first.")
        for item in rpc("tools/list")["tools"]:
            hints = item.get("annotations") or {}
            kind = "read-only" if hints.get("readOnlyHint") else ("changes cloud resources" if hints.get("destructiveHint") else "creates a record")
            print(f" - {item['name']} [{kind}]: {item['description']}")

        step("list_applications", "Only applications registered by the operator for this identity are visible.")
        show(tool("list_applications")[1])

        step("analyze_repository", "Reads manifests only, never runs code, and lists what a human must still confirm.")
        show(tool("analyze_repository", application="demo")[1])

        step("create_deployment_plan", "Validates the committed platform.yaml and pins a plan to one commit and one cloud target.")
        failed, text = tool("create_deployment_plan", application="demo")
        show(text)
        if failed:
            sys.exit(f"\nThe plan was refused, so the walkthrough stops here: {text}")
        plan_id = json.loads(text)["id"]

        step("deploy_staging", "In demo mode dispatch is disabled, so the plan stays planned and nothing leaves this machine.")
        show(tool("deploy_staging", plan_id=plan_id)[1])

        step("get_deployment_status", "Reports only what is actually known, with the audit trail of recorded events.")
        show(tool("get_deployment_status", plan_id=plan_id)[1])

        step("list_plans", "Each operator sees only their own plans.")
        show(tool("list_plans")[1])

        step("get_controller_status", "One answer for the operator: version, switches, registered apps, GitHub credential expiry and pricing freshness.")
        _, text = tool("get_controller_status")
        show(text)
        step("estimate_cost", "Cost bounds from Google's list prices and the reviewed configuration: a floor with zero traffic, "
                              "and a worst case if every allowed instance were busy all month.")
        show(tool("estimate_cost", application="demo")[1])

        step("estimate_cost with a traffic scenario", "100,000 requests per month averaging 0.2 seconds, shown as a best/worst range.")
        show(tool("estimate_cost", application="demo", requests_per_month=100000, average_request_seconds=0.2)[1])

        step("get_cost_report", "Measured right-sizing needs real telemetry, so demo mode says so instead of inventing numbers.")
        failed, text = tool("get_cost_report", application="demo")
        print(f"refused={failed}: {text}")

        step("Policy refusal: another cloud", "Unsupported targets are refused instead of being quietly converted.")
        failed, text = tool("create_deployment_plan", application="demo", cloud="aws")
        print(f"refused={failed}: {text}")

        step("Policy refusal: unknown plan", "A plan id that does not belong to this identity cannot be deployed.")
        failed, text = tool("deploy_staging", plan_id="0" * 32)
        print(f"refused={failed}: {text}")

        print(f"\n{'=' * 76}\n Demo complete. A real release additionally needs a GitHub repository, a GCP staging\n"
              f" project bootstrapped with infra/gcp, and live_dispatch enabled; see docs/SETUP.md.\n{'=' * 76}")
    except httpx.ConnectError:
        sys.exit(f"Nothing is listening on port {args.port}. Start the server first: python -m labdeploy.demo serve")
    except httpx.HTTPStatusError as exc:
        sys.exit(f"HTTP {exc.response.status_code}: {exc.response.text}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.demo", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("serve", help="start the demo controller on 127.0.0.1")
    s.add_argument("--path", type=Path, default=Path("examples/demo-app"), help="application directory (default: examples/demo-app)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--database", help="SQLite file for plans (default: a fresh temporary file)")
    w = commands.add_parser("walkthrough", help="exercise every tool against a running demo server")
    w.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    walkthrough(args) if args.command == "walkthrough" else serve(args)


if __name__ == "__main__":
    main()
