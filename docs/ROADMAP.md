# Roadmap: from staging pilot to a lab platform you can trust unattended

Written 2026-10-12 from three reviews of version 0.4.0: general gaps, versioning and redeploys, and monitoring.
Every item below is a gap found in those reviews. Nothing here is implemented yet.

Order of phases is by value per effort with cost first, then control, then scope, then the always-on move.
Each phase ships as one platform version with tests, lint, `docs/CHANGES.md`, and a live staging exercise.
Any phase that changes runner code (`labdeploy/release.py`, `labdeploy/models.py`, `labdeploy/cost.py`,
`labdeploy/scan.py`, the templates) requires regenerating the three workflow files in every application
repository with the new platform commit. Phase 1 therefore starts with a command that does that for all apps.

Effort is one person's focused work, including tests and the staging exercise. Cost figures are monthly, USD.

## Decisions the operator makes before each phase

| # | Decision | Needed by | Default if nothing is said |
|---|---|---|---|
| D1 | Where notifications go: Slack, Discord, email only | Phase 1 | GitHub's built-in failure emails plus one webhook secret per repo if provided |
| D2 | Spending-cap action at the budget line: close ingress on the platform's services, detach billing from the project, or both at different thresholds | Phase 3 | Close ingress at 100% of budget; detach billing off |
| D3 | Terraform layout for several apps in one project: one state per project plus one per app | Phase 3 | Split as described; migrate the pilot state once |
| D4 | Apps without tests: allowed with a visible warning, or refused | Phase 4 | Allowed when `checks.tests: none` is written in `platform.yaml` |
| D5 | Secret names and who creates values: operator with `gcloud`, never Claude | Phase 4 | Secret Manager, named `<service>-<NAME>` |
| D6 | Hostname for the always-on controller and whether it gets read-only cloud access | Phase 5 | University subdomain if available, otherwise a cheap domain; read-only access yes |
| D7 | Auto-deploy on merge, per app | Phase 5 | Off; enabled per app with `auto_deploy: true` |

## Phase 0: housekeeping and the two cost leaks (half a day)

Status: done on 2026-10-12 as version 0.4.1, except 0.8, which is the operator's GitHub setting.

