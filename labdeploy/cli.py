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
    return parser


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
