# Requirements reference

Everything the two services need, with versions, in one place: the language and packages, the two container images and
their pinned inputs, the tools CI runs, what a host needs to run each service, what the Google Cloud project must have
(APIs, the services' identities and their roles, network), the product versions the adapter offers, the limits that are
built in, and the clients that can call the services. Every number here is copied from a file in this repository; the
last section says which file is the source of truth for each and how to refresh it. Updated 2026-10-30 for
`cloudinfra` 1.37.1 and `appdelivery` 0.1.0.

## 1. Language and runtime

| Requirement | Version | Where it is set |
|---|---|---|
| Python | 3.12 or newer (`requires-python = ">=3.12"`); CI and both images use 3.12 | `pyproject.toml`, `.github/workflows/ci.yml`, both Dockerfiles |
| Operating system | Any that runs Python 3.12; the images are Linux (amd64 and arm64); the delivery service's host script is bash 3.2 compatible and runs on Container-Optimized OS | `Dockerfile.appdelivery`, `appdelivery/host/release.sh` |
| State | SQLite, one file per service in WAL mode, one process per file (an exclusive lock file beside it) | `cloudinfra/state/state_manager.py`, `appdelivery/state.py` |

## 2. Python packages

Direct dependencies, pinned exactly in `pyproject.toml` and hash-locked in `requirements.lock`:

| Package | Version | Used for |
|---|---|---|
| `mcp` | 1.30.0 | The MCP server (Streamable HTTP) and its OAuth routes for the Claude chat sign-in |
| `fastapi` | 0.143.0 | The HTTP application: REST routes, the mounted MCP app, health |
| `httpx` | 0.28.1 | Every call to Google's APIs, to a cluster's API, and from `appdelivery` to `cloudinfra` |
| `pydantic` | 2.14.1 | The strict contracts (requests, releases, configuration), unknown fields refused |
| `PyYAML` | 6.0.3 | Configuration files, request and release files |
| `PyJWT[crypto]` | 2.15.1 | Tokens of the Claude chat sign-in |
| `cryptography` | 50.0.2 | PyJWT's signatures, and the delivery service's check of Google's signature on Cloud Build provenance; declared directly since 2026-10-30, at the version PyJWT already brought |
| `uvicorn` | 0.54.0 | The ASGI server both images run |

Transitive packages, as `requirements.lock` resolves them (each with its hashes there): annotated-doc 0.0.5,
annotated-types 0.8.0, anyio 4.15.1, attrs 26.1.0, certifi 2026.10.19, cffi 2.1.1, click 8.5.0, cryptography 50.0.2,
h11 0.16.0, httpcore 1.0.9, httpx-sse 0.4.3, idna 3.20, jsonschema 4.26.0, jsonschema-specifications 2025.9.1,
opentelemetry-api 1.45.1, pycparser 3.11, pydantic-core 2.50.1, pydantic-settings 2.15.0, python-dotenv 1.2.4,
python-multipart 0.0.32, referencing 0.37.0, rpds-py 2026.9.1, sse-starlette 3.5.0, starlette 1.7.0,
typing-extensions 4.16.0, typing-inspection 0.4.4.

Development extras (`pip install -e .[dev]`, or `requirements-dev.lock` with hashes, which CI installs; it adds iniconfig 2.3.1,
packaging 26.3, pluggy 1.6.0, pygments 2.21.0 and sortedcontainers 2.4.0 to the list above):

| Package | Version | Used for |
|---|---|---|
| `pytest` | 9.1.1 | The test suite (`tests/`), 1,083 tests on 2026-11-05, none touching a cloud |
| `ruff` | 0.17.0 | Lint: correctness, imports, bug-prone patterns, modern syntax and the bandit security rules, line length 200 |
| `zizmor` | 1.30.1 | Lint of the GitHub workflows and the Dependabot file |
| `hypothesis` | 6.170.12 | Property tests of the validators and the redaction over generated input (`test_infra_properties.py`, `test_delivery_properties.py`) |

Dependency audit (`audit.in` → `audit.lock`, installed by CI only): pip-audit 2.10.1 with its own locked dependencies
(cachecontrol 0.14.4, cyclonedx-python-lib 11.12.0, packageurl-python 0.17.6, pip 26.2.1, pip-api 0.0.35,
pip-requirements-parser 32.0.1, requests 2.34.2, rich 15.0.0, urllib3 2.8.0 and the rest listed in the file). CI audits
`requirements.lock` and `requirements-dev.lock` on every push and weekly.

The lock files are written by `uv pip compile --generate-hashes` (the exact command is the first line of each lock);
`uv` itself is not pinned.

## 3. Container images

Both images start from the same Python image, pinned by its multi-arch index digest, and run as user 65532 with the
configuration mounted at `/config` and the state under `/data`.

| Image | File | Base | Adds | Port | Command |
|---|---|---|---|---|---|
| Infrastructure service | `Dockerfile.cloudinfra` | `python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e` (resolved 2026-11-05, Python 3.12.16; the image of 2026-11-03 carried apt 3.0.3, CVE-2026-109518) | nothing but the package | 8100 | `uvicorn cloudinfra.mcp.server:create_app --factory` |
| Delivery service | `Dockerfile.appdelivery` | the same digest | gcloud CLI, Cosign, `openssh-client`, `ca-certificates` | 8200 | `uvicorn appdelivery.server:create_app --factory` |

Pinned inputs of the delivery image, each checked by SHA-256 at build time:

| Input | Version | SHA-256 (amd64) | SHA-256 (arm64) | Source |
|---|---|---|---|---|
| Google Cloud CLI | 591.0.0 | `78c570f5fcfe58a9311bd98c06b75530595e3cae6277aad75a676a39e6ee7b63` | `f5e5df0433f77c0e7f5df9fd4bce2e46c7b7967e79cf66e31b1164d3d4c86546` | `storage.googleapis.com/cloud-sdk-release/google-cloud-cli-591.0.0-linux-{x86_64,arm}.tar.gz` |
| Cosign | v3.1.4 | `424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa` | `ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2` | `github.com/sigstore/cosign/releases/download/v3.1.4/cosign-linux-{amd64,arm64}` |

The gcloud sums were derived on 2026-10-29 as the Dockerfile's header describes (Google publishes a SHA-256 only for the
unversioned archive); repeat that check when moving the version. The build needs `--build-arg TARGETARCH=amd64|arm64`
on a builder without BuildKit (Cloud Build's docker step). gcloud runs on the image's Python: the archive's bundled
interpreter (`platform/bundledpythonunix`) is removed and `CLOUDSDK_PYTHON=/usr/local/bin/python3` set, so the base
image's digest is the one Python to keep current. The delivery image sets
`CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1`, `CLOUDSDK_CONFIG=/data/gcloud` and `HOME=/data`.

## 4. Continuous integration and local checks

| Tool | Version | Pinned how | Runs |
|---|---|---|---|
| GitHub Actions runner | `ubuntu-24.04` | label | every workflow; `.github/workflows/ci.yml` on push, pull request and weekly (Monday 06:19 UTC) |
| `actions/checkout` | v7.0.1 | commit `3d3c42e5aac5ba805825da76410c181273ba90b1` | |
| `actions/setup-python` | v7.1.0 | commit `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` (Dependabot moves it) | Python 3.12 |
| ruff | 0.17.0 | `requirements-dev.lock` | `python -m ruff check .` |
| zizmor | 1.30.1 | `requirements-dev.lock` | `zizmor .github/workflows .github/dependabot.yml` |
| hadolint | v2.15.1 | downloaded by version, SHA-256 `c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507` | both Dockerfiles, failing on errors |
| pip-audit | 2.10.1 | `audit.lock` | every lock: `requirements.lock`, `requirements-dev.lock`, `audit.lock` itself and `pip.lock` |
| pip | 26.2.2 | `pip.lock` (from `pip.in`) | installed first in both images, by hash: the base image's pip is older than the 2026 advisories fixed in 26.x |
| pytest | 9.1.1 | `requirements-dev.lock` | `python -m pytest -q`, with `tests/test_repository.py`: no credential in a tracked file, every lock hashed, the dependency set exactly section 2's |
| hypothesis | 6.170.12 | `requirements-dev.lock` | property tests of both contracts' validators and the redaction, inside `pytest` |
| grype | v0.121.0 | downloaded by version, SHA-256 `64e07133d3486115de647a66581461dccf5c732748a0a3119077395565f15519` (linux amd64, from the release's checksums file) | both images built in CI (`images` job of `ci.yml`), failing on a high or critical vulnerability that has a fix; its database is fetched current on each run |
| `github/codeql-action` | v4.39.1 | commit `9b3254085494b55a0f7c4c2b3d2d52cc712ce370` | `.github/workflows/codeql.yml`: `security-extended` over Python and the workflows, on push to `main`, pull request and weekly (Monday 06:41 UTC); `upload-sarif` for Scorecard |
| `ossf/scorecard-action` | v2.5.0 | commit `fa7117db83c52d787ae5a89f350999701505d78e` | `.github/workflows/scorecard.yml`: on push to `main`, a branch-protection change and weekly (Monday 05:07 UTC); results published to the Scorecard API and the Security tab |
| `actions/dependency-review-action` | v5.0.0 | commit `a1d282b36b6f3519aa1f3fc636f609c47dddb294` | `.github/workflows/dependency-review.yml`: each pull request, failing on a dependency it adds with a known vulnerability of moderate severity or above |
| `cloud-infra validate` and `plan` | the package | | `examples/infrastructure-request.yaml`, offline |
| Dependabot | | `.github/dependabot.yml` | weekly, the workflows' actions and the Dockerfiles' base image digest, releases at least 7 days old; Dependabot alerts are expected to be on in the repository's settings |
| pre-commit (optional) | ruff-pre-commit v0.17.0, zizmor-pre-commit v1.30.1 | `.pre-commit-config.yaml` | the same two lints locally |

Docker is needed only for a local development build of the images (Docker Desktop built both for amd64 and arm64 on
2026-10-29). Images that are deployed are built only in the trusted project by Cloud Build from `cloudbuild.yaml`, whose
docker step is pinned by digest (`gcr.io/cloud-builders/docker@sha256:f772ab45…`, the delivery service's default
`builder` too; a test ties them).

## 5. What a host needs to run each service

**The infrastructure service (`cloudinfra`).**

- Python 3.12 with `requirements.lock`, or the image.
- A Google identity: on Google Cloud, the machine's attached service account (read through the metadata server); on a
  laptop, the gcloud CLI signed in (`gcloud auth login`), optionally impersonating the service's account
  (`providers.gcp.impersonate_service_account`). No key file is read, ever. On a laptop also set
  `providers.gcp.quota_project`, or Google counts the calls against gcloud's shared project and rate limits them
  (seen 2026-10-29); the person needs `serviceusage.services.use` on that project.
- Network reach to `*.googleapis.com`, and to each cluster's API endpoint it made (for namespaces, RoleBindings, the
  admission policy and the front door).
- With an `azure` block (every kind, not yet run on a real subscription): an Azure
  identity, the first the host offers: an AKS pod's workload identity (`AZURE_FEDERATED_TOKEN_FILE`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, which
  AKS's webhook sets), a Container Apps or App Service managed identity (`IDENTITY_ENDPOINT`, `IDENTITY_HEADER`), a
  machine's managed identity through the metadata service at `169.254.169.254` (reached directly, never through a
  proxy), or on a laptop the Azure CLI signed in (`az login`; 2.54.0 or newer, which reports `expires_on`). No client
  secret or certificate is read, ever. Network reach to the cloud's Resource Manager (`management.azure.com` on the
  public cloud), Microsoft Graph (`graph.microsoft.com`, to look members up), the blob service of the buckets it made
  (`*.blob.core.windows.net`, to check one is empty before deleting it), and, for workload identity, Entra ID's sign-in
  host, and each AKS cluster's API server it made (`*.azmk8s.io`, for the namespace and the admission policy), from an
  address the operator's `cluster_authorized_networks` admits when it lists any. The operator's price snapshot
  (`cloud-infra prices --write`) reads `prices.azure.com`, which needs no sign-in.
