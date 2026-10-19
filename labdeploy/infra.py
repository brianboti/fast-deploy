"""Infrastructure as pull requests.

One Terraform root per application, generated from the controller configuration and the app's reviewed
``platform.yaml``; a policy check over a Terraform plan; the pull request summary; and the step that publishes a root's
outputs to the application's GitHub environment. Standard library only, so the pipeline runs it from a bare checkout.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
# A root as the drift report names it: the service for staging, production-<service> for its production root (D37).
ROOT_LABEL = re.compile(r"^(production-)?[a-z][a-z0-9-]{2,20}$")
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
# A provenance key counts as data: its versions are what every stored signature verifies against, and Cloud KMS never frees
# the name, so a replaced key could not even be created again.
DATA_TYPES = (
    "google_storage_bucket", "google_sql_database_instance", "google_sql_database", "google_bigquery_dataset", "google_firestore_database",
    "google_secret_manager_secret", "google_artifact_registry_repository", "google_kms_crypto_key",
)
LABELLED_TYPES = ("google_cloud_run_v2_service", "google_storage_bucket", "google_artifact_registry_repository",
                  "google_sql_database_instance", "google_bigquery_dataset", "google_pubsub_topic", "google_pubsub_subscription",
                  "google_kms_crypto_key")
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


EXPOSURES = ("authenticated", "lab-members")
VIEWER = re.compile(r"^(user|group|domain):[A-Za-z0-9._%+-]+(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?$")
# A service account of a project, with the project's id as group 1: a production deployer reading staging (D37).
SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@([a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com$")


PLAIN_WORKFLOWS = ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml")
GITHUB_ENVIRONMENT = re.compile(r"^staging(-[a-z][a-z0-9-]{1,19}[a-z0-9])?$")
# Where a root's outputs may be published: a staging environment, or production-<service> of the platform repository.
PUBLISHED_ENVIRONMENT = re.compile(r"^(staging(-[a-z][a-z0-9-]{1,19}[a-z0-9])?|production-[a-z][a-z0-9-]{1,19}[a-z0-9])$")


CENTRAL_WORKFLOWS = ("deploy.yml", "recover.yml", "rightsize.yml")  # the platform repository's own pipeline (docs/ROADMAP.md D33)
# Production (docs/ROADMAP.md D37): every promotion runs the platform repository's promote.yml in production-<service>,
# whose required reviewer is the approval; nothing else may sign in as a production deploy identity.
PRODUCTION_WORKFLOWS = ("promote.yml",)
PRODUCTION_SLO_GOAL = 0.995  # D39: 99.5 percent of requests without a server error over 30 days
PRODUCTION_UPTIME_REGIONS = ("USA_OREGON", "USA_IOWA", "USA_VIRGINIA")
PRODUCTION_ROOTS = "infra/gcp/apps-production"


def app_root(name, *, project_id, region, state_bucket, github_repository, github_repository_id, github_owner_id,
             bootstrap_image, health_path="/health", environment="staging", settings=None, needs=None, exposure="authenticated", viewers=(),
             github_environment="staging", workflow_files=PLAIN_WORKFLOWS, pipeline="repository", promotion_readers=()):
    """The three files of an application's Terraform root, with every input a literal so a reviewer sees the truth.
    `pipeline`: repository when the application's own workflows release it (github_repository is the application's);
    platform when the platform repository's pipeline does (github_repository and the ids are the platform's, the
    environment is staging-<service> there, and the workflow files are deploy.yml, recover.yml and rightsize.yml).
    `promotion_readers`: in a staging root, the application's production deployer, which may read the images staging
    released and check their provenance to promote them (docs/ROADMAP.md D37)."""
    checks = [
        (NAME, name, "application name"), (PROJECT, project_id, "project id"), (BUCKET, state_bucket, "state bucket"),
        (REPOSITORY, github_repository, "GitHub repository"), (DIGITS, github_repository_id, "GitHub repository id"),
        (DIGITS, github_owner_id, "GitHub owner id"), (IMAGE, bootstrap_image, "bootstrap image"),
        (HEALTH_PATH, health_path or "/", "health path"), (ENVIRONMENT, environment, "environment"),
    ]
    for pattern, value, what in checks:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"Invalid {what}: {value!r}")
    if region not in ALLOWED_LOCATIONS:
        raise ValueError(f"Region {region!r} is outside the platform policy ({', '.join(ALLOWED_LOCATIONS)})")
    if exposure not in EXPOSURES:
        raise ValueError(f"exposure must be one of {', '.join(EXPOSURES)}; public access is never an option")
    viewers = sorted(set(viewers))
    for viewer in viewers:
        if not isinstance(viewer, str) or not VIEWER.fullmatch(viewer):
            raise ValueError(f"Viewer {viewer!r} must be user:EMAIL, group:EMAIL or domain:DOMAIN")
    if exposure == "lab-members" and not viewers:
        raise ValueError("exposure: lab-members needs at least one viewer (onboarding.default_viewers or the application's viewers)")
    promotion_readers = sorted(set(promotion_readers))
    for reader in promotion_readers:
        match = SERVICE_ACCOUNT.fullmatch(reader) if isinstance(reader, str) else None
        if match is None or match.group(1) == project_id:
            raise ValueError(f"A promotion reader is another project's service account (NAME@PROJECT.iam.gserviceaccount.com), not {reader!r}")
    if promotion_readers and environment == "production":
        raise ValueError("Only a staging root grants promotion readers: production reads nothing from another project")
    # A further application of a repository runs in its own GitHub environment with its own workflow files (D24); the
    # deploy identity's federation trusts exactly those, so the repository's other service cannot release this one.
    if pipeline not in ("repository", "platform"):
        raise ValueError("pipeline is repository or platform")
    suffix = "" if github_environment == "staging" else github_environment.removeprefix("staging")
    if environment == "production":
        # Promotions only: the platform repository's promote.yml in production-<service>, never an application's own workflows.
        if pipeline != "platform" or github_environment != f"production-{name}" or tuple(workflow_files) != PRODUCTION_WORKFLOWS:
            raise ValueError("A production root trusts the platform repository's promote.yml in the environment production-<service>")
        suffix = f"-{name}"
    elif pipeline == "platform":
        # Every application the platform pipeline releases runs in its own environment of the platform repository.
        if github_environment != f"staging-{name}" or tuple(workflow_files) != CENTRAL_WORKFLOWS:
            raise ValueError("The platform pipeline runs in the environment staging-<service> of the platform repository with deploy.yml, recover.yml and rightsize.yml")
    else:
        expected = tuple(f"{kind}-gcp{suffix}.yml" for kind in ("deploy", "recover", "rightsize"))
        if not GITHUB_ENVIRONMENT.fullmatch(github_environment) or (suffix and suffix != f"-{name}") or tuple(workflow_files) != expected:
            raise ValueError("The GitHub environment and workflow files are staging with the plain names, or staging-<service> with names suffixed by the service")
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
    needs = {"buckets": [], "topics": [], "database": "none", "migrate": [], "database_auth": "iam", **(needs or {})}
    if set(needs) != {"buckets", "topics", "database", "migrate", "database_auth"} or needs["database"] not in ("none", "firestore", "postgres"):
        raise ValueError("needs must hold buckets, topics, database (none, firestore or postgres), migrate and database_auth")
    if needs["database_auth"] not in ("iam", "password") or (needs["database_auth"] == "password" and needs["database"] != "postgres"):
        raise ValueError("database_auth is iam or password, and password applies to database: postgres only")
    password = needs["database_auth"] == "password"
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
    random_provider = ("\n    random = {\n      source  = \"hashicorp/random\"\n      version = \"~> 3.7\"\n    }" if password else "")
    backend = header + f'''terraform {{
  required_version = ">= 1.6, < 2.0"
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 7.0"
    }}{random_provider}
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
        "health_path": health_path or "/",
    }
    if health_path is None:  # no health route (docs/ROADMAP.md 13.4): the uptime check accepts any answer; other roots stay as they were
        inputs["health_probe"] = False
    inputs.update(merged)
    inputs["exposure"] = exposure
    inputs["viewers"] = viewers
    if promotion_readers:  # only a root whose application has a production carries it; the others stay as they were
        inputs["promotion_readers"] = promotion_readers
    if environment == "production":
        # Production's reliability (docs/ROADMAP.md D39): one objective with its burn-rate alerts, and the uptime check every
        # minute from three US regions; staging keeps the module's defaults.
        inputs.update(slo_goal=PRODUCTION_SLO_GOAL, uptime_period="60s", uptime_regions=list(PRODUCTION_UPTIME_REGIONS))
    if suffix:  # only a further application of a repository carries these; the first keeps the module's defaults
        inputs["github_environment"] = github_environment
        inputs["workflow_files"] = list(workflow_files)
    width = max(len(key) for key in list(inputs) + ["image_vulnerability_scanning"])
    lines = [f"  {key.ljust(width)} = {_hcl(value)}" for key, value in inputs.items()]
    auth = ', database_auth = "password"' if password else ""
    declared = f"buckets = {_hcl(needs['buckets'])}, topics = {_hcl(needs['topics'])}, database = {_hcl(needs['database'])}, migrate = {_hcl(needs['migrate'])}"
    lines.append(f"  {'needs'.ljust(width)} = {{ {declared}{auth} }}")
    if password:
        # Generated in every run and never stored: the module writes it to Cloud SQL and to the DATABASE_URL secret through
        # write-only arguments, once (their version number pins the first value), so neither state nor plan ever holds it.
        lines.append(f"  {'database_password'.ljust(width)} = ephemeral.random_password.database.result")
    shared = {
        "project_number": "project_number", "image_vulnerability_scanning": "image_vulnerability_scanning",
        "tag_manager_role": "tag_manager_role", "alert_channels": "alert_channels", "cap_service_account": "cap_service_account",
        "billing_dataset": "billing_dataset", "network": "network", "subnet": "subnet", "cap_operator_role": "cap_operator_role",
    }
    lines += [f"  {key.ljust(width)} = data.terraform_remote_state.project.outputs.{output}" for key, output in shared.items()]
    # Added in 1.7.0: a foundation applied before then has no such output, and an empty id means "no IAP client yet",
    # so an application that is not `lab-members` plans cleanly before the operator re-applies the foundation.
    lines.append(f"  {'iap_client_id'.ljust(width)} = try(data.terraform_remote_state.project.outputs.iap_client_id, \"\")")
    # Added in 1.8.6, read the same way: until the foundation has its key ring the app has no signing key, and a private
    # repository's release says so when it tries to sign.
    lines.append(f"  {'kms_key_ring'.ljust(width)} = try(data.terraform_remote_state.project.outputs.kms_key_ring, \"\")")
    ephemeral = ('''
# The database password for database_auth: password; see the module input database_password below.
ephemeral "random_password" "database" {
  length  = 32
  special = false
}
''' if password else "")
    main = header + f'''provider "google" {{
  project = {_hcl(project_id)}
  region  = {_hcl(region)}
}}

{ephemeral}# Project-wide values (identities, channels, roles) come from the foundation root's state, read-only.
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
    # A further application of a repository names its own GitHub environment, where the pipeline publishes its variables.
    environment_output = f'output "github_environment" {{ value = {_hcl(github_environment)} }}\n' if suffix else ""
    outputs = header + f'''output "github_repository" {{ value = {_hcl(github_repository)} }}
{environment_output}output "github_environment_variables" {{ value = module.service.github_environment_variables }}
output "service_url" {{ value = module.service.service_url }}
output "console_url" {{ value = module.service.console_url }}
output "dashboard_url" {{ value = module.service.dashboard_url }}
output "runtime_alerts" {{ value = module.service.runtime_alerts }}
'''
    return {"backend.tf": backend, "main.tf": main, "outputs.tf": outputs}


def roots(changed, apps_dir):
    """Which application roots of `apps_dir` (infra/gcp/apps for staging, infra/gcp/apps-production for production) a set
    of changed paths affects: a module or pipeline change affects every root."""
    directory = Path(apps_dir)
    names = sorted(p.name for p in directory.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if directory.is_dir() else []
    if any(path.startswith(("infra/gcp/modules/", ".github/workflows/infra-")) for path in changed):
        return names
    prefix = ["infra", "gcp", "apps-production" if directory.name == "apps-production" else "apps"]
    touched = set()
    for path in changed:
        parts = path.split("/")
        if parts[:len(prefix)] == prefix and len(parts) > len(prefix) and parts[len(prefix)] in names:
            touched.add(parts[len(prefix)])
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
    "roles/iap.httpsResourceAccessor",
    # The release reads its image's vulnerability findings (the scan summary); the service module grants it to the deployer.
    "roles/containeranalysis.occurrences.viewer",
    # The deployer signs and checks its own application's build provenance; the service module grants both on that one key.
    "roles/cloudkms.signerVerifier", "roles/cloudkms.viewer",
    # The production deployer checks a staging image's provenance before promoting it: the key's public half only (D37).
    "roles/cloudkms.publicKeyViewer",
})
CUSTOM_ROLE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/labDeploy[A-Za-z]+$")
# The one grant to a platform identity an application root makes by design: the spending cap may read and update the
# application's own Cloud Run service through the foundation's cap-operator role, nothing more and nowhere else.
CAP_OPERATOR_ROLE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/roles/labDeployCapOperator$")
CAP_IDENTITY = "serviceAccount:lab-deploy-spending-cap@"
STATE_BUCKET = re.compile(r"-tfstate$")
# Buckets whose old object versions are what makes them useful: a release's recovery checkpoint and the controller's backups.
VERSIONED_BUCKET = re.compile(r"-(recovery|lab-deploy-backups)$")
ENCRYPTED_ONLY = ("ENCRYPTED_ONLY", "TRUSTED_CLIENT_CERTIFICATE_REQUIRED")
# Resources whose secret material lands in Terraform's state and in every plan: never in an application root. Their
# write-only forms (a `_wo` argument, an ephemeral resource) keep the value out of both and are what a root must use.
SECRET_IN_STATE = {
    "google_secret_manager_secret_version": "secret_data",
    "google_sql_user": "password",
    "random_password": None,
    "tls_private_key": None,
    "google_kms_secret_ciphertext": "plaintext",
}


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
        if touching and (kind in ("google_compute_router_nat", "google_compute_router") or (kind == "google_compute_route" and "internet" in str(after.get("next_hop_gateway") or ""))):
            # docs/ROADMAP.md 14.5: code nobody has reviewed is contained because the lab's network has no way to the internet.
            add("no_internet_gateway", address, "the lab's network has no NAT, router or route to the internet: applications with private egress rely on it")
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
            if kind == "google_kms_crypto_key":
                ring = re.fullmatch(r"projects/[^/]+/locations/([^/]+)/keyRings/[^/]+", after.get("key_ring") or "")
                if _unknown(change, "key_ring") or ring is None or ring.group(1).lower() not in allowed_locations:
                    add("region_policy", address, f"key_ring {after.get('key_ring')!r} is outside {', '.join(allowed_locations)}")
            if kind == "google_sql_database_instance":
                if _nested(after, "settings", "ip_configuration", "ipv4_enabled") is not False or _unknown(change, "settings", "ip_configuration", "ipv4_enabled"):
                    add("private_database", address, "databases get a private address only: settings.ip_configuration.ipv4_enabled must be false")
                if after.get("deletion_protection") is not True or _nested(after, "settings", "deletion_protection_enabled") is not True:
                    add("protected_database", address, "databases keep deletion protection on, in Terraform and in Cloud SQL itself "
                        "(deletion_protection and settings.deletion_protection_enabled)")
                if _nested(after, "settings", "backup_configuration", "enabled") is not True or _unknown(change, "settings", "backup_configuration", "enabled"):
                    add("database_backups", address, "databases keep automated backups on: settings.backup_configuration.enabled must be true")
                if _nested(after, "settings", "ip_configuration", "ssl_mode") not in ENCRYPTED_ONLY or _unknown(change, "settings", "ip_configuration", "ssl_mode"):
                    add("encrypted_connections", address, "databases accept encrypted connections only: settings.ip_configuration.ssl_mode must be "
                        "ENCRYPTED_ONLY or TRUSTED_CLIENT_CERTIFICATE_REQUIRED")
            if kind == "google_firestore_database":
                if after.get("delete_protection_state") != "DELETE_PROTECTION_ENABLED":
                    add("protected_database", address, "databases keep delete protection on")
                if after.get("deletion_policy") == "DELETE":
                    add("protected_database", address, "a Firestore database is abandoned, never deleted, when Terraform stops managing it (deletion_policy)")
            if kind == "google_storage_bucket":
                if (after.get("uniform_bucket_level_access") is not True or after.get("public_access_prevention") != "enforced"
                        or _unknown(change, "public_access_prevention")):
                    add("private_bucket", address, "buckets use uniform bucket-level access and enforced public access prevention")
                if after.get("force_destroy") is True or _unknown(change, "force_destroy"):
                    add("no_force_destroy", address, "a bucket is never force-destroyed: force_destroy would delete every object with it")
                name = after.get("name") or ""
                if VERSIONED_BUCKET.search(name) and _nested(after, "versioning", "enabled") is not True:
                    add("versioned_checkpoints", address, f"{name} keeps object versioning on: recovery reads the checkpoint's earlier versions")
            if kind in SECRET_IN_STATE:
                field = SECRET_IN_STATE[kind]
                if field is None or after.get(field) is not None or _unknown(change, field):
                    add("no_secret_in_state", address, "secret material would be stored in Terraform's state and printed in every plan; use the "
                        "write-only argument (secret_data_wo, password_wo) or an ephemeral resource instead")
            if kind in LABELLED_TYPES:
                labels = (_nested(after, "settings", "user_labels") if kind == "google_sql_database_instance" else after.get("labels")) or {}
                if not isinstance(labels, dict) or "lab-deploy-app" not in labels:
                    add("labels_present", address, "every managed resource carries the lab-deploy-app label for cost allocation and the spending cap")
    return violations


SERIOUS_DRIFT = ("iam", "network", "exposure")
EXPOSURE_FIELDS = ("ingress", "invoker_iam_disabled", "iap_enabled", "default_uri_disabled", "custom_audiences")
EXPOSURE_ROLES = ("roles/run.invoker", "roles/iap.httpsResourceAccessor")


def drift_category(change):
    """What kind of difference a change of an applied root is: exposure, iam, network, data, monitoring or other."""
    kind = change.get("type", "")
    before, after = change.get("change", {}).get("before") or {}, _after(change)
    if kind.startswith("google_iap_") or (kind.endswith("_iam_member") and (after.get("role") or before.get("role")) in EXPOSURE_ROLES):
        return "exposure"
    if kind == "google_cloud_run_v2_service" and any(before.get(field) != after.get(field) for field in EXPOSURE_FIELDS):
        return "exposure"
    if kind.endswith(("_iam_member", "_iam_binding", "_iam_policy")) or kind.startswith(("google_service_account", "google_iam_", "google_project_iam_")):
        return "iam"
    if kind.startswith(("google_compute_", "google_vpc_access_", "google_service_networking_", "google_dns_")):
        return "network"
    if kind in DATA_TYPES or kind in ("google_pubsub_topic", "google_pubsub_subscription"):
        return "data"
    if kind.startswith("google_monitoring_"):
        return "monitoring"
    return "other"


def drift(plan, app):
    """What a plan of an applied root would change. Each change is a difference between the code on main and what runs:
    an edit made outside the pipeline (drift), or an apply that did not finish."""
    changes = []
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if actions in (["no-op"], ["read"], []):
            continue
        changes.append({"address": change.get("address", "?"), "type": change.get("type", ""), "actions": actions, "category": drift_category(change)})
    return {"app": app, "state": "present", "drifted": bool(changes), "changes": changes,
            "categories": sorted({c["category"] for c in changes}), "serious": [c for c in changes if c["category"] in SERIOUS_DRIFT]}


def drift_report(records, apps, run_url=None, now=None):
    """The weekly drift check's result for every application root; a root without a record means its plan failed."""
    found = {record["app"]: record for record in records if isinstance(record, dict) and record.get("app")}
    return {"checked_at": now or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"), "run_url": run_url,
            "applications": [found[app] for app in sorted(found)],
            "drifted": sorted(app for app, record in found.items() if record.get("drifted")),
            "serious": sorted(app for app, record in found.items() if record.get("serious")),
            "failed": sorted(set(apps) - set(found))}


