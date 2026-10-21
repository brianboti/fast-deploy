import datetime
import hashlib
import json
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RESERVED_ENV = {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "PATH", "HOME", "PYTHONPATH", "GOOGLE_APPLICATION_CREDENTIALS"}


SERVICE_PATH = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Resources(StrictModel):
    cpu: int = Field(default=1, ge=1, le=4)
    memory_mib: int = Field(default=512, ge=512, le=4096)


RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
# A host name of the lab's own for production (docs/ROADMAP.md 16.6): at least two lowercase labels of letters, digits and
# inner dashes, the last starting with a letter; no wildcard and no address. labdeploy/infra.py keeps the same pattern.
DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
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
    # How a postgres database is signed in to. iam (the default): the runtime identity is the user, no password exists
    # anywhere. password: a built-in user whose generated password reaches the app only as the secret DATABASE_URL, for
    # code that takes a connection URL; Terraform writes it through write-only arguments, so it is never in its state.
    database_auth: Literal["iam", "password"] = "iam"
    # The postgres database in production (docs/ROADMAP.md D39); staging always runs the smallest. standard: one zone,
    # shared core, no SLA (about 9 dollars a month). ha: a standby in a second zone, still shared core and no SLA (about 17).
    # sla: a standby and one dedicated CPU, the shape Google's SLA covers (about 100). Each tier's floor is in the approval
    # block of every promotion.
    database_tier: Literal["standard", "ha", "sla"] = "standard"
    # Read-only mounts of the app's own buckets (docs/ROADMAP.md D47, 19.1): a declared bucket's name -> the absolute path
    # where the code reads it. Cloud Run mounts it through Cloud Storage FUSE in the second-generation environment, so files
    # read at a fixed path (a corpus, a model, an index) live in the bucket, never in the repository or the image.
    mounts: dict[str, str] = Field(default_factory=dict)
    # Model APIs the app calls through Google Cloud (D47, 19.3). claude: Anthropic's Claude on Vertex AI, reached on the
    # global endpoint through Private Google Access, so an app with private egress needs no internet; its runtime identity
    # may call models and nothing else, and the calls bill per token to the project, inside its budget and cap.
    models: list[Literal["claude"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if self.database_auth == "password" and self.database != "postgres":
            raise ValueError("database_auth: password applies to database: postgres only")
        if self.database_tier != "standard" and self.database != "postgres":
            raise ValueError("database_tier applies to database: postgres only")
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
        if len(set(self.models)) != len(self.models):
            raise ValueError("models must be unique")
        paths = []
        for bucket, path in self.mounts.items():
            if bucket not in self.buckets:
                raise ValueError(f"mounts names {bucket!r}, which is not under needs.buckets: declare the bucket, then mount it")
            if not isinstance(path, str) or not MOUNT_PATH.fullmatch(path) or len(path) > 200:
                raise ValueError(f"The mount path {path!r} is an absolute path of letters, digits, dots, dashes and underscores, each part "
                                 "starting with a letter, digit or underscore (/app/data/index)")
            if path in MOUNT_SHADOWED or any(path == root or path.startswith(root + "/") for root in MOUNT_REFUSED_TREES):
                raise ValueError(f"A mount at {path} would hide what the container itself needs there; mount below the app's own folder "
                                 "(/app/data/index) or a folder of its own (/models)")
            paths.append(path)
        for one in paths:
            for other in paths:
                if one != other and (other + "/").startswith(one + "/"):
                    raise ValueError(f"The mounts at {one} and {other} overlap: give each bucket a folder of its own")
        if len(set(paths)) != len(paths):
            raise ValueError("Two buckets cannot be mounted at the same path")
        return self


# A bucket mount's path: absolute, one to eight parts, each starting with a letter, digit or underscore, so it carries no
# '.' or '..', and nothing gcloud's KEY=VALUE lists would misread (no comma, equals sign or semicolon).
MOUNT_PATH = re.compile(r"(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}){1,8}")
# Mounted over, these would hide what any container needs: the platform's working folder, home and run-time folders. Cloud
# Run itself refuses /dev, /proc and /sys; the system trees hold the image's own programs and libraries.
MOUNT_SHADOWED = frozenset({"/app", "/home", "/root", "/run", "/var", "/tmp"})  # noqa: S108 - paths a mount may not hide, never opened
MOUNT_REFUSED_TREES = ("/dev", "/proc", "/sys", "/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64")


def variable_name(need):
    return need.upper().replace("-", "_")


def needs_env(spec, project_id, region="us-central1"):
    """The variables the platform sets for declared needs: fixed conventions, so no output has to travel anywhere.

    BUCKET_<NAME> = <project>-<service>-<name>; TOPIC_<NAME> = projects/<project>/topics/<service>-<name>;
    SUBSCRIPTION_<NAME> = projects/<project>/subscriptions/<service>-<name>; FIRESTORE_DATABASE = <service>;
    for postgres: DATABASE_INSTANCE = <project>:<region>:<service> (connection name), DATABASE_NAME = app,
    DATABASE_USER = <service>-runtime@<project>.iam (IAM sign-in, no password), DATABASE_IP_TYPE = private;
    for models: [claude], ANTHROPIC_VERTEX_PROJECT_ID = <project> and CLOUD_ML_REGION = global, which the Anthropic SDK's
    Vertex client reads (the global endpoint is the one Private Google Access reaches).
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
        user = "app" if spec.needs.database_auth == "password" else f"{spec.name}-runtime@{project_id}.iam"
        env.update({"DATABASE_INSTANCE": f"{project_id}:{region}:{spec.name}", "DATABASE_NAME": "app",
                    "DATABASE_USER": user, "DATABASE_IP_TYPE": "private"})
    if "claude" in spec.needs.models:
        env.update({"ANTHROPIC_VERTEX_PROJECT_ID": project_id, "CLOUD_ML_REGION": "global"})
    # The jobs the service starts (docs/ROADMAP.md D64): JOB_<NAME> = the job's full resource name, for the Cloud Run API.
    for name in spec.jobs:
        env[f"JOB_{variable_name(name)}"] = job_resource(spec, name, project_id, region)
    return env


def job_resource(spec, name, project_id, region="us-central1"):
    return f"projects/{project_id}/locations/{region}/jobs/{spec.name}-{name}"


def job_env(spec, name, project_id, region="us-central1", own_env=True):
    """The variables a job gets (D64): BUCKET_<NAME> for the buckets it was given, the Vertex variables for its models, and
    its own env; nothing of the service's other needs or settings, so a job holds only what it was given."""
    job = spec.jobs[name]
    env = {f"BUCKET_{variable_name(bucket)}": f"{project_id}-{spec.name}-{bucket}" for bucket in sorted(job.buckets)}
    if "claude" in job.models:
        env.update({"ANTHROPIC_VERTEX_PROJECT_ID": project_id, "CLOUD_ML_REGION": "global"})
    if own_env:
        env.update(job.env)
    return env


def job_egress(spec, name):
    """Where a job may connect: private (Google's APIs and the lab's network, no internet) unless its contract says internet."""
    return spec.jobs[name].egress or "private"


def needs_secrets(spec):
    """Secrets the platform itself creates for a declared need, by variable name: the Secret Manager secret <service>-NAME.
    database_auth: password -> DATABASE_URL, a postgresql:// URL with the generated password, over the private address."""
    if spec.needs.database == "postgres" and spec.needs.database_auth == "password":
        return {"DATABASE_URL": f"{spec.name}-DATABASE_URL"}
    return {}


# A start command the build can pass on as it is: one line of words, `$PORT` allowed, no quotes or shell operators other
# than the parentheses of a WSGI factory (gunicorn's "module:create_app()"), which the buildpacks' shell would misread.
# Words, $PORT, and && between commands (a chain runs under the init of the platform's images, D49).
ENTRYPOINT = r"^[A-Za-z0-9_./:=,+@%${}() &-]+$"

# The Node.js lines a node-http service may run on (docs/ROADMAP.md D41): the Active LTS lines, each refused from its end of
# life (Node's release schedule, read 2026-10-19; the dates are in docs/RUNBOOK.md). A drafted contract names the newest
# line in Active LTS on the day it is drafted.
NODE_END_OF_LIFE = {"22": datetime.date(2027, 4, 30), "24": datetime.date(2028, 4, 30), "26": datetime.date(2029, 4, 30)}
NODE_ACTIVE_LTS_FROM = {"22": datetime.date(2024, 10, 29), "24": datetime.date(2025, 10, 28), "26": datetime.date(2026, 10, 28)}
# Lines that have reached their end of life (docs/ROADMAP.md D48): a version file that asks for one of these gets the
# lowest supported line and a note naming the date; a Dockerfile's own FROM stays refused. Node's release schedule and
# Python's version status page, read on 2026-10-20.
NODE_ENDED = {"16": datetime.date(2023, 9, 11), "17": datetime.date(2022, 6, 1), "18": datetime.date(2025, 4, 30), "19": datetime.date(2023, 6, 1),
              "20": datetime.date(2026, 4, 30), "21": datetime.date(2024, 6, 1), "23": datetime.date(2025, 6, 1), "25": datetime.date(2026, 6, 1)}
PYTHON_ENDED = {"3.7": datetime.date(2023, 6, 27), "3.8": datetime.date(2024, 10, 7), "3.9": datetime.date(2025, 10, 31), "3.10": datetime.date(2026, 10, 1)}
# What starts a Node.js service is node itself: npm and the other managers add latency and do not pass SIGTERM on (D45).
PACKAGE_MANAGERS = ("npm", "npx", "yarn", "pnpm", "bun", "corepack")
# A package and its exact version, as package.json's allowScripts pins it (npm install-scripts approve): an optional scope,
# a name, an exact version. A name alone, a range or --all is never enough (D43).
SCRIPT_PACKAGE = re.compile(r"((?:@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)")


def today_utc():
    return datetime.datetime.now(datetime.UTC).date()


def default_node(today=None):
    """The newest Node.js line in Active LTS on `today` (UTC): 24 until 2026-10-28, 26 from then (D41)."""
    today = today or today_utc()
    lines = [line for line, start in NODE_ACTIVE_LTS_FROM.items() if start <= today < NODE_END_OF_LIFE[line]]
    return max(lines, key=int) if lines else "24"


class Build(StrictModel):
    """How the image is built.

    context: service (the default), the service's own folder is the Docker build context; repository, the whole repository
    is, for a service in a monorepo that copies shared code from elsewhere in it (the Dockerfile still lives in the
    service's folder and its paths are relative to the repository root).

    recipe (docs/ROADMAP.md D31, 13.3): repository (the default), the service's own Dockerfile, else its Procfile through
    Google's buildpacks; platform, the platform's own Dockerfile, rendered at build time beside a copy of the service's files
    and never committed; buildpacks, Google's buildpacks even when a Dockerfile exists. entrypoint: the command that starts
    the service when the platform makes the image (the platform Dockerfile's command, the buildpacks' GOOGLE_ENTRYPOINT)."""
    context: Literal["service", "repository"] = "service"
    recipe: Literal["repository", "platform", "buildpacks"] = "repository"
    entrypoint: str | None = Field(default=None, max_length=400, pattern=ENTRYPOINT)
    # none: the image builds with no network at all, for a repository that vendors its dependencies (docs/ROADMAP.md 14.3).
    network: Literal["default", "none"] = "default"
    # Node.js only (docs/ROADMAP.md D43): every package installs with its scripts off; afterwards only these run theirs
    # (npm rebuild), each named with its exact version (onnxruntime-node@1.30.0). Nothing else ever runs at install.
    scripts: list[str] = Field(default_factory=list)
    # A repository's own Dockerfile whose final image runs as root (docs/ROADMAP.md D52, 22.3): the platform builds it, then
    # adds a stage on top of it (FROM the built image, USER this number), so the service runs as a user that is not root.
    # Drafted for code nobody has reviewed; a reviewer who accepts root removes it.
    user: int | None = Field(default=None, ge=1000, le=2147483647)
    # runtime: static (D55, 22.4): the folder the build script writes the site into, served by the platform's static image.
    output: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")

    @model_validator(mode="after")
    def coherent(self):
        if len(self.scripts) > 32 or len(set(self.scripts)) != len(self.scripts):
            raise ValueError("build.scripts lists at most 32 packages, each once")
        for entry in self.scripts:
            match = SCRIPT_PACKAGE.fullmatch(entry)
            if not match or len(match.group(1)) > 214:
                raise ValueError(f"'{entry}' in build.scripts is not a package with its exact version (onnxruntime-node@1.30.0): "
                                 "a name alone, a range or --all is never enough")
        if self.context == "repository" and self.recipe == "buildpacks":
            # repository: the service's own Dockerfile, or the platform's image for one package of a workspace (docs/ROADMAP.md 21.4).
            raise ValueError("build.context: repository needs the service's own Dockerfile (build.recipe: repository) or the platform's image (platform)")
        if self.user is not None and self.recipe != "repository":
            raise ValueError("build.user wraps a repository's own Dockerfile (build.recipe: repository); the platform's images already run as 65532")
        if self.recipe == "platform" and not self.entrypoint:
            raise ValueError("build.recipe: platform needs build.entrypoint, the command that starts the service")
        if self.recipe == "repository" and self.entrypoint:
            raise ValueError("build.entrypoint applies to build.recipe: platform or buildpacks; to replace a Dockerfile's start command, use command and args")
        if self.recipe == "buildpacks" and self.entrypoint and re.search(r"[()]", self.entrypoint):
            raise ValueError("The buildpacks run build.entrypoint through a shell, which misreads parentheses; use build.recipe: platform for a factory")
        return self


class Source(StrictModel):
    """Where a contract kept in the platform repository points (docs/ROADMAP.md D31): the application repository, the
    service's folder in it, and the commit that runs. A release is the pull request that moves `commit` (D32)."""
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    path: str = ""
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")

    @model_validator(mode="after")
    def plain_path(self):
        if self.path and (not SERVICE_PATH.fullmatch(self.path) or ".." in self.path.split("/")):
            raise ValueError("source.path is the service's folder inside the repository: relative, no hidden or parent folders")
        return self


class Scaling(StrictModel):
    minimum: int = Field(default=0, ge=0, le=1)
    maximum: int = Field(default=2, ge=1, le=5)


class JobResources(StrictModel):
    """A job's size (docs/ROADMAP.md D67). Cloud Run lets 1 vCPU have up to 4 GiB and 2 vCPUs up to 8 GiB; the lab's policy
    stops at 2 CPUs and 4096 MiB (enforce)."""
    cpu: int = Field(default=1, ge=1, le=4)
    memory_mib: int = Field(default=512, ge=512, le=8192)


# A job's name: the Cloud Run job is <service>-<name> and its identity the account <service>-<name>, so service and job
# together stay within an account id's 30 characters (AppSpec checks the sum). The platform's own identities and the
# migration job keep their names.
JOB_NAME = re.compile(r"^[a-z][a-z0-9-]{0,12}[a-z0-9]$")
RESERVED_JOBS = frozenset({"runtime", "deploy", "probe", "wake", "migrate"})
MAX_JOBS = 4


class Job(StrictModel):
    """A Cloud Run job the service starts itself (docs/ROADMAP.md D64): its own image, identity and limits.

    dockerfile: the job's Dockerfile, relative to the service's folder; the folder holding it is the build context.
    base: an onboarded Node.js application whose test image the platform builds and passes as the build argument BASE (D69).
    One task, never retried, stopped after timeout_minutes; the controller's guard cancels a run beyond runs_per_day (D70).
    buckets: the service's declared buckets this job may read or write; models, secrets and env are its own; egress is
    private (Google's APIs and the lab's network only) unless the contract says internet."""
    dockerfile: str = Field(max_length=200, pattern=SERVICE_PATH.pattern)
    base: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,19}[a-z0-9]$")
    command: list[str] | None = None
    args: list[str] | None = None
    resources: JobResources = Field(default_factory=JobResources)
    timeout_minutes: int = Field(default=10, ge=1, le=60)
    runs_per_day: int = Field(default=30, ge=1, le=30)
    buckets: dict[str, Literal["read", "write"]] = Field(default_factory=dict)
    models: list[Literal["claude"]] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    egress: Literal["private", "internet"] | None = None

    @model_validator(mode="after")
    def valid(self):
        if ".." in self.dockerfile.split("/"):
            raise ValueError("A job's dockerfile is a path inside the service's folder, without parent folders")
        if self.resources.memory_mib % 256:
            raise ValueError("A job's memory_mib must be a multiple of 256")
        if self.resources.memory_mib > 4096 * self.resources.cpu:
            raise ValueError("Cloud Run gives a job at most 4 GiB a CPU: raise cpu or lower memory_mib")
        for field, parts in (("command", self.command), ("args", self.args)):
            if parts is None:
                continue
            if not parts and field == "command":
                raise ValueError("A job's command needs at least one word, or leave it out")
            if len(parts) > 32 or not all(isinstance(p, str) and 0 < len(p) <= 200 and p.isprintable() and "," not in p for p in parts):
                raise ValueError(f"A job's {field} is a list of at most 32 printable words of at most 200 characters, without commas")
        if len(set(self.models)) != len(self.models) or len(set(self.secrets)) != len(self.secrets):
            raise ValueError("A job's models and secrets are each named once")
        if len(self.env) > 16 or len(self.secrets) > 16:
            raise ValueError("A job takes at most 16 env values and 16 secrets")
        for name in list(self.env) + self.secrets:
            if not ENV_NAME.fullmatch(name) or name in RESERVED_ENV:
                raise ValueError(f"'{name}' is not a variable name a job may set (uppercase letters, digits, underscores; not one Cloud Run sets)")
        for name, value in self.env.items():
            if len(value) > 1024 or "\n" in value:
                raise ValueError(f"A job's env value for {name} must be a single line of at most 1024 characters")
        if set(self.secrets) & set(self.env):
            raise ValueError("A job's secrets must not repeat one of its env names")
        return self


class Wake(StrictModel):
    """The platform's clock (docs/ROADMAP.md D66): the controller sends POST `path` every `every_minutes` as the
    application's <service>-wake identity, so an application that must look for work runs without an instance kept on."""
    path: str = Field(pattern=r"^/[A-Za-z0-9/_-]*$", max_length=200)
    every_minutes: int = Field(default=5, ge=1, le=60)


class AppSpec(StrictModel):
    api_version: Literal["lab/v1"] = "lab/v1"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,19}[a-z0-9]$")  # 3 to 21 characters: what Cloud Run, the registry and the root accept
    # python-http: tested and audited on `python`; node-http (docs/ROADMAP.md D41): tested and audited on `node`.
    runtime: Literal["python-http", "node-http", "static"] = "python-http"
    port: int = Field(default=8080, ge=1024, le=65535)
    # null: the service has no health route; Cloud Run's TCP startup probe gates it and the platform's own probes only check
    # that it answers HTTP at all (docs/ROADMAP.md 13.4). A path is probed for 200.
    health_path: str | None = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    # Run-time overrides of the image's ENTRYPOINT (command) and CMD (args), Cloud Run's container command and arguments, for
    # an image whose own start command does not suit Cloud Run (docs/ROADMAP.md 13.4); args alone keep the ENTRYPOINT.
    command: list[str] | None = None
    args: list[str] | None = None
    resources: Resources = Field(default_factory=Resources)
    scaling: Scaling = Field(default_factory=Scaling)
    concurrency: int = Field(default=80, ge=1, le=1000)
    # authenticated: programs with a Google identity token. lab-members: people through Identity-Aware Proxy, the listed
    # Google accounts only (the operator keeps the list). Public is never an option.
    exposure: Literal["authenticated", "lab-members"] = "authenticated"
    # A service that listens on 127.0.0.1 or localhost and reads no variable for it (docs/ROADMAP.md D51, 22.1): an Nginx
    # container, pinned by digest, takes the requests on an ingress port and forwards them to `port` on the loopback the
    # two containers share. Its memory and CPU are billed as the app's, only while requests are served.
    proxy: bool = False
    state: Literal["stateless"] = "stateless"
    dependencies: list[str] = Field(default_factory=list)
    gpu: bool = False
    # Plain configuration values set on every revision; secrets are names of Secret Manager secrets called <service>-NAME.
    env: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    # "none" declares that no automated tests run before a release; every plan then carries a warning.
    # report (docs/ROADMAP.md D54, 22.5): the suite runs and is counted, and a failure is reported, never a stop: for tests
    # that need what the platform's runner does not have (a browser, a database, a seed, Docker).
    tests: Literal["required", "none", "report"] = "required"
    # The Python the tests and the dependency audit run on; match the Dockerfile's base image. A lock compiled for one
    # version can omit packages another needs (live: SQLAlchemy needs greenlet below 3.13, so a 3.13 lock failed on 3.12).
    python: Literal["3.11", "3.12", "3.13", "3.14", "3.15"] = "3.12"
    # The Node.js line of a node-http service (D41): its tests, its audit and the platform's image (node:<line>-trixie-slim) use it.
    # Required for node-http and refused for python-http, so a contract names exactly one of python and node. A Node.js
    # service keeps python at its default, which every dump leaves out, so its hash and its full dump read back the same.
    node: Literal["22", "24", "26"] | None = None
    # How long one request may run, Cloud Run's own ceiling of 60 minutes included (docs/ROADMAP.md 19.4): a service that
    # streams long answers (server-sent events) holds its request open for as long as the answer runs.
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    # Buckets, topics and a database the platform provisions for the app (docs/SETUP.md 6b); variables are derived.
    needs: Needs = Field(default_factory=Needs)
    build: Build = Field(default_factory=Build)
    # Who has read the code (docs/ROADMAP.md D35): reviewed, a person of the lab; unknown, nobody (a repository taken as it
    # is). Left unset it is reviewed in a repository's own platform.yaml and unknown in a contract the platform keeps.
    # Unknown code is contained: the vulnerability gate blocks, a working key found in it stops the build, it runs in the
    # second-generation environment and reaches no internet unless `egress` says so.
    trust: Literal["reviewed", "unknown"] | None = None
    # Where the service may connect: internet, anywhere; private, Google's APIs and the lab's private network only (all its
    # traffic goes into the lab's network, which has no way out). Left unset: private for unknown code, internet otherwise.
    egress: Literal["internet", "private"] | None = None
    # A name of the lab's own for production (docs/ROADMAP.md D39, 16.6): a global load balancer with a Google-managed
    # certificate and Cloud Armor answers at it, about $25 a month; staging keeps its run.app address. It is reached behind
    # the same sign-in as the run.app address (a Google identity token, or IAP for lab-members), which keeps answering.
    domain: str | None = Field(default=None, pattern=DOMAIN.pattern)
    # Where the service lives (docs/ROADMAP.md D35, 14.9): shared, the staging project every application shares; own, a
    # project of its own, for code nobody has reviewed or an application that holds people's data, so its identities,
    # resources, quotas and bill stay apart. The operator creates the project and applies its foundation first.
    project: Literal["shared", "own"] | None = None
    # Cloud Run jobs the service starts itself, and the clock that wakes it (docs/ROADMAP.md D64, D66).
    jobs: dict[str, Job] = Field(default_factory=dict)
    wake: Wake | None = None
    reviewed: bool = False

    @model_validator(mode="after")
    def compatible(self):
        if self.scaling.minimum > self.scaling.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if self.domain is not None and (len(self.domain) > 253 or self.domain.endswith(".run.app")):
            raise ValueError("domain is a name of the lab's own of at most 253 characters; a run.app address needs no load balancer")
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
        for field, parts in (("command", self.command), ("args", self.args)):
            if parts is None:
                continue
            if not parts and field == "command":
                raise ValueError("command needs at least one word, or leave it out")
            if len(parts) > 32 or not all(isinstance(p, str) and 0 < len(p) <= 200 and p.isprintable() and "," not in p for p in parts):
                raise ValueError(f"{field} is a list of at most 32 printable words of at most 200 characters, without commas")
        if (self.command is not None or self.args is not None) and self.build.recipe == "buildpacks":
            raise ValueError("command and args replace the image's start command, which on a buildpacks image bypasses its launcher; use build.entrypoint")
        self.runtime_fits()
        self.jobs_fit()
        # IAP_AUDIENCE: what a lab-members app checks the proxy's signed header against, set by the release (1.25.1).
        derived = {**needs_env(self, "project"), **needs_secrets(self), **({"IAP_AUDIENCE": ""} if self.exposure == "lab-members" else {})}
        taken = set(self.env) | set(self.secrets)
        for name in derived:
            if name in taken:
                raise ValueError(f"'{name}' is set by the platform for a declared need or the exposure and cannot be an env or secret name")
        if len(derived) + len(self.env) > 48:
            raise ValueError("Too many variables: needs, env and secrets together must stay under 48")
        return self

    def jobs_fit(self):
        """The jobs the service starts and the clock (docs/ROADMAP.md D64, D66): names that make valid job and account names,
        and only what the service itself declared (its buckets, its secrets)."""
        if len(self.jobs) > MAX_JOBS:
            raise ValueError(f"A service starts at most {MAX_JOBS} jobs")
        if (self.jobs or self.wake) and self.runtime == "static":
            raise ValueError("runtime: static has no server to start jobs or to wake; give the jobs to a web service")
        for name, job in self.jobs.items():
            if not JOB_NAME.fullmatch(name) or "--" in name:
                raise ValueError(f"'{name}' is not a job name: 2 to 14 lowercase letters, digits and single dashes, starting with a letter")
            if name in RESERVED_JOBS:
                raise ValueError(f"'{name}' is a name the platform uses for its own identities or the migration job; call the job something else")
            if len(self.name) + 1 + len(name) > 30:
                raise ValueError(f"The job's identity {self.name}-{name} would exceed the 30 characters of an account id: shorten the job's name "
                                 f"to {29 - len(self.name)} characters or fewer")
            unknown = sorted(set(job.buckets) - set(self.needs.buckets))
            if unknown:
                raise ValueError(f"The job {name} names {', '.join(unknown)}, which is not under needs.buckets: declare the bucket, then give it to the job")
            missing = sorted(set(job.secrets) - set(self.secrets))
            if missing:
                raise ValueError(f"The job {name} reads {', '.join(missing)}, which is not under secrets: name the secret there first")
            if job.base == self.name:
                raise ValueError(f"The job {name} cannot build on this service's own test image; base names another onboarded application")
            platform = job_env(self, name, "project", own_env=False)
            clash = sorted(set(job.env) & set(platform))
            if clash:
                raise ValueError(f"The job {name} sets {', '.join(clash)}, which the platform sets for what the job was given")
            if len(platform) + len(job.env) + len(job.secrets) > 48:
                raise ValueError(f"Too many variables for the job {name}: its buckets, models, env and secrets together must stay under 48")

    def runtime_fits(self):
        """Exactly one of python and node, as the runtime says, and the Node.js rules of docs/ROADMAP.md D43 and D45."""
        if self.build.output is not None and self.runtime != "static":
            raise ValueError("build.output applies to runtime: static, the folder the build writes the site into")
        if self.proxy and self.runtime == "static":
            raise ValueError("proxy applies to a web service that binds loopback; a static site is served by the platform's image")
        if self.runtime == "static":
            # D55: a front-end site with no server, built with its build script and served by the platform's image behind IAP.
            if self.node is None:
                raise ValueError('runtime: static needs node, the Node.js line its build runs on: "22", "24" or "26"')
            if self.build.recipe != "platform" or not self.build.output:
                raise ValueError("runtime: static is built by the platform (build.recipe: platform) and needs build.output, the folder the build writes")
            if self.exposure != "lab-members":
                raise ValueError("runtime: static is a site for people: exposure: lab-members (behind IAP); public stays refused")
            if self.python != type(self).model_fields["python"].default or self.command is not None or self.args is not None:
                raise ValueError("runtime: static takes no python, command or args")
            return
        if self.runtime == "python-http":
            if self.node is not None:
                raise ValueError("node applies to runtime: node-http; a Python service names its version in python")
            if self.build.scripts:
                raise ValueError("build.scripts applies to runtime: node-http")
            return
        if self.node is None:
            raise ValueError('runtime: node-http needs node, the Node.js line it runs on: "22", "24" or "26"')
        if self.python != type(self).model_fields["python"].default:
            raise ValueError("python applies to runtime: python-http; a Node.js service names its line in node")
        starts = [words[0] for words in (self.build.entrypoint.split() if self.build.entrypoint else None, self.command) if words]
        if any(word.rsplit("/", 1)[-1] in PACKAGE_MANAGERS for word in starts):
            raise ValueError("A Node.js service is started by node itself (node server.js), never through npm or another package manager: "
                             "they add latency and do not pass SIGTERM on, so Cloud Run's shutdown would cut requests off")
        if self.build.network == "none" and self.build.recipe != "repository":
            raise ValueError("build.network: none cannot install Node.js packages: the platform's image installs them from the registry "
                             "with npm ci; vendor them with a Dockerfile of the repository's own instead")


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


