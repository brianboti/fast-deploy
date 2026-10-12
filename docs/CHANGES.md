# 0.9.1: fix from the live run

## Fixed

1. **The observer error quoted gcloud's impersonation warning** instead of the reason the token could not be minted; it now quotes gcloud's error line.

# 0.9.0: always on, on a laptop

## Added

1. **Live views.** `get_service_health` shows what Cloud Run serves right now (readiness, revisions with the plan and commit they carry, ingress), the last hour's requests, server errors, p95 latency, instances and memory, and whether it agrees with the release this controller recorded. `get_recent_logs` returns the newest log lines at or above a severity. Both read through a viewer-only **observer identity** created by Terraform: on a machine the identity is attached; on a laptop the operator impersonates it through gcloud (`observer_impersonators`), so the controller never holds a key and never gains write access. Configure `observer.service_account`.
2. **Auto-deploy.** `auto_deploy: true` on an application makes the controller release the tip of main whenever it is neither the live release nor already attempted, as the identity `auto-deploy`, with every guardrail (pause, policy, cost ceiling, platform pin) still applied. Interval `auto_deploy_interval_seconds` (default 300).
3. **Watchdog.** Once a day the controller checks its configuration file, the GitHub credential's expiry, each app's schedules, platform pin and newest release attempt, records the result, and posts problems to `notify_webhook_url`. `lab-deploy watchdog` runs it on demand.
4. **Backups.** A consistent copy of the database every night into `backup_dir` (default next to the database), kept `backup_keep_days` (default 14). `lab-deploy backup` runs it on demand.
5. **Controller watch** from GitHub: the platform repository's `controller-watch.yml` pings the public health address every thirty minutes when the `CONTROLLER_HEALTH_URL` repository variable is set; a failed run means the laptop, the tunnel or the controller is down, and GitHub's failed-workflow email says so.
6. `get_controller_status` reports the observer, the auto-deploy apps and the background jobs' last and next runs.

The always-on machine of the roadmap's Phase 5 is deferred by the operator's choice; everything above also works there with the observer identity attached.

## Upgrade steps

1. Apply the Terraform change (observer identity, its viewer roles and your impersonation grant) and set `observer.service_account` in the configuration.
2. Optionally set `notify_webhook_url`, `auto_deploy: true` per app, and the `CONTROLLER_HEALTH_URL` variable on the platform repository.

# 0.8.0: more kinds of apps

## Added

1. **Configuration and secrets in `platform.yaml`.** `env` holds plain values set on every revision; `secrets` lists names of Secret Manager secrets called `<service>-NAME` that Cloud Run mounts as variables. The reviewed file is the whole truth: values not in it are removed from the revision. Before deploying, the runner checks that every referenced secret exists with an enabled version and names the missing ones. Terraform grants the runtime identity read access, and the deployer existence checks, only to secrets named after the service, through IAM conditions. Values are created by the operator with gcloud and never pass through Claude.
2. **Apps without tests.** `tests: none` in `platform.yaml` skips the unit-test step knowingly: the plan's checks say `skipped_by_config`, every plan carries a warning, and the run annotates itself. An unexpected skip still shows as `not_run`.
3. **Request timeout** `timeout_seconds` (1 to 300) replaces the fixed sixty seconds.
4. **Reproducible builds.** The analyzer flags a `requirements.txt` without hashes and recommends a `requirements.lock`; the test step installs such a lock with `--require-hashes` when the repository has one.
5. **`lab-deploy decommission APP`** prints the ordered checklist that retires an application without leaving a bill or a dangling trust.

## Upgrade steps

1. The specification gained fields, so every plan hash changes: regenerate the app workflows with this platform commit **before** deploying, and set `platform.minimum_commit` to it so the controller refuses older pins.
2. Apply the Terraform change (Secret Manager API and the two conditional grants).

# 0.7.1: fix from the live run

## Fixed

1. **The cost-report issue step failed** because the right-sizing job never checks out the application repository and the GitHub CLI looked for one; every call now names the repository explicitly.

# 0.7.0: real cost control

## Added

