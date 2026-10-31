"""The command line: a caller token, the catalog document, and offline validation and planning of a request file."""
import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

import pydantic
import yaml

from . import __version__
from .config import Config, load_config
from .mcp.catalog import render_markdown
from .models.errors import InfrastructureError, problems_from
from .models.request import InfrastructureRequest
from .orchestration import access, planner, validator
from .providers import ProviderRegistry
from .security import validation
from .security.auth import keyed, pepper

OFFLINE = {"providers": {"gcp": {"project_id": "example-project", "allowed_regions": ["us-central1"]}}, "access": {"allowed_domains": ["example.com"]},
           "principals": [{"name": "offline", "token_sha256": "0" * 64, "can_provision": True}]}


def read_request(path: str) -> InfrastructureRequest | list:
    """The request a file holds, or the list of problems the contract found with it."""
    text = Path(path).read_text()
    data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    try:
        return InfrastructureRequest.model_validate(data)
    except pydantic.ValidationError as exc:
        return problems_from(exc)


def config_or_offline(path: str | None, request: InfrastructureRequest | None = None) -> Config:
    if path and Path(path).exists():
        return load_config(path)
    print("No configuration file: validating against an example project with the default limits", file=sys.stderr)
    config = Config.model_validate(OFFLINE)
    if request is not None and request.access:
        # offline, nobody knows the operator's allowed domains: the members' own are taken, and that is said
        domains = sorted({validation.member_domain(entry.member) for entry in request.access})
        config = config.model_copy(update={"access": config.access.model_copy(update={"allowed_domains": domains})})
        print("Access members are not checked offline: the operator's access.allowed_domains decides who may be granted", file=sys.stderr)
    return config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cloud-infra", description=f"Cloud Infrastructure MCP {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="print a new caller token and the hash to put in the configuration")
    catalog = commands.add_parser("catalog", help="print the MCP service catalog as Markdown")
    catalog.add_argument("--write", metavar="PATH", help="write it to this file instead (docs/MCP-CATALOG.md)")
    for name in ("validate", "plan"):
        command = commands.add_parser(name, help=f"{name} a request file (YAML or JSON) offline, without any provider call")
        command.add_argument("file")
        command.add_argument("--config", default=None, help="the service configuration (default: CLOUDINFRA_CONFIG or an example project)")
    args = parser.parse_args(argv)
    if args.command == "token":
        token = secrets.token_urlsafe(32)
        print(f"token: {token}")
        key = pepper()
        if key:  # keyed with the pepper in the environment: put this line in the configuration instead
            print(f"token_hmac_sha256: {keyed(token, key)}")
        else:
            print(f"token_sha256: {hashlib.sha256(token.encode()).hexdigest()}")
        return 0
    if args.command == "catalog":
        text = render_markdown()
        if args.write:
            Path(args.write).write_text(text)
            print(f"wrote {args.write}", file=sys.stderr)
        else:
            print(text, end="")
        return 0
    request = read_request(args.file)
    if isinstance(request, list):
        print(json.dumps({"valid": False, "problems": [item.model_dump(mode="json") for item in request]}, indent=1))
        return 1
    config = config_or_offline(args.config, request)
    registry = ProviderRegistry(config)
    try:
        provider = registry.get(request.provider)
    except InfrastructureError as exc:
        print(json.dumps({"valid": False, "problems": [exc.detail().model_dump(mode="json")]}, indent=1))
        return 1
    principal = config.principals[0]
    problems = validator.validate(request, config, provider) + access.problems(request, config, principal)
    if problems:
        print(json.dumps({"valid": False, "problems": [{"code": item.code.value, "message": item.message, "field": item.field} for item in problems]}, indent=1))
        return 1
    ctx = planner.context(request, "0" * 32, principal.name, config, provider)
    planned = planner.plan(request, ctx, provider, config)
    estimate = provider.estimate_cost(planned)
    cost = planner.cost_problem(estimate, config, principal)
    if args.command == "validate":
        print(json.dumps({"valid": cost is None, "problems": [] if cost is None else [{"code": cost.code.value, "message": cost.message}]}, indent=1))
        return 0 if cost is None else 1
    print(json.dumps({"planned": [item.model_dump(mode="json") for item in planned], "estimate": estimate.model_dump(mode="json"),
                      "warnings": planner.warnings(request, planned, config, provider, principal), "cost_problem": None if cost is None else cost.message}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
