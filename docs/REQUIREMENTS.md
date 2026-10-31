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
packaging 26.3, pluggy 1.6.0 and pygments 2.21.0 to the list above):

| Package | Version | Used for |
|---|---|---|
| `pytest` | 9.1.1 | The test suite (`tests/`), 611 tests on 2026-10-30, none touching a cloud |
| `ruff` | 0.17.0 | Lint: correctness, imports, bug-prone patterns, modern syntax and the bandit security rules, line length 200 |
| `zizmor` | 1.30.1 | Lint of the GitHub workflow and the Dependabot file |

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
| Infrastructure service | `Dockerfile.cloudinfra` | `python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1` (resolved 2026-10-11) | nothing but the package | 8100 | `uvicorn cloudinfra.mcp.server:create_app --factory` |
| Delivery service | `Dockerfile.appdelivery` | the same digest | gcloud CLI, Cosign, `openssh-client`, `ca-certificates` | 8200 | `uvicorn appdelivery.server:create_app --factory` |

Pinned inputs of the delivery image, each checked by SHA-256 at build time:

| Input | Version | SHA-256 (amd64) | SHA-256 (arm64) | Source |
|---|---|---|---|---|
| Google Cloud CLI | 591.0.0 | `78c570f5fcfe58a9311bd98c06b75530595e3cae6277aad75a676a39e6ee7b63` | `f5e5df0433f77c0e7f5df9fd4bce2e46c7b7967e79cf66e31b1164d3d4c86546` | `storage.googleapis.com/cloud-sdk-release/google-cloud-cli-591.0.0-linux-{x86_64,arm}.tar.gz` |
| Cosign | v3.1.3 | `4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71` | `c5d324e091826b0d7a78eb16fef316450b4eb9aaec045611c08ba06f5e73220a` | `github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-{amd64,arm64}` |

The gcloud sums were derived on 2026-10-29 as the Dockerfile's header describes (Google publishes a SHA-256 only for the
unversioned archive); repeat that check when moving the version. The build needs `--build-arg TARGETARCH=amd64|arm64`
on a builder without BuildKit (Cloud Build's docker step). The delivery image sets
`CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1`, `CLOUDSDK_CONFIG=/data/gcloud` and `HOME=/data`.

## 4. Continuous integration and local checks

| Tool | Version | Pinned how | Runs |
|---|---|---|---|
| GitHub Actions runner | `ubuntu-24.04` | label | `.github/workflows/ci.yml`, on push, pull request and weekly (Monday 06:19 UTC) |
| `actions/checkout` | v7.0.1 | commit `3d3c42e5aac5ba805825da76410c181273ba90b1` | |
| `actions/setup-python` | v7.1.0 | commit `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` (Dependabot moves it) | Python 3.12 |
| ruff | 0.17.0 | `requirements-dev.lock` | `python -m ruff check .` |
| zizmor | 1.30.1 | `requirements-dev.lock` | `zizmor .github/workflows .github/dependabot.yml` |
| hadolint | v2.15.1 | downloaded by version, SHA-256 `c7187db94eeeeca956519a6af171adc31453941a1e777961f6e680f697c8c507` | both Dockerfiles, failing on errors |
| pip-audit | 2.10.1 | `audit.lock` | every lock: `requirements.lock`, `requirements-dev.lock`, `audit.lock` itself and `pip.lock` |
| pip | 26.2.2 | `pip.lock` (from `pip.in`) | installed first in both images, by hash: the base image's pip is older than the 2026 advisories fixed in 26.x |
| pytest | 9.1.1 | `requirements-dev.lock` | `python -m pytest -q` |
| `cloud-infra validate` and `plan` | the package | | `examples/infrastructure-request.yaml`, offline |
| Dependabot | | `.github/dependabot.yml` | weekly, the workflow's actions and the Dockerfiles' base image digest, releases at least 7 days old; Dependabot alerts are expected to be on in the repository's settings |
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
| clusters | `roles/container.clusterAdmin` (which also lets it create namespaces, RoleBindings and policies in the clusters it made) |
| HTTPS load balancers, Cloud Armor, egress control | `compute.sslCertificates.*` and `compute.sslPolicies.*`; `compute.securityPolicies.use` on the operator's `cloud_armor_policy`; `roles/dns.admin` with `deny_egress` |
| image registries | `artifactregistry.repositories.{create,get,delete,getIamPolicy,setIamPolicy}` |
| access grants (release A) | a custom role with exactly `getIamPolicy` and `setIamPolicy` on Compute Engine instances, IAP tunnel instances, secrets, buckets, topics, subscriptions and Artifact Registry repositories |
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
`deny_egress` false, `binary_authorization` true, `cluster_authorized_networks` empty (the control plane open to any
address, behind IAM), `kms_keys` empty (Google's own keys), `cloud_armor_policy` none; a request's network has a NAT
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
| CI tools and action commits | `.github/workflows/ci.yml`, `.pre-commit-config.yaml` | Dependabot for the actions; the rest by hand |
| Resource kinds, fields, tools, error codes | `cloudinfra/mcp/catalog.py` and `cloudinfra/models/` → `docs/MCP-CATALOG.md`; `appdelivery/catalog.py` → `docs/DELIVERY-CATALOG.md` | `python -m cloudinfra.cli catalog --write docs/MCP-CATALOG.md`; `app-delivery catalog --write docs/DELIVERY-CATALOG.md`; a test fails while either is stale |
| Limits and defaults | `cloudinfra/config.py`, `cloudinfra/models/request.py`, `appdelivery/config.py`, `appdelivery/models.py` | with the code |
| Google product versions and mappings | `cloudinfra/providers/gcp/translate.py`, `cluster.py`, `run.py`, `settings.py` | with the adapter |
| Roles and APIs | `docs/INFRASTRUCTURE-ARCHITECTURE.md`, `docs/DELIVERY-SPEC.md` section 9, `docs/DELIVERY-SPIKE.md`, `cloudinfra/providers/gcp/api.py`, `iam.py` | when a kind is added or a run on a real project finds a missing one |
| Versions | `pyproject.toml` and `cloudinfra/__init__.py` (tied by a test); `appdelivery/__init__.py` on its own | with `docs/CHANGES.md` |
