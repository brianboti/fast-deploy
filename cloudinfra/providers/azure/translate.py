"""Generic request → Azure shapes: VM sizes, images, disk types, tags and the names of the parts a resource is made of.

Azure sells fixed sizes and no custom shape (learn.microsoft.com/azure/virtual-machines/sizes/overview), so a machine
group's CPUs and memory must be one of the sizes below exactly; anything else is refused with the sizes there are, before
any call. A group with GPUs gets the smallest size of its GPU's family with that many GPUs that covers the CPUs and
memory asked, as on Google, and the plan says which. The families are the current generation of each kind, read from
their size pages on 2026-11-03 (docs/research/RESEARCH-2026-11-03-azure.md, section 5.2):

- D (Dsv5, 4 GB per vCPU), E (Esv5, 8 GB), Dl (Dlsv5, 2 GB): the general-purpose families, 2 to 96 vCPUs; Dlsv5 is the
  2 GB sibling of Dsv5, chosen over Fsv2 for the same sizes and generation.
- B1s and B1ms: the only sizes with one vCPU (1 and 2 GB), burstable.
- t4: NCasT4_v3; a100: NC_A100_v4 (1, 2 or 4 GPUs) and ND96asr_v4 (8); h100: NCads_H100_v5 (1 or 2) and
  ND96isr_H100_v5 (8). Azure has no L4: such a request is refused.

Every size here takes Premium storage and Trusted Launch, except the ND sizes, whose page does not list Trusted Launch:
they boot as Standard machines, and the real run confirms it (section 9.2). The quota family is the name Compute's
usages give each family's vCPUs; a GPU family's starts at zero in a new subscription.
"""
from dataclasses import dataclass

from ...models.errors import ErrorCode
from ...models.request import ComputeRequest
from ..base import Problem


@dataclass(frozen=True)
class Size:
    name: str
    cpu: int
    memory_gb: int
    quota: str
    gpu: str | None = None
    gpus: int = 0
    trusted_launch: bool = True


def _family(pattern: str, quota: str, shapes) -> list[Size]:
    return [Size(pattern.format(cpu), cpu, memory, quota) for cpu, memory in shapes]


SIZES = [
    Size("Standard_B1s", 1, 1, "standardBSFamily"), Size("Standard_B1ms", 1, 2, "standardBSFamily"),
    *_family("Standard_D{}s_v5", "standardDSv5Family", [(cpu, cpu * 4) for cpu in (2, 4, 8, 16, 32, 48, 64, 96)]),
    *_family("Standard_E{}s_v5", "standardESv5Family", [(2, 16), (4, 32), (8, 64), (16, 128), (20, 160), (32, 256), (48, 384), (64, 512), (96, 672)]),
    *_family("Standard_D{}ls_v5", "standardDLSv5Family", [(cpu, cpu * 2) for cpu in (2, 4, 8, 16, 32, 48, 64, 96)]),
    Size("Standard_NC4as_T4_v3", 4, 28, "standardNCASv3_T4Family", "t4", 1), Size("Standard_NC8as_T4_v3", 8, 56, "standardNCASv3_T4Family", "t4", 1),
    Size("Standard_NC16as_T4_v3", 16, 110, "standardNCASv3_T4Family", "t4", 1), Size("Standard_NC64as_T4_v3", 64, 440, "standardNCASv3_T4Family", "t4", 4),
    Size("Standard_NC24ads_A100_v4", 24, 220, "StandardNCADSA100v4Family", "a100", 1), Size("Standard_NC48ads_A100_v4", 48, 440, "StandardNCADSA100v4Family", "a100", 2),
    Size("Standard_NC96ads_A100_v4", 96, 880, "StandardNCADSA100v4Family", "a100", 4),
    Size("Standard_ND96asr_v4", 96, 900, "standardNDASv4_A100Family", "a100", 8, trusted_launch=False),
    Size("Standard_NC40ads_H100_v5", 40, 320, "standardNCadsH100v5Family", "h100", 1), Size("Standard_NC80adis_H100_v5", 80, 640, "standardNCadsH100v5Family", "h100", 2),
    Size("Standard_ND96isr_H100_v5", 96, 1900, "standardNDSH100v5Family", "h100", 8, trusted_launch=False),
]
BY_NAME = {size.name.lower(): size for size in SIZES}
GENERAL = [size for size in SIZES if size.gpu is None]
GPU_NAMES = {"t4": "NVIDIA T4", "a100": "NVIDIA A100", "h100": "NVIDIA H100"}

