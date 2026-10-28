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
  │                           MCP (Streamable HTTP, 27 tools) or REST, bearer token per caller
  ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│ Cloud Infrastructure MCP  (cloudinfra)                                             │
│                                                                                    │
│  mcp/server.py       FastAPI + StrictMCP: auth gate, rate limits, audit, hints     │
│  mcp/catalog.py      the service catalog: one record per tool, renders the doc     │
│  mcp/tools/*         compute · storage · network · database · infrastructure · ops │
│  orchestration/      validator → planner → provisioner (status, destroy, cleanup)  │
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
                 │  REST: compute, sqladmin, storage, servicenetworking, monitoring
                 ▼
            Google Cloud
```

## What each layer owns

| APPLICATION LAYER OWNS | INFRASTRUCTURE LAYER OWNS |
|---|---|
| application source code | compute provisioning (virtual machines) |
| application testing | network provisioning (networks, subnets, firewall rules) |
| application builds | storage provisioning (block disks, object buckets) |
| Docker images and other application artifacts | managed database provisioning |
| application release decisions and versions | the cloud resource lifecycle: validate, plan, provision, query, destroy |
| application-level scaling behaviour | infrastructure status, health and usage |
| application-level clustering and sessions | provider abstraction and the request/response contract |
| application configuration and secrets | cloud authentication (the provider credential) |
| the GitHub workflow, if any | infrastructure cleanup, on failure and on request |

The infrastructure layer does not clone a repository, run a test, build anything, hold a GitHub credential, choose an
application's architecture, decide when or what to release, or scale an application. A request that names an image, a
script, a command or a key is refused by the contract (`InfrastructureRequest` forbids unknown fields). A test holds the
line: `tests/cloudinfra/test_infra_catalog.py` fails if any module of `cloudinfra` imports `labdeploy`, `github`,
`templates` or `workers`, if a command runs anywhere but the token helper, or if Google Cloud is named outside its adapter.

## The contract

A request is a provider name, a region and at least one block. Every field is a size, a count, a name, a version or a
switch; the operator's project, credentials, limits and allowed regions are configuration, never request parameters.

```yaml
provider: gcp
region: us-central1
name: analytics                      # optional prefix of every resource name
labels: {team: data}
compute:  {count: 2, cpu: 4, memory_gb: 16, os: ubuntu, boot_disk_gb: 20, public_ip: true}
storage:  {type: persistent, size_gb: 200, performance: balanced}
database: {engine: postgresql, version: "16", size: small, storage_gb: 10, high_availability: false, public_access: false}
network:  {public_access: true, cidr: 10.10.0.0/24, allowed_ports: [80, 443], allowed_sources: ["0.0.0.0/0"], iap_ssh: true}
```

The response has the same shape for every provider: `request_id`, `status`, `resources` by kind (each with the id this
service issued, the provider's own identifier, name, zone, state, addresses), `endpoints` (public and internal addresses,
database host and port, bucket URLs), `operations`, the cost estimate, and `error` when something failed. A database's
password is in `credentials` of the response that created it, once; the service never stores or logs it. Every failure,
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
  it). Quota headroom is checked first when the provider reports it. The call returns at once with `PROVISIONING`, or waits
  up to 45 seconds. A failure marks the request `FAILED` with the cause and deletes what this request created, in reverse
  order; what could not be deleted is named in `error.details.cleanup.remaining`. The operator may keep failed resources for
  inspection (`cleanup_on_failure: false`); the response says so.
- **Query.** Status refreshes each resource from the provider; health reads the provider's state; usage reads Cloud
  Monitoring; cost re-prices the stored request. A provider that cannot be reached leaves a resource `UNKNOWN` with the
  error, never a crash.
- **Destroy.** Reverse order: database (deletion protection off, then delete), machines, disks, buckets (only when empty),
  network (rules, peering, subnet, network). `DESTROYED`, or `DESTROY_FAILED` with the remaining resources, and the call can
  be repeated once the cause is fixed. A network that still carries machines or databases, of any request, is refused.

The primitives (`create_compute`, `create_network`, …) go through the same path: each creates a request of one block, so
every resource has a request id, an operation record, an audit trail and a destroy path. `provision_infrastructure` is the
composite; primitives compose through `network_id` (machines and databases join a network created earlier) and
`attach_storage`.

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
| Authorization | `can_provision` for tools that create or change, `can_destroy` for deletes; `scope: own` sees only its own requests (another caller's request is NOT_FOUND, not merely forbidden); `scope: all` for an operator identity |
| Budgets | Per-identity request budget, per-address failed-login budget (429 with Retry-After) |
| Provider credential | Taken by the adapter from the host (metadata server, or gcloud with optional impersonation); never configured in a file, never accepted from a caller, never visible to the Application Layer |
| Least privilege on what is created | Machines get OS Login (IAM) and no service account; SSH only from the identity-aware proxy range; subnets only in private ranges; buckets with uniform access and public-access prevention; databases encrypted-only with deletion protection and backups; a database's deletion protection is turned off only by an explicit destroy |
| No command execution | The contract carries no script, command, key or provider structure; names, ranges, ports and labels are validated (`security/validation.py`); strict models refuse unknown fields; the server refuses unknown arguments by name before a tool runs |
| Structured errors | Provider exceptions never escape; every error has a code; provider messages are kept in `details`, truncated |
| Cost controls | Limits on counts and sizes, a monthly cost ceiling per service and per caller, a cap on live requests per caller, quota checks, labels `managed-by`, `cloudinfra-request`, `cloudinfra-name`, `cloudinfra-owner` on every resource plus the operator's own, cleanup on failure, destroy on request, and estimates on every plan |

## Observability

Every call is an audit event with identity, client address, tool, outcome and duration (`events` table). Every step of
the lifecycle is a JSON line on the `cloudinfra` logger carrying `request_id`, `operation_id` and `identity`:
`request_received`, `request_validated`, `provider_selected`, `resources_requested`, `provisioning_started`,
`quota_checked`, `resource_created`, `storage_attached`, `provisioning_completed`, `provisioning_failed`,
`cleanup_started`, `resource_deleted`, `cleanup_completed`, `destruction_started`, `destruction_completed`,
`destruction_failed`, `resource_resized`, `firewall_configured`, `internal_error`, and `provider_call` for each provider
request (method, URL, HTTP status). Fields under secret-looking keys are redacted before a line is written.

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

## Running it

```bash
python -m cloudinfra.cli token                      # a caller token and its hash for the configuration
cp cloudinfra.example.yaml cloudinfra.local.yaml    # project, regions, limits, principals
CLOUDINFRA_CONFIG=cloudinfra.local.yaml uvicorn cloudinfra.mcp.server:create_app --factory --host 127.0.0.1 --port 8100
python -m cloudinfra.cli plan request.yaml          # an offline plan with its estimate, no provider call
python -m pytest tests/cloudinfra -q                # the boundary tests, no cloud
CLOUDINFRA_INTEGRATION=1 CLOUDINFRA_PROJECT=my-project python -m pytest tests/integration/cloudinfra -q   # one real machine, then destroyed
```

The MCP endpoint is `/mcp`; the REST endpoints are `POST /infrastructure/validate`, `POST /infrastructure/plan`,
`POST /infrastructure`, `GET /infrastructure`, `GET /infrastructure/{id}`, `DELETE /infrastructure/{id}`, `GET /catalog`
and `GET /status`. The same Docker image as Fast Deploy carries the package; its command is the uvicorn line above. The
Fast Deploy controller and this service are separate processes with separate principals, state files and credentials;
Fast Deploy's OAuth sign-in can front this server the same way when Claude's hosted connector needs it.