CONTAINED_TAG = "lab-deploy-contained"  # the network tag of a service whose egress is private, for a firewall rule to name


def containment(spec: AppSpec, default_trust: str = "reviewed") -> dict:
    """How the platform treats a service's code (docs/ROADMAP.md D35): its trust level, where it may connect, its execution
    environment and whether the gates block. `default_trust` is what an unset `trust` means where the contract lives:
    reviewed for a repository's own platform.yaml, unknown for a contract the platform keeps. A bucket mount needs the
    second-generation environment too (Cloud Storage FUSE runs there only; docs/ROADMAP.md 19.1)."""
    trust = spec.trust or (default_trust if default_trust in ("reviewed", "unknown") else "reviewed")
    egress = spec.egress or ("private" if trust == "unknown" else "internet")
    unknown = trust == "unknown"
    return {"trust": trust, "egress": egress, "execution_environment": "gen2" if unknown or spec.needs.mounts else "default",
            "vulnerability_gate": "blocks fixable critical and high findings" if unknown else "reports",
            "secret_scan": "a working key stops the build" if unknown else "reports", "runs_as_root": "refused" if unknown else "reported"}


def draft_dump(spec: AppSpec) -> dict:
    """The specification as a draft shows it: every field, except the options of a repository taken as it is left unset (no
    command or args, the repository's own recipe, no entrypoint), so drafts for repositories that follow the contract read
    as they always did."""
    data = spec.model_dump()
    for key in ("command", "args", "trust", "egress", "node", "project"):
        if data.get(key) is None:
            data.pop(key, None)
    if spec.runtime in ("node-http", "static"):
        data.pop("python", None)  # a Node.js service or a site names its line in node; python keeps its unused default
    build = data.get("build") or {}
    if build.get("entrypoint") is None:
        build.pop("entrypoint", None)
    if build.get("recipe") == "repository":
        build.pop("recipe", None)
    if build.get("network") == "default":
        build.pop("network", None)
    for key in ("user", "output"):  # Phase 22's, shown only when set
        if build.get(key) is None:
            build.pop(key, None)
    if not data.get("proxy"):
        data.pop("proxy", None)
    if not build.get("scripts"):
        build.pop("scripts", None)
    for key in ("jobs", "wake"):  # Phase 27's, shown only when declared, so other drafts read as before
        if not data.get(key):
            data.pop(key, None)
    needs = data.get("needs") or {}
    for key in ("mounts", "models"):  # Phase 19's needs, shown only when declared, so other drafts read as before
        if not needs.get(key):
            needs.pop(key, None)
    return data


