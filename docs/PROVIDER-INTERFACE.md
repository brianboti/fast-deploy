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
| `resize_compute(resource, cpu, memory_gb, ctx)` | `resize_compute` | Change the shape, keeping a machine's GPUs and the family they need; refuse one the provider has not got with `INVALID_COMPUTE_CONFIGURATION`. `resize_problem(resource, cpu, memory_gb)` says so before any provider call, so a refused resize never touches the machine (the base class answers None). |
| `delete_compute(resource, ctx)` | destroy, cleanup | Delete the machine. |
| `create_database(spec, planned, ctx, network)` | provisioning | The instance, one database, one user with `ctx.database_password`; return `(DatabaseResource, password)`. Encrypted-only, backups on, deletion protection on. |
| `get_database(resource)` / `delete_database(resource, ctx)` | status, destroy | As above; delete turns deletion protection off first. |
| `create_cache(spec, planned, ctx, network)` / `get_cache` / `delete_cache` | provisioning, status, destroy | A private in-memory cache on the network; no credential returned. |
| `create_queue(spec, planned, ctx)` / `get_queue` / `delete_queue` | provisioning, status, destroy | A topic and, when asked, one pull subscription. |
| `create_secret(spec, planned, ctx)` / `get_secret` / `delete_secret` | provisioning, status, destroy | A secret container; a generated first version is written straight to the provider. |
| `create_registry(spec, planned, ctx)` / `get_registry` / `delete_registry` | provisioning, status, destroy | A container image repository in the region; deleting it deletes its images. |
| `create_cluster(spec, planned, ctx, network)` / `get_cluster` / `delete_cluster` | provisioning, status, destroy | A managed Kubernetes cluster with one node pool; no credential returned. |
| `create_load_balancer(spec, planned, ctx, backends, network)` / `get_load_balancer` / `delete_load_balancer` | provisioning, status, destroy | A public load balancer over the given machines; its pieces recorded on the resource for teardown in reverse. |
| `supported_kinds` | validation, `list_resource_kinds` | The kinds the adapter implements; a request for another is `UNSUPPORTED_RESOURCE_KIND` before any call. |
| `refresh(resource)` / `resource_status(resource)` / `delete(resource, ctx)` | status, destroy | Provided by the base class from the `get_*` and `delete_*` methods. |
| `resource_health(resource)` | `get_resource_health` | `Health` from the provider's state. |
| `resource_usage(resource, minutes)` | `get_resource_usage` | `Usage` from the provider's monitoring; metrics as fractions, `null` where nothing was recorded. |

### Optional capabilities

An adapter without one of these refuses it with `UNSUPPORTED_RESOURCE_KIND` (the base class does), and the rest of the
service keeps working: a request with access members still provisions, and the members are recorded.