- A writable directory for the state file, its lock file and, if set, the log file.
- A configuration file (`cloudinfra.example.yaml` is the template) named by `CLOUDINFRA_CONFIG`.

**The delivery service (`appdelivery`).**

- Python 3.12 with `requirements.lock`, or the image.
- The gcloud CLI with an SSH client: machines are reached only through `gcloud compute ssh --tunnel-through-iap`; the
  image ships 591.0.0, a laptop ran 539.0.0 in the spike.
- Cosign 3.1.x when an environment sets `require_attestation` (`cosign verify-attestation`).
- A Google identity as above, which must hold `roles/iam.serviceAccountTokenCreator` on each environment's service
  account; every action is taken with a 15-minute token of that account issued by the IAM Credentials API.
- HTTP reach to the infrastructure service's REST API (`cloudinfra.url`), to `iamcredentials.googleapis.com`,
  `secretmanager.googleapis.com`, `run.googleapis.com`, and to the API endpoint of each target cluster.
- A writable `/data` (the state, the log, gcloud's configuration and the SSH key it makes for OS Login).
- The environment variables that hold each environment's `cloudinfra` token (`environments.<name>.token_env`), and a
  configuration file (`appdelivery.example.yaml` is the template) named by `APPDELIVERY_CONFIG`.

Both services, when Claude chat is to sign in: a public HTTPS address behind a TLS proxy, its host in `allowed_hosts`,
and the `sign_in` block. They hold bearer tokens, not TLS.

## 6. The Google Cloud project

**APIs the adapter calls** (enable the ones the kinds you offer need; `cloudinfra/providers/gcp/api.py` lists the
endpoints):

| API | Used for |
|---|---|
| Compute Engine `compute.googleapis.com` | machines, disks, networks, subnets, firewall rules, addresses, load balancers, managed certificates, project metadata |
| Cloud SQL Admin `sqladmin.googleapis.com` | managed databases |
| Cloud Storage `storage.googleapis.com` | buckets and their IAM policies |
| Service Networking `servicenetworking.googleapis.com` | private databases (reserved range and peering) |
| Cloud Resource Manager `cloudresourcemanager.googleapis.com` | the project number, project IAM for node accounts |
| Pub/Sub `pubsub.googleapis.com` | queues |
| Secret Manager `secretmanager.googleapis.com` | secrets, the database password and cache AUTH string among them |
| Memorystore for Redis `redis.googleapis.com` | caches |
| Artifact Registry `artifactregistry.googleapis.com` | image registries, and the repository grants of node accounts |
| Kubernetes Engine `container.googleapis.com` | clusters; the cluster's own API is then called directly |
| IAM `iam.googleapis.com` | service accounts made per cluster, service and job |
| Identity-Aware Proxy `iap.googleapis.com` | the tunnel IAM policy of each machine |
| Cloud Run Admin v2 `run.googleapis.com` | managed container services and jobs |
| Cloud Scheduler `cloudscheduler.googleapis.com` | a job's schedule |
| Cloud Monitoring `monitoring.googleapis.com` | usage reads and idle detection |
| IAM Credentials `iamcredentials.googleapis.com` | the delivery service's per-environment tokens |
| Cloud Build `cloudbuild.googleapis.com` | builds of a linked source in the trusted project; 2nd-gen repository connections |
| Container Analysis `containeranalysis.googleapis.com` | the signed SLSA provenance Cloud Build records for each image it pushes |
| OS Login and IAP TCP forwarding | SSH to machines as a service account (`gcloud compute ssh`) |
| Binary Authorization `binaryauthorization.googleapis.com` | the project policy clusters enforce and Cloud Run applies (`providers.gcp.binary_authorization`, on by default) |
| Cloud DNS `dns.googleapis.com` | with `providers.gcp.deny_egress`: the response policy that sends Google's API names to private.googleapis.com |
| Cloud KMS `cloudkms.googleapis.com` | with `providers.gcp.kms_keys`: the operator's keys, used by each service's own agent |

**The infrastructure service's identity.** A keyless, single-purpose service account with roles on the project it
provisions in, and nothing elsewhere. The roles the documents name, by what they are for (shrink later with the IAM
recommender):

| For | Roles and permissions |
|---|---|
| machines, disks, networks, load balancers | `roles/compute.instanceAdmin.v1`, `roles/compute.networkAdmin`, `roles/compute.securityAdmin`; for a cluster's front door also `compute.globalAddresses.{insert,get,delete}`, `compute.sslCertificates.{insert,get,delete}`, `compute.forwardingRules.list` |
| databases | `roles/cloudsql.admin`, `roles/servicenetworking.networksAdmin` |
| buckets | `roles/storage.admin` |
| caches | `roles/redis.admin` |
| queues | `roles/pubsub.editor` |
| secrets | `roles/secretmanager.admin` |
| clusters | `roles/container.admin`: the clusters themselves, and inside them the namespace, RoleBindings, ClusterRole, Gateway and ValidatingAdmissionPolicy it writes. `roles/container.clusterAdmin` gives no right inside a cluster, and IAM has no permission a custom role could hold for admission policies (found on 2026-11-02); the service makes every cluster in this project, so the project-wide role reaches only its own |
| HTTPS load balancers, Cloud Armor, egress control | `compute.sslCertificates.*` and `compute.sslPolicies.*`; `compute.securityPolicies.use` on the operator's `cloud_armor_policy`; `roles/dns.admin` with `deny_egress` |
| image registries | `artifactregistry.repositories.{create,get,delete,getIamPolicy,setIamPolicy}` |
| access grants (release A) | a custom role with exactly `getIamPolicy` and `setIamPolicy` on Compute Engine instances, IAP tunnel instances, secrets, buckets, topics, subscriptions and Artifact Registry repositories, and `iam.roles.get` (the node-account check reads the custom roles the service's account holds) |
| a workload identity per request | nothing beyond the node-account roles below |
| a node account per cluster | `roles/iam.serviceAccountCreator`, `roles/iam.serviceAccountDeleter`, `roles/iam.serviceAccountUser` (the project), `roles/resourcemanager.projectIamAdmin` limited by the IAM condition `api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(['roles/container.defaultNodeServiceAccount'])`, and `roles/artifactregistry.admin` on each repository in `providers.gcp.registries` |
| managed services and jobs | `roles/run.admin`, `roles/iam.serviceAccountAdmin` (limited as the node accounts' role is), `roles/cloudscheduler.admin` |
| usage and health | `roles/monitoring.viewer` |
| the project itself | `roles/browser` (the run of 2026-10-29 gave it), and `serviceusage.services.use` on the quota project when run from a laptop |

Backstops the positioning review and the security assessment of 2026-10-30 recommend as organisation policies on that
project: `compute.requireOsLogin`, `compute.vmExternalIpAccess`, `compute.trustedImageProjects`,
`compute.restrictLoadBalancerCreationForTypes`, `sql.restrictPublicIp`, `sql.restrictAuthorizedNetworks`,
`iam.disableServiceAccountKeyCreation`, `iam.disableServiceAccountKeyUpload`,
`iam.automaticIamGrantsForDefaultServiceAccounts`, `iam.allowedPolicyMemberDomains`, `compute.requireShieldedVm`,
`compute.disableSerialPortAccess`, `compute.skipDefaultNetworkCreation`, `storage.publicAccessPrevention`,
`storage.uniformBucketLevelAccess`; and Data Access audit logs for IAM, STS, Secret Manager, Cloud SQL and Cloud Storage.

**The delivery service's identities.** One service account per environment (dev, staging, prod kept apart), each:

| What | Where |
|---|---|
| a `cloudinfra` principal: `can_provision: false`, `can_destroy: false`, `scope: all`, `identity: serviceAccount:<the account>` | `cloudinfra`'s configuration; its token in the variable `environments.<name>.token_env` names |
| `roles/iam.serviceAccountTokenCreator` for the host the delivery service runs as | on the environment's service account |
| a custom role with `compute.projects.get` and `compute.instances.get` (so gcloud finds a machine) | the project |
| `roles/container.clusterViewer` (cluster targets: to fetch credentials) | the project |
| `roles/artifactregistry.reader` on each repository in `allowed_registries` (service and job targets: Cloud Run checks that the deployer can read the image, found 2026-10-30) | each repository |
| the environment's `pull_service_account`: `roles/artifactregistry.reader` on each repository in `allowed_registries` and nothing else; the delivery service's host holds `roles/iam.serviceAccountTokenCreator` on it, as on the environment's account | each repository, the account |
| `roles/cloudbuild.builds.editor` (builds: create and read) | the project |
| `roles/containeranalysis.occurrences.viewer` (environments with `require_provenance`: read an image's provenance) | the project that holds the images |
| `roles/iam.serviceAccountUser` on the environment's build account (to start builds as it) | the build account |
| everything else: OS Login admin and the IAP tunnel on machines, secret access, `edit` in a cluster's namespace, `run.developer` and Service Account User on a service or job | granted by `cloudinfra` when the team names the account at `admin` in the request's `access` |

**The build accounts.** One user-managed service account per environment, never the environment's own, that Cloud Build
runs builds as (a build always names it; the default build accounts are never used):

| What | Where |
|---|---|
| `roles/logging.logWriter` (builds log to Cloud Logging only) | the project |
| `roles/artifactregistry.writer`: push and tag, never delete | each source's registry: for one a `cloudinfra` request made, the account is listed in `cloudinfra`'s `access.registry_writers` and named at `admin` by the team, and is granted nothing else |

**Sources.** A Cloud Build 2nd-gen connection to the company's Git host in the trusted project (GitHub: the Cloud Build
GitHub App installed on the organisation), one linked repository per source; the host's token is a secret in Secret
Manager there, read by the Cloud Build service agent `service-NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com` with
`roles/secretmanager.admin` on the project, to create that secret and set its policy (found on 2026-10-30: the
documentation names only `secretAccessor`, and creating the connection fails with it). Linking needs `roles/cloudbuild.connectionAdmin` once. The project must belong to
an organisation (a user-specified build account needs it), and `iam.disableCrossProjectServiceAccountUsage` stays
enforced: build account, Cloud Build and registries are in one project.

The `cloudinfra` operator also defines the profile the delivery service needs for machine targets:
`container-host`, image `projects/cos-cloud/global/images/family/cos-133-lts`, `accelerator: none`.

**Network.**

| Need | Value |
|---|---|
| SSH through IAP to a machine | a firewall rule from `35.235.240.0/20` on port 22: a request's own network has it with `iap_ssh: true` (the default); the operator's default network only if `gcp.default_network_iap: true` says so |
| Google's load balancer health checks | `130.211.0.0/22` and `35.191.0.0/16` to the backend port (the adapter opens it) |
| image pulls from a machine without a public address | Private Google Access on the request's subnet (set by the adapter) for Artifact Registry (`*-docker.pkg.dev`); other registries through the network's Cloud NAT, which a request's network has unless the operator denies egress |
| image pulls by cluster nodes | the node account's `roles/artifactregistry.reader` on repositories the operator lists in `providers.gcp.registries` |
| Claude chat | a public HTTPS address reachable from Claude's servers; callbacks `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback` |
| a cluster's front door with HTTPS | hostnames under `providers.gcp.public_domains`, DNS pointed at the reserved address before the Google-managed certificate becomes ACTIVE (not yet run on a real domain) |

### The Azure subscription, before its first real run

Steps 1, 2, 4 and 5 of [research/RESEARCH-2026-11-03-azure.md](research/RESEARCH-2026-11-03-azure.md), section 10:
quota; identity, network, storage and compute; databases, caches, queues, secrets and registries; and clusters,
services, jobs and load balancers. None of it has run
against a real subscription yet; that run comes once every kind is built, and it will turn the role below into the
tested list Google's section 6 is. Section 4.2 of the note is the reasoning.

| Need | Value |
|---|---|
| The subscription | `providers.azure.subscription_id`, in the directory `tenant_id`, in the cloud `cloud` (`public`, `usgovernment`, `china`) |
| Resource providers registered | `Microsoft.Compute`, `Microsoft.Network`, `Microsoft.Storage`, `Microsoft.ManagedIdentity`, `Microsoft.Insights`, `Microsoft.KeyVault`, `Microsoft.ServiceBus`, `Microsoft.DBforPostgreSQL`, `Microsoft.DBforMySQL`, `Microsoft.Cache`, `Microsoft.ContainerRegistry`, `Microsoft.ContainerService`, `Microsoft.App`, `Microsoft.Cdn`, and `Microsoft.MarketplaceOrdering` for a Marketplace image with a plan; registered once by the operator, since the service's role holds no `register/action` |
| The service's role, at the subscription (or the landing-zone scope its groups are made under) | a custom role with `Microsoft.Resources/subscriptions/resourceGroups/*`, `…/resources/read` and `Microsoft.Resources/subscriptions/read` (the tenant), `Microsoft.Compute/virtualMachines/*`, `…/disks/*`, `…/locations/usages/read`, `Microsoft.Network/virtualNetworks/*`, `…/networkSecurityGroups/*`, `…/natGateways/*`, `…/publicIPAddresses/*`, `…/networkInterfaces/*`, `…/privateEndpoints/*`, `…/privateDnsZones/*`, `…/networkWatchers/flowLogs/*`, `…/locations/usages/read`, `Microsoft.Storage/storageAccounts/*`, `Microsoft.ManagedIdentity/userAssignedIdentities/*`, `Microsoft.KeyVault/vaults/*`, `…/locations/deletedVaults/read` and `…/locations/deletedVaults/purge/action`, `Microsoft.ServiceBus/namespaces/*`, `Microsoft.DBforPostgreSQL/flexibleServers/*`, `Microsoft.DBforMySQL/flexibleServers/*`, `Microsoft.Cache/redisEnterprise/*`, `Microsoft.ContainerRegistry/registries/read`, `Microsoft.ContainerService/managedClusters/*` (with `listClusterUserCredential/action`, for the API server's address and CA), `Microsoft.App/managedEnvironments/*`, `Microsoft.App/containerApps/*`, `Microsoft.App/jobs/*`, `Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials/*` and `…/assign/action` (an app, a job and a cluster run as identities it made), `Microsoft.Network/loadBalancers/*`, `Microsoft.Network/networkSecurityGroups/securityRules/*` (a cluster's pod range and a load balancer's port, on the networks it made only), `Microsoft.Network/virtualNetworks/subnets/join/action`, `Microsoft.Cdn/profiles/*`, read and link rights on the operator's Front Door WAF policy when it names one (`Microsoft.Network/frontdoorWebApplicationFirewallPolicies/*` on that policy alone, until the real run narrows it), `Microsoft.Authorization/locks/*`, `Microsoft.Insights/metrics/read`, `Microsoft.Authorization/roleAssignments/read`; and the data actions `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read` (to see that a bucket is empty before deleting it) and `Microsoft.KeyVault/vaults/secrets/setSecret/action`, `…/secrets/delete`, `…/secrets/purge/action` (to write the values it makes and remove them; never `getSecret`, so it reads no secret), and on clusters the Kubernetes data actions `Microsoft.ContainerService/managedClusters/namespaces/read` and `…/write`, `…/admissionregistration.k8s.io/validatingadmissionpolicies/*` and `…/validatingadmissionpolicybindings/*` (the request's namespace and the admission policy; nothing an application puts in a cluster) |
| The service's grant right | Role Based Access Control Administrator (`f58310d9-a9f6-439a-9e8d-f62e7b41a168`) at the same scope, and on the operator's registry, with the condition of section 4.2 of the note naming only Virtual Machine User Login, Virtual Machine Administrator Login, Reader, Storage Blob Data Contributor and Owner, Key Vault Secrets User and Officer, Azure Service Bus Data Sender and Receiver, Container Registry Repository Reader and Writer, Azure Kubernetes Service Cluster User Role, Azure Kubernetes Service RBAC Reader and Writer, Container Apps Operator, Container Apps Jobs Reader and Contributor, and Network Contributor with the principal type ServicePrincipal only (a cluster's own control-plane identity, on its nodes' subnet), and only the principal types User, Group and ServicePrincipal |
| Members by address | Microsoft Graph's `User.Read.All` and `Group.Read.All` application permissions on the service's identity, granted once by a Privileged Role Administrator; without them, members are named by object id (`user:<object id>`), and only a principal's own identity may be. A group is found by its mail address, so a security group without one is named by object id. A principal's identity that is another cloud's account (a Google service account) is recorded as failed on Azure requests, which go on |
| Machines without a network block | `providers.azure.default_subnet`, a subnet the service may join (`Microsoft.Network/virtualNetworks/subnets/join/action`); Azure has no default network, so without it such a request is refused |
| Reaching machines | an Azure Bastion, Standard with native client support, in a network peered with the request networks (`providers.azure.bastion`, and its `AzureBastionSubnet` range as `bastion_subnet`); members need Reader on it, which the platform team grants once. Without one, `az ssh vm` reaches a machine's own address only |
| Sign-in on a machine | the `AADSSHLoginForLinux` extension reaches `login.microsoftonline.com`, `pas.windows.net` and `packages.microsoft.com` on 443, through the request network's NAT or the machine's own address; on a network with neither, or with `deny_egress`, the machine is made without it and its connect block says so (Run Command is then the way in, with a role the platform team grants) |
| Flow logs | optional: `providers.azure.flow_logs_storage_account`, an account in the request's region, and the region's network watcher in `network_watcher_group` (`NetworkWatcherRG`, which Azure makes) |
| Registries | optional: `providers.azure.registry`, the operator's container registry (Standard or Premium) with its role assignment mode `AbacRepositoryPermissions`; the service holds Container Registry Repository Contributor and Repository Catalog Lister on it, to delete a request's repositories, and signs in to it with a token for the registry's own audience (`https://containerregistry.azure.net`). A member holds one assignment of a repository role on it, whose condition names every request prefix it is granted on. Without it, a request may hold no registry |
| Databases and caches | private ones resolve through private DNS zones the service makes in the network's group (each cloud's own zone for databases; caches in the public cloud only, the one Microsoft documents a zone for); a cache takes 10 to 20 minutes to make and the cache's wait is capped by `limits.operation_timeout_seconds`, so an operator that offers caches raises it to 1,800 |
| Clusters | a network of the request (or `network_id`) with a NAT gateway: nodes join its subnet and leave through it; `providers.azure.cluster_authorized_networks`, the ranges where cloudinfra and appdelivery run, admitted to the API server (empty leaves it open behind Entra sign-in); `cluster_registries`, the prefixes of the operator's registry the nodes may pull from. A cluster's front door is refused while Application Gateway for Containers is a preview add-on on AKS |
| Services and jobs | a network of the request (or `network_id` naming one made with a service or job), a `/26` or larger, so that Container Apps' `/27` fits beside the machines' subnet; outbound access (not `deny_egress`); for a service callable only with sign-in, `providers.azure.service_auth_client_id`, an app registration of the operator's in the tenant that callers ask tokens for (`api://<client id>`), with group claims in its tokens when groups are granted; `allow_public_services` for a public one |
| Load balancers | `allow_public_exposure`; for hostnames, `providers.azure.public_domains`, a DNS zone where the operator adds each hostname's TXT record (`_dnsauth.<hostname>`, the token the resource reports) and CNAME (the Front Door endpoint); optionally `front_door_waf_policy`, a Front Door WAF policy (custom rules on Standard) |
| Wait ceilings, each capped by `limits.operation_timeout_seconds` | resource group 300 s (its delete 1,800), network 600, flow log 300, identity 300, disk 600, storage account 600, machine 1,200, sign-in extension 900, attach 600, deallocate or start 900, resize 1,200, delete 1,200, vault 600, namespace 900, database 1,800, cache 1,800, cluster 2,400, Container Apps environment 1,800, app or job 900, Front Door 900 |
| API versions | Compute `2026-04-01` (machines, extensions, usages) and `2026-03-02` (disks); Network `2026-03-01` (with private endpoints) and `2024-06-01` (private DNS zones); Storage `2026-09-01` and the blob service's `2025-01-05`; ManagedIdentity `2024-11-30`; Resources `2025-04-01`; Authorization `2022-04-01` (role assignments) and `2020-05-01` (locks); Insights metrics `2023-10-01`; KeyVault `2026-05-15` and a vault's own API `7.5`; ServiceBus `2026-01-01`; ContainerRegistry `2025-11-01`; DBforPostgreSQL `2026-10-01`; DBforMySQL `2024-12-30`; Cache `2026-09-01`; ContainerService `2026-08-01`; App `2026-07-01`; Cdn `2026-07-01`; Microsoft Graph `v1.0`; the Retail Prices API `2023-01-01-preview`; the instance metadata service `2018-02-01`; the identity endpoint `2019-08-01`; each cloud's metadata document `2022-09-01` |
| Regions and zones | Microsoft's region list of 2026-10-27 (`cloudinfra/providers/azure/regions.py`); others, and every sovereign cloud's, under `providers.azure.zones` |
| Quota | each VM family's vCPUs in the region (a GPU family starts at zero in a new subscription, and is raised by a quota request), the regional vCPUs, and Standard public addresses; the orchestrator checks the family before it makes anything |