1. **Spending cap** (`spending_cap` in Terraform, on by default with a budget): every budget notification reaches a Pub/Sub topic; an Eventarc trigger runs a Cloud Workflow that closes ingress on every platform service once the budget line is crossed, and optionally detaches billing at a higher line. No code to build. Reopen a service with `gcloud run services update SERVICE --ingress=all`; the next release reopens it as well.
2. **Terraform in two modules**: `modules/project` (APIs, scanning, budget, cap, alert channels, tag role, billing dataset) and `modules/service` (one per application, keyed by service name in the `services` map). `moved.tf` migrates the pilot's single-module state in place; a plan against the staging state showed every resource moving and nothing destroyed.
3. **Living cost-report issue**: the weekly right-sizing workflow creates or updates one issue per repository with the summary, proposals, drift and actual spend (`issues: write`).
4. **`propose_rightsizing(application)`** turns the latest report's concrete proposals into a pull request against `platform.yaml`, editing values in place so comments survive, with the savings and the cost bounds before and after. The staging policy and the cost ceiling still apply.
5. **Actual spend**: with `billing_export_dataset` in Terraform, the billing export enabled once in the Billing console, and the `BILLING_EXPORT_TABLE` repository variable, the weekly report and the issue show the project's net spend by Google service next to the list-price figures.
6. **Schedule watchdog**: `list_applications` reports, per app, whether GitHub still runs the recovery and right-sizing schedules, when they last ran and how they concluded. GitHub pauses schedules after sixty idle days.
7. Image scanning is documented as a per-project cost choice (`image_vulnerability_scanning`).

## Upgrade steps

1. Rewrite `terraform.tfvars` into the `services` map (see docs/SETUP.md) and apply; the state migrates automatically.
2. Regenerate the app workflows (the right-sizing workflow changed).
3. Optional: enable the billing export to the new dataset and set `BILLING_EXPORT_TABLE`.

# 0.6.2: two more findings from the live exercise

## Fixed

1. **A reused image's release was reported as `completed_unverified`** because the skipped build job has no test and audit steps. The plan's `passed_in_earlier_release` checks are now kept and count as passed.
2. **The audit trail recorded a final outcome twice** when the record was re-saved with the recovery run's result. An event is now recorded only when the state actually changes.

# 0.6.1: fixes from the live steering exercise

## Fixed

1. **Status of a plan pinned to an older commit** was rejected as a workflow identity mismatch: GitHub reports the run's head as the branch tip, not the plan's commit. The unique plan id in the run title is the identity; the head check now applies only to plans of the tip itself.
2. **Image reuse ignored a rolled-back release.** A rolled-back release's image is still the attested image of that commit, so it now counts for reuse.
3. **Provenance verification failed when the build was skipped**, because the registry login lived in the push step. A dedicated "Configure registry access" step now runs in both modes.
4. **Stale serving tags could not be deleted**: the deployer's writer role creates tags but cannot delete them. Terraform adds a one-permission custom role on the repository, and the reconcile reports a failed deletion instead of abandoning the update.

## Upgrade steps

1. Apply the Terraform change (custom role and binding) and regenerate the app workflows.

# 0.6.0: steer after a release

## Added

1. **`rollback_staging(application)`**: traffic returns to the split that served before the live release, through the recovery workflow's new `rollback_plan_id` input. No build; seconds. The runner refuses when the checkpoint belongs to another release, the release did not complete, or traffic has moved since. The rolled-back release stops counting as live.
2. **`cancel_deployment(plan_id)`** cancels the GitHub run; the recovery workflow restores traffic if the release had started changing it.
3. **Deploy a specific commit**: `create_deployment_plan(application, commit_sha=...)` accepts any commit already on main. The controller checks the history through GitHub's compare, and the workflow checks it again before building.
4. **Image reuse**: a commit released before is redeployed from its already-attested image. The build job is skipped, the configuration comes from the commit itself, provenance is verified again, and the plan's checks say `passed_in_earlier_release`.
5. **`compare_with_live(application)`**: commits and files on main since the live release, what kinds of change they are (code, dependencies, Dockerfile, configuration, workflows), and the cost bounds before and after when `platform.yaml` changed.
6. **`deploy_latest(application)`** plans and dispatches in one call.
7. **Pause switches** honoured without a restart: `paused: true` on the whole configuration or on one application refuses new plans and dispatches; reads keep working. The controller re-reads its configuration file whenever it changes; an invalid file is reported by `get_controller_status` and ignored.
8. **`lab-deploy sessions`** lists Claude's sign-in sessions and ends one or all of them.
9. **Platform pin check**: `list_applications` reports the platform commit each app's workflows pin; with `platform.minimum_commit` set, dispatch refuses older pins and names the fix.
10. **Drift in the weekly report**: live Cloud Run settings compared with the reviewed `platform.yaml`, differences named in the report and its summary.

## Changed

- The explicit Deployments API steps added in 0.5.0 duplicated the record GitHub keeps for the deploy job's environment; the job's environment now carries the service URL instead, and the `deployments` permission is gone.
- CI passes the template files to the workflow linter explicitly; given the directory, it audited only the platform's own workflow.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (`update-workflows --all`); the deploy workflow has a new `image_digest` input and the recovery workflow a `rollback_plan_id` input.
2. Optionally set `platform.minimum_commit` to this platform commit so apps on older pins are refused until regenerated.

