"""List prices for estimates on Azure. An estimate from these numbers is a bound for a decision, never a bill.

Two sources, the second replacing the first where it has a figure:
1. the built-in table below: approximate pay-as-you-go Linux prices in eastus2 entered by hand, `verified` null, as the
   Google adapter's are;
2. a snapshot the operator writes with `cloud-infra prices --write FILE` (snapshot() below), from Azure's Retail Prices
   API, which needs no sign-in (learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices), for
   each allowed region, dated: providers.azure.prices_file names it.

Managed disks are sold by tier, a fixed size each (P10 is 128 GiB), so a disk is priced as the smallest tier that holds
it. Reservations, savings plans, Azure Hybrid Benefit, egress, transactions and snapshots are outside the estimate.
"""
import datetime
import json
from pathlib import Path

import httpx

from ...models.response import CostEstimate, CostLine, PlannedResource
from . import translate

HOURS_PER_MONTH = 730
RETAIL = "https://prices.azure.com/api/retail/prices"
RETAIL_VERSION = "2023-01-01-preview"
SOURCE = f"{RETAIL} (Azure Retail Prices API)"
TIER_SIZES = (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32767)
TIERS = {"Premium_LRS": ("P", (1, 2, 3, 4, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80)), "StandardSSD_LRS": ("E", (1, 2, 3, 4, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80)),
         "Standard_LRS": ("S", (None, None, None, 4, 6, 10, 15, 20, 30, 40, 50, 60, 70, 80))}
DISK_PRODUCTS = {"Premium SSD Managed Disks": "Premium_LRS", "Standard SSD Managed Disks": "StandardSSD_LRS", "Standard HDD Managed Disks": "Standard_LRS"}
BUILT_IN = {
    "region": "eastus2",
    "verified": None,
    "vm_hour": {
        "Standard_B1s": 0.0104, "Standard_B1ms": 0.0207,
        "Standard_D2s_v5": 0.096, "Standard_D4s_v5": 0.192, "Standard_D8s_v5": 0.384, "Standard_D16s_v5": 0.768, "Standard_D32s_v5": 1.536,
        "Standard_D48s_v5": 2.304, "Standard_D64s_v5": 3.072, "Standard_D96s_v5": 4.608,
        "Standard_E2s_v5": 0.126, "Standard_E4s_v5": 0.252, "Standard_E8s_v5": 0.504, "Standard_E16s_v5": 1.008, "Standard_E20s_v5": 1.26,
        "Standard_E32s_v5": 2.016, "Standard_E48s_v5": 3.024, "Standard_E64s_v5": 4.032, "Standard_E96s_v5": 6.048,
        "Standard_D2ls_v5": 0.085, "Standard_D4ls_v5": 0.17, "Standard_D8ls_v5": 0.34, "Standard_D16ls_v5": 0.68, "Standard_D32ls_v5": 1.36,
        "Standard_D48ls_v5": 2.04, "Standard_D64ls_v5": 2.72, "Standard_D96ls_v5": 4.08,
        "Standard_NC4as_T4_v3": 0.526, "Standard_NC8as_T4_v3": 0.752, "Standard_NC16as_T4_v3": 1.204, "Standard_NC64as_T4_v3": 4.352,
        "Standard_NC24ads_A100_v4": 3.673, "Standard_NC48ads_A100_v4": 7.346, "Standard_NC96ads_A100_v4": 14.692, "Standard_ND96asr_v4": 27.197,
        "Standard_NC40ads_H100_v5": 6.98, "Standard_NC80adis_H100_v5": 13.96, "Standard_ND96isr_H100_v5": 98.32,
    },
    "disk_month": {  # by tier size in GiB
        "Premium_LRS": {4: 0.77, 8: 1.54, 16: 3.01, 32: 5.28, 64: 10.21, 128: 19.71, 256: 38.01, 512: 73.22, 1024: 135.17, 2048: 259.05, 4096: 495.57,
                        8192: 946.08, 16384: 1802.24, 32767: 3604.48},
        "StandardSSD_LRS": {4: 0.3, 8: 0.6, 16: 1.2, 32: 2.4, 64: 4.8, 128: 9.6, 256: 19.2, 512: 38.4, 1024: 76.8, 2048: 153.6, 4096: 307.2, 8192: 614.4,
                            16384: 1228.8, 32767: 2457.6},
        "Standard_LRS": {32: 1.54, 64: 3.01, 128: 5.89, 256: 11.33, 512: 21.76, 1024: 40.96, 2048: 77.83, 4096: 143.36, 8192: 262.14, 16384: 524.29, 32767: 1048.58},
    },
    "public_ip_hour": 0.005,
    "nat_hour": 0.045,
    "blob_gb_month": {"Standard_LRS": 0.0184, "Standard_ZRS": 0.023},
}
NOTE = ("Approximate pay-as-you-go list prices (reservations, savings plans, Hybrid Benefit, egress, transactions and snapshots excluded); "
        "without a snapshot from the Retail Prices API they are the adapter's own figures for eastus2. Never a bill.")


