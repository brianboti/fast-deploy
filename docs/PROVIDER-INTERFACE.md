# The CloudProvider interface

`cloudinfra/providers/base.py` is the one place the infrastructure core meets a cloud. An adapter implements primitives:
creating, reading, changing and deleting each resource kind, plus knowledge (regions, zones, shape rules, planning,
pricing) and reads (health, usage, quota). The orchestrator owns the lifecycle, the state, the order of operations, the
cleanup on failure and the destroy in reverse, so an adapter gets all of that without writing it.

## Methods

| Method | Called by | Must |
|---|---|---|
| `regions()` | validation, status | List the regions the adapter can serve; the operator's `allowed_regions` narrows it. |
| `zones(region)` | planning | List the zones of a region; the first is the default. Empty means unknown, which the validator reports as `INVALID_REGION`. |
| `validate(request)` | validation | Return `Problem`s for shapes the provider cannot offer (a machine type that does not exist, a database version it does not run, a zone not in the region). Pure. |
| `plan(request, ctx)` | planning | Return `PlannedResource`s in creation order: kind, name, `provider_mapping` (the provider operation), `spec` (the chosen shape). Pure and deterministic. The planner adds `spec["resource_id"]`. |
| `estimate_cost(planned)` | planning | A `CostEstimate` from list prices with a line and a basis per resource; say when the prices were verified. |
| `quota(region)` | provisioning | Best effort: `{metric: {limit, usage}}`, or `{}` when it cannot be read; never raise. |
| `create_network(spec, planned, ctx)` | provisioning | Create the network, its subnet and the firewall rules the request implies; return a `NetworkResource` with `provider_id`. |
| `prepare_network_for_database(network, ctx)` | provisioning | Whatever a private managed database needs on that network; return the network as it is afterwards. Default: nothing. |
| `configure_firewall(network, ports, sources, ctx)` | `configure_firewall` | Add one ingress rule; return the `FirewallRule`. |
| `get_network(resource)` | status | The network as the provider sees it now; `DELETED` when gone. |
| `delete_network(resource, ctx)` | destroy, cleanup | Delete rules, peering, subnet, network; a resource already gone is not an error. |
| `create_storage(spec, planned, ctx)` | provisioning | A block disk in the planned zone, or a bucket. |
| `attach_storage(storage, compute, ctx)` | provisioning, `attach_storage` | Attach a disk without auto-delete; refuse a bucket or a zone mismatch with `INVALID_STORAGE_CONFIGURATION`. |
| `get_storage(resource)` / `delete_storage(resource, ctx)` | status, destroy | As above; a bucket with objects is refused with `PROVIDER_CONFLICT`, never emptied. |
| `create_compute(spec, planned, ctx, network)` | provisioning | A machine in the given network (or the operator's default), with the ownership labels; no key, script or service account from the request. |
| `get_compute(resource)` / `list_compute(labels)` | status, `list_compute` | The machine now; every machine carrying the labels. |
| `resize_compute(resource, cpu, memory_gb, ctx)` | `resize_compute` | Change the shape; refuse one the provider has not got with `INVALID_COMPUTE_CONFIGURATION`. |
| `delete_compute(resource, ctx)` | destroy, cleanup | Delete the machine. |
| `create_database(spec, planned, ctx, network)` | provisioning | The instance, one database, one user with `ctx.database_password`; return `(DatabaseResource, password)`. Encrypted-only, backups on, deletion protection on. |
| `get_database(resource)` / `delete_database(resource, ctx)` | status, destroy | As above; delete turns deletion protection off first. |
| `refresh(resource)` / `resource_status(resource)` | status | Provided by the base class from the `get_*` methods. |
| `resource_health(resource)` | `get_resource_health` | `Health` from the provider's state. |
| `resource_usage(resource, minutes)` | `get_resource_usage` | `Usage` from the provider's monitoring; metrics as fractions, `null` where nothing was recorded. |

Every method raises `InfrastructureError` with a code and keeps the provider's own message in `details`; nothing else
escapes. The orchestrator treats any other exception as `INTERNAL_ERROR` and still cleans up, but a good adapter never
lets one out.

`ProvisionContext` carries the request id, the request's name (the prefix of every resource name), region, zone, the
owner, the labels every resource gets, and the generated database password (never logged; `repr` hides it).

## GCPProvider

`cloudinfra/providers/gcp/` is the Google Cloud adapter, composed from one module per concern. Only this package names a
Google API; a test fails if any other module of the core does.

| Module | Holds |
|---|---|
| `auth.py` | `GoogleToken`: a token from the metadata server on a Google Cloud machine, else from gcloud (impersonating a configured service account when asked). Moved here from `labdeploy/cloud.py`, which re-exports it. |
| `api.py` | `GcpApi`: one REST client with the bearer token, HTTP error → `InfrastructureError` translation (401 auth, 403 permission or quota, 404, 409/412 conflict, 429 and 5xx unavailable), and the operation waits for Compute Engine (`operations.wait`), Cloud SQL (poll) and Service Networking (poll). The single seam the unit tests replace with a fake. |
| `translate.py` | Generic → Google shapes: `machine_type(cpu, memory_gb)` (E2 predefined when the ratio matches, E2 custom within 0.5 to 8 GB per vCPU, shared-core for one CPU), image families, disk types, database versions and tiers, the IAP range, zone table, labels and descriptions. |
| `pricing.py` | Approximate list prices for us-central1 and the estimate; `verified` is null until someone checks them. |
| `compute.py` | Instances: insert with OS Login, no service account, shielded VM; get, aggregated list by labels, stop/setMachineType/start, delete, attachDisk. |
| `storage.py` | Persistent disks and buckets (uniform access, public access prevention). |
| `network.py` | Custom-mode VPC, one subnet with Private Google Access, firewall rules (internal, IAP SSH, public ports), Private Services Access (reserved range and peering) for private databases, teardown in the right order. |
| `database.py` | Cloud SQL: instance (Enterprise edition, SSD, backups, encrypted-only, deletion protection, private or public address), database, user; delete after turning protection off. |
| `monitoring.py` | Health from state; CPU, memory and disk utilisation from Cloud Monitoring. |
| `provider.py` | `GCPProvider(CloudProvider)`: composes the above, plans names and shapes, reads quota. |

### Mapping

| Generic | Google Cloud |
|---|---|
| `compute.cpu`, `compute.memory_gb` | `e2-standard-N` (4 GB per vCPU), `e2-highmem-N` (8), `e2-highcpu-N` (1), `e2-custom-C-M` otherwise; `e2-micro`, `e2-small`, `e2-medium` for one CPU |
| `compute.os` | `ubuntu-os-cloud/ubuntu-2404-lts-amd64`, `debian-cloud/debian-12`, `rocky-linux-cloud/rocky-linux-9` image families |
| `compute.public_ip` | one `ONE_TO_ONE_NAT` access config |
| `storage.type: persistent`, `performance` | `pd-standard`, `pd-balanced`, `pd-ssd` disk in the machine's zone; `attachDisk` with `autoDelete: false` |
| `storage.type: object` | a bucket `<project>-<name>-<6 hex>` in the region, STANDARD class |
| `network` | VPC with `autoCreateSubnetworks: false`; subnet with the CIDR; rules `<name>-net-allow-internal`, `-allow-iap-ssh` (35.235.240.0/20), `-allow-public` |
| private `database` | reserved range `<name>-net-services` (VPC_PEERING, /20) and a `servicenetworking.googleapis.com` connection |
| `database.engine`, `version` | `POSTGRES_13…17`, `MYSQL_5_7`, `MYSQL_8_0`, `MYSQL_8_4` |
| `database.size` | `db-f1-micro`, `db-custom-2-7680`, `db-custom-4-15360` |
| `database.high_availability` | `availabilityType: REGIONAL` |
| ownership | labels `managed-by=cloudinfra`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner`; a description on resources without labels |

## Adding a provider

1. `cloudinfra/providers/<name>/provider.py` with a class implementing every abstract method above; keep the provider's
   API client in one module so tests can replace it, as `tests/cloudinfra/gcp_fakes.py` replaces `GcpApi`.
2. A settings model in `cloudinfra/config.py` under `Providers` (project or account, allowed regions, whatever the
   credential discovery needs; never the credential itself).
3. `register("<name>", factory)` in `cloudinfra/providers/__init__.py`, and the name removed from `PLANNED_PROVIDERS`.
4. Tests against a fake of the provider's API: the bodies sent, the resources read back, the error translation, and the
   lifecycle through `Orchestrator` (the orchestration tests take any adapter).

Nothing above the registry changes.
