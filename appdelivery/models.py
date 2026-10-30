"""The release contract. Strict, unknown fields refused: a release names an image by digest, a request, a group of its
machines, a port, a health path and where each environment value comes from. Nothing in it can carry source, a build,
a command, an entrypoint, a volume or a privilege, and a test holds that."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import DeliveryError, ErrorCode

# host[:port]/path@sha256:digest; a tag is refused, so what runs is exactly what was verified.
IMAGE = re.compile(r"^(?P<host>[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?)/(?P<path>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@sha256:(?P<digest>[a-f0-9]{64})$")
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
GROUP = re.compile(r"^[a-z](?:[a-z0-9-]{0,10}[a-z0-9])?$")
WORKLOAD = re.compile(r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")  # a Kubernetes name, short enough for the release Secret's name
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ROUTE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
HEALTH_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}(?:\?[A-Za-z0-9._~=&%-]{0,200})?$")
SECRET_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,10}[a-z0-9])?$|^db-password$|^cache-auth$")
# The infrastructure layer's endpoints that are one value each; lists (addresses of several machines) are not bindable.
ENDPOINTS = ("database_host", "database_port", "cache_host", "cache_port", "load_balancer_address", "cluster_endpoint")
# A literal under a name with one of these words (or pairs of words) is a credential in plain text: it belongs in a secret.
CREDENTIAL_WORDS = frozenset({"PASSWORD", "PASSWD", "PWD", "SECRET", "TOKEN", "APIKEY", "CREDENTIAL", "CREDENTIALS", "AUTH"})
CREDENTIAL_PAIRS = frozenset({("PRIVATE", "KEY"), ("API", "KEY"), ("ACCESS", "KEY"), ("SIGNING", "KEY")})
MAX_ENV = 64
MAX_VALUE = 4096


def looks_like_credential(name: str) -> bool:
    words = [word for word in name.split("_") if word]
    return any(word in CREDENTIAL_WORDS for word in words) or any(pair in CREDENTIAL_PAIRS for pair in zip(words, words[1:], strict=False))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(StrictModel):
    """Where one environment value comes from: exactly one of a literal, an endpoint of the request, or a secret of it."""
    value: str | None = Field(default=None, max_length=MAX_VALUE)
    endpoint: Literal[ENDPOINTS] | None = None
    secret: str | None = Field(default=None, description="A secret of the same request, by its short name (db-password, cache-auth, or one the request named).")

    @model_validator(mode="after")
    def _one_source(self):
        given = [name for name in ("value", "endpoint", "secret") if getattr(self, name) is not None]
        if len(given) != 1:
            raise DeliveryError(ErrorCode.INVALID_BINDING, f"an environment value comes from exactly one of value, endpoint or secret; got {', '.join(given) or 'none'}")
        if self.value is not None and ("\n" in self.value or "\r" in self.value or "\0" in self.value):
            raise DeliveryError(ErrorCode.INVALID_BINDING, "a value cannot hold a line break or a NUL: Docker's env file takes one line per variable")
        if self.secret is not None and not SECRET_NAME.match(self.secret):
            raise DeliveryError(ErrorCode.INVALID_BINDING, f"{self.secret!r} is not a secret name of a request")
        return self

    def source(self) -> str:
        """The binding as a caller may see it: never a secret's value, and a literal only as 'value'."""
        if self.endpoint is not None:
            return f"endpoint {self.endpoint}"
        if self.secret is not None:
            return f"secret {self.secret}"
        return "value"


class Expose(StrictModel):
    """A workload reached from outside through its cluster's front door, which the infrastructure layer made."""
    hostname: str | None = Field(default=None, description="One of the front door's hostnames; every one of them when omitted. None on a plain HTTP front door.")
    path: str = Field(default="/", description="The path prefix sent to the workload.")

    @field_validator("hostname")
    @classmethod
    def _hostname(cls, value):
        if value is not None and not HOSTNAME.fullmatch(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not a lowercase DNS name")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        if not ROUTE_PATH.fullmatch(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not a path prefix: /, then letters, digits and . _ ~ / -")
        return value