def drift_issue(report):
    """Markdown for the living drift issue on the platform repository."""
    lines = ["Updated by the weekly drift check (`.github/workflows/infra-drift.yml`), which plans every application root against the live "
             "infrastructure with the read-only identity. A difference is an edit made outside the pipeline, or an apply that did not finish; "
             "merging any change to the root (or dispatching the infrastructure apply) puts the code's version back.", ""]
    for record in report["applications"]:
        if not record.get("drifted"):
            continue
        lines.append(f"### `{record['app']}`: {', '.join(record['categories'])}")
        lines += [f"- `{change['address']}` ({change['category']}): {'/'.join(change['actions'])}" for change in record["changes"][:50]]
        lines.append("")
    if report["failed"]:
        lines += ["Plans that did not finish (see the run): " + ", ".join(f"`{app}`" for app in report["failed"]), ""]
    if not report["drifted"] and not report["failed"]:
        lines += ["No drift: every application root matches the live infrastructure.", ""]
    lines.append(f"Checked {report['checked_at']}" + (f" by [this run]({report['run_url']})." if report.get("run_url") else "."))
    return "\n".join(lines) + "\n"


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


# ---- the approval block (docs/ROADMAP.md 12.1): what a reviewer approves, computed from the plan by this code, never
# written by Claude. Approval prompts built from the agent's own description are the most exploited failure of agentic
# systems (docs/RESEARCH-2026-10-14.md, 4.4); this block is built from the resource changes themselves.