## 7. Product versions the adapter offers

| Kind | What a request may ask for | Provider mapping |
|---|---|---|
| identity | `{name, level}`: an account for the request's own workloads, no key ever; `compute.identity` runs a group as it; with clusters, the Kubernetes service account of its name may act as it | an IAM service account `id-<request id>-<name>`, granted on the request's resources at its level; Workload Identity Federation for GKE (`roles/iam.workloadIdentityUser`) |
| compute, `os` | `ubuntu` (24.04 LTS amd64), `debian` (12), `rocky` (9) image families | Compute Engine E2 (`e2-standard`, `e2-highmem`, `e2-highcpu`, `e2-custom`, `e2-micro/small/medium`) |
| compute, `accelerator` | `t4`, `l4`, `a100`, `h100` | `nvidia-tesla-t4` on N1, `nvidia-l4` on G2, `nvidia-tesla-a100` on A2, `nvidia-h100-80gb` on A3; boot disk 50 GB by default, at least 40 GB |
| compute, `profile` | an operator profile, such as `container-host` (Container-Optimized OS 133 LTS; LTS 117 to 133 support OS Login and Shielded VM) or a GPU image with signed drivers | the profile's image family or pinned image |
| storage | `persistent` (`standard`, `balanced`, `ssd`), `object` | `pd-standard`, `pd-balanced`, `pd-ssd`; a STANDARD bucket with uniform access and public-access prevention |
| database | `postgresql` 13, 14, 15, 16, 17, 18 (default 18; 13 flagged as paid extended support); `mysql` 5.7 (flagged), 8.0, 8.4 (default), 9.7; sizes `small`, `medium`, `large` | Cloud SQL `POSTGRES_18`, `MYSQL_8_4`, … on `db-f1-micro`, `db-custom-2-7680`, `db-custom-4-15360`; encrypted-only, backups, deletion protection |
| cache | `redis` 6.2, 7.0, 7.2 (default 7.2) | Memorystore `REDIS_7_2`, `BASIC` or `STANDARD_HA`, AUTH and TLS |
| queue | a topic, optionally a pull subscription | Pub/Sub |
| secret | a container, optionally a generated first version | Secret Manager, automatic replication |
| registry | a Docker repository, optionally immutable tags | Artifact Registry `DOCKER` format at `<region>-docker.pkg.dev/<project>/<name>-<registry>` |
| cluster | nodes, node shape, optional `kubernetes_version`, `registries`, `public.hostnames` | GKE Standard, release channel `REGULAR`, VPC-native, shielded nodes, workload identity, Gateway API `CHANNEL_STANDARD` when public; the admission policy needs Kubernetes 1.30 or later (built in on every current channel) |
| service | CPU 1, 2, 4 or 8 with Cloud Run's memory pairs, instances, concurrency, timeout, `ingress`, `public`, `egress` | Cloud Run Admin API v2; stand-in image `us-docker.pkg.dev/cloudrun/container/hello` |
| job | CPU and memory, tasks, parallelism, retries, task timeout, `egress`, `schedule` | Cloud Run jobs (v2) and Cloud Scheduler v1; stand-in `us-docker.pkg.dev/cloudrun/container/job` |
| load_balancer | `http` or `tcp` | global external managed HTTP(S) load balancer, or a regional TCP one |

