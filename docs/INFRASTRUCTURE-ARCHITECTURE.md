# Cloud Infrastructure MCP: architecture and boundaries

The repository holds two layers. `cloudinfra` is the **Infrastructure Layer**: a provider-independent service that
provisions, inspects, monitors and destroys cloud infrastructure on request. `labdeploy` (Fast Deploy) is an optional
**application delivery workflow** that sits on top: it owns repositories, builds, tests, images and releases, and may use
the infrastructure layer. The infrastructure layer knows nothing about it. This document describes the infrastructure
layer; [ARCHITECTURE.md](ARCHITECTURE.md) describes the delivery workflow; [REFACTOR-2026-10-28.md](REFACTOR-2026-10-28.md)
records why the split was made this way.

```
Application Layer            any system: Claude, a CI job, another service, the Fast Deploy workflow
  │
  │  Infrastructure contract  InfrastructureRequest → InfrastructureResponse (cloudinfra/models)
  │                           MCP (Streamable HTTP, 45 tools) or REST, bearer token per caller
  ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Cloud Infrastructure MCP  (cloudinfra)                                             │
│                                                                                    │
│  mcp/server.py       FastAPI + StrictMCP: auth gate, rate limits, audit, hints     │
│  mcp/catalog.py      the service catalog: one record per tool, renders the doc     │
│  mcp/tools/*         compute · storage · network · database · infrastructure · ops │
│  orchestration/      validator → planner → provisioner (status, destroy, cleanup)  │
│                      access (grants) · lifecycle (time) · sweeper (expiry, idle)   │
│  state/              SQLite: requests, resources, operations, events               │
│  security/           principals and token hashes · request value validation        │
│  observability/      JSON events with request_id and operation_id, redaction       │
│  providers/base.py   CloudProvider: the primitive interface                        │
└──────────────────────────────────────┬─────────────────────────────────────────────┘
                                       │  CloudProvider
                 ┌─────────────────────┼─────────────────────┬──────────────────────┐
                 ▼                     ▼                     ▼                      ▼
       providers/gcp/            AWSProvider           AzureProvider        JetstreamProvider
       GCPProvider               (later)               (later)              (later)
                 │  REST: compute, sqladmin, storage, servicenetworking, redis, pubsub, secretmanager, container, iap, monitoring
                 ▼
            Google Cloud
```

## What each layer owns

| APPLICATION LAYER OWNS | INFRASTRUCTURE LAYER OWNS |
|---|---|
| application source code | compute provisioning (virtual machines, in groups, with or without GPUs) |
| application testing | network provisioning (networks, subnets, firewall rules, load balancers) |
| application builds | storage provisioning (block disks, object buckets) |
| Docker images and other application artifacts | managed data services: databases, caches, queues, secret containers |
| application release decisions and versions | managed Kubernetes clusters (the control plane and node pool, not what runs on them) |
| application-level scaling behaviour | the cloud resource lifecycle: validate, plan, provision, query, destroy |
| application-level clustering and sessions | infrastructure status, health and usage |
| application configuration, secret values, GPU drivers on a plain image, cluster workloads | provider abstraction, the request/response contract, cloud authentication |
| the GitHub workflow, if any | infrastructure cleanup, on failure and on request |
| who works on the application | access to what it created: grants on each resource (never on the project) and how to connect |
| when and how long the application runs | time on what it created: stop and start, idle stop, maximum run time, expiry |

The infrastructure layer does not clone a repository, run a test, build anything, hold a GitHub credential, choose an
application's architecture, decide when or what to release, or scale an application. A request that names an image, a
script, a command or a key is refused by the contract (`InfrastructureRequest` forbids unknown fields). A test holds the
line: `tests/cloudinfra/test_infra_catalog.py` fails if any module of `cloudinfra` imports `labdeploy`, `github`,
`templates` or `workers`, if a command runs anywhere but the token helper, or if Google Cloud is named outside its adapter.

## The contract

A request is a provider name, a region and at least one of nine blocks. Every field is a size, a count, a name, a version
or a switch; the operator's project, credentials, limits and allowed regions are configuration, never request parameters.
`list_resource_kinds` returns the blocks with their fields; a block of any other name is refused with
`UNSUPPORTED_RESOURCE_KIND` and the nearest kind.

