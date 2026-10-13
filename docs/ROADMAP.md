# Roadmap: from staging pilot to a lab platform you can trust unattended

Written 2026-10-12 from three reviews of version 0.4.0: general gaps, versioning and redeploys, and monitoring.
Every item below was a gap found in those reviews when this was written; the status lines under each phase say what has shipped since (Phases 0 to 6 by 2026-10-12, with the always-on machine of 5.1 deferred).

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
| D8 | Where infrastructure changes are applied from: a GitHub Actions pipeline in the platform repository (plan on pull request, apply on merge, Workload Identity Federation) or Google's Infrastructure Manager | Phase 6 | GitHub Actions; the operator merges, the pipeline applies |
| D9 | The first resource catalog and its cheapest defaults: Firestore as the default database, Cloud SQL Postgres only when SQL is required, Cloud Storage buckets, Pub/Sub topics, Cloud Run jobs, domain mapping; load balancer on request; Memorystore excluded | Phase 6 | As listed |
| D10 | Database safety: deletion protection always on, decommission needs a second confirmation and a final backup, migrations run as a job before traffic moves and must keep the previous revision working | Phase 6 | As listed |
| D11 | Terraform state per application, with the project-level resources in their own state (the second half of D3) | Phase 6 | Split when the pipeline arrives; the pilot state is migrated once |

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

Status: implemented on 2026-10-12 as version 0.9.0 in the laptop variant the operator chose: live views through an impersonated observer identity, auto-deploy, watchdog, backups and a GitHub controller watch. Item 5.1, the machine, is deferred; the observer identity attaches to it unchanged when the time comes.

| Item | Change | Where |
|---|---|---|
| 5.1 | Controller on a free-tier e2-micro (D6): Terraform module with Caddy for HTTPS, the controller image built by platform CI, SQLite on the disk with a snapshot schedule, the GitHub token in Secret Manager, a read-only identity for Cloud Run, Monitoring and Logging; the laptop launcher becomes the fallback | `infra/gcp/controller`, `Dockerfile`, docs |
| 5.2 | Live tools: `get_service_health(application)` (ready, serving revisions with commits, requests, 5xx, p95 latency, instances in the last hour) and `get_recent_logs(application, minutes, severity)`; the controller still cannot change anything in the cloud | `labdeploy/cloud.py`, `labdeploy/server.py` |
| 5.3 | Auto-deploy on merge (D7): `auto_deploy: true` per app; the controller watches main and creates and dispatches plans as a dedicated identity, so the cost ceiling and audit trail still apply | `labdeploy/controller.py` |
| 5.4 | Watch the watchers: uptime check on the controller's own health address; daily watchdog events for stale schedules and token expiry with notifications | Terraform, `labdeploy/server.py` |
| 5.5 | Backups: disk snapshots on the machine; on a laptop, a nightly copy of the database | Terraform, launcher |

Cost effect: machine within the free tier; a reserved external address is about 3.65 if Google charges it; uptime checks within the free allotment.

## Phase 6: everything an app needs (about eleven days) → versions 1.0 to 1.4