Delivery targets: a compute group on Container-Optimized OS (Docker, `--restart=always`, curl health check), a cluster
workload (Deployment with `maxUnavailable: 0`, ClusterIP Service, a Secret per release, an HTTPRoute and
HealthCheckPolicy when exposed), a Cloud Run service (a revision per release, `candidate` tag, traffic moved when ready)
or a Cloud Run job (the job's container, run on demand). Attestations: Cosign `verify-attestation` with a Cloud KMS
key (`gcpkms://…`), a public key file, or a keyless identity and issuer; type `slsaprovenance1` by default.

On Azure, every kind maps as the table "Mapping on Azure" in [PROVIDER-INTERFACE.md](PROVIDER-INTERFACE.md) says: exact
Dsv5, Esv5, Dlsv5 and B sizes, T4, A100 and H100 sizes (no L4), the same three OS families from their publishers'
Marketplace images, Standard HDD, Standard SSD and Premium SSD disks, zone-redundant storage accounts for buckets,
PostgreSQL 14 to 18 and MySQL 8.0 flexible servers, Azure Managed Redis 7.4, Service Bus Standard topics, Key Vault
secrets, repository prefixes in the operator's registry, AKS Standard-tier clusters on Azure Linux 3 (two nodes of four
vCPUs at least), Container Apps on the Consumption profile (1, 2 or 4 vCPUs with 2 GiB each; stand-ins
`mcr.microsoft.com/k8se/quickstart` and `quickstart-jobs`), and Standard load balancers with Front Door Standard for
hostnames.

