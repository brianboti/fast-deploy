import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

import yaml

from .analyzer import analyze_local
from .models import AppSpec, enforce, parse_spec, spec_hash


def build_parser():
    parser = argparse.ArgumentParser(description="Lab Deploy: generate, validate and plan before deploying")
    commands = parser.add_subparsers(dest="command", required=True)
    a = commands.add_parser("analyze")
    a.add_argument("path", type=Path)
    a.add_argument("--name", required=True)
    a.add_argument("--output", type=Path)
    v = commands.add_parser("validate")
    v.add_argument("path", type=Path)
    c = commands.add_parser("cost", help="cost bounds and findings for a platform.yaml")
    c.add_argument("path", type=Path)
    c.add_argument("--requests", type=int, help="expected requests per month for a scenario estimate")
    c.add_argument("--average-seconds", type=float, help="average request duration in seconds for the scenario")
    commands.add_parser("schema")
    commands.add_parser("token")
    w = commands.add_parser("render-workflow")
    w.add_argument("--platform-repo", required=True)
    w.add_argument("--platform-sha", required=True)
    w.add_argument("--template", type=Path, default=Path("templates/deploy-gcp.yml"))
    w.add_argument("--output", type=Path, required=True)
    s = commands.add_parser("sync-pins", help="copy the action commit pins from the platform CI workflow into the templates")
    s.add_argument("--ci", type=Path, default=Path(".github/workflows/ci.yml"))
    s.add_argument("--templates", type=Path, default=Path("templates"))
    se = commands.add_parser("sessions", help="list or end Claude sign-in sessions (OAuth token families) in the controller database")
    se.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    se.add_argument("--revoke", action="append", default=[], metavar="SESSION", help="end one session by its id (repeatable)")
    se.add_argument("--revoke-all", action="store_true", help="end every session; Claude must sign in again")
    for name, text in (("watchdog", "run the daily watchdog once and print its findings"), ("backup", "write one backup of the controller database now")):
        job = commands.add_parser(name, help=text)
        job.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    d = commands.add_parser("decommission", help="print the checklist that retires one application cleanly")
    d.add_argument("app")
    d.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    u = commands.add_parser("update-workflows", help="open pull requests that pin registered apps' workflows to a platform commit")
    u.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    u.add_argument("--all", action="store_true", help="every registered application")
    u.add_argument("--app", action="append", default=[], help="one application id (repeatable)")
    u.add_argument("--platform-commit", default=None, help="full platform commit SHA (default: platform.commit, else the platform branch tip)")
    return parser


def decommission_steps(app_id, app):
    """The order that retires an app without leaving a bill or a dangling trust: drain, delete, then let Terraform forget it."""
    t = app.target
    return [
        f"Pause it: add `paused: true` under applications.{app_id} in the controller configuration (applies without a restart).",
        f"Tell its owners; a rollback target no longer exists once the service is gone. Last chance for `gcloud run services describe {t.service} --region {t.region} --project {t.project_id}`.",
        f"Delete the service: gcloud run services delete {t.service} --region {t.region} --project {t.project_id}",
        f"Empty and delete the recovery bucket: gcloud storage rm -r gs://{t.recovery_bucket}/** && gcloud storage buckets delete gs://{t.recovery_bucket}",
        f"Delete the images: gcloud artifacts repositories delete {t.registry} --location {t.region} --project {t.project_id}",
        f"Delete its secrets, if any: gcloud secrets list --filter='name:{t.service}-' --project {t.project_id}",
        f"Remove the \"{t.service}\" entry from the services map in infra/gcp/terraform.tfvars and apply; Terraform drops the identities, trust and alerts it still owns.",
        f"Remove applications.{app_id} from the controller configuration; its plans stay in the database as history.",
        f"In {app.repository}: disable or delete the three workflows and the staging environment, and revoke the platform credential's access to the repository.",
    ]


