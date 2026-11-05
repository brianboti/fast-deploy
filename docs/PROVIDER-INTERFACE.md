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
| `resolve_member(member)` | provisioning (before the first create), `manage_infrastructure_access` | For an adapter whose grants name an id rather than the member as written: the member's `{"id", "type"}`, kept on the request's access entry (`principal`) and handed back in `ProvisionContext.principals`, so it is looked up once. A member the request names that the provider's directory does not know raises `INVALID_ACCESS`, and nothing of the request is created; the requester's own identity (added by `grant_requester`, perhaps another provider's account) is recorded `FAILED` with the reason instead. The base class answers None (Google grants by the address). |
| `identity_member(resource)` | provisioning | The member a workload identity the adapter made is granted as: `serviceAccount:<email>` by default, `serviceAccount:<object id>` on Azure. |
| `finished(ctx)` | destroy, cleanup, the last single delete | Nothing of the request is left: remove a container the adapter keeps for it (Azure's resource group). Raising leaves the destroy `DESTROY_FAILED` with the container named; destroying again tries again. The base class does nothing. |
| `preflight(request, planned)` | provisioning, after the quota check and before the first create | What the provider's own listings say it would refuse, such as a machine size it does not offer this account in that zone (Azure reads its Resource SKUs, an hour's cache per region); each is a `Problem`, and the request fails with nothing made. Best effort, as quota: a listing that cannot be read holds nothing up. The base class answers [] |
| `warnings(request, planned)` | `plan`, `provision` | What the plan should say about the provider's shapes: a size raised to its floor, a limit it applies (Azure: AKS's system pool, Container Apps' fixed memory and timeout). The base class answers [] |
| `enforces_run_limit` | the sweeper | True (the default) when the provider stops a machine after `max_run_hours` itself, as Google does; False, and the sweeper stops it on the service's own clock (Azure). |

Every method raises `InfrastructureError` with a code and keeps the provider's own message in `details`; nothing else
escapes. The orchestrator treats any other exception as `INTERNAL_ERROR` and still cleans up, but a good adapter never
lets one out.

`ProvisionContext` carries the request id, the request's name (the prefix of every resource name), region, zone, the
owner, the labels every resource gets, the members' provider ids kept with the access list (`principals`), and the
generated database password (never logged; `repr` hides it; dropped when the provisioning worker ends).

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
| `compute.py` | Instances: insert with OS Login, no project-wide SSH keys, no serial port, deletion protection (lifted by delete), no service account unless a group names an identity at `use`, shielded VM (Secure Boot on, except on an image without GPU drivers on a machine with GPUs, whose driver installation needs it off), GPUs as guest accelerators with terminate-on-maintenance, `maxRunDuration` with `instanceTerminationAction: STOP` for `max_run_hours`; get (numeric id, last start), aggregated list by labels, stop, start (or resume, for a suspended instance), setMachineType in the machine's GPU family, delete, attachDisk. |
| `cache.py` | Memorystore for Redis: direct peering on the network, AUTH on, TLS, basic or standard-HA tier; the server certificate, and the AUTH string read once (`instances.getAuthString`) to be copied into the request's secret. |
| `queue.py` | Pub/Sub: topic and pull subscription. |
| `secret.py` | Secret Manager: container with automatic replication (in the request's region under the operator's key when one is set), optional generated first version, and `addVersion` for a value the service writes. |
| `registry.py` | Artifact Registry: a Docker repository in the region, created and deleted through long-running operations. |
| `iam.py` | Grants on the resources a request created: the policy of a machine, of its IAP tunnel (by numeric id), of a secret, a bucket, a topic and a subscription. Read, change one member's roles from a fixed list, write back with the etag read, retry a conflicting write. Never project IAM. |
| `connect.py` | The connect block of each kind: gcloud ssh and port forwarding through the IAP tunnel, psql or mysql with the password secret's read command, redis-cli with TLS and the AUTH secret, bucket and queue commands, cluster credentials; prerequisites the platform team grants once. |
| `cluster.py` | Kubernetes Engine: zonal cluster, one node pool, VPC-native, shielded and private nodes, workload identity, Dataplane V2, Binary Authorization, security posture, authorized networks from the operator, regular channel. |
| `loadbalancer.py` | Instance group, health-check firewall rule (the probe ranges differ by type), health check, backend service (logged; the operator's Cloud Armor policy on http), URL map and target proxy (http; with hostnames a managed certificate, an SSL policy MODERN with TLS 1.2, an HTTPS proxy and a redirect on 80), address, forwarding rule; global for http, regional for tcp. |
| `storage.py` | Persistent disks and buckets (uniform access, public access prevention). |
| `network.py` | Custom-mode VPC, one subnet with Private Google Access and flow logs, logged firewall rules (internal, IAP SSH, public ports), a Cloud Router with Cloud NAT, with `deny_egress` egress rules and a DNS response policy for private.googleapis.com, Private Services Access (reserved range and peering) for private databases, teardown in the right order. |
| `database.py` | Cloud SQL: instance (Enterprise edition, SSD, backups, encrypted-only, deletion protection, the CIS flags of the engine, IAM database authentication, a root password nobody keeps, the operator's KMS key, private or public address), database, user; stop and start through `settings.activationPolicy` (`NEVER`, `ALWAYS`); delete after turning protection off. |
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

## AzureProvider (every kind)

`cloudinfra/providers/azure/` is the Microsoft Azure adapter, built in the steps of
[research/RESEARCH-2026-11-03-azure.md](research/RESEARCH-2026-11-03-azure.md), section 10. Only this package names an
Azure endpoint or resource provider; a test fails if any other module of the core does. Four steps are in place: the
credential, client, clouds, regions and quota (step 1); release A's kinds, **identity, network, storage and compute**,
with grants, stop and start, resize, connect blocks, metrics and prices (step 2); the managed services, **database,
cache, queue, secret and registry** (step 4); and the platform kinds, **cluster, service, job and load balancer**
(step 5). What Azure lacks is refused before any call with the reason, as section 6 of the research note decides: a
cluster's front door (Application Gateway for Containers is a preview add-on on AKS), a service reachable from the
internet, a request timeout above 240 seconds, a schedule outside UTC. Nothing here has run against a real
subscription yet: the user chose to build every kind first (2026-11-05), then run them all.

| Module | Holds |
|---|---|
| `settings.py` | `AzureSettings`: the subscription and its directory (tenant), the cloud (`public`, `usgovernment`, `china`), the allowed regions, zones the adapter's table leaves out, which user-assigned identity of the host to use, the software profiles (a Marketplace image with or without a plan, or a Compute Gallery image), the default subnet, the Bastion and its subnet, `deny_egress`, the flow-log account, the price snapshot, the operator's registry and the prefixes clusters may pull from, the clusters' authorized ranges, the public domains and Front Door WAF policy of load balancers, whether services may be public, and the app registration services' Entra sign-in names. No credential. |
| `clouds.py` | Each cloud's Resource Manager, sign-in host and token audiences (Resource Manager, Graph, Key Vault, the database sign-in, and for the public cloud the metrics plane, Storage and Service Bus), as each cloud's own metadata document gave them on 2026-11-03; the documents are test fixtures, compared with the table. |
| `auth.py` | `AzureToken`: a token per audience from the host, the first of an AKS pod's workload identity (its federated token exchanged with Entra ID), a Container Apps or App Service identity endpoint, a machine's instance metadata service, or the Azure CLI on a laptop; the one that answered is remembered. Kept in memory per audience until a minute before it ends. |
| `api.py` | `AzureApi`: one REST client, with the api-version of each resource type (`API_VERSIONS`, the latest stable on each template reference page on 2026-11-04). A 429 and a call another operation held are tried again after Retry-After (reported instead when Azure asks for more than a minute); a PUT, DELETE or GET is tried again after a 5xx or a lost answer, a POST is not; a 401 once with a fresh token. Writes wait for their long-running operation through `Azure-AsyncOperation`, then `Location`, then the resource's `provisioningState`, and follow those addresses only on the host the call went to. Azure's error code (`RequestDisallowedByPolicy`, `AnotherOperationInProgress`, `SkuNotAvailable`, `ScopeLocked`…) decides the service's. The single seam `tests/cloudinfra/azure_fakes.py` replaces. |
| `naming.py` | Each resource type's naming rule, the resource group of a request (`<name>-<8 hex of the request id>`), and names unique across all of Azure (storage accounts, key vaults, registries, Service Bus namespaces, flexible servers, Redis) salted with the same eight hex digits. |
| `regions.py` | The public cloud's regions and their numbered zones (`1`, `2`, `3`), from Microsoft's region list of 2026-10-27; regions with restricted access or zones in preview are left to the operator. |
| `translate.py` | Generic → Azure shapes: the sizes (Dsv5, Esv5, Dlsv5, B1s and B1ms; NCasT4_v3, NC_A100_v4, ND96asr_v4, NCads_H100_v5, ND96isr_H100_v5) with each family's quota name and Trusted Launch, the built-in images, disk SKUs, the software rules of a profile, and tags. |
| `groups.py` | The request's resource group: made by the first create (once per process), deleted by `finished` with anything in it that carries the request's tag, or no tag and the request's name (the parts Azure makes with a machine carry none); anything else in it keeps the group and says so. |
| `network.py` | Network security group (internal, Bastion SSH, public ports, the load-balancer probes, then deny; with `deny_egress` only the network and Entra ID outbound), a NAT gateway and its address in the request's zone, the virtual network with one subnet and no default outbound access, and, when the request holds a service or job, a second subnet `<network>-apps` (a /27 delegated to Container Apps, the machines' subnet then the first half of the range), a flow log through the region's network watcher (named with the request id, since the watcher's group is shared); an added rule from priority 200; the delete removes the network's Container Apps environment first. |
| `compute.py` | Machines with their interface, public address and OS disk made in one PUT, each with `deleteOption: Delete`, Trusted Launch, a system-assigned identity, the `AADSSHLoginForLinux` extension where the machine reaches the internet (it installs a package; elsewhere the connect block says so), a throw-away administrator key, the request's identity when a group names one; get with the power state, and the addresses from the parts the machine itself names; the subscription-wide list by tags, deallocate and start, resize (one PATCH on the same hardware, else deallocate around it), disks attached at the next LUN, delete with its parts. |
| `storage.py` | Managed disks in the request's zone; buckets as a storage account (no keys, no public access, TLS 1.2, soft delete seven days, zone-redundant where the region has zones) with one container `data`, deleted only when the blob service lists no blob. |
| `identity.py` | User-assigned managed identities; their principal id is what grants name, their client id what a workload asks for tokens with. |
| `access.py` | Members resolved through Microsoft Graph (`users/{address}`, else `users?$filter=mail eq`, `groups?$filter=mail eq`), or taken as written when an object id; role assignments on each resource's own scope, named from scope, principal and role, with `principalType`; a revoke deletes every role of the table. |
| `connect.py` | `az network bastion ssh`/`tunnel` through the operator's Bastion with Entra sign-in (`az ssh vm` without one), `az storage blob … --auth-mode login`, the identity's client id. |
| `monitoring.py` | Health from state; a machine's `Percentage CPU` from Azure Monitor, mean and peak, and its busiest minute in cores for idle stop. |
| `secret.py` | One Key Vault per request (Azure RBAC, soft delete seven days, no purge protection), made with its first secret; values written through the vault's own API by a service that can set, delete and purge a secret but never read one; a secret with no value is a disabled empty version until a member writes one; the vault deleted and purged when nothing of the request is left. |
| `queue.py` | One Service Bus Standard namespace per request (local authentication off: Entra sign-in only), a topic per queue and its subscription. |
| `registry.py` | A request's registry as a repository prefix (`<name>-<registry>-<8 hex>`) in the operator's registry; nothing made until a push; its repositories deleted through the registry's own API with a registry token the service gets by exchanging its own. |
| `database.py` | PostgreSQL and MySQL flexible servers: the request's user is the administrator, a CanNotDelete lock as deletion protection (waited out on delete), the database child, a private endpoint when private; stop and start. |
| `cache.py` | Azure Managed Redis: the smallest Balanced size holding the memory, its database (TLS, port 10000, OSS clustering), a private endpoint, the primary key copied into the request's secret. |
| `private.py` | Private endpoints and the private DNS zones they are found by, the zones kept with the network (a network takes one zone of a name) and removed with it. |
| `pricing.py` | Approximate built-in pay-as-you-go prices for eastus2, a dated snapshot from the Retail Prices API (`cloud-infra prices --write`) that replaces them where it has a figure, disks priced by the tier that holds them. |
| `cluster.py` | AKS: one Standard-tier cluster with one system pool (AKS's floor of two 4-vCPU nodes, raised to and said so), Azure CNI overlay with Cilium, Azure Linux 3, the request's subnet and its NAT gateway, Entra sign-in only with Azure RBAC; a control-plane identity granted Network Contributor on the subnet first; the kubelet identity granted Repository Reader under the named registry prefixes; a federated credential per request identity; the admission policy and the request's namespace through the cluster's API (`cloudinfra/providers/kubernetes.py`, shared with GKE). |
| `apps.py` | Container Apps: the network's internal environment on the Consumption profile; services with a runtime identity, ingress on the environment's internal load balancer, scaling, and Entra sign-in in front (an allow-list of object ids and groups, started with nobody); jobs, manual or on a UTC schedule. |
| `loadbalancer.py` | A Standard public load balancer with an IP-based pool of the machines, a probe and a rule, an NSG rule at the first free priority from 300; with hostnames, Front Door Standard in front (endpoint, origin group, the load balancer as origin, a custom domain and managed certificate per hostname, a route that redirects HTTP, the operator's WAF policy). |
| `provider.py` | `AzureProvider(CloudProvider)`: composes the above, validates and plans (each machine's and cluster node's family quota named in its spec), says in `warnings` what Azure raised or gives differently, reads quota (regional and family vCPUs, machines, Standard public addresses), the wait ceiling of each kind. |

Zones on Azure are logical and numbered per subscription: a request's `zone` is `1`, `2` or `3`, and a region without
zones takes none. The contract's region and zone rules accept Azure's names (`eastus2`, `1`) beside Google's.

### Mapping on Azure

| Generic | Azure |
|---|---|
| a request | a resource group `<name>-<8 hex>` with the ownership tags; everything the request makes is in it, and it goes when the request is gone |
| `compute.cpu`, `compute.memory_gb` | exactly one size: `Standard_D<n>s_v5` (4 GB per vCPU), `Standard_E<n>s_v5` (8), `Standard_D<n>ls_v5` (2), for 2 to 96 vCPUs; `Standard_B1s`, `B1ms` for one; any other shape refused with the sizes there are (no custom shapes) |
| `compute.accelerator` | `t4`: `NC<4,8,16,64>as_T4_v3`; `a100`: `NC<24,48,96>ads_A100_v4`, `ND96asr_v4` for 8; `h100`: `NC40ads_H100_v5`, `NC80adis_H100_v5`, `ND96isr_H100_v5` for 8; the smallest covering the request; `l4` refused |
| `compute.os`, `compute.profile` | `Canonical:ubuntu-24_04-lts:server:latest`, `Debian:debian-12:12-gen2:latest`, `resf:rockylinux-x86_64:9-base:latest` (with its plan); a profile's Marketplace or gallery image; the OS disk at least 30 GB, Standard SSD |
| `compute` security | Trusted Launch with vTPM, Secure Boot as the software allows; `securityType: Standard` on the ND sizes |
| sign-in | Entra ID: the `AADSSHLoginForLinux` extension (on a machine that reaches the internet) and Virtual Machine User Login (`use`) or Administrator Login (`admin`) on the machine, Reader on its interface for the Bastion |
| `compute.public_ip` | a Standard static public address made with the machine, deleted with it |
| `compute.max_run_hours` | no Azure equivalent: the sweeper deallocates the machine |
| stop and start | `deallocate` (never `powerOff`: an allocated machine bills) and `start`; a machine shut down from inside is `UNKNOWN` with an error saying it still bills |
| resize | `PATCH hardwareProfile.vmSize`, after `deallocate` when the machine's hardware does not offer the size; a machine keeps its GPUs and Trusted Launch |
| `storage.type: persistent`, `performance` | a managed disk in the request's zone: `Standard_LRS` (standard), `StandardSSD_LRS` (balanced), `Premium_LRS` (ssd), at most 32,767 GB; attached at the next LUN with `deleteOption: Detach` |
| `storage.type: object` | a storage account `<name><8 hex>` and its container `data`, `Standard_ZRS` (`Standard_LRS` where the region has no zones); Storage Blob Data Contributor (`use`) or Owner (`admin`) on the container |
| `identity` | a user-assigned managed identity `<name>-<identity>`, granted as `serviceAccount:<principal id>` |
| `network` | `<name>-net` with subnet `<name>-subnet`, security group `<name>-net-nsg` (rules `-allow-internal`, `-allow-bastion-ssh`, `-allow-public`, `-allow-probes`, `-deny-inbound`), NAT gateway `<name>-net-nat` with address `<name>-net-nat-ip`, flow log `<name>-net-<8 hex>-flowlog` |
| `access` | one role assignment per (resource scope, member, role), its name a UUID from the three; members' object ids from Graph, kept with the access list |
| quota | Compute usages (`cores`, `virtualMachines`, every family's vCPUs) and the Standard public addresses of the Network usages |
| `secret` | a secret `<name>-<secret>` in the request's vault `<name>-<8 hex>`; Key Vault Secrets User (`use`) or Officer (`admin`) at the secret's own scope; credentials as `<name>-db-password` and `<name>-cache-auth` there |
| `queue` | topic `<name>-<queue>` and subscription of the same name (TTL from `retention_hours`, lock from `ack_deadline_seconds`, at most 300) in the request's namespace `<name>-<8 hex>`; Data Sender on the topic, Data Receiver on the subscription |
| `registry` | the prefix `<name>-<registry>-<8 hex>/` in the operator's registry; Container Registry Repository Reader (`use`, and anyone but a registry writer) or Writer (the operator's registry writers at `admin`) on the registry, conditioned to the prefix; `immutable_tags` refused until the delivery service locks tags on Azure |
| `database.engine`, `version` | PostgreSQL 14 to 18 (18 the default; 13 refused), MySQL 8.0 (sent as `8.0.21`; 8.4 is in a preview API only, 5.7 closed): `flexibleServers` named `<name>-db-<8 hex>` |
| `database.size`, `storage_gb`, `high_availability` | `Standard_B1ms` (Burstable, no standby), `Standard_D2ds_v5`, `Standard_D4ds_v5`; storage raised to PostgreSQL's next disk size (32 GB at least) or MySQL's 20; `ZoneRedundant`, or `SameZone` in a region without zones |
| private `database`, `cache` | `publicNetworkAccess: Disabled`, a private endpoint `<name>-pe` in the network's subnet (`postgresqlServer`, `mysqlServer`, `redisEnterprise`), and the zone `privatelink.postgres.database.azure.com`, `privatelink.mysql.database.azure.com` or `privatelink.redis.azure.net` in the network's group, linked to it |
| `cache` | Azure Managed Redis `<name>-<8 hex>`, `Balanced_B1` to `B350` by memory, Redis 7.4 only, high availability only when asked, its access key in `<name>-cache-auth` |
| `cluster` | AKS `managedClusters` `<name>-<cluster>`: Standard tier, one `System` pool of `count` (2 at least) nodes of the exact size (4 vCPUs at least, never B-series), `AzureLinux3`, `vnetSubnetID` the request's subnet, Azure CNI overlay with Cilium, `outboundType: userAssignedNATGateway` (a network without NAT, or whose rules refuse egress, is refused), a rule `<name>-<cluster>-pods` in the subnet's security group admitting the pod range to itself (overlay traffic between nodes is not encapsulated), `aadProfile {managed, enableAzureRBAC}`, `disableLocalAccounts`, OIDC issuer and workload identity, `authorizedIPRanges` from the operator; control-plane identity `<name>-<cluster>-control` with Network Contributor on the subnet; members get Azure Kubernetes Service Cluster User Role on the cluster and RBAC Reader (`use`) or Writer (`admin`) at `<cluster>/namespaces/<request's namespace>`; `public` refused |
| `cluster.registries` | prefixes in the operator's registry (`LOGIN_SERVER/PREFIX`), from the operator's `cluster_registries`; the kubelet identity gets Container Registry Repository Reader conditioned to them |
| `identity` with a cluster | a federated credential `<name>-<cluster>` on the identity: the cluster's issuer, subject `system:serviceaccount:<namespace>:<identity>`, audience `api://AzureADTokenExchange` |
| `service` | a container app `<name>-<service>` (32 characters at most) in the network's internal environment `<network>-apps` (Consumption profile, its own /27): `cpu` 1, 2 or 4 with 2 GiB per vCPU, `min_instances`/`max_instances`, an HTTP scale rule at `concurrency`, a runtime identity `<name>-<service>-run` with Key Vault Secrets User on its named secrets; not `public`: Entra sign-in for the operator's app registration, members on its allow-list at `use` and `admin`, Container Apps Operator at `admin` (revisions, logs, a console; never Contributor, which could turn the sign-in off); `ingress: all` and `timeout_seconds` above 240 refused |
| `job` | a Container Apps job `<name>-<job>` in the same environment: `Manual`, or `Schedule` with the cron in UTC (another `time_zone` refused), `replicaTimeout`, `replicaRetryLimit`, `parallelism` and `replicaCompletionCount` from `tasks`; Jobs Reader (`use`) or Jobs Contributor (`admin`, who alone starts it: Azure's start may change the image) |
| `load_balancer` | a Standard public load balancer `<name>-lb` with address `<name>-lb-ip`, the machines in an IP-based pool by their private addresses in the network their interfaces are on (machines of two networks refused), a TCP or HTTP probe, one rule on the port (no outbound SNAT); `<name>-lb-allow` in that network's security group from `Internet`, to the machines' addresses only (nothing on the operator's subnet) |
| `load_balancer.hostnames` | Front Door Standard `<name>-lb-fd` in front: the load balancer's address as origin, a custom domain per hostname with a managed certificate (TLS 1.2), HTTP redirected; the security rule from `AzureFrontDoor.Backend` only, which admits any tenant's profiles, so the profile's `front_door_id` is reported for the application's `X-Azure-FDID` check; hostnames in the operator's `public_domains` |
| ownership | tags `managed-by=cloudinfra`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner` (and `cloudinfra-group` on machines) |

## Adding a provider

1. `cloudinfra/providers/<name>/provider.py` with a class implementing every abstract method above; keep the provider's
   API client in one module so tests can replace it, as `tests/cloudinfra/gcp_fakes.py` replaces `GcpApi` and
   `azure_fakes.py` replaces `AzureApi`. A command may run only in the adapter's token helper (`auth.py`).
2. A settings model in `cloudinfra/config.py` under `Providers` (project or account, allowed regions, whatever the
   credential discovery needs; never the credential itself).
3. `register("<name>", factory)` in `cloudinfra/providers/__init__.py`, and the name removed from `PLANNED_PROVIDERS`;
   `<Name>Settings.described()` says what the status shows about the account (Google: the project; Azure: the subscription
   and cloud).
4. Tests against a fake of the provider's API: the bodies sent, the resources read back, the error translation, and the
   lifecycle through `Orchestrator` (the orchestration tests take any adapter).

Nothing above the registry changes.