```yaml
provider: gcp
region: us-central1
name: platform                       # optional prefix of every resource name
labels: {team: data}
network:  {public_access: true, cidr: 10.10.0.0/24, allowed_ports: [80, 443], allowed_sources: ["0.0.0.0/0"], iap_ssh: true}
compute:                             # one group, or a list of groups
  - {group: web, count: 2, cpu: 2, memory_gb: 8, os: ubuntu, public_ip: false}
  - {group: gpu, count: 1, cpu: 8, memory_gb: 32, accelerator: {type: l4, count: 1},
     profile: gpu-signed,             # an operator profile: an image with signed drivers (os otherwise)
     autostop: {idle_minutes: 60}, max_run_hours: 24}
storage:  {type: persistent, size_gb: 200, performance: balanced, group: gpu}
database: {engine: postgresql, version: "16", size: small, storage_gb: 10, high_availability: false, public_access: false}
cache:    {engine: redis, memory_gb: 2, high_availability: false}
queue:    [{name: events}, {name: jobs, subscription: false}]
secret:   [{name: api-key, generate_value: true}, {name: token}]
cluster:  {node_count: 2, node_cpu: 2, node_memory_gb: 8,   # one cluster, or a list of named ones (name: blue)
           registries: [us-central1-docker.pkg.dev/apps/shop], public: {hostnames: [shop.example.com]}}
load_balancer: {type: http, port: 8080, backend_group: web, health_check_path: /healthz}
lifetime: {hours: 168, on_expiry: stop}       # optional; none unless asked or set by the operator
access:   [{member: "group:data-team@example.com", level: use}]
```

Blocks compose across requests: `network_id` on compute, database, cache and cluster joins a network created earlier,
and `load_balancer.backends` fronts machines created earlier. Values that would be secrets never reach a response, the
state file or a log: a database password is generated here and written to a secret the request owns (`db-password`), and a
cache's AUTH string is copied from the provider into another (`cache-auth`); the request's access members read them with
their own identity. A generated secret value goes straight to the provider; a cluster's credentials come from the
provider's IAM. An operator may also have the password returned once, in the creating response
(`return_database_password`).

The response has the same shape for every provider: `request_id`, `status`, `resources` by kind (each with the id this
service issued, the provider's own identifier, name, zone, state, addresses), `endpoints` (public and internal addresses,
database host and port, bucket URLs), `operations`, the cost estimate, and `error` when something failed. Each resource
carries a `connect` block (commands, prerequisites, notes) written from what exists; `lifecycle` says when the request
expires, why it is stopped and what keeps billing while it is; `access` lists each member with its grants; `warnings` say
what to know now. `credentials`, in the creating response only, names the secrets that hold a database password or a
cache AUTH string. Every failure,
anywhere, is `{"status": "FAILED", "error": {"code", "message", "details"}}`; the codes are listed in
[MCP-CATALOG.md](MCP-CATALOG.md). The models are in `cloudinfra/models/` (pydantic, strict, unknown fields refused).

## Lifecycle

```
REQUEST ──► VALIDATE ──► PLAN ──► PROVISION ──► READY ──► QUERY / MONITOR ──► DESTROY ──► DESTROYED
               │                     │                                           │
               └─ refused ───────────┴─► FAILED (what was created is deleted)    └─► DESTROY_FAILED (what remains is named)
```

- **Validate.** The contract's own checks, then the operator's allowed regions and limits (`limits.*`), the provider's shape
  rules (machine types, database versions, zones), the rules that tie blocks together (a private database needs a network;
  a public address needs a public network), and the cost ceiling from list prices. All problems are reported, with codes.
- **Plan.** The provider adapter turns the request into named resources with their provider operation and shape; the planner
  issues resource ids, prices them, and lists warnings. Planning is pure: no provider call, nothing recorded.
- **Provision.** The request and its pending resources are recorded, an operation starts, and a bounded worker creates the
  resources in dependency order: network, storage, compute (then disks attach), database (after the network is prepared for
  it), cache, queues, secrets, cluster, load balancer (over the machines just created). Quota headroom is checked first when
  the provider reports it. The call returns at once with `PROVISIONING`, or waits
  up to 45 seconds. A failure marks the request `FAILED` with the cause and deletes what this request created, in reverse
  order; what could not be deleted is named in `error.details.cleanup.remaining`. The operator may keep failed resources for
  inspection (`cleanup_on_failure: false`); the response says so.
