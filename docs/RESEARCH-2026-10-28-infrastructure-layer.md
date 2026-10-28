# The infrastructure layer against official guidance, practice and research. cloudinfra 1.36.0 reviewed on 2026-10-28

Written 2026-10-28 UTC as a read-only review of the Cloud Infrastructure MCP (`cloudinfra`, version 1.36.0, commit
`04002a9` on `main`). Nothing in the code was changed; one stale number in `docs/INFRASTRUCTURE-ARCHITECTURE.md` (the
diagram said 27 tools; the catalog has 43) was corrected while writing this. The two questions asked were the
operator's: is this layer built the way the specifications, the industry and the research say such a layer should be
built, or should it change; and which kinds of infrastructure do applications most commonly need from it, so that the
catalog grows in the right order?

Method: the code on `main` was read where it defines the contract, the lifecycle and the provider adapter
(`cloudinfra/models`, `cloudinfra/orchestration`, `cloudinfra/providers/gcp`, `cloudinfra/mcp`, the three documents and
the catalog). Five themed research threads then ran in parallel, each reading primary pages rather than search
snippets and reporting with marked sources: the MCP specification and Anthropic's and OWASP's guidance for tool
servers, with five peer infrastructure servers; provisioning-service contracts and lifecycles in industry (Open
Service Broker, Crossplane, Terraform and Pulumi engines, Google's Infrastructure Manager, the CNCF platform
papers, Humanitec and Score, AWS Service Catalog, idempotency, long-running operations, errors, tagging, pricing,
expiry); the academic literature on cloud-neutral topologies, infrastructure as code and LLM agents acting on
infrastructure; Google Cloud's own documentation for every resource the adapter creates; and demand evidence for
which resource kinds applications need. Eleven claims that would change the design were then re-read on the primary
page by the reviewer. Section 11 marks each source as read on the official page or the paper (F), re-read by the
reviewer as well (F+), seen in a secondary source or a search snippet only (S), or unverified (U). Where a page could
not be fetched, the section says so.