def tier(size_gb: int, sku: str) -> int:
    """The tier size a disk of this size is sold as: the smallest that holds it."""
    sizes = [size for size, name in zip(TIER_SIZES, TIERS[sku][1], strict=True) if name is not None]
    return next((size for size in sizes if size >= size_gb), sizes[-1])


def load(path: str | None) -> dict:
    """The built-in table, with a snapshot's regions beside it when the operator wrote one. A snapshot that cannot be read
    leaves the built-in table, and every estimate says why."""
    prices = {"built_in": BUILT_IN, "regions": {}, "verified": None, "problem": None}
    if path:
        try:
            snapshot = json.loads(Path(path).read_text())
            prices["regions"] = snapshot.get("regions") or {}
            prices["verified"] = snapshot.get("verified")
        except (OSError, ValueError, AttributeError) as exc:
            prices["problem"] = f"the price snapshot {path} could not be read ({type(exc).__name__}); the built-in prices are used"
    return prices


def _table(prices: dict, region: str | None) -> tuple[dict, bool]:
    """The figures for a region and whether they are the snapshot's; a figure the snapshot lacks comes from the built-in table."""
    found = prices["regions"].get(region or "")
    if not found:
        return BUILT_IN, False
    snapshot_disks = found.get("disk_month") or {}
    return {"vm_hour": {**BUILT_IN["vm_hour"], **(found.get("vm_hour") or {})},
            "disk_month": {sku: {**tiers, **{int(size): price for size, price in (snapshot_disks.get(sku) or {}).items()}} for sku, tiers in BUILT_IN["disk_month"].items()},
            "public_ip_hour": found.get("public_ip_hour", BUILT_IN["public_ip_hour"]), "nat_hour": found.get("nat_hour", BUILT_IN["nat_hour"]),
            "blob_gb_month": {**BUILT_IN["blob_gb_month"], **(found.get("blob_gb_month") or {})}}, True


def disk_month(table: dict, sku: str, size_gb: int) -> float:
    return table["disk_month"][sku].get(tier(size_gb, sku), 0.0)


