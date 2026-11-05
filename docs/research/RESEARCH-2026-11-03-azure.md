# Adding Microsoft Azure as the second provider: what to know before building it. 2026-11-03

Written 2026-11-03 UTC as a read-only review, before any code is changed. The user asked for Azure support, "very
strong", and for everything the implementation should know first. The repository holds `cloudinfra` 1.38.0 (one
adapter, Google Cloud) and `appdelivery` 0.2.0.

Method: the reviewer read the code the adapter must fit (section 2); four research threads then read Microsoft's
documentation and the primary sources on (A) the platform's API conventions, authentication, RBAC, pricing, quota
and monitoring; (B) compute, networking, storage, load balancing and machine identity; (C) the managed data services,
Key Vault, the container registry, AKS and Container Apps; (D) governance, the security benchmarks, cost management,
the trusted-build and execute-on-machine equivalents, and what the multi-cloud tools learned about Azure. The
reviewer then re-read on the primary page the six claims that change a recommendation (marked F+). Each source is
marked F (read on the page), F+ (re-read by the reviewer), or S (a search snippet or a secondary source); section 11
lists them. Where a page answered 404 or a number did not render, the note says so and the fact is a question for
the first real run (section 9.2).

Acronyms: ARM (Azure Resource Manager, the control plane every Azure API shares), RBAC (role-based access control),
ABAC (attribute-based access control: a condition on a role assignment), Entra ID (Microsoft's identity service,
formerly Azure AD), IMDS (the instance metadata service on a virtual machine), NSG (network security group), ACR
(Azure Container Registry), AKS (Azure Kubernetes Service), LRO (a long-running operation), SKU (a product size or
tier), CAF (Microsoft's Cloud Adoption Framework), MCSB (the Microsoft cloud security benchmark), IAP (Google's
Identity-Aware Proxy, the tunnel the Google adapter uses for SSH).
## 1. The short answer

**Azure fits the interface, and the hard parts are not where Google's were.** The `CloudProvider` primitives map
one to one onto Azure Resource Manager: every create is an idempotent `PUT` of a name the service chooses, which is a
better fit for `accepted()` than Google's insert-then-operation; a resource group per request gives a clean unit of
ownership and teardown; role assignments at any resource scope, through one call shape, give "grant on what the
service made, never on the project" for every kind, with a built-in role made for delegating that right under an
allow-list condition; and list prices come from an unauthenticated API instead of a hand-typed table. Thirteen kinds
have an Azure product each, all generally available, and none needs a field the contract lacks.

Six things are different in kind, not only in name, and the design must take them head on:

1. **One token per audience.** Resource Manager, Key Vault, Graph, Storage, the metrics plane and a cluster's API
   server each want their own bearer token; `GoogleToken`'s one-audience cache becomes a cache keyed by audience, and
   every host (a VM, Container Apps, AKS, a laptop) has a different token endpoint.
2. **Members are object ids, not e-mails.** A role assignment names an Entra object id and a principal type; the
   service resolves `user:`, `group:` and `serviceAccount:` members through Microsoft Graph before the first create (three
   application permissions a privileged administrator grants once) and stores the id, with raw ids as the fallback.
3. **Five names are global**, and two cannot be reused soon after a delete (a Key Vault for its soft-delete period, a
   Service Bus namespace for four hours); and a subscription holds at most 5,000 role assignments, fixed, which
   per-resource grants spend quickly, so the login roles go on the request's resource group and the data roles on
   the resources.
4. **A stopped machine bills.** Only `deallocate` stops the meter; there is no maximum run duration, no hibernation
   under Trusted Launch, and a delete leaves disks, NIC and address behind unless each is told to go.
5. **Two products are chosen for the contract, not for Azure's habits:** the cache is Azure Managed Redis (Azure
   Cache for Redis is closed to new customers since 2026-04-01 and retires 2028-09-30), and a `registry` is a
   repository prefix in the operator's registry under ABAC repository permissions, not a registry per request.
6. **The delivery service's trust story is weaker.** Nothing on Azure builds an image from a commit *and* signs a
   provenance statement as the cloud: ACR Tasks builds inside the subscription with a run record, and the platform
   must write and sign the statement itself; AKS cannot yet enforce signatures in production. Reaching a machine goes
   through Bastion with Entra authentication (or Run Command, with no network path at all), and there is no
   Container-Optimized OS: the container host is the operator's image.

What the user asked for, Azure support "very strong", is reachable: the mapping (section 5) covers every kind and
every capability the Google adapter has, with the refusals listed (section 6) rather than faked. The order of work
(section 10) is the same as the delivery service's: the seams and the fake first, release A's kinds, a real run on a
sandbox subscription, the managed services, clusters and serverless, then the delivery service. The nine facts the
documentation leaves open (section 9.2) are the first real run's checklist.
## 2. What the adapter must fit: the repository side

Read from the code on 2026-11-03 (`cloudinfra` 1.38.0, `appdelivery` 0.2.0). Nothing here is from a web page.

**The interface is provider-neutral; its edges are not.** `CloudProvider` (`cloudinfra/providers/base.py`) asks an
adapter for knowledge (`regions`, `zones`, `validate`, `plan`, `estimate_cost`), creation, reading and deletion per
kind, and the optional capabilities (stop/start, grants, secret values, connect blocks, usage). The orchestrator owns
the order, the state, the cleanup and the destroy; `ProvisionContext.accepted()` records a resource as soon as the
provider accepts its create. All of that carries over. Six things around the interface are Google-shaped and must be
generalised before, or with, a second adapter:

| Where | Today | Why Azure breaks it |
|---|---|---|
| `security/validation.py` `MEMBER` | `user:`, `group:` or `serviceAccount:` and an e-mail address; `allowed_domains` checks the address's domain | Azure role assignments take an Entra **object id** (a GUID) and a `principalType`; a user has a UPN that looks like an e-mail, a group has a display name and maybe a mail, a service principal or managed identity has no address at all (section 4.4) |
| `providers/gcp/auth.py` `GoogleToken` | one token for one audience (`cloud-platform`) | Azure issues one token **per audience**: ARM, Key Vault data plane, Microsoft Graph, Azure Monitor, Storage, Service Bus, the AKS server application (section 4.1); the Azure token class is a cache keyed by audience |
| `Config._consistent` and `configured_providers()` | `providers: configure at least one provider (gcp)`; the tuple `("gcp",)` | mechanical: both become "any provider block set" |
| `limits.operation_timeout_seconds` | default 900, maximum 3600 | several Azure creates take longer than 15 minutes (section 9.1) |
| `providers/gcp/settings.py` profiles | an image is a Google image path | an Azure image is a four-part reference (publisher, offer, sku, version) or a gallery image id, and a Marketplace image may need a plan accepted (section 5.2) |
| `tests/cloudinfra/test_infra_catalog.py` | Google is named only under `providers/gcp/` and in the catalog's mapping column | the same test, with Azure's words (`management.azure.com`, `azurerm`, `az `, `microsoft.`), over `providers/azure/` |

Smaller edges, each one line of code: `request.region` is validated against the adapter's `regions()` and the
operator's `allowed_regions` (Azure names are `eastus2`, `westeurope`); `request.zone` is optional and the adapter's
first zone is the default (Azure zones are `1`, `2`, `3`, logical per subscription, and some regions and sizes have
none; section 6); `Resource.provider_id` is free text (an ARM resource id fits); `ComputeResource.instance_id` holds
"the provider's numeric id, which grants that outlive a name are bound to" (Azure has `vmId`, a GUID; a role
assignment is bound to the resource id, which is the name, so it is less needed); `StorageResource.url` is a `gs://`
URL today (Azure: `https://<account>.blob.core.windows.net/<container>`); `ClusterResource.registries` is described as
Artifact Registry repositories (Azure: ACR repositories `<registry>.azurecr.io/<repository>`); `SecretResource.resource_name`
and `connect.commands` name `gcloud` commands (the Azure adapter writes `az` ones).