- **Query.** Status refreshes each resource from the provider; health reads the provider's state; usage reads Cloud
  Monitoring; cost prices the resources as they exist now (after a resize, whatever request or profile they came from),
  each stopped one at its stopped rate. A provider that cannot be reached leaves a resource `UNKNOWN` with the error, never
  a crash.
- **Destroy.** Reverse order: load balancer (its pieces, forwarding rule first), cluster, secrets, queues, cache, database
  (deletion protection off, then delete), machines, disks, buckets (only when empty), network (rules, peering, subnet,
  network). `DESTROYED`, or `DESTROY_FAILED` with the remaining resources, and the call can
  be repeated once the cause is fixed. A network that still carries machines or databases, of any request, is refused.
  A single delete (`delete_database`, `delete_compute`, …) takes a database's or a cache's credential secret with it, takes
  the resource off the members' access list, and makes the request `DESTROYED`, its access `REVOKED`, when it was the last.
- **Changes wait for each other.** Stop, start, destroy, a resize and a single delete are refused (`INVALID_STATE`, reason
  `changing`) while another of them, or an idle stop, is still running on the request; work that finishes late never
  overwrites a status set meanwhile, since every final status is written only from the one the work began from.

```
READY ──stop──► STOPPING ──► STOPPED ──start──► STARTING ──► READY
  │  (asked, or at expiry)       │
  └─ at expiry, on_expiry: destroy, or when the retention ends ──► DESTROYING ──► DESTROYED
```

The primitives (`create_compute`, `create_network`, …) go through the same path: each creates a request of one block, so
every resource has a request id, an operation record, an audit trail and a destroy path. `provision_infrastructure` is the
composite; primitives compose through `network_id` (machines and databases join a network created earlier) and
`attach_storage`.

## Access to what is created

Whoever asked for a machine needs to log in to it; whoever runs an application needs its database password. The service
grants access on the resources a request created, through each resource's own IAM policy, and changes the project's
policy for one thing only: a cluster's node account (below).

- **Members.** `access` on a request lists people, groups or service accounts (`user:`, `group:`, `serviceAccount:` and an
  email address) with a level, `use` or `admin`. The requester is added as `admin` when the operator maps its principal to
  an `identity` (`access.grant_requester`, on by default). A member must be a principal's identity or be in one of the
  operator's `access.allowed_domains`; `access.max_members` caps the list. A database's password goes only to its secret
  unless the operator sets `return_database_password`, so a database is refused when the operator's settings let the
  service grant access to no one at all.
  Email addresses compare without regard to case, as Google's do, and are kept in lowercase.
- **Grants**, applied once the resources exist:

  | Kind | `use` | `admin` |
  |---|---|---|
  | compute | `roles/compute.osLogin` on the machine, `roles/iap.tunnelResourceAccessor` on its IAP tunnel | `roles/compute.osAdminLogin`, and the tunnel |
  | secret (the database password and cache AUTH string among them) | `roles/secretmanager.secretAccessor` | the same and `roles/secretmanager.secretVersionAdder` |
  | storage (a bucket) | `roles/storage.objectUser` | `roles/storage.objectAdmin` |
  | queue | `roles/pubsub.publisher` on the topic, `roles/pubsub.subscriber` on the subscription | the same |
  | cluster | a RoleBinding to the built-in ClusterRole `view` in the request's namespace | a RoleBinding to `edit` there |
  | service (Cloud Run) | `roles/run.invoker` on the service (call it) | the same, `roles/run.developer` on the service, and `roles/iam.serviceAccountUser` on its runtime account (deploy revisions) |
  | job (Cloud Run) | `roles/run.invoker` on the job (run it) | the same, `roles/run.developer` on the job, and `roles/iam.serviceAccountUser` on its runtime account |

  A database and a cache are reached through their secrets and the request's network. A cluster gets one namespace,
  named after the request, when it is created, and its grants are Kubernetes RoleBindings in that namespace only: IAM's
  Kubernetes roles cannot be limited to one cluster. A `user:` or `serviceAccount:` member is bound as a `User` named by
  its email, a `group:` member as a `Group` (which needs Google Groups for RBAC); `edit` changes workloads, services,
  config maps and secrets, never RBAC or the namespace. `edit` would also let a namespace make a public Google load
  balancer (a Service of type `LoadBalancer`, an Ingress or a Gateway), so every cluster gets a ValidatingAdmissionPolicy,
  `cloudinfra-no-public-entry`, bound to the namespaces made for requests (labelled `cloudinfra-tenant: "true"`, which
  `edit` cannot change), that refuses all three there: a public address is this service's to make. The service makes these calls to the cluster's own API with its
  Google token, checking the server against the cluster's CA certificate. The tunnel grant names the machine's numeric
  id, so a later machine with the same name never inherits it.
