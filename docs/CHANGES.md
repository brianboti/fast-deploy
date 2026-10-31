# Unreleased: fixes from the code review of 2026-10-31

A review of everything releases S1 and S2 changed found eight problems; each is fixed with a test in
`tests/cloudinfra/test_infra_review_fixes.py` or `tests/appdelivery/test_delivery_review_fixes.py`.

- cloudinfra: a create whose answer was lost is looked for up to four times, five seconds apart, since Google may show it
  late; found, it is read until it has settled (a cluster out of PROVISIONING, a Cloud SQL instance out of
  PENDING_CREATE, a machine out of PROVISIONING and STAGING, a disk, a cache, a Cloud Run service or job no longer
  reconciling), so the adapter's next step never meets a resource still being made. A subnet asked of a found network
  Google says is not ready is asked again.
- cloudinfra: `cluster.private_nodes: false` gives every node a public address, and is now refused like every other public
  path while `allow_public_exposure` is off.
- cloudinfra: a resize is priced against the cost ceiling under its request's lock, so two resizes of one request never
  each pass it against the other's old shape.
- cloudinfra: the node-role check (M15) also counts a custom role with `resourcemanager.projects.setIamPolicy` and the
  folder and organization administrator roles bound on the project. A grant through a group or on a folder or the
  organization is not visible to it; docs/SETUP.md says to keep the account out of both.
- Both: logs also redact a credential that base64's `/` splits, and a long run of letters and digits in one case that is
  not hex; digests and hyphenated names stay.
- appdelivery: a build keeps the step it sent on its record, and its provenance is held to that step, not to what this
  release would compose now. A build recorded before this is checked as before, with the unanchored context it was sent
  with before S1 accepted too; a build of an environment since removed fails its provenance instead of the service.
- appdelivery: one team's build order waits only on that team's other orders in the same environment, never on another
  team's call to Cloud Build.

# Unreleased: the security assessment's second release (S2)

Release S2 of the assessment of 2026-10-30 ([research/RESEARCH-2026-10-30-security-assessment.md](research/RESEARCH-2026-10-30-security-assessment.md),
section 7): the provider defaults the CIS benchmarks and Google's hardening guides expect, and the Low findings that
sit in code. Each item has a test in `tests/cloudinfra/test_infra_security_s2.py` or
`tests/appdelivery/test_delivery_security_s2.py`.

**Before you upgrade.** Several defaults close what was open:

- `allow_public_exposure` is off, so a request for a public machine address, an open network or firewall rule, a load
  balancer, a front door, a public database or a Cloud Run service taking all traffic is refused until you turn it on.
- Binary Authorization is on for new clusters and Cloud Run (`providers.gcp.binary_authorization`): enable
  `binaryauthorization.googleapis.com`. The project's default policy allows every image until you write one.
- A Cloud Run service or job reads only the secrets its block names (`secrets: [...]`). Before, it read all of the
  request's. A Cloud Run service's ingress defaults to `internal_and_load_balancer`.
- Cluster nodes are private by default and reach the internet through the request network's new Cloud NAT. A cluster on
  the operator's default network needs a NAT there.
- A machine may run only as an identity at `use`.
- The service refuses to grant the node role on the project if its account could grant any other role. Set the
  condition in docs/SETUP.md section 2.
- `appdelivery`'s `cloudinfra.url` must be `https` unless it is this host.
- Namespaces made before this release keep their labels: Pod Security applies to clusters made from now on.

Security:

- **Pod Security on tenant namespaces, NodePort refused (M6).** `baseline` enforced, `restricted` warned and audited,
  on labels `edit` cannot change; the admission policy refuses a Service of type NodePort beside LoadBalancer, Ingress
  and Gateway.
- **Clusters (M7).** Private nodes by default, Dataplane V2 (NetworkPolicy), the security posture dashboard, Binary
  Authorization enforcing the project's policy, and the operator's `cluster_authorized_networks` for the control plane.
- **Networks (M10).** Flow logs on the subnet (five minutes, half the flows, all metadata) and logging on every rule
  (`network_logs`), a Cloud Router with Cloud NAT (`network.nat`, on by default), and with `deny_egress` egress refused
  except to private ranges and private.googleapis.com, with a DNS response policy for `*.googleapis.com`, `*.pkg.dev`
  and `*.gcr.io`.
- **One switch over every public path (M11)**, `allow_public_exposure`; the example configuration points
  `default_network` at a network the operator made, not Google's `default`.
- **Load balancers (M12).** With `hostnames` in the operator's domains: a managed certificate, an SSL policy (MODERN, TLS
  1.2 at least), HTTPS on 443 and a 301 from 80. Every backend logs its requests; an http backend carries the
  operator's `cloud_armor_policy`.
- **Cloud SQL (M13).** The CIS flags of the engine, IAM database authentication, the operator's KMS key for the region,
  and a default-user password set at creation and never kept.
- **Machines (M14).** No serial port, no project-wide SSH keys, deletion protection lifted only by an explicit delete,
  and no `admin` identity on a machine, whose token every login can read.
- **The node account's project role (M15)** is granted only when the service's own account holds its IAM-admin role
  under the granting condition, checked from the policy before the write.
- **Cloud Run (L9)**: internal and load-balancer ingress by default, Binary Authorization, named secrets only; the
  delivery plan refuses a binding to a secret the runtime account does not read.
- **Storage, secrets, registries (L10).** Buckets state seven days of soft delete; Secret Manager policies are read at
  version 3; registries carry a dry-run cleanup policy for untagged images older than ninety days; one setting,
  `kms_keys`, puts the operator's key on disks, databases, buckets, secrets, registries and a cluster's Secrets and node
  disks.
- **What `appdelivery` reads from `cloudinfra` (L11)** must have the shape of what it names before it reaches gcloud, a
  TLS endpoint or a Google URL; a run's name is checked before it becomes part of a URL.
- **A create Google took but never answered (L8)** is looked up by its planned name, recorded and cleaned up, never left
  an orphan.
- **Keyed token hashes (L7)**, `token_hmac_sha256` with a pepper in `CLOUDINFRA_TOKEN_PEPPER` or
  `APPDELIVERY_TOKEN_PEPPER`; the services will not start without it, and the token commands print the keyed hash.