class Target(StrictModel):
    """Where a release runs: every machine of one compute group, one workload on one of the request's clusters, one of the
    request's managed container services, or one of its managed jobs."""
    group: str | None = Field(default=None, description="A compute group of the request; every machine in it is a target.")
    cluster: bool | str = Field(default=False, description="A cluster of the request by the name its cluster block gave it (cluster when it gave none), or true for "
                                                            "the request's only cluster; the release runs in the namespace the infrastructure layer made there.")
    service: bool | str = Field(default=False, description="A managed container service of the request by the name its service block gave it (service when it "
                                                            "gave none), or true for the request's only one; each release is a revision of it.")
    job: bool | str = Field(default=False, description="A managed job of the request by the name its job block gave it (job when it gave none), or true for "
                                                        "the request's only one; a release becomes what the job runs, and run_release starts it.")
    workload: str | None = Field(default=None, description="On a cluster: the Deployment and Service name (lowercase letters, digits, dashes).")
    replicas: int = Field(default=1, ge=1, le=20, description="On a cluster: how many copies run.")
    expose: Expose | None = Field(default=None, description="On a cluster: reach the workload from outside through the cluster's front door.")

    @field_validator("group")
    @classmethod
    def _group(cls, value):
        if value is not None and not GROUP.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not a group name: lowercase, up to 12 characters")
        return value

    @field_validator("cluster", "service", "job")
    @classmethod
    def _named(cls, value, info):
        if isinstance(value, str) and not GROUP.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not a {info.field_name} name: lowercase, up to 12 characters, as the request's "
                                f"{info.field_name} block names it")
        return value

    @field_validator("workload")
    @classmethod
    def _workload(cls, value):
        if value is not None and not WORKLOAD.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not a workload name: lowercase letters, digits and dashes, up to 40 characters")
        return value

    @model_validator(mode="after")
    def _one_kind(self):
        if [self.group is not None, bool(self.cluster), bool(self.service), bool(self.job)].count(True) != 1:
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "target names one of a group of machines, a cluster, a service or a job")
        if (self.service or self.job) and (self.workload is not None or self.replicas != 1 or self.expose is not None):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "workload, replicas and expose belong to a cluster target; a service or a job scales itself")
        if self.cluster and self.workload is None:
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "a cluster target names its workload")
        if self.group is not None and (self.workload is not None or self.replicas != 1 or self.expose is not None):  # a stored release carries the defaults back
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "workload, replicas and expose belong to a cluster target; a group runs one container per machine")
        return self

    @property
    def single(self) -> bool:
        """One target for the whole release (a workload on a cluster, or a service), not a group of machines."""
        return bool(self.cluster) or bool(self.service) or bool(self.job)

    def key(self) -> str:
        """What one release at a time is kept for: a group of machines, or a workload on one cluster. The unnamed cluster
        keeps the key releases had before clusters had names; true is resolved to a name before a release is kept."""
        if self.service:
            return f"service:{'service' if self.service is True else self.service}"
        if self.job:
            return f"job:{'job' if self.job is True else self.job}"
        if not self.cluster:
            return self.group
        name = "cluster" if self.cluster is True else self.cluster
        return f"cluster:{self.workload}" if name == "cluster" else f"cluster:{name}:{self.workload}"


class Health(StrictModel):
    path: str = "/"
    timeout_seconds: int = Field(default=60, ge=5, le=600)

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        if not HEALTH_PATH.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"health.path {value!r}: an absolute path of letters, digits and ._~/- with an optional simple query")
        return value