- **Project-level prerequisites** are the platform team's, once: a role with `compute.projects.get` and
  `compute.instances.get` so that gcloud finds a machine, `cloudsql.client` for the Cloud SQL Auth Proxy,
  `container.clusterViewer` for cluster credentials. Each connect block names the ones it needs.
- **Failures and changes.** A grant that fails is recorded on the member (`FAILED` or `PARTIAL`, with the error), as is
  one that met a resource still being created; the request stays READY and `manage_infrastructure_access` grants again. A
  member of a request where nothing has a policy of its own (disks only) is `RECORDED`, not granted. A revoke removes the
  member from every resource of the request, including a policy a failed grant reached, and also on a request that failed
  or whose destroy failed; a revoke that fails is a failed operation. A level change removes the old roles first and
  grants the new ones wherever the member had access. Destroy takes members out of the tunnel policy, which outlives the
  machine. The roles recorded on a resource are kept when the provider's view of it is saved (after a refresh, a resize,
  a stop). The service only ever grants or removes the roles above.
- **The service's own identity** needs the `getIamPolicy` and `setIamPolicy` permissions of those resource types (Compute
  Engine instances, IAP tunnel instances, secrets, buckets, topics and subscriptions); a custom role that lists exactly
  those is the narrowest way to give them. For clusters it needs to create namespaces and RoleBindings in the clusters
  it made, which the Kubernetes Engine role it creates them with already allows.

### A cluster's nodes

A cluster's nodes run as a service account made for that cluster (`node-<request id>-<cluster name>`), never the Compute
Engine default account, which in a new project has no rights at all and in an old one often has too many. Google
recommends exactly this ([GKE service accounts](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/service-accounts)).
The account holds `roles/container.defaultNodeServiceAccount` on the project, the minimum the nodes need to write logs
and metrics, and `roles/artifactregistry.reader` on each Artifact Registry repository the request names in
`cluster.registries`, on nothing else; nodes pull images as this account. A request may name only repositories the
operator lists in `providers.gcp.registries`. The account, its project role and its repository roles are removed after
the cluster is deleted, or at once when Google refuses the cluster. `providers.gcp.cluster_node_accounts: false` keeps
the old behaviour (the default account, no registries).

The service's own identity then needs, once, from the platform team:

