"""Moving the controller onto its own machine (docs/ROADMAP.md 15.5), from the laptop's side.

`export` writes the two files the machine imports (scripts/install-machine.sh copies them there): a consistent copy of the
database (VACUUM INTO, so a copy taken while anything reads it is still whole) and the configuration rewritten for the
machine: its paths under /var/lib/fast-deploy, where Claude reaches it (the machine's own address, or a DNS name), its
own folder in the backup bucket (`machine`), the Litestream replica in that folder, and the GitHub App it signs in to
GitHub as. Principals, applications and every other setting move unchanged, and the laptop's own file is left as it is.

    python -m labdeploy.machine export --config config.local.yaml --host 34.123.45.67 --github-app-id 123456 --out DIR
"""
import argparse
import copy
import ipaddress
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

from .config import Config, UniqueLoader

DATA = "/var/lib/fast-deploy"
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*"]
APP_KEY_SECRET = "FAST_DEPLOY_GITHUB_APP_KEY"  # noqa: S105 - the secret's name (controller.tf, github_app_key_secret), not a value
DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def check_host(host):
    """Where Claude reaches the machine: a public IPv4 address (Let's Encrypt certifies the address itself) or a DNS name."""
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        if DOMAIN.fullmatch(host or ""):
            return host
        raise ValueError(f"{host!r} is neither the machine's address (like 34.123.45.67) nor a DNS name (like deploy.lab.example.edu)") from None
    if address.version != 4 or not address.is_global:
        raise ValueError(f"{host} is not a public IPv4 address: Claude's connectors reach the machine over the internet, on IPv4")
    return str(address)


def machine_config(data, host, github_app_id=None):
    """The laptop's configuration (as parsed YAML) rewritten for the machine; refused when the machine could not run it."""
    host = check_host(host)
    config = copy.deepcopy(data)
    bucket = config.get("backup_bucket")
    if not bucket:
        raise ValueError("Set backup_bucket first (the foundation's backup_bucket output): the machine keeps its copies and its live replica there")
    config["database"] = f"{DATA}/deployments.db"
    config["log_file"] = f"{DATA}/controller.log"
    config.pop("backup_dir", None)  # next to the database, under /var/lib/fast-deploy
    config.pop("machine_url", None)  # a laptop's setting
    config["backup_prefix"] = "machine"
    config["litestream_replica"] = f"gs://{bucket}/machine/litestream/deployments.db"
    config["oauth"] = {**(config.get("oauth") or {}), "public_url": f"https://{host}"}
    config["allowed_hosts"] = [*LOOPBACK_HOSTS, host]
    if github_app_id:
        project = (config.get("onboarding") or {}).get("project_id")
        if not project:
            raise ValueError("onboarding.project_id names the project whose Secret Manager holds the GitHub App's key; it is not set")
        config["github_app"] = {"app_id": str(github_app_id), "private_key_secret": f"projects/{project}/secrets/{APP_KEY_SECRET}"}
    if not config.get("github_app"):
        raise ValueError("The machine signs in to GitHub as the GitHub App: create it and give its App ID (docs/SETUP.md section 4)")
    Config.model_validate(config)  # the machine refuses a configuration that does not load; say so here first
    return config


def export_database(source, destination):
    """A consistent, compact copy of the controller's database; refused when the copy fails SQLite's integrity check."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError(f"No database at {source}: check `database:` in the configuration")
    if destination.exists():
        raise ValueError(f"{destination} already exists")
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("VACUUM INTO ?", (str(destination),))
    finally:
        db.close()
    copied = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = copied.execute("PRAGMA quick_check").fetchone()[0]
        tables = {row[0] for row in copied.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        plans = copied.execute("SELECT COUNT(*) FROM plans").fetchone()[0] if "plans" in tables else 0
    finally:
        copied.close()
    if integrity != "ok":
        raise ValueError(f"The copy fails SQLite's integrity check: {integrity}")
    return {"database": str(destination), "bytes": destination.stat().st_size, "plans": plans}


def export(config_path, host, out, github_app_id=None):
    """Both files in `out` (created, private); returns what was written, without any value from the configuration."""
    data = yaml.load(Path(config_path).read_text(), Loader=UniqueLoader)  # noqa: S506 - SafeLoader subclass
    config = machine_config(data, host, github_app_id)
    folder = Path(out)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = export_database(Path(data.get("database") or "deployments.db"), folder / "deployments.db")
    target = folder / "config.yaml"
    target.write_text("# The machine's configuration, written by python -m labdeploy.machine export from the laptop's.\n"
                      + yaml.safe_dump(config, sort_keys=False))
    target.chmod(0o600)
    return {**report, "configuration": str(target), "public_url": config["oauth"]["public_url"], "backup_prefix": config["backup_prefix"],
            "litestream_replica": config["litestream_replica"], "github_app_id": config["github_app"]["app_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.machine")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("export", help="write the database copy and the machine's configuration into a folder")
    command.add_argument("--config", default="config.local.yaml")
    command.add_argument("--host", required=True, help="where Claude reaches the machine: its address (34.123.45.67) or a DNS name")
    command.add_argument("--github-app-id", help="the GitHub App's ID or Client ID; needed unless the configuration already has github_app")
    command.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export(args.config, args.host, args.out, args.github_app_id), indent=2))
    except ValueError as exc:
        print(f"Not exported: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
