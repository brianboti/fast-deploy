# The infrastructure layer: the runs on Azure

The record of every run of the infrastructure layer on a real Azure subscription, in order. Each run says what held,
what it found and what was fixed, what it could not test, and ends with everything deleted. The Google runs are in
[DELIVERY-SPIKE.md](DELIVERY-SPIKE.md); the plan the runs follow is section 10 of
[research/RESEARCH-2026-11-03-azure.md](research/RESEARCH-2026-11-03-azure.md).

## 1. The first run, on a student subscription (2026-11-05)

Run between 21:42 and 23:15 UTC by Claude for the operator, from branch `azure-real-run` (from `main` at `4e28c6f`,
with steps 1, 2, 4 and 5 of the adapter merged). `cloudinfra` ran on the operator's laptop as the operator's own Entra
account (`az login`), driven through its REST API with a caller token made for the run. Nothing was deployed by the
delivery service, which has no Azure side yet (step 6).

**The subscription.** Azure for Students, in a university's Entra directory: $100 of credit, no
card, the operator its Owner. What it allows decided the run's shape:

| Limit | Value | Effect |
|---|---|---|
| Regions | an "Allowed resource deployment regions" policy: westus, northcentralus, canadacentral, belgiumcentral, denmarkeast | eastus2, the adapter's default in the documents, is refused |
| vCPU quota | 6 per region; 0 for the Dsv5, Esv5 and Dlsv5 families the adapter's sizes come from; 4 for B (B1s, B1ms); 3 public addresses per region | only one-vCPU machines; no cluster (two nodes of four vCPUs) |
| Sizes offered (Resource SKUs) | canadacentral and westus offer this subscription none of the adapter's sizes (canadacentral: only ARM `B*ps_v2` with quota); denmarkeast offers B1s and B1ms in zones 1 to 3 | machines in denmarkeast |
| Services by region | Container Apps exists in canadacentral and not in denmarkeast | services and jobs in canadacentral |
| Front Door | "Free Trial and Student account is forbidden for Azure Frontdoor resources" | no load balancer with hostnames |
| Quota increases | none for free and student subscriptions | as above |

