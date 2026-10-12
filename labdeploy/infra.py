"""Infrastructure as pull requests.

One Terraform root per application, generated from the controller configuration and the app's reviewed
``platform.yaml``; a policy check over a Terraform plan; the pull request summary; and the step that publishes a root's
outputs to the application's GitHub environment. Standard library only, so the pipeline runs it from a bare checkout.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
DIGITS = re.compile(r"^[0-9]{1,20}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{3,300}$")
HEALTH_PATH = re.compile(r"^/[A-Za-z0-9/_-]*$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

# Google's public sample, pinned by digest: what a new service runs until its first release replaces it.
BOOTSTRAP_IMAGE = "us-docker.pkg.dev/cloudrun/container/hello@sha256:ea86b59c787261f424f9de114900e598f19e036c73aa95ff12b6ad5f022122fd"

DEFAULT_SETTINGS = {
    "image_versions_to_keep": 10,
    "image_retention_days": 180,
    "registry_cleanup_dry_run": True,
    "runtime_alerts": True,
    "error_alert_count": 5,
    "latency_alert_ms": 2000,
    "instance_alert_count": 3,
}
SETTING_RANGES = {
    "image_versions_to_keep": (1, 100),
    "image_retention_days": (1, 3650),
    "error_alert_count": (1, 10_000),
    "latency_alert_ms": (100, 300_000),
    "instance_alert_count": (1, 1000),
}
ALLOWED_LOCATIONS = ("us-central1",)
DATA_TYPES = (
    "google_storage_bucket", "google_sql_database_instance", "google_bigquery_dataset", "google_firestore_database",
    "google_secret_manager_secret", "google_artifact_registry_repository",
)
LABELLED_TYPES = ("google_cloud_run_v2_service", "google_storage_bucket", "google_artifact_registry_repository",
                  "google_sql_database_instance", "google_bigquery_dataset", "google_pubsub_topic", "google_pubsub_subscription")
PLATFORM_IDENTITIES = ("lab-deploy-infra-", "lab-deploy-observer", "lab-deploy-spending-cap")
BROAD_ROLES = ("roles/owner", "roles/editor")
GITHUB_API = "https://api.github.com"


def _hcl(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_hcl(item) for item in value) + "]"
    # A value is always a literal: Terraform's template sequences are escaped so nothing in a spec becomes an expression.
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


def app_root(name, *, project_id, region, state_bucket, github_repository, github_repository_id, github_owner_id,
             bootstrap_image, health_path="/health", environment="staging", settings=None, needs=None):
    """The three files of an application's Terraform root, with every input a literal so a reviewer sees the truth."""
    checks = [
        (NAME, name, "application name"), (PROJECT, project_id, "project id"), (BUCKET, state_bucket, "state bucket"),
        (REPOSITORY, github_repository, "GitHub repository"), (DIGITS, github_repository_id, "GitHub repository id"),
        (DIGITS, github_owner_id, "GitHub owner id"), (IMAGE, bootstrap_image, "bootstrap image"),
        (HEALTH_PATH, health_path, "health path"), (ENVIRONMENT, environment, "environment"),
    ]
    for pattern, value, what in checks:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"Invalid {what}: {value!r}")
    if region not in ALLOWED_LOCATIONS:
        raise ValueError(f"Region {region!r} is outside the platform policy ({', '.join(ALLOWED_LOCATIONS)})")
    merged = dict(DEFAULT_SETTINGS)
    for key, value in (settings or {}).items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Unknown setting {key!r}")
        expected = type(DEFAULT_SETTINGS[key])
        if type(value) is not expected:
            raise ValueError(f"Setting {key!r} must be {expected.__name__}")
        if key in SETTING_RANGES and not SETTING_RANGES[key][0] <= value <= SETTING_RANGES[key][1]:
            raise ValueError(f"Setting {key!r} must be between {SETTING_RANGES[key][0]} and {SETTING_RANGES[key][1]}")
        merged[key] = value
    needs = {"buckets": [], "topics": [], "database": "none", "migrate": [], **(needs or {})}
    if set(needs) != {"buckets", "topics", "database", "migrate"} or needs["database"] not in ("none", "firestore", "postgres"):
        raise ValueError("needs must hold buckets, topics, database (none, firestore or postgres) and migrate")
    parts_ok = isinstance(needs["migrate"], list) and len(needs["migrate"]) <= 16 and all(
        isinstance(p, str) and p.isprintable() and not any(c in p for c in ',"$^') and len(p) <= 200 for p in needs["migrate"])
    if not parts_ok:
        raise ValueError("migrate is a list of at most 16 printable arguments without commas, double quotes, dollar signs or carets")
    for kind in ("buckets", "topics"):
        for item in needs[kind]:
            if not isinstance(item, str) or not RESOURCE_NAME.fullmatch(item):
                raise ValueError(f"Invalid {kind[:-1]} name: {item!r}")
        if len(set(needs[kind])) != len(needs[kind]):
            raise ValueError(f"{kind} must be unique")
    for item in needs["buckets"]:
        if len(f"{project_id}-{name}-{item}") > 63:
            raise ValueError(f"Bucket name {project_id}-{name}-{item} exceeds 63 characters; shorten the need's name")
    header = (f"# Generated by `lab-deploy infra render {name}` from the controller configuration and the app's platform.yaml.\n"
              "# Change those sources and render again; the pipeline plans this root on a pull request and applies it on merge.\n")
    backend = header + f'''terraform {{
  required_version = ">= 1.6, < 2.0"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 7.0"
    }}
  }}
  backend "gcs" {{
    bucket = {_hcl(state_bucket)}
    prefix = {_hcl(f"apps/{name}/{environment}")}
  }}
}}
'''
    inputs = {
        "project_id": project_id, "region": region, "service": name, "github_repository": github_repository,
        "github_repository_id": github_repository_id, "github_owner_id": github_owner_id, "bootstrap_image": bootstrap_image,
        "health_path": health_path,
    }
    inputs.update(merged)
    width = max(len(key) for key in list(inputs) + ["image_vulnerability_scanning"])
    lines = [f"  {key.ljust(width)} = {_hcl(value)}" for key, value in inputs.items()]
    lines.append(f"  {'needs'.ljust(width)} = {{ buckets = {_hcl(needs['buckets'])}, topics = {_hcl(needs['topics'])}, database = {_hcl(needs['database'])}, migrate = {_hcl(needs['migrate'])} }}")
    shared = {
        "project_number": "project_number", "image_vulnerability_scanning": "image_vulnerability_scanning",
        "tag_manager_role": "tag_manager_role", "alert_channels": "alert_channels", "cap_service_account": "cap_service_account",
        "billing_dataset": "billing_dataset", "network": "network", "subnet": "subnet", "cap_operator_role": "cap_operator_role",
    }
    lines += [f"  {key.ljust(width)} = data.terraform_remote_state.project.outputs.{output}" for key, output in shared.items()]
    main = header + f'''provider "google" {{
  project = {_hcl(project_id)}
  region  = {_hcl(region)}
}}

# Project-wide values (identities, channels, roles) come from the foundation root's state, read-only.
data "terraform_remote_state" "project" {{
  backend = "gcs"
  config = {{
    bucket = {_hcl(state_bucket)}
    prefix = {_hcl(f"project/{environment}")}
  }}
}}

module "service" {{
  {"source".ljust(width)} = "../../modules/service"
{chr(10).join(lines)}
}}
'''
    outputs = header + f'''output "github_repository" {{ value = {_hcl(github_repository)} }}
output "github_environment_variables" {{ value = module.service.github_environment_variables }}
output "service_url" {{ value = module.service.service_url }}
output "console_url" {{ value = module.service.console_url }}
output "dashboard_url" {{ value = module.service.dashboard_url }}
output "runtime_alerts" {{ value = module.service.runtime_alerts }}
'''
    return {"backend.tf": backend, "main.tf": main, "outputs.tf": outputs}