| What | Role | Limit it to |
|---|---|---|
| make and delete the node accounts | `roles/iam.serviceAccountCreator`, `roles/iam.serviceAccountDeleter` | the project |
| run a node pool as them | `roles/iam.serviceAccountUser` | the project: it can then act as any service account there, so run the service in a project of its own, with no other accounts worth taking |
| give a node account its project role | `roles/resourcemanager.projectIamAdmin` | an IAM condition, so it can grant that one role and no other: `api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', []).hasOnly(['roles/container.defaultNodeServiceAccount'])` ([limits on granting roles](https://docs.cloud.google.com/iam/docs/setting-limits-on-granting-roles)) |
| let a node account read a repository | `roles/artifactregistry.admin` | each repository listed in `providers.gcp.registries`, granted on the repository |

### HTTPS on the load balancer

`load_balancer.hostnames` (http only) adds HTTPS on 443 of the same address: a Google-managed certificate for the names,
a target HTTPS proxy on the same URL map, and a forwarding rule; plain HTTP stays on its port. The names must be in the
operator's `public_domains`. The certificate becomes ACTIVE once their DNS points at the address; a DNS zone in the same
request does that with `to: load_balancer`. The load balancer reports `hostnames` and the certificate's state; a second
forwarding rule is priced.

### A DNS zone

`dns: {zone, records}` makes a public Cloud DNS zone for a domain in one the operator allows (the same
`providers.gcp.public_domains` a front door uses) and writes its record sets in one change. A record has values, or
`to: load_balancer` or `to: cluster[:NAME]`, an A record to the request's own load balancer or a cluster's front door,
filled in here: the zone is made last, after everything it can point to. The operator points the domain at the zone's
name servers once, at the registrar; the connect block names them. `use` grants reading the zone, `admin` changing it.
Destroy empties the zone, then deletes it, as Cloud DNS deletes only an empty zone. About 0.20 dollars a month per zone.
The service's identity needs `roles/dns.admin`.

### A workload identity

`identity: {name, level}` makes an account for the request's own workloads (`id-<request id>-<name>`). No key is ever
made for it. It joins the request's access list at its level and is granted on the request's resources like any member,
on nothing outside the request; it is made first and deleted last. A compute group that names it (`compute.identity`)
runs its machines as it, with the `cloud-platform` scope and IAM deciding the rest; otherwise machines run as no account.
When the request has clusters, the Kubernetes service account of the identity's name in the request's namespace may act
as it through Workload Identity Federation for GKE (`roles/iam.workloadIdentityUser` on the account), so pods need no key
either; the connect block gives the two `kubectl` commands. Ranked first among the missing kinds by the research of
2026-10-28 and confirmed on 2026-10-30: Datadog's 2025 report found 55% of Google Cloud service accounts with keys older
than a year. The service's identity needs nothing beyond the node-account roles above.

### A managed container service

`service: {cpu, memory_mb, min_instances, max_instances, concurrency, timeout_seconds, ingress, public, network_id, egress}`
makes a Cloud Run service, empty: Google's sample image stands in until the delivery layer puts a release on it. It runs
as an account made for it (`run-<request id>-<name>`) that may read the request's secrets and nothing else, and both go
when the request is destroyed. `ingress` says where requests may come from (`all`, `internal`, or
`internal_and_load_balancer`); every caller needs the invoker role, which `use` grants, unless `public` turns Cloud Run's
invoker check off, which the operator must allow (`providers.gcp.allow_public_services`, off by default). With a network
(the request's own or `network_id`) the service sends private traffic through it (Direct VPC egress). CPU and memory are
checked against Cloud Run's pairs. Idle cost is warm instances only (`min_instances`); requests are billed by use.

The service's own identity also needs, for services: `roles/run.admin` (create, delete, and the policy that turns the
invoker check off and holds grants), and `roles/iam.serviceAccountAdmin` limited by the same practice as node accounts
(it grants Service Account User on a runtime account to the request's `admin` members); its `secretmanager.admin`
already covers the secrets' policies.

### A managed job

`job: {cpu, memory_mb, tasks, parallelism, max_retries, task_timeout_seconds, network_id, egress, schedule}` makes a Cloud
Run job that runs to completion, empty: Google's sample job image stands in until the delivery layer puts a release on
it. Its tasks run as an account made for it (`run-<request id>-<name>`) that reads the request's secrets and nothing else.
With `schedule: {cron, time_zone}` the service also makes a Cloud Scheduler job that calls the job's run endpoint as a
second account (`sch-<request id>-<name>`), which holds `roles/run.invoker` on this job only. On destroy the schedule goes
first, so nothing starts the job while it is deleted, then the job, then both accounts. A job costs only while it runs.
The service's identity also needs `roles/cloudscheduler.admin`, and the project the Cloud Scheduler API.

### A cluster's front door

`cluster.public: {hostnames: [shop.example.com]}` asks for a public address for the request's workloads, made and owned
by this service (a team's namespace may not make one; see the admission policy above). The cluster is made with Gateway
API on; the service reserves a global address (`<cluster>-door`), makes a Google-managed certificate for the hostnames,
and puts one Gateway, `cloudinfra-gateway/public`, of class `gke-l7-global-external-managed` on that address: HTTPS on 443
taking routes from the request's namespace only (matched by its name), and plain HTTP on 80 redirecting to HTTPS. Without
hostnames it serves plain HTTP on the address. A hostname must be in one of the operator's `providers.gcp.public_domains`.
The cluster reports `public.address`, the hostnames and the certificate's state; the operator points DNS at the address,
and the certificate becomes ACTIVE after that. A namespace with `edit` may write HTTPRoutes and GKE's HealthCheckPolicies (a
ClusterRole aggregated into `edit`) and attach them to that Gateway; Gateways stay out of `edit`.

GKE makes the Gateway's load balancer and deletes it with the Gateway, so the service deletes the Gateway first, waits
until no forwarding rule uses the address (ten minutes at most), then deletes the cluster, the certificate and the
address. It also needs, beyond the roles above, Compute Engine's `globalAddresses` and `sslCertificates` insert, get and
delete, and `forwardingRules` list. A forwarding rule per listener is priced in the estimate.

## Time: stop, start, idle stop and expiry

Nothing stops or expires by itself unless a request or the operator asks for it, so nothing in production stops by
surprise.

- **Stop and start** (`control_infrastructure`). Machines and databases stop (a Cloud SQL instance through its activation
  policy; a suspended machine is resumed); machines stop before their database and start after it. A request is STOPPED
  only when nothing in it that can stop is still running, whatever was asked: a start that fails on an idle-stopped
  machine leaves a request whose other machines run READY. While stopped, boot disks, database storage and addresses,
  buckets, caches, clusters and load balancers keep billing: the `lifecycle` block lists them, and `get_resource_cost`
  gives `current_monthly_usd` at the stopped rate.
- **Maximum run time** (`compute.max_run_hours`). Google's own `maxRunDuration` with `instanceTerminationAction: STOP`:
  each start begins the count again, and it holds even when this service is down.
- **Expiry** (`lifetime`, or the operator's `limits.default_lifetime_hours`, else `limits.max_lifetime_hours`). Hours from
  creation, at most `limits.max_lifetime_hours` with extensions. Every lifetime ends in a destroy, so a request that sets
  one needs `can_destroy`; an expiry the operator sets applies to every caller. Warnings 48 and 2 hours before (an
  `expiry_warning` audit event, and the `warnings` of every answer). At expiry, `on_expiry: stop` (the default) stops
  machines and databases and keeps everything for `limits.expiry_retention_days` (7) before destroying it; a request
  with nothing that can stop is kept the same way, and nothing says it stopped. `destroy`, or a retention of 0, deletes at
  once; while that destroy is refused (a network another request still uses), what can stop is stopped. A request that
  failed, or whose destroy failed, is destroyed at its expiry. Nothing is marked stopped while a machine is in the middle
  of a change or its state cannot be read. An attempt that fails is tried again after a wait that doubles each time
  (5 minutes, at most a day), so a lasting failure adds a failed operation a day, not one a sweep. `extend` adds hours,
  also after expiry while the data is kept, once a stop or start in progress has finished; a request that expired starts
  again only after an extend.
- **Idle stop** (`compute.autostop`, or the operator's `limits.default_autostop_idle_minutes` for machines with GPUs or
  for all). A machine stops when its busiest CPU sample in the window used fewer than `limits.idle_cpu_cores` cores (0.5),
  at least one window after it last started; no samples is not idleness. The samples are the machine's own, selected by its
  numeric id, never those of a machine of the same name in another zone. Each machine is judged and stopped on its own:
  one that cannot be measured or stopped never keeps the others running, and its next attempt waits as expiry's does.
  `keep_awake` suspends it for up to `limits.max_keep_awake_hours`. GPU utilisation is not measured, so a job that keeps
  the GPUs busy with little CPU needs `keep_awake`.
- **The sweeper** runs these checks every `sweep_interval_seconds` (300) as its own principal, `cloudinfra-sweeper`, which no
  token maps to; each action it takes is an operation and an audit event like any other. It decides on each request as it
  is at that moment, under the request's lock, which every change to a request's status, lifecycle, access list or
  recorded grants also takes; the stops and destroys it decides on run in the workers, so the lock is never held while the
  provider works, and a sweep reports what it started. 0 turns the sweeper off, and with it every lifetime and idle stop:
  requests may then not ask for them, and the operator's time defaults are refused at start.
- **One process per state file.** The server holds an exclusive lock on `<database>.lock` from start to the end of its
  shutdown; a second process on the same file refuses to start, rather than take over, as interrupted, work the first is
  still doing. At shutdown, work under way finishes before the lock goes; work not yet started is dropped, and its request
  is recovered at the next start.
- **Restarts.** At start the service puts a request left STOPPING or STARTING back to READY or STOPPED, as the provider
  sees it. A request left PROVISIONING becomes FAILED, and what it had created is deleted in the workers, as after any
  failure (unless `cleanup_on_failure` is off); one left DESTROYING becomes DESTROY_FAILED, so it can be destroyed again.
  Operations still RUNNING are marked interrupted.

## State

The service is a stateless function of its configuration plus the infrastructure state it must keep: requests (the
normalized request, owner, status, estimate, error), resources (id, kind, provider identifier, state), operations (kind,
status, timing, error) and audit events (identity, client, tool, outcome, duration). All in one SQLite file in WAL mode,
one writer. No application state, no business state, no credential: a test reads the database file and the log after a
database was created and finds no trace of the password.

## Security boundaries

| Boundary | How |
|---|---|
| Caller identity | Bearer token per principal, stored as a SHA-256 hash, compared in constant time; identity comes from the credential, never a parameter |
| Sign-in for Claude chat | Off unless `sign_in` is set (`security/oauth.py`): the service is its own OAuth 2.1 server for claude.ai, which cannot send a fixed token. A person pastes their principal's token on the service's own page, never in the chat; Claude gets a one-hour access token and a thirty-day refresh token standing for that principal, with its rights. PKCE, registration only for the configured callbacks (Claude's), codes and tokens kept as hashes, a refresh token used once (a second use ends the sign-in), and a principal removed or given a new token ends its sign-ins |
| Authorization | `can_provision` for tools that create or change, `can_destroy` for deletes and for a lifetime, which ends in one; `scope: own` sees only its own requests (another caller's request is NOT_FOUND, not merely forbidden); `scope: all` for an operator identity |
| Budgets | Per-identity request budget, per-address failed-login budget (429 with Retry-After) |
| Provider credential | Taken by the adapter from the host (metadata server, or gcloud with optional impersonation); never configured in a file, never accepted from a caller, never visible to the Application Layer |
| Access to what is created | Grants only on the resources a request created, through each resource's own policy, never on the project; members limited to the operator's domains and count; only a fixed list of roles is ever granted or removed |
| Least privilege on what is created | Machines get OS Login (IAM) and no service account, vTPM and integrity monitoring, and Secure Boot except on a GPU machine whose image has no GPU drivers, whose installation needs it off; SSH only from the identity-aware proxy range; subnets only in private ranges; buckets with uniform access and public-access prevention; databases encrypted-only with deletion protection and backups, turned off only by an explicit destroy; caches private with AUTH and TLS; cluster nodes with logging, monitoring and read-only storage scopes only, shielded, workload identity on; load balancers open only the backend port to the provider's health checkers |
| No command execution | The contract carries no script, command, key or provider structure; names, ranges, ports and labels are validated (`security/validation.py`); strict models refuse unknown fields; the server refuses unknown arguments by name before a tool runs |
| Structured errors | Provider exceptions never escape; every error has a code; provider messages are kept in `details`, truncated |
| Cost controls | Limits on counts and sizes, a monthly cost ceiling per service and per caller, a cap on live requests per caller, quota checks, labels `managed-by`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner` on every resource plus the operator's own, cleanup on failure, destroy on request, and estimates on every plan |

## Observability

Every call is an audit event with identity, client address, tool, outcome and duration (`events` table). Every step of
the lifecycle is a JSON line on the `cloudinfra` logger carrying `request_id`, `operation_id` and `identity`:
`request_received`, `request_validated`, `provider_selected`, `resources_requested`, `provisioning_started`,
`quota_checked`, `resource_created`, `storage_attached`, `provisioning_completed`, `provisioning_failed`,
`cleanup_started`, `resource_deleted`, `cleanup_completed`, `destruction_started`, `destruction_completed`,
`destruction_failed`, `resource_resized`, `firewall_configured`, `credential_stored`, `access_granted`,
`access_grant_failed`, `access_revoked`, `access_revoke_failed`, `stop_started`, `stop_completed`, `stop_failed`,
`start_started`, `start_completed`, `start_failed`, `resource_stopped`, `resource_started`, `lifetime_extended`,
`idle_stop_suspended`, `expiry_warning`, `request_expired`, `machine_idle`, `sweep_completed`, `sweep_failed`,
`internal_error`, and `provider_call` for each provider request (method, URL, HTTP status). Fields under secret-looking keys are redacted before a line is written.

## Terraform

The Terraform roots and modules under `infra/gcp` belong to the application delivery workflow: they describe Cloud Run
services and are applied by the GitHub pipeline from merged pull requests. The infrastructure core calls the provider's
APIs directly through one thin client (`providers/gcp/api.py`): the unit tests need no binary, state bucket or plugin,
every call is mockable, and a plan can be computed offline. A Terraform-backed driver can implement the same
`CloudProvider` interface later, with the state it needs kept in the service's own state store; the Application Layer
never writes HCL either way.

## Extending to another provider

`providers/aws/provider.py` implementing `CloudProvider` (see [PROVIDER-INTERFACE.md](PROVIDER-INTERFACE.md)), one
`register("aws", …)` line in `providers/__init__.py`, and an `aws:` block in the operator's configuration. The models,
the orchestrator, the state, the server, the catalog and the Application Layer do not change; the same request with
`provider: aws` provisions on AWS. Until then the name is refused with `UNSUPPORTED_PROVIDER` and a sentence that says
which adapters exist.

## The design test

`tests/cloudinfra/test_infra_orchestration.py::test_design_test_one_ubuntu_vm…` sends
`{"provider": "gcp", "region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "os": "ubuntu"}}` and gets a READY
machine with an address, an operation record and a cost estimate, having given no repository, source, Dockerfile,
workflow, test or build; the test also asserts that nothing sent to the provider mentions any of those, and
`test_infra_models.py` asserts that the contract has no field that could.

## Calls to Google

Every call goes through one client (`providers/gcp/api.py`). A call Google rate limited (HTTP 429, refused before it
ran) is tried again up to four times, after the pause Google names in `Retry-After` or else after 1, 2 and 4 seconds; a
read Google answered with a 5xx is tried again the same way, and a write is not, since it may have been done. Each retry
is a `provider_retry` event. On a laptop with a person's sign-in, set `providers.gcp.quota_project`: Google then counts
the calls against that project (`X-Goog-User-Project`) instead of gcloud's own shared one, whose per-minute limits a
single provisioning reached on 2026-10-29. The person needs `serviceusage.services.use` on it; a service account on
Google Cloud needs neither the setting nor the permission.

## Running it

```bash
python -m cloudinfra.cli token                      # a caller token and its hash for the configuration
cp cloudinfra.example.yaml cloudinfra.local.yaml    # project, regions, limits, principals
CLOUDINFRA_CONFIG=cloudinfra.local.yaml uvicorn cloudinfra.mcp.server:create_app --factory --host 127.0.0.1 --port 8100
python -m cloudinfra.cli plan request.yaml          # an offline plan with its estimate, no provider call
python -m pytest tests/cloudinfra -q                # the boundary tests, no cloud
CLOUDINFRA_INTEGRATION=1 CLOUDINFRA_PROJECT=my-project python -m pytest tests/integration/cloudinfra -q   # one real machine, then destroyed
```

The catalog has 57 tools: create, get and delete for each of the thirteen kinds (plus list, resize, attach and firewall
configuration where they apply), the lifecycle tools (stop, start, extend and keep awake among them), access, and the
operations tools. The MCP endpoint is `/mcp`; the REST endpoints are `POST /infrastructure/validate`,
`POST /infrastructure/plan`, `POST /infrastructure`, `GET /infrastructure` (with `status` and `expiring_within_hours`),
`GET /infrastructure/{id}`, `DELETE /infrastructure/{id}`, `POST /infrastructure/{id}/actions` (`action`, `hours`),
`POST /infrastructure/{id}/access` (`action`, `member`, `level`), `GET /catalog` and `GET /status`. The same Docker image as Fast Deploy carries the package; its command is the uvicorn line above. The
Fast Deploy controller and this service are separate processes with separate principals, state files and credentials;
Claude chat connects through the service's own sign-in; see "Connect Claude chat" below.

## Connect Claude chat

claude.ai adds a remote MCP server as a custom connector, and signs in to it only with OAuth: it cannot send a fixed
bearer token. So the service can sign a person in itself.

1. Serve the service on a public HTTPS address that Claude's servers reach, such as `https://infra.example.com`, behind
   a TLS proxy or load balancer.
2. In the configuration, add that host to `allowed_hosts` and turn sign-in on:

   ```yaml
   allowed_hosts: ["infra.example.com"]
   sign_in:
     url: https://infra.example.com
     # callbacks: [https://claude.ai/api/mcp/auth_callback, https://claude.com/api/mcp/auth_callback]   # the default
     # access_token_minutes: 60
     # refresh_token_days: 30
   ```

3. In Claude: Customize, Connectors, Add custom connector, URL `https://infra.example.com/mcp`. Claude registers itself
   and opens the service's sign-in page in the browser. Paste the token of the principal Claude should act as; Claude
   then has that principal's rights and nothing more. Every call is audited under that principal's name, and the
   sign-in itself is an event (`sign_in`, `token_reuse`, `sign_out`).

Give the person their own principal (for example `scope: all` and no `can_destroy` for someone who only looks) rather
than a system's. To end a sign-in, give the principal a new token or remove it. Static tokens keep working beside it.
The delivery service (`appdelivery`) takes the same `sign_in` block.
