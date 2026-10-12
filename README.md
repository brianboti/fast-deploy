# Lab Deploy

A working first implementation of the lab deployment platform: Claude calls authenticated MCP tools; the Python controller creates reviewed configuration proposals and immutable deployment plans; GitHub Actions tests, builds, and deploys to GCP Cloud Run.

**Status: live-tested GCP staging MVP. On 2026-10-12 the platform released a demo app to a disposable staging project end to end, rolled back a deliberately unhealthy release, recovered a checkpoint, and produced a measured cost report; see docs/VALIDATION.md.** This project does not call the Anthropic API; Claude Chat is the model and conversational interface. No Anthropic API key is required by this backend.

## What is implemented

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
- Measured right-sizing: a weekly read-only workflow prices actual Cloud Monitoring usage, proposes memory, CPU, concurrency and warm-instance changes with estimated savings, and includes Google's own Cloud Run cost recommender. Claude reads the report with `get_cost_report`.
- Infrastructure savings: Artifact Registry cleanup policies, checkpoint-bucket lifecycle rules, and an optional billing budget alert in Terraform.
- Request budgets per operator identity and per client address for failed logins, as the MCP specification requires.
- Every tool declares a safety hint (read-only, creates a record, or changes cloud resources) and a structured output schema, so Claude's client can ask before risky calls.
- Cloud Run gates traffic on an HTTP startup probe at the reviewed health path before the platform's own probes run.
- Signed SLSA build provenance for every pushed image digest, verified before release, plus an Artifact Analysis vulnerability summary with optional release blocking.
- CI lints Python (ruff) and workflows (zizmor), audits the platform's own dependencies, and validates the Terraform; Dependabot keeps action and provider pins current.
- Deployments and recovery share an explicit pending-work queue. A versioned GCS checkpoint lets a separate workflow recover a release after runner loss.
- GitHub Actions template: tests, dependency vulnerability audit, container build, artifact transfer, federated GCP authentication, image push, digest deployment, authenticated probes, and traffic restoration on failure.
- Terraform foundation: service-specific IAM, Artifact Registry, Cloud Run, workload identities and GitHub OIDC trust restricted by numeric repository/owner IDs, main branch, staging environment, and workflow path.
- Example FastAPI app and tests, dependency lockfiles with hashes, generated JSON schema, setup instructions, and documentation sources.

## Supported scope

| Item | First release |
|---|---|
| App | Python HTTP service, stateless, Dockerfile at repo root |
| Branch/environment | Protected `main`, GCP `staging` |
| Region | `us-central1` foundation |
| Access | Authenticated Cloud Run; no public grants |
| Resources | Up to 2 CPUs, 2048 MiB, 3 instances per revision |
| Database/GPU | Rejected; not provisioned |
| AWS/production | Rejected; not implemented |
| Rollout | Test candidate tag, switch traffic, probe again |
| Rollback | Restore previous concrete traffic allocation; report failure honestly |
| Interrupted release | Immediate recovery attempt on graceful termination; separate workflow reads durable checkpoint after runner loss |
| Cost | List-price bounds and findings in every plan; optional per-app worst-case ceiling; weekly measured right-sizing report; budget alert and image cleanup in Terraform. Still no hard spending cap |

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

`GET /healthz` is public. Every other request requires `Authorization: Bearer <operator-token>` and is rate limited per identity. The MCP endpoint is `/mcp`; REST endpoints are `GET /applications`, `GET /applications/{id}/cost`, `GET /applications/{id}/cost-report`, `POST /applications/{id}/plans`, `GET /plans`, `POST /plans/{id}/deploy`, and `GET /plans/{id}`.

GitHub access is real even with dispatch disabled. No fake GitHub data is substituted by the running service. Automated tests use explicit fakes and HTTP mocks.

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
3. **Measured right-sizing, after real traffic.** `templates/rightsize-gcp.yml` runs weekly on the deployment runner, reads Cloud Monitoring usage for the service (allocated vCPU-seconds and GiB-seconds, requests, p95 CPU and memory utilization, concurrency, instance counts), prices it at list rates, proposes concrete `platform.yaml` changes with estimated monthly savings, and attaches Google's Cloud Run cost recommender output. Proposals are never applied automatically; a reviewer edits `platform.yaml` through the normal process. `get_cost_report` returns the latest report to Claude.

The platform always deploys the cheapest billing model for bursty traffic (CPU allocated only during requests), scales to zero by default, requires authentication, deploys to a Tier 1 region, and caps instances. Terraform adds image cleanup policies (dry run first), checkpoint-bucket lifecycle rules, and an optional monthly budget alert.

Try the estimator locally:

```bash
python -m labdeploy.cli cost examples/demo-app/platform.yaml --requests 100000 --average-seconds 0.2
```

## Connect and deploy

Follow [docs/SETUP.md](docs/SETUP.md) for GitHub, GCP bootstrap, Claude remote connection, and the first release. Follow [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership, trust boundaries, and known limitations. Official references read before implementation are in [docs/SOURCES.md](docs/SOURCES.md).

Version 0.3.0 adds the 2026-practice hardening (rate limits, tool safety hints, startup probes, build provenance, image scanning, CI gates); 0.2.0 added the cost optimizer; 0.1.2 added the local demo mode, plan listing with audit events, and small hardening changes; 0.1.1 fixed the review findings. See [docs/CHANGES.md](docs/CHANGES.md) for details and upgrade steps. Existing configurations must add a cloud target, and both app workflow files must use the new pinned platform commit.

You will need the target GitHub app repository, a repository for this platform code, a GCP staging project, billing enabled for that project, and authority to establish its initial IAM/WIF resources. Platform setup is performed once by an administrator; routine app releases use the restricted workflow identity.

## Later phases

AWS adapter, production policy/approvals, managed-database provisioning, hard spending caps, automatic application of right-sizing proposals, container/secret scanning, SBOM and attestation verification, workload secret bindings, Cloud Deploy canary monitoring, enterprise OAuth, and a durable multi-instance job store are not included in this release. The code deliberately rejects unsupported deployment targets instead of treating them as successful.