**Set up by hand for the run:** the 14 resource providers registered (by the operator); a custom role `cloudinfra real
run data actions` with the four data actions of [REQUIREMENTS.md](REQUIREMENTS.md) (blob read; Key Vault `setSecret`,
`delete`, `purge`), which Azure accepted name by name, assigned to the operator at the subscription (by the operator,
as the session's own grant was refused); the operator is Owner, which carries every control-plane action and no data
action. The configuration allowed denmarkeast and canadacentral, an operation timeout of 1,800 seconds, a placeholder
`service_auth_client_id` (no app registration was made in the university's directory) and `example.com` as a public
domain. Each request had a lifetime of four hours ending in a destroy, so that nothing could be forgotten.

**What held:**

| Request | Result |
|---|---|
| a network open on port 22 to the laptop's own /32 | refused before any call: SSH is never opened to a public range, on Azure as on Google; members reach machines through the operator's Bastion |
| `r1` (denmarkeast): an identity, a network, two B1s machines (Ubuntu with the identity, Debian with an 8 GB disk), the operator at `admin` | `READY`. Machines: Trusted Launch with Secure Boot and vTPM, password sign-in off, zone 1, `SystemAssigned` (and the request's identity on `web`), no public address; `AADSSHLoginForLinux` installed and `Succeeded` on both through the NAT. Subnet: no default outbound access, the NAT gateway, the security group with `allow-internal`, `allow-probes`, `deny-inbound`. The disk attached at LUN 0 of `app` |
| `r1`'s grants | the operator: Virtual Machine Administrator Login on each machine, Reader on each interface; the request's identity at `use`: Virtual Machine User Login and Reader. The interfaces' names carry a suffix Azure adds (`-nic-2d443986`), which the adapter reads from the machine (the step 2 review's fix) |
| stop and start of `r1` | both machines `VM deallocated` in 28 seconds; the lifecycle named the three disks as still billing; started again in under two minutes |
| resize of `app` | B1s to B1ms while stopped in 14 seconds, still stopped; refused with `INVALID_STATE` while the request was starting; B1ms back to B1s while running, in place, in 27 seconds |
| revoke and grant of the operator | revoke left no role on `r1`; a grant at `use` gave User Login, not Administrator Login |
| `r2` (denmarkeast): a bucket, a small PostgreSQL database, a 1 GB cache, a queue, a secret with a generated value, on a network without NAT | `READY` in 18 minutes: the bucket in 35 seconds, the database in about 7 minutes, the cache in about 9. Bucket: ZRS, no shared keys, no public blobs, TLS 1.2. Database: PostgreSQL 18 on B1ms, public access off, a private endpoint and `privatelink.postgres.database.azure.com` linked to the network, Entra sign-in on beside the password, backups seven days, the `cloudinfra-deletion-protection` lock. Cache: Balanced_B1, public access off, TLS 1.2, encrypted on port 10000, OSS clustering, a private endpoint. Queue: a Standard namespace with local authentication off. Vault: RBAC, soft delete seven days. The creating response named the database's and the cache's secrets (`credentials`) |
| as the operator, with the grants `r2` gave | a file written to and listed in the bucket with Entra sign-in (the account has no keys); a message sent to the topic with an Entra token for Service Bus |
| `r3` (canadacentral): a network, a secret, a service and a scheduled job, the operator at `admin` | `READY` in 5 minutes. The network's subnet `10.30.0.0/25` and a `/27` delegated to Container Apps; the environment internal, zone-redundant, on the Consumption profile. The service's Entra sign-in on (`Return401`, audience `api://<client id>`), its allow-list the operator's object id alone once granted (nobody's stand-in gone). Roles: each runtime identity Key Vault Secrets User on its own secret; the operator Container Apps Operator on the app and Jobs Contributor on the job. Four reads of the job met Azure's 500 and were tried again by the client |
| `r4` (denmarkeast): a load balancer over `r1`'s machines, another request's | `READY` in under 45 seconds; its rule in `r1`'s security group, port 80 from `Internet` to the two machines' addresses only, at priority 300; but its pool was empty (below) |
| `r6`: the same load balancer, with the pool fix | its pool held both machines |
| destroy | `r4` in 44 seconds, its rule gone from `r1`'s security group; `r6`; `r3` in 30 minutes, 28 of them Azure deleting the Container Apps environment; `r2` refused its bucket while it held the file, as it must ("the service deletes only empty buckets"), then went through once the file was deleted (and the zone fix, below); `r1` |

**What the run found.** Each fix is in [CHANGES.md](CHANGES.md), with a test in
`tests/cloudinfra/test_infra_real_run_2026_11_05_azure.py` (or beside the client) that fails without it.

| Request | What happened | Fix |
|---|---|---|
| the first `r1` | the identity's create answered 201 with `Location: /subscriptions/…`, a relative address naming the identity itself; the client took it for another host and stopped. The cleanup deleted what was made | a relative address is read on the call's own host, with the write's `api-version` when it has none; one on another host is still never followed |
| the second `r1`, in canadacentral | Azure refused B1s with `SkuNotAvailable` (capacity restrictions) at the machine's create, after the network and the disk were made; the cleanup deleted them. The research note had planned a check before any call that was never built | `CloudProvider.preflight`, read after the quota: on Azure the Resource SKUs listing, cached an hour per region; a size not offered in the request's region and zone is refused before anything is made, naming the adapter's sizes offered there or the zones the size is offered in |
| every event | each line of the service's log was written twice: a library (the MCP SDK) puts a `RichHandler` on the root logger, and the service's events reached it too | the service's logger stops at its own handlers |
| `r2` | reading the request's status while it was provisioning crashed with `IndexError`: the database was ready before its password's secret was made, and its connect block read the vault from the secret's placeholder name | a secret not made yet gets no command; the status reads |
| `r4` | the load balancer was `READY` with an empty pool: Azure keeps no IP-based member named in the load balancer's own PUT, so it would have sent traffic nowhere | the members go by the pool's own PUT and are read back; a pool Azure left short fails the create |
| `r5`, a load balancer with a hostname | Front Door is forbidden to free and student subscriptions; the request cleaned up after itself | none: the subscription's |
| destroy of `r2` | the redis zone's delete came a second after its network link's had ended, and Azure answered `CannotDeleteResource`, still counting the link; the network stayed | the zone's delete is tried again, six times ten seconds apart |
| every secret's delete | the purge met 409 at once, the secret's delete still running, and was left to the vault's purge at the end: right for a destroy, but a secret deleted on its own would stay recoverable for seven days | a 409 is tried again as a secret not yet among the deleted ones is |
| destroy of `r3` | Azure took 28 minutes to delete the Container Apps environment, against a ceiling of 30 | the ceiling is 45 minutes; an operator offering services or jobs raises the operation timeout to 2,700 seconds ([REQUIREMENTS.md](REQUIREMENTS.md)) |

**Not tested here**, by the subscription's limits, and kept for a run on one with quota: clusters (AKS), every size
above one vCPU and every GPU, Front Door and HTTPS on a hostname, Entra sign-in on a machine through Bastion (no Bastion
was made), a call into the internal Container Apps environment (canadacentral offers no machine to call it from) and
the service's sign-in with a real app registration, registries (no operator registry), `deny_egress` and flow logs.

**Everything was deleted.** The six requests through cloudinfra's own destroy (`r2` on the second try); then, by hand,
the probe file, the run's custom role and its assignment. No vault was left soft-deleted, and no role assignment was
left on any resource. Azure's own `NetworkWatcherRG`, with the network watchers it makes for a region's first network,
was left as Azure keeps it, and the resource providers stay registered.