def line(planned: PlannedResource, prices: dict) -> tuple[CostLine, bool]:
    spec = planned.spec
    table, from_snapshot = _table(prices, spec.get("region"))
    if planned.kind == "compute":
        boot = disk_month(table, translate.OS_DISK_SKU, spec.get("boot_disk_gb", 30))
        address = table["public_ip_hour"] * HOURS_PER_MONTH if spec.get("public_ip") else 0.0
        monthly = table["vm_hour"].get(spec["machine_type"], 0.0) * HOURS_PER_MONTH + boot + address
        basis = f"{spec['machine_type']} for {HOURS_PER_MONTH} hours plus a {spec.get('boot_disk_gb', 30)} GB Standard SSD OS disk"
        basis += " and a static public address" if spec.get("public_ip") else ""
        stopped = round(boot + address, 2)  # deallocated: the disk and a static address still bill
    elif planned.kind == "storage" and spec.get("type") == "persistent":
        monthly = disk_month(table, spec["disk_sku"], spec["size_gb"])
        basis = f"a {spec['size_gb']} GB {spec['disk_sku']} disk, sold as the {TIERS[spec['disk_sku']][0]} tier of {tier(spec['size_gb'], spec['disk_sku'])} GiB"
        stopped = round(monthly, 2)
    elif planned.kind == "storage":
        redundancy = spec.get("redundancy", "Standard_LRS")
        monthly = spec.get("size_gb", 0) * table["blob_gb_month"].get(redundancy, 0.0)
        basis = f"{spec.get('size_gb', 0)} GB of hot {redundancy.split('_')[1]} blobs, if used; a bucket bills by what it holds, and transactions on top"
        stopped = round(monthly, 2)
    elif planned.kind == "network":
        monthly = (table["nat_hour"] + table["public_ip_hour"]) * HOURS_PER_MONTH if spec.get("nat") else 0.0
        basis = "a network, its subnet and security group cost nothing"
        basis += "; its NAT gateway and address bill by the hour, and data through the NAT by the GB on top" if spec.get("nat") else ""
        basis += "; flow logs bill by volume in the operator's storage account" if spec.get("flow_logs") else ""
        stopped = round(monthly, 2)
    elif planned.kind == "identity":
        monthly, basis, stopped = 0.0, "managed identities are free", 0.0
    else:
        monthly, basis, stopped = 0.0, "not priced by this adapter yet", 0.0
    return CostLine(resource=planned.name, kind=planned.kind, monthly_usd=round(monthly, 2), basis=basis, stopped_usd=stopped), from_snapshot


def estimate(planned: list[PlannedResource], prices: dict) -> CostEstimate:
    priced = [line(item, prices) for item in planned]
    lines = [item for item, _ in priced]
    snapshot = any(from_snapshot for _, from_snapshot in priced)
    return CostEstimate(monthly_usd=round(sum(item.monthly_usd for item in lines), 2), stopped_monthly_usd=round(sum(item.stopped_usd or 0 for item in lines), 2),
                        lines=lines, source=SOURCE if snapshot else "the Azure adapter's built-in approximate prices (eastus2)",
                        verified=prices["verified"] if snapshot else None, note=NOTE + (f" Note: {prices['problem']}." if prices.get("problem") else ""))


def as_planned(resource, zoned: bool = False) -> PlannedResource:
    """A resource as it exists now, in the shape an estimate prices; zoned: its region has zones (a bucket there is zone-redundant)."""
    kind = resource.kind.value
    spec = {"region": resource.region}
    if kind == "compute":
        spec.update(machine_type=resource.machine_type, boot_disk_gb=resource.boot_disk_gb, public_ip=bool(resource.public_ip))
    elif kind == "storage":
        spec.update(type=resource.type, size_gb=resource.size_gb, disk_sku=translate.disk_sku(resource.performance or "balanced"),
                    redundancy="Standard_ZRS" if zoned else "Standard_LRS")
    elif kind == "network":
        spec.update(nat=resource.nat, flow_logs=bool(resource.flow_log))
    return PlannedResource(kind=kind, name=resource.name, provider_mapping="existing", spec=spec)


# ---- the snapshot, read from the Retail Prices API by the operator's command
def _items(client: httpx.Client, query: str) -> list[dict]:
    items, url, params = [], RETAIL, {"api-version": RETAIL_VERSION, "currencyCode": "'USD'", "$filter": query}
    for _ in range(200):
        response = client.get(url, params=params)
        response.raise_for_status()
        body = response.json()
        items.extend(body.get("Items") or [])
        url, params = body.get("NextPageLink"), None
        if not url:
            return items
        if not url.startswith(RETAIL):
            raise ValueError(f"the Retail Prices API pointed elsewhere: {url[:80]}")
    raise ValueError("the Retail Prices API did not stop paging")


