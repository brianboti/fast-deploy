"""The command line: a caller token, a check of the configuration, and the plan of a release file against the
infrastructure layer as it is now (a read; nothing runs on a machine)."""
import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

import pydantic
import yaml

from . import __version__, planner
from .config import load_config
from .errors import DeliveryError, problems_from
from .infra import InfraClient
from .models import ReleaseRequest


def read_release(path: str) -> ReleaseRequest:
    text = Path(path).read_text()
    return ReleaseRequest.model_validate(json.loads(text) if path.endswith(".json") else yaml.safe_load(text))


def failed(problems) -> int:
    print(json.dumps({"status": "FAILED", "problems": [item.model_dump(mode="json") for item in problems]}, indent=1))
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="app-delivery", description=f"Application delivery {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="print a new caller token and the hash to put in the configuration")
    check = commands.add_parser("check-config", help="validate a configuration file")
    check.add_argument("config")
    plan = commands.add_parser("plan", help="plan a release file (YAML or JSON) against the infrastructure layer; nothing runs")
    plan.add_argument("file")
    plan.add_argument("--config", required=True)
    plan.add_argument("--as", dest="principal", required=True, help="the principal of the configuration to plan as")
    args = parser.parse_args(argv)
    if args.command == "token":
        token = secrets.token_urlsafe(32)
        print(f"token: {token}")
        print(f"token_sha256: {hashlib.sha256(token.encode()).hexdigest()}")
        return 0
    try:
        config = load_config(args.config)
    except pydantic.ValidationError as exc:
        return failed(problems_from(exc))
    if args.command == "check-config":
        print(json.dumps({"valid": True, "environments": sorted(config.environments), "principals": [item.name for item in config.principals]}, indent=1))
        return 0
    principal = next((item for item in config.principals if item.name == args.principal), None)
    if principal is None:
        print(f"No principal {args.principal!r} in {args.config}", file=sys.stderr)
        return 2
    try:
        release = read_release(args.file)
        planner.authorize(release, principal, config)
        infra = InfraClient.for_environment(config, release.environment).get_request(release.request_id)
        print(json.dumps(planner.plan(release, principal, config, infra).model_dump(mode="json"), indent=1))
        return 0
    except pydantic.ValidationError as exc:
        return failed(problems_from(exc))
    except DeliveryError as exc:
        print(json.dumps(exc.to_dict(), indent=1))
        return 1


if __name__ == "__main__":
    sys.exit(main())
