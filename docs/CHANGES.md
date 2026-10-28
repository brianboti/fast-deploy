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