## 8. Built-in limits and defaults

The infrastructure service's `limits` (the operator may lower or raise each within the bounds in `cloudinfra/config.py`):

| Limit | Default | Bounds |
|---|---|---|
| `max_instances_per_request` | 10 | 1 to 100 |
| `max_cpu_per_instance` | 32 | 1 to 96 |
| `max_memory_gb_per_instance` | 128 | 1 to 640 |
| `max_storage_gb_per_request` | 2000 | 1 to 64000 |
| `max_database_storage_gb` | 1000 | 10 to 10000 |
| `max_accelerators_per_instance` | 8 (0 refuses every GPU) | 0 to 16 |
| `max_cache_memory_gb` | 64 | 1 to 300 |
| `max_cluster_nodes` | 20, counted over every cluster of a request | 1 to 100 |
| `max_monthly_cost_usd` | 500 (from list prices, before any provider call; a principal may carry a tighter one) | 0 or more |
| `max_active_requests_per_principal` | 20 | 1 to 1000 |
| `operation_timeout_seconds` | 900 | 60 to 3600 |
| `default_lifetime_hours`, `max_lifetime_hours` | none (nothing expires unless asked) | 1 to 8760 |
| `expiry_retention_days` | 7 | 0 to 90 |
| `default_autostop_idle_minutes` | none; `default_autostop_scope` `accelerators` | 15 to 1440 |
| `idle_cpu_cores` | 0.5 | more than 0, up to 16 |
| `max_keep_awake_hours` | 72 | 1 to 720 |

