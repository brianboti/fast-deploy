"""List prices for estimates. An estimate from these numbers is a bound for a decision, never a bill.

The figures are approximate on-demand list prices for us-central1 entered by hand from Google's public pricing pages; `verified`
stays null until someone checks them against those pages and writes the date. Sustained-use discounts, committed use, free
tiers, egress, snapshots, backups beyond the first copy and operations are outside the estimate.
"""
from ...models.response import CostEstimate, CostLine, PlannedResource
from .translate import disk_type, machine_shape

HOURS_PER_MONTH = 730
PRICING = {
    "source": "https://cloud.google.com/compute/vm-instance-pricing; https://cloud.google.com/compute/disks-image-pricing; "
              "https://cloud.google.com/sql/pricing; https://cloud.google.com/storage/pricing; https://cloud.google.com/vpc/network-pricing",
    "verified": None,
    "currency": "USD",
    "region": "us-central1",
    "e2_vcpu_hour": 0.021811,
    "e2_gb_hour": 0.002923,
    # Other families, per vCPU-hour and GB-hour; the GPU is priced apart. A3 is sold by the machine, so its parts are in the GPU line.
    "family_hour": {"n1": (0.0475, 0.0063), "g2": (0.0425, 0.0057), "a2": (0.0329, 0.0044), "a3": (0.0, 0.0)},
    "gpu_hour": {"nvidia-tesla-t4": 0.35, "nvidia-l4": 0.56, "nvidia-tesla-a100": 2.933, "nvidia-h100-80gb": 11.06},
    "cluster_management_hour": 0.10,
    "cache_gb_hour": {"BASIC": 0.049, "STANDARD_HA": 0.062},
    "secret_version_month": 0.06,
    "forwarding_rule_hour": 0.025,
    "run_idle_vcpu_second": 0.0000025,  # Cloud Run, request-based, idle minimum instances, us-central1
    "run_idle_gib_second": 0.0000025,
    "shared_core_hour": {"e2-micro": 0.008376, "e2-small": 0.016751, "e2-medium": 0.033503},
    "disk_gb_month": {"pd-standard": 0.04, "pd-balanced": 0.10, "pd-ssd": 0.17},
    "external_ip_hour": 0.005,
    "bucket_gb_month": 0.020,
    "sql": {"db-f1-micro": 7.67, "db-g1-small": 25.55, "vcpu_hour": 0.0413, "gb_hour": 0.007, "storage_ssd_gb_month": 0.17, "ha_multiplier": 2.0},
}
NOTE = ("Approximate list prices for us-central1 (sustained-use discounts, free tiers, egress, snapshots and operations excluded); "
        "verify against the pricing pages before relying on a figure. Never a bill.")


def machine_month(machine: str) -> float:
    if machine in PRICING["shared_core_hour"]:
        return PRICING["shared_core_hour"][machine] * HOURS_PER_MONTH
    shape = machine_shape(machine)
    if shape is None:
        return 0.0
    cpu, memory_gb = shape
    family = machine.split("-", 1)[0]
    vcpu_hour, gb_hour = PRICING["family_hour"].get(family, (PRICING["e2_vcpu_hour"], PRICING["e2_gb_hour"]))
    return (cpu * vcpu_hour + memory_gb * gb_hour) * HOURS_PER_MONTH


def tier_month(tier: str) -> float:
    sql = PRICING["sql"]
    if tier in sql:
        return sql[tier]
    if tier.startswith("db-custom-"):
        _, _, cpu, memory_mb = tier.split("-")
        return (int(cpu) * sql["vcpu_hour"] + int(memory_mb) / 1024 * sql["gb_hour"]) * HOURS_PER_MONTH
    return 0.0


def stopped(planned: PlannedResource, running: float) -> float:
    """What a line costs a month while stopped: a machine keeps only its boot disk (an ephemeral address is released and a
    GPU is not billed); a database keeps its storage; everything else cannot stop and costs what it costs running."""
    spec = planned.spec
    if planned.kind == "compute":
        return round(spec.get("boot_disk_gb", 0) * PRICING["disk_gb_month"]["pd-balanced"], 2)
    if planned.kind == "database":
        return round(spec["storage_gb"] * PRICING["sql"]["storage_ssd_gb_month"], 2)
    return running


