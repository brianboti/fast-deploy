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
    parser = argparse.ArgumentParser(description="Fast Deploy: generate, validate and plan before deploying")
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
    dr = commands.add_parser("restore-drill", help="restore the newest controller backup from the backup bucket into a temporary folder, read-only, "
                                                   "check and count it, and record the outcome (the quarterly drill)")
    dr.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    tc = commands.add_parser("tools-checksum", help="print the SHA-256 of every MCP tool's name, description and schemas; --write updates "
                                                    "schema/tools.sha256.json after a deliberate change")
    tc.add_argument("--write", action="store_true", help="write schema/tools.sha256.json (review and commit the diff)")
    d = commands.add_parser("decommission", help="print the checklist that retires one application cleanly")
    d.add_argument("app")
    d.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    g = commands.add_parser("unregister", help="forget an onboarded application (its entry and onboarding record); plans stay as history")
    g.add_argument("app")
    g.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    u = commands.add_parser("update-workflows", help="open pull requests that pin registered apps' workflows to a platform commit")
    u.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    u.add_argument("--all", action="store_true", help="every registered application")
    u.add_argument("--app", action="append", default=[], help="one application id (repeatable)")
    u.add_argument("--platform-commit", default=None, help="full platform commit SHA (default: platform.commit, else the platform branch tip)")
    i = commands.add_parser("infra", help="infrastructure as pull requests: render an app's Terraform root, check or summarise a plan")
    infra = i.add_subparsers(dest="infra_command", required=True)
    r = infra.add_parser("render", help="render infra/gcp/apps/SERVICE for one registered application from the configuration, its reviewed spec and GitHub's ids")
    r.add_argument("app")
    r.add_argument("--config", default=None, help="controller configuration (default: $LAB_CONFIG or config.local.yaml)")
    r.add_argument("--write", action="store_true", help="write the files under --root instead of printing them")
    r.add_argument("--root", type=Path, default=Path("infra/gcp/apps"), help="directory that holds one subdirectory per application root")
    r.add_argument("--pull-request", action="store_true", help="open the pull request on the platform repository instead")
    r.add_argument("--state-bucket", default=None, help="GCS bucket of the Terraform states (default: PROJECT-tfstate)")
    r.add_argument("--bootstrap-image", default=None, help="image the service starts with before its first release (default: Google's sample, pinned)")
    k = infra.add_parser("check", help="check a `terraform show -json` plan against the platform's policies; exit status 2 on violations")
    k.add_argument("plan", type=Path)
    m = infra.add_parser("summary", help="the pull request comment for a `terraform show -json` plan")
    m.add_argument("plan", type=Path)
    m.add_argument("--app", default="app")
    return parser


