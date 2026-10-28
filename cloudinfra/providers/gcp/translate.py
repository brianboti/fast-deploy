"""Generic request → Google Cloud shapes: machine types, image families, disk types, database versions and tiers, names, labels."""
import re

from ...models.errors import ErrorCode
from ...models.request import ComputeRequest, DatabaseRequest
from ..base import Problem

IMAGES = {
    "ubuntu": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
    "debian": "projects/debian-cloud/global/images/family/debian-12",
    "rocky": "projects/rocky-linux-cloud/global/images/family/rocky-linux-9",
}
DISK_TYPES = {"standard": "pd-standard", "balanced": "pd-balanced", "ssd": "pd-ssd"}
DB_VERSIONS = {
    "postgresql": {"default": "16", "versions": {"13": "POSTGRES_13", "14": "POSTGRES_14", "15": "POSTGRES_15", "16": "POSTGRES_16", "17": "POSTGRES_17"}},
    "mysql": {"default": "8.0", "versions": {"5.7": "MYSQL_5_7", "8.0": "MYSQL_8_0", "8.4": "MYSQL_8_4"}},
}
# small: shared core, the cheapest instance Cloud SQL offers; medium and large: dedicated vCPUs with Google's custom tiers.
DB_TIERS = {"small": "db-f1-micro", "medium": "db-custom-2-7680", "large": "db-custom-4-15360"}
DB_PORTS = {"postgresql": 5432, "mysql": 3306}
IAP_RANGE = "35.235.240.0/20"  # Google's Identity-Aware Proxy TCP forwarding range
E2_SIZES = (2, 4, 8, 16, 32)
SHARED_CORE = {1: "e2-micro", 2: "e2-small", 4: "e2-medium"}  # one CPU's worth, by memory in GB
ZONES = {
    "us-central1": ["a", "b", "c", "f"], "us-east1": ["b", "c", "d"], "us-east4": ["a", "b", "c"], "us-east5": ["a", "b", "c"], "us-south1": ["a", "b", "c"],
    "us-west1": ["a", "b", "c"], "us-west2": ["a", "b", "c"], "us-west3": ["a", "b", "c"], "us-west4": ["a", "b", "c"],
    "northamerica-northeast1": ["a", "b", "c"], "northamerica-northeast2": ["a", "b", "c"], "southamerica-east1": ["a", "b", "c"],
    "europe-west1": ["b", "c", "d"], "europe-west2": ["a", "b", "c"], "europe-west3": ["a", "b", "c"], "europe-west4": ["a", "b", "c"],
    "europe-west6": ["a", "b", "c"], "europe-west9": ["a", "b", "c"], "europe-north1": ["a", "b", "c"], "europe-central2": ["a", "b", "c"],
    "asia-east1": ["a", "b", "c"], "asia-east2": ["a", "b", "c"], "asia-northeast1": ["a", "b", "c"], "asia-northeast3": ["a", "b", "c"],
    "asia-south1": ["a", "b", "c"], "asia-southeast1": ["a", "b", "c"], "asia-southeast2": ["a", "b", "c"], "australia-southeast1": ["a", "b", "c"],
}
LABEL_CHARS = re.compile(r"[^a-z0-9_-]")
MACHINE = re.compile(r"^e2-(standard|highmem|highcpu)-(\d+)$|^e2-custom-(\d+)-(\d+)$|^e2-(micro|small|medium)$")


def machine_type(cpu: int, memory_gb: int) -> str | None:
    """The E2 machine type for a shape, or None when E2 has none: a predefined one when the ratio matches, else a custom one."""
    if cpu == 1:
        return next((name for gb, name in SHARED_CORE.items() if memory_gb <= gb), None)
    if cpu in E2_SIZES:
        for family, per_cpu in (("standard", 4), ("highmem", 8), ("highcpu", 1)):
            if memory_gb == cpu * per_cpu:
                return f"e2-{family}-{cpu}"
    if cpu % 2 == 0 and 2 <= cpu <= 32 and cpu * 0.5 <= memory_gb <= cpu * 8:
        return f"e2-custom-{cpu}-{memory_gb * 1024}"
    return None


def machine_shape(name: str) -> tuple[int, int] | None:
    """(cpu, memory_gb) of a machine type this adapter issues; None for any other."""
    found = MACHINE.match(name)
    if not found:
        return None
    if found.group(1):
        cpu = int(found.group(2))
        return cpu, cpu * {"standard": 4, "highmem": 8, "highcpu": 1}[found.group(1)]
    if found.group(3):
        return int(found.group(3)), int(found.group(4)) // 1024
    return 1, {"micro": 1, "small": 2, "medium": 4}[found.group(5)]


def compute_problems(spec: ComputeRequest) -> list[Problem]:
    if machine_type(spec.cpu, spec.memory_gb) is None:
        if spec.cpu == 1:
            text = "1 CPU allows 1, 2 or 4 GB of memory on Google Cloud (a shared-core machine); ask for 2 CPUs or more for anything larger"
        elif spec.cpu % 2 or spec.cpu > 32:
            text = "Google Cloud's E2 machines have an even number of CPUs from 2 to 32"
        else:
            text = f"Google Cloud allows 0.5 to 8 GB of memory per CPU: {spec.cpu} CPUs take {spec.cpu // 2} to {spec.cpu * 8} GB"
        return [Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, text, "compute")]
    return []


def database_version(spec: DatabaseRequest) -> tuple[str, str] | Problem:
    """(label, API enum) for the engine and version, or the problem."""
    known = DB_VERSIONS[spec.engine]
    label = spec.version or known["default"]
    if label not in known["versions"]:
        return Problem(ErrorCode.INVALID_DATABASE_CONFIGURATION, f"{spec.engine} {label} is not offered; versions: {', '.join(known['versions'])}", "database.version")
    return label, known["versions"][label]


def image(os_family: str) -> str:
    return IMAGES[os_family]


def disk_type(performance: str) -> str:
    return DISK_TYPES[performance]


def label_value(value: str) -> str:
    return LABEL_CHARS.sub("-", str(value).lower())[:63]


def labels(ctx, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Ownership first, then the operator's and the request's own labels; every value made legal for Google Cloud."""
    found = {"managed-by": "cloudinfra", "cloudinfra-request": label_value(ctx.request_id), "cloudinfra-name": label_value(ctx.name)}
    if ctx.owner:
        found["cloudinfra-owner"] = label_value(ctx.owner)
    for key, value in {**(extra or {}), **ctx.labels}.items():
        found[label_value(key)] = label_value(value)
    return found


def description(ctx) -> str:
    """For resources Google Cloud gives no labels (networks, subnets, firewall rules): the ownership as text."""
    return f"cloudinfra request {ctx.request_id} ({ctx.name}) owner {ctx.owner or 'unknown'}"
