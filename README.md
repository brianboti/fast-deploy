# Fast Deploy

## Two layers (1.35.0)

This repository holds a provider-independent **Cloud Infrastructure MCP** (`cloudinfra`) and, on top of it, the
**Fast Deploy** application delivery workflow (`labdeploy`), the controller the rest of this README describes.

```
Application Layer (Claude, a CI job, another service, Fast Deploy)
  │  InfrastructureRequest → InfrastructureResponse, over MCP or REST
  ▼
Cloud Infrastructure MCP (cloudinfra): validation, catalog, orchestration, provider abstraction
  │  CloudProvider
  ▼
GCPProvider → Google Cloud          (AWS, Azure, Jetstream: adapters to come; nothing above changes)
```

The infrastructure layer provisions machines (in groups, with or without GPUs), disks, buckets, networks, load balancers,
managed databases, caches, queues, secret containers and Kubernetes clusters from a request such as
`{"provider": "gcp", "region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "os": "ubuntu"}}`, and returns
resource ids, addresses, endpoints and status. It grants people access to what it created (never to the project), says
how to connect to each resource, keeps database passwords and cache AUTH strings in secrets the request owns, offers the
operator's approved images (a GPU image with signed drivers among them), and stops, starts and expires what it created
when a request or the operator asks. It takes no repository, image, script or command, holds no GitHub credential, and
decides nothing about an application; those are the Application Layer's. Fast Deploy keeps owning
repositories, builds, tests, images and releases, and never has to be present for the infrastructure layer to work.

- Architecture and ownership: [docs/INFRASTRUCTURE-ARCHITECTURE.md](docs/INFRASTRUCTURE-ARCHITECTURE.md)
- The 45 MCP tools: [docs/MCP-CATALOG.md](docs/MCP-CATALOG.md)
- Where the service stands and what comes next: [docs/RESEARCH-2026-10-28-positioning.md](docs/RESEARCH-2026-10-28-positioning.md)
- Whether a separate delivery service above it is the right shape: [docs/RESEARCH-2026-10-29-delivery-layer.md](docs/RESEARCH-2026-10-29-delivery-layer.md)
- The delivery service's draft specification: [docs/DELIVERY-SPEC.md](docs/DELIVERY-SPEC.md)
- The provider interface and the Google Cloud adapter: [docs/PROVIDER-INTERFACE.md](docs/PROVIDER-INTERFACE.md)
- The analysis behind the split: [docs/REFACTOR-2026-10-28.md](docs/REFACTOR-2026-10-28.md)

```bash
python -m cloudinfra.cli token                      # a caller token and its hash
cp cloudinfra.example.yaml cloudinfra.local.yaml    # project, regions, limits, principals; no credential in the file
CLOUDINFRA_CONFIG=cloudinfra.local.yaml uvicorn cloudinfra.mcp.server:create_app --factory --host 127.0.0.1 --port 8100
python -m pytest tests/cloudinfra -q                # the boundary tests; no cloud is touched
```

## Fast Deploy: the application delivery workflow

*Formerly Lab Deploy.* What people see was renamed in 1.14.0 and this repository in 1.14.1 (`brianboti/fast-deploy`;
GitHub redirects the old `brianboti/lab-deploy`). The Python package `labdeploy`, the command `lab-deploy`, the local
folder and the Google Cloud resources keep their old names, so nothing running had to move.

A working first implementation of the lab deployment platform: Claude calls authenticated MCP tools; the Python controller creates reviewed configuration proposals and immutable deployment plans; GitHub Actions tests, builds, and deploys to GCP Cloud Run.

**Status: live-tested on Google Cloud staging and operated from Claude chat. On 2026-10-12 the platform released, rolled back, cancelled, redeployed, auto-deployed and right-sized a demo app in a disposable staging project, and read its live health and logs; see docs/VALIDATION.md.** This project does not call the Anthropic API; Claude Chat is the model and conversational interface. No Anthropic API key is required by this backend.

## What is implemented

