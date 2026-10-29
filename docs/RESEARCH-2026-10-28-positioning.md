# Where the infrastructure layer stands, and what would make it practical. cloudinfra 1.36.1 on 2026-10-28

Written 2026-10-28 UTC as a read-only review of the Cloud Infrastructure MCP (`cloudinfra`, version 1.36.1, commit
`984ec36` on `main`). Nothing in the code was changed; one sentence of the previous review was corrected (section 8). The
question asked was the operator's: where does this service stand among the tools people use today, and what can we add
or change to make it better and more practical? It follows `docs/RESEARCH-2026-10-28-infrastructure-layer.md`, which
asked whether the layer is built right. This one asks whether it is useful, to whom, and what stands between it and
daily use. The service is meant for industry: developers and platform teams in companies. A first draft assumed a
university research lab; the operator corrected that, and this version is written for companies (section 8).

Method: four themed research threads ran in parallel, each reading primary pages and reporting with marked sources. The
first mapped the tools that give an AI agent or a developer cloud infrastructure: the cloud vendors' own MCP servers, the
infrastructure-as-code vendors' agents, the design assistants and the spec-driven deploy tools. The second studied what
makes self-service infrastructure practical every day in Humanitec, Port, Backstage, Kratix, Crossplane, Massdriver,
env0, Spacelift, Azure Deployment Environments, Google's Application Design Center, AWS Service Catalog, Coder, GitHub
Codespaces, Qovery, Northflank, CloudLab and Chameleon. The third studied what research teams need from cloud machines,
GPUs above all (SkyPilot, dstack, Jetstream2, Google's GPU images); it was briefed for a university lab, so its findings
are used here only where they apply to companies too. The fourth asked what it takes to run the service for real and to
use it through Claude: connectors, skills, the MCP registry, hosting, the service's own identity, people's access to what
it creates, and software on its machines. The reviewer then re-read on the primary page the claims that would change a
recommendation and measured the tool list the service publishes. Section 10 marks each source F (read on the page by a
thread), F+ (re-read by the reviewer), S (a snippet or secondary source) or U (unverified).

Acronyms: SSO (single sign-on through a company's identity provider, such as Google Workspace, Okta or Entra ID), RBAC
(role-based access control), IAP (Identity-Aware Proxy, Google's front door for SSH without public addresses), OS Login
(Google's IAM-based login to machines), DLVM (Deep Learning VM, Google's images with GPU drivers and machine-learning
frameworks preinstalled), TTL (time to live), chargeback (billing each team or cost center for what it used), Spot and
Flex-start (Google's discounted capacity that can be reclaimed, and its queued capacity that runs for up to seven days).

## 1. The short answer

The core is sound and distinctive; the product around it is not yet ready for a company.

**Where it stands.** The cloud vendors now ship their own MCP servers that create and delete infrastructure. Google
lists remote MCP servers for Compute Engine, GKE, Cloud SQL, Memorystore, Pub/Sub, Filestore and about sixty other
products; Microsoft's Azure MCP Server acts with the user's Azure credentials and role assignments; AWS's managed server
runs the agent's Python in a sandbox with the caller's IAM permissions. All of them act as the caller's own cloud
identity and keep no record of what an agent made. The platform products (HCP Terraform, Pulumi, Spacelift, env0, Port,
Humanitec, Massdriver) do govern self-service, several with agent interfaces, but they need modules or templates written
first and run as paid control planes. The closest open-source design to ours, Spacelift Intent, an MCP server that
provisioned by calling provider APIs and kept its state in SQLite, was archived on the day of this review.

What sets the service apart: a typed, provider-neutral request for nine kinds of infrastructure with nothing executable
in it, a plan and a price before anything exists, a dollar ceiling the service enforces, the cloud credential held by the
service, and a lifecycle it owns, with no modules to write and no paid control plane. That is a credible position:
**a self-hosted, governed infrastructure API for AI agents.** A company's platform team sets the regions, shapes,
prices and rights once; developers' agents get typed blocks, never cloud credentials, scripts or images; everything made
is priced, labelled, cleaned up and destroyable.

**What a company would miss today.** Against what companies expect of platform tooling, it is a prototype. It has never
run on a real project and keeps its state in one SQLite file on one machine. Callers use static tokens: there is no SSO
with the company's identity provider, no teams and no RBAC. There are no environments such as dev, staging and prod with
their own rules, no approvals, no budgets per team or cost center, and only Google Cloud, where most companies also or
mainly run AWS. There is no update, drift detection, import or Terraform export. And for the developer on the other end:
they cannot log in to what they get, the database password comes back in the chat, a dev environment runs until someone
deletes it, there are no approved golden images, and machines cannot read the bucket created beside them.

**What to do.** Three releases (section 4):

1. **Usable by a developer (release A, now):** login access and a "how to connect" block, credentials delivered through
   the secret manager instead of the chat, golden-image profiles chosen by the platform team, GPU images with signed
   drivers among them, and stop, start, expiry and idle stop as operator opt-ins; plus running it for real in the
   company's project.
2. **Governed for a company (B):** teams with leads and RBAC, environments with their own limits, lifetimes and approval
   rules, budgets per team or cost center with chargeback labels and actual spend, approvals, notifications, audit
   export and presets.
3. **Enterprise-ready and multi-cloud (C):** SSO with the company's identity provider and per-person sign-in from Claude,
   durable and highly available state, an AWS adapter and then Azure, day-two changes with drift detection, import and
   Terraform export, and the next kinds: identities, DNS and certificates, NAT and monitoring.

| Area | Today | After the three releases |
|---|---|---|
| Proof | Never run on a real project | Running in the company's project; integration suite and an end-to-end scenario passed |
| Identity | Static tokens per caller | SSO with the company's identity provider; per-person sign-in from Claude |
| People | Principals | Teams with leads, RBAC, approvals, notifications |
| Environments | None | dev, staging and prod with their own limits, lifetimes and approvals |
| Access | Addresses returned; nobody can log in | Requester and named members granted; connect commands returned |
| Credentials | Database password returned once in the response | Written to the secret manager; the response names the secret |
| Software | Plain OS images | Golden images from the platform team; GPU images with signed drivers |
| Time | Runs until destroyed | Stop and start, expiry with warnings, idle stop; opt-in per operator, later per environment |
| Money | One estimate per request against a ceiling | Budgets per team or cost center, chargeback labels, actual spend |
| State | One SQLite file | Durable, highly available state |
| Clouds | Google Cloud | Google Cloud, AWS, then Azure |
| Day two | Resize only | Update, drift detection, import, Terraform export |
| Claude | Static tokens; 43 tools in about 26,500 tokens | OAuth sign-in, a skill, a consolidated tool list |

## 2. The landscape

| Tool | What the caller gives | Clouds | Guardrails | Tracks and cleans up what it made | Status |
|---|---|---|---|---|---|
| Google Cloud remote MCP servers | Per-API tool calls: create, delete, start, stop an instance; create a cluster; create a database | Google | The caller's IAM; tools denied by name or by their read-only attribute | No | Listed for 60+ products; Compute Engine, GKE and Cloud SQL carry no preview label |
| Google Cloud CLI MCP, gcloud-mcp | Any gcloud command | Google | The caller's IAM; a command deny-list in the open-source one | No | Preview |
| AWS MCP Server | Python run in a sandbox against AWS APIs | AWS | The caller's IAM; CloudTrail | No | GA May 2026 |
| Azure MCP Server | 276 tools over 57 services, VMs and SQL servers included | Azure | The user's role assignments; tools off by default in hosts | No | Open source; 2.0 GA April 2026 |
| Gemini Cloud Assist with Application Design Center | An intent, then an architecture and Terraform from templates | Google | Curated templates | Drift detection | Design Center GA December 2025; Gemini design in preview |
| Terraform MCP with HCP Terraform | Modules, workspaces, runs | Any | Apply and destroy off by default; approval | State; ephemeral workspaces | MCP GA June 2026; HCP paid |
| Pulumi MCP, Neo, `pulumi do` | Code, or one resource | Any | Role-based access, approvals, usage caps | State; TTL stacks | SaaS; `pulumi do` a research preview |
| env0 | Templates and agent self-service | Any | Approval policies, budgets, cost limits, TTL | State; drift detection | Paid |
| Spacelift Intent | Natural language, executed as provider API calls | OpenTofu providers | Policies before execution | SQLite state | Open source archived 2026-10-28; commercial edition continues |
| Defang | A Compose file and Dockerfiles | AWS, Google, Azure, DigitalOcean | An estimate tool | Destroy | MIT |
| SkyPilot | YAML with resources, setup and run commands | 25+ clouds, Kubernetes, Slurm | Admin policies: enforce autostop, GPU quota per user; no dollar budget | Autostop, teardown, preemption recovery | Apache-2.0, about 10,800 stars |
| DigitalOcean MCP | Droplets, databases, Kubernetes, load balancers | DigitalOcean | Service selection | No | MIT |
| **cloudinfra 1.36.1** | A typed nine-kind request with nothing executable | Google | Operator limits, a dollar ceiling, a quota check; principals start with no rights; a separate destroy right | Request ids, labels, cleanup on failure, destroy in reverse | Self-hosted; never run on a real project |

The thread also read AWS Labs' API and Cloud Control servers, which are superseded and default to read-write; Kiro,
which wraps AWS's infrastructure-as-code MCP server; Amazon Q Developer, whose IDE plugins end in April 2027; Azure
Copilot's deployment agent, which plans and asks before it deploys; StackGen, Brainboard and Firefly, which write or
codify Terraform; and the deploy tools of Qovery, Porter, Northflank, Massdriver, Cloudflare, Render, Railway, Vercel and
Fly.io. None of them changes the picture above. Two platforms of this kind are being withdrawn: AWS Proton ended support
on 7 October 2026 and Azure Deployment Environments retires on 22 February 2027.

**Where the service is ahead.**

- **A request that carries nothing executable, and no modules to write.** The vendors buy breadth through execution:
  Google's CLI server runs any gcloud command, AWS runs Python, Cloudflare runs JavaScript. The spec-driven tools carry
  code: Defang takes Dockerfiles, SkyPilot takes run commands. The platform products that govern self-service, HCP
  no-code workspaces, Massdriver bundles, env0 and Port templates, need a platform team to author modules first; this
  service ships its catalog.
- **A dollar ceiling the service enforces.** Elsewhere cost is advisory (Defang, Azure's pricing tool, Brainboard), off
  by default (HCP Terraform's estimates), or absent (SkyPilot, Google, AWS). env0 is the exception, as a paid product.
- **The service owns the lifecycle.** The vendors' servers record nothing an agent made; AWS's advice is to monitor
  CloudTrail. The one open-source tool that kept state was archived today.
- **The cloud credential stays with the service.** The vendors' servers act as the caller; ours holds the credential on
  its host, and its principals start with no rights, where Azure's and AWS Labs' read-only switches default off.
- **Self-hosted and open.** No paid control plane sits between a company and its cloud.

**Where it is behind, ranked for companies.**

1. **Proof and durability.** The vendors' servers are generally available and the platform products run as managed
   services; ours has never touched a real project and keeps state in one SQLite file.
2. **Identity and teams.** The hosted tools use OAuth and SSO; the platform products have teams, RBAC and approvals;
   ours has static tokens per caller.
3. **Governance over time.** env0 and Spacelift have approval policies, budgets and TTLs per environment; ours judges
   each request alone against a ceiling.
4. **Clouds.** Most companies run AWS, and the infrastructure-as-code platforms cover every cloud; ours covers Google.
5. **Day two.** env0, Spacelift and Application Design Center detect drift; Spacelift and Azure export to Terraform;
   ours resizes a machine and nothing else.
6. **Developer experience.** Access to what was created, golden images and presets, all of which the platform products
   offer.

**ML launchers are a layer above, not rivals.** SkyPilot and dstack run workloads, which our contract refuses to carry.
SkyPilot's SSH node pools and dstack's SSH fleets can adopt machines that already exist, so an ML team's launcher can run
its jobs on machines this service vends under the platform team's limits.

## 3. A day with the service today

**The developer.** They ask Claude for "a dev environment: two machines, a Postgres database and a bucket for test data".
The plan passes and everything is created. Then:

- They cannot log in: OS Login is on, and nobody was granted a login role or the IAP tunnel role.
- The database password arrives once, in the chat transcript, where a credential does not belong.
- The machines boot plain public images, not the company's hardened or tool-loaded images. A GPU machine has no drivers,
  and its 20 GB boot disk is under the 40 GiB Google's GPU guide specifies.
- The machines cannot read the bucket made in the same request: they carry no service account.
- The environment runs until someone deletes it. Forgotten dev environments are the classic cloud waste.
- Nothing labels it with a team or a cost center, so it cannot be charged back.

**The platform team.** The service has nowhere to run yet, and nowhere highly available. Callers hold static tokens;
there is no SSO, no team and no role beyond provision and destroy. Every request is judged alone against a monthly
ceiling, so a team's spend is bounded only by the cloud budget alert, and Google's budgets alert without capping Compute
Engine spend. There are no environments, no approval path for one expensive request, no audit export to the company's
log system, and no AWS.

**Claude.** In Claude Code it connects today with a bearer header. On claude.ai a person cannot add it without OAuth, and
claude.ai still drops a server's instructions, so how to use the service must live in tool descriptions or a skill. Its
43 tool definitions take 106,208 characters, about 26,500 tokens; the three tools that each carry the whole request schema
(`provision_infrastructure`, `plan_infrastructure`, `validate_infrastructure_request`) take 43 percent of that.

## 4. What to add or change, ranked

Effort: S is hours, M a day or two, L a week or more. Items from the previous review keep its numbers in brackets.

### Release A: a developer can use what they get

**A1. Run it for real.** (M)
- **Where.** A small VM of its own in the company's project, reusing Fast Deploy's controller Terraform and startup script
  (Caddy with TLS, Litestream, an uptime check), until release C brings highly available state. Not on another service's
  VM: any process on a machine can read its metadata server, and so its identity. Cloud Run fits poorly while state is
  one SQLite file: CPU runs only during requests unless billed by instance, and the background workers want one steady
  process.
- **Identity.** A keyless, single-purpose service account with roles only on a separate target project:
  `compute.instanceAdmin.v1`, `compute.networkAdmin`, `compute.securityAdmin`, `cloudsql.admin`,
  `container.clusterAdmin`, `redis.admin`, `pubsub.editor`, `secretmanager.admin`, `storage.admin`,
  `servicenetworking.networksAdmin` and `monitoring.viewer`, plus `iam.serviceAccountUser` on any service account it
  attaches, and for A2 a custom role holding exactly the `getIamPolicy` and `setIamPolicy` permissions on instances, IAP
  tunnel instances, secrets, buckets, topics and subscriptions. Shrink later with the IAM recommender.
- **Backstops.** Organisation policies on the target project: `compute.requireOsLogin`, `compute.vmExternalIpAccess`,
  `compute.trustedImageProjects`, `compute.restrictLoadBalancerCreationForTypes`, and `sql.restrictPublicIp` unless
  databases are to be reached through a public connector address.
- **State and monitoring.** Litestream to a bucket with `storage.objectUser` and a restore drill; an uptime alert; log
  alerts on `provisioning_failed`, `destruction_failed` and `internal_error`; a daily sweep that compares resources
  labelled `managed-by=cloudinfra` with the service's state.
- **Money.** A budget on the target project whose Pub/Sub alert pauses provisioning, and GPU quota caps through quota
  overrides, because budgets alert but do not cap and spend caps do not cover Compute Engine.
- **Proof.** The integration suite, then an end-to-end scenario on a sandbox project: a machine, a database and a bucket,
  with access granted and checked.

**A2. Access, credentials in the secret manager, and a "how to connect" block.** (M)
- **Contract.** `access: [{member: "user:…" | "group:…" | "serviceAccount:…", level: use | admin}]` on a request. The
  requester is added by default when the operator maps the principal to an identity in configuration. Members must
  belong to the operator's `access.allowed_domains`, which for a company is its own domain. Tool to grant and revoke
  later. Grants are recorded with the request.
- **Only resource-level grants.** The service grants on what it created, through each resource's own IAM policy, and
  never changes project IAM. Project-level prerequisites, such as a custom role with `compute.projects.get` for gcloud,
  `cloudsql.client` for the Cloud SQL Auth Proxy or `container.clusterViewer` for cluster credentials, are the platform
  team's one-time setup, and the connect block names them.

| Kind | Grant on the resource | Connect |
|---|---|---|
| compute | `compute.osLogin` (use) or `compute.osAdminLogin` (admin) on the instance; `iap.tunnelResourceAccessor` on the IAP tunnel instance, bound by numeric instance ID so a later machine with the same name never inherits it | `gcloud compute ssh VM --zone Z --tunnel-through-iap`, or `gcloud compute start-iap-tunnel VM PORT --local-host-port=localhost:PORT` for a port |
| database | Cloud SQL has no instance-level IAM: the generated password goes into a request-owned secret, and members get `secretmanager.secretAccessor` on it | From a machine on the request's network: `psql "host=… dbname=… user=… sslmode=require"` with the password from `gcloud secrets versions access latest --secret=S` |
| cache | No instance-level IAM either: the service copies the AUTH string into a request-owned secret | From a machine on the network: `redis-cli --tls` with the server certificate from the response and the AUTH string from the secret |
| secret | `secretmanager.secretAccessor`; admins also `secretmanager.secretVersionAdder` | `gcloud secrets versions access latest --secret=S` |
| bucket | `storage.objectUser` (use) or `storage.objectAdmin` (admin) | `gcloud storage cp` |
| queue | `pubsub.publisher` on the topic and `pubsub.subscriber` on the subscription | `gcloud pubsub topics publish T`, `gcloud pubsub subscriptions pull S` |
| cluster | None on the resource; `container.clusterViewer` is a project-level prerequisite | `gcloud container clusters get-credentials C --location L` |

- **Credentials.** The generated database password and the cache's AUTH string go into secrets the request owns. The
  response names the secrets; it carries the password itself only if the operator turns that on (previous review [11]).
- **Failure.** A grant that fails, for example to an identity that does not exist, does not undo the infrastructure: the
  request is ready, the grant is reported as failed with its reason, and it can be granted again.

**A3. Golden-image profiles.** (code S; images M for the platform team)
- **Contract.** `profile` beside `os`: a name the platform team defines in configuration, resolved to a pinned image or
  image family, a Secure Boot setting and a minimum boot disk. Requests carry no image paths or scripts. The response
  reports the image actually used.

| Profile, as a company might define it | Google image |
|---|---|
| `base` | The company's hardened image family, or `ubuntu-os-cloud/ubuntu-2404-lts-amd64`, `debian-cloud/debian-13` |
| `gpu` | An operator-built image with signed NVIDIA drivers, made by Google's `cuda_installer.pyz build_image` from an Ubuntu, Debian, Rocky or RHEL base in up to 20 minutes; Google says the procedure supports all GPU machine types, and Secure Boot stays on |
| `pytorch` | `deeplearning-platform-release/pytorch-2-9-cu129-ubuntu-2404-nvidia-580`, driver 580, supported until 4 August 2028; whether it boots G2 (L4) machines is unresolved (section 9) |
| `container` | `cos-cloud/cos-133-lts`, with GPU drivers installed at each boot; its default driver does not support L4, per the readiness thread |
| `team-*` | Images a team bakes from a reviewed repository with Packer |

- **Enforcement.** `compute.trustedImageProjects` lets only these image projects boot.
- **Why not caller scripts.** A startup script runs as root on every boot and can be changed by anyone with
  `setMetadata`; any process on the machine can read it.

**A4. Stop, start, idle stop, a maximum run time, and an expiry.** (M)
- **Tools.** Stop and start a whole request: machines stop, databases stop; the answer names what keeps billing, namely a
  stopped machine's disks, snapshots and static addresses, and a stopped database's storage and address.
- **Fields.** `lifetime: {hours, on_expiry: stop | destroy}` with an operator default and maximum; `autostop:
  {idle_minutes}` on a compute group; `max_run_hours`, mapped to Compute Engine's maximum run duration with the stop
  action. Extending a lifetime up to the maximum, and keeping a request awake for an unattended job, are tool actions.
- **Opt-in.** Expiry and idle stop apply only where a request or the operator asks for them. Release B makes them
  per-environment defaults, typically on in dev and off in prod, so nothing in production stops by surprise.
- **Expiry.** Warnings 48 hours and 2 hours before; stop first, keep disks and databases for a retention period, then
  destroy. No data is deleted without a warning.
- **Idleness.** From utilization, never from sessions, which would stop unattended jobs: the busiest CPU sample in the
  window, counted in cores so that one busy core on a large machine is not mistaken for idleness.
- **Estimates.** Each plan shows the running and the stopped monthly cost.
- **Caveat.** A stopped GPU machine may not start again for lack of capacity; the answer says so, and C6 adds fallback.

**A5. Two corrections found during this review.** (S)
- **GPU boot disks.** Accelerator groups default to the general 20 GB boot disk; Google's GPU creation guide specifies at
  least 40 GiB. Refuse less than 40 GB for accelerator groups and default to 50.
- **Secure Boot.** Release 1.36.1 turned Secure Boot off on GPU machines so that drivers could be installed. A profile
  with signed drivers turns it back on; off stays only for plain images.

### Release B: governed for a company

**B1. Teams with leads, and RBAC.** (M)
- `teams: [{name, members, leads, cost_center, budget, limits}]`, a `cloudinfra-team` and a `cost-center` label on every
  resource, and `scope: team`. Leads extend, stop, destroy and approve their members' requests; viewers read.

**B2. Environments.** (M)
- `environment` on a request, from operator-defined environments such as dev, staging and prod, each with its own
  allowed kinds and sizes, default and maximum lifetime, idle stop, approval rules and the teams that may use it, as Azure
  Deployment Environments' environment types and Humanitec's environment types do. Typically dev expires and stops when
  idle; prod never does and needs approval (previous review [22]).

**B3. Budgets, chargeback and actual spend.** (M)
- Budgets per team or cost center; admission adds the new request's estimate to every live request's running cost;
  warnings at 50, 80 and 100 percent; `BUDGET_EXCEEDED` past the budget. Actual spend from the BigQuery billing export,
  which carries labels, grouped by request, team and cost center, through a `get_spend` tool.

**B4. Approval instead of refusal.** (M)
- Rules by environment, cost, accelerators or lifetime turn a refusal into `PENDING_APPROVAL` with the reasons; approval
  tools for principals with an approver role, never the requester; approvals expire; the hard ceiling still refuses.

**B5. Notifications and audit export.** (S)
- Operator targets (email, a webhook, Slack or Teams), never taken from a request. Events: ready, failed, cleanup
  incomplete, expiring, stopped for idleness, budget thresholds, approval requested, destroyed. The audit events go to
  the company's log system through Cloud Logging or a webhook.

**B6. Presets.** (S)
- The platform team's versioned templates with the fields a developer may override; a `list_presets` tool and `preset`
  with `overrides` on plan and provision; each preset also offered as an MCP prompt.

### Release C: enterprise-ready and multi-cloud

**C1. SSO, and Claude.** (M)
- Port `labdeploy/oauth.py` and add protected-resource metadata, the `401` pointer and a client ID metadata document.
  Sign in through the company's identity provider over OIDC; the person becomes the default access member. One connector
  then serves claude.ai, Claude Desktop, mobile, Cowork and Claude Code (previous review [19]).
- A skill in a plugin: validate, plan, show the cost and ask, provision, follow, relay the connect block, and destroy
  only after confirmation. It is needed because claude.ai drops a server's instructions.
- A smaller tool list: fold the nine `get_*` and nine `delete_*` tools into `get_resource` and `delete_resource`
  (previous review [14]), and carry the request schema once: `plan_infrastructure` validates and returns a plan id, and
  `provision_infrastructure` takes that id. Together these cut the list by about 40 percent, to roughly 61,000
  characters (measured on `main`).

**C2. Durable, highly available state.** (L)
- PostgreSQL instead of SQLite, two or more replicas, and a lease per operation so that only one worker runs it; SQLite
  with Litestream stays for small installations.

**C3. AWS, then Azure.** (L each)
- An AWS adapter behind the same interface: EC2 and EBS, S3, VPC and security groups, RDS, ElastiCache, SQS and SNS,
  Secrets Manager, EKS, and application and network load balancers, with the credential from the host's IAM role. Azure
  follows.

**C4. Day two.** (M)
- Updates to fields declared mutable, a scheduled drift check that marks a request `DRIFTED` with the difference, import
  of existing resources, a Terraform export of a request, an idempotency key on provisioning (previous review [5]) and a
  history of each request.

**C5. The next kinds.** (M each)
- From the previous review's demand ranking: an identity per request bound to the request's own buckets, secrets and
  queues, so a machine can read the bucket beside it; DNS records and certificates with TLS on the load balancer; NAT
  for private machines; monitoring and alerting; a shared file system.

**C6. GPU capacity.** (M)
- `capacity: on_demand | spot | flex_start` with restarts; an ordered list of accelerator types with fallback zones and
  regions; the response reports placement and price.

**C7. Smaller things.** (S each)
- A small command-line client over REST; a finder for billable resources without the `managed-by=cloudinfra` label; a
  read-only web page of requests, costs and expiries once sign-in exists.

**Optional, for research customers.** A Jetstream2 adapter with a ledger in service units, notebooks and class sets, if
such customers appear.

| # | Item | Release | Effort |
|---|---|---|---|
| 1 | Run it for real | A | M |
| 2 | Access, credentials in the secret manager, a connect block | A | M |
| 3 | Golden-image profiles, with signed GPU drivers | A | S code, M images |
| 4 | Stop, start, idle stop, maximum run time, expiry, all opt-in | A | M |
| 5 | GPU boot disk minimum; Secure Boot back on with signed drivers | A | S |
| 6 | Teams with leads, RBAC | B | M |
| 7 | Environments | B | M |
| 8 | Budgets, chargeback, actual spend | B | M |
| 9 | Approval instead of refusal | B | M |
| 10 | Notifications and audit export | B | S |
| 11 | Presets | B | S |
| 12 | SSO, Claude sign-in, a skill, a smaller tool list | C | M |
| 13 | Durable, highly available state | C | L |
| 14 | AWS, then Azure | C | L each |
| 15 | Day two: update, drift, import, Terraform export, idempotency | C | M |
| 16 | The next kinds: identity, DNS and certificates, NAT, monitoring, file system | C | M each |
| 17 | GPU capacity | C | M |
| 18 | A small CLI; unmanaged-resource finder; a read-only page | C | S each |

## 5. Traps to avoid

- **Automation that surprises production.** Idle stop or expiry on a production resource is an outage. Make them opt-in,
  then per-environment defaults.
- **Idleness measured by sessions.** Coder and Codespaces count terminal and IDE activity, which stops unattended jobs.
  Measure utilization, and offer a way to keep a request awake.
- **Expiry that deletes data, or arrives unannounced.** Codespaces deletes a codespace whatever it holds; Azure's
  expiration, Port's timer and Spacelift's delete schedule document no warning. Warn, stop first, keep the data for a
  while.
- **Limits too short for long jobs.** Codespaces stops any codespace after 12 hours; one Harness user asks for more than
  the UI's 24-hour TTL. Training and batch runs take days.
- **Features that cancel each other.** Turning on env0's scheduling disables its TTL; Qovery applies only the first rule
  that matches. Lifetime, schedule and idle stop must compose.
- **Teardown nobody hears about.** Northflank notifies only setup runs. Notify every destroy and every leftover.
- **Restarts assumed.** Google's instance schedules guarantee no capacity and can run up to 15 minutes late; a stopped GPU
  machine may not start again.
- **Budget alerts that lag.** env0's alerts may take a day, and so does billing data; keep the service's own ledger as the
  first meter.

## 6. What not to build

- **Running code.** Scripts, jobs and run commands belong to the application layer or a launcher on top. Golden images
  carry software without carrying code.
- **Breadth through execution.** The vendors' servers already run any gcloud command or any Python; competing there gives
  up the typed contract, which is the reason to exist.
- **Serverless containers.** They need an image; they stay in Fast Deploy.
- **Another platform's control plane underneath.** AWS Proton has ended and Azure Deployment Environments retires in
  February 2027; the providers' own APIs are the stable layer.

## 7. Decisions only the company can make

- **Identity provider.** Google Workspace, Okta or Entra ID? This decides SSO, how members are named in grants, and the
  allowed domains.
- **Clouds and hosting.** Which cloud after Google: AWS? Will each company host the service itself, or is it to be
  offered as a hosted product?
- **Environments.** Which ones, and their rules: who may use each, sizes, lifetimes, idle stop, approvals.
- **Money.** Cost centers and budget owners, the chargeback labels, and whether the service may read the billing export.
- **Approvers.** Team leads, the platform team or both, and through which channel.
- **Expiry.** In dev, should expiry stop or destroy by default, and how long are stopped disks kept?
- **Compliance.** Regions and data residency, customer-managed encryption keys, audit retention, and the organisation
  policies that apply.
- **Exposure.** Is a public endpoint acceptable? claude.ai connects from `160.79.104.0/21`; a private endpoint limits the
  service to Claude Code and the company's own clients.

## 8. Corrections

- **Audience.** The first draft of this review assumed a university research lab; the operator corrected that the service
  is for industry. Sections 1, 3, 4 and 7 are written for companies; the research-lab findings are kept only where they
  apply generally, such as GPU images and idle stop, and in an optional list.
- **Static tokens.** The previous review's section 3 said static bearer tokens are what Claude's connectors accept.
  Claude Code and the Messages API's MCP connector do; claude.ai accepts a fixed request header only in a beta for a
  limited set of organisations, as one organisation-wide credential, so per-person use there needs OAuth. The sentence
  is corrected in place.
- **Secure Boot.** The previous review's item 2 turned Secure Boot off on GPU machines, which release 1.36.1 shipped.
  That is right for plain images; the better fix is a signed-driver image (A3), which keeps Secure Boot on.
- **Ranking.** The previous review put OAuth at 19 and environments at 22. For a company, access, credentials, images
  and time limits come first; then teams, environments, budgets and approvals; then SSO, durable state and AWS.

## 9. Limits of this review

The threads read documentation; nothing was provisioned and no product was trialled. The landscape thread did not record
every URL it read; those sources are named in section 10 without a link. The third thread was briefed for a university
research lab; its findings are used here only where they generalize. Whether Google's Deep Learning VM images boot G2
(L4) machines is unresolved: Google's G-series guide lists Deep Learning VM images as a boot option and states no limit,
while the research thread and a search result report that they cannot; the `gpu` profile of A3 avoids the question,
because Google's signed-driver images support all GPU machine types. The Google remote MCP servers' launch stage is read
from the absence of a preview label, not a stated GA date.

## 10. Sources

F: read on the page by a research thread. F+: also re-read by the reviewer. S: a snippet or secondary source. U:
unverified. "URL not recorded" means the thread named the page it read but did not keep its address.

**Landscape**

| Source | Mark |
|---|---|
| [Google Cloud MCP supported products](https://docs.cloud.google.com/mcp/supported-products), [MCP overview](https://docs.cloud.google.com/mcp/overview) | F+ |
| Google Compute Engine MCP tool reference (read on the docs.cloud.google.cn mirror), Google Cloud CLI MCP, gcloud-mcp, gke-mcp and cloud-run-mcp repositories | F, URLs not recorded |
| [Azure MCP Server overview](https://learn.microsoft.com/en-us/azure/developer/azure-mcp-server/overview): the user's credentials, role assignments, tools off by default in hosts | F+ |
| Azure MCP Server tool and service counts, 2.0 GA date | F, URL not recorded |
| AWS MCP Server documentation and launch post; Azure Copilot deployment agent; Gemini Cloud Assist | F, URLs not recorded; the Microsoft post S |
| [Application Design Center overview](https://docs.cloud.google.com/application-design-center/docs/overview) | F |
| [Terraform MCP server](https://github.com/hashicorp/terraform-mcp-server); Pulumi MCP, Neo and `pulumi do` | F; Pulumi URLs not recorded |
| [Spacelift Intent repository](https://github.com/spacelift-io/spacelift-intent) | F+ |
| [Spacelift Intent docs](https://docs.spacelift.io/concepts/intelligence/intent.md), [Spacelift MCP](https://docs.spacelift.io/concepts/intelligence/spacelift-mcp.md) | F |
| Defang, Encore, DigitalOcean MCP, Qovery, Porter, Northflank, Massdriver, Cloudflare, Render, Railway, Vercel, Fly.io | F, URLs not recorded |
| [SkyPilot admin policies](https://docs.skypilot.ai/en/latest/cloud-setup/policy.html) | F+ |
| [AWS Proton end of support](https://docs.aws.amazon.com/proton/latest/userguide/Welcome.html) | F+ (previous review) |
| [Azure Deployment Environments](https://learn.microsoft.com/en-us/azure/deployment-environments/concept-environments-key-concepts), retirement date | F |

**Self-service practicality**

| Source | Mark |
|---|---|
| Humanitec: [environments](https://developer.humanitec.com/platform-orchestrator/docs/platform-orchestrator/working-with/environments/), [RBAC](https://developer.humanitec.com/platform-orchestrator/docs/platform-orchestrator/security/rbac/), [pipelines](https://developer.humanitec.com/platform-orchestrator/docs/integration-and-extensions/humanitec-pipelines/overview/), [ephemeral environments](https://developer.humanitec.com/platform-orchestrator/guides/getting-started/master-your-internal-developer-platform/tutorial-deploy-ephemeral-environments/) | F |
| Port: [action RBAC](https://docs.port.io/workflows/actions-and-automations/create-self-service-experiences/set-self-service-actions-rbac.md), [action UI](https://docs.port.io/workflows/actions-and-automations/create-self-service-experiences/setup-ui-for-action.md), [timer property](https://docs.port.io/context-lake/data-model/setup-blueprint/properties/timer/), [MCP server](https://docs.port.io/context-lake/consuming-the-lake/port-mcp-server.md) | F |
| [Backstage templates](https://backstage.io/docs/features/software-templates/writing-templates) | F |
| Kratix: [Promises](https://docs.kratix.io/main/reference/promises/intro), [updates](https://docs.kratix.io/main/reference/resources/updates), [gating deletion](https://docs.kratix.io/main/guides/gating-deletion-with-workflow-control) | F |
| Massdriver: [bundles](https://docs.massdriver.cloud/concepts/bundles), [projects and environments](https://docs.massdriver.cloud/concepts/projects-and-environments) | F |
| env0: [TTL policy](https://docs.envzero.com/guides/policies-governance/policy-ttl), [scheduling](https://docs.envzero.com/guides/admin-guide/environments/scheduling.md), [budget notifications](https://docs.envzero.com/guides/cost-monitoring/budget-notifications.md), [approval policies](https://docs.envzero.com/guides/policies-governance/approval-policies.md), [drift detection](https://docs.envzero.com/guides/admin-guide/environments/drift-detection.md), [import](https://docs.envzero.com/guides/admin-guide/environment-discovery/onboarding-import-external-environments-into-env-zero.md) | F |
| Spacelift: [scheduling](https://docs.spacelift.io/concepts/stack/scheduling), [templates](https://docs.spacelift.io/concepts/template.md), [approval policy](https://docs.spacelift.io/concepts/policy/approval-policy.md), [notification policy](https://docs.spacelift.io/concepts/policy/notification-policy.md) | F |
| Azure Deployment Environments: [scheduled deletion](https://learn.microsoft.com/en-us/azure/deployment-environments/how-to-schedule-environment-deletion), [environment types](https://learn.microsoft.com/en-us/azure/deployment-environments/how-to-configure-project-environment-types) | F |
| AWS Service Catalog: [service actions](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/using-service-actions.html), [budgets](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/catalogs_budgets.html), [notifications](https://docs.aws.amazon.com/servicecatalog/latest/adminguide/constraints-notification.html) | F |
| Coder: [template schedules](https://coder.com/docs/admin/templates/managing-templates/schedule), [workspace scheduling](https://coder.com/docs/user-guides/workspace-scheduling), [quotas](https://coder.com/docs/admin/users/quotas), [notifications](https://coder.com/docs/admin/monitoring/notifications), [parameters](https://coder.com/docs/admin/templates/extending-templates/parameters) | F |
| Codespaces: [timeout](https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces), [automatic deletion](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces), [billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces) | F |
| [Qovery deployment rules](https://www.qovery.com/docs/configuration/deployment-rule), [Northflank preview blueprints](https://northflank.com/docs/v1/application/release/manage-preview-blueprints), [CloudLab concepts](https://docs.cloudlab.us/basic-concepts.html), [Chameleon reservations](https://chameleoncloud.readthedocs.io/en/latest/technical/reservations/gui_reservations.html), [Harness TTL request](https://ideas.harness.io/feature-request/p/support-ttl-values-beyond-24-hours-in-environment-management-ui) | F |
| Google: [stop and start](https://docs.cloud.google.com/compute/docs/instances/stop-start-instance), [instance schedules](https://docs.cloud.google.com/compute/docs/instances/schedule-instance-start-stop), [Cloud SQL stop](https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance), [GPU monitoring](https://docs.cloud.google.com/compute/docs/gpus/monitor-gpus), [billing export](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery) | F |

**Research computing**

| Source | Mark |
|---|---|
| [Install GPU drivers, Secure Boot section](https://docs.cloud.google.com/compute/docs/gpus/install-drivers-gpu) | F+ |
| [Deep Learning VM images](https://docs.cloud.google.com/deep-learning-vm/docs/images) | F+ |
| [G-series GPU machines](https://docs.cloud.google.com/compute/docs/gpus/create-gpu-vm-g-series) | F+ |
| The Deep Learning VM restriction on G2 machines | F by the thread, S by the reviewer; contradicted by the G-series guide |
| SkyPilot docs (autostop, managed jobs, optimizer, `sky status`, SSH node pools, Claude Code skill), dstack docs (idle duration, fleets, SSH fleets) | F, URLs not recorded |
| [Jetstream2 documentation](https://docs.jetstream-cloud.org/) (featured images, shelving, allocations, CACAO) | F |
| CloudBank, ACCESS allocations, Google research credits, Vertex AI Workbench idle shutdown, Spot and Flex-start, Open OnDemand on UTSA Arc, JupyterHub culler, NERC, Stanford and Berkeley account vending | F, URLs not recorded |
| A survey of 50 AI researchers at 35 institutions, COLM 2025 | F by the thread, title not recorded |

**Running it, and Claude**

| Source | Mark |
|---|---|
| [Claude connector authentication](https://claude.com/docs/connectors/building/authentication) | F+ |
| [Claude Code MCP](https://code.claude.com/docs/en/mcp), [Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector), Claude skills and plugins docs, [claude-ai-mcp issue 93](https://github.com/anthropics/claude-ai-mcp/issues/93) | F |
| MCP registry: [about](https://modelcontextprotocol.io/registry/about), remote servers, package types | F |
| Google: hosting MCP servers on Cloud Run, Cloud Run billing settings, IAM role-grant limits and conditions, service-account best practices, organisation-policy constraints, budgets and spend caps, quotas, [OS Login setup](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin), [IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding), Cloud SQL Auth Proxy and IAM users, GKE cluster access, Memorystore access control, startup scripts and metadata, image management | F, most URLs not recorded |
| [Litestream on Cloud Storage](https://litestream.io/guides/gcs), [Google OpenID Connect](https://developers.google.com/identity/openid-connect), Packer's googlecompute builder | F |
| The tool list's size | Measured by the reviewer on `main` at `984ec36` |