def update_workflows(config, github, app_ids, platform_sha=None):
    """Open one pull request per app on the operator's behalf; GitHub failures are reported per app, not raised."""
    from .config import Principal
    from .controller import Controller, Store
    from .github import explain
    operator = Principal(name="operator-cli", token_sha256="0" * 64, applications=sorted(config.applications), can_deploy=True)
    controller = Controller(config, github, Store(config.database))
    results = []
    for app_id in app_ids:
        try:
            results.append(controller.update_workflows(operator, app_id, platform_sha, allow_writes=True))
        except ValueError as exc:
            results.append({"application": app_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - every GitHub transport failure is reported in plain words
            results.append({"application": app_id, "error": explain(exc) or str(exc)})
    return results


USES = re.compile(r"^(?P<lead>\s*-?\s*uses:\s*)(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@(?P<sha>[a-f0-9]{40})(?P<comment>\s*#.*)?\s*$")


def sync_pins(ci_path, templates_dir):
    """Dependabot updates only .github/workflows; this carries each new pin into templates/*.yml. Returns changed lines."""
    pins = {}
    for line in ci_path.read_text().splitlines():
        match = USES.match(line)
        if match:
            pins[match["action"]] = (match["sha"], (match["comment"] or "").strip())
    changed = []
    for template in sorted(templates_dir.glob("*.yml")):
        lines = template.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            match = USES.match(line.rstrip("\n"))
            if not match or match["action"] not in pins:
                continue
            sha, comment = pins[match["action"]]
            if sha == match["sha"]:
                continue
            tail = (" " + comment) if comment else (match["comment"] or "")
            lines[index] = f"{match['lead']}{match['action']}@{sha}{tail}\n"
            changed.append(f"{template.name}: {match['action']} {match['sha'][:12]} -> {sha[:12]}")
        template.write_text("".join(lines))
    return changed


def run(parser, args):
    if args.command == "analyze":
        result = analyze_local(args.path, args.name)
        if args.output:
            # Never silently replace reviewed configuration.
            with args.output.open("x") as f:
                yaml.safe_dump(result["draft_spec"], f, sort_keys=False)
        print(json.dumps(result, indent=2))
    elif args.command == "validate":
        spec = parse_spec(args.path.read_text())
        enforce(spec)
        print(json.dumps({"policy": "passed", "spec_hash": spec_hash(spec)}, indent=2))
    elif args.command == "cost":
        from .cost import estimate, findings
        if (args.requests is None) != (args.average_seconds is None):
            parser.error("Provide both --requests and --average-seconds for a scenario")
        spec = parse_spec(args.path.read_text())
        print(json.dumps({"spec_hash": spec_hash(spec), **estimate(spec, args.requests, args.average_seconds), "findings": findings(spec)}, indent=2))
    elif args.command == "schema":
        print(json.dumps(AppSpec.model_json_schema(), indent=2))
    elif args.command == "token":
        token = secrets.token_urlsafe(32)
        print(json.dumps({"token": token, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}, indent=2))
    elif args.command == "sync-pins":
        changed = sync_pins(args.ci, args.templates)
        print("\n".join(changed) if changed else "Templates already match the CI pins")
    elif args.command == "sessions":
        import datetime
        import os

        from .config import load_config
        from .controller import Store
        from .oauth import OAuthStore
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        store = OAuthStore(Store(config.database))
        sessions = store.sessions()
        targets = [s["session"] for s in sessions if s["state"] == "live"] if args.revoke_all else args.revoke
        for family in targets:
            store.revoke_family(family)
        for session in store.sessions():
            when = datetime.datetime.fromtimestamp(session["refresh_expires"], datetime.UTC).strftime("%Y-%m-%d")
            print(f"{session['session']}  {session['state']:8}  {session['identity'] or '-':12}  {session['client'] or '-':24}  refresh until {when}")
        if targets:
            print(f"Ended {len(targets)} session(s).")
        elif not sessions:
            print("No sign-in sessions recorded.")
    elif args.command in ("watchdog", "backup"):
        import os

        from .config import load_config
        from .controller import Controller, Store
        from .github import GitHub
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        controller = Controller(config, GitHub(), Store(config.database))
        print(json.dumps(controller.watchdog_tick() if args.command == "watchdog" else controller.backup_tick(), indent=2))
    elif args.command == "decommission":
        import os

        from .config import load_config
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        if args.app not in config.applications:
            parser.error(f"Unknown application: {args.app}")
        for number, step in enumerate(decommission_steps(args.app, config.applications[args.app]), 1):
            print(f"{number}. {step}")
    elif args.command == "update-workflows":
        import os

        from .config import load_config
        from .github import GitHub
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        app_ids = sorted(config.applications) if args.all else args.app
        if not app_ids:
            parser.error("Give --all or at least one --app")
        unknown = [a for a in app_ids if a not in config.applications]
        if unknown:
            parser.error(f"Unknown application(s): {', '.join(unknown)}")
        print(json.dumps(update_workflows(config, GitHub(), app_ids, args.platform_commit), indent=2))
    else:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.platform_repo) or not re.fullmatch(r"[a-f0-9]{40}", args.platform_sha):
            parser.error("Provide owner/repository and a full 40-character platform commit SHA")
        rendered = args.template.read_text().replace("PLATFORM_REPOSITORY", args.platform_repo).replace("PLATFORM_COMMIT", args.platform_sha)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as f:
            f.write(rendered)
        print(args.output)


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        run(parser, args)
    except FileExistsError as exc:
        # Reviewed files are never replaced; an operator removes or renames them deliberately.
        sys.exit(f"Refusing to overwrite existing file: {exc.filename}")
    except (ValueError, OSError) as exc:
        # Policy and validation failures are reported plainly; the non-zero exit status still fails CI.
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