def roots(changed, apps_dir):
    """Which application roots a set of changed paths affects: a module or pipeline change affects every root."""
    directory = Path(apps_dir)
    names = sorted(p.name for p in directory.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if directory.is_dir() else []
    if any(path.startswith(("infra/gcp/modules/", ".github/workflows/infra-")) for path in changed):
        return names
    touched = set()
    for path in changed:
        parts = path.split("/")
        if parts[:3] == ["infra", "gcp", "apps"] and len(parts) > 3 and parts[3] in names:
            touched.add(parts[3])
    return sorted(touched)


def _after(change):
    return change.get("change", {}).get("after") or {}


def _nested(value, *keys):
    for key in keys:
        if isinstance(value, list):
            value = value[0] if value else {}
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _unknown(change, *keys):
    """True when a field the policy relies on is only known after apply."""
    return _nested(change.get("change", {}).get("after_unknown") or {}, *keys) is True


def _strings(value):
    """Every string inside a plan value, however nested, so an identity or a public member cannot hide in a sub-field."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


PUBLIC_MEMBERS = ("allUsers", "allAuthenticatedUsers")
# The only roles an application root may grant; everything else is the foundation's business.
ALLOWED_ROLES = frozenset({
    "roles/run.invoker", "roles/run.developer", "roles/run.viewer", "roles/artifactregistry.writer", "roles/artifactregistry.reader",
    "roles/storage.objectUser", "roles/storage.objectViewer", "roles/storage.bucketViewer", "roles/pubsub.publisher", "roles/pubsub.subscriber",
    "roles/pubsub.viewer", "roles/datastore.user", "roles/datastore.viewer", "roles/cloudsql.client", "roles/cloudsql.instanceUser", "roles/cloudsql.viewer",
    "roles/logging.viewer", "roles/monitoring.viewer", "roles/secretmanager.secretAccessor", "roles/secretmanager.viewer", "roles/bigquery.dataViewer",
    "roles/bigquery.jobUser", "roles/iam.serviceAccountUser", "roles/iam.serviceAccountOpenIdTokenCreator", "roles/iam.workloadIdentityUser",
})
CUSTOM_ROLE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/labDeploy[A-Za-z]+$")
# The one grant to a platform identity an application root makes by design: the spending cap may read and update the
# application's own Cloud Run service through the foundation's cap-operator role, nothing more and nowhere else.
CAP_OPERATOR_ROLE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/labDeployCapOperator$")
CAP_IDENTITY = "serviceAccount:lab-deploy-spending-cap@"
STATE_BUCKET = re.compile(r"-tfstate$")


def check_plan(plan, allowed_locations=ALLOWED_LOCATIONS):
    """Violations of the platform's policies in a `terraform show -json` plan; an empty list means the plan may apply.

    The rules fail closed: a value the plan does not know yet, or a field the policy expects and cannot find, is a
    violation. Invariants hold on creation and on every later update, not only when a resource first appears.
    """
    violations = []

    def add(rule, address, detail):
        violations.append({"rule": rule, "address": address, "detail": detail})

    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"], []):
            continue
        address, kind, after = change.get("address", "?"), change.get("type", ""), _after(change)
        creating, updating, deleting = "create" in actions, "update" in actions, "delete" in actions
        touching = creating or updating
        iam = kind.endswith(("_iam_member", "_iam_binding", "_iam_policy"))
        if kind == "google_service_account_key" and creating:
            add("no_service_account_keys", address, "service account keys are never created; use Workload Identity Federation")
        if deleting and kind in DATA_TYPES:
            add("no_data_loss", address, "the pipeline never destroys resources that hold data; retire them through `lab-deploy decommission`")
        texts = list(_strings(after))
        cap_grant = (kind == "google_cloud_run_v2_service_iam_member" and CAP_OPERATOR_ROLE.fullmatch(after.get("role") or "") is not None
                     and (after.get("member") or "").startswith(CAP_IDENTITY))
        if touching and not cap_grant and any(marker in text for marker in PLATFORM_IDENTITIES for text in texts):
            add("platform_identities_untouched", address, "application roots may not create, grant or reference the platform's own identities")
        if touching and iam:
            if kind.endswith("_iam_policy"):
                add("authoritative_policy_forbidden", address, "an authoritative IAM policy replaces every binding on the resource; grant members one at a time")
            if any(text in PUBLIC_MEMBERS for text in texts):
                add("no_public_access", address, "nothing managed here is ever public; allUsers and allAuthenticatedUsers are refused")
            role = after.get("role")
            if not kind.endswith("_iam_policy"):
                if _unknown(change, "role") or role is None:
                    add("unknown_value", address, "the role is not known until apply; policy cannot judge it")
                elif role not in ALLOWED_ROLES and not CUSTOM_ROLE.fullmatch(role):
                    add("role_not_allowed", address, f"{role} is not a role an application root may grant")
            if kind.startswith("google_storage_bucket_iam") and STATE_BUCKET.search(after.get("bucket") or ""):
                add("state_bucket_untouched", address, "the Terraform state bucket's access is managed by the foundation only")
        if touching and kind == "google_iam_workload_identity_pool_provider":
            condition = after.get("attribute_condition") or ""
            if _unknown(change, "attribute_condition") or "assertion.repository_id ==" not in condition or "assertion.repository_owner_id ==" not in condition:
                add("federation_unconditioned", address, "a federation provider must pin the repository and owner ids in its attribute condition")
        if touching:
            for field in ("location", "region", "location_id"):
                if _unknown(change, field):
                    add("unknown_value", address, f"{field} is not known until apply; policy cannot judge it")
                value = after.get(field)
                if isinstance(value, str) and value.lower() not in allowed_locations:
                    add("region_policy", address, f"{field} {value!r} is outside {', '.join(allowed_locations)}")
            if kind == "google_sql_database_instance":
                if _nested(after, "settings", "ip_configuration", "ipv4_enabled") is not False or _unknown(change, "settings", "ip_configuration", "ipv4_enabled"):
                    add("private_database", address, "databases get a private address only: settings.ip_configuration.ipv4_enabled must be false")
                if after.get("deletion_protection") is not True:
                    add("protected_database", address, "databases keep deletion protection on")
            if kind == "google_firestore_database" and after.get("delete_protection_state") != "DELETE_PROTECTION_ENABLED":
                add("protected_database", address, "databases keep delete protection on")
            if kind == "google_storage_bucket" and (after.get("uniform_bucket_level_access") is not True or after.get("public_access_prevention") != "enforced"
                                                   or _unknown(change, "public_access_prevention")):
                add("private_bucket", address, "buckets use uniform bucket-level access and enforced public access prevention")
            if kind in LABELLED_TYPES:
                labels = (_nested(after, "settings", "user_labels") if kind == "google_sql_database_instance" else after.get("labels")) or {}
                if not isinstance(labels, dict) or "lab-deploy-app" not in labels:
                    add("labels_present", address, "every managed resource carries the lab-deploy-app label for cost allocation and the spending cap")
    return violations


def summary(plan):
    """Counts and one line per resource change, for the pull request comment."""
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0}
    lines = []
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"], []):
            continue
        if actions == ["create"]:
            counts["create"] += 1
            symbol = "+"
        elif actions == ["delete"]:
            counts["delete"] += 1
            symbol = "-"
        elif "create" in actions and "delete" in actions:
            counts["replace"] += 1
            symbol = "-/+"
        else:
            counts["update"] += 1
            symbol = "~"
        lines.append(f"{symbol} {change.get('address', '?')}")
    return {"counts": counts, "lines": lines}


def comment(app, plan, violations, plan_text=None):
    """Markdown for the pull request: what the apply would do, the full plan folded away, and whether policy allows it."""
    digest = summary(plan)
    counts = digest["counts"]
    head = (f"<!-- lab-deploy-infra:{app} -->\n### Infrastructure plan for `{app}`\n\n"
            f"{counts['create']} to add, {counts['update']} to change, {counts['delete']} to destroy, {counts['replace']} to replace.\n")
    if digest["lines"]:
        body = "\n```\n" + "\n".join(digest["lines"][:200]) + ("\n…" if len(digest["lines"]) > 200 else "") + "\n```\n"
    else:
        body = "\nNo changes.\n"
    if plan_text and digest["lines"]:
        text = plan_text if len(plan_text) <= 50_000 else plan_text[:50_000] + "\n… (truncated; the full plan is in the workflow log)"
        body += "\n<details><summary>Full plan with every attribute</summary>\n\n```\n" + text.replace("```", "'''") + "\n```\n</details>\n"
    if violations:
        verdict = "\n**Policy: blocked.** The apply job refuses this plan until these are resolved:\n\n" + "\n".join(
            f"- `{v['address']}`: {v['detail']} ({v['rule']})" for v in violations) + "\n"
    else:
        verdict = "\n**Policy: passed.** Merging applies exactly this plan.\n"
    return head + body + verdict


def github_request(method, url, token, body=None):
    """One GitHub API call with the standard library; returns the status code and the decoded body (None when empty)."""
    if not url.startswith(GITHUB_API + "/"):
        raise ValueError("Only the GitHub API is called from here")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={  # noqa: S310 - https only, checked above
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json", "User-Agent": "lab-deploy",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https only, checked above
            text = response.read().decode()
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as error:
        return error.code, None


def publish(outputs, token=None, environment="staging", request=github_request):
    """Set the application's GitHub environment variables from the root's outputs; without a token, say what to set."""
    try:
        values = outputs["github_environment_variables"]["value"]
        repository = outputs["github_repository"]["value"]
    except KeyError as missing:
        raise ValueError(f"The root's outputs lack {missing}; render the root again with the current platform and merge it") from None
    if not REPOSITORY.fullmatch(repository) or not ENVIRONMENT.fullmatch(environment):
        raise ValueError("Unexpected repository or environment name in the outputs")
    lines = [f"### Variables for `{repository}`, environment `{environment}`", "", "| Variable | Value |", "|---|---|"]
    lines += [f"| {name} | `{value}` |" for name, value in sorted(values.items())]
    if not token:
        lines += ["", "No APP_REPOS_TOKEN secret on the platform repository: set these on the environment by hand (docs/SETUP.md 7f)."]
        return "\n".join(lines) + "\n", True
    base = f"{GITHUB_API}/repos/{repository}/environments/{environment}/variables"
    results = []
    for name, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", name):
            raise ValueError(f"Unexpected variable name {name!r}")
        status, _ = request("PATCH", f"{base}/{name}", token, {"name": name, "value": str(value)})
        if status == 404:
            status, _ = request("POST", base, token, {"name": name, "value": str(value)})
        outcome = {204: "updated", 201: "created"}.get(status, f"failed (HTTP {status})")
        results.append(f"- {name}: {outcome}")
    lines += ["", *results]
    ok = not any("failed" in line for line in results)
    if not ok:
        lines += ["", "A failure usually means the environment does not exist yet or the token lacks Variables: write on this repository."]
    return "\n".join(lines) + "\n", ok


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m labdeploy.infra check PLAN.json | summary PLAN.json APP [PLAN.txt] | roots CHANGED.txt APPS_DIR | publish OUTPUTS.json"
    if len(argv) < 2 or argv[0] not in ("check", "summary", "roots", "publish"):
        print(usage, file=sys.stderr)
        return 1
    if argv[0] == "roots":
        changed = [line.strip() for line in Path(argv[1]).read_text().splitlines() if line.strip()]
        print("apps=" + json.dumps(roots(changed, argv[2] if len(argv) > 2 else "infra/gcp/apps")))
        return 0
    document = json.loads(Path(argv[1]).read_text())
    if argv[0] == "check":
        violations = check_plan(document)
        for violation in violations:
            print(f"{violation['rule']}: {violation['address']}: {violation['detail']}")
        print(f"{len(violations)} violation(s)")
        return 2 if violations else 0
    if argv[0] == "publish":
        try:
            text, ok = publish(document, os.environ.get("APP_REPOS_TOKEN") or None)
        except ValueError as exc:
            print(f"Outputs not published: {exc}")
            return 1
        print(text)
        return 0 if ok else 1
    app = argv[2] if len(argv) > 2 else "app"
    plan_text = Path(argv[3]).read_text() if len(argv) > 3 and Path(argv[3]).is_file() else None
    print(comment(app, document, check_plan(document), plan_text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
