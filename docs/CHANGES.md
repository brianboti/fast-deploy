# 0.4.0: browser sign-in for Claude chat

## Added

1. **OAuth 2.1 authorization server** (`labdeploy/oauth.py`), enabled by `oauth.public_url`: protected-resource and authorization-server metadata, dynamic client registration restricted to Claude's callback and loopback redirects, PKCE S256, five-minute single-use codes, one-hour access tokens, thirty-day rotating refresh tokens with reuse detection, revocation, and a sign-in page at `/login` that accepts only a configured operator token. Tokens and codes are stored as SHA-256 hashes in the existing SQLite file. Unauthenticated requests now answer `WWW-Authenticate: Bearer resource_metadata="..."` so Claude discovers the sign-in. Rotating an operator's token hash ends its sessions.
2. **Request gate** accepts either a static operator token or a live access token; the OAuth endpoints are rate limited per client address and failed sign-ins count against the existing failed-login budget.

## Upgrade steps

1. Optional: set `oauth.public_url` to the controller's public https origin and list that host in `allowed_hosts`. Without it nothing changes.
2. The database gains four `oauth_*` tables automatically on start.

# 0.3.1: fixes from the live staging trial

## Fixed

1. **Identity federation condition:** GitHub's OIDC `sub` claim now embeds numeric IDs, so the Workload Identity Federation condition matches the `environment` claim plus the immutable repository and owner IDs, the branch and the workflow paths. Re-apply Terraform; the provider updates in place.
2. **Status right after dispatch:** a run that GitHub has not finished creating is reported as not visible yet instead of failing the identity check.
3. **Traffic and tag commands verify the routing state** rather than trusting gcloud's exit code, which is non-zero whenever the service's newest revision is not Ready.
4. **Failed candidates stay in place:** Cloud Run refuses to delete a service's newest revision. The failure path restores traffic, drops the tag, and records `service_ready` with a note; the previous revision keeps serving until the next successful release.
5. **Receipts record the underlying error text** (the command's ERROR line) for the failed step and for a failed restoration.

## Upgrade steps

1. Apply the Terraform change (one in-place update of the identity provider).
2. Regenerate the three app workflows with the new platform commit.

# 0.3.0: hardening against 2026 practice

## Added

1. **Rate limiting** in the authentication layer: per-identity request budgets and per-client failed-login budgets (`rate_limit_per_minute`, `auth_failures_per_minute`), answering 429 with `Retry-After`.
2. **Tool safety hints and structured output**: every MCP tool declares a title, `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, publishes an output schema and returns `structuredContent`. The demo walkthrough shows the hints.
3. **Cloud Run startup probe** from the reviewed `health_path` and port (`--startup-probe`, 60 seconds of grace), so Cloud Run itself gates traffic.
4. **Build provenance**: `actions/attest-build-provenance` signs the pushed image digest and `gh attestation verify` checks it before release; the deployment job gains `attestations: write`.
5. **Image vulnerability summary** (`labdeploy/scan.py`): waits for the Artifact Analysis scan, writes `vulnerability-summary.json` into the release receipt, and blocks the release on critical findings when `BLOCK_ON_CRITICAL_VULNERABILITIES=true`. Terraform enables the Container Scanning API (`image_vulnerability_scanning`) and grants the deployer read access to results.
6. **Repository hygiene**: ruff configuration and a clean lint, zizmor workflow linting (with one documented suppression for the recovery workflow's `workflow_run` trigger), pip-audit of the platform lock and a Terraform validation job in CI, Dependabot for actions and providers, `.github/CODEOWNERS` (placeholder owners), `SECURITY.md`, `.pre-commit-config.yaml`, and tests that the templates' action pins match the platform's own.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit; the deployment workflow has new steps and permissions.
2. Apply the Terraform change (Container Scanning API and the deployer's occurrence-viewer role; set `image_vulnerability_scanning = false` to opt out).
3. Optionally set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES=true` and the controller's rate-limit fields.
4. Replace the CODEOWNERS placeholder with real maintainers and choose a license.

# 0.2.0: cost optimizer

## Added

1. **Cost bounds in every plan** (`labdeploy/cost.py`): idle floor, worst-case compute ceiling and an optional traffic-scenario range computed from Google's published Tier 1 Cloud Run list prices, with assumptions and exclusions listed. New `estimate_cost` tool, `GET /applications/{id}/cost` route and `lab-deploy cost` CLI command.
2. **Operator cost ceiling**: `cost_ceiling_usd` per application in the controller configuration. Plans whose worst-case monthly compute exceeds it are refused at planning and again at dispatch.
3. **Cost findings** from Google's cost-optimization guidance: warm instances with their monthly price, unmeasured memory or CPU, low concurrency, the effect of `scaling.maximum`, and authenticated-only exposure.
4. **Measured right-sizing**: `templates/rightsize-gcp.yml` (weekly, read-only, pinned platform code, no application checkout) reads Cloud Monitoring usage through `labdeploy/telemetry.py`, prices it, proposes `platform.yaml` changes with estimated savings, lists `google.run.service.CostRecommender` recommendations, and uploads `cost-report.json`. New `get_cost_report` tool and `GET /applications/{id}/cost-report` route fetch the newest artifact.
5. **`concurrency`** is now a reviewed `platform.yaml` field (default 80, the Cloud Run default). The analyzer drafts 8 for synchronous frameworks and adds a review question. Existing spec hashes change; create fresh plans after upgrading.
6. **Terraform cost controls**: Artifact Registry cleanup policies (dry run by default), recovery-bucket lifecycle rules, project-level `roles/monitoring.viewer` and `roles/run.viewer` for the deployer identity, the right-sizing workflow in the WIF condition, and an optional monthly billing budget with current and forecast thresholds.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit and commit `rightsize-gcp.yml`.
2. Apply the Terraform change (new APIs, IAM grants, cleanup policies, WIF condition; budget only if configured).
3. Optionally add `cost_ceiling_usd` to each application in the controller configuration and restart the controller.
4. Create fresh plans; the spec hash now includes `concurrency`.

# 0.1.2: local demo mode and hardening

## Added

1. **Local demo mode:** `python -m labdeploy.demo serve` runs the real controller and MCP server against an application directory on disk, with dispatch permanently disabled and a repository adapter that refuses every write. `python -m labdeploy.demo walkthrough` exercises every tool and two policy refusals. `labdeploy.server:create_app` never uses this module, so the running service still substitutes no local or fake GitHub data.
2. **Plan listing and audit trail:** the `list_plans` tool and `GET /plans` return the caller's own plans, newest first. Status responses include the persisted `events` for the plan (`planned`, `dispatching`, `dispatched`, `dispatch_unknown`).
3. **Model instructions:** the MCP server sends usage instructions during the handshake: tool order, plan expiry, what `dispatched` means, and that policy refusals must be relayed rather than worked around.

## Changed

4. **Pinned base images:** both Dockerfiles pin `python:3.12-slim` to its multi-arch index digest, with the refresh command in a comment.
5. **CLI errors:** validation, policy, missing-file and overwrite failures print one plain line and exit non-zero instead of a traceback.
6. **Database connections** are closed after every operation; commit and rollback behaviour is unchanged.
7. **Authentication scheme:** `bearer` is matched case-insensitively per RFC 9110; tokens are still compared by hash.
8. **Configuration** rejects wildcard `allowed_hosts` entries, which the MCP transport would silently never match.

## Upgrade steps

No configuration or workflow changes are required. Rebuild the controller image to pick up the pinned base image.

# 0.1.1: deployment review fixes

## Fixed

1. **Stale status:** the controller now reads jobs from the exact GitHub run attempt. The returned lifecycle state and individual check results reflect the observed steps. Overall workflow success without expected step evidence is `completed_unverified`, never an invented pass. Dispatch bookkeeping stays separate so a completed plan cannot be redispatched by mistake.
2. **Unbound destination:** application registration now requires a typed cloud target. Plans carry it; the controller rejects registry drift; the workflow rejects environment drift before authentication. Project, region, service, artifact repository, deploy/probe service accounts, WIF provider and recovery bucket are included. Older plans without a target cannot deploy.
3. **Pending deployments replaced:** release and recovery workflows use the same concurrency group with `queue: max` and `cancel-in-progress: false`. GitHub currently allows up to 100 pending runs with this setting. It is not an unlimited queue, and dispatch order is not guaranteed. The installed actionlint release does not yet recognize this documented field; all other lint errors remain fatal.
4. **Interrupted releases:** before traffic changes, the runner saves its exact rollback map to a versioned GCS object. Writes use generation preconditions. A graceful interruption attempts immediate traffic restoration. A separate pinned recovery workflow runs after release completion and on a ten-minute schedule, under the same concurrency group. It recovers unfinished checkpoints, refuses target mismatches or unrelated current revisions, and never rolls back a checkpoint marked successful.

The recovery workflow does not check out or execute the triggering app's code. It operates only on the operator-configured service and saved checkpoint. Scheduled GitHub runs may be delayed or disabled; recovery requires working GitHub/GCP APIs and configured credentials. This improves crash recovery without claiming instantaneous or guaranteed availability.

## Upgrade steps

1. Publish this updated platform source and record its full commit SHA.
2. Review and apply the Terraform change: a private versioned recovery bucket, its scoped IAM grant, and the additional recovery workflow in the WIF condition.
3. Set the new `RECOVERY_BUCKET` GitHub staging environment variable from Terraform output.
4. Add each app's `target` block to the controller configuration; values must match the GitHub staging environment exactly.
5. Regenerate and review **both** `deploy-gcp.yml` and `recover-gcp.yml` using the new platform commit. The render command refuses overwrite: write temporary output files and review their diff before replacing existing workflow files.
6. Restart the controller and create fresh plans. Older workflow templates do not accept the new `target_json` dispatch input.
7. Verify a normal release, failed candidate, post-promotion failure, and killed-runner recovery in disposable staging.

## Still outside this patch

Telemetry-driven resource optimization, dollar-cost estimates, AWS support and production readiness are features/integration milestones, not silently completed by these bug fixes. The analyzer still reports conservative defaults and review assumptions. Full live cloud testing remains required.