def parse_contract(text: str) -> tuple[AppSpec, Source]:
    """A contract kept in the platform repository (apps/<service>/platform.yaml, docs/ROADMAP.md D31): the specification
    plus a `source` block naming the application repository, the service's folder in it and the commit that runs.

    D32: what makes it reviewed is its merge into the platform repository's main under the code owners' review, never a
    field anybody writes, so the file must not carry `reviewed`; read from main, it is reviewed, and the returned
    specification says so. Every reader (the controller's plan, the pipeline's checks) therefore hashes the same thing."""
    if len(text.encode()) > 32_768:
        raise ValueError("The contract exceeds 32 KiB")
    data = yaml.load(text, Loader=UniqueLoader)  # noqa: S506 - UniqueLoader subclasses SafeLoader
    if not isinstance(data, dict) or "source" not in data:
        raise ValueError("A contract in the platform repository needs a `source` block (repository, path, commit)")
    if "reviewed" in data:
        raise ValueError("Leave `reviewed` out of a contract kept in the platform repository: merging it is the review (docs/ROADMAP.md D32)")
    source = Source.model_validate(data.pop("source"))
    return AppSpec.model_validate({**data, "reviewed": True}), source


def spec_text(spec: AppSpec) -> str:
    """The specification as platform.yaml text, defaults left out, keys in the model's order; what the pipeline hashes."""
    return yaml.safe_dump(spec.model_dump(exclude_defaults=True), sort_keys=False, default_flow_style=False)