class ReleaseRequest(StrictModel):
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{1,20}$")
    request_id: str = Field(description="The cloudinfra request that holds the target machines.")
    target: Target
    image: str = Field(description="host/path@sha256:digest; a tag is refused.")
    port: int = Field(ge=1, le=65535, description="The container's port, published on the same port of each machine.")
    health: Health = Field(default_factory=Health)
    env: dict[str, Binding] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value):
        if not REQUEST_ID.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "request_id is the 32 hexadecimal characters cloudinfra issued")
        return value

    @field_validator("image")
    @classmethod
    def _image(cls, value):
        if "@sha256:" not in value:
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "image must name a digest (host/path@sha256:…): a tag can change after it is checked")
        if not IMAGE.match(value):
            raise DeliveryError(ErrorCode.INVALID_RELEASE, f"{value!r} is not host/path@sha256:<64 hex characters>")
        return value

    @field_validator("port")
    @classmethod
    def _port(cls, value):
        if value == 22:
            raise DeliveryError(ErrorCode.INVALID_RELEASE, "port 22 is the machine's SSH, through which this service reaches it")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value):
        if len(value) > MAX_ENV:
            raise DeliveryError(ErrorCode.INVALID_BINDING, f"at most {MAX_ENV} environment values")
        for name, binding in value.items():
            if not ENV_NAME.match(name):
                raise DeliveryError(ErrorCode.INVALID_BINDING, f"{name!r}: environment names are uppercase letters, digits and underscores")
            if binding.value is not None and looks_like_credential(name):
                raise DeliveryError(ErrorCode.INVALID_BINDING, f"{name} looks like a credential: put it in a secret of the request and bind {{secret: NAME}}",
                                    {"field": f"env.{name}"})
        return value

    def registry(self) -> str:
        return IMAGE.match(self.image).group("host")

    def repository(self) -> str:
        """host/path, without the digest: what allowed_registries prefixes are compared with."""
        found = IMAGE.match(self.image)
        return f"{found.group('host')}/{found.group('path')}"


class PlannedMachine(BaseModel):
    resource: str
    name: str
    zone: str
    instance_id: str | None = None
    internal_ip: str | None = None


class PlanResponse(BaseModel):
    """What a release would do, checked against the request as the infrastructure layer reports it now. Nothing ran."""
    status: Literal["PLANNED"] = "PLANNED"
    environment: str
    request_id: str
    image: str
    service_account: str
    machines: list[PlannedMachine]
    env: dict[str, str] = Field(description="Each name and where its value comes from; never a secret's value.")
    attestation_required: bool
    approval_required: bool
    target: Literal["machines", "cluster", "service", "job"] = "machines"
    namespace: str | None = Field(default=None, description="On a cluster: the request's namespace.")
    workload: str | None = None
    replicas: int | None = None
    url: str | None = Field(default=None, description="On a cluster with expose: where the workload is reached from outside.")
    replaces: str | None = Field(default=None, description="The release running on this target now, which this one would replace.")
    warnings: list[str] = Field(default_factory=list)


class TargetView(BaseModel):
    resource: str
    name: str
    zone: str
    status: Literal["PENDING", "DEPLOYING", "HEALTHY", "UNHEALTHY", "FAILED", "RESTORED", "SKIPPED", "ROLLED_BACK", "REMOVED", "UNDO_FAILED"]
    started_at: str | None = None
    finished_at: str | None = None
    error: dict | None = None
    log: str | None = Field(default=None, description="The container's last log lines when it failed, every secret value the release read removed.")


class ReleaseResponse(BaseModel):
    release_id: str
    status: Literal["AWAITING_APPROVAL", "ROLLING_OUT", "RUNNING", "FAILED", "SUPERSEDED", "ROLLING_BACK", "ROLLED_BACK", "ROLLBACK_FAILED", "REMOVING", "REMOVED",
                    "REMOVE_FAILED"]
    environment: str
    request_id: str
    group: str
    image: str
    previous: str | None = Field(default=None, description="The release this one replaced; what a rollback restores.")
    can_roll_back: bool = False
    requested_by: str
    approved_by: str | None = None
    note: str | None = None
    env: dict[str, str] = Field(default_factory=dict, description="Each name and where its value comes from; never a secret's value.")
    warnings: list[str] = Field(default_factory=list)
    targets: list[TargetView] = Field(default_factory=list)
    error: dict | None = None
    created_at: str
    updated_at: str


class RunResponse(BaseModel):
    """One run of a job: its tasks' counts as the provider reports them, and where its log is."""
    release_id: str
    execution: str = Field(description="The run's name at the provider.")
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    running: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    log_url: str | None = Field(default=None, description="The provider's console page with the run's log.")