| Item | Change | Where | Cost effect |
|---|---|---|---|
| 0.1 | Commit version 0.4.0 (OAuth sign-in), already tested and live | repository | none |
| 0.2 | Recovery cron from every 10 minutes to every six hours; the deploy-completion trigger already covers the normal case, and a lost runner leaves traffic on the previous revision, so recovery is not urgent | `templates/recover-gcp.yml`, docs | saves about 30 to 55 per private app repo |
| 0.3 | Cost summary names per-release overheads: image scan fee, Actions minutes on private repos, registry storage while cleanup is in dry run | `labdeploy/cost.py`, plan output, README | honesty only |
| 0.4 | Warning in every estimate when the recorded list prices are older than 90 days | `labdeploy/cost.py` | none |
| 0.5 | Plain-language errors for GitHub failures: expired token, missing `platform.yaml`, rate limit, outage; startup warning when the token expires within 14 days (GitHub's expiration header) | `labdeploy/github.py`, `labdeploy/server.py`, launcher | none |
| 0.6 | Report Lab Deploy's own version in the MCP handshake | `labdeploy/server.py` | none |
| 0.7 | Merge the three open Dependabot pull requests after syncing template pins; add `cli sync-pins` so the next ones are one command | `labdeploy/cli.py`, templates | none |
| 0.8 | Operator turns on GitHub's failed-workflow email notifications | GitHub settings | none |

Requires regeneration of app workflows: yes (0.2).

## Phase 1: see every step (two days) → version 0.5.0

Status: implemented on 2026-10-12 as version 0.5.0 and exercised live the same day (see docs/VALIDATION.md); the Terraform apply with the alerts and the two new roles is the operator's command to run.

| Item | Change | Where |
|---|---|---|
| 1.1 | `cli update-workflows --all`: regenerate the three workflows for every registered app and open one pull request per repo with the new platform commit | `labdeploy/cli.py`, `labdeploy/github.py` |
| 1.2 | Full timeline in status: every step of both jobs with timing, the first failed step named, the release receipt fetched and stored once the deploy job ends, release phases shown separately (preparing, candidate, probe, promoted, final probe, cleanup), the gcloud error line, whether traffic was restored and which revision serves, the recovery run that followed a cancelled or lost release, and a plain next action for every failure class | `labdeploy/status.py`, `labdeploy/controller.py`, `labdeploy/github.py` |
| 1.3 | Release history: when status sees a final result it is stored with revision, image digest and commit; `list_plans` becomes history; `list_applications` reports the live release, when it shipped, and how many commits on main are not deployed | `labdeploy/controller.py` (store), `labdeploy/github.py` (compare API) |
| 1.4 | Commit facts in every plan: message, author, date, short SHA | `labdeploy/github.py`, `labdeploy/controller.py` |
| 1.5 | `get_deployment_status(plan_id, wait_seconds)` waits up to 45 seconds for a change before answering | `labdeploy/server.py`, `labdeploy/controller.py` |
| 1.6 | Candidate logs on failure: the runner reads the failed revision's last 50 log lines into the receipt | `labdeploy/release.py`; Terraform grants `roles/logging.viewer` to the deployer |
| 1.7 | Notification job at the end of deploy, recovery and right-sizing workflows: success or failure with the receipt summary and a link, to a webhook secret when set (D1) | templates |
| 1.8 | GitHub Deployments API: each release shows as "deployed to staging" on the commit with status and URL (`deployments: write`) | `templates/deploy-gcp.yml` |
| 1.9 | Cloud Run revisions labelled with commit and plan id; each release also pushes an immutable `release-<plan>` tag; the weekly job tags serving digests `live` so cleanup never deletes an image that still serves | `labdeploy/release.py`, `labdeploy/cost.py`, Terraform cleanup policy |
| 1.10 | Audit everything: every tool call with identity, outcome and duration; sign-ins; failed-login bursts; token reuse; a `list_events` tool; a controller log file with rotation; a launch agent so the laptop controller restarts after a crash | `labdeploy/server.py`, `labdeploy/oauth.py`, `labdeploy/controller.py`, launcher |
| 1.11 | Runtime alerts in Terraform: authenticated uptime check on the health path, alert policies for 5xx rate, p95 latency, instances at maximum, memory above 80%, and error lines in logs; email and webhook channels; a console dashboard whose link appears in status answers | `infra/gcp` |

Live exercise: a healthy release, a candidate that crashes at startup (logs in receipt, notification received, timeline correct), a cancelled release (recovery run shown), an uptime alert fired by a deliberately broken health path.
Requires regeneration of app workflows: yes. Terraform apply: yes (logging viewer, alerts, cleanup policy).
Cost effect: none; alerts, uptime checks and logs stay within Google's free allotment at lab traffic.

## Phase 2: steer after a release (two days) → version 0.6.0

Status: implemented on 2026-10-12 as version 0.6.0 and exercised live the same day; six findings fixed in 0.6.1 and 0.6.2 (see docs/VALIDATION.md).

| Item | Change | Where |
|---|---|---|
| 2.1 | `cancel_deployment(plan_id)`: cancels the GitHub run; recovery handles the rest | `labdeploy/github.py`, `labdeploy/server.py` |
| 2.2 | `rollback_staging(application)`: traffic-only rollback to the previous split recorded by the last release, through the recovery workflow with a new input; no rebuild, seconds | `templates/recover-gcp.yml`, `labdeploy/release.py` |
| 2.3 | Deploy by commit: `create_deployment_plan(application, commit)` for any commit that is an ancestor of main with a reviewed config; the workflow verifies the ancestry itself | `labdeploy/controller.py`, `labdeploy/github.py`, `templates/deploy-gcp.yml` |
| 2.4 | Image reuse: a commit released before is redeployed from its already-attested image; provenance is verified before use; the run records which build it reused | `templates/deploy-gcp.yml`, `labdeploy/controller.py` |
| 2.5 | `compare_with_live(application)`: commits and files between the live release and main, flags for Dockerfile, dependency and config changes, cost bounds before and after | `labdeploy/github.py`, `labdeploy/controller.py`, `labdeploy/server.py` |
| 2.6 | `deploy_latest(application)`: plan and dispatch in one call when every policy passes; keeps the destructive safety hint so Claude confirms first | `labdeploy/server.py` |
| 2.7 | Per-app `paused: true` and a global pause, both honoured without restart: the controller reloads its configuration when the file changes and keeps the old one if the new one is invalid | `labdeploy/config.py`, `labdeploy/server.py` |
| 2.8 | `cli sessions` to list and end Claude sign-in sessions | `labdeploy/cli.py`, `labdeploy/oauth.py` |
| 2.9 | Platform pin check: at plan time the controller reads the app's workflow, extracts the pinned platform commit and refuses unsupported pins with the command to fix them | `labdeploy/controller.py`, `labdeploy/github.py` |
| 2.10 | Drift check in the weekly report and in status: live Cloud Run settings compared with `platform.yaml`, differences named | `labdeploy/cost.py`, `templates/rightsize-gcp.yml` |

Live exercise: cancel during build, rollback after a healthy release, redeploy an older commit from its reused image, a paused app refused, a console edit reported as drift.
Requires regeneration of app workflows: yes. Terraform apply: no.

## Phase 3: real cost control (one and a half days) → version 0.7.0

Status: implemented on 2026-10-12 as version 0.7.0; the Terraform apply (now including the migration to modules and the cap) and the billing-export console step are the operator's.

| Item | Change | Where |
|---|---|---|
| 3.1 | Spending cap (D2): budget alerts publish to Pub/Sub; a small function closes ingress on every platform service at the chosen threshold and, if enabled, detaches billing at a higher one; a notification says what it did and how to reopen | `infra/gcp`, `infra/gcp/cap-function` |
| 3.2 | Terraform split (D3): a project module for APIs, scanning, budget, cap, channels; a service module per app; one documented state migration for the pilot | `infra/gcp/project`, `infra/gcp/service`, docs |
| 3.3 | Weekly cost report opens or updates a GitHub issue with the summary; `propose_rightsizing(application)` turns accepted proposals into a pull request against `platform.yaml` | `templates/rightsize-gcp.yml`, `labdeploy/github.py`, `labdeploy/server.py` |
| 3.4 | Actual spend next to list-price figures: billing export to BigQuery (console step by the operator) read by the weekly job | `labdeploy/cost.py`, Terraform roles, docs |
| 3.5 | Schedule watchdog: a weekly check that each app's recovery and right-sizing schedules ran, because GitHub pauses schedules after sixty idle days | platform CI or controller |
| 3.6 | Per-project switch for image scanning documented as a cost choice for staging | docs |

Live exercise: force the budget threshold with a tiny budget and confirm ingress closes and the notification arrives; reopen with one command.
Requires regeneration of app workflows: yes (3.3). Terraform apply: yes.
Cost effect: cap can only reduce spend; BigQuery export stays within the free tier.

## Phase 4: more kinds of apps (one and a half days) → version 0.8.0

Status: implemented on 2026-10-12 as version 0.8.0; the Secret Manager grants join the operator's pending Terraform apply.

| Item | Change | Where |
|---|---|---|
| 4.1 | Secrets and plain environment variables (D5): `env` and `secrets` in `platform.yaml`; the runner checks the secrets exist before deploying; the runtime identity may read only its own app's secrets | `labdeploy/models.py`, `labdeploy/release.py`, Terraform, docs |
| 4.2 | Tests policy (D4): `checks.tests: none` allowed with a warning in every plan; `propose_configuration` also drafts a health test so new apps start with one | `labdeploy/models.py`, templates, `labdeploy/analyzer.py` |
| 4.3 | Request timeout in the spec, up to 300 seconds in staging | `labdeploy/models.py`, `labdeploy/release.py` |
| 4.4 | Reproducible builds: the analyzer flags requirements without hashes; the workflow uses `--require-hashes` when a lock file is present | `labdeploy/analyzer.py`, templates, docs |
| 4.5 | Decommission procedure: documented steps and a `cli decommission` that prints them for one app | `labdeploy/cli.py`, docs |
| 4.6 | Later, not in this phase: container-only runtime for non-Python apps, batch jobs, preview deployments per branch |

Requires regeneration of app workflows: yes. Terraform apply: yes (Secret Manager).

## Phase 5: always on, and live answers (two days) → version 0.9.0

| Item | Change | Where |
|---|---|---|
| 5.1 | Controller on a free-tier e2-micro (D6): Terraform module with Caddy for HTTPS, the controller image built by platform CI, SQLite on the disk with a snapshot schedule, the GitHub token in Secret Manager, a read-only identity for Cloud Run, Monitoring and Logging; the laptop launcher becomes the fallback | `infra/gcp/controller`, `Dockerfile`, docs |
| 5.2 | Live tools: `get_service_health(application)` (ready, serving revisions with commits, requests, 5xx, p95 latency, instances in the last hour) and `get_recent_logs(application, minutes, severity)`; the controller still cannot change anything in the cloud | `labdeploy/cloud.py`, `labdeploy/server.py` |
| 5.3 | Auto-deploy on merge (D7): `auto_deploy: true` per app; the controller watches main and creates and dispatches plans as a dedicated identity, so the cost ceiling and audit trail still apply | `labdeploy/controller.py` |
| 5.4 | Watch the watchers: uptime check on the controller's own health address; daily watchdog events for stale schedules and token expiry with notifications | Terraform, `labdeploy/server.py` |
| 5.5 | Backups: disk snapshots on the machine; on a laptop, a nightly copy of the database | Terraform, launcher |

Cost effect: machine within the free tier; a reserved external address is about 3.65 if Google charges it; uptime checks within the free allotment.

## Totals

| Phase | Effort | Workflows regenerated | Terraform apply |
|---|---|---|---|
| 0 | half a day | yes | no |
| 1 | two days | yes | yes |
| 2 | two days | yes | no |
| 3 | one and a half days | yes | yes |
| 4 | one and a half days | yes | yes |
| 5 | two days | no | yes |

About nine and a half days of work in total, plus the live exercises, which take an hour or two per phase.