def decommission_steps(app_id, app, needs=None, note=None, registered=False, siblings=()):
    """The order that retires an app without leaving a bill or a dangling trust: drain, delete, then let Terraform forget it.
    `registered`: the app was onboarded, so its entry lives in the controller's database rather than the configuration file.
    `siblings`: the other applications deployed from the same repository, which keep its access and their own workflows."""
    t = app.target
    needs = needs or {}
    extra = []
    for name in needs.get("buckets") or []:
        extra.append(f"Empty and delete the bucket {t.project_id}-{t.service}-{name} (its data is gone for good): gcloud storage rm -r gs://{t.project_id}-{t.service}-{name}/** "
                     f"&& gcloud storage buckets delete gs://{t.project_id}-{t.service}-{name}")
    for name in needs.get("topics") or []:
        extra.append(f"Delete the subscription and topic: gcloud pubsub subscriptions delete {t.service}-{name} --project {t.project_id} && "
                     f"gcloud pubsub topics delete {t.service}-{name} --project {t.project_id}")
    if needs.get("database") == "postgres":
        extra.append(f"Turn deletion protection off and delete the Cloud SQL instance {t.service} (its data is gone for good; export it first if it matters, "
                     f"and the name stays reserved for about a week): gcloud sql instances patch {t.service} --no-deletion-protection --project {t.project_id} "
                     f"&& gcloud sql instances delete {t.service} --project {t.project_id}")
    if needs.get("migrate"):
        extra.append(f"Delete the migration job: gcloud run jobs delete {t.service}-migrate --region {t.region} --project {t.project_id}")
    if needs.get("database") == "firestore":
        extra.append(f"Turn delete protection off and delete the database {t.service} (its data is gone for good): gcloud firestore databases update --database={t.service} "
                     f"--no-delete-protection --project {t.project_id} && gcloud firestore databases delete --database={t.service} --project {t.project_id}")
    if note:
        extra.append(note)
    where = f"application_settings.{app_id}" if registered else f"applications.{app_id}"
    forget = (f"Unregister it: `lab-deploy unregister {app_id}` removes its entry and onboarding record from the controller's database; its plans stay as history."
              if registered else f"Remove applications.{app_id} from the controller configuration; its plans stay in the database as history.")
    return [
        f"Pause it: add `paused: true` under {where} in the controller configuration (applies without a restart).",
        f"Tell its owners; a rollback target no longer exists once the service is gone. Last chance for `gcloud run services describe {t.service} --region {t.region} --project {t.project_id}`.",
        f"Delete the service: gcloud run services delete {t.service} --region {t.region} --project {t.project_id}",
        f"Empty and delete the recovery bucket: gcloud storage rm -r gs://{t.recovery_bucket}/** && gcloud storage buckets delete gs://{t.recovery_bucket}",
        f"Delete the images: gcloud artifacts repositories delete {t.registry} --location {t.region} --project {t.project_id}",
        f"Delete its secrets, if any: gcloud secrets list --filter='name:{t.service}-' --project {t.project_id}",
        *extra,
        f"As the operator, run `terraform state rm` for each need deleted above, then `terraform destroy` in infra/gcp/apps/{t.service} (the pipeline never destroys), "
        "and delete that directory in a pull request; Terraform drops the identities, trust and alerts it still owns, and schedules its provenance "
        "key's versions for destruction (Cloud KMS keeps the key's name, so a later app of the same name imports it rather than creating it).",
        forget,
        (f"In {app.repository}: delete {', '.join(app.workflows.values())} and the {app.environment} environment. Keep the platform credential's "
         f"access to the repository: {', '.join(sorted(siblings))} still deploy from it." if siblings else
         f"In {app.repository}: disable or delete {', '.join(app.workflows.values())} and the {app.environment} environment, and revoke the platform "
         "credential's access to the repository."),
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
    """Dependabot updates only .github/workflows; this carries each new pin into templates/*.yml and the CI workflow the
    checker drafts for applications (templates/repo/ci.yml). Returns changed lines."""
    pins = {}
    for line in ci_path.read_text().splitlines():
        match = USES.match(line)
        if match:
            pins[match["action"]] = (match["sha"], (match["comment"] or "").strip())
    changed = []
    for template in sorted([*templates_dir.glob("*.yml"), *templates_dir.glob("repo/*.yml")]):
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
            changed.append(f"{template.relative_to(templates_dir)}: {match['action']} {match['sha'][:12]} -> {sha[:12]}")
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
        config_path = args.config or os.environ.get("LAB_CONFIG", "config.local.yaml")
        config = load_config(config_path)
        controller = Controller(config, GitHub.from_config(config), Store(config.database))
        print(json.dumps(controller.watchdog_tick() if args.command == "watchdog" else controller.backup_tick(config_path=config_path), indent=2))
    elif args.command == "restore-drill":
        import getpass
        import os

        from .config import load_config
        from .controller import Store
        from .drill import lines, restore_drill
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        try:
            who = getpass.getuser()
        except (OSError, KeyError):
            who = "unknown"
        report = restore_drill(config, Store(config.database), identity=f"operator-cli:{who}")
        print("\n".join(lines(report)))
        if not report["ok"]:
            sys.exit(1)
    elif args.command == "tools-checksum":
        from .toolpin import PINS, checksums, compare, listed_tools, pinned, write
        found = checksums(listed_tools())
        print(json.dumps(found, indent=2))
        if args.write:
            print(f"Wrote {write(found)}; review the diff and commit it with the change that caused it.", file=sys.stderr)
            return
        difference = compare(found, pinned())
        if any(difference.values()):
            parts = [f"{what}: {', '.join(names)}" for what, names in difference.items() if names]
            print(f"Differs from {PINS.name} ({'; '.join(parts)}). If the change is deliberate, run this again with --write and commit the reviewed diff.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Matches {PINS.name}.", file=sys.stderr)
    elif args.command == "decommission":
        import os

        from .config import Application, load_config
        from .controller import Store
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        registered = {app_id: Application.model_validate(entry) for app_id, entry in Store(config.database).registered_applications().items()}
        applications = {**registered, **config.applications}
        if args.app not in applications:
            parser.error(f"Unknown application: {args.app}")
        needs, note = None, None
        try:
            from .controller import Controller
            from .github import GitHub
            app = applications[args.app]
            controller = Controller(config, GitHub.from_config(config), Store(config.database))
            needs = controller.spec_at(app, controller.github.head(app)).needs.model_dump()
        except Exception as exc:  # noqa: BLE001 - the checklist is still useful without the spec; say what could not be read
            note = f"The app's platform.yaml could not be read ({str(exc)[:100]}); check it for buckets, topics and a database and delete their data deliberately."
        siblings = [other for other, entry in applications.items() if other != args.app and entry.repository.lower() == applications[args.app].repository.lower()]
        steps = decommission_steps(args.app, applications[args.app], needs, note, registered=args.app not in config.applications, siblings=siblings)
        for number, step in enumerate(steps, 1):
            print(f"{number}. {step}")
    elif args.command == "unregister":
        import os

        from .config import load_config
        from .controller import Store
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        if args.app in config.applications:
            parser.error(f"{args.app} is written in the configuration file; remove it there instead")
        if not Store(config.database).unregister_application(args.app):
            parser.error(f"No onboarded application called {args.app}")
        print(f"Unregistered {args.app}: its entry and onboarding record are gone; its plans and audit events stay as history. "
              "Its cloud resources are untouched: `lab-deploy decommission` lists them first.")
    elif args.command == "infra":
        from .infra import main as infra_main
        if args.infra_command == "check":
            sys.exit(infra_main(["check", str(args.plan)]))
        if args.infra_command == "summary":
            sys.exit(infra_main(["summary", str(args.plan), args.app]))
        import os

        from .config import Principal, load_config
        from .controller import Controller, Store
        from .github import GitHub
        config = load_config(args.config or os.environ.get("LAB_CONFIG", "config.local.yaml"))
        if args.app not in config.applications:
            parser.error(f"Unknown application: {args.app}")
        operator = Principal(name="operator-cli", token_sha256="0" * 64, applications=sorted(config.applications), can_deploy=True)
        controller = Controller(config, GitHub.from_config(config), Store(config.database))
        options = {"state_bucket": args.state_bucket, "bootstrap_image": args.bootstrap_image}
        if args.pull_request:
            print(json.dumps(controller.propose_infrastructure(operator, args.app, allow_writes=True, **options), indent=2))
        else:
            rendered = controller.render_infrastructure(operator, args.app, **options)
            if rendered["note"]:
                print(f"Note: {rendered['note']}", file=sys.stderr)
            if args.write:
                directory = args.root / rendered["service"]
                directory.mkdir(parents=True, exist_ok=True)
                lock = args.root.parent / "project" / ".terraform.lock.hcl"
                if lock.is_file():  # every root pins the same provider build as the foundation
                    rendered["files"][".terraform.lock.hcl"] = lock.read_text()
                for name, text in rendered["files"].items():
                    (directory / name).write_text(text)  # generated files are replaced on purpose; the sources are elsewhere
                    print(directory / name)
            else:
                for name, text in rendered["files"].items():
                    print(f"# ---- {rendered['directory']}/{name}\n{text}")
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
        print(json.dumps(update_workflows(config, GitHub.from_config(config), app_ids, args.platform_commit), indent=2))
    else:
        from .workflows import render
        try:
            rendered = render(args.template.read_text(), args.platform_repo, args.platform_sha)  # the plain layout: one service at the root
        except ValueError as exc:
            parser.error(str(exc))
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