def _linux_hourly(item: dict) -> bool:
    text = f"{item.get('productName', '')} {item.get('skuName', '')} {item.get('meterName', '')}".lower()
    return item.get("type") == "Consumption" and item.get("unitOfMeasure") == "1 Hour" and "windows" not in text and "spot" not in text and "low priority" not in text


def region_prices(client: httpx.Client, region: str) -> tuple[dict, list[str]]:
    """One region's figures from the Retail Prices API, and the names it had no figure for."""
    found = {"vm_hour": {}, "disk_month": {sku: {} for sku in TIERS}, "blob_gb_month": {}}
    sizes = [size.name for size in translate.SIZES]
    for start in range(0, len(sizes), 10):  # a filter of ten sizes at a time stays well under the API's URL length
        names = " or ".join(f"armSkuName eq '{name}'" for name in sizes[start:start + 10])
        for item in _items(client, f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and priceType eq 'Consumption' and ({names})"):
            if _linux_hourly(item) and item.get("armSkuName") in sizes:
                found["vm_hour"][item["armSkuName"]] = min(float(item["retailPrice"]), found["vm_hour"].get(item["armSkuName"], float("inf")))
    products = " or ".join(f"productName eq '{name}'" for name in DISK_PRODUCTS)
    for item in _items(client, f"serviceName eq 'Storage' and armRegionName eq '{region}' and priceType eq 'Consumption' and ({products})"):
        sku, meter = DISK_PRODUCTS.get(item.get("productName", "")), str(item.get("meterName", ""))
        if sku and meter.endswith(" LRS Disk") and item.get("unitOfMeasure") == "1/Month":
            letter, numbers = TIERS[sku]
            code = meter.split(" ", 1)[0]
            if code.startswith(letter) and code[1:].isdigit() and int(code[1:]) in numbers:
                found["disk_month"][sku][TIER_SIZES[numbers.index(int(code[1:]))]] = float(item["retailPrice"])
    for item in _items(client, f"serviceName eq 'Storage' and armRegionName eq '{region}' and productName eq 'General Block Blob v2' and priceType eq 'Consumption'"):
        meter = str(item.get("meterName", ""))
        if meter in ("Hot LRS Data Stored", "Hot ZRS Data Stored") and float(item.get("tierMinimumUnits") or 0) == 0:
            found["blob_gb_month"]["Standard_" + meter.split(" ")[1]] = float(item["retailPrice"])
    for item in _items(client, f"serviceName eq 'Virtual Network' and armRegionName eq '{region}' and priceType eq 'Consumption'"):
        meter, unit = str(item.get("meterName", "")), item.get("unitOfMeasure")
        if meter == "Standard IPv4 Static Public IP" and unit == "1 Hour":
            found["public_ip_hour"] = float(item["retailPrice"])
        elif item.get("productName") == "NAT Gateway" and meter.endswith("Gateway") and unit == "1 Hour":
            found["nat_hour"] = float(item["retailPrice"])
    missing = [name for name in sizes if name not in found["vm_hour"]]
    missing += [f"{sku} {size} GiB" for sku, tiers in BUILT_IN["disk_month"].items() for size in tiers if size not in found["disk_month"][sku]]
    missing += [name for name in ("public_ip_hour", "nat_hour") if name not in found]
    missing += [f"blob {name}" for name in BUILT_IN["blob_gb_month"] if name not in found["blob_gb_month"]]
    return found, missing


def snapshot(regions: list[str], client: httpx.Client | None = None, today: datetime.date | None = None) -> dict:
    """Every allowed region's figures, dated; what the API had no figure for is named, and the built-in figure stays."""
    client = client or httpx.Client(timeout=60, follow_redirects=False)
    result = {"source": SOURCE, "verified": (today or datetime.date.today()).isoformat(), "currency": "USD", "regions": {}, "missing": {}}
    for region in regions:
        figures, missing = region_prices(client, region)
        result["regions"][region] = figures
        if missing:
            result["missing"][region] = missing
    return result