**The contract needs no new field for Azure.** Every block is a size, a count, a name, a version or a switch. Three
Google words sit in field descriptions and one in a field name: `network.iap_ssh` ("the provider's identity-aware
proxy range"), `cluster.registries` ("Artifact Registry repositories"), `service.ingress`'s
`internal_and_load_balancer` and the `connect` purposes. Renaming `iap_ssh` would break callers; its description can
say what it means on each provider ("SSH only through the provider's identity-aware path: Google's IAP range, Azure's
Bastion subnet or no rule at all when Run Command is the path"). The catalog's `provider` input says "gcp (default) is
the one implemented" and `PLANNED_PROVIDERS` names azure: both change when the adapter registers.

**What the Google adapter is, as a yardstick.** 4,198 lines in 24 modules (`provider.py` 442, `run.py` 271,
`api.py` 276, `cluster.py` 263, `translate.py` 239, `network.py` 226), one REST client with a fake of 808 lines in the
tests, and the real-run notes. An Azure adapter of the same scope is the same order of size; the parts that are
cheaper on Azure (resource-group delete, idempotent PUT, one role-assignment call for every kind) are paid back by the
parts that are dearer (per-audience tokens, Entra object ids, the private DNS zones managed databases need, the
Container Apps environment, the two data planes of Key Vault and Service Bus, Marketplace plans).

**The delivery service is Google-shaped in four places**, and they are the whole of its cloud surface
(`appdelivery/google.py`, `remote.py`, `cloudbuild.py`, `provenance.py`, `run.py`, `kube.py`'s credentials):
(1) the host's token and the per-environment token minted through the IAM Credentials API; (2) reaching a machine with
`gcloud compute ssh --tunnel-through-iap` as the environment's account, and the Container-Optimized OS profile with
Docker on it; (3) the build: Cloud Build from a connected repository at a commit, and Google's signed SLSA provenance
checked by `provenance.py`; (4) the targets' own APIs: Cloud Run services and jobs, GKE credentials. Each has an Azure
counterpart (section 7), none is a drop-in, and the delivery service has no provider seam today: it imports
`google.py` directly. Azure support in the delivery service is a second change after the infrastructure adapter, and
it starts by cutting the same seam there that `cloudinfra` already has.
## 3. The Azure platform model, as it meets the adapter

**A subscription is the project; a resource group is the unit of life.** Microsoft's own guide for Google engineers
says a project "is conceptually similar to the Azure subscription, in terms of billing, quotas, and limits. However, in
function, a Google Cloud project more closely resembles an Azure resource group" (F, [GCP professional](https://learn.microsoft.com/en-us/azure/architecture/gcp-professional/)).
Resources in a group "should share the same lifecycle. You deploy, update, and delete them together"; deleting the group
deletes everything in it; a resource is in exactly one group; a resource may be in another region than its group; a
resource may use resources of another group (F, [ARM overview](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/overview)).
The Cloud Adoption Framework expects a company to hand a platform service a subscription in a "Corp" or "Online"
landing zone, policy already inherited from the management groups above it, and says never to assign workload teams
rights at management-group scope (F, [management groups](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/landing-zone/design-area/resource-org-management-groups)).

So the natural shape is **one resource group per request** (`<prefix>-<request id's first 6 hex>` or the request's
name), tagged with the ownership tags, holding everything the request creates, and deleted as the last step of a
destroy after the resources inside were deleted in reverse. It also answers two Azure limits at once: 800 resources per
type per group, and 980 groups per subscription (F, [subscription limits](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/azure-subscription-service-limits)):
a request never nears the first, and the second caps active requests per subscription at a few hundred, which the
operator's `max_active_requests_per_principal` already keeps far below. A network joined by a later request
(`network_id`) stays in its own group: a NIC in one group may use a subnet in another, and the Virtual Machine
Contributor role carries `subnets/join/action` for exactly that (F, [compute roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/compute)).
A virtual network is regional, so a machine and its network are in one region (F, [VNet FAQ](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-faq)).

**Every write is an idempotent PUT by a name the caller chooses.** Azure's REST guidelines prefer PUT because it lets
"the customer name their own resource" and require every method to be idempotent (F, [Azure REST guidelines](https://github.com/microsoft/api-guidelines/blob/vNext/azure/Guidelines.md)).
That fits `ProvisionContext.accepted()` better than Google's insert-then-operation: the adapter records the resource id
it chose before the PUT, a crash is recovered by a GET on the same path (404: never made; otherwise read
`provisioningState`), and a repeated PUT of the same body is harmless. Concurrent writes to one resource make one
caller win and the other get 409 (F, ARM overview).

**Long-running operations.** A create answers 201 or 202; the resource's `provisioningState` is `Succeeded`,
`Failed` or `Canceled` when done and anything else (`Creating`, `Updating`, `Accepted`…) while running. The adapter
polls the `Azure-AsyncOperation` header's URL (`{status, error}`), falls back to `Location` (202 while running, 200 or
204 when done), honours `Retry-After`, and backs off itself when there is none; Compute says to track the operation
rather than the resource (F, [async operations](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations);
F, [throttling errors](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/troubleshooting-throttling-errors)).
A delete answers 200 (done), 202 (running, with `Location`) or 204 (nothing there), so a repeated delete is safe
(F, [VM delete](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/delete)). This is one `wait` in
`AzureApi`, as `GcpApi` has three.

**Throttling is per principal and per region, and deeper than Google's.** ARM's token bucket per subscription, per
service principal and per region: reads 250 with 25 a second refilled, writes and deletes 200 with 10 a second;
headers `x-ms-ratelimit-remaining-subscription-reads|writes|deletes`; a 429 carries `Retry-After` and a call sent
before it elapses is not processed (F, [request limits](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling)).
The Compute provider has its own buckets per resource and per subscription per minute (a VM's PUT, start, stop,
deallocate, run command: 4 refilled, 12 cap per resource; 500 and 1,500 per subscription; a "high-cost" list 300 and
900 per subscription), answered with `x-ms-ratelimit-remaining-resource: Microsoft.Compute/HighCostGet;159` and a 429
whose body is `OperationNotAllowed` / `TooManyRequests` with a `Retry-After` that can be 1,200 seconds (F, [Compute
throttling](https://learn.microsoft.com/en-us/azure/virtual-machines/compute-throttling-limits)). The Network provider
answers 429 `RetryableErrorDueToAnotherOperation` while another operation holds a resource, and parallel subnet creates
on one network fail with `AnotherOperationInProgress` (F, request limits; S, a Terraform issue). Three rules follow
for `AzureApi`: GET a machine by name, never list; honour `Retry-After` exactly, as `GcpApi` does; serialise the
writes to one virtual network (the planner's creation order does that for a request; two requests joining one
network need a lock in the adapter).

**Names.** A resource group name is 1 to 90 characters; a Linux VM name 1 to 64; network names 1 to 80 (2 to 64 for a
network and a private endpoint); and five kinds are **globally unique across all of Azure**: storage accounts (3 to 24
lowercase alphanumerics), key vaults (3 to 24), container registries (5 to 50 alphanumerics), Service Bus namespaces (6
to 50), PostgreSQL and MySQL flexible servers (3 to 63) and Redis caches (1 to 63) (F, [name rules](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules)).
Google has this only for buckets, and the adapter already salts a bucket's name with the project and six hex digits
of the request id. On Azure every globally unique name gets a salt, and the naming goes into one module, as Google's
`translate.py` holds its image families. Two names cannot be reused soon after a delete: a Key Vault's for up to 90
days (soft delete, section 5.4) and a Service Bus namespace's for 4 hours (F, [Service Bus FAQ](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-faq)),
which is why the salt must change when a request is recreated with the same name.

**Tags, locks, the audit trail.** Up to 50 tags per resource, name up to 512 characters (128 on a storage account) and
value 256, names case-insensitive; not inherited from the group; a private DNS zone takes only 15; role assignments,
locks, federated credentials and Marketplace agreements take none (F, [tag resources](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-resources);
F, [tag support](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-support)). The
ownership labels (`managed-by`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner`) fit as they are. A
platform team's "inherit a tag from the resource group" policy (`Modify` effect) rewrites a request before the provider
sees it, so tags read back may differ from tags sent, and a "require a tag" policy denies a create without it (F,
[Modify effect](https://learn.microsoft.com/en-us/azure/governance/policy/concepts/effect-modify)). Deletion
protection is a management lock, `Microsoft.Authorization/locks/<name>` with `level: CanNotDelete`, on the resource:
inherited by children, a lock on any one resource blocks deleting the whole group, and creating or deleting one needs
`Microsoft.Authorization/locks/*`, which Contributor does not have (F, [locks](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources)).
Microsoft gives no figure for how long a deleted lock is still enforced; a Terraform module fix found a delete 62 ms
after the lock's deletion still refused, so the adapter reads the lock until 404 and then retries a `ScopeLocked`
refusal (S, [AVM fix](https://github.com/Azure/terraform-azurerm-avm-res-compute-virtualmachine/pull/430)). The
Activity Log records every control-plane write with its caller for 90 days, free, and is "the only place that stores
the creator of a resource" (F, [Activity Log](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/activity-log));
it stands where Google's Admin Activity audit logs stand in the security assessment.

**Policy denials are a coded error.** A PUT a platform policy refuses answers `RequestDisallowedByPolicy` with the
assignment and definition ids in the body; the common-errors table also names `AnotherOperationInProgress`,
`ResourceGroupBeingDeleted`, `StorageAccountAlreadyTaken`, `SkuNotAvailable`, `OperationNotAllowed` (quota or SKU),
`InUseSubnetCannotBeDeleted`, `SubscriptionRequestsThrottled`, `MarketplacePurchaseEligibilityFailed` (F, [common
errors](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/common-deployment-errors);
F, [policy denial](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-policy-requestdisallowedbypolicy)).
`translate_http_error` for Azure keys on the body's `error.code`, not only the HTTP status: `RequestDisallowedByPolicy`
→ `PROVIDER_PERMISSION_DENIED` with the policy named in `details`; `AnotherOperationInProgress`,
`RetryableErrorDueToAnotherOperation`, `ResourceGroupBeingDeleted` → retry; `SkuNotAvailable`, `OperationNotAllowed`
with "quota" → `QUOTA_EXCEEDED`; `*AlreadyTaken`, `NameInUse`, `Conflict` → `PROVIDER_CONFLICT`.

**Resource providers must be registered** in the subscription once, as APIs are enabled on a Google project:
`Microsoft.Compute`, `Network`, `Storage`, `KeyVault`, `ContainerRegistry`, `ContainerService`, `DBforPostgreSQL`,
`DBforMySQL`, `Cache`, `ServiceBus`, `ManagedIdentity`, `App`, `Insights` (metrics answer 403 without it), and
`MarketplaceOrdering` when a Marketplace image is used; registration needs `{namespace}/register/action`, which
Contributor has and the service's role should not (F, [resource providers](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-providers-and-types);
F, [metrics troubleshooting](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/metrics-troubleshoot)).

**Sovereign clouds are a configuration, not a fork.** An unauthenticated `GET https://management.azure.com/metadata/endpoints?api-version=2022-09-01`
returns the cloud's login endpoint, the ARM audiences, the Graph and `ossrdbms` resource ids and the DNS suffixes of
Key Vault, Storage, ACR, PostgreSQL and MySQL; the same path on `management.usgovcloudapi.net` or
`management.chinacloudapi.cn` returns that cloud's values (F, observed on 2026-11-03; F, [Azure Government](https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure);
F, [Azure China](https://learn.microsoft.com/en-us/azure/china/concepts-service-availability)). `providers.azure.cloud`
(`public` by default) selects the ARM host, and every other endpoint and audience is read from that document once.
## 4. Identity: the service's own, and the members it grants

### 4.1 The service's credential: no key file, one token per audience

The same rule as on Google: the host's identity, never a file. Four token sources, by where the service runs:

| Host | Source | What the adapter calls |
|---|---|---|
| A virtual machine | the Instance Metadata Service | `GET http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=<audience>` with header `Metadata: true`; `client_id` selects a user-assigned identity; answer `access_token`, `expires_on` (POSIX seconds); on 404 or 429 back off, on 410 wait up to 70 s, never through a proxy (F, [VM token](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token)) |
| Container Apps, App Service, a Container Apps job | a local token service, not IMDS | `GET $IDENTITY_ENDPOINT?resource=<audience>&api-version=2019-08-01` with header `X-IDENTITY-HEADER: $IDENTITY_HEADER`; the back end caches a token per audience for about 24 hours, so a new role assignment can take a day to show in a token (F, [Container Apps identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity); F, [App Service identity](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity)) |
| AKS with workload identity | a projected token file | the webhook sets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_AUTHORITY_HOST`; the file is re-read on every exchange (Kubernetes rotates it hourly); it is exchanged at Entra's v2 token endpoint, `grant_type=client_credentials` with a `jwt-bearer` client assertion, for a scope `<audience>/.default` (F, [workload identity](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview)) |
| A laptop | the Azure CLI | `az account get-access-token --resource <audience>` (or `--scope`); answer `accessToken`, `expires_on` (POSIX, since CLI 2.54.0); "valid for at least 5 minutes with the maximum at 60"; the identity from `az account show` and its object id from `az ad signed-in-user show` (F, [az account](https://learn.microsoft.com/en-us/cli/azure/account)) |

The one real difference from `GoogleToken`: **a bearer token is for one audience**, its `aud` claim is the resource
asked for, and a token for Resource Manager is refused by Key Vault, Graph, Storage, Service Bus, the metrics data
plane and a cluster's API server. The adapter needs a token for each of these:

| Audience | For |
|---|---|
| `https://management.azure.com/` | every ARM call: create, read, delete, role assignments, locks, the ARM metrics path, quota |
| `https://vault.azure.net` | writing a secret's value (the data plane, `PUT https://<vault>.vault.azure.net/secrets/<name>?api-version=7.5`) (F, [Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide)) |
| `https://graph.microsoft.com` | resolving a member's e-mail or group to an object id (section 4.4) |
| `https://metrics.monitor.azure.com` | the batch metrics endpoint for idle stop over many machines (section 5) (F, [metrics batch](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch)) |
| `https://storage.azure.com/` | checking that a bucket is empty before deleting its account (section 5.3) (F, [Storage auth](https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory)) |
| `https://servicebus.azure.net` | nothing in the adapter today (members use it) (F, [Service Bus auth](https://learn.microsoft.com/en-us/azure/service-bus-messaging/authenticate-application)) |
| `6dae42f8-4368-4678-94ff-3960e28e3630` | the AKS API server, the same application id in every cloud; `kubelogin -l msi|azurecli|workloadidentity` does it for a laptop (F, [kubelogin](https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication)) |
| `https://ossrdbms-aad.database.windows.net` | a member's Entra sign-in to a PostgreSQL or MySQL flexible server; the token is the password (F, [PostgreSQL with managed identity](https://learn.microsoft.com/en-us/azure/postgresql/security/security-connect-with-managed-identity)) |
| the registry itself | an ACR login is an exchange of an Entra token at `POST https://<registry>/oauth2/exchange` for an ACR refresh token (F, [ACR OAuth](https://github.com/Azure/acr/blob/main/docs/AAD-OAuth.md)); the delivery service's pull path, not the adapter's |

So `AzureToken` is `GoogleToken` with the audience as a key: one cache per audience, each entry kept until a minute
before `expires_on`, invalidated on a 401. All four sources accept the audience as a parameter, so the class stays one
class. Everything above is plain HTTP; the SDK is not needed for it.

**SDK or REST.** Microsoft's own page says the libraries "build on top of the underlying Azure REST API… However, you
can always use the REST API directly from Python code" (F, [SDK overview](https://learn.microsoft.com/en-us/azure/developer/python/sdk/azure-sdk-overview)).
The SDK route for this adapter is about fourteen packages (`azure-identity`, `azure-mgmt-resource`, `-compute`,
`-network`, `-storage`, `-keyvault`, `-containerregistry`, `-containerservice`, `-rdbms`, `-redis`, `-servicebus`,
`-msi`, `-appcontainers`, `-authorization`, `-monitor`), each pinned and hash-locked, bringing `msal`, `requests`,
`isodate` and `azure-core` (F, PyPI metadata read 2026-11-03); the Google adapter has zero. The REST route keeps the
dependency list, the fakes and the boundary tests exactly as they are and costs one `wait` and one error translator.
**Recommendation: raw REST with httpx, as the Google adapter is**, and borrow two numbers from `azure-core`: its retry
of 408, 429, 500, 502, 503 and 504 with `Retry-After`, and `ARMPolling`'s 30-second default poll (F, [azure-core](https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme)).
If the delivery service's image needs the `az` CLI anyway (section 7), that is a separate decision: the CLI is a
14.7 MB wheel with 108 dependencies, 66 of them `azure-mgmt-*`, and does not belong in `cloudinfra`'s image.

### 4.2 The service's role: a custom role plus a conditioned grant right

Google gave the service predefined roles and one conditioned `projectIamAdmin`. Azure's shape is the same with better
tools. A **custom role definition** (`{Actions, NotActions, DataActions, AssignableScopes}`, 5,000 per tenant, no
wildcard in scopes, created with `roleDefinitions/write` on each assignable scope; F, [custom roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/custom-roles))
lists exactly the operations the adapter sends, by provider: `Microsoft.Resources/subscriptions/resourceGroups/*`,
`Microsoft.Compute/virtualMachines/*`, `/disks/*`, `/skus/read`, `/locations/usages/read`, `Microsoft.Network/*` for
the kinds built, `Microsoft.Storage/storageAccounts/*`, `Microsoft.KeyVault/vaults/*`,
`Microsoft.ContainerRegistry/registries/*`, `Microsoft.ContainerService/managedClusters/*`, `Microsoft.App/*`,
`Microsoft.DBforPostgreSQL/flexibleServers/*`, `Microsoft.DBforMySQL/flexibleServers/*`, `Microsoft.Cache/*`,
`Microsoft.ServiceBus/namespaces/*`, `Microsoft.ManagedIdentity/userAssignedIdentities/*`,
`Microsoft.Authorization/locks/*`, `Microsoft.Insights/metrics/read`, `Microsoft.Authorization/roleAssignments/read`.
Never `*`, never `/register/action`, never `Microsoft.Authorization/roleDefinitions/write`. REQUIREMENTS.md section 6
gets a sibling table for Azure, built from this list on the real run, as the Google one was.

The grant right is the **Role Based Access Control Administrator** built-in role (`f58310d9-a9f6-439a-9e8d-f62e7b41a168`),
made "for delegating role assignment management", with an ABAC condition that names the only roles it may assign and
the only principal types. Microsoft's example (F, [delegate with conditions](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-overview);
F, [examples](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-examples)):

```
((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (
  @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ROLE-GUIDS} AND
  @Request[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'User','Group','ServicePrincipal'}))
AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (
  @Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ROLE-GUIDS} AND
  @Resource[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {'User','Group','ServicePrincipal'}))
```

with `conditionVersion: "2.0"` and `ROLE-GUIDS` the allow-list of section 4.3. This is the exact counterpart of the
Google condition `modifiedGrantsByRole.hasOnly([...])` that REQUIREMENTS.md already asks for, and it is stronger: the
Google condition lives on `projectIamAdmin`; this one lives on a role that can do nothing else. One known limit: a
role holding Storage *data actions* cannot be combined with a GUID-comparison condition (F, delegate overview), which
touches `Storage Blob Data Reader` in the allow-list; the real run must try it, and the fallback is the condition
without that one role plus a container-scoped grant through a second, unconditioned assignment of only that role.

Assigned at the subscription, or at a landing-zone resource group under which the service makes its request groups.
The role-assignment call: `PUT {scope}/providers/Microsoft.Authorization/roleAssignments/{guid}?api-version=2022-04-01`
with `principalId`, `roleDefinitionId`, `principalType`; the name is a GUID the caller makes, and Microsoft suggests
deriving it from scope, principal and role so that a retry is the same assignment (F, [role assignment create](https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/create);
F, [RBAC troubleshooting](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshooting)).

### 4.3 Grants on what the service created: the role per kind and level

Azure RBAC assigns at any resource id, including a Key Vault secret, a blob container, a Service Bus queue, topic or
subscription, and an AKS namespace (`<cluster id>/namespaces/<ns>`) (F, Key Vault RBAC; F, [blob roles](https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access);
F, [AKS authorization](https://learn.microsoft.com/en-us/azure/aks/entra-id-authorization)). So the rule "grant on
the resource, never on the project" holds everywhere, through one call shape, where Google needs one IAM method per
API. `GcpIam.ROLES` becomes a table of built-in role GUIDs (F, the built-in roles pages of 2026-07-01):

| Kind | `use` | `admin` |
|---|---|---|
| compute | Virtual Machine User Login `fb879df8-f326-4884-b1cf-06f3ad86be52` | Virtual Machine Administrator Login `1c0163c0-47e6-4577-8991-ea5c82e286e4` |
| the tunnel | Reader `acdd72a7-3385-48ef-bd42-f606fba81ae7` on the machine and its NIC (Bastion needs it; section 5.1) | the same |
| secret | Key Vault Secrets User `4633458b-17de-408a-b874-0445c86b69e6` at the secret's scope | Key Vault Secrets Officer `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` |
| bucket | Storage Blob Data Contributor `ba92f5b4-2d11-453d-a403-e96b0029c9fe` at the container | Storage Blob Data Owner `b7e6dc6d-f1e8-4753-8033-0f276bb0955b` |
| queue (topic) | Azure Service Bus Data Sender `69a216fc-b8fb-44d8-bc22-1f3c2cd27a39` | the same |
| queue (subscription) | Azure Service Bus Data Receiver `4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0` | the same |
| registry | AcrPull `7f951dda-4ed3-4680-a7ca-43fe172d538d` (or the repository-scoped reader of section 5.4) | AcrPush `8311e382-0749-4cb8-b61a-304f252e45ec`, registry writers only, as on Google |
| cluster | Azure Kubernetes Service RBAC Reader `7f6c6a51-bcf8-42ba-9220-52d62157d7db` at the namespace, plus Azure Kubernetes Service Cluster User Role `4abbcc35-e782-43d8-92c5-2d3f1bd2253f` on the cluster (credentials) | Azure Kubernetes Service RBAC Writer `a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb` at the namespace |
| service | (section 5.5; Container Apps has no invoker role: "not public" is Easy Auth with Entra) | Container Apps Contributor `358470bc-b998-42bd-ab17-a7e34c199c0f` on the app |
| job | — | Container Apps Jobs Operator `b9a307c4-5aa3-4b52-ba60-2b17c136cd7b` on the job |

Three facts shape the implementation. **Propagation**: "it can take up to 10 minutes for changes to take effect"
(Storage and Key Vault say 10, Service Bus and AKS 5), the same wait the Google adapter learned for IAP and Cloud Run
(F, RBAC troubleshooting). **The race on a new principal**: a role assignment for an identity made seconds ago fails
with `PrincipalNotFound` unless `principalType` is set, which makes ARM wait for replication (F, RBAC
troubleshooting); the node identity per cluster and the runtime identity per service hit this, as they hit "Verify
if principal exists" on Google. **The limit**: 5,000 role assignments per subscription, "fixed and cannot be
increased" (F, [RBAC limits](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshoot-limits)).
Per-resource grants spend it fast: a request with two machines, a secret and a bucket, and three members at `use`,
is 2×2 + 1 + 1 = 6 assignments per member, 18 per request, 270 requests to the ceiling. Google has no such limit at
this scale. Two answers, both in the adapter: grant at the **request's resource group** for the roles whose scope is
all the request's resources anyway (both login roles, Reader; Microsoft itself recommends group scope for the login
roles "to avoid the Azure role assignments limit", F, [Entra SSH](https://learn.microsoft.com/en-us/entra/identity/devices/howto-vm-sign-in-azure-ad-linux)),
keep per-resource grants for the data roles (a secret, a container, a subscription), and have `quota()` report
`roleAssignments` so that the planner refuses a request that would cross the limit. Because the resource group holds
only the request's own resources, a group-scoped grant is still "on what the service created, never on the project".
(Changed while building step 2: the login roles are granted per machine after all, since the orchestrator revokes
resource by resource; section 10, step 2.)

### 4.4 Members: an Entra object id, not an e-mail

A role assignment takes `principalId`, an object id (GUID), and `principalType` (`User`, `Group`,
`ServicePrincipal`, `ForeignGroup`, `Device`) (F, role assignment create). The contract's members are
`user:<email>`, `group:<email>`, `serviceAccount:<email>`, checked against the operator's `allowed_domains`. Two ways
to bridge them, and the note recommends the first with the second as the escape hatch:

1. **Resolve through Microsoft Graph before the first create**, beside the quota check, since an adapter's `validate`
   must stay pure (corrected 2026-11-03 while building step 1): `GET /v1.0/users/{upn}` (application
   permission `User.Read.All`; a guest's UPN carries `#EXT#`, sent as `%23`), `GET /v1.0/groups?$filter=mail eq '…'`
   or `displayName eq '…'` (`Group.Read.All`), `GET /v1.0/servicePrincipals?$filter=appId eq '…'`
   (`Application.Read.All`) (F, [users](https://learn.microsoft.com/en-us/graph/api/user-get); F, [groups](https://learn.microsoft.com/en-us/graph/api/group-list);
   F, [service principals](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list)). The domain check
   stays on the UPN or mail. The resolved id and type are stored on the member's record, so a later grant or revoke
   never resolves again and a renamed user keeps their grants. The service's managed identity gets those three Graph
   application roles by a one-time act of a Privileged Role Administrator (`POST /servicePrincipals/{graph sp}/appRoleAssignedTo`;
   there is no portal for it) (F, [assign app role to a managed identity](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-assign-app-role-managed-identity);
   F, [appRoleAssignedTo](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto)).
   A member the directory does not know is `INVALID_ACCESS` before anything is created, which is better than
   Google's `PrincipalNotFound` at grant time.
2. **Accept an object id in the member string** for operators who will not grant the service Graph rights:
   `user:<guid>`, `group:<guid>`, `serviceAccount:<guid>` (the form `principal:` is already refused by `MEMBER`, and
   should stay so). Then the domain check cannot run; the operator's `allowed_domains` must be empty and the
   operator's own `principals[].identity` must be ids too, and the catalog says so.

`serviceAccount:` on Azure is a service principal or a managed identity (Graph shows the latter as a service principal
of type `ManagedIdentity`; the `principalId` ARM returns for a user-assigned identity is its object id) (F, service
principals; F, [user-assigned identity](https://learn.microsoft.com/en-us/rest/api/managedidentity/user-assigned-identities/create-or-update)).
`access.registry_writers` names build identities the same way. Azure's `MEMBER` pattern lives beside Google's in
`security/validation.py`, selected by the request's provider, and `allowed_domains` keeps its meaning on both.
## 5. The mapping, kind by kind

The table has the shape of the Google one in PROVIDER-INTERFACE.md. Each row is what the Azure adapter would send; the
notes after the table say what is different in kind, not only in name. api-versions are the latest stable ones on
2026-11-03 (F, the ARM template reference pages).

| Generic | Azure |
|---|---|
| a request | a resource group `<prefix>-<6 hex of the request id>` with the ownership tags; everything below is in it |
| `network` | `Microsoft.Network/virtualNetworks` with one subnet (`cidr`; `defaultOutboundAccess: false`), a `networkSecurityGroups` on the subnet with rules `allow-internal` (VirtualNetwork), `allow-bastion-ssh` (from `AzureBastionSubnet` or the operator's Bastion range, port 22), `allow-public` (allowed ports from allowed sources), `deny-internet-out` with `allow-azure-out` service tags when `deny_egress`; a `natGateways` (Standard, zone-redundant with a StandardV2 gateway where offered) with one Standard public IP on the subnet when `nat`; VNet flow logs through the operator's Network Watcher and storage account when `network_logs` |
| private `database` on the network | a `privateEndpoints` in the subnet (subresource `postgresqlServer` or `mysqlServer`) and a `privateDnsZones` `privatelink.postgres.database.azure.com` (or `mysql`) linked to the network, made by `prepare_network_for_database` |
| `compute.cpu`, `memory_gb` | the smallest size of the family whose ratio fits: `Standard_D<n>s_v5` (4 GB per vCPU), `Standard_E<n>s_v5` (8), `Standard_F<n>s_v2` (2), `Standard_B<n>s_v2` / `B<n>ls_v2` for 1 or 2 vCPUs; sizes are 2, 4, 8, 16, 32, 48, 64, 96 vCPUs, and v6 (`D<n>s_v6`, Gen2 only, NVMe) where the operator allows |
| `compute.accelerator` | `t4`: `Standard_NC<4,8,16,64>as_T4_v3`; `a100`: `Standard_NC<24,48,96>ads_A100_v4` (80 GB), `Standard_ND96asr_v4` (8); `h100`: `Standard_NC<40,80>ad[i]s_H100_v5`, `Standard_ND96isr_H100_v5` (8); `l4`: **refused** (no L4 on Azure; the message names `t4` and the A10 `NVadsA10_v5` family as the neighbours) |
| `compute` as a list | one `virtualMachines` per machine, `<prefix>-<group>-<n>`, each with its own `networkInterfaces` (`deleteOption: Delete`), a Standard public IP on the NIC when `public_ip`, `zones: ["1"]` (or the request's zone), `securityProfile.securityType: TrustedLaunch` with Secure Boot and vTPM as the profile says, `identity.type: SystemAssigned` (the Entra login extension needs it) plus the request's user-assigned identity when a group names one, `osProfile.linuxConfiguration.disablePasswordAuthentication: true` and a throw-away SSH public key the service generates and forgets (Azure requires one; nobody holds the private half), the extension `AADSSHLoginForLinux`, `storageProfile.osDisk.deleteOption: Delete`, `diskSizeGB: boot_disk_gb`, `imageReference` from the OS family or the profile (with `plan` and accepted terms for a Marketplace image), no `customData`, no `userData` |
| `compute.os` | `Canonical:ubuntu-24_04-lts:server:latest`, `Debian:debian-12:12-gen2:latest`, Rocky 9 from the `resf` publisher (`rockylinux-x86_64:9-base`, a Marketplace plan: terms accepted once by the operator) |
| `compute.profile` | the operator's image: a Marketplace reference (`publisher:offer:sku:version`, with `plan` if any) or a Compute Gallery image id; Secure Boot as the profile says; a GPU machine on a non-GPU profile boots with Secure Boot off, as on Google, because the NVIDIA extension does not run under Secure Boot (F, section 5.2) |
| `compute.max_run_hours` | **no native equivalent**; the sweeper's own clock, as it already does for expiry, then `deallocate` |
| stop and start | `POST …/virtualMachines/<vm>/deallocate` (never `powerOff`: a stopped-allocated machine bills) and `…/start`; a database `POST …/flexibleServers/<s>/stop` and `/start` (auto-restarts after 7 days on PostgreSQL, 30 on MySQL) |
| resize | `PATCH` `hardwareProfile.vmSize` after `deallocate` when the target is not on the machine's current hardware (`…/vmSizes` lists what is); a GPU family change always deallocates first; Trusted Launch machines cannot move to a non-Trusted-Launch family |
| `storage.type: persistent`, `performance` | `Microsoft.Compute/disks` in the machine's zone: `StandardSSD_LRS` (standard), `Premium_LRS` (balanced), `PremiumV2_LRS` (ssd, zonal, data disks only; `Premium_LRS` where the size does not take it); attached by `PATCH` of the machine's `storageProfile.dataDisks` with `createOption: Attach`, a free `lun`, `deleteOption: Detach` |
| `storage.type: object` | a `Microsoft.Storage/storageAccounts` `<prefix><6 hex>` (3 to 24 lowercase alphanumerics, globally unique) with `kind: StorageV2`, `allowBlobPublicAccess: false`, `allowSharedKeyAccess: false`, `minimumTlsVersion: TLS1_2`, `supportsHttpsTrafficOnly: true`, blob soft delete 7 days, and one container `data`; the URL `https://<account>.blob.core.windows.net/data`; a delete first lists the container with the Storage audience and refuses `PROVIDER_CONFLICT` when it holds blobs, since ARM deletes an account with everything in it |
| `identity` | `Microsoft.ManagedIdentity/userAssignedIdentities` `<prefix>-<name>`; its `principalId` is granted on the request's resources at `level`; with a cluster, one `federatedIdentityCredentials` per cluster (issuer = the cluster's OIDC URL, subject `system:serviceaccount:<namespace>:<name>`, audience `api://AzureADTokenExchange`; at most 20 per identity, created one at a time) |
| `load_balancer: tcp` | a Standard public IP (zone-redundant), a Standard `loadBalancers` with the machines' NICs in a backend pool, a TCP probe on the port, one rule per port; the NSG opens the port from `allowed_sources` and health probes come from the `AzureLoadBalancer` tag, which the default rule admits |
| `load_balancer: http` without hostnames | the same Standard load balancer with an HTTP probe on `health_check_path` (an L4 pass-through, like the TCP one; no URL map) |
| `load_balancer: http` with hostnames | **Azure Front Door** Standard (Premium with the operator's WAF policy or when the machines must stay private): an origin group over the machines' addresses (Premium: a Private Link to an internal Standard load balancer), a route with a redirect from 80, a managed certificate per hostname, TLS 1.2 minimum; the hostnames must be in `providers.azure.public_domains` and the operator points DNS (a CNAME, plus a TXT for validation) at the endpoint |
| `database.engine`, `version` | `Microsoft.DBforPostgreSQL/flexibleServers@2026-10-01`, versions `14` to `18` (18 the default; 13 and below refused: extended support only), or `Microsoft.DBforMySQL/flexibleServers@2024-12-30`, `8.4` the default, `8.0` flagged (standard support ends 2027-01-31), `5.7` refused for new servers; name `<prefix>-db-<6 hex>` (globally unique, becomes `<name>.postgres.database.azure.com`) |
| `database.size` | `Standard_B1ms` (small, Burstable, "not for production"), `Standard_D2ds_v5` (medium), `Standard_D4ds_v5` (large); `storage.storageSizeGB` (32 GB minimum on PostgreSQL, `autoGrow: Enabled`) |
| `database.high_availability` | `highAvailability.mode: ZoneRedundant` (refused on Burstable) |
| `database`, the rest | `backup.backupRetentionDays: 7`, `geoRedundantBackup: Disabled`, encryption always on (the operator's `kms_keys` → `dataEncryption.type: AzureKeyVault`), `network.publicNetworkAccess: Disabled` with the private endpoint above (`public_access: true`: `Enabled` with firewall rules from `allowed_sources`), `authConfig.activeDirectoryAuth: Enabled` with the request's members as `administrators` (object id) where they ask, `administratorLogin` a name the service chooses and a root password it forgets, as on Google; the database by the `databases` child; **the user by SQL** (`CREATE ROLE … LOGIN PASSWORD`, there is no ARM resource), which needs a path to port 5432 (section 9); a `CanNotDelete` lock as deletion protection, removed before delete |
| `cache` | **Azure Managed Redis**, `Microsoft.Cache/redisEnterprise@2026-09-01` with a `databases/default` child: `sku.name: Balanced_B<n>` by `memory_gb` (1 → B1, 3 → B3, 6 → B5, 12 → B10, 24 → B20, 50 → B50), `highAvailability: Enabled` when asked (two nodes in two zones, cannot be turned off later), `minimumTlsVersion: 1.2`, `clientProtocol: Encrypted`, port 10000, `clusteringPolicy: OSSCluster`, `publicNetworkAccess: Disabled` and a private endpoint in `privatelink.redis.azure.net`; `accessKeysAuthentication: Enabled` so that `read_cache_auth` can `listKeys` into the request's `cache-auth` secret, Entra authentication on beside it. Not Azure Cache for Redis: closed to new customers since 2026-04-01, retired 2028-09-30 (F+) |
| `queue` | a Service Bus **Standard** namespace per request `<prefix>-<6 hex>` (6 to 50, globally unique; Basic has no topics; Premium only when private endpoints are demanded), `disableLocalAuth: true`, `minimumTlsVersion: 1.2`; a `topics/<prefix>-<queue>` and a `subscriptions/<prefix>-<queue>` with `defaultMessageTimeToLive` from `retention_hours` (ISO 8601), `lockDuration` from `ack_deadline_seconds`, `deadLetteringOnMessageExpiration: true`; members: Data Sender on the topic, Data Receiver on the subscription |
| `secret` | one `Microsoft.KeyVault/vaults` per request `<prefix>-<6 hex>` (3 to 24, globally unique; soft delete 7 days, no purge protection, `enableRbacAuthorization: true`, `publicNetworkAccess` as the request's network allows) and one secret per `secret` block inside it (`PUT https://<vault>.vault.azure.net/secrets/<name>?api-version=7.5`); `generate_value` writes version 1; members get Key Vault Secrets User **at the secret's scope**; a destroy **purges** the vault after deleting it (Key Vault Purge Operator on the service's role), or the name is reserved for 7 days |
| `registry` | one **repository prefix** `<prefix>-<registry>/` in the operator's ACR (`providers.azure.registry`, Premium or Standard, `roleAssignmentMode: AbacRepositoryPermissions`, `adminUserEnabled: false`), not a registry per request: members get Container Registry Repository Reader with the condition `StringStartsWithIgnoreCase '<prefix>-<registry>/'`, registry writers Repository Writer with the same condition; `immutable_tags` locks each tag after push through the data plane (`PATCH /acr/v1/<repo>/_tags/<tag>` `writeEnabled: false`); the address `<registry>.azurecr.io/<prefix>-<registry>`; deleting it deletes the repositories under the prefix (data plane) |
| `cluster` | `Microsoft.ContainerService/managedClusters@2026-08-01` `<prefix>-<name>`: `sku {name: Base, tier: Standard}`, `kubernetesVersion` the operator's default (1.36 or 1.37 today) with `autoUpgradeProfile.upgradeChannel: stable` and `nodeOSUpgradeChannel: NodeImage`, one `agentPoolProfiles` entry `mode: System`, `vmSize` from `node_cpu`/`node_memory_gb` (at least 2 nodes and 4 vCPUs on a system pool, no B-series), `osSKU: AzureLinux3`, `osDiskSizeGB: node_disk_gb`, `vnetSubnetID` the request's subnet, `availabilityZones`; `networkProfile {networkPlugin: azure, networkPluginMode: overlay, outboundType: userAssignedNATGateway}`; `aadProfile {managed: true, enableAzureRBAC: true}` and `disableLocalAccounts: true`; `apiServerAccessProfile {enableVnetIntegration: true, authorizedIPRanges: cluster_authorized_networks}` (a private cluster cannot take authorized ranges); `oidcIssuerProfile.enabled` and `securityProfile.workloadIdentity.enabled`; `identity.type: UserAssigned` with a control-plane identity per cluster (Network Contributor on the subnet, created with `principalType: ServicePrincipal` in its grants); the kubelet identity gets Repository Reader on the `registries` the request names (ABAC registries do not take `--attach-acr`); the namespace, RoleBindings (through Azure RBAC for Kubernetes at `<cluster id>/namespaces/<ns>`) and the `ValidatingAdmissionPolicy` against public `LoadBalancer` Services and Ingresses exactly as on Google; the front door through the **Application Gateway for Containers** add-on (a `/24` subnet delegated to `Microsoft.ServiceNetworking/trafficControllers`, Gateway API v1, hostnames CNAMEd to the frontend's `*.alb.azure.com` name) with **cert-manager** for the certificate, since the add-on has no managed certificates |
| `service` | a `Microsoft.App/managedEnvironments@2026-07-01` per request (`vnetConfiguration {infrastructureSubnetId: a /27 subnet delegated to Microsoft.App/environments, internal: true}`, `publicNetworkAccess: Disabled` unless the operator's `allow_public_services`, `workloadProfiles: [Consumption]`, `zoneRedundant` when the region has zones), then a `containerApps` `<prefix>-<name>`: `activeRevisionsMode: Multiple`, `ingress {external: service.public or ingress=all, targetPort, transport: auto, traffic: [...]}`, `scale {minReplicas, maxReplicas, rules: [{http: {concurrentRequests: concurrency}}]}`, resources `cpu`/`memory` from `cpu`, `memory_mb` (0.25 to 4 vCPU on Consumption), `identity.type: UserAssigned` with a runtime identity `run-<prefix>-<name>` holding Key Vault Secrets User on each named secret, `configuration.secrets` as Key Vault references read by that identity, `registries[].identity` for pulls; `public: false` → an `authConfigs` child requiring Entra sign-in (Easy Auth); a `timeout_seconds` above 240 is refused (the ingress limit); the "hold then move" release is a new revision at weight 0 followed by a `PATCH` of the weights, which is the delivery service's, as today |
| `job` | a `Microsoft.App/jobs@2026-07-01` in the same environment: `triggerType: Manual` or `Schedule` (`scheduleTriggerConfig.cronExpression`, five fields, **UTC only**: a `time_zone` other than UTC is refused), `replicaTimeout: task_timeout_seconds`, `replicaRetryLimit: max_retries`, `parallelism`, `replicaCompletionCount: tasks`; the schedule needs no scheduler resource and no scheduler identity, unlike Cloud Scheduler; a run is `POST …/jobs/<name>/start` |
| `access` | one `Microsoft.Authorization/roleAssignments/<guid>` per (member, resource, role) at the scopes of section 4.3, with `principalType`; revoke deletes them by the deterministic GUID |
| credentials | the request's `db-password` and `cache-auth` secrets in the request's vault, written through the data plane with the Key Vault audience |
| `connect` | `az network bastion ssh --name <bastion> --resource-group <rg> --target-resource-id <vm id> --auth-type AAD` and `az network bastion tunnel … --resource-port PORT --port PORT`; `psql "host=<name>.postgres.database.azure.com … sslmode=require"` from a machine on the network with the password from `az keyvault secret show --vault-name <vault> --name db-password`; `redis-cli -h <name>.<region>.redis.azure.net -p 10000 --tls --askpass`; `az storage blob … --auth-mode login`; `az aks get-credentials` then `kubelogin convert-kubeconfig -l azurecli`; the prerequisites: Reader on the Bastion (the platform team grants once), the `ssh` and `bastion` CLI extensions, membership of the vault's network |
| ownership | tags `managed-by`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner` on every resource and on the resource group; the resource group's name carries the request id |
| usage and health | `GET <vm id>/providers/Microsoft.Insights/metrics?metricnames=Percentage CPU,Available Memory Bytes&interval=PT1M` for one machine (an ARM call, so counted by ARM's bucket); `POST https://<region>.metrics.monitor.azure.com/subscriptions/<sub>/metrics:getBatch` with up to 50 machine ids for the sweeper's idle check; `busy_cores` = the maximum `Percentage CPU` sample × the size's vCPUs ÷ 100; disk utilisation `null` (no host metric for space used: the Azure Monitor agent would be needed, which the service does not install) |
| quota | `GET /subscriptions/<sub>/providers/Microsoft.Compute/locations/<region>/usages`: `cores`, the family's `standard<Family>Family` vCPUs (GPU families start at 0 in a new subscription), `virtualMachines`, `PublicIPAddresses` from the Network usages, and the subscription's role-assignment count against 5,000 |
| prices | a snapshot of the Retail Prices API per region, written by the operator (`cloud-infra prices --write`), with `verified` the date of the snapshot |

### 5.1 Network: private by default, egress on purpose

Two Azure facts shape the network block. First, since the API version after 2026-03-31 a new subnet has
`defaultOutboundAccess: false`, so a machine without a NAT gateway, a public address or a load balancer with outbound
rules reaches **nothing** on the internet, and reaches same-region Storage, the platform DNS and IMDS only (F+, [default
outbound access](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access)).
That is the behaviour `deny_egress` gives on Google with a response policy; on Azure it is the default, and `nat: true`
(the request's default) is what adds the NAT gateway. The adapter sets the property explicitly either way, since a
tool that sends an older api-version gets the old implicit egress. Second, there is no per-rule logging: NSG flow logs
retire on 2027-09-30 and VNet flow logs replace them, with a Network Watcher and a storage account in the region the
operator owns (F, [NSGs](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview);
F, [VNet flow logs](https://learn.microsoft.com/en-us/azure/network-watcher/vnet-flow-logs-overview)); `network_logs`
means a flow log on the request's network pointed at that account, and nothing when the operator names none.

`iap_ssh: true` opens port 22 from the operator's Bastion subnet (`providers.azure.bastion_subnet`, the `/26`
`AzureBastionSubnet` of the hub network peered to every request network, or a Bastion per request network when the
operator prefers, at Bastion Standard's hourly price); with no Bastion configured the rule is not made and the connect
block says so, and Run Command is the delivery service's path. The identity-aware part is Entra's, not a tunnel's: the
login roles on the machine (section 4.3), checked by the `AADSSHLoginForLinux` extension, which also needs the machine
to reach `login.microsoftonline.com`, `pas.windows.net` and `packages.microsoft.com` on 443, which a deny-egress
network must allow through the `AzureActiveDirectory` tag and a NAT or a firewall for the package host (F, [Entra
SSH](https://learn.microsoft.com/en-us/entra/identity/devices/howto-vm-sign-in-azure-ad-linux)). The health-probe
range is one service tag, `AzureLoadBalancer`, already admitted by the NSG's default rule 65001; Google's two ranges
go away.

Private managed services use **private endpoints and private DNS zones**, one zone per service
(`privatelink.postgres.database.azure.com`, `privatelink.mysql.database.azure.com`, `privatelink.vaultcore.azure.net`,
`privatelink.redis.azure.net`, `privatelink.blob.core.windows.net`), linked to the request's network, where Google used
Private Services Access and peering (F, [private endpoint DNS](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns)).
A private DNS zone takes only 15 tags. Azure Managed Redis has no network injection at all; PostgreSQL offers it but
a server made that way can never take a private endpoint or be moved to public access, so the adapter does not use it
(F+, [PostgreSQL Private Link](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-private-link)).
Creating a private endpoint before its subnet is `Succeeded` fails with `RetryableError`, and parallel subnet writes
on one network fail with `AnotherOperationInProgress`: the planner's order and a per-network lock in the adapter.

### 5.2 Compute: no custom sizes, Secure Boot versus drivers, deallocate

Azure sells fixed sizes and no custom shapes (F, [sizes overview](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/overview)),
so `compute_shape` picks the smallest size whose family ratio and vCPU count cover the request and the response reports
what was chosen, as the GPU families already do on Google; a request of 3 vCPUs or 6 GB on 2 vCPUs is refused with the
same `INVALID_COMPUTE_CONFIGURATION` sentence shape ("Azure's D family has 2, 4, 8, 16, 32, 48, 64 or 96 vCPUs with
4 GB each"). The size table is small (D, E, F, B v5 and v6; NCasT4_v3, NC_A100_v4, ND_A100_v4, NCads_H100_v5,
ND_H100_v5) and lives in `translate.py` with the ratios (F, the size family pages). Two retirements matter to an
operator's profile: NCv3 (V100) retired 2025-09-30 and the NC_A100_v4 page says new capacity goes only to the H100
family (F, [NC A100 v4](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nca100v4-series)).

Trusted Launch is the default for new Gen2 machines and the api-version 2025-11-01 or later makes it the ARM default
too (F, [Trusted Launch](https://learn.microsoft.com/en-us/azure/virtual-machines/trusted-launch)). The driver rule
is the same as Google's and better documented: "Installation of the GRID driver requires Secure Boot to be disabled",
and the NVIDIA extension says "Secure Boot is not supported using Windows or Linux extensions" (F, Trusted Launch; F,
[GPU driver extension](https://learn.microsoft.com/en-us/azure/virtual-machines/extensions/hpccompute-gpu-linux)). So
a GPU profile with Secure Boot on is an image the operator builds with distribution-signed drivers or Microsoft's
`microsoft-dsvm:ubuntu-hpc` image, and a GPU machine on a plain profile boots with Secure Boot off, which the
`secure_boot` field already reports.

Three lifecycle facts have no Google analogue and must be right in the first version: **`stop` is `deallocate`**
("Stopped… Billed"; "Deallocated… Not billed"; F+, [states and billing](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing));
**a delete leaves the NIC, the disks and the public address behind** unless each carries `deleteOption: Delete`, which
the adapter sets at create and the destroy also checks by deleting the parts it recorded (F, [VM delete](https://learn.microsoft.com/en-us/azure/virtual-machines/delete));
**there is no maximum run duration**, only a daily auto-shutdown at a time of day (`Microsoft.DevTestLab/schedules`),
so `max_run_hours` is the sweeper's, which already runs for expiry and idle stop (F, [auto-shutdown](https://learn.microsoft.com/en-us/azure/virtual-machines/auto-shutdown-vm)).
Hibernation exists but not with Trusted Launch on Linux, so `start` after a stop is a cold start (F, [hibernate](https://learn.microsoft.com/en-us/azure/virtual-machines/hibernate-resume)).

### 5.3 Storage, identity, load balancers

A disk is zonal and attaches only to a machine in its zone, by a `PATCH` of the machine, hot (F, [add a disk](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/add-disk));
the device appears as `/dev/disk/azure/scsi1/lun<n>` (SCSI, v5) or by `azure-nvme-id` (v6), which the connect note
says instead of `/dev/disk/by-id/google-*`. A bucket is a storage account, globally unique and limited to 250 per
region per subscription, and ARM deletes it with its blobs, so the "never emptied" rule is the adapter's own check
(F, [storage account](https://learn.microsoft.com/en-us/azure/storage/common/storage-account-create)). A user-assigned
identity is a resource with a `principalId` for grants and a `clientId` for tokens, soft-deleted for 30 days, and its
federated credentials for clusters are made one at a time (F, [federated credentials](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations)).

The load balancer is where Azure and Google part most. Google's one product covers L4 and L7 with a managed
certificate; Azure splits it: the Standard Load Balancer is L4 only (the Basic SKU retired 2025-09-30), Application
Gateway v2 is regional L7 with a WAF but takes certificates only from Key Vault or an upload, and only Azure Front Door
has **managed certificates**, a WAF and a redirect, at the price of being global and reaching private machines only
through Private Link on its Premium tier (F, [Load Balancer](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-overview);
F, [Application Gateway certificates](https://learn.microsoft.com/en-us/azure/application-gateway/key-vault-certs);
F, [Front Door](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-overview); F, [Front Door Private Link](https://learn.microsoft.com/en-us/azure/frontdoor/private-link)).
The mapping above follows the contract's promise ("hostnames with a managed certificate") rather than the nearest
regional product; an operator who must have a regional gateway gets `providers.azure.l7: application_gateway` later,
with certificates from the operator's vault, as a documented extension.
### 5.4 Databases, caches, queues, secrets, registries: five shapes to decide

**A database user needs a SQL connection.** Cloud SQL has a `users.insert`; the flexible servers have no ARM resource
for roles, so the one user the request asks for is `CREATE ROLE … LOGIN PASSWORD` over port 5432 or 3306 as the
administrator (F, [PostgreSQL quickstart](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/quickstart-create-server)).
A private server is reachable only from its network. Three ways, in order of preference: (1) run the service inside the
subscription on a network peered to the request networks (the operator's hub), which is where it runs in production
anyway; (2) create the server with `publicNetworkAccess: Enabled` and a firewall rule for the service's own address
for the seconds the role takes, then close it (the server's networking mode allows it, the private endpoint stays);
(3) Entra-only: `passwordAuth: Disabled`, the request's members as Entra administrators or principals, no generated
password at all, which the contract's `credentials` block then reports as absent. The first is the design; the second
is the laptop's path; the third is a later option. MySQL's Entra authentication additionally needs a user-assigned
identity on the server with three Graph permissions, which the service's own identity already has (section 4.4) (F,
[MySQL Entra](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-azure-ad-authentication)).

**The cache is Azure Managed Redis or nothing.** Azure Cache for Redis Basic, Standard and Premium are closed to any
tenant that had no cache before 2026-04-01 and retire 2028-09-30; the Enterprise tiers are closed since 2026-04-01 and
migrate on 2027-03-31 (F+, [Azure Cache for Redis what's new](https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/cache-whats-new)).
Azure Managed Redis (GA May 2025) runs Redis 7.4, is a two-resource shape (the cluster and its `default` database),
takes Private Link only, and keeps access keys optional beside Entra; `read_cache_auth` is a `listKeys` on the
database (S, the action name from the operations list; the page answered 404) (F, [Managed Redis](https://learn.microsoft.com/en-us/azure/redis/overview);
F, [private link](https://learn.microsoft.com/en-us/azure/redis/private-link); F, [Entra](https://learn.microsoft.com/en-us/azure/redis/entra-for-authentication)).
The port is 10000, not 6379; `high_availability` doubles the nodes across zones and cannot be turned off later; the
smallest SKU, B0, is about $12 a month (S). Creation takes 10 to 20 minutes (S).

**A queue is a Service Bus namespace per request on the Standard tier.** Basic has no topics; Standard has no private
endpoints (Premium only, at a much higher base price) (F, [premium messaging](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-premium-messaging)).
So a `queue` on Azure is reachable through its public endpoint with Entra authentication and `disableLocalAuth`, and
the connect block says so; a request on a deny-egress network reaches it through the `ServiceBus` service tag. The
namespace name cannot be reused for four hours after a delete (F, [Service Bus FAQ](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-faq)),
which the salted name absorbs. Event Grid namespaces with pull delivery are the closer Pub/Sub shape but a younger
product with less entity-scoped RBAC; Service Bus is the default.

**Secrets live in a vault per request, with grants per secret.** Key Vault's RBAC model is the default since
api-version 2026-02-01; a role assignment at a secret's own scope is supported, with Microsoft's note that it is for
"limited scenarios" (F, [Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide)). A
vault per request keeps the project-wide rule and the blast radius, and makes the request's `db-password` and
`cache-auth` secrets ordinary members of it. The cost is the name: 3 to 24 characters, globally unique, reserved for
the soft-delete period after a delete, so the destroy purges the vault (Purge Operator on the service's role, purge
protection off, retention at the minimum 7 days) and the create never reuses a name (F, [soft delete](https://learn.microsoft.com/en-us/azure/key-vault/general/soft-delete-overview)).
Container Apps and AKS are not on Key Vault's trusted-services list, so a vault with its firewall on needs a private
endpoint for them (F, [trusted services](https://learn.microsoft.com/en-us/azure/key-vault/general/overview-vnet-service-endpoints)).

**A registry is a repository prefix in the operator's registry.** An ACR is a heavy, globally named thing with a
monthly fee per tier; a "registry per request" would be slow and wasteful, and Azure's own answer to "a repository per
team" is the **ABAC repository permissions** mode, GA, soon the default, with the roles Container Registry Repository
Reader, Writer and Contributor conditioned on a repository name prefix (`StringStartsWithIgnoreCase 'team/'`, the
trailing slash required); `AcrPull` and `AcrPush` are not honoured in that mode (F+, [ABAC repository permissions](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions)).
This is a better fit for `access.registry_writers` than Artifact Registry's per-repository IAM: one registry, one
build identity as the only writer under the prefix, every member a reader under the prefix. Tag immutability is a
per-tag lock after push, not a repository property (F, [image lock](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-image-lock)).
The delivery service's builds (section 7.2) write under the same prefix.

### 5.5 Clusters, services and jobs

AKS gives the cluster design of phases 2 and 3 everything it has on GKE and two things it lacks. Azure RBAC for
Kubernetes assigns the `use` and `admin` levels at a **namespace scope through ARM** (`<cluster id>/namespaces/<ns>`),
so the grant loop needs no RoleBinding and no cluster API call for members, only for the namespace itself and the
admission policy (F, [AKS authorization](https://learn.microsoft.com/en-us/azure/aks/entra-id-authorization)). API
Server VNet Integration (GA) puts the control plane's endpoint in the request's network while still taking authorized
ranges, which a private cluster cannot (F, [API server VNet integration](https://learn.microsoft.com/en-us/azure/aks/api-server-vnet-integration);
F, [authorized ranges](https://learn.microsoft.com/en-us/azure/aks/api-server-authorized-ip-ranges)). Workload
identity is the OIDC issuer plus a federated credential per (identity, cluster, namespace, service account), the
`identity` kind's job. The node identity per cluster is the kubelet's user-assigned identity, granted Repository
Reader under the registries' prefixes. What is weaker: the front door. Application Gateway for Containers implements
Gateway API but needs a `/24` subnet of its own and **has no managed certificate**: the listener's certificate is a
Kubernetes Secret, so the adapter installs cert-manager with an ACME issuer in the request's namespace, or the operator
syncs a certificate from a vault (F, [AGC components](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components);
F, [AGC TLS](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/how-to-ssl-offloading-gateway-api)).
`ValidatingAdmissionPolicy` is Kubernetes 1.30+ and works unchanged. A system pool wants 2 nodes and 4 vCPUs at
least, which `node_count: 1, node_cpu: 2` cannot give: the validator raises the floor and says why (F, [system pools](https://learn.microsoft.com/en-us/azure/aks/use-system-pools)).
Binary Authorization's place is the GA policy "only use allowed images" (a registry allow-list); signature
enforcement (Image Integrity) is preview and audit-only (section 7.2).

Container Apps is Cloud Run with one extra object. A **managed environment** per request (a `/27` delegated subnet,
an internal load balancer, 2 to 5 minutes to make, no charge on the Consumption profile; deleted by itself after 90
idle days) holds the request's apps and jobs; a request with only a job still needs one (F, [Container Apps
networking](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks); F, [environments](https://learn.microsoft.com/en-us/azure/container-apps/environment)).
Revisions, weighted traffic, scale to zero, a runtime identity reading Key Vault references, and `public: false` as
built-in Entra authentication all map one to one; a job's schedule is a trigger type with a cron in UTC and needs no
scheduler resource, which removes the `sch-` identity Google needs (F, [jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs)).
A request timeout above 240 seconds and a non-UTC `time_zone` are the two refusals.
## 6. Where the contract must refuse, not pretend

The multi-cloud literature says one thing in several voices: a shared contract for the portable core, explicit
provider-specific extensions, and refusals where a provider lacks a feature rather than a quiet substitute
(F, [Thoughtworks on generic cloud usage](https://www.thoughtworks.com/radar/techniques/generic-cloud-usage);
F, [Hohpe on lock-in](https://martinfowler.com/articles/oss-lockin.html)). Terraform's `azurerm` provider carries a
whole `features` block for Azure's quirks (purge soft-deleted vaults, delete the OS disk with the machine, force
delete, restart a server on a parameter change), which is the list of things an adapter must decide, written by the
people who maintain one (F, [azurerm features](https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/guides/features-block.html.markdown)).
For this contract the decisions are:

| Request | On Azure | Why |
|---|---|---|
| `compute.accelerator.type: l4` | refused, naming `t4` and the A10 family | Azure has no L4 |
| an odd CPU count, or memory outside the family ratios | refused with the family's sizes | no custom sizes |
| `compute.max_run_hours` | accepted; the sweeper stops the machine | no native equivalent; the service already owns time |
| `stop` | always `deallocate`; the response says `STOPPED` and `lifecycle` says billing continues for disks only | a stopped-allocated machine bills in full |
| `database.engine: mysql, version: "5.7"`; PostgreSQL 13 or below | refused | closed to new servers, or extended support only |
| `database.size: small` | `Standard_B1ms` with a warning "not for production" in `warnings` | the Burstable tier is what Microsoft says it is |
| `cache.version` other than `7.4` | refused | Azure Managed Redis runs 7.4 |
| `queue` with a private-only network (`deny_egress`) | accepted, with a note that the namespace's endpoint is public and reached through the `ServiceBus` tag with Entra authentication | private endpoints are Premium only |
| `load_balancer: http` with hostnames | Front Door, global | the only managed certificate; an operator switch for a regional gateway later |
| `service.timeout_seconds` above 240; `job.schedule.time_zone` not UTC | refused | Container Apps ingress and cron limits |
| `cluster.public` | refused (step 5) | Application Gateway for Containers is a preview add-on on AKS |
| `service.ingress: all` | refused (step 5) | the network's Container Apps environment is internal; Front Door Premium's Private Link to it comes later |
| `service.cpu: 8`, memory other than 2 GiB per vCPU | 8 refused; less memory raised with a warning, more refused | the Consumption profile's fixed pairs |
| `cluster.node_count: 1` or `node_cpu` below 4 | raised to the system pool's floor, with a warning | AKS's system pool rules |
| a `zone` | optional; `1` by default where the region has zones; a size not offered in that zone for this subscription is refused before any call, from the Resource SKUs listing | zones are logical per subscription |
| `region` | Azure names (`eastus2`, `westeurope`); the operator's `allowed_regions` | — |
| the identity-aware SSH path | `iap_ssh` keeps its name; its description says what it means on each provider | renaming would break callers |

And one extension the contract should grow, provider-tagged rather than smuggled into shared fields: a
`providers.azure` block in the operator's configuration (subscription, tenant, cloud, the landing-zone resource group
or the subscription as the scope, allowed regions, the Bastion subnet, the ACR, the Network Watcher storage account,
the public domains, the profiles, the budget switch), and nothing provider-specific in the request itself, which keeps
the request portable between the two clouds for everything in the table above except the refusals.
## 7. The delivery service on Azure

`appdelivery` depends on Google in four places (section 2). Each has an Azure counterpart; none is equal to the Google
one, and the build is the one that changes the trust story.

### 7.1 Reaching a machine as the environment's identity

Google: `gcloud compute ssh --tunnel-through-iap --impersonate-service-account`, OS Login as the account, root
through `sudo`. Azure offers four paths (F, [Bastion native client](https://learn.microsoft.com/en-us/azure/bastion/native-client);
F, [Entra SSH](https://learn.microsoft.com/en-us/entra/identity/devices/howto-vm-sign-in-azure-ad-linux);
F, [managed Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command-managed);
F, [Compute permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute)):

| Path | Identity | Public address | Audit | Scope that limits it to the request's machines |
|---|---|---|---|---|
| Azure Bastion native client (`az network bastion ssh --target-resource-id <vm> --auth-type AAD`, or `tunnel` for port forwarding) | the caller's Entra identity, a certificate from Entra; service principals can sign in | none on the machine; Bastion has one unless Premium private-only | Bastion diagnostics, Entra sign-in logs, sshd | Reader on the machine, its NIC and the Bastion; the login roles at machine or group scope |
| Entra SSH straight to port 22 (`az ssh vm`) | the same | the machine's own address, or a private one reached through VPN or a tunnel | Entra sign-in logs, sshd | the login roles |
| Managed Run Command (`PUT …/virtualMachines/<vm>/runCommands/<name>`) | the control plane; the script runs as root through the VM agent; `protectedParameters` carry secrets; `outputBlobUri` carries output over 4 KB; 90-minute default timeout | none; the machine needs outbound 443 to Azure | the Activity Log records `MICROSOFT.COMPUTE/VIRTUALMACHINES/RUNCOMMAND/ACTION` with the caller, not the script | a custom role with `virtualMachines/runCommands/write|read|delete` assigned at each machine |
| CustomScript extension | the control plane, root | none | the Activity Log | `extensions/write` at the machine |

For the delivery service the honest comparison is: **Bastion keeps the shape of the Google design** (a tunnel the
platform owns, a login granted per machine by the team naming the environment's identity at `admin`, the same
`release.sh` over stdin); it costs a Bastion Standard per hub network (about $0.29 an hour, S; the pricing page did not
render), a `/26` `AzureBastionSubnet`, Reader grants, and the native-client extension of the CLI. **Run Command needs
no network path at all**, has the stronger audit trail for *that* something ran, and is what Microsoft calls "one of
the only ways" to act on a machine without an open port (F, [Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command)),
but it is a control-plane right to run anything as root, which the request's `access` grant must express just as
`admin` expresses it today, and its output path is a blob with a SAS. Either keeps `cloudinfra`'s own promise: the
infrastructure service never holds that right; only the environment's identity does, on the machines a team named it
on. Recommendation: **Bastion with Entra authentication first** (one change to `remote.py`, the same host script),
Run Command as the second target reach when a company's network policy forbids Bastion or port 22.

**The container host.** Container-Optimized OS has no Azure twin that ships Docker. Azure Linux 3 (publisher
`MicrosoftCBLMariner`, offer `azure-linux-3`, SKU `azure-linux-3-gen2`, S) ships containerd, not Docker (F, [Azure
Linux](https://learn.microsoft.com/en-us/azure/azure-linux/intro-azure-linux)); `tdnf install moby-engine moby-cli`
is the community path (S), and the managed Run Command page lists only Azure Linux 2.x (F). Flatcar
(`kinvolk:flatcar-container-linux-corevm-amd64`, no plan) ships Docker and takes Ignition on first boot (F, [Flatcar on
Azure](https://www.flatcar.org/docs/latest/deploy/cloud/azure/)); Ubuntu 24.04 with Docker installed by the operator's
image build is the third option. The `container-host` profile on Azure is the operator's image either way, and the
spike (section 10) decides between Flatcar and an Ubuntu golden image by what `release.sh` finds: Docker, `curl`, `jq`,
`sudo` for the Entra user.

### 7.2 The build inside the trusted subscription

The requirement of 2026-10-30 — no outside builder, a build record tied to a commit, a signed statement of origin,
one writer per registry — has **no single Azure service that gives all three** (F, the pages below; the summary is
this note's):

| Option | Inside the subscription | Record tied to a commit | Signed provenance | Verdict |
|---|---|---|---|---|
| ACR Tasks quick build (`az acr build` / `registries/<r>/scheduleRun`), context `https://<host>/<org>/<repo>.git#<sha>:<dir>`, optionally on a dedicated agent pool in the platform's network (Premium, still preview) | yes, run by the registry under the identity that scheduled it | a run id, 30 days of logs, the pushed digest and the base image's digest | **none**: no SLSA or in-toto statement is produced | the builder; `appdelivery` writes the statement |
| Azure Pipelines with Microsoft-hosted agents or Managed DevOps Pools | no: the agents "exist in a subscription owned by Microsoft", even when their NICs are in the platform's subnet | a run with `resources.repositories.self.version` = the SHA | none native | fails the requirement without a self-hosted agent |
| GitHub Actions with OIDC to a managed identity | no (GitHub-hosted); yes with Actions Runner Controller on AKS | yes | GitHub artifact attestations, SLSA Build L2 (L3 with an isolated reusable workflow), signed by GitHub's Sigstore | the statement comes from outside, which the requirement forbids |
| Container Apps cloud build (`az containerapp up --source`) | yes | no documented record or digest API | none | a developer convenience |

(F, [ACR Tasks overview](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-overview);
F, [quick task](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-quick-task);
F, [agent pools](https://learn.microsoft.com/en-us/azure/container-registry/tasks-agent-pools);
F, [Azure Pipelines run](https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/run-pipeline);
F, [Managed DevOps Pools](https://learn.microsoft.com/en-us/azure/devops/managed-devops-pools/architecture-overview);
F, [GitHub attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations);
F, [Container Apps up](https://learn.microsoft.com/en-us/azure/container-apps/containerapp-up).)

No retirement of ACR Tasks was found in two extended searches; the ACR retirements of record are the `2017-03-01`
API on 2026-09-30 and Docker Content Trust on 2028-03-31 (S, [end of support 2026](https://learn.microsoft.com/en-us/lifecycle/end-of-support/end-of-support-2026)).
Two things only a real run can settle: how a quick task reads a *private* repository (the documentation shows public
ones; a token in the context URL would be logged, which the overview itself warns about), and the exact fields of the
`scheduleRun` and `runs` resources (the reference pages answered 404 to the thread; `runId`, `status`, `outputImages`
are from memory).

**The design that meets the requirement on Azure, then:** `appdelivery` schedules an ACR quick task from the connected
source at a commit SHA, as the environment's build identity, on an agent pool in the platform's network; records the
run id and the digest; then **writes the provenance statement itself** (an in-toto statement whose builder is the
service's identity, whose source is the repository and SHA, whose subject is the digest), signs it with Notation
through a Key Vault certificate or Artifact Signing (the renamed Trusted Signing; F, [sign with Notation](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-sign-build-push);
F, [Artifact Signing](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-github-sign-notation-artifact-signing)),
attaches it as an OCI referrer, and verifies signature and statement before any release, as `provenance.py` verifies
Google's. The registry's only pusher is the build identity (AcrPush, or the repository-scoped writer of section 5.4;
admin user off). What is weaker than Google: the signer is the platform, not the cloud, so the statement proves "the
platform says it built this from that commit", one trust level below Cloud Build's hosted-worker signature. What is
not yet usable: AKS Image Integrity (Ratify) is preview, audit-only, "shouldn't [be used] for production" (F, [Image
Integrity](https://learn.microsoft.com/en-us/azure/aks/image-integrity)); the GA policy "Kubernetes cluster containers
should only use allowed images" (`febd0533-…`, Deny) is the registry allow-list that stands in for Binary
Authorization (F, azadvertizer).

### 7.3 The targets' own APIs, and the CLI in the image

Cloud Run services and jobs become Container Apps and Container Apps jobs (section 5.5); GKE credentials become
`az aks get-credentials` plus `kubelogin`, or the adapter's own token for the AKS audience (section 4.1) with the
cluster's CA from `managedClusters/<name>/listClusterUserCredential`. The `az` CLI, if the image carries it for
Bastion: release 2.92.0 ships standalone Linux tarballs with SHA-256 on GitHub (`azure-cli-2.92.0-linux-x86_64.tar.gz`,
`-arm64`), and the PyPI wheel is 14.7 MB with 108 dependencies (F, [CLI releases](https://github.com/Azure/azure-cli/releases/latest);
F, PyPI); the `bastion` and `ssh` extensions are separate wheels, installed from a pinned path with
`az extension add --source` and `extension.use_dynamic_install=no` so a missing one fails instead of downloading
(F, [extensions](https://learn.microsoft.com/en-us/cli/azure/azure-cli-extensions-overview)). The same pinning
discipline as gcloud's in `Dockerfile.appdelivery`, with the extensions as two more pinned inputs.
## 8. Governance, conformance and cost on Azure

**What the operator will already have, and the adapter must survive.** A company's subscription sits under management
groups that assign Azure Policy. The built-ins a platform team turns from Audit to Deny, and what the adapter must do
so its PUTs pass (F, [policy ids read on azadvertizer.net](https://www.azadvertizer.net/), S for the Redis one):

| Policy | The adapter's answer |
|---|---|
| Allowed locations (`e56962a6-…`) | `allowed_regions` lists only regions the policy allows; a denial is `PROVIDER_PERMISSION_DENIED` naming the policy |
| Require a tag on resources (`871b6d14-…`), inherit a tag from the resource group (`ea3f2387-…`, Modify) | every create carries the ownership tags; the operator's `labels` carry the required ones; tags read back may differ |
| Network interfaces should not have public IPs (`83a86a26-…`) | `compute.public_ip` fails at the provider with the policy named; `allow_public_exposure: false` refuses it first |
| Storage accounts should prevent shared key access (`8c6a50c6-…`), Key Vault should disable public network access (`405c5871-…`), Service Bus local auth disabled (`cfb11c26-…`), container registry admin account disabled (`dc921057-…`), Redis without access keys (`3827af20-…`) | set on every create: `allowSharedKeyAccess: false`, `publicNetworkAccess: Disabled` where the request is private, `disableLocalAuth: true`, `adminUserEnabled: false`, `disableAccessKeyAuthentication: true` (the Redis one needs the Entra path for the AUTH secret; section 5.4) |
| Deploy diagnostic settings (DeployIfNotExists initiatives) | they add `microsoft.insights/diagnosticSettings` children after the adapter's PUT; destroy tolerates extension resources it did not create (a resource-group delete does) |
| Marketplace images | a Rocky image needs its terms accepted once per subscription and a `plan` on every create, or the create fails with `MarketplacePurchaseEligibilityFailed` / `VMMarketplaceInvalidInput` (F, [programmatic Marketplace deploy](https://learn.microsoft.com/en-us/marketplace/programmatic-deploy-of-marketplace-products)); an EA billing policy or a private Marketplace may forbid them outright |

**What to claim.** The Microsoft cloud security benchmark is at v2 (preview, 2026-01, 12 domains, over 420 built-in
policies, each control mapped to NIST SP 800-53 r5, PCI-DSS v4, CIS Controls v8.1, ISO 27001:2022) (F, [MCSB](https://learn.microsoft.com/en-us/security/benchmark/azure/overview));
CIS lists Microsoft Azure Foundations Benchmark 7.0.0 and separate Compute, Storage and Database benchmarks 2.0.0 (F,
[CIS](https://www.cisecurity.org/benchmark/azure)), while the built-in Policy initiatives still stop at CIS v3.0.0 (F,
[built-in initiatives](https://learn.microsoft.com/en-us/azure/governance/policy/samples/built-in-initiatives)).
Defender for Cloud's free foundational tier applies the benchmark and computes the secure score; only MCSB
recommendations move it (F, [secure score](https://learn.microsoft.com/en-us/azure/defender-for-cloud/secure-score-security-controls)).
As on Google, the adapter can claim conformance **of the resources it creates** to the controls Policy can measure
(public network access off, private endpoints, an NSG on every subnet, TLS 1.2 minimum, encryption at rest, managed
identities and local auth off, tags, locks, diagnostics where the operator deploys them) and compatibility with an
MCSB or CIS initiative assigned above it; it cannot claim tenant-level controls (MFA, Defender plans, log
destinations), which are the operator's, exactly as the Google S3 items are. The security assessment's "S2 defaults"
section gets an Azure column with these properties.

**Backstops the operator should set**, the counterpart of the Google organisation policies in REQUIREMENTS.md: the
Deny policies above; `defaultOutboundAccess: false` on new subnets (the API default after 2026-03-31; section 5.1);
Defender for Cloud on with the MCSB initiative; Activity Log exported to a Log Analytics workspace (90 days free, then
the operator's retention); PIM for the platform team's own Owner and User Access Administrator (eligible, time-bound,
MFA on activation) (F, [PIM](https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-resource-roles-assign-roles));
the CLI's own MFA requirement for user sign-ins since September 2025 (F, [CLI sign-in](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-interactively)).

**Cost: the estimate, the ceiling and the actuals.** Three things are better on Azure than on Google and one is
worse.

- *List prices come from an API, not a hand-typed table.* The Retail Prices API is unauthenticated:
  `GET https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=armRegionName eq 'eastus2' and armSkuName eq 'Standard_D4s_v5' and priceType eq 'Consumption'`
  returns the Linux hourly price (0.192 on 2026-11-03), the Windows row (product name ending " Windows"), and Spot
  and Low Priority rows by meter-name suffix; managed disks by `skuName` ("P10 LRS"), PostgreSQL flexible compute per
  vCore-hour with `armSkuName`, AKS's "Standard Uptime SLA" at 0.10 an hour, Container Apps' per-second vCPU and
  memory meters (F, [Retail Prices](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices);
  F, observed answers of 2026-11-03). Values are case-sensitive, 1,000 rows a page, no documented rate limit but
  `x-ms-ratelimit-remaining-retailprices-requests` headers and 429 on abuse (S). `pricing.py` for Azure therefore
  keeps a **snapshot** written by a CLI command (`cloud-infra prices --write`, run by the operator, dated in
  `verified`) rather than calling the API on every plan: the estimate stays pure and deterministic, as `plan` must be,
  and the snapshot is refreshed on purpose.
- *Actual spend by tag is one query.* `POST {scope}/providers/Microsoft.CostManagement/query?api-version=2026-06-01`
  at the subscription, daily granularity, grouped by `TagKey` `cloudinfra-request`; data arrives 8 to 24 hours late
  (72 for pay-as-you-go), and the API meters Query Processing Units (12 per 10 s, 60 a minute, 600 an hour per
  tenant) so the sweeper caches it for hours and never queries per request (F, [Cost Management query](https://learn.microsoft.com/en-us/rest/api/cost-management/query/usage);
  F, [data latency](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data);
  F, [automation limits](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/manage-automation)).
  This is release B's "billing-export actuals" with no export to set up. Tags count only when on the resource itself,
  not inherited; the AKS node resource group's resources carry the cluster's tags but shared resources cannot be split
  (F, [AKS tags](https://learn.microsoft.com/en-us/azure/aks/use-tags)).
- *A budget per request's resource group exists natively.* `Microsoft.Consumption/budgets` at resource-group scope,
  monthly, with tag filters and up to five notifications to e-mails, roles or action groups; evaluated once a day;
  it stops nothing by itself (F, [budgets](https://learn.microsoft.com/en-us/rest/api/consumption/budgets/create-or-update);
  F, [budget scenario](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/cost-management-budget-scenario)).
  The service's ceiling stays its own pre-create check and its own idle stop; a budget is a backstop alert the
  adapter can create per request when the operator asks (`providers.azure.budget_alerts`).
- *Worse*: there is no sustained-use discount and no per-second billing subtlety to get right, but **a stopped machine
  bills until it is deallocated** (section 5.1), so every "stop" the service does must be a deallocate, or the idle
  stop saves nothing.
## 9. What only a real subscription can prove, and the gotchas to carry in

### 9.1 Creation times and the operation timeout

The operator's `limits.operation_timeout_seconds` is 900 by default and 3,600 at most. Azure's typical times, from
Microsoft's pages where they say one and from community reports where they do not (S): PostgreSQL and MySQL flexible
servers 5 to 10 minutes (F); AKS 5 to 10; Azure Managed Redis 10 to 20 (S); a Container Apps environment 2 to 5 (S);
Application Gateway for Containers and Front Door several minutes; a Bastion about 10. A request with a cache and a
cluster is at the default's edge, so the Azure adapter's `wait` takes its own ceiling per kind (the operator's
timeout as the cap), and REQUIREMENTS.md records the ceilings. The lost-answer lookup is simpler than Google's: a
GET of the chosen name.

### 9.2 Facts the documentation does not settle

Each is a step of the first real run (section 10), as DELIVERY-SPIKE.md records the Google ones:

1. Whether the Role Based Access Control Administrator condition of section 4.2 accepts the whole allow-list, given
   the documented limit on Storage data-action roles with GUID comparisons.
2. How a quick ACR task reads a private repository without a token in the logged context URL, and the fields of its
   run record (`scheduleRun`, `runs`): the reference pages answered 404.
3. Whether `listKeys` is the action that returns an Azure Managed Redis database's access keys, and its shape.
4. The Entra SSH extension on Azure Linux 3 and on Flatcar (listed for one, not stated for the other), and which
   container host gives `release.sh` Docker, `curl`, `jq` and `sudo`.
5. The NVIDIA extension or a distribution-signed driver under Secure Boot on Ubuntu 24.04, and the minimum OS disk of
   the HPC images.
6. How long a deleted lock is still enforced, and how long after a `deallocate` a `start` succeeds.
7. Bastion Standard's tunnel from the delivery service's container (stdin over `az network bastion ssh`), as the IAP
   spike proved for gcloud.
8. The exact quota family names for the H100 sizes, and whether a new subscription can get any GPU quota without a
   support request.
9. The `RequestDisallowedByPolicy` body as a company's Deny policies return it.

Step 2 added these, each a line of the first run's checklist:

10. Trusted Launch on the GPU sizes: the adapter boots NCasT4_v3, NC_A100_v4 and NCads_H100_v5 with it and the ND sizes
    without (`securityType: Standard`); a size that refuses it fails at create.
11. Whether a Standard public address made with no zone in a zonal region is zone-redundant by default and so refused
    by a zonal NAT gateway; the adapter gives both the request's zone.
12. The `AADSSHLoginForLinux` extension on a request network with its NAT, and what a `deny_egress` network must allow
    for it (`packages.microsoft.com` is outside the `AzureActiveDirectory` tag).
13. Graph's `mail eq` filter on users and groups with `User.Read.All` and `Group.Read.All`, and a guest found by mail.
14. Whether Azure removes the role assignments at a resource's scope when the resource is deleted; the adapter revokes
    a machine's and a bucket's before deleting them, so none is left either way.
15. The Retail Prices API's meter names for the NAT gateway and the static public address, and the Network usage name
    of Standard public addresses (`StandardSkuPublicIpAddresses`).
16. How long a role assignment takes to apply to Entra sign-in on a machine and to a bucket's data plane.

Step 4 added these:

17. Whether a vault data action held at the subscription applies at once to a vault made moments ago, and whether a
    secret takes an empty disabled value.
18. That a PUT of an existing role assignment may change its condition (the adapter widens and narrows a member's
    repository condition that way), and the condition's length limit for a member on many requests' registries.
19. The Writer role's ABAC condition, written by analogy with Microsoft's Reader example; and the registry token
    exchange with a token for `https://containerregistry.azure.net`.
20. The private endpoint group ids (`postgresqlServer`, `mysqlServer`, `redisEnterprise`) and the Managed Redis zone
    `privatelink.redis.azure.net`; the Balanced sizes' memory; how long a Managed Redis takes to make.
21. Whether a flexible server's administrator password from `token_urlsafe` passes Azure's complexity rule every time
    (the orchestrator now ensures an upper and a lower case letter and a digit).

Step 5 added these:

22. That `listClusterUserCredential` answers on a cluster with local accounts off (the user kubeconfig holds no secret),
    and that a role assignment at `<cluster>/namespaces/<ns>` takes effect for a member signed in with kubelogin.
23. How long AKS refuses a create (`LinkedAuthorizationFailed`) after the control-plane identity's role on the subnet is
    granted; the adapter tries again six times, twenty seconds apart.
24. That a Container Apps environment's subnet takes the request's security group (allow-internal, the probes, deny)
    and NAT gateway, and what Azure does on deleting an environment that still holds apps (the fake refuses it).
25. Easy Auth on Container Apps honouring `allowedPrincipals.identities` and `groups` as App Service documents them, a
    token for `api://<client id>` from `az account get-access-token`, and the 500-character limit.
26. Front Door Standard taking an IPv4 origin forwarded over HTTP, the custom domain's `validationProperties` and
    `domainValidationState` as the REST reference shows them, and how long a managed certificate takes after the TXT
    record resolves.
27. An IP-based backend pool on a Standard load balancer for machines with no public address, and that the probe's
    source (the `AzureLoadBalancer` tag) passes the network's security group without a rule of its own.

The first real run, on a student subscription on 2026-11-05 ([AZURE-RUNS.md](../AZURE-RUNS.md), section 1), settled
these: 24, an environment's subnet takes the request's security group and NAT gateway, and its delete took 28 minutes;
25 in part, Azure takes the auth config and its allow-list (no call could reach the internal environment); 27, an
IP-based pool's members must go by the pool's own PUT, since Azure keeps none named in the load balancer's; 15, the
Network usage of public addresses is `PublicIPAddresses` on this offer. It also met what no page said: a managed
identity's create answers with a relative `Location`; a size the subscription is not offered fails only at the create
(the check of section 6 had not been built); a private DNS zone is refused a second after its last link's delete; a
secret's purge meets 409 while its delete runs; Front Door is forbidden to free and student subscriptions. 22, 23 and
26 wait for a subscription with quota, with clusters and GPUs.

### 9.3 Gotchas, each a test in the fake

- A role assignment made seconds after its principal fails with `PrincipalNotFound` unless `principalType` is set;
  a reused assignment name with a different principal fails with `RoleAssignmentUpdateNotPermitted`; grants show up
  to 10 minutes late (F, [RBAC troubleshooting](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshooting)).
- A Key Vault name is reserved for the soft-delete period; a Service Bus namespace name for four hours; a storage
  account name is global and collides as `StorageAccountAlreadyTaken` (F, the pages of section 5.4).
- A VM delete leaves NIC, disks and public IP unless `deleteOption: Delete`; a stopped VM bills; a subnet without
  `defaultOutboundAccess: false` on an old api-version has implicit egress (F+, section 5.2 and 5.1).
- Two writes on one virtual network at once: `AnotherOperationInProgress`; a private endpoint before its subnet is
  `Succeeded`: `RetryableError`; a subnet with NICs: `InUseSubnetCannotBeDeleted` (F, [request limits](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling);
  F, [common errors](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/common-deployment-errors)).
- AKS answers `OperationNotAllowed` "Another operation is in progress" during its own autoscaler or upgrade work; the
  adapter reads `provisioningState` and retries (S, [AKS troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/operationisnotallowed)).
- A Compute 429 may ask for 1,200 seconds; the adapter honours it and the sweeper's batch metrics call is the one that
  must not become per-machine ARM calls (F, [Compute throttling](https://learn.microsoft.com/en-us/azure/virtual-machines/compute-throttling-limits)).
- A managed identity's token in Container Apps is cached about 24 hours per audience, so a grant made after the
  service started may not show until a restart (F, [Container Apps identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity)).
- A Marketplace image without accepted terms fails with `MarketplacePurchaseEligibilityFailed`; without a `plan`
  with `VMMarketplaceInvalidInput` (F, [Marketplace deploy](https://learn.microsoft.com/en-us/marketplace/programmatic-deploy-of-marketplace-products)).
- A deny lock on one resource blocks the whole resource group's delete; a `CanNotDelete` lock also blocks removing a
  role assignment (F, [locks](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources)).
- The AKS node resource group (`MC_*`) is Azure's, tagged with the cluster's tags but not deletable by the adapter; a
  Container Apps environment's `ME_*` group likewise.
## 10. The build plan

The same shape as the delivery service's: steps that need no cloud first, each landing as its own pull request with
tests against a fake, then a spike on a real subscription, then the rest. The Google adapter took three days for
thirteen kinds with the orchestrator already written; Azure's kinds are the same count, the fake is new, and the two
generalisations of section 2 come first.

**Step 0, decisions (this note's, to confirm):** raw REST, no SDK; a resource group per request; Graph resolution of
members with object ids as the fallback; Azure Managed Redis; Service Bus Standard; a vault per request with
per-secret grants; a repository prefix in the operator's ACR; Front Door for hostnames; Bastion first for the delivery
service, Run Command second; prices from a snapshot of the Retail Prices API; `stop` is `deallocate`.

**Step 1, the seams (no cloud).** `Providers.azure: AzureSettings | None` and the two `("gcp",)` tuples generalised;
the contract's region and zone rules taught Azure's names (`eastus2`, zone `1`; found while building it: they took
only Google's and AWS's shape);
`providers/azure/auth.py` (`AzureToken`: IMDS, the Container Apps endpoint, the workload identity file, `az`; one
cache per audience), `api.py` (`AzureApi`: api-version per resource type, `request`, `wait` for 201/202 with
`Azure-AsyncOperation` and `Location`, `translate_http_error` on `error.code`, the Compute and ARM retry rules), the
cloud metadata document, `naming.py` (lengths, global uniqueness, the salt), `tests/cloudinfra/azure_fakes.py` with
`FakeAzureApi` answering ARM paths from memory with planted failures, and the boundary test's Azure twin. Register
`azure`, drop it from `PLANNED_PROVIDERS`. Built on 2026-11-03 on branch `azure-step-1`. Two items moved to step 2,
where they first have a use: the member syntax per provider with the resolved id stored (it belongs to the grant loop,
and resolution cannot sit in the pure `validate`), and a ceiling per kind for the waits (no kind exists yet; the
client's waits already take one per call).

**Step 2, release A's kinds.** Members resolved to object ids before the first create and stored with the request's
access; a ceiling per kind for the waits; resource group, network (VNet, subnet, NSG, NAT, flow log), compute (sizes, images,
Trusted Launch, the Entra login extension, `deleteOption`), disks and buckets, identities; grants through role
assignments with the deterministic GUID and `principalType`; `deallocate`/`start`; resize; the connect blocks;
metrics for health and idle stop; quota from `usages`; the price snapshot command and `estimate_cost`. Every
orchestration test (`tests/cloudinfra/test_infra_orchestration.py` takes any adapter) runs green against the fake.
Built on 2026-11-04 on branch `azure-step-2`, with `tests/cloudinfra/test_infra_azure_kinds.py`: the orchestration
tests are Google-shaped (they read the Google fake's own records), so their Azure twins are written there instead.
api-versions were read on each template reference page that day (REQUIREMENTS.md lists them). Five decisions changed
while building it, each for a reason the code met:

- *Login roles on the machine and its interface, not the request's group.* The orchestrator grants and revokes
  resource by resource (a machine is released before it is deleted, a member revoked from one resource), and a
  group-scoped assignment shared by every machine would be removed with the first. Each machine costs two assignments
  per member; quota() does not count the subscription's 5,000 yet (the read is a full listing), so the real run
  measures it.
- *The 2 GB family is Dlsv5, not Fsv2*: Dsv5's sibling, with the same sizes and generation, so a resize stays on the
  same hardware more often.
- *Disks follow the contract's names*: `standard` is Standard HDD, `balanced` Standard SSD, `ssd` Premium SSD; Premium
  SSD v2 is zonal-only and data-only, and waits for a request that needs it.
- *The NAT gateway and its address are zonal, in the request's zone*, where its machines are; a Standard gateway does
  not take a zone-redundant address, and StandardV2 is not in every region.
- *A machine shut down from inside is `UNKNOWN`, not `STOPPED`*: Azure still bills it in full, so it must neither be
  priced as stopped nor skipped by a stop, which deallocates it.

The resource group is removed through a new interface hook, `finished`, called when nothing of a request is left
(after a destroy, a failed provisioning's cleanup, or the last single delete); a group holding something the service
did not make is kept, and the destroy says which.

A code review of the step before it was committed found five things, all fixed with a test each: the sign-in extension
cannot install on a machine that reaches no package host, so it is made only where the machine has a NAT or an address
of its own (and section 9.2 asks what a `deny_egress` network must allow); flow logs share the watcher's group across
requests, so their names carry the request id; a requester identity that is another cloud's account must not fail an
Azure request; a member never found must still be revocable; and a machine's parts are read from the machine, since
Azure names the interface it makes from `networkInterfaceConfigurations` itself (and tags none of them, so the group's
removal takes an untagged resource named after the request as the request's).

**Step 3, the first real run** on a sandbox subscription the user names, as the Google spikes were run: the service's
custom role and the conditioned RBAC Administrator grant, a request with a network, two machines (one Marketplace
image), a disk, a bucket, two members at `use` and `admin`, grant, Bastion login as a member, stop, start, resize,
destroy; the nine facts of section 9.2 that step 2 touches. Recorded in a new `docs/AZURE-SPIKE.md` or a section of
DELIVERY-SPIKE.md, as the user prefers.

**Step 4, the managed services.** Secrets (vault per request, data-plane writes, purge on destroy), queues, the
registry prefix with ABAC conditions, database (private endpoint, the SQL user from inside the network), cache
(Azure Managed Redis, `listKeys` into the secret). Then their real run. Built on 2026-11-05 on branch `azure-step-4`,
with `tests/cloudinfra/test_infra_azure_services.py`; the user chose that day to build every kind before any real run.
api-versions were read on each template reference page that day. What changed while building it:

- *The database's user is its administrator.* Azure has no API for a database user and the service cannot reach a
  private server to run SQL, so the request's username is the server's administrator, as a Cloud SQL user is a
  cloudsqlsuperuser on Google; Azure's reserved names and, on PostgreSQL, any name but letters and digits are refused.
- *MySQL is 8.0.* The GA API offers `8.0.21` and `5.7`; 8.4 is only in a preview version, which the adapter does not use.
- *The service writes secrets but never reads them.* Its role holds the vault data actions `setSecret`, `delete` and
  `purge` at its scope, not `getSecret`, so no vault grant is made per request (and none waits ten minutes to apply).
- *The private DNS zone lives with the network*, in its group and tagged as its request: a network takes one zone of a
  name, and several requests' databases may join one network.
- *A registry prefix carries the request id* (`<name>-<registry>-<8 hex>`), so two requests of one name never share
  repositories. Azure keeps one assignment of a role per member and scope, and the ACR ABAC page puts several prefixes in
  one condition, so a member's repository role on the operator's registry is one assignment whose condition names every
  prefix it is granted on: a grant widens it, a revoke narrows it, the last one deletes it, one change at a time.
- *Grants are removed before their resource is deleted*, for every member the request ever had (a grant that failed
  halfway may have made one assignment): a registry's scope is the operator's registry, which outlives the request.
  A bucket that still holds blobs is refused before any grant goes, and keeps its members.
- *A registry is torn down in the registry it was made in*, read from the resource, at the login server Azure answers,
  with a token for the registry's own audience; its catalog is read from the prefix on, page by page as its Link header
  says.
- *Private DNS zones follow the cloud*: `privatelink.` and the cloud's own database suffix; Azure Managed Redis's zone is
  documented for the public cloud only, so caches are refused elsewhere.

A code review of the step found these last four, the registry's, and two slow paths (a purge waited on for a secret
never written, a vault purge asked for a request that never had a vault); all fixed with a test each.


**Step 5, clusters, services and jobs.** AKS with Azure RBAC at namespace scope, workload identity, the node
identity, the admission policy, Application Gateway for Containers with cert-manager; the Container Apps environment,
apps and jobs with Easy Auth and Key Vault references. Then their real run, over plain HTTP first, HTTPS when the
user has a domain, as on Google.

Built on 2026-11-05 on branch `azure-step-5`, with `tests/cloudinfra/test_infra_azure_platform.py`, together with the
load balancer the step-2 plan had left for later; api-versions read on each template reference page that day
(ContainerService `2026-08-01`, App `2026-07-01`, Cdn `2026-07-01`). What changed while building it:

- *No front door on a cluster yet.* Application Gateway for Containers' AKS add-on is preview only on 2026-11-05 (its
  property is in no GA managedClusters version) and wants a `/24` of its own; the adapter calls GA APIs only, so
  `cluster.public` is refused with the reason, and cert-manager is not installed. AKS's RBAC Writer role has no Gateway
  API data actions either, so a front door will also need a custom role for HTTPRoutes.
- *Members are Azure RBAC, not RoleBindings.* The Cluster User role on the cluster (its user kubeconfig) and RBAC
  Reader or Writer at the request's namespace, by object id: no cluster call per member, and no doubt about how AKS names
  a user to Kubernetes.
- *No API server VNet integration.* It needs a delegated subnet and a role on it for no change in exposure: the API
  server stays public behind Entra sign-in and the operator's authorized ranges, as a GKE cluster's is behind IAM.
- *A cluster needs its request network's NAT* (`userAssignedNATGateway`); its nodes are raised to the system pool's floor
  (two of four vCPUs, the same memory per vCPU where a size has it) and the plan says so through a new
  `CloudProvider.warnings`. The orchestrator's quota check now counts a cluster's nodes.
- *The kubelet identity is AKS's own*, granted Repository Reader under prefixes the operator lists (`cluster_registries`,
  `LOGIN_SERVER/PREFIX`), the same one-assignment-per-member model as members' registry roles.
- *The Container Apps environment is the network's, not the request's.* A delegated subnet holds one environment, so a
  network shared through `network_id` shares it. A network sets aside a `/27` when its request holds a service or job
  (the machines' subnet becomes the first half of its range); a network made without one cannot take services later,
  and says so. The network's delete removes the environment first.
- *Internal only.* The environment is internal, so a service is reached from its own network (the network's security
  group admits nothing else); `ingress: all` is refused until Front Door Premium's Private Link to the environment is
  built.
- *Easy Auth needs an app registration*, which the operator makes once (`service_auth_client_id`): the service never
  creates directory applications. Its allow-list holds object ids (500 characters, thirteen members) and groups, and
  starts with the nil GUID so that it is never empty, since an empty one admits the whole tenant.
- *No Key Vault references in the shell.* A secret asked for without a value is a disabled version Container Apps could
  not read at creation; the runtime identity gets Secrets User on each named secret, and the delivery layer adds the
  references with the release that uses them.
- *Container Apps' fixed shapes*: 2 GiB per vCPU, at most 4 vCPUs, a 240-second request; a default the caller did not
  set is lowered with a warning, a value they did set is refused. A job starts all of a run's tasks at once and gives no
  task index; a schedule is UTC.
- *Narrower roles than the note planned.* `admin` on a service is Container Apps Operator, not Contributor, whose
  `containerApps/*` includes the auth config and so turning the sign-in off; `use` on a job is Jobs Reader, not Jobs
  Operator, because a job's start takes an image and a command and runs them as its identity, which makes starting an
  `admin` right. Members of Writer in a cluster stay as planned.
- *A cluster opens its pod range on its subnet.* Azure CNI overlay does not encapsulate pod traffic between nodes, and the
  subnet's security group sees it (concepts-network-azure-cni-overlay), so the request's deny-inbound rule would drop
  DNS from a pod on one node to CoreDNS on another; the cluster adds `<cluster>-pods` (pod range to pod range) before
  its nodes start and removes it after them.
- *Every load balancer is a Standard load balancer* with an IP-based pool on the network the machines' own interfaces
  are on (read from them: a load balancer over another request's machines is not on this request's network), its port
  opened to those machines' addresses only (no write to the machines' interfaces); with
  hostnames, Front Door Standard sits in front with the load balancer's address as its origin, and the network admits
  `AzureFrontDoor.Backend` only. Checking `X-Azure-FDID` against the profile is the application's, and the connect block
  says so.

**Step 6, the delivery service.** Cut the provider seam `appdelivery` lacks (`google.py` → a `cloud` module per
provider chosen by the environment), then Bastion reach and a container-host profile for Azure, Container Apps and
AKS targets, and the build: ACR quick task from a commit on an agent pool, the statement the service writes and signs
with Notation, its verification before a release. REQUIREMENTS.md, SETUP.md, PROVIDER-INTERFACE.md and the security
assessment's S2 table each get an Azure column as the steps land; CHANGES.md records each.

**What not to build.** Azure Cache for Redis (closed to new customers); a registry per request; Application Gateway
as the default L7; AKS Image Integrity enforcement (preview, audit-only); Managed DevOps Pools or GitHub-hosted
runners as the trusted builder; the SDK; hibernation; a network-injected PostgreSQL; AKS Automatic (it removes the
settings the adapter sets); Azure Policy assignments by the service (the operator's).
## 11. Sources

F: read on the page by a thread. F+: re-read by the reviewer on 2026-11-03. S: a search snippet or a secondary source.
Dates are the page's own "updated" date where it shows one.

**Platform, API, authentication, RBAC (section 3 and 4)**

| Source | Mark |
|---|---|
| [ARM overview](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/overview) (2026-08-04); [async operations](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/async-operations) (2026-03-26); [resource providers and types](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-providers-and-types); [Azure REST guidelines](https://github.com/microsoft/api-guidelines/blob/vNext/azure/Guidelines.md); [VM delete](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/delete) | F |
| [Request limits and throttling](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling) (2026-04-03); [Compute throttling limits](https://learn.microsoft.com/en-us/azure/virtual-machines/compute-throttling-limits) (2026-04-02); [troubleshooting throttling errors](https://learn.microsoft.com/en-us/troubleshoot/azure/virtual-machines/troubleshooting-throttling-errors) | F |
| [Subscription limits](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/azure-subscription-service-limits) (2026-09-29); [RBAC limits](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshoot-limits) (2026-09-11); [name rules](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules) (2026-08-05); [tags](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-resources); [tag support](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-support); [locks](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/lock-resources) (2026-04-10); [Activity Log](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/activity-log) | F (RBAC limits F+) |
| [GCP professional](https://learn.microsoft.com/en-us/azure/architecture/gcp-professional/) (2026-10-22); [AWS professional](https://learn.microsoft.com/en-us/azure/architecture/aws-professional/); [CAF subscriptions](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/landing-zone/design-area/resource-org-subscriptions); [CAF management groups](https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/landing-zone/design-area/resource-org-management-groups) | F |
| [VM token through IMDS](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token) (2026-06-15); [Container Apps managed identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity); [App Service managed identity](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity); [AKS workload identity](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview) (2026-10-20); [az account](https://learn.microsoft.com/en-us/cli/azure/account); [CLI sign-in](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-interactively) | F |
| Audiences: [Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide); [metrics batch](https://learn.microsoft.com/en-us/rest/api/monitor/metrics-batch/batch); [Storage auth](https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory); [ACR OAuth](https://github.com/Azure/acr/blob/main/docs/AAD-OAuth.md); [Service Bus auth](https://learn.microsoft.com/en-us/azure/service-bus-messaging/authenticate-application); [kubelogin](https://learn.microsoft.com/en-us/azure/aks/kubelogin-authentication); [PostgreSQL with managed identity](https://learn.microsoft.com/en-us/azure/postgresql/security/security-connect-with-managed-identity); the cloud metadata document `management.azure.com/metadata/endpoints` (observed) | F |
| [Python SDK overview](https://learn.microsoft.com/en-us/azure/developer/python/sdk/azure-sdk-overview); [credential chains](https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/credential-chains); [azure-identity](https://learn.microsoft.com/en-us/python/api/overview/azure/identity-readme); [azure-core](https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme); PyPI metadata of azure-identity 1.27.0, azure-mgmt-* and azure-cli 2.92.0 | F |
| [Role assignment create](https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/create); [RBAC troubleshooting](https://learn.microsoft.com/en-us/azure/role-based-access-control/troubleshooting) (2026-10-19); [delegate role assignments with conditions](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-overview) (2026-03-30) and [examples](https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-examples); [custom roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/custom-roles); the built-in roles pages for [compute](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/compute), [security](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/security), [storage](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage), [containers](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/containers), [integration](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/integration), [privileged](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged), [identity](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/identity), [monitor](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/monitor); [Compute permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute) | F |
| Graph: [get user](https://learn.microsoft.com/en-us/graph/api/user-get); [list groups](https://learn.microsoft.com/en-us/graph/api/group-list); [list service principals](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list); [appRoleAssignedTo](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto); [assign an app role to a managed identity](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-assign-app-role-managed-identity); [groups](https://learn.microsoft.com/en-us/entra/fundamentals/concept-learn-about-groups); [PIM](https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-resource-roles-assign-roles); [workload identity federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation) | F |
| [Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) (2026-01-06) and its answers on 2026-11-03; [Compute usages](https://learn.microsoft.com/en-us/rest/api/compute/usage/list); [Microsoft.Quota](https://learn.microsoft.com/en-us/rest/api/quota/quota/list); [quotas overview](https://learn.microsoft.com/en-us/azure/quotas/quotas-overview); [per-VM quota](https://learn.microsoft.com/en-us/azure/quotas/per-vm-quota-requests); [GPU quota defaults](https://learn.microsoft.com/en-us/azure/machine-learning/how-to-manage-quotas); [Resource SKUs](https://learn.microsoft.com/en-us/rest/api/compute/resource-skus/list) | F |
| [Metrics list](https://learn.microsoft.com/en-us/rest/api/monitor/metrics/list); [REST walkthrough](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/rest-api-walkthrough); [migrate to batch](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/migrate-to-batch-api); [VM metrics](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-metrics/microsoft-compute-virtualmachines-metrics) (2026-09-08); [VM alerts](https://learn.microsoft.com/en-us/azure/azure-monitor/vm/monitor-virtual-machine-alerts); [metrics platform](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/data-platform-metrics); [metrics troubleshooting](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/metrics-troubleshoot) | F |
| [Azure Government](https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure); [Azure China](https://learn.microsoft.com/en-us/azure/china/concepts-service-availability) | F |
| [AVM lock fix](https://github.com/Azure/terraform-azurerm-avm-res-compute-virtualmachine/pull/430); Graph app-role ids; the literal `register` path | S |

**Compute, network, storage, load balancing (section 5.1 to 5.3)**

| Source | Mark |
|---|---|
| [Sizes overview](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/overview) (2026-09-25); [Dsv5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series); [Dsv6](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv6-series); [Esv5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/memory-optimized/esv5-series); [Fsv2](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/compute-optimized/fsv2-series); [Bsv2](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bsv2-series); [NCasT4_v3](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncast4v3-series); [NC A100 v4](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nca100v4-series); [ND A100 v4](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndasra100v4-series); [NCads H100 v5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncadsh100v5-series); [ND H100 v5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndh100v5-series); [NVadsA10 v5](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series); [NCv3 retirement](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/lifecycle/retirement/ncv3-retirement) | F |
| [Find images](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/cli-ps-findimage) (2026-06-24); [Marketplace agreements](https://learn.microsoft.com/en-us/rest/api/marketplaceordering/marketplace-agreements/create); [programmatic Marketplace deploy](https://learn.microsoft.com/en-us/marketplace/programmatic-deploy-of-marketplace-products); [Azure Linux](https://learn.microsoft.com/en-us/azure/azure-linux/intro-azure-linux); [Flatcar on Azure](https://www.flatcar.org/docs/latest/deploy/cloud/azure/); [NVMe](https://learn.microsoft.com/en-us/azure/virtual-machines/enable-nvme-interface) | F |
| Ubuntu and Debian image references; the Rocky `resf` publisher and plan ([Rocky forum](https://forums.rockylinux.org/t/rocky-linux-images-on-azure-important-update/13721)); Azure Linux 3 image reference; purchase controls | S |
| [Trusted Launch](https://learn.microsoft.com/en-us/azure/virtual-machines/trusted-launch) (2026-09-26); [NVIDIA driver extension](https://learn.microsoft.com/en-us/azure/virtual-machines/extensions/hpccompute-gpu-linux) (2026-09-25); [driver setup](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/n-series-driver-setup); [HPC images](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/high-performance-compute/azure-hpc-vm-images) | F |
| [States and billing](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing) (2026-06-24); [power off](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/power-off); [deallocate](https://learn.microsoft.com/en-us/rest/api/compute/virtual-machines/deallocate); [hibernate](https://learn.microsoft.com/en-us/azure/virtual-machines/hibernate-resume); [resize](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/resize-vm); [auto-shutdown](https://learn.microsoft.com/en-us/azure/virtual-machines/auto-shutdown-vm); [DevTestLab schedules](https://learn.microsoft.com/en-us/azure/templates/microsoft.devtestlab/schedules); [Spot](https://learn.microsoft.com/en-us/azure/virtual-machines/spot-vms); [VM delete and deleteOption](https://learn.microsoft.com/en-us/azure/virtual-machines/delete) (2026-06-24) | F (states and billing F+) |
| [Disk types](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types) (2026-09-16); [add a disk](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/add-disk); [managed identities on a VM](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-configure-managed-identities); [user-assigned identity create](https://learn.microsoft.com/en-us/rest/api/managedidentity/user-assigned-identities/create-or-update); [federated credentials](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-considerations); [managed identities FAQ](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identities-faq) | F |
| [Entra SSH for Linux](https://learn.microsoft.com/en-us/entra/identity/devices/howto-vm-sign-in-azure-ad-linux) (2026-11-02); [Bastion SKUs](https://learn.microsoft.com/en-us/azure/bastion/bastion-sku-comparison); [Bastion settings](https://learn.microsoft.com/en-us/azure/bastion/configuration-settings); [Bastion native client](https://learn.microsoft.com/en-us/azure/bastion/native-client) and [Linux](https://learn.microsoft.com/en-us/azure/bastion/connect-vm-native-client-linux); [Bastion peering](https://learn.microsoft.com/en-us/azure/bastion/vnet-peering); [JIT access](https://learn.microsoft.com/en-us/azure/defender-for-cloud/just-in-time-access-overview); [Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command); [managed Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command-managed); [Run Command overview](https://learn.microsoft.com/en-us/azure/virtual-machines/run-command-overview); [CustomScript](https://learn.microsoft.com/en-us/azure/virtual-machines/extensions/custom-script-linux); [Arc SSH](https://learn.microsoft.com/en-us/azure/azure-arc/servers/ssh-arc-overview); [VM Applications](https://learn.microsoft.com/en-us/azure/virtual-machines/vm-applications) | F |
| Bastion hourly prices ([pricing page](https://azure.microsoft.com/en-us/pricing/details/azure-bastion/) did not render; third-party figures) | S |
| [VNet FAQ](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-networks-faq); [NSGs](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-groups-overview) (2025-10-23); [NSG evaluation](https://learn.microsoft.com/en-us/azure/virtual-network/network-security-group-how-it-works); [service tags](https://learn.microsoft.com/en-us/azure/virtual-network/service-tags-overview) (2026-09-16); [VNet flow logs](https://learn.microsoft.com/en-us/azure/network-watcher/vnet-flow-logs-overview); [NAT Gateway](https://learn.microsoft.com/en-us/azure/nat-gateway/nat-overview); [default outbound access](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access) (2026-09-10); [private endpoint DNS](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns); [service endpoints](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-network-service-endpoints-overview); [DDoS tiers](https://learn.microsoft.com/en-us/azure/ddos-protection/ddos-protection-sku-comparison); [Azure Firewall pricing](https://azure.microsoft.com/en-us/pricing/details/azure-firewall/) | F (default outbound F+) |
| [Load Balancer](https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-overview) (2026-08-21); [Application Gateway features](https://learn.microsoft.com/en-us/azure/application-gateway/features); [TLS policy](https://learn.microsoft.com/en-us/azure/application-gateway/application-gateway-ssl-policy-overview); [Key Vault certificates](https://learn.microsoft.com/en-us/azure/application-gateway/key-vault-certs); [Front Door](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-overview) (2026-08-28); [Front Door Private Link](https://learn.microsoft.com/en-us/azure/frontdoor/private-link); [Application Gateway for Containers](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/overview) (2026-11-03) | F |
| [Availability zones](https://learn.microsoft.com/en-us/azure/reliability/availability-zones-overview) (2026-10-30); [storage account overview](https://learn.microsoft.com/en-us/azure/storage/common/storage-account-overview); [create a storage account](https://learn.microsoft.com/en-us/azure/storage/common/storage-account-create); [anonymous access](https://learn.microsoft.com/en-us/azure/storage/blobs/anonymous-read-access-prevent); [shared key](https://learn.microsoft.com/en-us/azure/storage/common/shared-key-authorization-prevent); [blob soft delete](https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-overview); [blob data roles](https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access); [scalability targets](https://learn.microsoft.com/en-us/azure/storage/common/scalability-targets-standard-account); [AzCopy identity](https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azcopy-authorize-user-identity) | F |

**Managed services (section 5.4 and 5.5)**

| Source | Mark |
|---|---|
| PostgreSQL: [supported versions](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-supported-versions) (2026-08-26); [version policy](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-version-policy); [compute](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-compute); [storage](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-storage); [reliability](https://learn.microsoft.com/en-us/azure/reliability/reliability-postgresql-flexible-server); [backup](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-backup-restore); [security](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-security); [TLS](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-ssl-tls); [private access](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-private); [Private Link](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-private-link) (2026-09-06); [quickstart](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/quickstart-create-server); [stop](https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/how-to-stop-server); [Entra sign-in](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-configure-sign-in-azure-ad-authentication); [limits](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-limits); the [flexibleServers](https://learn.microsoft.com/en-us/azure/templates/microsoft.dbforpostgresql/flexibleservers), [databases](https://learn.microsoft.com/en-us/azure/templates/microsoft.dbforpostgresql/flexibleservers/databases) and [administrators](https://learn.microsoft.com/en-us/azure/templates/microsoft.dbforpostgresql/flexibleservers/administrators) templates | F (Private Link F+) |
| MySQL: [version policy](https://learn.microsoft.com/en-us/azure/mysql/concepts-version-policy) (2026-09-16); [tiers and storage](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-service-tiers-storage); [HA](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-high-availability); [VNet](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-networking-vnet); [Private Link](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-networking-private-link); [Entra](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/concepts-azure-ad-authentication); [stop and start](https://learn.microsoft.com/en-us/azure/mysql/flexible-server/how-to-stop-start-server-portal); the [flexibleServers](https://learn.microsoft.com/en-us/azure/templates/microsoft.dbformysql/flexibleservers) template | F |
| Redis: [Azure Cache for Redis what's new](https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/cache-whats-new) (2026-10-03); [Azure Cache for Redis overview](https://learn.microsoft.com/en-us/azure/azure-cache-for-redis/cache-overview); [Managed Redis overview](https://learn.microsoft.com/en-us/azure/redis/overview); [architecture](https://learn.microsoft.com/en-us/azure/redis/architecture); [reliability](https://learn.microsoft.com/en-us/azure/reliability/reliability-managed-redis); [Private Link](https://learn.microsoft.com/en-us/azure/redis/private-link); [Entra](https://learn.microsoft.com/en-us/azure/redis/entra-for-authentication); the [redisEnterprise](https://learn.microsoft.com/en-us/azure/templates/microsoft.cache/redisenterprise) and [databases](https://learn.microsoft.com/en-us/azure/templates/microsoft.cache/redisenterprise/databases) templates | F (what's new and overview F+) |
| Redis `listKeys` action name; Managed Redis prices and creation time; the retirement FAQ page (404 on re-read) | S |
| Service Bus: [premium messaging](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-premium-messaging); [Private Link](https://learn.microsoft.com/en-us/azure/service-bus-messaging/private-link-service); [quotas](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-quotas); [expiration](https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-expiration); [disable local auth](https://learn.microsoft.com/en-us/azure/service-bus-messaging/disable-local-authentication); [FAQ](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-faq); the [namespaces](https://learn.microsoft.com/en-us/azure/templates/microsoft.servicebus/namespaces) and [subscriptions](https://learn.microsoft.com/en-us/azure/templates/microsoft.servicebus/namespaces/topics/subscriptions) templates; Event Grid [namespaces](https://learn.microsoft.com/en-us/azure/event-grid/concepts-event-grid-namespaces) and [pull delivery](https://learn.microsoft.com/en-us/azure/event-grid/pull-delivery-overview) | F |
| Service Bus prices; Event Grid retention | S |
| Key Vault: [soft delete](https://learn.microsoft.com/en-us/azure/key-vault/general/soft-delete-overview) (2026-04-10); [service limits](https://learn.microsoft.com/en-us/azure/key-vault/general/service-limits); [network security](https://learn.microsoft.com/en-us/azure/key-vault/general/network-security); [trusted services](https://learn.microsoft.com/en-us/azure/key-vault/general/overview-vnet-service-endpoints); [set secret](https://learn.microsoft.com/en-us/rest/api/keyvault/secrets/set-secret/set-secret); the [vaults](https://learn.microsoft.com/en-us/azure/templates/microsoft.keyvault/vaults) template; [Container Apps secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets); [AKS CSI driver](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver) | F |
| ACR: [SKUs](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-skus) (2026-10-15); [ABAC repository permissions](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions) (2026-06-29); [image lock](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-image-lock); [Tasks overview](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-overview); [quick task](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-quick-task); [task logs](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-logs); [agent pools](https://learn.microsoft.com/en-us/azure/container-registry/tasks-agent-pools); [task identity](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-authentication-managed-identity); [trusted services](https://learn.microsoft.com/en-us/azure/container-registry/allow-access-trusted-services); [Private Link](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-private-link); [soft delete](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-soft-delete-policy); [retention](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-retention-policy); [scoped tokens](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-repository-scoped-permissions); the [registries](https://learn.microsoft.com/en-us/azure/templates/microsoft.containerregistry/registries) and [tasks](https://learn.microsoft.com/en-us/azure/templates/microsoft.containerregistry/registries/tasks) templates; [Container Apps pull with identity](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity-image-pull); [AKS and ACR](https://learn.microsoft.com/en-us/azure/aks/cluster-container-registry-integration); [az acr](https://learn.microsoft.com/en-us/cli/azure/acr) | F (ABAC F+) |
| ACR `scheduleRun` and `runs` fields (reference pages 404); the ABAC GA announcement ([Tech Community](https://techcommunity.microsoft.com/blog/appsonazureblog/azure-container-registry-repository-permissions-with-attribute-based-access-cont/4467182)); the private-repository path of a quick task | S |
| AKS: [Entra authorization](https://learn.microsoft.com/en-us/azure/aks/entra-id-authorization) (2026-08-18); [private clusters](https://learn.microsoft.com/en-us/azure/aks/private-clusters); [authorized ranges](https://learn.microsoft.com/en-us/azure/aks/api-server-authorized-ip-ranges); [API server VNet integration](https://learn.microsoft.com/en-us/azure/aks/api-server-vnet-integration); [CNI overlay](https://learn.microsoft.com/en-us/azure/aks/azure-cni-overlay); [managed identity](https://learn.microsoft.com/en-us/azure/aks/use-managed-identity); [system pools](https://learn.microsoft.com/en-us/azure/aks/use-system-pools); [tiers](https://learn.microsoft.com/en-us/azure/aks/free-standard-pricing-tiers); [auto-upgrade](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-cluster) and [node OS](https://learn.microsoft.com/en-us/azure/aks/auto-upgrade-node-os-image); [versions](https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions); [deployment safeguards](https://learn.microsoft.com/en-us/azure/aks/deployment-safeguards); [AGC components](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/application-gateway-for-containers-components); [AGC TLS](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/how-to-ssl-offloading-gateway-api); [Istio add-on](https://learn.microsoft.com/en-us/azure/aks/istio-about); [app routing](https://learn.microsoft.com/en-us/azure/aks/app-routing); [AKS Automatic](https://learn.microsoft.com/en-us/azure/aks/intro-aks-automatic); [Image Integrity](https://learn.microsoft.com/en-us/azure/aks/image-integrity); [AKS tags](https://learn.microsoft.com/en-us/azure/aks/use-tags); [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/); the [managedClusters](https://learn.microsoft.com/en-us/azure/templates/microsoft.containerservice/managedclusters) template | F |
| AKS creation time; `OperationNotAllowed` ([AKS troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/operationisnotallowed)) | S |
| Container Apps: [environments](https://learn.microsoft.com/en-us/azure/container-apps/environment); [custom networks](https://learn.microsoft.com/en-us/azure/container-apps/custom-virtual-networks); [networking](https://learn.microsoft.com/en-us/azure/container-apps/networking); [revisions](https://learn.microsoft.com/en-us/azure/container-apps/revisions); [traffic splitting](https://learn.microsoft.com/en-us/azure/container-apps/traffic-splitting); [ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview); [IP restrictions](https://learn.microsoft.com/en-us/azure/container-apps/ip-restrictions); [scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app); [authentication](https://learn.microsoft.com/en-us/azure/container-apps/authentication); [jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs); [billing](https://learn.microsoft.com/en-us/azure/container-apps/billing); [workload profiles](https://learn.microsoft.com/en-us/azure/container-apps/workload-profiles-overview); [containerapp up](https://learn.microsoft.com/en-us/azure/container-apps/containerapp-up); [FAQ](https://learn.microsoft.com/en-us/azure/container-apps/faq); the [managedEnvironments](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/managedenvironments), [containerApps](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/containerapps) and [jobs](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/jobs) templates; [Container Instances](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-overview) | F |

**Governance, cost, build, multi-cloud (section 6, 7 and 8)**

| Source | Mark |
|---|---|
| [Well-Architected pillars](https://learn.microsoft.com/en-us/azure/well-architected/pillars); [MCSB](https://learn.microsoft.com/en-us/security/benchmark/azure/overview); [built-in initiatives](https://learn.microsoft.com/en-us/azure/governance/policy/samples/built-in-initiatives); [CIS Azure benchmarks](https://www.cisecurity.org/benchmark/azure); [Defender for Cloud](https://learn.microsoft.com/en-us/azure/defender-for-cloud/defender-for-cloud-introduction); [secure score](https://learn.microsoft.com/en-us/azure/defender-for-cloud/secure-score-security-controls); [Defender for Containers](https://learn.microsoft.com/en-us/azure/defender-for-cloud/defender-for-containers-introduction) | F |
| [RequestDisallowedByPolicy](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/error-policy-requestdisallowedbypolicy); [common deployment errors](https://learn.microsoft.com/en-us/azure/azure-resource-manager/troubleshooting/common-deployment-errors); [Modify effect](https://learn.microsoft.com/en-us/azure/governance/policy/concepts/effect-modify); the policy definitions on [azadvertizer.net](https://www.azadvertizer.net/) | F |
| [Cost Management query](https://learn.microsoft.com/en-us/rest/api/cost-management/query/usage); [cost data](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-cost-mgt-data); [automation limits](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/manage-automation); [tag inheritance](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/enable-tag-inheritance); [budgets](https://learn.microsoft.com/en-us/rest/api/consumption/budgets/create-or-update); [budget scenario](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/cost-management-budget-scenario); [exports](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-improved-exports); [cost allocation](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/allocate-costs) | F |
| [Azure Pipelines run](https://learn.microsoft.com/en-us/rest/api/azure/devops/pipelines/runs/run-pipeline); [workload identity for Pipelines](https://learn.microsoft.com/en-us/azure/devops/pipelines/release/configure-workload-identity); [Managed DevOps Pools](https://learn.microsoft.com/en-us/azure/devops/managed-devops-pools/architecture-overview); [GitHub attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations) and [use](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations); [SLSA levels](https://slsa.dev/spec/v1.0/levels); [ARC on AKS](https://learn.microsoft.com/en-us/azure/aks/github-actions-azure-files-overview); [sign with Notation](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-sign-build-push); [Artifact Signing](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-github-sign-notation-artifact-signing) and [overview](https://learn.microsoft.com/en-us/azure/artifact-signing/overview); [code to cloud options](https://learn.microsoft.com/en-us/azure/container-apps/code-to-cloud-options) | F |
| ACR retirements ([end of support 2026](https://learn.microsoft.com/en-us/lifecycle/end-of-support/end-of-support-2026)); SLSA in Azure Pipelines; the ACR provenance question | S |
| [CLI install](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-linux); [CLI releases](https://github.com/Azure/azure-cli/releases/latest); [CLI extensions](https://learn.microsoft.com/en-us/cli/azure/azure-cli-extensions-overview); PyPI azure-cli | F |
| [azurerm features](https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/guides/features-block.html.markdown); [azurerm role assignment](https://raw.githubusercontent.com/hashicorp/terraform-provider-azurerm/main/website/docs/r/role_assignment.html.markdown); [AzAPI](https://raw.githubusercontent.com/Azure/terraform-provider-azapi/main/docs/index.md); [Pulumi azure-native versions](https://www.pulumi.com/registry/packages/azure-native/version-guide/); [Crossplane provider-upjet-azure](https://raw.githubusercontent.com/crossplane-contrib/provider-upjet-azure/main/AUTHENTICATION.md); [Thoughtworks](https://www.thoughtworks.com/radar/techniques/generic-cloud-usage); [Hohpe](https://martinfowler.com/articles/oss-lockin.html) | F |
| [Radius recipes](https://docs.radapp.io/guides/recipes/overview); [subnet races](https://github.com/hashicorp/terraform-provider-azurerm/issues/21293); [multicloud trade-off](https://www.infoworld.com/article/3209737/understand-the-multicloud-management-trade-off.html) | S |