| Method | Called by | Must |
|---|---|---|
| `stoppable_kinds`, `stop_<kind>(resource, ctx)`, `start_<kind>(resource, ctx)` | `control_infrastructure`, the sweeper | List the kinds that stop and start; stop or start one resource and return it as the provider sees it afterwards (`STOPPED`, `READY`). The base class's `stop`/`start` dispatch by kind. |
| `grantable_kinds`, `takes_grants(kind, storage_type)`, `grant(resource, member, level, ctx)` | provisioning, `manage_infrastructure_access` | Say which resources have a policy of their own (a bucket does, a disk does not; the base class answers by kind); grant a member on one resource this service created, through that policy, and return the roles granted. Never a project-wide grant. A member of a request with nothing that takes grants is `RECORDED`. |
| `revoke(resource, member, ctx)` | `manage_infrastructure_access`, destroy | Remove the member from the roles this adapter grants on that resource, and from nothing else; a resource already gone is not an error. |
| `put_secret_value(resource, value, ctx)` | provisioning | Write a value the service generated (the database password) or copied (the cache AUTH string) as a new secret version; never return or log it. |
| `read_cache_auth(resource)` | provisioning | The cache's AUTH string, to be copied into the request's `cache-auth` secret. |
| `connect_info(resource, resources)` | every answer | A `ConnectInfo` (commands, prerequisites, notes) for a READY or STOPPED resource, from what exists: `resources` holds the request's own and those of other requests it names (a network joined, the machine a disk is attached to). Commands name secrets, never values. Pure. |
| `busy_cores(resource, minutes)` | the sweeper | The busiest CPU sample of that one machine in the window (by its own id, never a namesake's), in cores, or `None` when nothing was recorded. |
| `boot_disk_gb(group)` | validation | The boot disk a group's machines get, which the adapter's software may raise; the per-request disk limit counts it. The base class returns what was asked. |
| `estimate_resources(resources)` | `get_resource_cost` | The monthly cost of resources as they exist now (after a resize, whatever request or profile they came from), from list prices. |
| `profile_infos()` | `list_resource_kinds`, `get_service_status` | The operator's software profiles: name, description, accelerator rule (`required`, `optional`, `none`). |

Every method raises `InfrastructureError` with a code and keeps the provider's own message in `details`; nothing else
escapes. The orchestrator treats any other exception as `INTERNAL_ERROR` and still cleans up, but a good adapter never
lets one out.

`ProvisionContext` carries the request id, the request's name (the prefix of every resource name), region, zone, the
owner, the labels every resource gets, and the generated database password (never logged; `repr` hides it; dropped when
the provisioning worker ends).

`plan` also plans the secrets a request's own credentials go into: `<name>-db-password-<6 hex>` with a database and
`<name>-cache-auth-<6 hex>` with a cache (the request id's start, since secret ids are unique in a project and request names
are not), each with `spec.purpose` (`database_password`, `cache_auth`). The orchestrator links them (`password_secret`,
`auth_secret`), writes their values after the database or cache exists, and deletes the secret with its database or cache.

## GCPProvider

`cloudinfra/providers/gcp/` is the Google Cloud adapter, composed from one module per concern. Only this package names a
Google API; a test fails if any other module of the core does.

| Module | Holds |
|---|---|
| `auth.py` | `GoogleToken`: a token from the metadata server on a Google Cloud machine, else from gcloud (impersonating a configured service account when asked). |
| `api.py` | `GcpApi`: one REST client with the bearer token, HTTP error → `InfrastructureError` translation (401 auth, 403 permission or quota, 404, 409/412 conflict, 429 and 5xx unavailable), and the operation waits for Compute Engine (`operations.wait`), Cloud SQL (poll) and Service Networking (poll). The single seam the unit tests replace with a fake. |
| `translate.py` | Generic → Google shapes: `machine_type(cpu, memory_gb)` (E2 predefined when the ratio matches, E2 custom within 0.5 to 8 GB per vCPU, shared-core for one CPU), image families, disk types, database versions and tiers, the IAP range, zone table, labels and descriptions. |
| `pricing.py` | Approximate list prices for us-central1 and the estimate; `verified` is null until someone checks them. |
| `compute.py` | Instances: insert with OS Login, no service account, shielded VM (Secure Boot on, except on an image without GPU drivers on a machine with GPUs, whose driver installation needs it off), GPUs as guest accelerators with terminate-on-maintenance, `maxRunDuration` with `instanceTerminationAction: STOP` for `max_run_hours`; get (numeric id, last start), aggregated list by labels, stop, start (or resume, for a suspended instance), setMachineType in the machine's GPU family, delete, attachDisk. |
| `cache.py` | Memorystore for Redis: direct peering on the network, AUTH on, TLS, basic or standard-HA tier; the server certificate, and the AUTH string read once (`instances.getAuthString`) to be copied into the request's secret. |
| `queue.py` | Pub/Sub: topic and pull subscription. |
| `secret.py` | Secret Manager: container with automatic replication, optional generated first version, and `addVersion` for a value the service writes. |
| `registry.py` | Artifact Registry: a Docker repository in the region, created and deleted through long-running operations. |
| `iam.py` | Grants on the resources a request created: the policy of a machine, of its IAP tunnel (by numeric id), of a secret, a bucket, a topic and a subscription. Read, change one member's roles from a fixed list, write back with the etag read, retry a conflicting write. Never project IAM. |
| `connect.py` | The connect block of each kind: gcloud ssh and port forwarding through the IAP tunnel, psql or mysql with the password secret's read command, redis-cli with TLS and the AUTH secret, bucket and queue commands, cluster credentials; prerequisites the platform team grants once. |
| `cluster.py` | Kubernetes Engine: zonal cluster, one node pool, VPC-native, shielded nodes, workload identity, regular channel. |
| `loadbalancer.py` | Instance group, health-check firewall rule (the probe ranges differ by type), health check, backend service, URL map and target proxy (http), address, forwarding rule; global for http, regional for tcp. |
| `storage.py` | Persistent disks and buckets (uniform access, public access prevention). |
| `network.py` | Custom-mode VPC, one subnet with Private Google Access, firewall rules (internal, IAP SSH, public ports), Private Services Access (reserved range and peering) for private databases, teardown in the right order. |
| `database.py` | Cloud SQL: instance (Enterprise edition, SSD, backups, encrypted-only, deletion protection, private or public address), database, user; stop and start through `settings.activationPolicy` (`NEVER`, `ALWAYS`); delete after turning protection off. |
| `monitoring.py` | Health from state; CPU, memory and disk utilisation from Cloud Monitoring, a machine's series selected by its numeric id (by name and zone before the id is known); its busiest CPU sample in cores, for idle stop. |
| `settings.py` | The operator's settings, software profiles among them: an image (a family or a pinned image), Secure Boot, a minimum boot disk and an accelerator rule each, and the profile GPU groups get by default. |
| `provider.py` | `GCPProvider(CloudProvider)`: composes the above, plans names, shapes and credential secrets, reads quota. |

### Mapping

| Generic | Google Cloud |
|---|---|
| `compute.cpu`, `compute.memory_gb` | `e2-standard-N` (4 GB per vCPU), `e2-highmem-N` (8), `e2-highcpu-N` (1), `e2-custom-C-M` otherwise; `e2-micro`, `e2-small`, `e2-medium` for one CPU |
| `compute.accelerator` | `t4`: `nvidia-tesla-t4` on `n1-standard-N` or `n1-custom-C-M`; `l4`: `g2-standard-N` (the L4s are built in); `a100`: `a2-highgpu-Ng`; `h100`: `a3-highgpu-8g`; fixed-shape families give the smallest shape covering the request |
| `compute` as a list | one group per entry, machines named `<name>-<group>-<n>` |
| `cache` | Memorystore `redis.instances.create`: `BASIC` or `STANDARD_HA`, `REDIS_7_2` by default, `DIRECT_PEERING` on the network, AUTH and `SERVER_AUTHENTICATION` TLS |
| `queue` | `projects/<p>/topics/<name>-<queue>` and a pull subscription of the same name with the retention asked |
| `secret` | `projects/<p>/secrets/<name>-<secret>`, automatic replication; `generate_value` adds version 1 |
| `registry` | Artifact Registry `repositories.create` in the region, `format: DOCKER`, `dockerConfig.immutableTags` as asked; address `<region>-docker.pkg.dev/<p>/<name>-<registry>` |
| `cluster` | `container.clusters.create` in the zone: node pool `default-pool` with an E2 machine type, `pd-balanced` disks, release channel `REGULAR`, workload pool `<p>.svc.id.goog` |
| `load_balancer: http` | unmanaged instance group with a named port, firewall from 130.211.0.0/22 and 35.191.0.0/16, HTTP health check, `EXTERNAL_MANAGED` backend service, URL map, target HTTP proxy, global address, global forwarding rule |
| `load_balancer: tcp` | the same instance group and firewall, regional TCP health check, `EXTERNAL` regional backend service, regional address and forwarding rule |
| `compute.os` | `ubuntu-os-cloud/ubuntu-2404-lts-amd64`, `debian-cloud/debian-12`, `rocky-linux-cloud/rocky-linux-9` image families |
| `compute.profile` | the profile's image; Secure Boot as the profile says, except a GPU machine on a profile that is not a GPU profile (`accelerator: required`), which carries no driver and boots with it off; the boot disk raised to the profile's minimum. A signed-driver GPU image is built outside the service, with Google's `cuda_installer.pyz build_image`, and pinned in a family the profile names |
| `compute.accelerator` boot disk | 50 GB unless asked; at least 40 GB, Google's minimum for drivers and a toolkit |
| `compute.max_run_hours` | `scheduling.maxRunDuration` with `instanceTerminationAction: STOP` |
| stop and start | `instances.stop` and `instances.start` (`instances.resume` for a suspended instance); Cloud SQL `instances.patch` with `settings.activationPolicy` `NEVER` or `ALWAYS` |
| `access` | `getIamPolicy` and `setIamPolicy` on the instance (`roles/compute.osLogin` or `osAdminLogin`), on `projects/<number>/iap_tunnel/zones/<zone>/instances/<id>` (`roles/iap.tunnelResourceAccessor`), on the secret, the bucket (`storage.buckets.getIamPolicy`/`setIamPolicy`), the topic and the subscription, the repository (`roles/artifactregistry.reader` or `writer`, the only place a `principal://` or `principalSet://` member is granted) |
| credentials | Secret Manager secrets `<name>-db-password-<6 hex>` and `<name>-cache-auth-<6 hex>`, written with `addVersion` |
| `compute.public_ip` | one `ONE_TO_ONE_NAT` access config |
| `storage.type: persistent`, `performance` | `pd-standard`, `pd-balanced`, `pd-ssd` disk in the machine's zone; `attachDisk` with `autoDelete: false` |
| `storage.type: object` | a bucket `<project>-<name>-<6 hex>` in the region, STANDARD class |
| `network` | VPC with `autoCreateSubnetworks: false`; subnet with the CIDR; rules `<name>-net-allow-internal`, `-allow-iap-ssh` (35.235.240.0/20), `-allow-public` |
| private `database` | reserved range `<name>-net-services` (VPC_PEERING, /20) and a `servicenetworking.googleapis.com` connection |
| `database.engine`, `version` | `POSTGRES_13…18` (18 the default; 13 flagged as paid extended support), `MYSQL_5_7` (flagged), `MYSQL_8_0`, `MYSQL_8_4` (default), `MYSQL_9_7` |
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