# How far a change can be undone, in rising order: data-bearing outranks identity, which outranks reversible.
TIERS = ("reversible", "identity", "data-bearing")
TIER_MEANING = {
    "reversible": "reverting the pull request undoes it; nothing here holds data or grants access",
    "identity": "it changes who or what may act (grants, service accounts, workload identity); a revert removes the access, "
                "not what was done with it meanwhile",
    "data-bearing": "it adds or changes a resource that holds data; the pipeline never destroys one, so a revert cannot take it "
                    "away, and data written meanwhile stays",
}
# What a resource bills every month while nothing uses it, in USD at list price (the idle floor). A copy of the figures in
# labdeploy/cost.py, which this standard-library file cannot import; a test keeps them equal. Every other type costs nothing
# at rest or bills only by use (storage, messages, requests, scans), which the floor does not count.
IDLE_FLOOR_USD_MONTH = {
    # db-f1-micro with 10 GB of HDD, the only size the service module creates: cost.NEEDS_PRICING["postgres"], instance plus storage.
    "google_sql_database_instance": {"db-f1-micro": 8.57},
    # One software key version (the application's provenance key, docs/SETUP.md section 8).
    "google_kms_crypto_key": 0.06,
}
PULL_REQUEST_NUMBER = re.compile(r"^[0-9]{1,10}$")
COMMIT_SHA = re.compile(r"^[a-f0-9]{40}$")
RUN_URL = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+(/attempts/[0-9]+)?$")


