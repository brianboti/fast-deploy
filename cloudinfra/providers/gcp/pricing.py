"""List prices for estimates. An estimate from these numbers is a bound for a decision, never a bill.

The figures are approximate on-demand list prices for us-central1 entered by hand from Google's public pricing pages; `verified`
stays null until someone checks them against those pages and writes the date. Sustained-use discounts, committed use, free
tiers, egress, snapshots, backups beyond the first copy and operations are outside the estimate.
"""
from ...models.response import CostEstimate, CostLine, PlannedResource
from .translate import machine_shape

HOURS_PER_MONTH = 730
PRICING = {
    "source": "https://cloud.google.com/compute/vm-instance-pricing; https://cloud.google.com/compute/disks-image-pricing; "
              "https://cloud.google.com/sql/pricing; https://cloud.google.com/storage/pricing; https://cloud.google.com/vpc/network-pricing",
    "verified": None,
    "currency": "USD",
    "region": "us-central1",
    "e2_vcpu_hour": 0.021811,
    "e2_gb_hour": 0.002923,
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
    return (cpu * PRICING["e2_vcpu_hour"] + memory_gb * PRICING["e2_gb_hour"]) * HOURS_PER_MONTH


def tier_month(tier: str) -> float:
    sql = PRICING["sql"]
    if tier in sql:
        return sql[tier]
    if tier.startswith("db-custom-"):
        _, _, cpu, memory_mb = tier.split("-")
        return (int(cpu) * sql["vcpu_hour"] + int(memory_mb) / 1024 * sql["gb_hour"]) * HOURS_PER_MONTH
    return 0.0


def line(planned: PlannedResource) -> CostLine:
    spec = planned.spec
    if planned.kind == "compute":
        monthly = machine_month(spec["machine_type"]) + spec.get("boot_disk_gb", 0) * PRICING["disk_gb_month"]["pd-balanced"]
        basis = f"{spec['machine_type']} for {HOURS_PER_MONTH} hours plus a {spec.get('boot_disk_gb', 0)} GB balanced boot disk"
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
    else:
        monthly, basis = 0.0, "a network, its subnet and firewall rules cost nothing while idle"
    return CostLine(resource=planned.name, kind=planned.kind, monthly_usd=round(monthly, 2), basis=basis)


def estimate(planned: list[PlannedResource]) -> CostEstimate:
    lines = [line(item) for item in planned]
    return CostEstimate(monthly_usd=round(sum(item.monthly_usd for item in lines), 2), lines=lines, source=PRICING["source"], verified=PRICING["verified"], note=NOTE)