def line(planned: PlannedResource) -> CostLine:
    spec = planned.spec
    if planned.kind == "compute":
        monthly = machine_month(spec["machine_type"]) + spec.get("boot_disk_gb", 0) * PRICING["disk_gb_month"]["pd-balanced"]
        basis = f"{spec['machine_type']} for {HOURS_PER_MONTH} hours plus a {spec.get('boot_disk_gb', 0)} GB balanced boot disk"
        if spec.get("accelerator"):
            gpu, count = spec["accelerator"]["type"], spec["accelerator"]["count"]
            monthly += PRICING["gpu_hour"].get(gpu, 0.0) * count * HOURS_PER_MONTH
            basis += f" and {count} {gpu}"
        if spec.get("public_ip"):
            monthly += PRICING["external_ip_hour"] * HOURS_PER_MONTH
            basis += " and one public address"
    elif planned.kind == "storage" and spec.get("type") == "persistent":
        monthly = spec["size_gb"] * PRICING["disk_gb_month"][spec["disk_type"]]
        basis = f"{spec['size_gb']} GB of {spec['disk_type']}"
    elif planned.kind == "storage":
        monthly = spec.get("size_gb", 0) * PRICING["bucket_gb_month"]
        basis = f"{spec.get('size_gb', 0)} GB of standard storage, if used; a bucket bills by what it holds"
    elif planned.kind == "database":
        monthly = tier_month(spec["tier"]) * (PRICING["sql"]["ha_multiplier"] if spec.get("high_availability") else 1.0) + spec["storage_gb"] * PRICING["sql"]["storage_ssd_gb_month"]
        basis = f"{spec['tier']}{', high availability' if spec.get('high_availability') else ''} plus {spec['storage_gb']} GB of SSD storage"
    elif planned.kind == "cache":
        monthly = PRICING["cache_gb_hour"][spec["tier"]] * spec["memory_gb"] * HOURS_PER_MONTH
        basis = f"{spec['memory_gb']} GB of {spec['tier'].replace('_', ' ').lower()} cache for {HOURS_PER_MONTH} hours"
    elif planned.kind == "cluster":
        nodes = machine_month(spec["machine_type"]) * spec["node_count"] + spec["node_count"] * spec.get("node_disk_gb", 0) * PRICING["disk_gb_month"]["pd-balanced"]
        monthly = PRICING["cluster_management_hour"] * HOURS_PER_MONTH + nodes
        basis = f"the cluster fee (one zonal cluster per account is free) plus {spec['node_count']} {spec['machine_type']} nodes with {spec.get('node_disk_gb', 0)} GB disks"
        if spec.get("public") is not None:  # GKE's load balancer for the Gateway: a forwarding rule per listener
            rules = 2 if spec["public"].get("hostnames") else 1
            monthly += rules * PRICING["forwarding_rule_hour"] * HOURS_PER_MONTH
            basis += f", and the front door's {rules} forwarding rule{'s' if rules > 1 else ''} (data processed is billed by the GB on top)"
    elif planned.kind == "service":  # request-based billing: only warm instances cost when idle (https://cloud.google.com/run/pricing)
        idle = spec["min_instances"] * (spec["cpu"] * PRICING["run_idle_vcpu_second"] + spec["memory_mb"] / 1024 * PRICING["run_idle_gib_second"]) * HOURS_PER_MONTH * 3600
        monthly = idle
        basis = (f"{spec['min_instances']} warm instance{'s' if spec['min_instances'] != 1 else ''} at the idle rate; requests are billed by use on top"
                 if spec["min_instances"] else "billed by use only: CPU and memory while handling requests, and per million requests")
    elif planned.kind == "load_balancer":
        monthly = PRICING["forwarding_rule_hour"] * HOURS_PER_MONTH
        basis = "one forwarding rule; data processed is billed by the GB on top"
    elif planned.kind == "secret":
        monthly = PRICING["secret_version_month"] * spec.get("versions", 0)
        basis = f"{spec.get('versions', 0)} active versions; accesses are billed per 10,000 on top"
    elif planned.kind == "queue":
        monthly, basis = 0.0, "message delivery is billed by volume after the account's free allowance; nothing while idle"
    else:
        monthly, basis = 0.0, "a network, its subnet and firewall rules cost nothing while idle"
    return CostLine(resource=planned.name, kind=planned.kind, monthly_usd=round(monthly, 2), basis=basis, stopped_usd=stopped(planned, round(monthly, 2)))


def as_planned(resource) -> PlannedResource:
    """A resource as it exists now, in the shape an estimate prices: its machine type after a resize, the disk it has, the
    GPUs it carries, whatever request or profile it came from."""
    kind = resource.kind.value
    spec = {}
    if kind == "compute":
        gpu, _, count = (resource.accelerator or "").rpartition(" x")
        spec = {"machine_type": resource.machine_type, "boot_disk_gb": resource.boot_disk_gb, "public_ip": bool(resource.public_ip),
                "accelerator": {"type": gpu, "count": int(count)} if gpu else None}
    elif kind == "storage":
        spec = {"type": resource.type, "size_gb": resource.size_gb, "disk_type": disk_type(resource.performance or "balanced")}
    elif kind == "database":
        spec = {"tier": resource.tier, "high_availability": resource.high_availability, "storage_gb": resource.storage_gb}
    elif kind == "cache":
        spec = {"tier": resource.tier, "memory_gb": resource.memory_gb}
    elif kind == "cluster":
        spec = {"machine_type": resource.machine_type, "node_count": resource.node_count, "node_disk_gb": resource.node_disk_gb or 0,
                "public": {"hostnames": resource.public.hostnames} if resource.public else None}
    elif kind == "secret":
        spec = {"versions": resource.versions}
    elif kind == "service":
        spec = {"cpu": resource.cpu, "memory_mb": resource.memory_mb, "min_instances": resource.min_instances}
    return PlannedResource(kind=kind, name=resource.name, provider_mapping="existing", spec=spec)


def estimate(planned: list[PlannedResource]) -> CostEstimate:
    lines = [line(item) for item in planned]
    return CostEstimate(monthly_usd=round(sum(item.monthly_usd for item in lines), 2), stopped_monthly_usd=round(sum(item.stopped_usd or 0 for item in lines), 2),
                        lines=lines, source=PRICING["source"], verified=PRICING["verified"], note=NOTE)