# 0.5.0: see every step

## Added

1. **Full timeline in status.** `get_deployment_status` reports every step of both jobs with timing, names the first failed step, fetches the release receipt and vulnerability summary once the run ends and stores them with the plan, shows the release step's own phases with timestamps (preparing, deploying, promoting, succeeded or restored), the gcloud error line, whether traffic was restored and which revision serves, the recovery run that followed a cancelled or lost release, and a plain `next_action` for every state. `wait_seconds` (up to 45) waits for a change before answering.
2. **Release history.** A finished run's outcome, receipt, revision, digest and commit are stored, so history survives GitHub's artifact retention. `list_plans` shows outcome, commit summary and revision; `list_applications` shows the live release this controller recorded and the commits main holds beyond it. Plans carry the commit message, author and date.
3. **Candidate logs.** When a candidate fails, the runner puts its last fifty log lines into the receipt (Terraform grants the deployer `roles/logging.viewer`).
4. **Revision labels, release tags, serving tags.** Every revision is labelled with its plan id and commit; every pushed image also gets an immutable `release-<plan>` tag; the release and right-sizing jobs tag each serving revision's digest `serving-<revision>` and the registry cleanup policy keeps those, so an image that still serves is never deleted.
5. **Notifications.** A final job in the deploy, recovery and right-sizing workflows posts the outcome with the receipt summary and a link to the `NOTIFY_WEBHOOK_URL` repository secret (Slack or Discord incoming webhook). Nothing is sent when the secret is unset; routine recovery runs stay quiet.
6. **GitHub Deployments.** Each release is recorded through the Deployments API (in progress, then success, failure or error, with the service URL), visible on the commit and in the repository's Environments view. The deploy job gains `deployments: write`.
7. **Audit everything.** Every tool and REST call, sign-in, failed-login burst, client registration, token reuse and revocation is recorded with identity, client address, outcome and duration. New `list_events` tool and `GET /events`; deployers see every identity, read-only identities see their own. Optional `log_file` setting writes a rotating controller log.
8. **Runtime alerts in Terraform** (`runtime_alerts`, default on): an authenticated uptime check on the health path, alert policies for the health check, 5xx responses, p95 latency, instances at maximum, memory above 80% and error lines in the log, email and webhook notification channels (`alert_email`, `alert_webhook_url`), and a console dashboard. Outputs `console_url` and `dashboard_url`.
9. **`update_app_workflows` tool and `lab-deploy update-workflows --all`.** Regenerate an app's three workflows against the platform's current (or a given) commit and open one pull request per repository. Needs `platform.repository` in the configuration and a credential with Contents, Pull requests and Workflows write on the app repositories.

## Upgrade steps

1. Add `platform: {repository: OWNER/lab-deploy}` to the configuration; optionally `log_file`.
2. Apply the Terraform change (logging viewer, cleanup policy, alerts, uptime check, dashboard). Set `alert_email` and, if wanted, `alert_webhook_url` and `health_path` in `terraform.tfvars`. In a new project create the Monitoring service agent first: `gcloud beta services identity create --service=monitoring.googleapis.com`.
3. Add the `NOTIFY_WEBHOOK_URL` secret to each app repository if notifications are wanted.
4. Regenerate the three app workflows with the new platform commit, now with `update-workflows --all`.

# 0.4.1: housekeeping and the two cost leaks

## Changed

1. **Recovery schedule** runs every six hours instead of every ten minutes. The completion trigger after each release handles the normal case, and a lost runner leaves traffic on the previous revision, so recovery is not urgent. On a private repository the old schedule consumed roughly 6,000 to 9,000 Actions minutes a month.
2. **Cost estimates list per-release overheads** outside the Cloud Run figures (the image scan fee of about USD 0.26 per release, Actions minutes, registry storage) and warn when the recorded list prices are more than 90 days old.
3. **GitHub failures are explained in plain words** to Claude and to REST callers: expired token, missing permission, rate limit, missing repository or file, refused dispatch, outage. An app without `platform.yaml` is told to run `propose_configuration`.
4. **New `get_controller_status` tool**: version, dispatch and sign-in switches, registered apps, GitHub credential expiry with a two-week warning, pricing freshness and uptime. The MCP handshake reports Lab Deploy's version, and the controller logs a warning at startup when the credential is about to expire.
5. **`lab-deploy sync-pins`** copies action pins from the platform CI workflow into the templates, so a Dependabot update becomes one command. `actions/checkout` and `actions/setup-python` move to v7.0.1 and v7.0.0.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (schedule and action pins changed).
2. Dependabot's Terraform provider update (google 7.x to 8.x) is deferred to the Terraform restructuring planned in `docs/ROADMAP.md`.

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