Status: researched on 2026-10-12 after the operator restated the goal: "the whole process of build and deploy fully automatic; any service the app needs, the platform should provide". Item 6.3, the pipeline, shipped as 1.0.0 to 1.0.2 and was exercised live (state migration, foundation apply, plan on pull request, apply on merge). Items 6.1 and 6.2 shipped as 1.1.0 for the first three types: buckets, topics and a Firestore database declared under `needs`, with derived variables, least-privilege grants, policies and cost notes (6.4's variable wiring came with them). Item 6.7 shipped as 1.2.0 (needs in the health view through the observer, and in the retirement checklist); the per-application spend half of 6.6 shipped as 1.3.0; Cloud SQL with the migration job (6.5) and the cap stopping databases (the rest of 6.6) shipped as 1.4.0. Every item of Phase 6 is implemented; the live trials per type (6.8) are in `docs/VALIDATION.md`.

### What the research says

1. **Declare, do not script.** Mature platforms let the application declare what it needs in its own file and let the platform provision it. The CNCF Score specification writes `resources: {db: {type: postgres}}` and the app reads `${resources.db.host}`; the platform resolves and provisions each resource in dependency order ([Score reference](https://docs.score.dev/docs/score-specification/score-spec-reference/)). Google's Application Design Center, generally available since December 2025, does the same with Terraform-backed templates: administrators define the components and their connections, developers configure but cannot add components ([design templates](https://docs.cloud.google.com/application-design-center/docs/design-application-templates?hl=en)). Its Cloud Run connection table is the clearest statement of what Google thinks a Cloud Run app needs: Cloud SQL, AlloyDB, Cloud Storage, Pub/Sub, Memorystore, BigQuery, Spanner, Secret Manager, load balancers, another Cloud Run service, Vertex AI, each with the exact IAM role and environment variables the connection sets ([Cloud Run connections](https://docs.cloud.google.com/application-design-center/docs/configure-cloud-run)).
2. **Infrastructure changes go through pull requests and a pipeline, never a laptop.** Google's guidance: always generate a plan first, execute Terraform through automated tooling, get credentials through Workload Identity Federation, never download keys ([operations](https://docs.cloud.google.com/docs/terraform/best-practices/operations)). The common shape is plan on pull request with a read-only identity, apply on merge with a write identity bound to the main branch. Today's four manual apply runs are the cost of not having this.
3. **Small states.** Google: "Don't include more than 100 resources (and ideally only a few dozen) in a single state" and keep each application's resources in their own directory ([root modules](https://docs.cloud.google.com/docs/terraform/best-practices/root-modules)). The staging state already holds about fifty resources for one app; a failing resource for one app blocked the others today.
4. **Policy before apply, and a live trial per resource type.** `gcloud beta terraform vet` checks a plan against a policy library before anything is created ([policy validation](https://docs.cloud.google.com/docs/terraform/policy-validation/validate-policies)). Terraform validation cannot see Google's API rules; four of them were found live today, so every new resource type needs its own staging trial before it counts as supported.
5. **Databases need a migration step and a rollback rule.** Run migrations as a Cloud Run job before traffic moves, stop the release if the job fails, and keep every schema change compatible with the previous revision (expand first, contract in a later release) so a traffic rollback stays safe ([Cloud Run jobs](https://docs.cloud.google.com/run/docs/create-jobs); pattern write-up: [serverless migrations with Cloud Run jobs](https://oneuptime.com/blog/post/2026-02-17-how-to-implement-serverless-database-migrations-using-cloud-run-jobs-and-cloud-sql/markdown)). Connect through the Cloud SQL connector with the Cloud SQL Client role, keep credentials in Secret Manager with pinned versions, pool connections, and cap instances because each container may hold at most 100 connections ([connect from Cloud Run](https://docs.cloud.google.com/sql/docs/postgres/connect-run)). Stateful resources get deletion protection ([general best practices](https://docs.cloud.google.com/docs/terraform/best-practices-for-terraform)).
6. **Cost changes character.** Cloud Run costs nothing idle; a database does not. The smallest Cloud SQL instance is about 8 a month before storage, has no SLA, and stopping it suspends instance charges while storage and address charges continue ([start and stop](https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance)). Firestore has a permanent free tier. A load balancer carries a fixed charge of about 18 a month. Memorystore has no idle-free tier. The spending cap must learn to stop databases, not only close ingress.
7. **Tool choice for a small, non-Kubernetes team.** Crossplane needs a Kubernetes cluster to run; Radius has no Google Cloud recipe pack; Humanitec is a paid orchestrator; Application Design Center is console and gcloud centred and deploys through Infrastructure Manager and Cloud Build, which would duplicate the GitHub-centred pipeline this platform already has. Plain Terraform modules behind a pull-request pipeline have the lowest operating cost and are what Google's own guidance assumes. Revisit Application Design Center if Google adds an API-driven, GitHub-native flow.

### The design

The application's reviewed `platform.yaml` gains a `resources` block in the Score style. Each resource type maps to one Terraform module that encodes Google's recommended wiring, the cheapest sane default and deletion protection. The controller renders the app's Terraform inputs from that file and opens an infrastructure pull request; the pipeline plans it with a read-only identity, posts the plan and the monthly cost change, and applies it on merge with a write identity. Terraform outputs flow into the app's GitHub environment and into the revision as settings and secrets, and the runtime identity gets exactly the roles the connections need. Releases keep their shape; a database adds one phase, the migration job, before the candidate gets traffic.

| Item | Change | Where |
|---|---|---|
| 6.1 | `resources` block in `platform.yaml`: typed entries (`database: firestore` or `postgres`, `buckets`, `topics`, `jobs`, `domain`, `load_balancer`) with validated parameters, a monthly floor per type in every plan and estimate, and analyzer hints when the code imports a database or storage client | `labdeploy/models.py`, `labdeploy/analyzer.py`, `labdeploy/cost.py`, `schema/` |
| 6.2 | One Terraform module per resource type, written from Application Design Center's connection table: the IAM role per connection, the environment variable names, Secret Manager for credentials, labels for cost allocation, `prevent_destroy` on data; the service module composes them from the declared resources | `infra/gcp/modules/resource-*` |
| 6.3 | The infrastructure pipeline (D8, D11): one state per app; `lab-deploy render-infra` writes the app's root configuration and opens a pull request; plan on pull request with a read-only identity and `terraform vet` policies (labels present, no public database addresses, no keys); apply on merge with a write identity bound to main; outputs written to the app's GitHub environment variables by the pipeline instead of by hand | `.github/workflows/infra-plan.yml`, `infra-apply.yml`, `labdeploy/cli.py`, controller |
| 6.4 | Wiring into releases: connection names, bucket and topic names reach the revision from Terraform outputs; the plan lists the resources and their floor; the cost ceiling counts them; `compare_with_live` shows resource changes as a separate kind | `templates/deploy-gcp.yml`, `labdeploy/release.py`, `labdeploy/cost.py` |
| 6.5 | Migrations as a release phase (D10): a Cloud Run job from the same image runs the app's migrate command before the candidate gets traffic; a failure stops the release with the job's log in the receipt; rollback stays traffic-only; the expand-then-contract rule is documented and checked in review | `templates/deploy-gcp.yml`, `labdeploy/release.py`, `labdeploy/status.py` |
| 6.6 | Cost and the cap: idle floors in estimates, the spending cap stops Cloud SQL instances as well as closing ingress, right-sizing covers the instance tier, the cost report shows spend per resource from the billing export through labels | `labdeploy/cost.py`, `infra/gcp/modules/project/cap-workflow.yaml` |
| 6.7 | Live views and decommission: health shows each resource (database state, bucket size, last job run); `decommission` lists the resources with their data-loss warnings and the final backup step first | `labdeploy/cloud.py`, `labdeploy/cli.py` |
| 6.8 | A live staging trial per resource type, recorded in `docs/VALIDATION.md` before the type is called supported | docs |

Order: 6.3 first, because it also removes the manual apply that cost four runs today; then 6.1 and 6.2 with the serverless types (Firestore, buckets, Pub/Sub, jobs), which cost nothing idle; then Cloud SQL with 6.5; domain and load balancer last. Each step ships as its own version with a trial, so a lab app can use buckets and Firestore weeks before Cloud SQL is ready.

Cost effect per resource, monthly, at the defaults: Firestore 0 within the free tier; a bucket about 0.02 per gigabyte; Pub/Sub 0 within the free tier; a job pays per second it runs; Cloud SQL smallest about 8 plus storage, and only storage while stopped; domain mapping 0; a load balancer about 18 fixed. The pipeline itself runs on GitHub's minutes. Out of scope for this phase: Kubernetes, virtual machines, GPUs, several regions, and production releases, which keep their own phase.

## Phase 7: one sentence (about four and a half days) → version 1.6

Status: planned on 2026-10-13 after the operator asked for "a repository link with a build file, and one sentence that gives it every Google service it needs". Today that takes the five onboarding steps in `docs/CLAUDE-CHAT.md` (two pull requests on the app repository, one on the platform repository, a configuration edit, eight variables); the platform already does the hard part. Phase 7 makes the sentence `Deploy https://github.com/ORG/REPO on GCP` sufficient for a repository that follows `docs/DEVELOPER-CONTRACT.md`.

### Decisions the operator makes before "go"

| # | Decision | Default if nothing is said |
|---|---|---|
| D12 | Merge gates: the configuration and infrastructure pull requests merge on their own when the policy passes and no review question is open, or a person merges them | Automatic for an application's own files (its `platform.yaml`, its root under `infra/gcp/apps/`, its three workflows); the foundation stays manual; every automatic merge is audited and reversible |
| D13 | Where registered applications live: the operator's `config.local.yaml` (today) or the controller's database managed by tools | The database (`register_application`, `unregister_application`), with the same validation; the file keeps principals and settings |
| D14 | GitHub access for the controller: one fine-grained token per repository (today) or a GitHub App installed once on the lab's account | A GitHub App: new repositories need no new token, and the controller can create the `staging` environment, write its variables, open and merge pull requests, with permissions limited to what onboarding needs |
| D15 | Defaults when the developer says nothing | Smallest size, scale to zero, authenticated, staging, tests required, no needs; onboarding asks only what it cannot infer (a health path that is not `/health`, missing tests) |
| D16 | Needs inference: suggest buckets, topics or a database from the code's imports | Suggest in the review questions, never add silently |

### Items

| # | Item | Where |
|---|---|---|
| 7.1 | The developer contract and a validator: `lab-deploy check-repo URL` and `analyze_repository` report the contract's checklist with every missing item, before anything is created | `docs/DEVELOPER-CONTRACT.md`, `labdeploy/analyzer.py`, `labdeploy/cli.py` |
| 7.2 | Applications registry in the database (D13): register and unregister tools with the configuration model's validation, the demo entry migrated, `list_applications` unchanged for Claude | `labdeploy/config.py`, `labdeploy/controller.py`, `labdeploy/server.py` |
| 7.3 | GitHub App authentication (D14): installation tokens per repository, permissions contents, pull requests, actions, environments and variables, metadata; the private key in the Keychain; the fine-grained token remains the fallback | `labdeploy/github.py`, `scripts/start.sh`, `docs/SETUP.md` |
| 7.4 | `onboard_application(repository_url)`: validate against the contract; open the configuration pull request; register the app; render and open the infrastructure pull request; merge per D12 when the policy passes; wait for the apply; create the `staging` environment restricted to `main` and write its variables; open and merge the workflows pull request; dispatch the first release; report a timeline. Resumable: every step checks what exists and continues, so a second call finishes an interrupted first one | `labdeploy/onboarding.py`, `labdeploy/server.py` |
| 7.5 | A live trial from one sentence on a second test repository created from the contract (`lab-demo-two`, with a bucket and a topic declared), recorded in `docs/VALIDATION.md`; then its decommission | docs, the test repository |
| 7.6 | Documents: "Deploy a repository" in `docs/CLAUDE-CHAT.md`, setup for the GitHub App, the handoff | docs |

Order: 7.1 and 7.2 first (they stand alone and remove the configuration edit), then 7.3 (it removes the token-per-repository step), then 7.4 and the trial. What stays human whatever the decisions: secret values, the budget and the cap, the foundation applies, and production, which this platform does not do.

Cost effect: none at idle; each onboarded application adds only what it declares (compute at zero when idle, a few cents of registry storage, Cloud SQL about 9 a month when chosen).

## Totals

| Phase | Effort | Workflows regenerated | Terraform apply |
|---|---|---|---|
| 0 | half a day | yes | no |
| 1 | two days | yes | yes |
| 2 | two days | yes | no |
| 3 | one and a half days | yes | yes |
| 4 | one and a half days | yes | yes |
| 5 | two days | no | yes |
| 6 | about eleven days | yes | by the pipeline from then on |
| 7 | about four and a half days | yes (the trial repository) | by the pipeline |

About nine and a half days of work for Phases 0 to 5, plus the live exercises, which take an hour or two per phase. Phase 6 adds about eleven days and a trial per resource type.