# Built-in images: the OS families' current long-term releases, Gen2. Rocky Linux is sold by its publisher with a plan,
# whose terms the operator accepts once in the subscription (az vm image terms accept).
IMAGES = {
    "ubuntu": "Canonical:ubuntu-24_04-lts:server:latest",
    "debian": "Debian:debian-12:12-gen2:latest",
    "rocky": "resf:rockylinux-x86_64:9-base:latest",
}
PLANNED_IMAGES = ("rocky",)
MIN_BOOT_DISK_GB = 30  # the built-in images' own disks; Azure refuses a smaller OS disk
# The contract's disk classes: standard is HDD, balanced the SSD for general use, ssd the premium SSD
# (learn.microsoft.com/azure/virtual-machines/disks-types). Premium SSD v2 is zonal and data-only, so it is not used yet.
DISK_SKUS = {"standard": "Standard_LRS", "balanced": "StandardSSD_LRS", "ssd": "Premium_LRS"}
OS_DISK_SKU = "StandardSSD_LRS"
ADMIN_USER = "azureuser"  # Azure requires an administrator account; its key is thrown away (compute.py) and nobody signs in as it


def machine_shape(name: str) -> tuple[int, int] | None:
    """(cpu, memory_gb) of a size this adapter issues; None for any other."""
    size = BY_NAME.get((name or "").lower())
    return (size.cpu, size.memory_gb) if size else None


def size_of(name: str) -> Size | None:
    return BY_NAME.get((name or "").lower())


def _counts(gpu: str) -> list[int]:
    return sorted({size.gpus for size in SIZES if size.gpu == gpu})


def gpu_shape(spec: ComputeRequest) -> Size | Problem:
    accelerator = spec.accelerator
    if accelerator.type == "l4":
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "Azure has no NVIDIA L4: ask for t4, a100 or h100", "compute.accelerator.type")
    counts = _counts(accelerator.type)
    if accelerator.count not in counts:
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"On Azure, {accelerator.type} comes {', '.join(map(str, counts[:-1]))} or {counts[-1]} per machine",
                       "compute.accelerator.count")
    fitting = [size for size in SIZES if size.gpu == accelerator.type and size.gpus == accelerator.count and size.cpu >= spec.cpu and size.memory_gb >= spec.memory_gb]
    if not fitting:
        shapes = "; ".join(f"{size.cpu} vCPUs and {size.memory_gb} GB" for size in SIZES if size.gpu == accelerator.type and size.gpus == accelerator.count)
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{accelerator.count} {accelerator.type} on Azure come with {shapes}; ask for at most that", "compute")
    return min(fitting, key=lambda size: (size.cpu, size.memory_gb))


def compute_shape(spec: ComputeRequest) -> Size | Problem:
    """The size for a group, or the problem."""
    if spec.accelerator is not None:
        return gpu_shape(spec)
    found = next((size for size in GENERAL if size.cpu == spec.cpu and size.memory_gb == spec.memory_gb), None)
    if found is not None:
        return found
    offered = sorted({size.memory_gb for size in GENERAL if size.cpu == spec.cpu})
    if offered:
        text = f"With {spec.cpu} vCPUs, Azure's sizes have {', '.join(map(str, offered[:-1]))}{' or ' if len(offered) > 1 else ''}{offered[-1]} GB of memory"
    else:
        counts = sorted({size.cpu for size in GENERAL})
        text = f"Azure's sizes have {', '.join(map(str, counts[:-1]))} or {counts[-1]} vCPUs, with 2, 4 or 8 GB each (1 vCPU: 1 or 2 GB); there are no custom shapes"
    return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, text, "compute")