Acronyms: MCP (Model Context Protocol, the tool protocol Claude and other clients speak), OSB (Open Service Broker API,
the contract cloud platforms use to provision backing services), LRO (long-running operation, an operation that outlives
one request), IaC (infrastructure as code), TOSCA (OASIS's standard for cloud-neutral topology models), PSA (Private
Services Access, Google's peering-based private connectivity), PSC (Private Service Connect, its successor), GKE
(Google Kubernetes Engine), ALB and NLB (application and network load balancer), SKU (a priced unit in a provider's
catalog), SU (service unit, Jetstream2's allocation currency), FOCUS (the FinOps Foundation's cost data specification).

## 1. The short answer

The architecture is right. Every body of guidance consulted arrives at the shape this layer already has: a closed,
typed, provider-neutral contract with no scripts, keys or commands in it; validation, then an offline plan, then
provisioning, with a separate destroy; cleanup of what a failed request created; ownership labels on every resource;
operator limits and a cost ceiling enforced by the service rather than by the caller; and a provider adapter behind one
interface. The research literature adds a strong reason the shape is right for an agent-driven caller: the measured
failure mode of LLMs acting on infrastructure is generating infrastructure-as-code text (first-try deployability of 20
to 30 percent and single-digit security compliance in the 2024 to 2026 benchmarks), and a typed catalog removes that
surface entirely.

What should change is narrower and mostly concrete. Two findings are defects in the Google adapter: the tcp load
balancer's health-check firewall rule lacks the ranges Google's passthrough probes come from, so its health checks
fail; and Secure Boot, which the adapter turns on for every machine, blocks Google's GPU driver installation on the
GPU machines. Three are currency: Cloud SQL's defaults have moved to PostgreSQL 18 and MySQL 8.4 and the adapter still
offers versions in paid extended support; Google now recommends Private Service Connect over the peering the adapter
uses; Autopilot is Google's recommended cluster mode and the adapter creates Standard zonal clusters. One is a protocol
signal: failed results are returned as ordinary results, where the MCP specification says execution errors carry
`isError: true`. The rest is operational maturity the patterns expect and the layer does not yet have: an idempotency
key on provisioning, update of a whole request, expiry of ephemeral infrastructure, drift detection, live prices
instead of a hand-entered table, a durable way to deliver a database credential, and a two-step destroy.

On coverage, the nine kinds match the top of every demand ranking. The strongest gaps, in order, are identities and
role bindings, DNS and TLS certificates (which turn a load balancer into a usable https endpoint), NAT egress for
private machines, monitoring and alerting, a container registry, and a shared file system; the research-computing
context then adds fractional GPUs, allocation accounting in service units rather than dollars, snapshots and images,
and batch clusters. Serverless containers rank high in every survey but need an image, which this contract forbids on
purpose; they belong to the delivery workflow that sits on top.

| Area | Verdict | One line |
|---|---|---|
| Contract and boundary | right | Typed blocks, no code or secrets, unknown kinds refused with the nearest kind; the agent-safety literature asks for exactly this |
| Lifecycle | right, incomplete | Validate, plan, provision, status, destroy and cleanup match OSB and Crossplane; missing idempotency key, update, expiry, drift |
| MCP surface | mostly right | Annotations, structured output, strict arguments, audit; `isError` missing on failures, descriptions too short, nine `get_*`/`delete_*` pairs could be two tools, SDK pinned to the maintenance line |
| Security posture | right, extend | Hashed principals, scopes, rate limits, host credential only; add a two-step destroy, argument audit, OAuth resource metadata, catalog checksum |
| Google adapter | two defects, five currency items | NLB health-check ranges, Secure Boot on GPU machines; versions, PSC, Autopilot, Valkey, live prices |
| Cost control | right in design, weak in data | Ceiling, limits, quota are what the FinOps literature asks; the price table is approximate and unverified |
| Coverage | top nine present | Next: identity, dns and certificate, nat, monitor, registry, filesystem, schedule |

## 2. Where the layer stands

The service is described in `docs/INFRASTRUCTURE-ARCHITECTURE.md`; what matters for this review is the shape. A caller
sends an `InfrastructureRequest` with a provider, a region and one or more of nine typed blocks: network, storage,
compute (one group or a list, each optionally with GPUs), database, cache, queue, secret, cluster and load balancer.
Strict models refuse any other field. The orchestrator validates the request against the operator's regions and
limits, the adapter's shape rules and a monthly cost ceiling computed from a hand-entered price table; plans it offline
into named resources with their provider operation and an estimate; records it; provisions it in a bounded worker in
dependency order; refreshes each resource from the provider on read; and destroys in reverse. A failure deletes what
the request created and names what it could not. State is SQLite, holds no credential, and a generated database
password is returned once. Forty-three MCP tools in eleven groups expose create, get and delete per kind, the lifecycle
tools, operations tools and `list_resource_kinds`; a small REST API mirrors the lifecycle. Callers are principals with
hashed bearer tokens, `can_provision`, `can_destroy` and a scope; the provider credential comes from the host.

## 3. The MCP surface, against the specification, Anthropic and OWASP

**The specification has moved on.** The current protocol revision is 2026-07-28 (F+); the Python SDK's 2.x line
implements it (2.0.0 on 28 July 2026, 2.3.0 on 2 October 2026) and our pin, `mcp==1.30.0` of 7 September 2026, is the
1.x maintenance line speaking 2025-11-25 (F+). The new revision removes sessions and the `initialize` handshake,
carries the version in each request, adds a mandatory `server/discover`, moves long-running tasks into an extension
and replaces server-initiated elicitation with multi-round-trip requests (F). The 2.x SDK negotiates down to older
clients, and `stateless_http=True` already fits the sessionless model, so the move is a dependency change with tests
rather than a redesign; the Tasks extension is not yet in the Python SDK (F).

**Execution errors must say so.** All three revisions report tool execution errors, including input validation
errors, "in tool results with `isError: true`", so that models can self-correct (F+). Our `audited` wrapper and
`StrictMCP.call_tool` return the structured error object as an ordinary result; the structured payload is right, the
signal is missing. The SDK passes a `CallToolResult` with both `isError` and `structuredContent` through (F). The
specification also says a tool that returns structured content should return the serialized JSON in a text block as
well, which the SDK does for us, and that with an `outputSchema` results must conform to it (F+); ours is
`dict[str, Any]`, which validates nothing.

**Descriptions are too short for the model.** Claude's tool documentation asks for "at least 3 to 4 sentences for each
tool description", to consolidate related operations into fewer tools, to namespace names when servers are combined,
and to use `input_examples` for complex inputs (F+). Anthropic's engineering guidance adds: return names rather than
opaque ids, offer concise and detailed response formats, paginate, and write errors that say what to change (F). Our
descriptions are the catalog's one-sentence purposes (median about 110 characters); the catalog's output, errors and
security text never reaches the model. The nine `get_*` and nine `delete_*` tools differ only by kind, which the id
prefix already encodes, so `get_resource` and `delete_resource` would bring the catalog from 43 to 27 without losing
anything; `create_*` tools should stay separate because their schemas differ.

**Security guidance.** The specification requires servers to validate inputs, enforce access control, rate limit and
sanitize outputs, says clients must treat annotations as untrusted, forbids sessions as authentication and token
passthrough, and makes OAuth 2.0 protected resource metadata (RFC 9728) mandatory for servers that support
authorization (F). OWASP's MCP Top 10 and cheat sheet ask for multi-step confirmation of high-impact operations,
short-lived scoped tokens, deny-by-default, immutable audit of every invocation with parameters, `additionalProperties:
false`, and treating tool outputs as untrusted (F). OWASP's client-side risk gating floors a tool to the highest risk
when it is destructive without being idempotent (F); our deletes carry `idempotentHint: true` but a second delete of a
gone resource answers `INVALID_STATE`, so the hint is not quite true. We meet the server MUSTs, `additionalProperties:
false`, per-identity and per-address budgets and an audit row per call; we lack the confirmation step, parameters in
the audit row, and OAuth resource metadata (static bearer tokens work in Claude Code and the Messages API's MCP
connector, but claude.ai accepts a fixed request header only in a beta for a limited set of organisations, as one
organisation-wide credential, so per-person use there needs OAuth; corrected by `docs/RESEARCH-2026-10-28-positioning.md`).

**Peers.** HashiCorp's Terraform MCP server has 61 tools with write operations off by default and a plan modelled as a
run you poll; AWS Labs' servers are read-only by default with `--allow-write`, use an `operation` parameter per
resource, tag what they create and only modify what they created, and their Cloud Control server enforces a chain of
generate, explain, check, create with an explicit confirmation on delete; Google's Cloud Run MCP has seven tools and no
delete, and Google's managed MCP servers let IAM deny every tool whose `isReadOnly` attribute is false; Pulumi's hosted
server never runs `pulumi up` itself; the Kubernetes servers ship a read-only mode and a non-destructive mode (F). Our
`can_provision`, `can_destroy` and scope per principal is the same split; what peers add that we lack is a global
read-only or no-destroy switch independent of principals and a server-side confirmation on destruction.

## 4. The contract and the lifecycle, against industry patterns

**Open Service Broker.** The platform chooses the instance id and the broker treats a repeat with identical parameters
as the same request (200), with different parameters as a conflict (409), and one still in progress as the same
operation (202); asynchronous work is polled with `last_operation` and a recommended `Retry-After`; credentials are a
separate binding resource returned at least once; the platform should delete resources it cannot be sure were created,
and keep trying (F). Our lifecycle matches the asynchronous shape, the busy-request refusal and orphan mitigation; it
lacks the client-chosen idempotent id and the binding.

**Idempotency.** The IETF `Idempotency-Key` draft (version 08, 22 October 2026) recommends a UUID key, 422 when the
same key arrives with a different payload and 409 for a concurrent duplicate (F+); Google's AIP-155 and EC2's
`ClientToken` are the same pattern (F); Compute Engine's insert takes a `requestId` that makes a retry within a day a
no-op (S, the reference page did not render). Our `provision` mints a fresh id on every call, so a retried call after a
lost response creates a second paid request and eats the per-principal cap; the adapter sends no `requestId`.

**Long-running operations and errors.** Google's AIP-151 operation carries `name`, `metadata`, `done` and either
`error` or `response`, with get, wait and cancel; Azure's and AWS's async patterns return 202 with a status location
and `Retry-After` (F). Our operation record has the status and timing but no `done`, progress, cancel or retention
policy, and the REST provision returns 200 with `PROVISIONING` rather than 202. AIP-193 and RFC 9457 want a stable
reason or type, a domain, and help; ours has the code, message and details, which is close, and could add
`retryable` and a problem-details rendering on REST (F).

**Crossplane and the Kubernetes model.** Crossplane models platform APIs as typed definitions with compositions, keeps
reconciling (it overrides changes made outside it), supports a deletion policy of delete or orphan, a `Usage` that
refuses to delete a resource another still uses, and import by observe-only policy (F). Our `list_resource_kinds` is
the definition, the planner is the composition, refusing to delete a network with tenants is the `Usage`, and our
status refresh observes drift without reverting it, which matches Terraform's `-refresh-only` rather than a
reconciler (F). What we lack is a per-request deletion policy, an explicit drift state, and adoption of existing
resources.

**Engines.** Terraform's own documentation explains why it keeps dependencies in state: at destroy time the
configuration is gone and per-type ordering "quickly explodes" (F); our destroy order is by kind index, which holds for
nine kinds and cross-request references by id, and would need recorded dependencies once kinds multiply. Pulumi's
Automation API names "exposing Pulumi behind a REST API" as a use case, and Google's Infrastructure Manager runs
Terraform as a managed service with a deployment state machine, quota validation, labels and a `requestId` honoured for
an hour (F). Direct APIs remain the right engine for a fixed catalog that plans offline and tests against a fake; if
whole-request update with replacements becomes a requirement, an Infrastructure Manager-backed adapter behind the same
interface is the cheaper path.

**Platform engineering.** The CNCF platforms white paper lists provisioning APIs, infrastructure, data, messaging,
identity and secret services among a platform's capability domains, and names self-service, consistent interfaces,
secure by default and "thinnest viable platform" as the attributes; its maturity model puts "self-service solutions",
centrally enabled provisioning and documented ownership at the higher levels (F). Score lets a workload declare
resources by type and class and consume outputs; Humanitec matches resource definitions by environment, application
and class; AWS Service Catalog narrows parameters with template constraints and requires an idempotency token on
provisioning (F). AWS Proton, the other reference the thread looked at, ends support on 7 October 2026 and is not a model
to follow (F+). Our size classes and owner labels fit; an `environment` dimension (dev, staging, prod) that operator
policy could match is missing.

**Tags, prices and expiry.** Google labels are forwarded to billing; AWS's tagging categories include automation tags
for when a resource should be stopped or deleted; FOCUS requires tags in cost data (F). The Cloud Billing Catalog API
lists SKUs with regions, usage units and tiered prices under an API key; Infracost prices Terraform plans from the same
data (F). HCP Terraform's ephemeral workspaces, Pulumi's TTL stacks and Cloud Custodian's mark-for-op all give
infrastructure an expiry with notices (F). Our price table is approximate and marked unverified by design, and no
request has an expiry.

## 5. The Google Cloud adapter, resource by resource

The thread read Google's pages for every resource the adapter creates; where a page rendered only navigation
(the Compute REST reference, the GPU page's lower half, the pricing pages) the claim is marked S.

**Defects.**

- Load balancer, tcp. Google's health checks for a backend-service-based external passthrough NLB come from
  35.191.0.0/16, 209.85.204.0/22 and 209.85.152.0/22 (F+); our firewall rule allows 35.191.0.0/16 and 130.211.0.0/22 for
  both types, so a tcp load balancer's backends never pass their health check. The http rule is right: the global
  external ALB probes from 35.191.0.0/16 and its front ends reach backends from 130.211.0.0/22 (F). Fix: per-type
  ranges in `loadbalancer.py`, and target the rule at the backends by tag or service account rather than the whole
  network.
- Compute with GPUs. Google's driver installation scripts "won't work on Linux compute instances that have Secure Boot
  enabled" (F+); the adapter sets `enableSecureBoot: true` on every machine, and Google's own default leaves it off for
  this reason (F). Fix: Secure Boot off when an accelerator is set (keep vTPM and integrity monitoring), or document the
  signed-driver path.

**Currency.**

- Cloud SQL versions. Supported PostgreSQL is 9.6 to 18 with 18 the default; MySQL 5.6 to 9.7 with 8.4 the default;
  PostgreSQL 13 entered paid extended support on 1 February 2026 and MySQL 5.7 on 1 February 2025 (F+). The adapter
  offers 13 to 17 and 5.7 to 8.4 with defaults 16 and 8.0. Add `POSTGRES_18` and `MYSQL_9_7`, move the defaults, and
  warn on versions in extended support.
- Cloud SQL connectivity. "Private Service Connect is recommended over private services access" and is the console
  default for new projects (F+). The adapter uses PSA. Offer PSC (a service connection policy and `pscConfig`) and keep
  PSA as the fallback; the same choice applies to Memorystore.
- Cloud SQL production safety. `db-f1-micro` and `db-g1-small` are outside the SLA and not for production (F; our plan
  already warns), storage auto-resize is on by default in the console and off through the API, an instance that fills
  its disk goes offline (F), and Google recommends IAM database authentication over built-in users (F). Add
  `storageAutoResize` with a limit, map `small` to a shared-core tier with the warning kept, and offer IAM
  authentication beside the generated password.
- GKE. "Autopilot clusters are the recommended way to use GKE" and new Autopilot clusters are regional (F+); Google
  recommends a custom node service account with `roles/container.defaultNodeServiceAccount` rather than hand-picked
  scopes, Workload Identity Federation (on by default in Autopilot), the Regular channel (our choice), and, for private
  nodes, Cloud NAT and the newer control-plane endpoint configuration (F). Offer Autopilot as the default mode, keep
  Standard for the cases that need it, and refuse or provide NAT with private nodes.
- Memorystore. Google lists Valkey, Redis Cluster, Redis and Memcached (deprecated); Valkey launched with all of Redis
  Cluster's features and uses PSC and IAM authentication (F); for Redis, Google calls PSA "recommended" and direct
  peering, our choice, the default that cannot be changed later (F). Add Valkey as an engine; use PSA for Redis.
- Machine families and disks. Google names no single default family: E2 for lower cost, N4 and N2 for balance, C4 and
  C3 for consistent performance; Hyperdisk is recommended where the series supports it and the newest series take only
  Hyperdisk, while E2 stays on Persistent Disk (F). Our E2-only choice is sound for development work and could gain a performance
  tier on N4 with Hyperdisk, and a Spot option (`provisioningModel: SPOT`, up to 91 percent off, no SLA) (F).
- GPU facts. us-central1-f has no L4 and no H100 (F); the adapter's default zone is the region's first, which is
  us-central1-a, so the default is safe, but a caller choosing a zone is not checked; a3-highgpu-1g, 2g and 4g must be
  Spot or Flex-start (S); T4 is "approaching end of support" (F+; the thread's date of 1 August 2027 is U); G2 memory is
  customisable (F). Check accelerator availability per zone through `acceleratorTypes.aggregatedList`.
- Images. `debian-13` is generally available and `debian-11` ends support on 31 August 2026 (F); `ubuntu-2404-lts-amd64`
  and `rocky-linux-9` remain valid family names, with Rocky 10 and Ubuntu 26.04 images listed on the driver page (S).
- Identity on machines. Attaching no service account is allowed and the warning Google gives is about the default
  service account, not about having one; the Ops Agent cannot report logs or metrics without one (F). Our no-service-
  account choice is defensible as least privilege and should become an option: an identity the request creates
  (section 7), attached with the `cloud-platform` scope and IAM roles.
- Storage, secrets, queues. Cloud Storage's soft delete is on by default for seven days and billed (F): decide the
  policy per bucket. Secret Manager: automatic replication unless residency requires regional, rotation, disable before
  destroy (F). Pub/Sub: the adapter's defaults sit inside Google's ranges and "never expire" is allowed (F).
- Quotas, prices, listing, roles. The Cloud Quotas API gives per-region quota information with dimensions (F); the
  Billing Catalog API v1 lists SKUs for Compute (`6F81-5844-456A`) and Cloud SQL (`9662-B51E-5089`) by region and
  resource group, and the Pricing API is pre-GA (F); Cloud Asset Inventory's `searchAllResources` finds resources by
  label across kinds (F); the creator roles are `compute.instanceAdmin.v1`, `compute.networkAdmin`,
  `compute.securityAdmin`, `cloudsql.admin`, `container.clusterAdmin`, `memorystore.admin` and the storage, Pub/Sub,
  Secret Manager and service-networking admin roles, which the documents should list (F).

**What the adapter already does as Google asks.** Custom-mode VPC with Private Google Access; the IAP range and ports;
OS Login; vTPM and integrity monitoring; uniform bucket access and public-access prevention; the global ALB as
`EXTERNAL_MANAGED`, Premium tier, with no proxy-only subnet (it needs none) and both health-check ranges; encrypted-only
database connections, deletion protection, backups and point-in-time recovery explicitly on (the API defaults are off);
the Enterprise edition set explicitly; a PSA range wider than the required /24; Redis AUTH and TLS; GKE Regular channel,
workload identity pool, shielded and VPC-native nodes; `pd-balanced` on E2; terminate-on-maintenance for GPU machines;
the GPU-to-family mapping; label syntax; a wait loop that re-checks completion until a deadline (F).

## 6. What the research says

**Not generating code is the right boundary.** IaC-Eval (NeurIPS 2024) measured GPT-4 at 19 percent pass@1 on 458 AWS
infrastructure scenarios against 87 percent on a code benchmark; DPIaC-Eval (2025) found first-attempt deployability
of 21 to 30 percent, intent coverage of 25 percent and security compliance of 8 percent; AWS's Multi-IaC-Eval (2025)
found syntactic validity above 95 percent with semantic alignment the remaining failure; a 2026 Brazilian study found
validity and security orthogonal; a 2025 study found knowledge injection raises success but intent alignment plateaus
(F). The seven security smells of IaC scripts (ICSE 2019: admin by default, empty and hard-coded secrets, suspicious
comments, HTTP, weak crypto) are unrepresentable in a typed contract with no strings that can carry them (F). The
agent-safety work (ToolEmu at ICLR 2024, AgentDojo at NeurIPS 2024, Google's CaMeL and Progent in 2025) converges on
enforcing policy deterministically at the tool-call boundary over tool names and arguments, with privilege expansion
needing explicit approval (F). This is the design we have.

**Verify, then act.** A 2026 "verifier-first" evaluation found 79 percent of residual policy failures came from policy
text the model never saw (F), which argues for returning the applied defaults and any violated policy inside the plan,
so the agent can repair before provisioning. The NetOps and AIOps survey (Computer Science Review, 2026) finds evidence
strong for read-only assistance and weak past configuration change, and proposes an "assurance contract": autonomy
level tied to permitted tools, independent gates, execution budgets, rollback duties and audit (F). ITBench (ICML 2025)
scored agents at 14 percent on SRE tasks and 0 percent on FinOps tasks, which is the case for a ceiling the service
enforces rather than the agent (F).

**Reconcile and detect drift.** Sieve (OSDI 2022) found 46 bugs in ten Kubernetes controllers by perturbing their view
of state, resource leaks among them; Anvil (OSDI 2024) gives "eventually stable reconciliation" as the correctness
property (F). Our state is write-once: a read-only reconcile that diffs the stored request against live state and
yields a `DRIFTED` status, and a conformance check after provisioning (count, sizes, ports, visibility), are the
natural additions.

**Treat the catalog as untrusted data.** The 2025 MCP threat analyses (sixteen threats across the lifecycle; agents
trusting tool descriptions blindly; descriptor manipulation causing unsafe calls in up to 36 percent of trials, cut to
15 percent by descriptor integrity and vetting) (F) argue for signing or hashing the rendered catalog so clients can
pin it, which the sibling Fast Deploy server already does with a checksum, and for keeping provider messages bounded
and labelled as data in `details`, which we do.

**Abstraction without the lowest common denominator.** CloudBridge (XSEDE 2016) rejected Libcloud's lowest-common-
denominator design in favour of a required minimal feature set with one conformance suite run against every provider;
the TOSCA orchestrator comparison (2020) found universal orchestrators break on provider specifics; TOSCA 2.0 (OASIS,
December 2024) models requirements against capabilities and derives workflows from the graph (F). Our sizes, counts and
switches with provider-side translation and `supported_kinds` per adapter are the CloudBridge stance; the capability
matrix should grow per field (what a provider cannot offer within a kind) and the estimate needs a unit, because
Jetstream2 prices in service units.

**Jetstream2 is tractable.** It is OpenStack with Ceph, fixed flavors (m3, r3, g3 with fractional A100 through vGPU,
full A100, L40S and H100 by passthrough), one Keystone application credential per allocation with an expiry, a DNS zone
per project, security groups and Octavia load balancers, and no documented balance API (F). An adapter would translate
cpu and memory into the nearest flavor and report it, take the credential from host configuration as today, and need
fractional GPUs and SU accounting in the contract.

**Gaps the literature leaves.** No peer-reviewed study measures agents calling a typed provisioning API rather than
writing IaC text; the benefit above is inferred. Drift and idempotency evidence for cloud APIs is thin. Cost-estimation
accuracy is measured on AWS compute and network only (87 and 65 percent against invoices), not on managed services or
research-cloud budgeting. Nobody has measured confirmation fatigue for agent-initiated provisioning.

## 7. The most common resources, and what the catalog should add

No provider publishes usage by resource type, so the thread triangulated: Terraform Registry provider downloads and the
stars of the official AWS and Google module collections (a proxy for what people wrap), the Stack Overflow 2025, CNCF
2024, HashiCorp 2024 and Flexera 2026 surveys, Datadog's container, serverless and cost reports, Google's, AWS's and
Azure's reference architectures, the Score and Humanitec resource types, and the research-computing catalogs
(Jetstream2, CloudBank, several university research-computing services). Stars and downloads skew to AWS and to
enterprises; the operator's own request log would be the better sample once there is one.

| # | Kind | Evidence | Ours |
|---|---|---|---|
| 1 | Network: VPC, subnets, firewall | the second most-starred AWS module and the first thing every reference architecture creates | present; no NAT, VPN, bastion |
| 2 | Virtual machines, with GPUs | VM-first research clouds; GPU hours tripled 2023 to 2025 in Datadog's data | present; no spot, images, autoscaling |
| 3 | Managed Kubernetes | the most-starred AWS module; 80 percent run Kubernetes in production (CNCF) | present; one zonal pool, no GPU pool |
| 4 | Managed relational database | PostgreSQL used by 56 percent of developers (Stack Overflow 2025) | present; no replicas, restore |
| 5 | Identities and role bindings | Google's three-tier sample creates a service account and bindings before anything else | absent |
| 6 | Object storage | 12-factor backing service; Jetstream2 and the Open Storage Network | present; no lifecycle, CORS |
| 7 | Serverless containers and functions | 70 percent of Google Cloud customers use Cloud Run; 55 percent run serverless in production | absent on purpose: needs an image |
| 8 | Monitoring, alerting, log sinks | Datadog is the most-downloaded partner provider; Prometheus at 73 percent in production | absent (we read usage, create nothing) |
| 9 | Secrets | Vault provider at 970 million downloads; secrets leakage a top named threat | present |
| 10 | Load balancer and routes | Score and Humanitec `route`; the Azure baseline terminates TLS at the gateway | present; http and tcp, no TLS |
| 11 | TLS certificates | the tls provider at over a billion downloads; an https load balancer requires a certificate | absent |
| 12 | DNS zones and records | Cloudflare and Route 53 providers; Jetstream2 gives every project a zone | absent |
| 13 | Cache | Redis used by 28 percent of developers | present |
| 14 | Queues, topics, streams | Confluent the second partner provider; Score `amqp` and `kafka-topic` | present; pull only, no dead letter |
| 15 | Block disks | the largest single line of EBS spend; Score `volume` | present |
| 16 | Container registry | enabled by every Google solution; Harbor at 20 percent | absent |
| 17 | Shared file system | Filestore, Manila on Jetstream2, Lustre on campus clusters | absent |
| 18 | NoSQL and document databases | MongoDB 24 percent, DynamoDB 10 percent, Firestore 6 percent | absent |
| 19 | Data warehouse | 71 percent use a cloud warehouse (Flexera 2026) | absent |
| 20 | NAT, VPN, bastion, private connectivity | private machines need NAT to fetch packages (Google) | absent |

Below the line: scheduled jobs, CDN, batch and Slurm clusters, notebooks, email, snapshots and images, and project
creation (the most-starred pure Google module).

**The next batch, in order.** Identity first: a service account per request plus role bindings on the resources the
same service created (bucket, secret, queue, database), so that machines and clusters can use what was provisioned next
to them; bindings are grants, not credentials, so the "no value passes through" rule holds. Then DNS records and
certificates, with TLS termination on the load balancer, which turns it into a usable endpoint. Then a NAT switch on
the network, which removes the private-machine trap that `public_ip: false` and `private_nodes` set today. Then
monitoring: an uptime check, an alert policy, a notification channel and a log sink, which the service already knows
how to read. Then a container registry, a shared file system (Filestore, Manila on Jetstream2), and a schedule that
targets a queue. NoSQL and the warehouse are easy on Google Cloud and portable with difficulty; they come after.
Serverless containers stay in the delivery workflow.

**What the research context adds.** Fractional GPUs (vGPU and MIG slices) and an `accelerator.fraction`, allocation
accounting in service units with a balance where the provider exposes one, snapshots and images as a kind (teaching
uses them), a batch cluster kind (head node, workers, shared file system), and notebook and inference endpoints, which
are application-layer concerns on a machine except where a provider sells them managed.

## 8. What to change, ranked

Effort: S is hours, M is a day or two, L is a week or more. Corrections come first because they are wrong today.

| # | Change | Where | Effort | Why (section) |
|---|---|---|---|---|
| 1 | tcp load balancer: allow 209.85.204.0/22 and 209.85.152.0/22 for passthrough health checks; target the rule by tag | `providers/gcp/loadbalancer.py` | S | 5, defect |
| 2 | GPU machines: Secure Boot off (vTPM and integrity kept), or the signed-driver path documented | `providers/gcp/compute.py`, docs | S | 5, defect |
| 3 | Failed results carry `isError: true` with the structured payload; the SDK-level validation refusal too | `security/auth.py`, `mcp/server.py` | S | 3 |
| 4 | Cloud SQL: add PostgreSQL 18 and MySQL 9.7, defaults 18 and 8.4, warn on extended-support versions; storage auto-resize; IAM authentication option | `providers/gcp/translate.py`, `database.py` | S | 5 |
| 5 | Idempotency key on `provision_infrastructure` and the primitives (UUID; replay, 409 while running, 422 on mismatch, published retention); `requestId` on Compute inserts and deletes | `orchestration/provisioner.py`, `state`, `providers/gcp/api.py` | M | 4 |
| 6 | Live prices from the Billing Catalog API, cached daily, `effectiveTime` in `verified`; exclusions and a confidence note in the estimate | `providers/gcp/pricing.py` | M | 4, 6 |
| 7 | Model-facing descriptions of three or more sentences composed from purpose, output, security and the next tool; `input_examples` on the three request-taking tools | `mcp/catalog.py`, `mcp/tools/_common.py` | S | 3 |
| 8 | Two-step destroy with a principal-bound, short-lived confirmation token, and a global `allow_destroy` switch; honest `idempotentHint` on deletes (or a truly idempotent delete) | `orchestration/provisioner.py`, `config.py`, catalog | M | 3 |
| 9 | Private Service Connect for Cloud SQL with PSA as fallback; Valkey as a cache engine; PSA for Redis | `providers/gcp/network.py`, `database.py`, `cache.py` | M | 5 |
| 10 | GKE Autopilot (regional) as the default mode; custom node service account with the default node role; the current control-plane endpoint configuration; NAT with private nodes | `providers/gcp/cluster.py` | M | 5 |
| 11 | Deliver the database password as a secret reference (a Secret Manager secret the request owns) or a binding, instead of once in a response that can be lost | `orchestration/provisioner.py`, `providers/gcp/database.py` | M | 4 |
| 12 | Drift: a read-only reconcile that diffs the stored request against live state and yields `DRIFTED`; a conformance field in status after provisioning | `orchestration` | M | 6 |
| 13 | A policy report in the plan: defaults applied, policies violated, cost against the ceiling | `orchestration/planner.py` | S | 6 |
| 14 | `get_resource` and `delete_resource` replacing the nine pairs (43 tools to 27); a real `outputSchema` from the response models; redacted arguments in the audit row | `mcp/tools`, `security/auth.py` | M | 3 |
| 15 | Expiry: `expires_at` on a request, a `cloudinfra-expires` label, a sweeper operation with notices | `models`, `orchestration` | M | 4 |
| 16 | Update of a whole request: diff the stored request, apply in creation order, refuse replacement-type changes, `UPDATING` status | `orchestration` | L | 4 |
| 17 | Zone-aware accelerator checks (`acceleratorTypes.aggregatedList`), Spot option, `debian-13`, a performance tier on N4 with Hyperdisk | `providers/gcp/translate.py`, `compute.py` | S to M | 5 |
| 18 | Catalog integrity: a checksum of the rendered tool list that clients can pin, as Fast Deploy's `toolpin` does | `mcp/catalog.py`, CLI | S | 6 |
| 19 | OAuth resource metadata and scopes (`infra:read`, `infra:provision`, `infra:destroy`) in front of the Fast Deploy authorization server; expiry and rotation for static tokens | `mcp/server.py`, `security/auth.py` | M | 3 |
| 20 | Move to the 2.x SDK and the 2026-07-28 revision (sessionless; `server/discover`), with the Tasks extension when the SDK ships it | `pyproject.toml`, `mcp/server.py` | M | 3 |
| 21 | Per-request deletion policy (delete or orphan), recorded dependencies for destroy order, adoption of existing resources by provider id | `orchestration`, `state` | M | 4 |
| 22 | An `environment` dimension that operator policy matches (limits, ceilings, HA defaults) | `models`, `config.py` | M | 4 |
| 23 | The next kinds: identity, dns and certificate, nat, monitor, registry, filesystem, schedule | new adapters | L | 7 |
| 24 | A contract-level benchmark: scenario to expected plan, counting unsafe calls, against the fake provider | `tests/cloudinfra` | M | 6 |
| 25 | Cloud Quotas API for quota, Asset Inventory for cross-kind listing, Resource Manager tags where IAM conditions need them, the creator roles listed in the documents | `providers/gcp`, docs | S | 5 |

Items 1 to 4 and 7 fit one small release; 5, 6, 8 and 11 are the next operational step; 23 is the coverage work that
section 7 orders.

## 9. What the layer does right, consolidated

For the review's record: the typed, closed contract and the refusal of unknown kinds with the nearest kind; the
separation of validate, plan and provision with the plan computed offline; cleanup on failure in reverse with the
remainders named, and a retryable destroy; the refusal to delete a network with tenants; ownership labels in Google's
format on everything that takes labels, and a description on what does not; operator limits, a cost ceiling per service
and per caller, a cap on live requests, and a quota check before creation; the provider credential from the host and
never from a caller; hashed principals with provisioning and destruction rights and a scope that hides other callers'
requests; per-identity and per-address budgets and an audit row per call; structured errors with codes everywhere, and
provider messages kept as bounded data; annotations and strict argument checking on every tool; status refreshed from
the provider without reverting drift; the private, encrypted, protected defaults on databases and caches, OS Login and
shielded settings on machines, and least-privilege scopes on cluster nodes; and the adapter boundary, which a test holds
so that Google is named nowhere else.

## 10. Limits of this review

The threads read documentation and papers; no live Google Cloud project was touched and nothing was provisioned, so
the two defects are read from the pages, not reproduced. Several Google reference pages render only navigation to a
fetcher (the Compute REST reference, the lower half of the GPU page, the pricing pages), so the `requestId` semantics,
the T4 end-of-support date and the Spot-only A3 shapes are secondary. The demand ranking rests on proxies (module stars,
provider downloads, surveys) because no provider publishes usage by resource type. The MCP peer servers were read from
their repositories on 2026-10-28 and change quickly. The literature on agents calling typed provisioning APIs does not
exist yet; the benefit claimed in section 6 is inferred from the text-generation benchmarks.

## 11. Sources

F: read on the official page or the paper by the research thread. F+: also re-read by the reviewer. S: secondary or
search snippet only. U: unverified.

**MCP, Anthropic, OWASP, peers**

| Source | Mark |
|---|---|
| [MCP versioning](https://modelcontextprotocol.io/specification/versioning), [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog) | F+ / F |
| [MCP tools, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) | F+ / F |
| [MCP tasks, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks), [tasks extension 2026-07-28](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks) | F |
| [MCP authorization 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization), [security best practices](https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices), [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr), [elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation) | F |
| [mcp on PyPI](https://pypi.org/project/mcp/), [python-sdk releases](https://github.com/modelcontextprotocol/python-sdk/releases) | F+ / F |
| [Claude: define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools), [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), [MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector), [custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) | F+ / F / F / F |
| [OWASP MCP Top 10](https://owasp.org/www-project-mcp-top-10/), [MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html), [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) | F / F / S |
| [Terraform MCP server](https://github.com/hashicorp/terraform-mcp-server), [reference](https://developer.hashicorp.com/terraform/docs/tools/mcp-server/reference); AWS Labs EKS, ECS and Cloud Control servers (GitHub); [Cloud Run MCP](https://github.com/GoogleCloudPlatform/cloud-run-mcp); [Google managed MCP and IAM](https://docs.cloud.google.com/mcp/control-mcp-use-iam); [Pulumi MCP server](https://www.pulumi.com/docs/iac/using-pulumi/mcp-server/); containers/kubernetes-mcp-server and Flux159 (GitHub) | F |

**Provisioning patterns**

| Source | Mark |
|---|---|
| [Open Service Broker API spec](https://github.com/openservicebrokerapi/servicebroker/blob/master/spec.md) | F |
| Crossplane: [managed resources](https://docs.crossplane.io/latest/managed-resources/managed-resources/), [deletion policy v1.20](https://docs.crossplane.io/v1.20/concepts/managed-resources/), [XRDs](https://docs.crossplane.io/latest/composition/composite-resource-definitions/), [what's new](https://docs.crossplane.io/latest/whats-new/), [usages](https://docs.crossplane.io/v1.20/concepts/usages/), [import](https://docs.crossplane.io/latest/guides/import-existing-resources/) | F, import S |
| [Terraform state purpose](https://developer.hashicorp.com/terraform/language/state/purpose), [plan](https://developer.hashicorp.com/terraform/cli/commands/plan), [import](https://developer.hashicorp.com/terraform/cli/import), [drift tutorial](https://developer.hashicorp.com/terraform/tutorials/state/resource-drift), [OpenTofu state](https://opentofu.org/docs/language/state/purpose/) | F |
| [Pulumi Automation API](https://www.pulumi.com/docs/iac/using-pulumi/automation-api/), [state and backends](https://www.pulumi.com/docs/iac/concepts/state-and-backends/), [TTL stacks](https://www.pulumi.com/docs/pulumi-cloud/deployments/ttl/) | F |
| [Infrastructure Manager overview](https://docs.cloud.google.com/infrastructure-manager/docs/overview), [deployments](https://docs.cloud.google.com/infrastructure-manager/docs/reference/rest/v1/projects.locations.deployments), [Config Connector](https://docs.cloud.google.com/config-connector/docs/overview) | F |
| [CNCF Platforms white paper](https://tag-app-delivery.cncf.io/whitepapers/platforms/), [Platform engineering maturity model](https://tag-app-delivery.cncf.io/whitepapers/platform-eng-maturity-model/) | F |
| [Score specification](https://docs.score.dev/docs/score-specification/score-spec-reference/), [Humanitec resource definitions](https://developer.humanitec.com/platform-orchestrator/docs/platform-orchestrator/resources/resource-definitions/) | F |
| AWS Service Catalog: [introduction](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/introduction.html), [template constraints](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/catalogs_constraints_template-constraints.html), [ProvisionProduct](https://docs.aws.amazon.com/servicecatalog/latest/dg/API_ProvisionProduct.html); [AWS Proton end of support](https://docs.aws.amazon.com/proton/latest/userguide/Welcome.html); [Backstage templates](https://backstage.io/docs/features/software-templates/writing-templates) | F / F+ / F |
| [Idempotency-Key draft-08](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header), [AIP-155](https://google.aip.dev/155), [EC2 idempotency](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-idempotency.html) | F+ / F / F |
| [AIP-151](https://google.aip.dev/151), [AIP-193](https://google.aip.dev/193), [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html), [Azure async request-reply](https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply) | F |
| [AWS tag categories](https://docs.aws.amazon.com/tag-editor/latest/userguide/tag-categories.html), [Google labels](https://docs.cloud.google.com/resource-manager/docs/labels-overview), [FOCUS tags](https://focus.finops.org/docs/specification/v1-3/columns/cost-and-usage/tags) | F |
| [Cloud Billing Catalog API](https://docs.cloud.google.com/billing/v1/how-tos/catalog-api), [Pricing API](https://docs.cloud.google.com/billing/docs/how-to/get-pricing-information-api), [Infracost](https://www.infracost.io/docs/) | F |
| [HCP Terraform ephemeral workspaces](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings/deletion), [Cloud Custodian mark-for-op](https://cloudcustodian.io/docs/aws/resources/aws-common-actions.html) | F |

**Google Cloud**

| Source | Mark |
|---|---|
| [Machine families](https://docs.cloud.google.com/compute/docs/machine-resource), [general-purpose machines](https://docs.cloud.google.com/compute/docs/general-purpose-machines), [GPUs](https://docs.cloud.google.com/compute/docs/gpus), [GPU regions and zones](https://docs.cloud.google.com/compute/docs/gpus/gpu-regions-zones), [Spot VMs](https://docs.cloud.google.com/compute/docs/instances/create-use-spot) | F / F / F+ (partial) / F / F |
| [Install GPU drivers](https://docs.cloud.google.com/compute/docs/gpus/install-drivers-gpu), [Shielded VM options](https://docs.cloud.google.com/compute/shielded-vm/docs/modifying-shielded-vm) | F+ / F |
| [OS Login](https://docs.cloud.google.com/compute/docs/oslogin), [service accounts on VMs](https://docs.cloud.google.com/compute/docs/access/service-accounts), [Ops Agent authorization](https://docs.cloud.google.com/monitoring/agent/ops-agent/authorization), [IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding) | F |
| [instances.insert requestId](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert), [zoneOperations.wait](https://docs.cloud.google.com/compute/docs/reference/rest/v1/zoneOperations/wait) | S (pages did not render) |
| [OS details](https://docs.cloud.google.com/compute/docs/images/os-details), [disks](https://docs.cloud.google.com/compute/docs/disks), [labels](https://docs.cloud.google.com/compute/docs/labeling-resources), [tags overview](https://docs.cloud.google.com/resource-manager/docs/tags/tags-overview) | F (Ubuntu and Rocky rows S) |
| Cloud SQL: [private IP](https://docs.cloud.google.com/sql/docs/postgres/private-ip), [configure PSC](https://docs.cloud.google.com/sql/docs/postgres/configure-private-service-connect), [configure PSA](https://docs.cloud.google.com/sql/docs/postgres/configure-private-services-access), [IAM authentication](https://docs.cloud.google.com/sql/docs/postgres/iam-authentication), [editions](https://docs.cloud.google.com/sql/docs/postgres/editions-intro), [database versions](https://docs.cloud.google.com/sql/docs/db-versions), [instance settings](https://docs.cloud.google.com/sql/docs/postgres/instance-settings), [instances resource](https://docs.cloud.google.com/sql/docs/postgres/admin-api/rest/v1/instances) | F+ / F / F / F / F / F+ / F / F |
| Memorystore: [landing](https://docs.cloud.google.com/memorystore/docs), [Valkey networking](https://docs.cloud.google.com/memorystore/docs/valkey/networking), [Valkey IAM auth](https://docs.cloud.google.com/memorystore/docs/valkey/about-iam-auth), [Redis versions](https://docs.cloud.google.com/memorystore/docs/redis/supported-versions), [Redis networking](https://docs.cloud.google.com/memorystore/docs/redis/networking), [Valkey launch post](https://cloud.google.com/blog/products/databases/announcing-memorystore-for-valkey) | F, launch post S |
| GKE: [Autopilot overview](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-overview), [cluster types](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/types-of-clusters), [release channels](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/release-channels), [workload identity](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity), [hardening](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/hardening-your-cluster), [access scopes](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/access-scopes), [network isolation](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/latest/network-isolation), [choose compute options](https://docs.cloud.google.com/docs/compute-area/choose-compute-options) | F+ / F / F / F / F / F / F / F |
| Networking: [VPC design best practices](https://docs.cloud.google.com/architecture/best-practices-vpc-design), [firewall rules](https://docs.cloud.google.com/firewall/docs/firewalls), [network firewall policies](https://docs.cloud.google.com/firewall/docs/network-firewall-policies), [Cloud NAT](https://docs.cloud.google.com/nat/docs/overview) | F |
| Load balancing: [external ALB](https://docs.cloud.google.com/load-balancing/docs/https), [proxy-only subnets](https://docs.cloud.google.com/load-balancing/docs/proxy-only-subnets), [forwarding rules](https://docs.cloud.google.com/load-balancing/docs/forwarding-rule-concepts), [backend services](https://docs.cloud.google.com/load-balancing/docs/backend-service), [health checks](https://docs.cloud.google.com/load-balancing/docs/health-check-concepts), [passthrough NLB](https://docs.cloud.google.com/load-balancing/docs/network), [SSL certificates](https://docs.cloud.google.com/load-balancing/docs/ssl-certificates), [Certificate Manager](https://docs.cloud.google.com/certificate-manager/docs/overview) | F / F / F / F / F+ / F / F / F |
| [Pub/Sub subscription properties](https://docs.cloud.google.com/pubsub/docs/subscription-properties), [Secret Manager best practices](https://docs.cloud.google.com/secret-manager/docs/best-practices), [regional secrets](https://docs.cloud.google.com/secret-manager/docs/secret-manager-secrets-comparison), [Cloud Storage soft delete](https://docs.cloud.google.com/storage/docs/soft-delete), [Autoclass](https://docs.cloud.google.com/storage/docs/autoclass) | F |
| [Cloud Quotas API](https://docs.cloud.google.com/docs/quotas/api-overview), [Asset Inventory search](https://docs.cloud.google.com/asset-inventory/docs/searching-resources), [Compute IAM](https://docs.cloud.google.com/compute/docs/access/iam), [Cloud SQL IAM roles](https://docs.cloud.google.com/sql/docs/postgres/iam-roles), [GKE IAM](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/iam), [service account best practices](https://docs.cloud.google.com/iam/docs/best-practices-service-accounts) | F |

**Research**

| Source | Mark |
|---|---|
| [OASIS TOSCA v2.0 cs01, December 2024](https://docs.oasis-open.org/tosca/TOSCA/v2.0/cs01/TOSCA-v2.0-cs01.html); [Luzar, Stanovnik, Cankar, TOSCA orchestration tools, 2020](https://zenodo.org/record/4134091); [Challita et al., TOSCA and OCCI, 2020](https://arxiv.org/abs/2001.07900); Ferry et al., CloudML, IEEE CLOUD 2013 (abstract); [Brogi et al., SeaClouds, 2015](https://research.birmingham.ac.uk/en/publications/adaptive-management-of-applications-across-multiple-clouds-the-se/); [Goonasekera et al., CloudBridge, XSEDE16](https://pmc.ncbi.nlm.nih.gov/articles/PMC8375622); [Elkhatib, cross-cloud systems, 2016](https://arxiv.org/abs/1602.02698) | F |
| Petcu et al., mOSAIC, 2013; Cloud4SOA, 2013; Ré et al., FGCS 2018 (jclouds overhead); Grozev and Buyya, SPE 2014 | S (paywalled) |
| [Rahman, Mahdavi-Hezaveh, Williams, IaC mapping, IST 2019](https://arxiv.org/abs/1807.04872); Rahman, Parnin, Williams, The Seven Sins, ICSE 2019; [Rahman and Williams, defect prediction, IST 2019](https://arxiv.org/abs/1810.09605); [Verdet et al., Terraform security practices, 2023](https://arxiv.org/abs/2308.03952); Saavedra and Ferreira, GLITCH, ASE 2022; Kumara et al., IaC do's and don'ts, IST 2021 | F |
| Hummer et al., testing idempotence for IaC, Middleware 2013; Opdebeeck et al., Ansible smells, MSR 2022 | S |
| [Sun et al., Sieve, OSDI 2022](https://www.usenix.org/conference/osdi22/presentation/sun); Sun et al., Anvil, OSDI 2024 | F |
| Kon et al., IaC-Eval, NeurIPS 2024; [Zhang et al., DPIaC-Eval, 2025](https://arxiv.org/abs/2506.05623); [Davidson et al., Multi-IaC-Eval, 2025](https://arxiv.org/abs/2509.05303); [Nekrasov et al., 2025](https://arxiv.org/abs/2512.14792); [Jouini, verifier-first evaluation, 2026](https://arxiv.org/abs/2607.20478); [Vargas, Mansilha, Kreutz, SBSeg 2026](https://sol.sbc.org.br/index.php/sbseg/article/view/44342) | F |
| [Chen et al., AIOpsLab, 2025](https://arxiv.org/abs/2501.06706); [Jha et al., ITBench, ICML 2025](https://arxiv.org/abs/2502.05352); [Bilal et al., LLMs for agentic NetOps and AIOps, Computer Science Review 2026](https://arxiv.org/abs/2605.12729) | F |
| [Ruan et al., ToolEmu, ICLR 2024](https://arxiv.org/abs/2309.15817); Debenedetti et al., AgentDojo, NeurIPS 2024; [Debenedetti et al., CaMeL, 2025](https://arxiv.org/abs/2503.18813); [Shi et al., Progent, 2025](https://arxiv.org/abs/2504.11703); [Beurer-Kellner, Fischer, Vechev, DOMINO, ICML 2024](https://arxiv.org/abs/2403.06988) | F |
| [Hou et al., MCP landscape and threats, 2025](https://arxiv.org/abs/2503.23278); [Guo et al., MCPXKIT, IEEE TDSC](https://arxiv.org/abs/2508.12538); [Jamshidi et al., descriptor manipulation, 2025](https://arxiv.org/abs/2512.06556); [Narajala and Habler, 2025](https://arxiv.org/abs/2504.08623) | F (last abstract only) |
| Hancock et al., Jetstream2, PEARC 2021 (NSF PAR copy); [Jetstream2 documentation](https://docs.jetstream-cloud.org/); [Pistorius et al., Exosphere, SC20](https://arxiv.org/abs/2008.10640); Skidmore et al., CACAO, PEARC 2023; Norman et al., CloudBank, PEARC 2021 | F / F / F / F / S |
| Djouela Kamgang, Elhabbash, Elkhatib, PricingTheCloud, GECON 2024; Wu, Buyya, Ramamohanarao, cloud pricing survey, ACM CSUR 2019; Cortez et al., Resource Central, SOSP 2017; Rzadca et al., Autopilot, EuroSys 2020; Jeon et al., Philly, ATC 2019; Weng et al., MLaaS in the wild, NSDI 2022; [Kim et al., Flex-MIG, 2025](https://arxiv.org/abs/2511.09143); [Xu et al., SING, ASPLOS 2025](https://arxiv.org/abs/2110.01556) | F, survey S |

**Demand**

| Source | Mark |
|---|---|
| [Terraform Registry providers by downloads (v2 API)](https://registry.terraform.io/v2/providers?filter[tier]=partner&sort=-downloads); [terraform-aws-modules stars (GitHub API)](https://api.github.com/search/repositories?q=org:terraform-aws-modules&sort=stars&order=desc&per_page=25); terraform-google-modules stars (GitHub API) | F |
| [Stack Overflow Developer Survey 2025](https://survey.stackoverflow.co/2025/technology); [CNCF Annual Survey 2024](https://www.linuxfoundation.org/hubfs/Research%20Reports/cncf_annual_survey24_031225a.pdf); [HashiCorp State of the Cloud](https://www.hashicorp.com/en/state-of-the-cloud); [Flexera State of the Cloud 2026](https://info.flexera.com/CM-REPORT-State-of-the-Cloud); [Datadog containers and serverless](https://www.datadoghq.com/state-of-containers-and-serverless/); [Datadog cloud costs](https://www.datadoghq.com/state-of-cloud-costs/) | F |
| [Google three-tier web app (Terraform source)](https://raw.githubusercontent.com/GoogleCloudPlatform/terraform-google-three-tier-web-app/main/main.tf); [Google dynamic Python web app](https://raw.githubusercontent.com/GoogleCloudPlatform/terraform-dynamic-python-webapp/main/README.md); [AWS serverless multi-tier patterns](https://docs.aws.amazon.com/whitepapers/latest/serverless-multi-tier-architectures-api-gateway-lambda/sample-architecture-patterns.html); [Azure App Service baseline](https://learn.microsoft.com/en-us/azure/architecture/web-apps/app-service/architectures/baseline-zone-redundant) | F |
| [Score provisioners](https://docs.score.dev/docs/score-implementation/score-compose/resources-provisioners/); Humanitec resource types | F / S |
| [Jetstream2 storage](https://docs.jetstream-cloud.org/general/storage/), [Jetstream2 DNS](https://docs.jetstream-cloud.org/ui/horizon/dns/); [Filestore overview](https://docs.cloud.google.com/filestore/docs/overview); CloudBank catalog; university research-computing catalogs (UTSA Arc, Stanford, Harvard, Berkeley, Yale) | F |