def _action(actions):
    """One resource's verb in a plan, counted as `summary` counts it: create, update, delete or replace; None for no change."""
    if actions in (["no-op"], ["read"], []):
        return None
    if actions == ["create"]:
        return "create"
    if actions == ["delete"]:
        return "delete"
    return "replace" if "create" in actions and "delete" in actions else "update"


def tier(change):
    """How far one resource change can be undone: data-bearing (the types the policy never lets the pipeline destroy),
    identity (IAM members and bindings, service accounts, workload identity), or reversible."""
    kind = change.get("type", "")
    if kind in DATA_TYPES:
        return "data-bearing"
    if kind.endswith(("_iam_member", "_iam_binding", "_iam_policy")) or kind.startswith(("google_service_account", "google_iam_", "google_project_iam_")):
        return "identity"
    return "reversible"


def _floor_of(kind, values):
    """The idle floor of one resource in one state (before or after), or None when the table has no price for its size."""
    price = IDLE_FLOOR_USD_MONTH.get(kind, 0.0)
    return price.get(_nested(values, "settings", "tier")) if isinstance(price, dict) else price


def idle_floor(plan):
    """The application root's monthly idle floor before and after the plan, from every resource it manages (unchanged ones too)."""
    before, after, unpriced = 0.0, 0.0, []
    for change in plan.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if actions in (["read"], []):
            continue
        kind, detail = change.get("type", ""), change.get("change", {})
        for exists, values, side in ((actions != ["create"], detail.get("before") or {}, "before"),
                                     (actions not in (["delete"], ["forget"]), detail.get("after") or {}, "after")):
            if not exists:
                continue
            price = _floor_of(kind, values)
            if price is None:
                unpriced.append(change.get("address", "?"))
            elif side == "before":
                before += price
            else:
                after += price
    return {"before_usd_month": round(before, 2), "after_usd_month": round(after, 2), "delta_usd_month": round(after - before, 2),
            "unpriced": sorted(set(unpriced))}