- A visual **Deployments** workspace with automatic updates, real workflow steps, elapsed and remaining work, independently
  checked Cloud Run readiness and traffic, activity, reports, and provider log links. Missing or stale evidence stays explicit;
  time remaining is not guessed. See [docs/DEPLOYMENT-VISIBILITY.md](docs/DEPLOYMENT-VISIBILITY.md).
- Browser account setup at **Configure → Connections**: connect Google accounts, select a verified staging destination,
  inspect GitHub installation grants and restrict repository access, then check and start application onboarding from the
  same screen. Google sign-in requires a one-time OAuth client setup; see [docs/CONNECTIONS.md](docs/CONNECTIONS.md).
- Conservative repository analysis and automatic generation of `platform.yaml` with review questions.
- Optional draft onboarding PRs. Existing config is never overwritten or auto-merged.
- Strict schema and deterministic policy checks; unknown fields and unsupported workloads fail closed.
- Remote Streamable HTTP MCP plus a small REST API, authenticated using per-operator bearer tokens stored only as hashes.
- App allowlists and read-only/deployer permissions for each identity.
- GitHub App authentication or a development fine-grained token held by the server.
- Plans bound to a full Git commit and config hash, 15-minute expiration, persisted audit events, and atomic dispatch claims.
- Plans also bind the GCP project, region, service, registry, deployment/probe identities, WIF provider, and recovery bucket. The runner checks its environment against that destination before cloud authentication.
- Status reports reconcile actual workflow steps, including failed, skipped, canceled and unconfirmed checks.
- Per-operator plan listing and a persisted audit trail of plan events, returned with every status report.
- A local demo mode that runs the real controller against an application directory on disk with dispatch permanently disabled.
- Cost optimizer: every plan carries cost bounds from Google's list prices (zero-traffic floor, worst-case ceiling, optional traffic scenario) and cost-hygiene findings; an optional per-app ceiling refuses plans whose worst case is too high.
- Measured right-sizing: a weekly read-only workflow prices actual Cloud Monitoring usage, proposes memory, CPU, concurrency and warm-instance changes with estimated savings, and includes Google's Cloud Run recommender, which advises only the billing mode (Google offers no CPU or memory sizing for Cloud Run, so these measured proposals, memory first, are the sizing signal). The report also counts the month's per-event charges: freshly built images (each one is a scan) and the billable GitHub Actions minutes of a private repository. Claude reads the report with `get_cost_report`.
- Infrastructure savings: Artifact Registry cleanup policies, checkpoint-bucket lifecycle rules, and an optional billing budget alert in Terraform.
- Request budgets per operator identity and per client address for failed logins, as the MCP specification requires.
- Always on, on its own free machine (an `e2-micro` behind Caddy and Let's Encrypt, about $3.65 a month for its address, with Litestream copying the database every minute and an uptime check), or on a laptop as the fallback: live health and log views through a viewer-only identity, auto-deploy on merge, a daily watchdog with notifications, nightly backups, a controller watch from GitHub, and a GitHub App instead of a personal token.
- One sentence: `onboard_application` takes a repository link that follows `docs/DEVELOPER-CONTRACT.md` and, unattended, registers the app, opens and merges its infrastructure and workflow pull requests when the policy passes, prepares its GitHub environment and releases the first commit.
- More kinds of apps: plain configuration and Secret Manager secrets from `platform.yaml` with existence checks before deploying, apps without tests declared and warned about, request timeouts, lock-file advice for reproducible builds, and a decommission checklist.
- Real cost control: a spending cap that closes ingress at the budget line, actual spend from the billing export next to the list-price figures, a living cost-report issue, right-sizing proposals as pull requests, a schedule watchdog, and Terraform in project and per-app modules.
- Steer after a release: traffic rollback in seconds, cancel, deploy any commit already on main, redeploy a released commit from its attested image without a rebuild, compare main with the live release including the cost delta, one-call deploy, pause switches that apply without a restart, session management, a platform pin check and a drift check.
- A bake after every promotion: for five minutes the release watches the new revision's own Cloud Run logs and restores the previous traffic split on a crash, on more than 5% 5xx answers, or on a p95 latency three times the previous one; too little traffic is reported as inconclusive.
- Two spending guards: the platform's cap workflow, and Google's native budget spend cap for Cloud Run (generally available since 2026-10-12), which the operator creates with `scripts/spend-cap.sh` and every plan names; a project without one carries a warning.
- Dockerfile lint with hadolint in every build, as annotations and in the receipt; error-level findings stop the release (a repository switch turns that off).
- Several services in one repository, each in its own folder with its own workflows, GitHub environment, identities and Terraform root; shared code through `build: {context: repository}`; a Compose file read into one draft per web service, never deployed; Postgres by password (`DATABASE_URL` as a secret, written through write-only arguments so no Terraform state holds it) next to the default IAM sign-in.
- A repository as it is (1.15.0): `onboard_application` with `as_is` (or `import_contract` alone) reads a repository by fixed precedence rules, refuses what it cannot read safely, and proposes its contract as `apps/<service>/platform.yaml` in the platform repository; a person's merge is the review, the platform's own pipeline releases it, and nothing is added to the repository.
- The platform writes the boilerplate: `check_repository` lists what a repository lacks, and `propose_configuration` drafts it (Dockerfile, lock compiled for the app's Python, test tools, a health test, `platform.yaml` with `reviewed: false`, CI, security policy, Dependabot) as one draft pull request that never changes an existing file. A `Procfile` alone builds with Google's buildpacks.
- Approvals a person can read: the infrastructure plan comment and every release plan carry a fixed block rendered by code (what changes, the cost before and after, exposure, how far a rollback undoes it, the chain of identifiers); requester and approver can be kept apart; tool descriptions are pinned by checksum; an export script and a quarterly restore drill cover leaving Google Cloud and losing the laptop.
- Data-protection policy on every infrastructure plan: database backups, encrypted-only connections, deletion protection, no force-destroyed buckets, versioned checkpoints, and no secret material in Terraform state, each with a planted-violation test.
- A weekly drift check plans every application root against the live infrastructure with the read-only identity, keeps an issue open while anything differs, and feeds the watchdog.
- Every step visible: status reports every workflow step with timing, the release receipt and the candidate's own log lines on failure, the recovery run that followed, and a plain next action; a finished release is stored as history with its revision, digest and commit. Notifications to Slack or Discord, GitHub Deployments records, revision labels and protected serving images. An audit trail of every call and sign-in (`list_events`). Terraform runtime alerts: authenticated uptime check, errors, latency, saturation, memory, log errors, and a dashboard.
- Browser sign-in for Claude chat: the controller is its own OAuth 2.1 authorization server (PKCE, dynamic client registration limited to Claude's callbacks, single-use codes, one-hour access tokens, rotating refresh tokens with reuse detection). The operator token is the only credential and is entered on the controller's own page, never in Claude.
- Every tool declares a safety hint (read-only, creates a record, or changes cloud resources) and a structured output schema, so Claude's client can ask before risky calls.
- Cloud Run gates traffic on an HTTP startup probe at the reviewed health path before the platform's own probes run.
- Signed SLSA build provenance for every pushed image digest, verified before release, plus an Artifact Analysis vulnerability summary with optional release blocking.
- CI lints Python (ruff) and workflows (zizmor), audits the platform's own dependencies, and validates the Terraform; Dependabot keeps the provider pins current, and a weekly job proposes, in one reviewed pull request, the other pins (tools, images and the actions the templates use) once each release is a week old.
- Deployments and recovery share an explicit pending-work queue. A versioned GCS checkpoint lets a separate workflow recover a release after runner loss.
- GitHub Actions template: tests, dependency vulnerability audit, container build, artifact transfer, federated GCP authentication, image push, digest deployment, authenticated probes, and traffic restoration on failure.
- Terraform foundation: service-specific IAM, Artifact Registry, Cloud Run, workload identities and GitHub OIDC trust restricted by numeric repository/owner IDs, main branch, staging environment, and workflow path.
- Example FastAPI app and tests, dependency lockfiles with hashes, generated JSON schema, setup instructions, and documentation sources.

## Supported scope

| Item | First release |
|---|---|
| App | Python HTTP service, stateless, Dockerfile (or a `Procfile`) at the repository root or in the service's folder; several services per repository |
| Branch/environment | Protected `main`; GCP staging and explicitly registered production promotions |
| Region | `us-central1` foundation |
| Access | Authenticated Cloud Run; no public grants |
| Resources | Up to 2 CPUs, 2048 MiB, 3 instances per revision |
| Configuration | `env` values and Secret Manager `secrets` named `<service>-NAME`; values never pass through Claude |
| Tests | Required, or `tests: none` declared with a warning on every plan |
| Request timeout | `timeout_seconds`, up to 300 |
| Needs | Buckets, Pub/Sub topics, a Firestore or private Cloud SQL database and a migration job declared under `needs`, provisioned through a reviewed Terraform root per application; GPUs rejected |
| AWS | Rejected; not implemented |
| Rollout | Test candidate tag, switch traffic, probe again, then watch real traffic for five minutes (the bake) |
| Rollback | Restore previous concrete traffic allocation; report failure honestly |
| Interrupted release | Generation-checked checkpoints and conditional restoration; production supervisor is opt-in and requires a live drill |
| Cost | List-price bounds and findings in every plan; optional per-app ceiling on the compute bound plus the needs' floor; weekly measured right-sizing report; budget alert, a spending cap that closes ingress and stops databases at the budget line, Google's native Cloud Run spend cap as a second layer, and image cleanup in Terraform |

The initial resource settings are starting defaults, not measurements. Dependencies, memory consumption, traffic, and data classification cannot be reliably inferred from source code alone. Nothing in this repository asserts suitability for real patient data.

## Run locally

Python 3.12 is required. From this directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest -q
python -m pytest examples/demo-app -q
python -m labdeploy.cli analyze examples/demo-app --name lab-demo
python -m labdeploy.cli validate examples/demo-app/platform.yaml
```

To write a new draft without replacing an existing file:

```bash
python -m labdeploy.cli analyze /path/to/app --name my-app --output /path/to/app/platform.yaml
```

The generated config has `reviewed: false`. Resolve its assumptions, inspect the app, set `reviewed: true`, and merge it through the repository's review process. For a new remote repository, the MCP `propose_configuration` tool can create a draft PR instead.

## Start the controller

1. Copy `config.example.yaml` to `config.local.yaml`.
2. Run `python -m labdeploy.cli token` in a private terminal. Store the raw token securely; put only its SHA-256 hash in the principal configuration.
3. Set the real repository and service name. Leave `live_dispatch: false` initially. This setting also disables PR creation.
4. Configure server-side GitHub credentials using the setup guide. Do not send credentials in Claude conversations.
5. Start the service:

```bash
export LAB_CONFIG=config.local.yaml
uvicorn labdeploy.server:create_app --factory --host 127.0.0.1 --port 8000
```

`GET /healthz` is public. Every other request requires `Authorization: Bearer <operator-token>` (or an access token from the browser sign-in below) and is rate limited per identity. The MCP endpoint is `/mcp`; REST endpoints are `GET /applications`, `GET /applications/{id}/cost`, `GET /applications/{id}/cost-report`, `POST /applications/{id}/plans`, `GET /plans`, `POST /plans/{id}/deploy`, and `GET /plans/{id}`.

GitHub access is real even with dispatch disabled. No fake GitHub data is substituted by the running service. Automated tests use explicit fakes and HTTP mocks.

## Connect Claude chat (browser sign-in)

claude.ai adds remote MCP servers as custom connectors. Fixed request headers are a limited beta there, so the controller offers OAuth sign-in instead. Nothing leaves the controller: it is its own authorization server, and the sign-in page accepts only a configured operator token.

1. Host the controller on a public HTTPS origin (see [docs/SETUP.md](docs/SETUP.md) section 5) and set, in `config.local.yaml`:

```yaml
oauth:
  public_url: https://YOUR_HOST
allowed_hosts:
  - YOUR_HOST
```

2. Restart the controller. It now serves `/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server`, `/register`, `/authorize`, `/token`, `/revoke` and the sign-in page `/login`.
3. In Claude, add a custom connector with the URL `https://YOUR_HOST/mcp`, authentication **Sign in now**, OAuth client **Register automatically**. Click **Connect**: a browser tab opens the controller's sign-in page, you paste your operator token once, and Claude receives a one-hour access token plus a thirty-day refresh token.
4. Rotate the operator token's hash in the configuration to end every session created with it; `POST /revoke` ends one session.

Claude Code can use the same sign-in (`claude mcp add --transport http lab-deploy https://YOUR_HOST/mcp`, then `/mcp` to authenticate) or keep the bearer header shown below.

## Local demo (no GitHub, no cloud)

To show the complete conversation flow without any credential, run the demo server. It is the real controller and MCP server, but the application is read from a directory on this machine instead of GitHub, workflow dispatch is permanently disabled, and the repository adapter refuses every write. The production entry point never uses it.

```bash
python -m labdeploy.demo serve
```

The banner prints a one-time operator token. In a second terminal, export it as shown, then either run the scripted walkthrough, which exercises every tool and two policy refusals:

```bash
python -m labdeploy.demo walkthrough
```

or connect Claude Code to the demo server and ask it to list applications, analyze the demo app, create a plan, and deploy it:

```bash
claude mcp add --transport http lab-deploy http://127.0.0.1:8000/mcp --header "Authorization: Bearer $LAB_DEMO_TOKEN"
```

Plans created in demo mode stay `planned`, and deployment reports `dispatch: disabled`.

## Cost optimizer

Cost is handled in three honest layers. None of them invents a number.

1. **Bounds from list prices, before anything is deployed.** Every plan and the `estimate_cost` tool price the reviewed `platform.yaml` with Google's published Cloud Run rates: a zero-traffic floor (warm instances), a worst-case compute ceiling (every allowed instance busy for a 730-hour month), and an optional best/worst range for a traffic scenario you describe. The output lists its assumptions and everything it excludes. The operator can set `cost_ceiling_usd` per application; a plan whose worst case exceeds it is refused.
2. **Findings from Google's cost checklist.** Warm minimum instances, memory or CPU above the smallest size without measurements, low concurrency, the effect of `scaling.maximum`, and the fact that authentication blocks junk traffic are called out with the dollar effect where it can be computed.
3. **Measured right-sizing, after real traffic.** `templates/rightsize-gcp.yml` runs weekly on the deployment runner, reads Cloud Monitoring usage for the service (allocated vCPU-seconds and GiB-seconds, requests, p95 CPU and memory utilization, concurrency, instance counts), prices it at list rates, proposes concrete `platform.yaml` changes with estimated monthly savings, and attaches the output of Google's Cloud Run recommender, which only compares billing modes. Proposals are never applied automatically; a reviewer edits `platform.yaml` through the normal process. `get_cost_report` returns the latest report to Claude.

Every estimate also names the per-release overheads outside these figures, such as the image scan fee, Actions minutes on private repositories and registry storage, and warns when the recorded list prices are more than 90 days old.

The platform always deploys the cheapest billing model for bursty traffic (CPU allocated only during requests), scales to zero by default, requires authentication, deploys to a Tier 1 region, and caps instances. Terraform adds image cleanup policies (dry run first), checkpoint-bucket lifecycle rules, and an optional monthly budget alert.

Try the estimator locally:

```bash
python -m labdeploy.cli cost examples/demo-app/platform.yaml --requests 100000 --average-seconds 0.2
```

## Connect and deploy

Follow [docs/SETUP.md](docs/SETUP.md) for GitHub, GCP bootstrap, Claude remote connection, and the first release. Follow [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership, trust boundaries, and known limitations. Official references read before implementation are in [docs/SOURCES.md](docs/SOURCES.md).

Version 1.9.0 added the cost and safety layers of roadmap Phase 9, chosen by the research review `docs/RESEARCH-2026-10-14.md`: the bake after promotion, the native spend cap, Dockerfile lint, data-protection policy rules, the weekly drift check and per-event charges in the cost report. Version 1.5.0 applied the strict review of 2026-10-12 (`docs/REVIEW-2026-10-12.md`): pipeline identities pinned to their workflows and branch with no basic roles, a fail-closed plan policy judged from main, tools that no longer block the controller, recoverable migrations, a spending cap with retries, an alert and a lift label, off-machine backups, a runbook and a license. Versions 1.1.0 to 1.4.0 let an app declare buckets, topics, a Firestore database, a Cloud SQL database and a migration command under `needs`, show them in the health view and the cost report, and let the spending cap stop databases; 1.0.x turned infrastructure into pull requests (one Terraform root per application, plan on pull request, apply on merge, policies); 0.9.x added live views, auto-deploy, the watchdog, backups and the controller watch; 0.8.0 added configuration and secrets, the tests policy, timeouts, lock-file advice and decommissioning; 0.7.0 added real cost control (spending cap, actual spend, report issue, right-sizing pull requests, schedule watchdog, Terraform modules); 0.6.0 added steering after a release (rollback, cancel, deploy by commit, image reuse, compare, pause, sessions, pin and drift checks); 0.5.0 made every step visible (timeline, receipts, candidate logs, history, notifications, audit trail, runtime alerts) and adds `update-workflows`; 0.4.1 trimmed the recovery schedule, names per-release overheads, explains GitHub failures plainly and adds `get_controller_status`; 0.4.0 added the browser sign-in for Claude chat; 0.3.0 added the 2026-practice hardening (rate limits, tool safety hints, startup probes, build provenance, image scanning, CI gates); 0.2.0 added the cost optimizer; 0.1.2 added the local demo mode, plan listing with audit events, and small hardening changes; 0.1.1 fixed the review findings. See [docs/CHANGES.md](docs/CHANGES.md) for details and upgrade steps. Existing configurations must add a cloud target, and both app workflow files must use the new pinned platform commit.

You will need the target GitHub app repository, a repository for this platform code, a GCP staging project, billing enabled for that project, and authority to establish its initial IAM/WIF resources. Platform setup is performed once by an administrator; routine app releases use the restricted workflow identity.

## Later phases

AWS, enterprise identity-provider sign-in, Cloud Deploy integration, automatic application of right-sizing proposals, and multiple active controller writers remain outside the supported architecture. Production promotion/review, secret and image scanning, SBOMs, provenance verification and workload secret bindings are implemented; their effective installation settings still need verification. The full company production gate is specified in [docs/PRODUCTION-GATE.md](docs/PRODUCTION-GATE.md).

## Operations and license

`docs/DEVELOPER-CONTRACT.md` tells a developer what a repository must contain to be deployed. `docs/CLAUDE-CHAT.md` is the operator's daily reference for driving the platform from Claude chat: what to ask, what each tool does, what a normal release looks like and what the guardrails are. `skills/fast-deploy-operator` is the same knowledge as a skill for Claude chat (upload it once under Customize → Skills): when to ask before acting, how to read each status, which costs to show and how to tell a developer what their repository is missing. `docs/RUNBOOK.md` says who owns the platform, what to do when the laptop is off, how to restore the controller from the backup bucket, how to lift the spending cap and how to rotate credentials. The code is released under the MIT License (`LICENSE`).

## Company-readiness implementation

Phases 0 and 2 add universal verified-secret admission, independent contract CI, a durable operation ledger, bounded scheduler lanes, isolated asynchronous dependency resolution, authenticated job reservations, and opt-in independent production recovery. Local dependency execution is disabled; configure workers, supply reviewed lockfiles, or set `resolver_local_fallback: true` as a recorded, temporary exception until the workers exist.

Read the [control catalog](docs/CONTROL-CATALOG.md), [implementation record](docs/READINESS-IMPLEMENTATION.md) and [rollout/restore instructions](docs/READINESS-ROLLOUT.md) before upgrading. New IAM, worker and recovery defaults are deliberately opt-in. Local tests are not evidence that a live installation meets the controls.