- **Logs (L6)** redact credential-shaped text in any field; an internal error logs its class, its text only at DEBUG.
- **Sign-in (L4, L20).** A budget per public path and address; `resource` must name this server; the redirect back
  carries `iss` and the metadata says so. SETUP.md and both example files say how to run behind a proxy.
- **One gate, written twice (L13, L14).** The two services' authentication code is the same again (a rate limit is
  `LIMIT_EXCEEDED` in both) and a test fails when they drift; reading what a target runs now is checked like any action.

# Unreleased: the security assessment's first release (S1)

The security assessment of 2026-10-30 ([research/RESEARCH-2026-10-30-security-assessment.md](research/RESEARCH-2026-10-30-security-assessment.md))
measured both services against the MCP specification of 2026-07-28, OAuth 2.1 and RFC 9700, SLSA v1.2, OWASP ASVS 5.0,
CIS GCP and Google's foundations blueprint. Release S1 closes its four High findings and the cheap Medium ones; each
has a test (`tests/cloudinfra/test_infra_security_s1.py`, `tests/appdelivery/test_delivery_security_s1.py`, and the
suites they name).

Security:

- **`appdelivery`: a machine receives a pull-only identity, never the environment's (H1).** Each environment names a
  `pull_service_account` (reader on its registries and nothing else); its five-minute token is minted fresh for the one
  pull. The environment's own token never leaves the service, since root on a team's machine could read it and act as
  the environment on every other team's machines, secrets and services.
- **`appdelivery`: a build context is a path, never a git address (H2).** `github.com/…`, a scheme or `.git` is refused
  and the context is sent anchored (`./web`), so docker never clones another repository in place of the linked one;
  the signed provenance is now held to the one step the service composed (its arguments and tags), as SLSA asks.
- **`cloudinfra`: the tenant namespace is unique per request (H3).** `<name>-<6 hex of the request id>`: a Workload
  Identity principal is project-wide, and two requests of one name no longer share one.
- **`cloudinfra`: 22 and 3389 are never opened from a public range (H4).** By a network block's `allowed_ports` or by
  `configure_firewall`; the operator may close more (`closed_public_ports`).
- **Both: bounded bodies and registration (M1, M2).** 413 above `max_request_bytes` (64 KiB) before anything reads a
  body; the sign-in form takes two small fields; OAuth registration accepts public clients only (no client secret or key
  set), small, `registrations_per_hour` per address, forgotten after a day without a sign-in; the metadata advertises
  `token_endpoint_auth_methods_supported: ["none"]`.
- **Both: single use is atomic, files are private (L1, L2).** A code or sign-in link is taken with one `DELETE …
  RETURNING`; a refresh token exchanged twice at the same moment ends the sign-in; the state file is mode 0600 and the
  services set `umask 077`.
- **`appdelivery`: the OS Login key expires (M3)**, `--ssh-key-expire-after=15m`.
- **`appdelivery`: containers run bounded (M4, M5).** On machines: no privilege escalation, capabilities dropped but
  the ones an entrypoint needs, CPU, memory, process and log bounds; on clusters: the same securityContext, the default
  seccomp profile, resources. Bounds per environment under `workload`.
- **`cloudinfra`: a resize is held to the cost ceiling (M8).**
- **`appdelivery`: a team's builds are bounded (M9)**, `max_concurrent_builds` and `max_builds_per_day` per environment,
  refused with `BUILD_LIMIT_EXCEEDED`.
- **Repository and images (M16, M17 in part).** `audit.lock` regenerated (filelock 4.2.5) and audited in CI with
  `pip.lock`; Dependabot moves the base image digests; `cloudbuild.yaml` names its build account; both images install
  pip 26.2.2 by hash first and carry a `HEALTHCHECK`; `output/`, `.claude/` and the old `infra/` tree are kept out of
  git and the build context.
- **Documents corrected** where they said more than the code did: machines' service accounts, the node account's project
  role, the SSH key's expiry, the token the machine receives, CODEOWNERS' review claim.

# Unreleased: a workload identity with no key

The first of the kinds the coverage research ranks highest (docs/RESEARCH-2026-10-28-infrastructure-layer.md section 7;
docs/INFRASTRUCTURE-ARCHITECTURE.md, "A workload identity"):

- **A thirteenth kind, `identity`.** One or a list (named): an account for the request's workloads, `id-<request id>-<name>`,
  with no key, ever. It holds the request's grants at its `level` (`use` or `admin`) as any access member does, and
  nothing outside the request. Made first, deleted last.
- **Machines run as it**: `compute.identity` names an identity of the same request; a group that names none still runs as
  no account.
- **Pods act as it with no key**: with clusters in the request, the Kubernetes service account of its name in the
  request's namespace gets Workload Identity on it.
- **Tools**: `create_identity`, `get_identity`, `delete_identity` (57 tools).

# Unreleased: images are built only in the trusted project

Decided 2026-10-30, for security: no developer, CI job, local machine or other outside system builds a container image
and uploads it into the trusted Google Cloud project. A team asks for a build; the project fetches the source and builds
it ([research/RESEARCH-2026-10-30-trusted-build.md](research/RESEARCH-2026-10-30-trusted-build.md)).