def _shown(value, unknown=False):
    if unknown:
        return "known after apply"
    if value is None or value == [] or value == "":
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)


def exposure_changes(plan):
    """Every change to who can reach an application: Cloud Run ingress, invoker check, Identity-Aware Proxy and URL settings,
    invoker and IAP accessor grants, and any IAP resource."""
    found = []
    for change in plan.get("resource_changes", []):
        verb = _action(change.get("change", {}).get("actions", []))
        if verb is None:
            continue
        kind, address = change.get("type", ""), change.get("address", "?")
        before, after = change.get("change", {}).get("before") or {}, _after(change)
        if kind == "google_cloud_run_v2_service":
            if verb == "delete":
                found.append({"address": address, "action": verb, "detail": "the service and its address are removed"})
                continue
            fields = [field for field in EXPOSURE_FIELDS if before.get(field) != after.get(field) or _unknown(change, field)]
            if fields:
                found.append({"address": address, "action": verb, "detail": "; ".join(
                    f"{field} {_shown(before.get(field))} -> {_shown(after.get(field), _unknown(change, field))}" for field in fields)})
        elif kind.startswith("google_iap_") or (kind.endswith(("_iam_member", "_iam_binding")) and (after.get("role") or before.get("role")) in EXPOSURE_ROLES):
            values = before if verb == "delete" else after
            who = values.get("member") or values.get("members")
            found.append({"address": address, "action": verb,
                          "detail": f"{values.get('role') or kind} for {_shown(who, who is None and _unknown(change, 'member'))}"})
    return found