def contract_text(spec: AppSpec, source: Source, header: str | None = None) -> str:
    """The external contract as a file: an optional comment, the source block, then the specification without `reviewed`."""
    fields = {key: value for key, value in spec.model_dump(exclude_defaults=True).items() if key != "reviewed"}
    body = yaml.safe_dump({"source": source.model_dump(exclude_defaults=True), **fields}, sort_keys=False, default_flow_style=False)
    return (header.rstrip("\n") + "\n" if header else "") + body


def spec_hash(spec: AppSpec) -> str:
    # Defaults are left out, so adding an optional field to the schema does not change the hash of a spec that does not use it.
    return hashlib.sha256(json.dumps(spec.model_dump(exclude_defaults=True), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enforce(spec: AppSpec, cloud="gcp", environment="staging", today=None) -> None:
    # Production takes exactly what staging released, under the same limits (docs/ROADMAP.md D37): only a promotion reaches it.
    if cloud != "gcp" or environment not in ("staging", "production"):
        raise ValueError("The platform supports GCP staging, and production through promotion, only")
    if not spec.reviewed:
        raise ValueError("Review assumptions and merge reviewed: true in platform.yaml first")
    if spec.runtime == "node-http" and (today or today_utc()) >= NODE_END_OF_LIFE[spec.node]:
        # D41: a line past its end of life gets no security fixes; the runbook lists the dates ahead of time.
        newer = [line for line, ends in NODE_END_OF_LIFE.items() if ends > (today or today_utc())]
        raise ValueError(f"Node.js {spec.node} reached its end of life on {NODE_END_OF_LIFE[spec.node].isoformat()} and is no longer released: "
                         f"move node to {' or '.join(newer) or 'a supported line'} in the contract (and the Dockerfile), then release again")
    if spec.dependencies:
        raise ValueError(f"The platform does not provide {', '.join(spec.dependencies)}; declare databases, buckets and topics "
                         "under needs (database: postgres or firestore) and remove `dependencies`")
    if spec.gpu:
        raise ValueError("GPUs are not supported")
    if spec.resources.cpu > 2 or spec.resources.memory_mib > 2048 or spec.scaling.maximum > 3:
        raise ValueError(f"{environment.capitalize()} policy: maximum 2 CPUs, 2048 MiB, 3 instances")
    for name, job in spec.jobs.items():
        # D67: a job may be larger than a service (the agent used 4 GiB on a laptop); D64 and D65 bound its time and runs.
        if job.resources.cpu > JOB_POLICY["cpu"] or job.resources.memory_mib > JOB_POLICY["memory_mib"]:
            raise ValueError(f"{environment.capitalize()} policy: a job (here {name}) takes at most {JOB_POLICY['cpu']} CPUs and {JOB_POLICY['memory_mib']} MiB")


# The lab's policy for a job (docs/ROADMAP.md D64, D65, D67): its size, its time and how many runs a day. The model's own
# bounds hold the last two; enforce checks the size.
JOB_POLICY = {"cpu": 2, "memory_mib": 4096, "timeout_minutes": 60, "runs_per_day": 30}