def compute_problems(spec: ComputeRequest) -> list[Problem]:
    shape = compute_shape(spec)
    return [shape] if isinstance(shape, Problem) else []


def accelerator_of(size: Size | None):
    """The contract's GPUs a size carries, so that a new shape keeps them and their family."""
    from ...models.request import AcceleratorRequest
    return AcceleratorRequest(type=size.gpu, count=size.gpus) if size is not None and size.gpu else None


def image_reference(image: str) -> dict:
    """The imageReference of an image as a profile or the built-in table names it."""
    if image.startswith("/"):
        return {"id": image}
    publisher, offer, sku, version = image.split(":")
    return {"publisher": publisher, "offer": offer, "sku": sku, "version": version}


def plan_of(image: str) -> dict:
    """The Marketplace plan of a planned image: its publisher's product and sku."""
    publisher, offer, sku, _ = image.split(":")
    return {"name": sku, "publisher": publisher, "product": offer}


def software(group, settings, size: Size | None = None) -> dict | Problem:
    """The image, profile, Secure Boot, whether GPU drivers come with the image, and the boot disk a compute group gets.
    The rules are Google's: a profile is the operator's; a GPU group naming neither profile nor os gets the default
    accelerator profile; only a profile made for GPUs carries their drivers, and any other image on a GPU machine boots
    with Secure Boot off, because NVIDIA's driver extension does not run under it. A size without Trusted Launch has no
    Secure Boot at all."""
    name = group.profile
    if name is None and group.accelerator is not None and group.os is None:
        name = settings.default_accelerator_profile
    trusted = size.trusted_launch if size is not None else True
    if name is None:
        family = group.os_family
        return {"image": IMAGES[family], "plan": family in PLANNED_IMAGES, "profile": None, "secure_boot": group.accelerator is None and trusted,
                "drivers": False, "boot_disk_gb": max(group.boot_disk_gb, MIN_BOOT_DISK_GB), "trusted_launch": trusted}
    profile = settings.profiles.get(name)
    if profile is None:
        offered = ", ".join(sorted(settings.profiles)) or "none configured"
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"Unknown profile {name!r}; profiles: {offered}", "compute.profile")
    if profile.accelerator == "required" and group.accelerator is None:
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"Profile {name} is for machines with GPUs; add an accelerator or choose another profile", "compute.profile")
    if profile.accelerator == "none" and group.accelerator is not None:
        return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"Profile {name} is not for machines with GPUs; choose another profile", "compute.profile")
    drivers = profile.accelerator == "required"
    secure_boot = (profile.secure_boot if group.accelerator is None or drivers else False) and trusted
    return {"image": profile.image, "plan": profile.plan, "profile": name, "secure_boot": secure_boot, "drivers": drivers,
            "boot_disk_gb": max(group.boot_disk_gb, profile.min_boot_disk_gb), "trusted_launch": trusted}


def disk_sku(performance: str) -> str:
    return DISK_SKUS[performance]


def tags(ctx, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Ownership first, then the operator's and the request's own labels: the same names and values as Google's labels,
    which Azure's tag rules all admit (learn.microsoft.com/azure/azure-resource-manager/management/tag-resources)."""
    found = {"managed-by": "cloudinfra", "cloudinfra-request": ctx.request_id, "cloudinfra-name": ctx.name}
    if ctx.owner:
        found["cloudinfra-owner"] = ctx.owner
    for key, value in {**(extra or {}), **ctx.labels}.items():
        found[str(key)[:512]] = str(value)[:256]
    return found


def description(ctx) -> str:
    return f"cloudinfra request {ctx.request_id} ({ctx.name}) owner {ctx.owner or 'unknown'}"