def identifiers(pull_request=None, head_sha=None, run_url=None):
    """The chain a reviewer can follow from the approval to the pipeline: pull request, head commit, plan run. Values come
    from GitHub through the workflow; anything that does not look like one is refused rather than printed."""
    for pattern, value, what in ((PULL_REQUEST_NUMBER, pull_request, "pull request number"), (COMMIT_SHA, head_sha, "head commit"),
                                 (RUN_URL, run_url, "workflow run URL")):
        if value not in (None, "") and not pattern.fullmatch(str(value)):
            raise ValueError(f"Invalid {what}: {value!r}")
    return {"pull_request": int(pull_request) if pull_request not in (None, "") else None, "head_sha": head_sha or None, "run_url": run_url or None}


def approval(plan, pull_request=None, head_sha=None, run_url=None):
    """The facts of the approval block: counts by action and by resource type, the reversibility tier of each type and of
    the whole plan, the idle floor before and after, every exposure change, and the identifiers. Deterministic: the same
    plan and identifiers always give the same facts."""
    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0}
    by_type, tiers = {}, []
    for change in plan.get("resource_changes", []):
        verb = _action(change.get("change", {}).get("actions", []))
        if verb is None:
            continue
        level = tier(change)
        counts[verb] += 1
        row = by_type.setdefault(change.get("type", "?"), {"create": 0, "update": 0, "delete": 0, "replace": 0, "tier": level})
        row[verb] += 1
        tiers.append(level)
    overall = max(tiers, key=TIERS.index) if tiers else "reversible"
    return {"counts": counts, "by_type": dict(sorted(by_type.items())), "tier": overall, "tier_meaning": TIER_MEANING[overall],
            "idle_floor": idle_floor(plan), "exposure": exposure_changes(plan), "identifiers": identifiers(pull_request, head_sha, run_url)}


