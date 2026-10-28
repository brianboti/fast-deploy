"""Generic request → Google Cloud shapes: machine types, image families, disk types, database versions and tiers, names, labels."""
import re

from ...models.errors import ErrorCode
from ...models.request import CacheRequest, ClusterRequest, ComputeRequest, DatabaseRequest
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
MACHINE = re.compile(r"^(e2|n1)-(standard|highmem|highcpu)-(\d+)$|^(e2|n1)-custom-(\d+)-(\d+)$|^e2-(micro|small|medium)$|^g2-standard-(\d+)$|^a2-highgpu-(\d+)g$|^a3-highgpu-8g$")
PER_CPU = {"e2": {"standard": 4, "highmem": 8, "highcpu": 1}, "n1": {"standard": 3.75, "highmem": 6.5, "highcpu": 0.9}}
# GPUs: the machine family follows the type. T4 attaches to N1; L4 is built into G2; A100 into A2; H100 into A3.
ACCELERATORS = {"t4": "nvidia-tesla-t4", "l4": "nvidia-l4", "a100": "nvidia-tesla-a100", "h100": "nvidia-h100-80gb"}
G2_MEMORY = {4: 16, 8: 32, 12: 48, 16: 64, 24: 96, 32: 128, 48: 192, 96: 384}  # g2-standard-N: GB
G2_GPUS = {24: 2, 48: 4, 96: 8}  # every other g2-standard-N carries one L4
A2_SHAPES = {1: (12, 85), 2: (24, 170), 4: (48, 340), 8: (96, 680)}  # a2-highgpu-Ng: vCPUs, GB
A3_SHAPE = (208, 1872)  # a3-highgpu-8g
T4_MAX_CPU = {1: 48, 2: 48, 4: 96}
N1_SIZES = (1, 2, 4, 8, 16, 32, 64, 96)
REDIS_VERSIONS = {"default": "7.2", "versions": {"6.2": "REDIS_6_X", "7.0": "REDIS_7_0", "7.2": "REDIS_7_2"}}


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
        cpu = int(found.group(3))
        return cpu, int(cpu * PER_CPU[found.group(1)][found.group(2)])
    if found.group(4):
        return int(found.group(5)), int(found.group(6)) // 1024
    if found.group(7):
        return 1, {"micro": 1, "small": 2, "medium": 4}[found.group(7)]
    if found.group(8):
        size = int(found.group(8))
        return size, G2_MEMORY.get(size, size * 4)
    if found.group(9):
        return A2_SHAPES.get(int(found.group(9)), (12, 85))
    return A3_SHAPE


def gpu_shape(spec: ComputeRequest) -> tuple[str, int, int] | Problem:
    """(machine type, cpu, memory_gb) for a group with an accelerator, or the problem. The family follows the GPU type, and
    the fixed-shape families (G2, A2, A3) give the smallest shape that covers the request; the plan shows what was chosen."""
    accelerator, cpu, memory = spec.accelerator, spec.cpu, spec.memory_gb
    if accelerator.type == "t4":
        if accelerator.count not in T4_MAX_CPU:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "t4 comes 1, 2 or 4 per machine", "compute.accelerator.count")
        if cpu > T4_MAX_CPU[accelerator.count]:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{accelerator.count} t4 allows at most {T4_MAX_CPU[accelerator.count]} CPUs", "compute.cpu")
        if cpu in N1_SIZES and cpu % 4 == 0 and memory == int(cpu * 3.75):
            return f"n1-standard-{cpu}", cpu, memory
        if (cpu == 1 or cpu % 2 == 0) and cpu <= 96 and cpu * 0.9 <= memory <= cpu * 6.5:
            return f"n1-custom-{cpu}-{memory * 1024}", cpu, memory
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "With a t4, Google Cloud allows 1 or an even number of CPUs up to 96 and 0.9 to 6.5 GB of memory per CPU", "compute")
    if accelerator.type == "l4":
        fitting = [size for size in G2_MEMORY if G2_GPUS.get(size, 1) == accelerator.count and size >= cpu and G2_MEMORY[size] >= memory]
        if not fitting:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "l4 machines have fixed shapes: 1 GPU with 4 to 32 CPUs (16 to 128 GB), 2 with 24 CPUs (96 GB), "
                           "4 with 48 (192 GB), 8 with 96 (384 GB); ask for a shape within one of them", "compute")
        size = min(fitting)
        return f"g2-standard-{size}", size, G2_MEMORY[size]
    if accelerator.type == "a100":
        if accelerator.count not in A2_SHAPES:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "a100 comes 1, 2, 4 or 8 per machine", "compute.accelerator.count")
        vcpu, gb = A2_SHAPES[accelerator.count]
        if cpu > vcpu or memory > gb:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{accelerator.count} a100 gives {vcpu} CPUs and {gb} GB; ask for at most that", "compute")
        return f"a2-highgpu-{accelerator.count}g", vcpu, gb
    if accelerator.count != 8:
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "h100 comes 8 per machine", "compute.accelerator.count")
    if cpu > A3_SHAPE[0] or memory > A3_SHAPE[1]:
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"8 h100 give {A3_SHAPE[0]} CPUs and {A3_SHAPE[1]} GB; ask for at most that", "compute")
    return "a3-highgpu-8g", *A3_SHAPE


def compute_shape(spec: ComputeRequest) -> tuple[str, int, int] | Problem:
    """(machine type, cpu, memory_gb) for any group, or the problem."""
    if spec.accelerator is not None:
        return gpu_shape(spec)
    machine = machine_type(spec.cpu, spec.memory_gb)
    if machine is not None:
        return machine, spec.cpu, spec.memory_gb
    if spec.cpu == 1:
        text = "1 CPU allows 1, 2 or 4 GB of memory on Google Cloud (a shared-core machine); ask for 2 CPUs or more for anything larger"
    elif spec.cpu % 2 or spec.cpu > 32:
        text = "Google Cloud's E2 machines have an even number of CPUs from 2 to 32"
    else:
        text = f"Google Cloud allows 0.5 to 8 GB of memory per CPU: {spec.cpu} CPUs take {spec.cpu // 2} to {spec.cpu * 8} GB"
    return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, text, "compute")


def compute_problems(spec: ComputeRequest) -> list[Problem]:
    shape = compute_shape(spec)
    return [shape] if isinstance(shape, Problem) else []


def cluster_problems(spec: ClusterRequest) -> list[Problem]:
    if machine_type(spec.node_cpu, spec.node_memory_gb) is None:
        return [Problem(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"Nodes with {spec.node_cpu} CPUs and {spec.node_memory_gb} GB have no E2 shape: an even number of CPUs "
                        "from 2 to 32 with 0.5 to 8 GB per CPU, or 1 CPU with 1, 2 or 4 GB", "cluster")]
    return []


def cache_version(spec: CacheRequest) -> tuple[str, str] | Problem:
    label = spec.version or REDIS_VERSIONS["default"]
    if label not in REDIS_VERSIONS["versions"]:
        return Problem(ErrorCode.INVALID_CACHE_CONFIGURATION, f"redis {label} is not offered; versions: {', '.join(REDIS_VERSIONS['versions'])}", "cache.version")
    return label, REDIS_VERSIONS["versions"][label]


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
