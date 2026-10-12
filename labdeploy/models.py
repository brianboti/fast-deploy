import hashlib
import json
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RESERVED_ENV = {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "PATH", "HOME", "PYTHONPATH", "GOOGLE_APPLICATION_CREDENTIALS"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Resources(StrictModel):
    cpu: int = Field(default=1, ge=1, le=4)
    memory_mib: int = Field(default=512, ge=512, le=4096)


RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
# The foundation's VPC and the subnet Cloud Run uses for private egress; a Cloud SQL instance is reachable only through them.
PLATFORM_NETWORK = "lab-deploy"
PLATFORM_SUBNET = "lab-deploy-run"


class Needs(StrictModel):
    """What the application needs besides its container. Names are short; the platform derives the real resource names."""
    buckets: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    # firestore: a named Native-mode database, free while small. postgres: the smallest Cloud SQL instance with a private
    # address and IAM sign-in only; it bills around the clock (about 9 dollars a month), so declare it only when SQL is needed.
    database: Literal["none", "firestore", "postgres"] = "none"
    # A command run as a Cloud Run job from the release's image before the candidate gets traffic (schema migrations).
    migrate: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if len(self.migrate) > 16:
            raise ValueError("migrate takes at most 16 arguments")
        for part in self.migrate:
            if not 1 <= len(part) <= 200 or not part.isprintable() or any(c in part for c in ",$^\""):
                raise ValueError("each migrate argument is printable, at most 200 characters, and contains no comma, dollar sign, caret or double quote")
        for kind, names in (("buckets", self.buckets), ("topics", self.topics)):
            if len(names) > 8:
                raise ValueError(f"At most 8 {kind}")
            if len(set(names)) != len(names):
                raise ValueError(f"{kind} must be unique")
            for name in names:
                if not RESOURCE_NAME.fullmatch(name):
                    raise ValueError(f"'{name}' is not a valid {kind[:-1]} name (lowercase letters, digits and dashes, 2 to 21 characters)")
        return self


def variable_name(need):
    return need.upper().replace("-", "_")


def needs_env(spec, project_id, region="us-central1"):
    """The variables the platform sets for declared needs: fixed conventions, so no output has to travel anywhere.

    BUCKET_<NAME> = <project>-<service>-<name>; TOPIC_<NAME> = projects/<project>/topics/<service>-<name>;
    SUBSCRIPTION_<NAME> = projects/<project>/subscriptions/<service>-<name>; FIRESTORE_DATABASE = <service>;
    for postgres: DATABASE_INSTANCE = <project>:<region>:<service> (connection name), DATABASE_NAME = app,
    DATABASE_USER = <service>-runtime@<project>.iam (IAM sign-in, no password), DATABASE_IP_TYPE = private.
    """
    env = {}
    for name in spec.needs.buckets:
        env[f"BUCKET_{variable_name(name)}"] = f"{project_id}-{spec.name}-{name}"
    for name in spec.needs.topics:
        env[f"TOPIC_{variable_name(name)}"] = f"projects/{project_id}/topics/{spec.name}-{name}"
        env[f"SUBSCRIPTION_{variable_name(name)}"] = f"projects/{project_id}/subscriptions/{spec.name}-{name}"
    if spec.needs.database == "firestore":
        env["FIRESTORE_DATABASE"] = spec.name
    if spec.needs.database == "postgres":
        env.update({"DATABASE_INSTANCE": f"{project_id}:{region}:{spec.name}", "DATABASE_NAME": "app",
                    "DATABASE_USER": f"{spec.name}-runtime@{project_id}.iam", "DATABASE_IP_TYPE": "private"})
    return env


class Scaling(StrictModel):
    minimum: int = Field(default=0, ge=0, le=1)
    maximum: int = Field(default=2, ge=1, le=5)


class AppSpec(StrictModel):
    api_version: Literal["lab/v1"] = "lab/v1"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,19}[a-z0-9]$")  # 3 to 21 characters: what Cloud Run, the registry and the root accept
    runtime: Literal["python-http"] = "python-http"
    port: int = Field(default=8080, ge=1024, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    resources: Resources = Field(default_factory=Resources)
    scaling: Scaling = Field(default_factory=Scaling)
    concurrency: int = Field(default=80, ge=1, le=1000)
    exposure: Literal["authenticated"] = "authenticated"
    state: Literal["stateless"] = "stateless"
    dependencies: list[str] = Field(default_factory=list)
    gpu: bool = False
    # Plain configuration values set on every revision; secrets are names of Secret Manager secrets called <service>-NAME.
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    # "none" declares that no automated tests run before a release; every plan then carries a warning.
    tests: Literal["required", "none"] = "required"
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    # Buckets, topics and a database the platform provisions for the app (docs/SETUP.md 6b); variables are derived.
    needs: Needs = Field(default_factory=Needs)
    reviewed: bool = False

    @model_validator(mode="after")
    def compatible(self):
        if self.scaling.minimum > self.scaling.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if self.resources.memory_mib % 256:
            raise ValueError("memory_mib must be a multiple of 256")
        if len(self.env) > 32 or len(self.secrets) > 16:
            raise ValueError("At most 32 env values and 16 secrets")
        for name in list(self.env) + list(self.secrets):
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f"'{name}' is not a valid variable name (uppercase letters, digits, underscores)")
            if name in RESERVED_ENV:
                raise ValueError(f"'{name}' is set by Cloud Run or the platform and cannot be overridden")
        for name, value in self.env.items():
            if len(value) > 1024 or "\n" in value:
                raise ValueError(f"env value for {name} must be a single line of at most 1024 characters")
        if len(set(self.secrets)) != len(self.secrets) or set(self.secrets) & set(self.env):
            raise ValueError("secrets must be unique and must not repeat an env name")
        derived = needs_env(self, "project")
        taken = set(self.env) | set(self.secrets)
        for name in derived:
            if name in taken:
                raise ValueError(f"'{name}' is set by the platform for a declared need and cannot be an env or secret name")
        if len(derived) + len(self.env) > 48:
            raise ValueError("Too many variables: needs, env and secrets together must stay under 48")
        return self


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("YAML requires unique string keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def parse_spec(text: str) -> AppSpec:
    if len(text.encode()) > 32_768:
        raise ValueError("platform.yaml exceeds 32 KiB")
    return AppSpec.model_validate(yaml.load(text, Loader=UniqueLoader))  # noqa: S506 - UniqueLoader subclasses SafeLoader


def spec_hash(spec: AppSpec) -> str:
    # Defaults are left out, so adding an optional field to the schema does not change the hash of a spec that does not use it.
    return hashlib.sha256(json.dumps(spec.model_dump(exclude_defaults=True), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enforce(spec: AppSpec, cloud="gcp", environment="staging") -> None:
    if cloud != "gcp" or environment != "staging":
        raise ValueError("MVP supports GCP staging only")
    if not spec.reviewed:
        raise ValueError("Review assumptions and merge reviewed: true in platform.yaml first")
    if spec.dependencies or spec.gpu:
        raise ValueError("Database/dependency provisioning and GPUs are not implemented")
    if spec.resources.cpu > 2 or spec.resources.memory_mib > 2048 or spec.scaling.maximum > 3:
        raise ValueError("Staging policy: maximum 2 CPUs, 2048 MiB, 3 instances")