def _cell(text):
    """A plan value inside a table cell: code-formatted, and unable to break out of its cell or its code span."""
    return "`" + str(text).replace("`", "'").replace("|", "\\|").replace("\n", " ") + "`"


def _usd(value):
    return f"-${-value:.2f}" if value < 0 else f"${value:.2f}"


def approval_block(facts):
    """The fixed Markdown block a reviewer reads first: the same rows in the same order for every plan."""
    counts, floor, ids = facts["counts"], facts["idle_floor"], facts["identifiers"]
    delta = floor["delta_usd_month"]
    exposure = facts["exposure"]
    rows = [
        ("Resources", f"{counts['create']} to add, {counts['update']} to change, {counts['delete']} to destroy, {counts['replace']} to replace."),
        ("Reversibility", f"**{facts['tier']}**: {facts['tier_meaning']}"),
        ("Idle floor per month", f"{_usd(floor['before_usd_month'])} before, {_usd(floor['after_usd_month'])} after "
                                 f"({'+' if delta > 0 else ''}{_usd(delta)}; list prices, usage not included)"
         + (f"; no price for {', '.join(_cell(a) for a in floor['unpriced'])}" if floor["unpriced"] else "")),
        ("Exposure", f"**{len(exposure)} change{'' if len(exposure) == 1 else 's'}**, listed below" if exposure else "no change to who can reach the application"),
        ("Pull request", f"#{ids['pull_request']}" if ids["pull_request"] else "not given"),
        ("Head commit", f"`{ids['head_sha']}`" if ids["head_sha"] else "not given"),
        ("Plan run", ids["run_url"] or "not given"),
    ]
    text = ["#### What merging this changes", "",
            "Rendered by the pipeline from the Terraform plan (`labdeploy/infra.py`), not written by Claude.", "",
            "| | |", "|---|---|"] + [f"| {name} | {value} |" for name, value in rows]
    if facts["by_type"]:
        text += ["", "| Resource type | Add | Change | Destroy | Replace | Reversibility |", "|---|---|---|---|---|---|"]
        text += [f"| {_cell(kind)} | {row['create']} | {row['update']} | {row['delete']} | {row['replace']} | {row['tier']} |"
                 for kind, row in facts["by_type"].items()]
    if exposure:
        verbs = {"create": "add", "update": "change", "delete": "destroy", "replace": "replace"}
        text += ["", "| Exposure change | Resource | What |", "|---|---|---|"]
        text += [f"| {verbs[item['action']]} | {_cell(item['address'])} | {_cell(item['detail'])} |" for item in exposure[:50]]
        if len(exposure) > 50:
            text.append(f"| … | {len(exposure) - 50} more | see the full plan |")
    return "\n".join(text) + "\n"


COMMENT_LIMIT = 60_000  # GitHub refuses comments over 65,536 characters; the folded full plan gives way first