- **`appdelivery`: builds.** `build_image` (`POST /builds`), `get_build`, `list_builds`: one commit (a full SHA) of a
  source the operator linked to Cloud Build (2nd gen), a Dockerfile and a context in it, and an image name. The service
  composes the build itself (one `docker build` step; the image pushed by Cloud Build with `requestedVerifyOption:
  VERIFIED`; the environment's build account; Cloud Logging only) and follows it in a pool of its own, resumed after a
  restart. The build request takes no step, command, argument, secret, account or registry. Fourteen tools.
- **`appdelivery`: only built images run.** A release whose digest is not the `built` digest of a succeeded build of
  the same team is refused with `NOT_BUILT_HERE` before anything else is read; plans and releases name their
  `build_id`. New codes `INVALID_BUILD`, `NOT_BUILT_HERE`, `BUILD_UNAVAILABLE`, `BUILD_FAILED`.
- **`cloudinfra`: a registry has one kind of writer.** `access.registry_writers` lists the service accounts that build
  images in the trusted project. Named in a request at `admin`, they push (`roles/artifactregistry.writer`, which never
  deletes) and are granted on registries only; every other member of a registry pulls, whatever its level. A CI job's
  federated identity (`principal://`, `principalSet://`) is no longer a member, and `access.workload_identity_pools` is
  refused with a message naming its replacement. The registry's connect block has no push command; the plan warns who
  pushes.
- **Configuration.** `environments.<name>.build` (`service_account`, never the environment's own; `machine_type`,
  `timeout_seconds`, `builder`), `sources` (`repository`, `infra_owner`, `registry`, all in the trusted project), and
  `can_build` on a principal.

Upgrading: a release of an image that was built elsewhere is now refused. Link each source, create the build accounts
(docs/SETUP.md section 4), add `build`, `sources` and `can_build`, and build the images to release. In `cloudinfra`,
replace `access.workload_identity_pools` with `access.registry_writers` and revoke any federated member before
upgrading, since a stored one no longer validates.

Run on Google Cloud on 2026-10-30 (docs/DELIVERY-SPIKE.md section 14): a build by commit succeeded with signed SLSA
provenance naming the GitHub source, and a digest pushed by hand was refused. Making the connection needs the Cloud
Build service agent to hold Secret Manager Admin (docs/SETUP.md). After the run, the release gate verifies provenance too:

- **`require_provenance`** per environment: at rollout the image's Cloud Build provenance is read from Container
  Analysis and must be signed by Google's key for Cloud Build's hosted workers (pinned), with the signed SLSA v1
  statement naming the digest, the builder, the recorded Cloud Build build and its commit. Otherwise
  `PROVENANCE_FAILED`, and no target is touched. Plans say `provenance_required`. The environment's account needs
  `roles/containeranalysis.occurrences.viewer`. Tested against the real answer from the run, kept as a fixture.
- **The platform's own images** are built the same way: `cloudbuild.yaml` builds `Dockerfile.cloudinfra` and
  `Dockerfile.appdelivery` in the trusted project, pushed by Cloud Build with provenance, as a platform build account,
  from the platform's linked repository at a commit (docs/SETUP.md section 3); `.gcloudignore` keeps a local submit to
  the files the Dockerfiles copy. The docker builder is pinned by digest there and as the delivery service's default
  `builder`. A local `docker build` is for development only. Run in the sandbox (docs/DELIVERY-SPIKE.md section 15).

# Unreleased: the repository is the two services, and the documents say so

The Fast Deploy workflow (`labdeploy`), retired on 2026-10-29, is removed with everything that existed only for it: the
package, its tests, Terraform, workflow templates, eleven GitHub workflows, scripts, the operator skill, the schema,
contracts, examples and twenty-four documents. Git history keeps them. The repository is now `cloudinfra` and
`appdelivery`, and is named `infrastructure-layer` in `pyproject.toml`, with the console scripts `cloud-infra` and
`app-delivery`.

- **A twelfth kind, `registry`** (from the `registry-kind` branch, merged here with phase 4): a Docker repository in
  Artifact Registry, made and deleted with its request; `use` pulls, `admin` pushes (narrowed the same day to the
  operator's registry writers, above; the federated CI members first offered here are gone). Tools `create_registry`, `get_registry`,
  `delete_registry`: 54 tools, twelve kinds, `reg-` ids, `endpoints.registry_addresses`.
- **Images.** `Dockerfile` is now `Dockerfile.cloudinfra` and runs the infrastructure service (port 8100,
  `CLOUDINFRA_CONFIG=/config/cloudinfra.yaml`, state under `/data`); `Dockerfile.appdelivery` is unchanged.
- **Documents.** `docs/REQUIREMENTS.md` lists every requirement with its version and where it is set; `docs/SETUP.md`
  is the operator's guide for both services; `docs/DEVELOPMENT.md` the conventions; the research records moved to
  `docs/research/`; `README.md`, `SECURITY.md`, the architecture document and the delivery specification describe the
  two services as they are. `docs/CHANGES.md` keeps 1.35.0 and later.
- **Checks.** CI lints Python, the workflow and both Dockerfiles, audits the two lock files, runs the tests and
  validates `examples/infrastructure-request.yaml` offline; `python-hcl2` left the development extras. The boundary
  test now guards that the old package does not come back.

Upgrading: nothing changes for a caller of either service. An operator who built the old `Dockerfile` builds
`Dockerfile.cloudinfra` instead; its state path and port are new.

# Unreleased: a managed job, Cloud Run jobs, with its schedule

Phase 4, step 3 (docs/DELIVERY-SPEC.md, section 4; docs/INFRASTRUCTURE-ARCHITECTURE.md, "A managed job"):

- **An eleventh kind, `job`.** One or a list (named): CPU and memory (Cloud Run's pairs), tasks, parallelism, retries,
  task timeout, and Direct VPC egress. Made empty with Google's sample job image.
- **Its own runtime account**, which reads the request's secrets and nothing else.
- **`schedule: {cron, time_zone}`** makes a Cloud Scheduler job calling the job's run endpoint as a second account that
  may run this job only. The operator decided a schedule is infrastructure.
- **Grants** as for services: `use` → `run.invoker` (run it); `admin` → also `run.developer` and Service Account User
  on its runtime account.
- **Destroy** takes the schedule away first, then the job, then both accounts; a schedule Google refuses leaves nothing.
- **Tools**: `create_job`, `get_job`, `delete_job` (51 tools).
- **Fixed on a real project** (docs/DELIVERY-SPIKE.md section 13): a Cloud Run service whose newest revision failed while
  an earlier one serves all traffic is READY, not FAILED.

# Unreleased: a managed container service (Cloud Run)

Phase 4, step 1 (docs/DELIVERY-SPEC.md, section 4; docs/INFRASTRUCTURE-ARCHITECTURE.md, "A managed container service"):

- **A tenth kind, `service`.** One or a list (named, as clusters): CPU (1, 2, 4, 8), memory (checked against Cloud Run's
  pairs), minimum and maximum instances, concurrency, request timeout, ingress (`all`, `internal`,
  `internal_and_load_balancer`), `public`, and Direct VPC egress through the request's network. Made empty with Google's
  sample image; the delivery layer puts releases on it.
- **Its own runtime account**, `run-<request id>-<name>`, which reads the request's secrets and nothing else; deleted
  after the service, or at once when Google refuses the service.
- **Public only with the operator's leave**: `providers.gcp.allow_public_services` (off by default) turns Cloud Run's
  invoker check off for a request that asks.
- **Grants**: `use` → `run.invoker`; `admin` → also `run.developer` on the service and Service Account User on its
  runtime account, which deploying a revision needs.
- **Tools**: `create_service`, `get_service`, `delete_service` (48 tools). Priced as warm instances at the idle rate.
- `list_resource_kinds` now marks clusters as several per request too, which phase 3 had left out.

# Unreleased: Claude chat can sign in to cloudinfra and appdelivery

claude.ai adds an MCP server only with OAuth; it cannot send the fixed bearer token both services take. The OAuth
sign-in Claude chat used lived in labdeploy, which is retired, so Claude chat had no way in. Each service can now be its
own small OAuth 2.1 authorization server (`cloudinfra/security/oauth.py`, `appdelivery/oauth.py`, on the MCP SDK's
OAuth routes):

- **Off unless configured.** A `sign_in` block with `url`, the service's public HTTPS address, whose host must be in
  `allowed_hosts`. Without it nothing changes: the OAuth paths stay behind the token gate and the 401 says `Bearer`.
- **The flow.** A 401 names the protected resource metadata; Claude registers itself (only the configured callbacks,
  Claude's by default), the browser opens the service's own sign-in page, the person pastes their principal's token
  there, and Claude gets a one-hour access token and a thirty-day refresh token that stand for that principal, with its
  rights. Calls are audited under the principal's name; `sign_in`, `token_reuse` and `sign_out` are events.
- **How a sign-in ends.** A refresh token works once; using one twice ends the whole sign-in. A principal removed or
  given a new token ends its sign-ins at once. Revocation ends them too. Codes last five minutes and work once.
- **Kept small.** Codes, tokens and pending sign-ins are stored only as SHA-256 hashes in the service's state file; the
  OAuth paths have a per-address budget and wrong tokens on the page the failed-sign-in budget; the page cannot be
  framed and is never cached. Static tokens keep working for systems and Claude Code.

# Unreleased: a cluster's front door

Phase 3, step 5 (docs/DELIVERY-SPEC.md, section 4; docs/INFRASTRUCTURE-ARCHITECTURE.md, "A cluster's front door"):

- **`cluster.public`** with `hostnames`: the cluster gets Gateway API, a reserved global address, a Google-managed
  certificate for the hostnames and one Gateway, `cloudinfra-gateway/public` (`gke-l7-global-external-managed`), serving
  HTTPS and redirecting HTTP, taking routes from the request's namespace only. Without hostnames, plain HTTP on the
  address. Hostnames must be in the operator's `providers.gcp.public_domains`. Also `create_cluster`'s `public` and
  `hostnames`.
- **Routes for `edit`.** Every new cluster gets a ClusterRole aggregated into `edit` that writes HTTPRoutes and GKE's
  HealthCheckPolicies, so a request's namespace attaches its workloads with the health check they need; Gateways stay
  out of `edit`.
- **Reported:** `ClusterResource.public` with the address, the hostnames, the Gateway and the certificate's state.
- **Delete in order.** The Gateway first; the cluster waits until GKE's forwarding rules on the address are gone, then
  goes, then the certificate and the address. A front door that failed half way is closed the same way.
- **Priced:** a forwarding rule per listener. The plan says DNS must point at the address before the certificate is
  ACTIVE.

# Unreleased: a request's namespace cannot open a public address of its own

Phase 3, step 4 (docs/DELIVERY-SPEC.md, section 4). `edit` lets a namespace make a Service of type `LoadBalancer`, an
Ingress or a Gateway, each a public Google load balancer the platform team never sees. Every new cluster now gets a
ValidatingAdmissionPolicy and its binding, `cloudinfra-no-public-entry` (built into Kubernetes since 1.30), that refuse
all three in the namespaces made for requests, which carry the label `cloudinfra-tenant: "true"`; `edit` cannot change a
namespace's labels. The policy is put in before the namespace exists; a cluster that will not take it fails the request
and is deleted. Clusters made before keep no such policy.

# Unreleased: each cluster's nodes run as their own account, and may pull from private registries

Phase 3, step 3 (docs/DELIVERY-SPEC.md, section 4; docs/INFRASTRUCTURE-ARCHITECTURE.md, "A cluster's nodes"):

- **A node account per cluster.** `node-<request id>-<cluster name>`, with `roles/container.defaultNodeServiceAccount`
  on the project and nothing else there; set as the node pool's service account; removed with the cluster, or at once
  when Google refuses the cluster or a grant fails. A grant that IAM refuses because the new account is not known yet
  is tried again for up to a minute.
- **`cluster.registries`.** Artifact Registry repositories (`LOCATION-docker.pkg.dev/PROJECT/REPO`, at most 10) the
  nodes may pull from: the node account gets `roles/artifactregistry.reader` on each. Only repositories the operator
  lists in `providers.gcp.registries`. Also a `create_cluster` parameter.
- **Operator setup.** The service's identity needs new roles, one of them limited by an IAM condition to granting the
  node role only; the table in the architecture document lists them. `providers.gcp.cluster_node_accounts: false`
  keeps the Compute Engine default account and refuses registries.
- `ClusterResource` reports `node_service_account` and `registries`.

# Unreleased: a request may carry several clusters

Phase 3, step 1 of the delivery service (docs/DELIVERY-SPEC.md, section 4). `cluster` takes one cluster or a list, as
`compute`, `queue` and `secret` do:

- **Names.** Each cluster has an optional `name` (12 characters at most, as a group's), part of its provider name
  `<request>-<name>`; `cluster` when omitted, so one cluster is still `<request>-cluster`. Required and unique in a list
  of several; at most 4 clusters per request.
- **Each its own.** Every cluster has its own namespace named after the request and its own grants; destroying the
  request deletes them all.
- **Limits.** `max_cluster_nodes` now counts the nodes of every cluster of the request.

# Unreleased: a resource Google accepted is deleted even when the wait for it fails

Found on 2026-10-29 in a run on Google Kubernetes Engine (docs/DELIVERY-SPIKE.md section 11): Google accepted a
cluster's create, the request then failed while waiting on the operation, and the cluster was saved `FAILED` with no
provider id. Cleanup and destroy act only on resources with one, so the cluster kept running, and billing, after its
request was destroyed, until it was deleted by hand.

- **Known before the wait.** An adapter now calls `ProvisionContext.accepted(resource)` as soon as the provider has taken
  a create, and the orchestrator saves the resource with its provider id before the adapter waits. A failure while
  waiting (an expired token, a timeout, a 5xx) leaves it `FAILED` with its id, and cleanup, or destroy when the operator
  keeps failures, deletes it. A delete that finds nothing passes, as before.
- **Every kind that waits, and every part.** Machine, disk, network, database, cache, cluster and load balancer. A
  network is recorded with its subnet and firewall rules once Google takes its name, and with its reserved services range
  before the peering; a load balancer with each piece as Google takes it; a database before its own database and user
  are made. A queue's topic is recorded before its subscription, a generated secret before its first version.
- **Restart.** A request interrupted while provisioning deletes what it had begun to create along with what it created.
  The error now says that only a resource whose create call had not yet been answered at the restart can exist at the
  provider without an id here.

# Unreleased: clusters take grants, as RoleBindings in one namespace

The first step of the delivery service's phase 2 (docs/DELIVERY-SPEC.md, section 4). IAM's Kubernetes roles cannot be
limited to one cluster, so a cluster's access lives in the cluster:

- **A namespace per cluster**, named after the request, made as soon as the cluster runs (`ClusterResource.namespace`).
- **Clusters take grants.** `use` binds the member to the built-in ClusterRole `view` in that namespace, `admin` to
  `edit`; a level change leaves one binding, a revoke none, and destroying the request needs no revoke. A
  `serviceAccount:` or `user:` member is a `User` named by its email, a `group:` member a `Group` (Google Groups for
  RBAC). A grant the cluster refuses is recorded on the member like any other.
- **The cluster's own API** is called only inside the Google adapter (`providers/gcp/kubernetes.py`), with the service's
  Google token and the cluster's CA certificate (`ClusterResource.ca_certificate`, public).
- The connect block names the namespace; members still need the project-level `container.clusterViewer` to fetch
  credentials, as before.
- **Fixed on a real cluster** (docs/DELIVERY-SPIKE.md section 11): GKE takes the network and subnetwork by name, not
  self link; a token from `gcloud auth print-access-token` is kept 5 minutes, not 30, and a call Google answers with 401
  is tried once more with a fresh one.

# Unreleased: calls to Google are counted against the operator's project and tried again when rate limited

Found while running the delivery service against `cloudinfra` on a laptop (docs/DELIVERY-SPIKE.md section 8): an access
grant failed with HTTP 429 from Resource Manager, because Google counted the calls of a person's gcloud sign-in against
gcloud's own shared project.

- **`providers.gcp.quota_project`.** Optional. When set, every call carries `X-Goog-User-Project`, so Google counts and
  bills it against that project. For a laptop with a person's sign-in; the person needs `serviceusage.services.use` on
  it. A service account on Google Cloud needs neither.
- **Retries.** A call Google rate limited (429, refused before it ran) is tried again up to four times, after Google's
  `Retry-After` or after 1, 2 and 4 seconds, at most 30; a read answered 5xx likewise, a write not (it may have been
  done). Each retry is a `provider_retry` event.
- No version change: the version is tied across three files, one of them `labdeploy`'s, which is not to be touched.

# 1.37.1: what the review of release A found

A review of 1.37.0 read the change from ten angles; a separate verifier checked every candidate against the code and the
fake Google Cloud, and a last pass looked for what the first had missed. It found 43 defects. All are fixed here, each with
a test that fails on 1.37.0 (`tests/cloudinfra/test_infra_release_a_review.py`), and so is one more found while testing them.

- **Permissions.** Every lifetime ends in a destroy, so a request that sets one now needs `can_destroy`: a caller without
  it could have its requests, databases included, destroyed by the sweeper. An expiry the operator sets still applies to
  every caller.
- **Credential secrets.** They are named `<name>-db-password-<6 hex>` and `<name>-cache-auth-<6 hex>`: secret ids are
  unique in a project and request names are not, so a second database or cache of the same name failed. `delete_database`
  and `delete_cache` take the secret with them, so the request ends (it stayed live and counted against
  `max_active_requests_per_principal`). A secret whose value could not be written is cleaned up or destroyed instead of
  outliving the request. A database is refused when the operator's settings let the service grant access to no one and
  the password is not returned, since nobody could then read it; the plan says when nobody has been granted the secret
  yet, and when the creating response carries the password.
- **Access.** A principal's own identity may always be granted, in any domain. A member of a request where nothing has a
  policy of its own (disks only) is `RECORDED`, not `GRANTED`, and the plan says the members are recorded only; a grant
  that met a resource still being created says so. A level change grants the new roles wherever the member had access,
  failed resources included. A revoke reaches what a failed request or a failed destroy left behind, and a revoke that
  fails is a failed operation. Grants recorded on a resource are kept when the provider's view of it is saved after a
  resize, a refresh, a stop or an attach. A single delete takes the resource off the access list; the last one revokes it.
- **Stop, start, resize and delete.** A request is STOPPED only when nothing in it that can stop still runs: a start that
  failed on an idle-stopped machine marked a running request STOPPED, and idle stop and expiry then passed it over. Stop,
  start, destroy, a resize and a single delete wait for each other (`INVALID_STATE`, reason `changing`), and work that
  finishes late never overwrites a status set meanwhile. A delete or a resize the workers refuse at shutdown leaves the
  resource as it was. A suspended machine is resumed rather than started. `resize_compute`'s contract says a stopped
  machine stays stopped.
- **The sweeper.** A sweep reports what it started (`stopping`, `destroying`, `idle_stopping`) and the expired requests it
  keeps (`kept`): its stops and destroys run in the workers, and a destroy that failed was reported as done. A failed
  expiry destroy is tried again; while one is refused, what can stop is stopped; a request that failed, or whose destroy
  failed, is destroyed at its expiry. Every failed attempt waits longer before the next (5 minutes, doubling, at most a
  day) instead of adding a failed operation at every sweep. A request with nothing that can stop is kept until its
  retention ends without saying it stopped. Idle machines are stopped in the workers, each on its own: the sweeper no
  longer holds a request's lock while Google stops a machine, and one machine that cannot be measured or stopped no
  longer keeps the others running. Idle samples are the machine's own, by its numeric id, not those of a namesake in
  another zone. With `sweep_interval_seconds: 0` requests may not set a lifetime or idle stop, and the operator's time
  defaults are refused at start.
- **Restarts and processes.** The server holds a lock on `<database>.lock`: a second process on the same state file
  refuses to start instead of recovering the first's work as interrupted. Shutdown lets work under way finish before the
  lock goes. What an interrupted provisioning created is deleted at the next start, as after any failure. The table of
  per-request locks forgets requests nobody is working on.
- **Validation and cost.** The per-request disk limit counts the boot disk a profile gives. `create_compute` refuses 0 for
  `lifetime_hours` and `autostop_idle_minutes` instead of reading it as none. A GPU machine on a profile that is not a GPU
  profile (`accelerator: required`) boots with Secure Boot off, so that its driver can load. The boot-disk warning names a
  size only when one was asked. `get_resource_cost` prices the resources as they exist (after a resize, for requests an
  earlier release stored, after the operator's profiles change), each stopped one at its stopped rate.
- **The server.** The REST routes run in the thread pool, the older POST routes too, so a slow call no longer holds the
  event loop that serves every other caller. A body that is not JSON is `INVALID_REQUEST` with an audit event, not a 500.
- **Connect blocks and the command line.** A connect block names another request's network or machine that a resource
  uses. The IAP prerequisite says forwarded ports need a firewall rule too. Offline `validate` and `plan` accept access
  members, saying they are not checked.
- **Tests.** The fake Google Cloud refuses an existing secret id, honours the monitoring filter, and resumes a suspended
  instance; the audit-trail test now provisions a cache and a database, where the old one could not fail.

Upgrading: a 1.37.0 configuration with no `access.allowed_domains` and no principal identity can no longer create
databases until the operator sets one of those, or `return_database_password`. Callers reading the sweeper's results, the
credential secrets' names, or a grant's status (`RECORDED`) see the changes above; a caller that sets a lifetime needs
`can_destroy`.

Found while testing, older than release A: `resize_compute` on a machine with GPUs picked an E2 machine type, which cannot
carry GPUs, so the resize could only fail, after the machine had been stopped. A resize now keeps the machine's GPUs and their
family, chosen as at creation (an L4 machine asked for 8 CPUs and 64 GB becomes `g2-standard-16`, the smallest G2 shape
that covers it), and a shape the family cannot offer is refused before the machine is touched.

# 1.37.0: release A, so that a developer can use what the service creates

`docs/research/RESEARCH-2026-10-28-positioning.md` (added in this release) placed the service against the infrastructure products
companies use and ordered the work into three releases. Release A closes the gaps between "the machine exists" and "a
developer is working on it": who can reach it, how, with what software, and for how long. Nothing here stops or expires
on its own unless a request or the operator asks for it.

- **Access.** A request takes `access`: people, groups or service accounts (`user:`, `group:`, `serviceAccount:`) at
  level `use` or `admin`. The requester is added as `admin` when the operator maps its principal to an `identity`. Grants
  go on each created resource's own IAM policy, never on the project: OS Login on machines and the IAP tunnel bound to the
  machine's numeric id, secret access, bucket object roles, Pub/Sub publisher and subscriber. Members are limited to the
  operator's `access.allowed_domains` and `access.max_members` (`INVALID_ACCESS` otherwise). A grant that fails is the
  member's (`FAILED`, `PARTIAL`, with the error), not the request's. `manage_infrastructure_access` grants, changes a level
  (removing the old roles first) or revokes later; destroy takes members out of the tunnel policy.
- **Connect blocks.** Every READY or STOPPED resource carries `connect`: the commands to reach it (gcloud ssh and port
  forwarding through IAP, psql or mysql, redis-cli with TLS, bucket, queue and cluster commands), the prerequisites the
  platform team grants once at project level, and notes. Commands name secrets, never their values.
- **Credentials in secrets the request owns.** Changed: the generated database password is no longer returned in the
  creating response. It is written to the request's `<name>-db-password` secret, and the cache's AUTH string is copied into
  `<name>-cache-auth`; `credentials` names both secrets, and access members read them. An operator can have the password
  returned once as before with `return_database_password: true`.
- **Software profiles.** The operator defines images (`providers.gcp.profiles`): an image family or a pinned image, Secure
  Boot, a minimum boot disk, and whether GPUs are required, optional or refused; `compute.profile` names one instead of
  `os`, and `default_accelerator_profile` is what GPU machines get when they name neither. A GPU image with signed drivers
  (built with Google's `cuda_installer.pyz build_image`) keeps Secure Boot on; a plain image on a GPU machine still turns
  it off and the plan says why. `list_resource_kinds` and `get_service_status` list the profiles.
- **GPU boot disk.** Machines with GPUs get a 50 GB boot disk unless they ask, and at least 40 GB (Google's minimum for
  drivers and a toolkit); less is refused with `INVALID_COMPUTE_CONFIGURATION`.
- **Stop and start.** `control_infrastructure` stops a READY request's machines and databases (Cloud SQL through its
  activation policy), machines before their database, and starts them in the reverse order. New statuses `STOPPING`,
  `STOPPED`, `STARTING`. The `lifecycle` block lists what keeps billing while stopped, and `get_resource_cost` gives
  `current_monthly_usd` at the stopped rate (`stopped_monthly_usd` is on every estimate).
- **Maximum run time.** `compute.max_run_hours` sets Google's `maxRunDuration` with `instanceTerminationAction: STOP`.
- **Expiry.** `lifetime: {hours, on_expiry}` on a request, or the operator's `limits.default_lifetime_hours` (none by
  default), capped by `limits.max_lifetime_hours`, which also applies to a request that sets none when there is no default.
  Warnings 48 and 2 hours before, in the audit log and in every answer; at expiry the request stops and keeps its data for
  `limits.expiry_retention_days` (7) before it is destroyed, or is destroyed at once with `on_expiry: destroy` (or a
  retention of 0). `control_infrastructure extend` adds hours, once a stop or start in progress has finished;
  `list_infrastructure` takes `expiring_within_hours` and lists running or stopped requests only.
- **Idle stop.** `compute.autostop: {idle_minutes}`, or the operator's `limits.default_autostop_idle_minutes` for machines
  with GPUs (or all). A machine stops when its busiest CPU sample in the window used fewer than `limits.idle_cpu_cores`
  cores, at least one window after it started; `control_infrastructure keep_awake` suspends it.
- **The sweeper.** A background thread (every `sweep_interval_seconds`, 300; 0 turns it off) applies expiry and idle stop
  as its own principal, `cloudinfra-sweeper`; each action is an operation and an audit event.
- **Safety under concurrency and restarts.** An independent review of this release found eleven defects before it was
  committed; all are fixed and each has a test.
  - Changes to one request (its status, lifecycle and access list) are serialized in the process. Simultaneous grants
    no longer lose a member, and the sweeper decides on a request as it is when it acts, so an extension or a keep-awake
    that lands during a sweep holds.
  - A revoke or a lower level reaches every resource that may still exist, whatever state a failed refresh or a resize
    left it in. Members compare without regard to case and are stored in lowercase.
  - A stop refuses a machine or database in the middle of a change, tries those whose state cannot be read, and marks a
    request STOPPED only when everything that can stop did. A resize leaves a stopped machine stopped. Idle stop looks
    again at a machine after a failed refresh.
  - At start the service puts a request a restart left STOPPING or STARTING back to READY or STOPPED, as the provider
    sees it. A request left PROVISIONING becomes FAILED, and one left DESTROYING becomes DESTROY_FAILED, so it can be
    destroyed again. Operations still RUNNING are marked interrupted. Work refused during shutdown leaves the request as
    it was.
- **Interfaces.** Two tools (45 in all): `control_infrastructure` and `manage_infrastructure_access`; `create_compute` takes
  `profile`, `autostop_idle_minutes`, `max_run_hours` and `lifetime_hours`. REST: `POST /infrastructure/{id}/actions`,
  `POST /infrastructure/{id}/access`, and `expiring_within_hours` on `GET /infrastructure`. A wrong REST body for these is
  `INVALID_REQUEST` naming the fields it takes.
- **State.** Requests gain `expires_at`, a lifecycle record and an access list; an existing state file gains the columns
  when the service opens it.
- **Corrections.** Idle measurement takes the busiest sample, not the mean of the points. The catalog header names every
  resource id prefix. The 2026-10-28 review no longer says claude.ai custom connectors cannot send static headers (a beta
  for some organizations does, with one credential for the whole organization), and it no longer assumes the service is for
  a university lab: the audience is companies.

Not done here: running release A against a real project, which needs the company's project, its domains and a decision on
the service identity's custom role (the `getIamPolicy` and `setIamPolicy` permissions it needs are listed in
`docs/INFRASTRUCTURE-ARCHITECTURE.md`).

# 1.36.1: the first corrections from the review of 2026-10-28

`docs/research/RESEARCH-2026-10-28-infrastructure-layer.md` reviewed the infrastructure layer against the MCP specification, industry
patterns, the literature and Google's documentation. This release fixes what it found wrong today; the larger changes it
ranks (idempotency keys, live prices, a two-step destroy, drift detection, the next kinds) follow in their own releases.

- A tcp load balancer's health-check firewall rule now allows the ranges Google's passthrough probes come from
  (35.191.0.0/16, 209.85.204.0/22, 209.85.152.0/22); it allowed the application load balancer's ranges, so a tcp load
  balancer's backends never passed their health check. The http rule is unchanged.
- Machines with a GPU are created with Secure Boot off (vTPM and integrity monitoring stay on): Google's driver installation
  does not work with Secure Boot on, so every GPU machine the service created could not get its drivers.
- A FAILED tool result now carries the MCP execution-error signal (`isError: true`) with the same structured payload and the
  same JSON as text, as the specification asks; an invalid request reported by `validate_infrastructure_request` stays an
  ordinary result. The REST API is unchanged.
- Cloud SQL versions follow the provider's current offer: PostgreSQL 18 (now the default) and MySQL 9.7 added, MySQL 8.4 the
  default; a plan warns when the version asked for (PostgreSQL 13, MySQL 5.7) is one the provider keeps only under paid
  extended support.
- Every tool's description, as the model reads it, now carries the purpose, what comes back, the safety rule and what to
  call next (at least three sentences), composed from the catalog's columns; the catalog document is unchanged.
- The architecture diagram said 27 tools; it says 43.

# 1.36.0: five more kinds of infrastructure, GPUs, compute groups, and a catalog that explains itself

The infrastructure layer provided four kinds in fixed shapes. A caller who asked for a load balancer, a GPU, a queue, a
secret, a Kubernetes cluster or a cache was refused with an unspecific error, and two different machine groups needed two
requests. This release widens the contract to nine kinds and makes it discoverable, with the same rules as before: typed
blocks, no provider structures, every creation priced, validated, labelled and destroyable.

- Compute groups: `compute` takes one block or a list of groups, each with a `group` name that becomes part of the machine
  names (`<name>-<group>-<n>`); `storage.group` attaches disks to one group; `load_balancer.backend_group` picks the group it
  fronts. A single block keeps the old names.
- Accelerators: `compute.accelerator` (`t4`, `l4`, `a100`, `h100` and a count). The machine family follows the GPU type on
  Google Cloud (N1 custom or standard for T4, G2 for L4, A2 for A100, A3 for H100); fixed-shape families give the smallest
  shape that covers the request, and the plan shows what was chosen. GPU machines are set to terminate on host maintenance.
  Drivers are the Application Layer's. `limits.max_accelerators_per_instance` (0 refuses them all) is cost protection.
- `load_balancer`: an external application load balancer (`http`, with a health check on a path) or a passthrough network
  load balancer (`tcp`) in front of a compute group, or of machines created earlier (`backends`). Google's health checkers
  are let through the firewall on the backend port; every piece is recorded on the resource and torn down in reverse.
- `cache`: Memorystore for Redis on the request's network (or `network_id`): private, AUTH on, TLS, basic or
  high-availability tier. The AUTH string never passes through this service; the Application Layer reads it with its own
  identity.
- `queue`: a Pub/Sub topic with, by default, one pull subscription (retention, acknowledgement deadline); several per request.
- `secret`: a Secret Manager container, optionally with a generated random first version that is written to the provider
  and never returned, logged or kept; several per request.
- `cluster`: a zonal Kubernetes Engine cluster with one node pool (count, node shape, disk, optional version, optional
  private nodes), VPC-native, shielded nodes, workload identity, regular release channel; credentials come from the
  provider's IAM, never from this service.
- Discoverable: `list_resource_kinds` returns every kind with its fields, whether several fit in one request and which
  adapters provide it; an unknown block is refused with `UNSUPPORTED_RESOURCE_KIND` and the nearest kind ("loadbalancer:
  did you mean load_balancer?"); an adapter that lacks a kind refuses it with the same code before any call.
- The catalog grows to 43 tools: create, get and delete for each new kind, `list_resource_kinds`, and `accelerator` inputs on
  `create_compute`. Creation order is network, storage, compute, database, cache, queue, secret, cluster, load balancer;
  destruction is the reverse. Four new error codes: `INVALID_CACHE_CONFIGURATION`, `INVALID_CLUSTER_CONFIGURATION`,
  `INVALID_LOAD_BALANCER_CONFIGURATION`, `UNSUPPORTED_RESOURCE_KIND`.
- Limits: `max_accelerators_per_instance`, `max_cache_memory_gb`, `max_cluster_nodes`; the instance limit counts every group.
- Tests: the kinds against the fake Google API (bodies, teardown), a request of every kind provisioned in order and destroyed
  in reverse, a failure in the middle cleaning up eleven resources, the primitives composing through `network_id` and
  `backends`, the kinds listing, and the unknown-kind refusals.

Upgrade: nothing to do; existing requests and resource names are unchanged, except that disks attached to machines are now
named `<name>-<group>-disk-<n>` (`<name>-vm-disk-1` for a single block).

# 1.35.0: a provider-independent Cloud Infrastructure MCP, and Fast Deploy as the workflow on top of it

The repository now holds two layers with one direction of dependency. `cloudinfra` is the infrastructure layer: an
Application Layer asks for what infrastructure it needs (machines by CPU and memory, disks and buckets by size, a private
network, a managed database) in a provider-neutral request, and the service validates it, plans it, provisions it through
a provider adapter and reports resource ids, addresses, endpoints and status. It takes no repository, image, script, command
or key, holds no GitHub credential, and decides nothing about an application. `labdeploy` (Fast Deploy) is unchanged in
behaviour and becomes the optional application delivery workflow: repositories, builds, tests, images, releases and the
Cloud Run specifics. The analysis behind the split is `docs/research/REFACTOR-2026-10-28.md`.

- A typed contract (`cloudinfra/models`): `InfrastructureRequest` with `ComputeRequest`, `StorageRequest`, `NetworkRequest`
  and `DatabaseRequest`; `InfrastructureResponse` with resources by kind, endpoints, operations, a cost estimate and a
  structured error; strict models that refuse unknown fields; every failure is `{"status": "FAILED", "error": {code,
  message, details}}` with one of 23 codes.
- `CloudProvider` (`cloudinfra/providers/base.py`): the primitive interface every adapter implements; `GCPProvider` behind
  it, calling Compute Engine, Cloud SQL, Cloud Storage, Service Networking and Cloud Monitoring through one REST client.
  `aws`, `azure` and `jetstream` are refused with `UNSUPPORTED_PROVIDER` until their adapters exist; adding one changes
  nothing above the registry.
- An orchestrator (`cloudinfra/orchestration`): validate, plan, provision in dependency order in bounded workers, status
  refreshed from the provider, destroy in reverse; a failure deletes what the request created and names what it could
  not; quota checks, limits on counts and sizes, a monthly cost ceiling per service and per caller, a cap on live requests.
- An MCP service catalog of 27 primitive tools in six groups (compute, storage, network, database, infrastructure
  orchestration, operations), each with a safety hint, documented in `docs/MCP-CATALOG.md`, which is generated from the
  code and held equal to the registered tools by a test. A small REST API mirrors the lifecycle tools.
- Security: bearer principals with hashed tokens, `can_provision`, `can_destroy` and `scope`; rate limits; the provider
  credential from the host only (`GoogleToken`, moved from `labdeploy/cloud.py` to `cloudinfra/providers/gcp/auth.py` and
  re-exported); OS Login and no service account on machines, SSH from the identity-aware proxy only, private subnets only,
  encrypted-only databases with deletion protection and backups, buckets never emptied; a database password returned once
  and never stored or logged.
- Observability: one JSON line per lifecycle event with request and operation ids, secrets redacted; an audit event per
  call with identity, client, outcome and duration.
- Tests at the boundary (`tests/cloudinfra`, 100+ cases against a fake Google API): request validation and every code,
  provider selection, generic request to Google translation, provisioning order, failure with cleanup, destroy, status,
  authorization, the server end to end, the catalog document, the import boundary, and the design test: one Ubuntu
  machine with 4 CPUs and 16 GB from a request that names nothing about an application. Real-cloud tests live in
  `tests/integration/cloudinfra` and run only with `CLOUDINFRA_INTEGRATION=1`.
- Documents: `docs/INFRASTRUCTURE-ARCHITECTURE.md` (the two layers and what each owns, lifecycle, state, security,
  observability, extension), `docs/MCP-CATALOG.md`, `docs/PROVIDER-INTERFACE.md`; `cloudinfra.example.yaml`; the
  `cloud-infra` command (`token`, `catalog`, `validate`, `plan`).

Upgrade: nothing to do for Fast Deploy installations; `labdeploy.cloud.GoogleToken` still imports. To run the infrastructure
service, copy `cloudinfra.example.yaml`, add a principal from `cloud-infra token`, and start
`uvicorn cloudinfra.mcp.server:create_app --factory` with `CLOUDINFRA_CONFIG` pointing at the file.

Earlier versions (Fast Deploy 0.1.0 to 1.34.0, the release controller this repository began as) are in git history: the
workflow was removed on 2026-10-30, and its changelog went with it.