Per request, by the contract: at most 8 queues, 16 secrets, 4 registries, 4 clusters, 8 services and 8 jobs; names of
groups, queues, secrets, registries, clusters, services and jobs up to 12 characters; `access` members up to
`access.max_members` (10 by default, 0 to 50).

Per release, by the delivery contract: an image by digest only; port 1 to 65535 except 22; health timeout 5 to 600
seconds (60 by default); up to 64 environment values of up to 4096 characters, one line each, with no credential as a
literal; replicas 1 to 20 on a cluster; a note up to 300 characters.

The Google adapter's defaults since the assessment's release S2: `allow_public_exposure` false, `network_logs` true,
`deny_egress` false, `binary_authorization` true, `cluster_authorized_networks` empty (a cluster with private nodes is
then refused: GKE would let no address outside its network reach the control plane), `kms_keys` empty (Google's own keys), `cloud_armor_policy` none; a request's network has a NAT
(`nat: true`), a cluster private nodes, a Cloud Run service `internal_and_load_balancer` ingress and no secrets.

Server settings, both services: `max_request_bytes` 65,536 (413 above it), `registrations_per_hour` 10 per client address with sign-in on, `rate_limit_per_minute` 120 per identity, `auth_failures_per_minute` 20 per client
address, `workers` 4 (1 to 32), `allowed_hosts` `127.0.0.1:*` and `localhost:*` by default. The infrastructure
service's `sweep_interval_seconds` is 300 (0 turns expiry and idle stop off); a provisioning call may wait up to 45
seconds (`wait_seconds`). Sign-in tokens: access 60 minutes (5 to 1440), refresh 30 days (1 to 90), codes 5 minutes,
one use each. The delivery service's per-environment Google tokens last 15 minutes; a gcloud token is kept 5 minutes.

## 9. Clients

| Client | How it connects |
|---|---|
| Claude chat (claude.ai) | a custom connector on `https://HOST/mcp`; signs in with OAuth only, so the service's `sign_in` block must be on; the person pastes their principal's token on the service's own page |
| Claude Code | `claude mcp add --transport http NAME https://HOST/mcp --header "Authorization: Bearer TOKEN"`, or the same sign-in |
| Any MCP client | MCP Streamable HTTP at `/mcp`, bearer token, `Host` header in `allowed_hosts` |
| Any HTTP client | the REST routes (`docs/MCP-CATALOG.md`, `docs/DELIVERY-CATALOG.md`), JSON in and out, `{"status": "FAILED", "error": {...}}` on every failure |

Known quirk of the MCP SDK 1.30.0 OAuth routes: `POST /revoke` requires a `client_secret` form field even for a public
client; the sign-in page and Claude's client send one.

## 10. Where each number lives, and how to refresh it

| Requirement | Source of truth | Refresh |
|---|---|---|
| Python version, direct dependencies, console scripts | `pyproject.toml` | edit, then regenerate both locks |
| Locked packages and hashes | `requirements.lock`, `requirements-dev.lock`, `audit.lock` | `uv pip compile --generate-hashes pyproject.toml -o requirements.lock`, the same with `--extra dev -o requirements-dev.lock`, and `uv pip compile --generate-hashes audit.in -o audit.lock`; then `python -m pytest -q` and `python -m pip_audit --require-hashes -r …` |
| The Python image digest | both Dockerfiles (must match; a test checks) | `docker buildx imagetools inspect python:3.12-slim`, after review |
| gcloud and Cosign versions and sums | `Dockerfile.appdelivery` | as its header describes; a test checks the shape of every pin |
| Google's key for Cloud Build provenance | `appdelivery/provenance.py` (`GOOGLE_KEYS`, read 2026-10-30) | `gcloud kms keys versions get-public-key VERSION --location global --keyring attestor --key google-hosted-worker --project verified-builder` when a release is refused naming a key id it does not trust |
| CI tools and action commits | `.github/workflows/*.yml`, `.pre-commit-config.yaml` | Dependabot for the actions; hadolint and grype by hand, with the checksum from the release's own checksums file, at least a week old |
| Resource kinds, fields, tools, error codes | `cloudinfra/mcp/catalog.py` and `cloudinfra/models/` → `docs/MCP-CATALOG.md`; `appdelivery/catalog.py` → `docs/DELIVERY-CATALOG.md` | `python -m cloudinfra.cli catalog --write docs/MCP-CATALOG.md`; `app-delivery catalog --write docs/DELIVERY-CATALOG.md`; a test fails while either is stale |
| Limits and defaults | `cloudinfra/config.py`, `cloudinfra/models/request.py`, `appdelivery/config.py`, `appdelivery/models.py` | with the code |
| Google product versions and mappings | `cloudinfra/providers/gcp/translate.py`, `cluster.py`, `run.py`, `settings.py` | with the adapter |
| Roles and APIs | `docs/INFRASTRUCTURE-ARCHITECTURE.md`, `docs/DELIVERY-SPEC.md` section 9, `docs/DELIVERY-SPIKE.md`, `cloudinfra/providers/gcp/api.py`, `iam.py` | when a kind is added or a run on a real project finds a missing one |
| Azure clouds' endpoints and token audiences | `cloudinfra/providers/azure/clouds.py`, compared by a test with `tests/cloudinfra/fixtures/azure-cloud-*.json` | for each cloud, `curl https://<resource manager>/metadata/endpoints?api-version=2022-09-01`, write the fixture, run the tests |
| Azure regions, zones and api-versions | `cloudinfra/providers/azure/regions.py`, `api.py` (`API_VERSIONS`) | with the adapter, from Microsoft's region list and each service's REST reference |
| Azure list prices | `cloudinfra/providers/azure/pricing.py` (approximate, eastus2, `verified` null), or the operator's snapshot | `cloud-infra prices --write FILE` from the Retail Prices API for the allowed regions, then `providers.azure.prices_file: FILE` |
| Versions | `pyproject.toml` and `cloudinfra/__init__.py` (tied by a test); `appdelivery/__init__.py` on its own | with `docs/CHANGES.md` |