def comment(app, plan, violations, plan_text=None, pull_request=None, head_sha=None, run_url=None):
    """Markdown for the pull request: the approval block first, then every resource change, the full plan folded away, and
    whether policy allows it."""
    digest = summary(plan)
    head = (f"<!-- lab-deploy-infra:{app} -->\n### Infrastructure plan for `{app}`\n\n"
            + approval_block(approval(plan, pull_request, head_sha, run_url)))
    if digest["lines"]:
        body = "\n```\n" + "\n".join(digest["lines"][:200]) + ("\n…" if len(digest["lines"]) > 200 else "") + "\n```\n"
    else:
        body = "\nNo changes.\n"
    if violations:
        verdict = "\n**Policy: blocked.** The apply job refuses this plan until these are resolved:\n\n" + "\n".join(
            f"- `{v['address']}`: {v['detail']} ({v['rule']})" for v in violations) + "\n"
    else:
        verdict = "\n**Policy: passed.** Merging applies exactly this plan.\n"
    if plan_text and digest["lines"]:
        room = max(0, min(50_000, COMMENT_LIMIT - len(head) - len(body) - len(verdict) - 200))
        text = plan_text if len(plan_text) <= room else plan_text[:room] + "\n… (truncated; the full plan is in the workflow log)"
        body += "\n<details><summary>Full plan with every attribute</summary>\n\n```\n" + text.replace("```", "'''") + "\n```\n</details>\n"
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
        environment = (outputs.get("github_environment") or {}).get("value", environment)  # staging-<service> for a further app of a repository
    except KeyError as missing:
        raise ValueError(f"The root's outputs lack {missing}; render the root again with the current platform and merge it") from None
    if not REPOSITORY.fullmatch(repository) or not PUBLISHED_ENVIRONMENT.fullmatch(environment):
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


def _options(argv, names):
    """Take `--name value` pairs out of an argument list; the positional arguments keep their order."""
    found, rest, items = {}, [], iter(argv)
    for item in items:
        if item.startswith("--") and item[2:] in names:
            found[item[2:]] = next(items, "")
        else:
            rest.append(item)
    return found, rest


def main(argv=None):
    options, argv = _options(list(sys.argv[1:] if argv is None else argv), ("pull-request", "head-sha", "run-url"))
    usage = ("usage: python -m labdeploy.infra check PLAN.json | summary PLAN.json APP [PLAN.txt] [--pull-request N] [--head-sha SHA] [--run-url URL]"
             " | roots CHANGED.txt APPS_DIR | roots-all APPS_DIR | publish OUTPUTS.json | drift PLAN.json APP"
             " | drift-report RECORDS_DIR APPS_JSON [RUN_URL] | drift-issue REPORT.json")
    if len(argv) < 2 or argv[0] not in ("check", "summary", "roots", "roots-all", "publish", "drift", "drift-report", "drift-issue"):
        print(usage, file=sys.stderr)
        return 1
    if argv[0] == "roots":
        # --output NAME names the line (production for the production roots), so one job can list both kinds.
        options, rest = _options(argv[1:], ("output",))
        changed = [line.strip() for line in Path(rest[0]).read_text().splitlines() if line.strip()]
        output = options.get("output", "apps")
        if not re.fullmatch(r"[a-z]+", output):
            print("--output is a lowercase word", file=sys.stderr)
            return 1
        print(f"{output}=" + json.dumps(roots(changed, rest[1] if len(rest) > 1 else "infra/gcp/apps")))
        return 0
    if argv[0] == "roots-all":
        options, rest = _options(argv[1:], ("output",))
        directory = Path(rest[0])
        names = sorted(p.name for p in directory.iterdir() if p.is_dir() and NAME.fullmatch(p.name)) if directory.is_dir() else []
        output = options.get("output", "apps")
        if not re.fullmatch(r"[a-z]+", output):
            print("--output is a lowercase word", file=sys.stderr)
            return 1
        print(f"{output}=" + json.dumps(names))
        return 0
    if argv[0] == "drift-report":
        records = [json.loads(path.read_text()) for path in sorted(Path(argv[1]).glob("*.json"))] if Path(argv[1]).is_dir() else []
        print(json.dumps(drift_report(records, json.loads(argv[2]), argv[3] if len(argv) > 3 else None), indent=2))
        return 0
    document = json.loads(Path(argv[1]).read_text())
    if argv[0] == "check":
        violations = check_plan(document)
        for violation in violations:
            print(f"{violation['rule']}: {violation['address']}: {violation['detail']}")
        print(f"{len(violations)} violation(s)")
        return 2 if violations else 0
    if argv[0] == "drift":
        if not ROOT_LABEL.fullmatch(argv[2] if len(argv) > 2 else ""):
            print("drift needs the application's root name (production-<name> for a production root)", file=sys.stderr)
            return 1
        print(json.dumps(drift(document, argv[2]), indent=2))
        return 0
    if argv[0] == "drift-issue":
        print(drift_issue(document), end="")
        return 0
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
    try:
        text = comment(app, document, check_plan(document), plan_text, pull_request=options.get("pull-request") or None,
                       head_sha=options.get("head-sha") or None, run_url=options.get("run-url") or None)
    except ValueError as exc:
        print(f"Comment not written: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
