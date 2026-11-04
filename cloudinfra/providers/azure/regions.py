"""Azure's public-cloud regions and their availability zones, from "List of Azure regions"
(learn.microsoft.com/azure/reliability/regions-list, updated 2026-10-27).

Zones are logical and numbered per subscription ("1", "2", "3"): zone 1 of one subscription may be another's zone 2, so
a zone is only ever compared within the operator's own subscription. Left out on purpose, for the operator to add under
providers.azure.zones when their subscription has them: regions whose access is restricted to disaster-recovery
scenarios (Australia Central 2, Brazil Southeast, France South, Germany North, Norway West, South Africa West,
Switzerland West, UAE Central), regions in preview (Southeast Asia 3, Taiwan North), the zone in preview in East US 2
(its fourth), and North Central US's zones, which are in preview there. A sovereign cloud's regions are all the
operator's to list.
"""

WITH_ZONES = (
    "australiaeast", "austriaeast", "belgiumcentral", "brazilsouth", "canadacentral", "centralindia", "centralus", "chilecentral", "denmarkeast",
    "eastasia", "eastus", "eastus2", "francecentral", "germanywestcentral", "indiasouthcentral", "indonesiacentral", "israelcentral", "italynorth",
    "japaneast", "japanwest", "koreacentral", "malaysiawest", "mexicocentral", "newzealandnorth", "northeurope", "norwayeast", "polandcentral",
    "qatarcentral", "southafricanorth", "southcentralus", "southeastasia", "spaincentral", "swedencentral", "switzerlandnorth", "uaenorth",
    "uksouth", "westeurope", "westus2", "westus3",
)
WITHOUT_ZONES = ("australiacentral", "australiasoutheast", "canadaeast", "koreasouth", "northcentralus", "southindia", "ukwest", "westcentralus",
                 "westindia", "westus")

ZONES: dict[str, list[str]] = {**{region: ["1", "2", "3"] for region in WITH_ZONES}, **{region: [] for region in WITHOUT_ZONES}}
