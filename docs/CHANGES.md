# 1.37.0: release A, so that a developer can use what the service creates

`docs/RESEARCH-2026-10-28-positioning.md` (added in this release) placed the service against the infrastructure products
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

`docs/RESEARCH-2026-10-28-infrastructure-layer.md` reviewed the infrastructure layer against the MCP specification, industry
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
Cloud Run specifics. The analysis behind the split is `docs/REFACTOR-2026-10-28.md`.

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

# 1.34.0: a premium finish, a dark theme and pages for the long lists

A second pass over the design system of 1.33.0, after reading what the best product interfaces share: hierarchy from weight
and spacing rather than decoration, colour held back for meaning, structure from faint borders with shadows only where
something floats, one scale of spacing, and a hover, pressed, focused and disabled state on everything a person can touch.

- Tokens: cool-tinted neutrals, a deeper accent kept for the primary action, links, focus and the live state; cards and
  tiles carry a faint border and no resting shadow; the primary button is a subtle gradient with an inner highlight; inputs
  and switches are tighter; the sidebar's current page is a quiet filled row, not a block of colour.
- A dark theme, designed rather than inverted: surfaces step up in lightness, accents and status colours desaturated, never
  pure black. It follows the system setting (`prefers-color-scheme`). The charts colour themselves from the theme's tokens,
  so every chart, the deployment page and the sign-in page read in both.
- The long lists have pages: releases (25 a page), events (50, or 200 with `more`) and the deployments list (20), with a pager
  that says where you are and links to every page. Events are counted and read a page at a time from the database
  (`Store.find_events` takes an offset; `Store.count_events` is new).
- Smaller: empty states are drawn as an inset panel, the sign-in card sits on a soft glow, scrollbars are thin.

Routes, forms, class names and the strict headers are unchanged (no script outside the deployment page, no web font, no
image, no `url(`).

# 1.33.0: one design system for every page

The pages the controller serves (Monitor, Configure, the deployment page, the setup steps, sign-in and the OAuth consent page)
shared no look: three palettes, three kinds of card, sidebar labels that changed from page to page. This release draws them
all with one design system, `labdeploy/theme.py`, built on Apple's Human Interface Guidelines as they apply to a web page: the
system typeface with tabular numbers and a small type scale, semantic colours used for one meaning each, a grouped light
canvas with white surfaces and hairline separators, 8-point spacing, 44-point targets on a phone, and brief motion that is
off when a person asks for reduced motion.

- The sidebar is a translucent material with one vocabulary everywhere (Monitor: Overview, Deployments, Health, Releases,
  Cost, Activity; Configure: Connections, Applications, Platform, People, Production, Advanced), the current page in the
  accent colour and the signed-in identity as a small card. On a phone it becomes a bar that stays at the top.
- Cards, settings rows, fields, switches, segmented controls, chips, callouts, tables and the sign-in card share one set of
  tokens, and the charts use the same palette.
- Motion: a page rises in as it loads, cards lift under the pointer, the switch slides, the live dot ripples and the Monitor
  toolbar stays at the top of the screen while the page scrolls; `prefers-reduced-motion` turns every animation off.
- The deployment page keeps its board, track, spotlight and journey, now in the same colours and type as the rest, with its
  tools drawn as buttons.
- The OAuth consent page is the same sign-in card as the settings page.

Nothing a page does changed: the same routes, forms, class names and strict headers (no script outside the deployment page,
no web font, no image, no `url(`), and the same tests pass.

# 1.32.0: company readiness, phases 0 and 2

This release implements phases 0 and 2 of the October 23 readiness plan. Codex started the work and Claude finished it. Phase 1
(company identity) is not part of this release.

Phase 0 adds a control catalog with a dependency map, and verified secrets now block every release path whatever the
code's review status. A contract check in CI runs for every pull request, and a written production-gate specification
describes a future gate without enforcing it.

Phase 2 adds:

- an operation ledger, with intents written off-host before any dispatch;
- a scheduler whose safety work cannot be starved;
- dependency resolution in isolated workers;
- a job broker that reserves quota before a job starts;
- an opt-in supervisor that recovers an interrupted production release through GitHub.

An independent review found fifteen issues, and thirteen are fixed with tests (docs/READINESS-IMPLEMENTATION.md). Every
new part is off until configured. `resolver_local_fallback: true` keeps lockfile generation on the controller until the
worker project exists. Nothing here has been exercised live; docs/READINESS-ROLLOUT.md lists the drills.
