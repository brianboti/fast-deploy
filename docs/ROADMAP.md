# Roadmap: from staging pilot to a lab platform you can trust unattended

Written 2026-10-12 from three reviews of version 0.4.0: general gaps, versioning and redeploys, and monitoring.
Every item below was a gap found in those reviews when this was written; the status lines under each phase say what has shipped since (Phases 0 to 6 by 2026-10-12, with the always-on machine of 5.1 deferred).
Phases 9 to 12 were added on 2026-10-14 from the research review `docs/RESEARCH-2026-10-14.md`; their decisions are D21 to D30.
Phases 13 to 17 were added on 2026-10-16 from the research review `docs/RESEARCH-2026-10-16.md` (a repository as it is; production level); their decisions are D31 to D40.
Phases 18 and 19 were added on 2026-10-19 from the research review `docs/RESEARCH-2026-10-19.md` (Node.js web services; what a data-bearing application needs); their decisions are D41 to D47.
Phases 20 to 22 were added on 2026-10-20 from the research review `docs/RESEARCH-2026-10-20.md` (any Python or Node.js web service, as it is); their decisions are D48 to D56.

Order of phases is by value per effort with cost first, then control, then scope, then the always-on move.
Each phase ships as one platform version with tests, lint, `docs/CHANGES.md`, and a live staging exercise.
Any phase that changes runner code (`labdeploy/release.py`, `labdeploy/models.py`, `labdeploy/cost.py`,
`labdeploy/scan.py`, the templates) requires regenerating the three workflow files in every application
repository with the new platform commit. Phase 1 therefore starts with a command that does that for all apps. Since
1.20.4 the controller does it by itself after each release, once `platform_updates` is set, and updates its own machine
first (docs/RUNBOOK.md, "After a release").

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

Status: implemented on 2026-10-12 as version 0.9.0 in the laptop variant the operator chose: live views through an impersonated observer identity, auto-deploy, watchdog, backups and a GitHub controller watch. Item 5.1, the machine, was deferred here and built in Phase 15 (1.17.0).

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

Status: shipped as 1.6.0 on 2026-10-13 except the GitHub App (7.3, a wider fine-grained token stood in until Phase 15 built it in 1.17.0) and the live trial (7.5, recorded in `docs/VALIDATION.md` when run). Planned on 2026-10-13 after the operator asked for "a repository link with a build file, and one sentence that gives it every Google service it needs". Today that takes the five onboarding steps in `docs/CLAUDE-CHAT.md` (two pull requests on the app repository, one on the platform repository, a configuration edit, eight variables); the platform already does the hard part. Phase 7 makes the sentence `Deploy https://github.com/ORG/REPO on GCP` sufficient for a repository that follows `docs/DEVELOPER-CONTRACT.md`.

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

## Phase 8: pages for people (about two and a half days) → version 1.7

Status: items 8.2, 8.3 (probes and the pre-check), 8.4 and 8.6 shipped as 1.7.0 on 2026-10-13; 8.1 (the console step) and the trial (8.5) follow, recorded in `docs/VALIDATION.md`. Planned on 2026-10-13 after the operator said every lab application is a Python backend with UI pages connected to it. Today every service is deployed as `authenticated`: callers must present a Google identity token, which a person's browser cannot do, so the pages answer 403 to people. The backend and the served pages are already built and deployed correctly; what is missing is a sign-in for people in front of the service.

The design is Google's Identity-Aware Proxy (IAP) enabled directly on the Cloud Run service (no load balancer, no extra monthly cost): a Google sign-in page in front of the app, only the lab's listed accounts allowed, the app unchanged. Sources: [IAP on Cloud Run](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run) (`--iap`, Terraform `iap_enabled`, `roles/iap.httpsResourceAccessor` per service, the IAP service agent as invoker, a custom OAuth client for a project without an organization, first enablement in the console) and [Enabling IAP for Cloud Run](https://docs.cloud.google.com/iap/docs/enabling-cloud-run).

### Decisions the operator makes before "go"

| # | Decision | Default if nothing is said |
|---|---|---|
| D17 | Who may open an app's pages: a list per app, a lab-wide list, or both | Both: `onboarding.default_viewers` (the lab's Google accounts) applied to every onboarded app, plus `viewers` per app in the controller's registry; the operator edits them, developers cannot grant themselves |
| D18 | Which accounts: personal Google accounts, a Google group, or a Workspace domain | Any Google account listed by email (`user:`), groups when the lab has one (`group:`); the project has no organization, so a custom OAuth client with an External brand is created once in the console (the only manual step) |
| D19 | Exposure values in `platform.yaml` | `authenticated` (programs only, today's default) and `lab-members` (people through IAP); `public` stays refused |
| D20 | Probes and the uptime check behind IAP | The probe identity gets the IAP accessor role and presents IAP-aware tokens; the uptime check keeps its service-agent authentication only if IAP accepts it in the trial, otherwise it moves to a synthetic check through IAP |

### Items

| # | Item | Where |
|---|---|---|
| 8.1 | One-time enablement in the console (operator): IAP API, the custom OAuth client with "Auto generate credentials" on the demo service, recorded in `docs/SETUP.md` | docs |
| 8.2 | Terraform in the service module: `iap_enabled` from a variable, the IAP service agent as invoker, `google_iap_web_cloud_run_service_iam_member` per viewer; the renderer passes `exposure` and the viewer list; policy: `lab-members` requires a non-empty viewer list, `public` refused | `infra/gcp/modules/service`, `labdeploy/infra.py` |
| 8.3 | The runner: `--iap` on deploy when `exposure: lab-members`, the candidate and final probes through IAP with the probe identity (D20), the receipt names the sign-in | `labdeploy/release.py` |
| 8.4 | The model and the health view: `exposure: lab-members`, `viewers` in the registry and the configuration, `get_service_health` shows who may open the pages | `labdeploy/models.py`, `labdeploy/config.py`, `labdeploy/controller.py` |
| 8.5 | A live trial on `lab-demo-two` with a page: a listed account opens it after Google sign-in, an unlisted one is refused, programs still reach the API with tokens | docs |
| 8.6 | Documents: the contract (what the app must and must not do about sign-in: trust the `X-Goog-Authenticated-User-Email` header IAP adds, never roll its own login), the chat guide, the runbook | docs |

Order: 8.1 by the operator, then 8.2 and 8.4 (one apply per app through the pipeline), 8.3, the trial, the documents. What stays human: the console enablement once, and the viewer lists.

Cost effect: IAP on Cloud Run has no charge of its own; no load balancer is created.

## Decisions the operator makes before Phases 9 to 12

Planned on 2026-10-14 from the research review `docs/RESEARCH-2026-10-14.md` (its section 7 ranks the items). The
order is the roadmap's rule: value per effort, cost first; Phase 9 is the cheapest and protects the budget, Phase 10
removes the gaps the real lab application exposed, Phase 11 makes onboarding easy, Phase 12 makes approvals legible
and the platform survivable without its one operator. Item 12.1 can be pulled into Phase 9 if trust is more urgent
than ease. What stays human whatever the decisions: the console step for the native cap, secret values, the foundation
applies, naming the second operator, and merging the checker's pull requests on application repositories.

| # | Decision | Needed by | Default if nothing is said |
|---|---|---|---|
| D21 | The native budget spend cap: its amount and who creates it | Phase 9 | The operator creates it once (`scripts/spend-cap.sh create`, or the Billing console) for the staging project and Cloud Run, at the same amount as the budget; the platform's own cap keeps closing ingress and stopping databases, which the native cap does not do; both layers are named in the runbook |
| D22 | The bake after a release: how long the new revision is watched after traffic moves, and what makes it fail | Phase 9 | Five minutes; fail on a crash or restart of the new revision, on 5xx answers to more than five percent of at least twenty requests, or on p95 latency above three times the previous revisions' over the hour before the switch when both saw twenty requests; no traffic is inconclusive and passes with a note; a failure restores the previous traffic automatically, and the receipt says `traffic_restored` with `failed_phase: baking` |
| D23 | Where a service lives in a repository | Phase 10 | `path` per application (the repository root when absent); the Dockerfile, `platform.yaml`, requirement files and tests live at that path; the build context is that path unless `platform.yaml` says `build: {context: repository}` for shared code |
| D24 | Workflow files when a repository holds several services | Phase 10 | One set of the three workflows per service, named `deploy-gcp-<service>.yml`, `recover-gcp-<service>.yml` and `rightsize-gcp-<service>.yml`, with a concurrency group per service; a single-service repository keeps today's names |
| D25 | Front-ends | Phase 10 | One mode, `served`: the API container serves its static files behind the same sign-in; Firebase Hosting for public front-ends stays out because public exposure is refused by design |
| D26 | What the repository checker may write for a developer | Phase 11 | A draft pull request on a branch of the application repository with the files the contract requires and the repository lacks (`Dockerfile`, `.dockerignore`, `requirements-dev.txt`, `requirements.lock`, a health test, `platform.yaml` with `reviewed: false`, `.github/workflows/ci.yml`, `SECURITY.md`, `.github/dependabot.yml`); never a file that already exists; never merged by the platform |
| D27 | Where the lock file is compiled | Phase 11 | In a container of the application's own Python on the controller's machine (Docker is present there); when it is not, the pull request carries the exact command instead of the file |
| D28 | Building without a Dockerfile | Phase 11 | Google's buildpacks builder, pinned by digest, in the same build job; a Dockerfile wins when present; the trial decides whether no-Dockerfile becomes the contract's default. Decided 2026-10-15: the Dockerfile stays the default and the checker drafts one, because the buildpack image of the demo app was about 394 MB against 52 MB; a `Procfile` alone still builds |
| D29 | Who approves what a person asked for, once a second operator exists | Phase 12 | GitHub's required review with CODEOWNERS on the platform repository; the application's `auto_merge` switch turns off for pull requests the controller opened on that person's behalf |
| D30 | Drift: how often it is looked for and where it is reported | Phase 9 | A weekly read-only `terraform plan` of every application root by the plan identity, differences posted to a living issue on the platform repository, where the pipeline runs; a drift in IAM, networking or exposure also raises the watchdog notification |

## Phase 9: cost and safety layers that cost nothing (about three days) → version 1.9.0

Status: planned on 2026-10-14 (research items 1, 7, 8 and 12) and implemented the same day as 1.9.0 (`docs/CHANGES.md`) and exercised live that night: a bake that passed, one that restored traffic and one inconclusive, the planted violations refused, a drift found, repaired and closed, and the native spend cap tripped, lifted and re-armed (`docs/VALIDATION.md`); 1.9.1 fixed the three things the exercise found. Differences from the plan below: the native cap is recorded under a top-level `cloud_run_spend_caps` (a cap belongs to a project, and the onboarding block is optional); the drift check is its own workflow, `infra-drift.yml`, so the pull-request plan workflow stays unchanged; and the plan workflows store no plan files at all, so 9.4's retention point had nothing to shorten. Every item has zero idle cost.

| Item | Change | Where |
|---|---|---|
| 9.1 | Native spend cap (D21): the console step in SETUP; the runbook's entry for the day it trips (services answer 5xx, recovery takes up to an hour, the budget cannot be edited while enforced); `onboarding.native_spend_cap: true` in the configuration once it exists, so the cost report and `get_controller_status` name both layers and `estimate_cost` warns while it is missing | docs/SETUP.md, docs/RUNBOOK.md, labdeploy/config.py, labdeploy/cost.py, labdeploy/server.py |
| 9.2 | Bake after promotion (D22): a release phase `baking` between `promoting` and `succeeded`; the runner reads the new revision's request logs and container logs through the Logging API (the deployer already holds the logging viewer role) and the Monitoring API for the window, applies the rules, and on failure restores the previous traffic through the existing restore path; the receipt and the status show the window's numbers; the deploy job's timeout grows from 20 to 30 minutes | labdeploy/release.py, labdeploy/status.py, labdeploy/telemetry.py, templates/deploy-gcp.yml, docs/CLAUDE-CHAT.md |
| 9.3 | hadolint in the build job, downloaded by version and checksum like Cosign: warnings in this version, errors blocking one version later; the contract's Dockerfile section gains the dependencies-first layer order | templates/deploy-gcp.yml, docs/DEVELOPER-CONTRACT.md |
| 9.4 | Policy checker: the data-protection rules the research found most neglected (public-access prevention and uniform access on every bucket, versioning on the recovery and backup buckets, backups and SSL on Cloud SQL, deletion protection on every data resource), each with a planted-violation plan file as a negative test; a test that no rendered root can put a secret value into state or plan output; plan artifacts kept three days | labdeploy/infra.py, tests/, .github/workflows/infra-plan.yml |
| 9.5 | Drift (D30): a weekly schedule in infra-plan.yml running `terraform plan -detailed-exitcode -lock=false` per root with the plan identity; the foundation's plan provider accepts the `schedule` event as well as `pull_request` (apply stays pinned to its workflow, branch and environment); differences posted to the living issue and, for IAM, networking and exposure, to the watchdog | .github/workflows/infra-plan.yml, infra/gcp/modules/project/pipeline.tf, labdeploy/scheduler.py |
| 9.6 | Per-event cost tolls in the weekly report: the month's releases times the scan fee, Actions minutes used on private repositories, billing-export query bytes; the README and the cost documents say that Cloud Run's recommender covers only the billing mode, so the measured report is the sizing signal, memory first | labdeploy/cost.py, templates/rightsize-gcp.yml, README.md |

Live exercise: a release whose candidate answers 500 to a fifth of requests after promotion (restored by the bake), a healthy release with no traffic (inconclusive, passes with a note), a planted public bucket refused by the plan, a console edit to a service reported by the weekly drift, the native cap tripped with a tiny budget and lifted.
Requires regeneration of app workflows: yes (9.2, 9.3). Terraform apply: yes (9.5, foundation, by the operator).
Cost effect: none; the bake reads logs and metrics within the free allotment.

## Phase 10: one repository, several services (about four and a half days) → version 1.10.0

Status: shipped on 2026-10-15 as 1.10.0 (pull request #44); live on 2026-10-15 (`docs/VALIDATION.md`): the first service of a two-service repository onboarded with one sentence and released; the second, with Postgres by password, found a missing permission fixed in 1.12.1, then migrated, released and served rows through its `DATABASE_URL`; on 2026-10-16 Sophia's copy was onboarded again from `backend/` without its root files and released (1.12.3 fixed what that found). Planned on 2026-10-14 (research items 2 and 3, and the gaps the Sophia copy exposed). This phase removes the root-Dockerfile requirement Sophia's copy needed and gives the contract its answer for front-ends, caches, queues and workers.

| Item | Change | Where |
|---|---|---|
| 10.1 | `path` per application (D23): the registry, `onboard_application(url, path=)`, the analyzer reading files at the path, plans binding the path, the build job's context and working directories, the test and audit steps at the path, `list_applications` showing it | labdeploy/config.py, labdeploy/onboarding.py, labdeploy/analyzer.py, labdeploy/controller.py, labdeploy/github.py, templates/deploy-gcp.yml |
| 10.2 | Several services per repository (D24): per-service workflow names and concurrency groups, `update_app_workflows` and the onboarding `workflows` step rendering one set per service and touching only that service's files, one Terraform root per service as today | labdeploy/workflows.py, templates/*.yml, labdeploy/onboarding.py, labdeploy/controller.py |
| 10.3 | `build: {context: repository}` for shared code in a monorepo, with the `.dockerignore` rule for it in the contract | labdeploy/models.py, templates/deploy-gcp.yml, docs/DEVELOPER-CONTRACT.md |
| 10.4 | The component rule in the contract (the table in the research review, section 4.1): each HTTP component its own service, data under `needs`, no cache by default, workers through Pub/Sub push or a Cloud Run job, front-ends served by the API container (D25), proxies and telemetry as sidecars later; the analyzer's hints say what to do when it sees redis, celery, rq, a worker entry point or a `package.json` | docs/DEVELOPER-CONTRACT.md, labdeploy/analyzer.py, skills/lab-deploy-operator |
| 10.5 | Compose import: `analyze_repository` reads `compose.yaml` when present and drafts one `platform.yaml` per HTTP service with review questions mapping databases to `needs`, caches to the rule and volumes to buckets; never a deployment unit | labdeploy/analyzer.py, labdeploy/server.py |
| 10.6 | Postgres by password (the Sophia gap): `needs.database_auth: password` adds a built-in database user whose generated password is written once to Secret Manager through a write-only argument so it never enters state, and reaches the service as `DATABASE_URL`; IAM sign-in stays the default and the contract says why | infra/gcp/modules/service/needs.tf, labdeploy/infra.py, labdeploy/models.py, labdeploy/release.py |
| 10.7 | Documents and the skill: the repository standard's section on monorepos, CLAUDE-CHAT's onboarding sentence with a path, the handoff | docs/, skills/lab-deploy-operator |

Live exercise: a test repository with an API in `services/api` and a second service in `services/admin`, each onboarded with one sentence; the Sophia copy re-onboarded from `backend/` without the root Dockerfile; an application with `database_auth: password` connecting through `DATABASE_URL`; a Compose file turned into two drafts.
Requires regeneration of app workflows: yes. Terraform apply: by the pipeline (10.6 adds the Secret Manager write-only path; the provider's write-only argument is verified in the trial).
Cost effect: none at idle; a second service in a repository is another scale-to-zero service.

## Phase 11: easy, the platform writes the boilerplate (about five days) → version 1.11.0

Status: shipped on 2026-10-15 as 1.11.0; the buildpack comparison ran locally (D28 decided); live on 2026-10-15: the checker and its draft on a bare repository, Compose drafts, onboarding measures; on 2026-10-16 the bare repository was onboarded with one sentence after a person merged the draft, and released again without a Dockerfile through buildpacks (203 MB against 53 MB). Planned on 2026-10-14 (research items 5, 6 and 11).

| Item | Change | Where |
|---|---|---|
| 11.1 | `check_repository(url, path)`: the contract's checklist with each missing item and whether the platform can write it; the same check runs first in `onboard_application` and stops with the list | labdeploy/analyzer.py, labdeploy/server.py, labdeploy/onboarding.py |
| 11.2 | The checker writes (D26): `propose_configuration` gains the missing files from templates, detecting the server (uvicorn, gunicorn, Flask), the Python version and the health path; the lock compiled per D27; the pull request explains each file in one line and carries `reviewed: false` | templates/repo/, labdeploy/analyzer.py, labdeploy/github.py, labdeploy/controller.py |
| 11.3 | Buildpack trial (D28): when the path has no Dockerfile, the build job runs `pack build` with Google's builder pinned by digest and `--platform linux/amd64`, with a `Procfile` the checker writes; the same `docker save`, push, provenance, scan and deploy follow; measured against the Dockerfile path on the demo app for build time, image size and cold start | templates/deploy-gcp.yml, labdeploy/analyzer.py, docs/VALIDATION.md |
| 11.4 | The contract page opens with the three things a developer must do (a service that listens on `$PORT` and answers its health path, pinned requirements, tests) and moves everything else under "the platform writes these for you" | docs/DEVELOPER-CONTRACT.md, skills/lab-deploy-operator/references |
| 11.5 | Measures: the registry records when onboarding started and finished and how many steps blocked and were resolved by the operator; `list_applications` and the weekly issue show onboarding time and interventions per application | labdeploy/onboarding.py, labdeploy/controller.py, templates/rightsize-gcp.yml |

Live exercise: a repository with only `app.py`, `requirements.txt` and a test, onboarded by one sentence after merging the checker's pull request; the same repository without a Dockerfile released through the buildpack path; the demo app on both paths compared.
Requires regeneration of app workflows: yes (11.3). Terraform apply: no.
Cost effect: none; the builder image pull adds Actions minutes on private repositories (about two per build, measured in the trial).

## Phase 12: legible approvals and the operator's succession (about three days) → version 1.12.0

Status: shipped on 2026-10-15 as 1.12.0; live on 2026-10-15: approval blocks on real infrastructure pull requests, a tool-description change refused by CI, the exit dry run, the restore drill; the review by a second person waits for one. Planned on 2026-10-14 (research items 4, 9 and 10, and the trust gaps in the review's section 4.4).

| Item | Change | Where |
|---|---|---|
| 12.1 | Code-rendered approvals: the infrastructure pull-request comment and the `create_deployment_plan` output carry a fixed block rendered from the plan, never from Claude: resources added, changed and destroyed by type, the monthly floor before and after, any exposure change, a reversibility tier (reversible, data-bearing, identity), and the chain of identifiers (onboarding, plan, run, pull request) | labdeploy/infra.py, labdeploy/controller.py, labdeploy/cost.py, .github/workflows/infra-plan.yml |
| 12.2 | Requester and approver (D29): `auto_merge` per application and per principal; CODEOWNERS on `infra/gcp/apps/**` with required review, documented for the day a second operator exists | labdeploy/config.py, labdeploy/onboarding.py, .github/CODEOWNERS, docs/SETUP.md |
| 12.3 | Tool descriptions pinned: a checksum of every tool's name, description and schema checked in CI, so a change is a deliberate commit; every tool failure returns a structured error with a plain next action (an audit of server.py) | tests/, labdeploy/server.py |
| 12.4 | The exit procedure: a runbook section "Leaving Google Cloud" and `scripts/export-app.sh` (pull the live image by digest, `terraform state pull`, copy each bucket, export Postgres, export Firestore) with a dry run on the demo recorded in VALIDATION; `tofu validate` next to `terraform validate` in CI so OpenTofu stays a drop-in | docs/RUNBOOK.md, scripts/, .github/workflows/ci.yml |
| 12.5 | Succession: a second principal named by the operator, the runbook rehearsed by that person, a quarterly restore drill the watchdog reminds about, the handoff refreshed | config.local.yaml, docs/RUNBOOK.md, labdeploy/scheduler.py, PROJECT_HANDOFF.md |

Live exercise: an infrastructure pull request whose comment shows the rendered block for a bucket added and a database retired; a tool-description change caught by CI; the exit dry run on the demo; the restore drill from the backup bucket by the second person.
Requires regeneration of app workflows: no. Terraform apply: no.
Cost effect: none.

## Decisions the operator makes before Phases 13 to 17

Planned on 2026-10-16 from the research review `docs/RESEARCH-2026-10-16.md` (its section 6 ranks the items). The order
is the roadmap's rule: value per effort, cost first. Phase 13 answers "a repository as it is"; Phase 14 makes an
unreviewed repository safe to build and run; Phase 15 takes the controller off the laptop; Phase 16 adds a production
target; Phase 17 is the tier for protected health information, which needs decisions outside the code. What stays human
whatever the decisions: the review of an imported contract, secret values, the foundation applies, the production
approvals, the business associate agreement, and the organization steps of Phase 17.

| # | Decision | Needed by | Default if nothing is said |
|---|---|---|---|
| D31 | Where the contract of a repository taken as it is lives | Phase 13 | `apps/<id>/platform.yaml` in the platform repository with a `source` block (`repository`, `commit`); one authoritative contract per app, recorded in the registry as `contract: platform` or `contract: repository`; a `platform.yaml` found in the repository is imported once as the draft of the external one; repositories that follow the contract keep today's in-repository mode |
| D32 | What "reviewed" means for an external contract | Phase 13 | The pull request's approval on the platform repository under CODEOWNERS (`apps/**`); `reviewed: true` is written by the merge, never by the controller; a release is a pull request that moves `source.commit` and nothing else, opened by the controller and merged by a person, or merged by the controller when the application's `auto_merge` allows it and the policy passes |
| D33 | Where the pipeline of a repository taken as it is runs | Phase 13 | `deploy.yml`, `recover.yml` and `rightsize.yml` in the platform repository, started by `workflow_dispatch` with the application and commit; the application repository is checked out read-only at that commit with a GitHub App installation token narrowed to that repository; the Workload Identity condition names the platform repository's id, `refs/heads/main` and the environment `staging-<app>`; the run signs a custom attestation (repository, commit, contract commit, Dockerfile and lock hashes) with the application's Cloud KMS key and verifies it before deploying; the platform repository is named in the runbook as the lab's single control point |
| D34 | What the reader refuses rather than guesses | Phase 13 | Several frameworks or several candidate application objects; Django without `WSGI_APPLICATION`; no dependency manifest at all; conda-only or system packages; a Python version outside 3.11 to 3.15; a committed secret value; a notebook or a file over 100 MiB as the application; every refusal names the file and the fix |
| D35 | Trust levels and their defaults | Phase 14 | `reviewed` for a repository the lab owns and has read; `unknown` for one taken as it is: the vulnerability gate blocks, egress is denied unless the contract opts in, the second-generation environment is used, secret scanning fails the build on a verified hit, no build cache; `project: own` moves an application into a project of its own (unknown or data-bearing) |
| D36 | Where the controller runs | Phase 15 | One Always Free `e2-micro` in us-central1 with a 30 GB standard disk, Caddy with Let's Encrypt on a DNS name the lab owns (D6), a systemd unit, the observer service account attached, the GitHub App's private key in Secret Manager; Litestream replication of the database to the backup bucket on by default; Cloud Run with a scheduler job as the fallback if the free VM is withdrawn |
| D37 | The production project and who approves a promotion | Phase 16 | A second project (`prod`) in the same billing account with its own foundation, state and per-app identities, applied only from the `production` environment; a promotion deploys the digest staging released, never a rebuild; the approval is a required reviewer on the platform repository's `production-<app>` environment (public repository, so free) with self-review prevented; where that is unavailable, the code-rendered approval block and a ruleset requiring the staging deployment to have succeeded |
| D38 | The production release | Phase 16 | Deploy with no traffic and a tag, probe the tag, then 10, 50 and 100 percent, each step held for the bake's rules (D22); a crash, errors above five percent or latency above three times the baseline restores the previous split; a change of configuration or of a secret's version goes through the same path; the bake's "inconclusive on no traffic" still passes with a note |
| D39 | Production reliability and its paid options | Phase 16 | One SLO per application (99.5 percent of health and request successes over 30 days) with Cloud Monitoring's fast and slow burn-rate alerts and a 60-second uptime check from three regions; Postgres in production is a single-zone `db-f1-micro` with automated backups, point-in-time recovery and deletion protection, declared in the contract as "no SLA"; high availability and the SLA-covered shape are per-app opt-ins with their floor in the approval block; a custom domain is a per-app opt-in through a global load balancer, with Cloud Armor, both with their floor shown; a warm instance is a per-app opt-in |
| D40 | The tier for protected health information | Phase 17 | See Phase 17: the organization, the agreement and the controls it lists, each marked as the operator's, the institution's or the platform's |

## Phase 13: a repository as it is (about six days) → version 1.15.0

Status: planned on 2026-10-16 (research review sections 4.1 to 4.4 and 4.6), built the same day as version 1.15.0 with
the defaults of D31 to D34 (`docs/CHANGES.md`) and exercised live the same evening (`docs/VALIDATION.md`): two
repositories released as they are by the platform's own pipeline, a contract change released by its merge, refusals on
four real repositories. Still to see live: a release pull request moving `source.commit`, a `uv.lock`, a Compose file and
the GitHub App token. Nothing is added to an application repository; the contract, the build inputs and the pipeline
move to the platform.

| Item | Change | Where |
|---|---|---|
| 13.1 | The external contract (D31, D32): `apps/<id>/platform.yaml` with `source`, validated by the same model; the registry records which contract is authoritative; `import_contract` reads an in-repository `platform.yaml`, a Compose file or the reader's draft into the external file as a pull request; `create_deployment_plan` binds the external contract's commit and the source commit, and the approval block shows both | labdeploy/config.py, labdeploy/controller.py, labdeploy/onboarding.py, labdeploy/infra.py, apps/ |
| 13.2 | The central pipeline (D33): the three workflows in the platform repository taking `application` and `commit`; checkout of the application at the commit with a GitHub App installation token (contents read, one hour, revoked); the Workload Identity condition per environment `staging-<app>`; the custom attestation signed with the application's key and verified before `gcloud run deploy`; the release's identifier chain carries the platform run and the application commit; one GitHub environment per application in the platform repository | .github/workflows/deploy.yml, recover.yml, rightsize.yml, infra/gcp/modules/service/main.tf, labdeploy/github.py, labdeploy/provenance.py |
| 13.3 | Build inputs from outside: the platform's Dockerfile applied with `docker build -f-` or `--build-context` and its own `.dockerignore`; the lock compiled by the platform into `apps/<id>/requirements.lock` and installed with `--require-hashes`; the buildpack path driven by `GOOGLE_ENTRYPOINT`, `GOOGLE_PYTHON_VERSION` and a platform `project.toml` with `pack --descriptor`; the builder pinned by digest | templates/, .github/workflows/deploy.yml, labdeploy/scaffold.py |
| 13.4 | Run-time defaults: the contract's `command` and `args` override the image's entrypoint; `port` sets `PORT`; a service without a health route starts under the TCP startup probe and the release says the HTTP probe is off; the migration job's command fixed from the contract | infra/gcp/modules/service/main.tf, labdeploy/release.py, labdeploy/models.py |
| 13.5 | The reader (D34): the precedence ladders of the review's section 4.4 for the Python version, the lock, the entrypoint, the port, the health route, configuration names, needs and tests; one service per Compose build or per folder with a manifest and an entry file; every refusal names the file and the fix; `check_repository` reports the ladder's choices and the files it ignored | labdeploy/analyzer.py, labdeploy/scaffold.py, labdeploy/onboarding.py |
| 13.6 | Documents: the contract page opens with "what the platform assumes when you give it nothing"; CLAUDE-CHAT's "Deploy it as it is"; the runbook names the platform repository as the single control point and the rule that its protection never loosens; the handoff | docs/DEVELOPER-CONTRACT.md, docs/CLAUDE-CHAT.md, docs/RUNBOOK.md, PROJECT_HANDOFF.md, skills/ |

Live exercise: a lab repository with a Dockerfile and nothing else deployed with no file added; the bare repository with
its Dockerfile removed built through the buildpacks from an external entrypoint; a repository with `pyproject.toml` and
`uv.lock` locked by the platform; a two-service repository read from its Compose file; one repository refused with the
list; the custom attestation verified and shown in the release; the Workload Identity condition proved by a plan run and
an apply run from the platform repository.
Requires regeneration of app workflows: no (the central workflows are new; repositories that follow the contract keep
theirs). Terraform apply: by the pipeline (the trust condition per service).
Cost effect: none at idle; the central runs bill the platform repository, which is public.

## Phase 14: safety for code nobody has reviewed (about four days) → version 1.16.0

Status: planned on 2026-10-16 (research review section 4.5, controls 1 to 10); 14.1 to 14.7 built the same day as
version 1.16.0 with the default of D35 (`docs/CHANGES.md`). Open: 14.8 (Binary Authorization reads only its own
attestations under one project-wide rule on Cloud Run; settle the attestor in a dry run), 14.9 (built with Phase 16's
second-project machinery) and 14.10 (needs credentials in a step that never runs application code).

| Item | Change | Where |
|---|---|---|
| 14.1 | The Dockerfile policy gate before any build: refuse `RUN --security`, `--network=host`, `ADD` from a URL without `--checksum`, piping a download into a shell, disabled TLS or signature checks, `sudo`, and `COPY` of `.git`, `.env` or key files; require an explicit non-root `USER`; the build never passes `--allow`; Trivy's configuration scanner beside hadolint, pinned by digest | templates/deploy-gcp.yml, .github/workflows/deploy.yml, labdeploy/analyzer.py |
| 14.2 | Secret scanning: gitleaks on the checkout and TruffleHog on the built image, failing on a verified hit; a deny-list `.dockerignore` synthesised when the repository has none; a cap on the build context | templates/, .github/workflows/deploy.yml |
| 14.3 | No build cache in release workflows (a test asserts it); `--network none` for the build when the contract says the dependencies are vendored | templates/, tests/ |
| 14.4 | The vulnerability gate in the industry's wording: fixable critical and high findings and any malicious-package finding block, on by default for `unknown` (D35); images older than 30 days are re-scanned before a redeploy | labdeploy/scan.py, templates/deploy-gcp.yml |
| 14.5 | Egress default-deny for `unknown`: Direct VPC egress with an egress-deny firewall rule keyed on the service identity, Private Google Access for Google APIs, `egress: internet` as the contract's opt-in | infra/gcp/modules/service/main.tf, infra/gcp/modules/project/, labdeploy/models.py |
| 14.6 | The second-generation environment for `unknown` and, as the operator's switch, Cloud Run threat detection through Security Command Center Premium at project level (the Billing Catalog prices it at $0.0071 per vCPU-hour) | infra/gcp/modules/service/main.tf, docs/SETUP.md |
| 14.7 | SBOM and provenance attestations attached to every image (`--sbom --provenance`) and recorded in the release | templates/deploy-gcp.yml, labdeploy/release.py |
| 14.8 | Binary Authorization on Cloud Run requiring the application's Cosign attestation, dry-run first, then enforced per project (no Cloud Run SKU exists in the catalog) | infra/gcp/modules/project/, infra/gcp/modules/service/, docs/SETUP.md |
| 14.9 | `project: own` (D35): a foundation and a root in a project of the application's own, for unknown or data-bearing applications; the registry carries the project; the cap and the drift check cover it | labdeploy/config.py, labdeploy/onboarding.py, infra/gcp/, scripts/apply-foundation.sh |
| 14.10 | A Sensitive Data Protection inspection of data files over a size threshold before they can enter an image, reported in the plan (first GiB a month free) | labdeploy/analyzer.py, templates/deploy-gcp.yml |

Live exercise: planted violations (a Dockerfile piping a download into a shell, a committed `.env`, an unsigned image, an
outbound connection from an `unknown` app) each refused or blocked, recorded in VALIDATION; one unknown repository in a
project of its own; threat detection switched on for a day and its findings read.
Requires regeneration of app workflows: yes (the gates run in the build). Terraform apply: by the pipeline; the foundation
once for Binary Authorization.
Cost effect: none at idle; scans unchanged at $0.26 per fresh image.

## Phase 15: the controller off the laptop (about three days) → version 1.17.0

Status: built on 2026-10-16 as 1.17.0 with the defaults of D36, and one refinement: the machine carries its own identity, `lab-deploy-controller`, with the observer's viewer roles plus its own folder of the backup bucket and the App's key, so impersonating the observer from a laptop reaches neither. No DNS name is needed (the operator's choice on 2026-10-17, in place of D6's university subdomain or cheap domain): Claude reaches the machine at its own reserved address, which Let's Encrypt certifies with six-day certificates that Caddy renews; a name stays optional. Exercised live on 2026-10-18 (`docs/VALIDATION.md`): the machine has been the controller since then, Claude reaches it at 34.133.114.245, a power cycle took 50 seconds, a fresh machine restored itself from Litestream in about five minutes, and an onboarding ran through the GitHub App (1.17.1 to 1.17.3 fixed what that showed). It completes roadmap items 5.1 and 7.3.

| Item | Change | Where |
|---|---|---|
| 15.1 | GitHub App authentication (D14, 7.3): installation tokens per repository with the permissions onboarding needs; the private key read from Secret Manager through the attached identity; the fine-grained token stays the fallback on a laptop | labdeploy/github.py, scripts/start.sh, docs/SETUP.md |
| 15.2 | The machine (D36): Terraform for the `e2-micro`, its standard disk, the attached observer identity, a firewall that opens 80 and 443 and keeps SSH behind IAP; a startup script that installs Caddy, the controller and two systemd units with `Restart=always`; configuration, database and backups on the disk; `scripts/install-machine.sh` for the one-time steps | infra/gcp/modules/project/controller.tf, scripts/, docs/SETUP.md |
| 15.3 | Durability: Litestream replication of the SQLite file to the backup bucket beside the nightly copy; the restore drill extended to restore from it; `VACUUM INTO` for the nightly copy | labdeploy/drill.py, scripts/, docs/RUNBOOK.md |
| 15.4 | Reachability: a Cloud Monitoring uptime check on `/healthz` every minute from three regions with an alert, beside the GitHub ping; the connector's name moved to the lab's DNS name; the tunnel retired | infra/gcp/modules/project/, docs/CLAUDE-CHAT.md |
| 15.5 | The move: export the laptop's database and configuration, import on the machine, switch DNS, re-register the connector; the runbook section "The controller's machine" (restart, update, restore, rotate) | docs/RUNBOOK.md, docs/SETUP.md, PROJECT_HANDOFF.md |

Live exercise: Claude chat through the new name; the machine power-cycled and the controller back within a minute; a
restore from Litestream on a fresh machine; a GitHub App token used for an onboarding.
Requires regeneration of app workflows: no. Terraform apply: the foundation once (the machine).
Cost effect: about $3.65 a month (the IPv4 address) to $4.04; $6.11 more if the free tier ends.

## Phase 16: a production target (about six days) → version 1.18.0

Status: planned on 2026-10-16 (research review sections 5.1, 5.2 and 5.5). Item 16.1 built on 2026-10-17 as 1.18.0
(`docs/CHANGES.md`), with two refinements. An application's production is registered by a step of its own,
`add_production`, in the controller's database (the same for applications of the file and onboarded ones), instead of an
`environments` list in the registry entry, so onboarding is unchanged. Production roots live in
`infra/gcp/apps-production/<service>` and run in pipeline jobs of their own. Production infrastructure is merged by a
person, never by the controller; the environment `infra-production` that applies it has the operator as required reviewer.
Item 16.2 built on 2026-10-17 as 1.18.1: `promote_to_production` and the platform repository's `promote.yml`. The image is
copied by digest with a carbon copy (`docker buildx imagetools create --prefer-index=false`) rather than a new tool, the
staging provenance is checked by the production deployer with read access to that one application's registry and the
public half of its key (a second pull request of `add_production`), and production's key signs a SLSA verification summary
that names the source and who approved it. A production rollback waits for 16.3; until then the runbook gives the command.
Exercised live on 2026-10-18 (`docs/VALIDATION.md`): the production project under the organization, its foundation and
pipeline, `add_production` for the demo through all four steps, and the demo promoted by digest with the operator's
approval, serving staging's exact image in production; 1.18.2 fixed what that showed. Item 16.3 built on 2026-10-18 as
1.18.3: the split release in the runner (`ROLLOUT_STEPS`, production only; staging still moves everyone at once, so no
application's workflows change), secrets pinned to their version at release in production, and `rollback_production`
as the rollback of `promote.yml`, which production's deployer already trusts, so no root changes either. Exercised live on
2026-10-18 (`docs/VALIDATION.md`, the rollback drill): a fault the probes cannot see failed its bake at 10 percent and
the earlier split came back; the fix passed every step on real traffic; the rollback by tool moved production back in 16
seconds; 1.18.4 fixed what that showed. Item 16.4 built on 2026-10-18 as 1.18.5: production's objective counts Cloud Run's
own requests (a request-based objective on a custom service, so nothing depends on Service Monitoring finding the
service), with Google's starting points for its two alerts (ten times over an hour, twice over a day); staging gets none.
Item 16.5 built on 2026-10-19 as 1.18.7: production's databases recoverable to any moment of the week (Postgres and
Firestore), `needs.database_tier` (standard, ha, sla) with its floor in every promotion, the migration's ten-second lock
timeout, and `scripts/data-drill.sh`. Not done: squawk, since the platform has no SQL-file convention for migrations to
lint (an application runs its own command, such as `alembic upgrade head`), and with it a check that a release never
contracts a schema; the developer contract asks for add-only migrations instead. The live drill waits for an application
with a database in production. Item 16.7 built on 2026-10-19 as 1.18.8: a release tag per promotion in the application's
repository, production's spend beside staging's in the weekly cost report (the billing account's one export holds both,
so no access changes), `scripts/cap-drill.sh` along the path a real notification takes, and the incident procedure in the
runbook. Its first drill on production found that the platform's own cap could not close a service (Cloud Run asks for
`actAs` on the runtime identity); with that fixed in 1.18.8 the drill passed, production closed for about six seconds. The drift check, which has covered production's roots since 16.1, planned the demo's clean on its first run with
a production. Item 16.6 built on 2026-10-19 as 1.18.9: `domain:` in the contract (production only) puts a global load
balancer with a Google-managed certificate and a Cloud Armor rate limit in front of the service, with its floor ($25.25 a
month, Billing Catalog 2026-10-19) in every promotion and a warning that it exceeds a $20 budget by itself; IAP stays on
the service, which Google documents as enforced on every way in. Not done: ingress restricted to the balancer. Every
release probes the candidate at its `run.app` tag address, which restricted ingress would refuse, so the `run.app`
address keeps answering behind the same sign-in (no application is public); the domain adds a name and a rate limit, not a
new way in. The live trial waits for a name the lab controls.

| Item | Change | Where |
|---|---|---|
| 16.1 | The production project (D37): a second foundation, state and identities; each application's registry entry gains `environments` (`staging`, `production`) with its own target; the production root rendered from the same contract with production's defaults; the `production` GitHub environment with its Workload Identity condition | infra/gcp/, labdeploy/config.py, labdeploy/infra.py, labdeploy/onboarding.py |
| 16.2 | Promotion (`promote_to_production`): a plan bound to the digest of a staging release that succeeded, approved through the platform repository's `production-<app>` environment (required reviewer, no self-review), released by the same runner; the approval block shows the digest, both environments' floors and who approved | labdeploy/controller.py, labdeploy/server.py, .github/workflows/, schema/tools.sha256.json |
| 16.3 | The split release (D38): tag, probe, 10, 50 and 100 percent with the bake's rules at each step and restore of the previous split; changes of configuration and of secret versions through the same path; a rollback drill | labdeploy/release.py, labdeploy/bake.py, templates/, docs/RUNBOOK.md |
| 16.4 | Reliability (D39): an SLO per application with fast and slow burn-rate alerts and the 60-second uptime check; a production readiness checklist rendered by code at the first promotion (probes, alerts, runbook entry, backup, capacity, rollback path); `get_service_health` shows the error budget | infra/gcp/modules/service/, labdeploy/cloud.py, labdeploy/chat.py |
| 16.5 | Production data: Postgres defaults (backups, point-in-time recovery, deletion protection; the contract says "no SLA"); `database_ha: true` and the SLA shape as opt-ins with their floors; migrations checked by squawk with a lock timeout; a release never contracts a schema; the restore drill covers production | infra/gcp/modules/service/needs.tf, labdeploy/cost.py, templates/, docs/DEVELOPER-CONTRACT.md |
| 16.6 | Reach: `domain:` as a per-app opt-in through a global load balancer with a Google-managed certificate and Cloud Armor, ingress restricted to the balancer, the floor in the approval block; IAP stays the default for people | infra/gcp/modules/service/domain.tf, labdeploy/cost.py, labdeploy/models.py |
| 16.7 | Operations: release tags per promotion; an incident procedure for one operator (detect, mitigate first, communicate, review); a cap-fire drill for production; the weekly cost report and the drift check cover both projects | docs/RUNBOOK.md, labdeploy/scheduler.py, .github/workflows/infra-drift.yml |

Live exercise: the demo promoted to production by digest with an approval; a planted failure restored at 10 percent; the
SLO alert fired by a test; a configuration change released through the split; one application given a domain and the
floor seen in its approval block; the rollback and cap-fire drills recorded.
Requires regeneration of app workflows: yes (the split release). Terraform apply: the production foundation once by the
operator, then by the pipeline.
Cost effect: about $0.06 a month at idle for the project; about $10 a month per production Postgres; a domain with Cloud
Armor about $25 a month per application and an SLA database about $100 a month, only where chosen.

## Phase 17: the tier for protected health information (about four days, plus the institution's steps) → version 1.19.0

Status: planned on 2026-10-16 (research review section 5.4). Every service the platform uses is on Google's BAA list
(2025-09-17); what the tier adds is mostly organisational, with cheap technical controls behind it. It is planned now so
that nothing built in Phases 15 and 16 has to be undone: the production project is created under the organization from
the start, the audit logs and their locked copy exist from day one, and the logs tool is gated for data-bearing apps.

| Item | Change | Where |
|---|---|---|
| 17.1 | The organization: the production project created under `mail-mboti-org` (created 2026-07-27, so the seven baseline constraints apply on their own), an Assured Workloads folder with the free "US Data Boundary for Healthcare and Life Sciences" package, Security Command Center Standard at organization level, Access Transparency and Access Approval; the organization policies of the review's section 5.4 as Terraform (`sql.restrictPublicIp`, `storage.publicAccessPrevention`, `run.allowedIngress`, `run.managed.requireInvokerIam`, `run.allowedBinaryAuthorizationPolicies`, `gcp.resourceLocations`, `iam.workloadIdentityPoolProviders`) | infra/gcp/org/, docs/SETUP.md |
| 17.2 | The agreement and the status: the BAA accepted by someone authorised to bind the institution, the lab's status (covered entity, hybrid entity or business associate) recorded; `phi: true` in an application's contract turns the tier on, and the policy refuses for such an application any resource or feature outside Google's covered-product list and anything pre-GA | labdeploy/infra.py, labdeploy/models.py, docs/DEVELOPER-CONTRACT.md, docs/RUNBOOK.md |
| 17.3 | Logs and keys: Data Access audit logs on Cloud SQL, Firestore, Cloud Storage and Secret Manager, routed to the locked bucket; customer-managed keys with automatic rotation on the data stores, the registry and the Cloud Run service of a `phi: true` application | infra/gcp/modules/project/, infra/gcp/modules/service/ |
| 17.4 | The perimeter: VPC Service Controls around the production project, dry run first, with the access level for the pipeline's federated identity, then enforced; the egress deny of 14.5 is the default for `phi: true` | infra/gcp/org/, docs/SETUP.md |
| 17.5 | PHI out of the control plane: `get_recent_logs` refused for a `phi: true` application until its lines pass through Sensitive Data Protection's de-identification; a test that plans, receipts, summaries and the audit trail carry no application data; no third party between the controller and Claude (Phase 15) | labdeploy/controller.py, labdeploy/cloud.py, labdeploy/chat.py, tests/ |
| 17.6 | The duties, written down: the 60-day breach-notification clock and who is told; the asset inventory and network map printed from the registry and the Terraform state; the 72-hour restore proved by the drills; the controls reviewed every six months; the handoff | docs/RUNBOOK.md, labdeploy/cli.py, PROJECT_HANDOFF.md |

Live exercise: a `phi: true` demo with no real data in the production project under the organization; the perimeter in
dry run, then enforced, with the pipeline still applying; a Data Access log entry found in the locked bucket; a logs call
refused; a planted resource outside the covered list refused by the policy; the inventory printed.
Requires regeneration of app workflows: no. Terraform apply: the organization and the production foundation once, by
the operator.
Cost effect: about $1 to $5 a month (key versions and Data Access logs); the Assured Workloads package, VPC Service
Controls, Access Transparency and Security Command Center Standard have no charge.

## Decisions the operator makes before Phases 18 and 19

Planned on 2026-10-19 from the research review `docs/RESEARCH-2026-10-19.md` (its section 6 ranks the items), asked for
by the first non-Python application the lab wants deployed: the BetterCare chatbot ("Sophia"), a Node.js service the
reader refused that morning with "no Python code or dependency manifest". Phase 18 adds Node.js web services to the
contract, the reader, the pipeline and the platform's image, with npm's 2026 defaults built in; Phase 19 adds what a
data-bearing application needs whatever its language: a read-only bucket mount and a Google-hosted path to Claude (a
replicated SQLite file was planned too and dropped before it was built; D47 says why). What stays human: the review of the contract, the script allow-list, the Model Garden click
per model, the application's own changes, and the decision to open an application beyond the lab's members.

| # | Decision | Needed by | Default if nothing is said |
|---|---|---|---|
| D41 | The runtime and its version | Phase 18 | `runtime: node-http` beside `python-http`; `node` is one of `"22"`, `"24"`, `"26"` (the Active LTS lines; 20 and the odd lines refused); the default is the newest Active LTS on the day the contract is drafted (24 until 2026-10-28, 26 after); 22 is refused from 2027-04-30 and 24 from 2028-04-30, both dates in the runbook; the version ladder: the Dockerfile's `FROM node:X`, `.nvmrc`, `.node-version`, `.tool-versions`, `engines.node` (a range resolves to the highest supported major that satisfies it; no upper bound is noted), `devEngines.runtime` |
| D42 | Packages and the lockfile | Phase 18 | npm only in this phase: `package-lock.json` (version 2 or 3) or `npm-shrinkwrap.json`, installed with `npm ci`; every entry must carry `integrity` and a registry `resolved` (git URLs, tarballs outside the registry, `file:` and `link:` paths and `bundleDependencies` are refused); no lockfile: the platform locks it in a container of the service's Node (D27) with `npm install --package-lock-only --ignore-scripts --min-release-age=3` into `apps/<service>/package-lock.json`, run by npm 11.10 or later whatever the service's Node (Node 22 bundles npm 10.9, which has no minimum release age); a `yarn.lock`, `pnpm-lock.yaml` or `bun.lock` is refused with the fix until the later item |
| D43 | Install scripts | Phase 18 | Never at install: `npm ci --omit=dev --ignore-scripts` on every npm (no Node line bundles npm 12 yet, and 11.x only warns); afterwards the platform runs `npm rebuild <name@version>` for the packages the contract lists under `build.scripts`, and nothing else; the reader drafts that list from the lockfile's `hasInstallScript` entries, keeping only those the repository's own `allowScripts` pins by version, and says why it may stay empty (a native binary shipped in the tarball needs no script); name-only or `--all` approvals are refused; the root package's own `preinstall`, `postinstall` and `prepare` never run |
| D44 | The dependency audit and its gate | Phase 18 | OSV-Scanner on the lockfile before the build, pinned by version and SHA-256, in the wording of the Trivy gate (14.4): a `MAL-` record, a fixable critical or high finding, or a scan that did not finish blocks for every trust level; the rest is listed in the summary; `npm audit signatures` is a note; `npm audit` is not the gate (it needs the registry, and whether it returns malware advisories is undocumented); the contract page recommends Dependabot's default three-day cooldown and names npm's `min-release-age` |
| D45 | The image | Phase 18 | The platform's Node Dockerfile: `node:<major>-slim` (Debian) by multi-architecture index digest, resolved on a recorded date like the Python pins; `NODE_ENV=production`, `HOME=/tmp`, dependencies before the code, `npm cache clean --force`, the deny-list with `node_modules` added, `USER 65532:65532`, `EXPOSE`, an exec-form `CMD ["node", ...]`, never `npm start`; the memory knob stays `resources.memory_mib` (Node sizes its heap from the cgroup limit; a note on the contract page); Google's buildpacks (`GOOGLE_NODEJS_VERSION`, `GOOGLE_ENTRYPOINT`, the `google-24` builder) for `trust: reviewed` only, because the builder runs install scripts and fetches the runtime at build time; the Dockerfile policy adds two notes (a `CMD` through npm, an Alpine base with native addons) |
| D46 | What the reader refuses for Node, beyond D34 | Phase 18 | A start script that watches, transpiles or chains (`nodemon`, `ts-node`, `tsx`, `node --watch`, a development server, `concurrently`, a shell); a project that needs a build (`scripts.build`, TypeScript on a Node below 24.12, Next, Nest, Remix, SvelteKit, Astro or Nuxt) until the later item; a listen call on a literal port, on `127.0.0.1` or `localhost`, or a Fastify server without `host: '0.0.0.0'`; a static site; two lockfiles, or `packageManager` and `engines.*` naming different managers; a workspace without a service path; a committed `.npmrc` or `.yarnrc.yml` with a literal token; `engines.iojs`; a committed `node_modules/`, `dist/` or `.next/` is ignored with a note; tests are `scripts.test` unless it is npm's placeholder, else `node --test`'s patterns or a Jest, Vitest, Mocha, AVA or tap configuration, else "none", said plainly |
| D47 | What a data-bearing application gets, whatever its language | Phase 19 | As built on 2026-10-19: `needs.mounts` maps a declared bucket to a path, mounted read only through Cloud Storage FUSE by every release (the runtime identity already holds `roles/storage.objectUser` on its buckets); `needs.models: [claude]` grants the runtime identity the foundation's one-permission role `labDeployModelUser` (`aiplatform.endpoints.predict`, not Vertex AI User and its 459 permissions) and sets `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION=global`, so an egress-denied service reaches Claude through Private Google Access on the global endpoint, with the Model Garden click named as the operator's step and the per-token price in the review; no NAT, no Secure Web Proxy, no external identities: an application whose users are not Google accounts stays with the lab's members behind IAP. **Dropped before it was built:** `state: sqlite` (a Litestream sidecar replicating a SQLite file). Every release runs the old and the new revision at once (the candidate's probes, the bake, production's split), each with its own copy of the file and its own Litestream writing the same replica, so one side's writes would be lost on every release, not rarely as the review said; `max_instances` is per revision. A small database goes in Firestore or Postgres; Sophia's moves to Firestore (decided with Brian on 2026-10-19) |

## Phase 18: Node.js web services (about six days) → version 1.20.0

Status: built on 2026-10-19 as 1.20.0 and exercised live on 2026-10-19 and 2026-10-20 (`docs/VALIDATION.md`): every
item of the list below passed, after seven findings fixed in 1.20.1, 1.20.2 and 1.20.7 to 1.20.11 (the Node.js 22 lock,
GitHub's malware advisories, the docker command on the machine, a repository with services in folders, the platform's
Node.js image on Debian 13 without a package manager, a false secret from the image's Yarn, and the reuse of a kept
contract's image). Planned the same day from research review sections 4.1 to 4.6; 1.19.0 stays Phase 17's number. The platform is Python-only at three points: the contract's `runtime` and `python` fields, the
reader's first refusal, and the pipeline's test and audit steps. Each has a Node equivalent that is released, pinned and
documented, and two of them (OSV-Scanner for the audit, install scripts off by default) are stronger than the Python
path's. Nothing built in Phases 13 to 16 changes: the secret scan, the Dockerfile policy, hadolint, Trivy, Artifact
Analysis, the SBOM, provenance, egress, the cap, the bake and the split release already handle a Node image.

| Item | Change | Where |
|---|---|---|
| 18.1 | The contract (D41, D43): `runtime: node-http`, `node` with the three Active LTS lines, exactly one of `python` and `node` per runtime; `build.scripts`, a list of `name@version`; the policy step's `read_spec` outputs the runtime and its version; the chat answers name the runtime | labdeploy/models.py, labdeploy/contract.py, templates/deploy-gcp.yml, labdeploy/chat.py |
| 18.2 | The reader (D41, D42, D46): the Node ladders of the review's section 4.2 for the version, the lockfile, the start command, the port and host, the health route, the settings, the install scripts and the tests; every refusal with the file and the fix; the first refusal becomes "no Python or Node.js code or manifest"; `check_repository` reports the choices and the ignored files as for Python | labdeploy/reader.py, labdeploy/analyzer.py, labdeploy/scaffold.py, tests/ |
| 18.3 | The lock the platform makes (D42, D27): `npm install --package-lock-only --ignore-scripts --min-release-age=3` in a `node:<major>-slim` container on the controller's machine, with npm 11.10 or later installed by version where the image's npm is older (Node 22), into `apps/<service>/package-lock.json`; every entry checked for `integrity` and a registry `resolved` before it is proposed | labdeploy/scaffold.py, labdeploy/contract.py |
| 18.4 | The pipeline (D43, D44): `actions/setup-node` pinned by commit with `package-manager-cache: false`; `npm ci --ignore-scripts` into the checkout, then `npm rebuild` for `build.scripts`, then `npm test` (or `node --test --test-reporter junit` when the placeholder script says there are none and files match the default patterns); OSV-Scanner 2.7.0 on the lockfile, downloaded by version and checked against its SHA-256, with the gate's wording and its JSON in the bundle; the Python steps run only for `python-http` | templates/deploy-gcp.yml, .github/workflows/deploy.yml, labdeploy/scan.py, labdeploy/release.py |
| 18.5 | The image (D45): `templates/repo/Dockerfile.node` rendered by `render-build` with the base image's index digest, `NODE_ENV`, the install line, the script list and the exec-form `CMD`; `node_modules` in the deny-list and the descriptor; the buildpack path with `GOOGLE_NODEJS_VERSION` for reviewed code; the Dockerfile policy's two Node notes | templates/repo/, labdeploy/scaffold.py, labdeploy/dockerpolicy.py, templates/deploy-gcp.yml |
| 18.6 | The drafter: for a repository that follows the standard, the Node files of its own (the Dockerfile above, `.dockerignore`, a `test/health.test.mjs` on `node --test`, `platform.yaml`, `ci.yml` with `setup-node`, Dependabot for `npm` with the cooldown written in) | templates/repo/, labdeploy/scaffold.py |
| 18.7 | Documents: the contract page's "what the platform assumes" table and refusal list for Node, the signal, memory and keep-alive notes, the cooldown advice and the conversion commands for pnpm, Yarn and Bun lockfiles; CLAUDE-CHAT's "Deploy it as it is" for a Node repository; the runbook's two end-of-life dates; the skill; the handoff | docs/DEVELOPER-CONTRACT.md, docs/CLAUDE-CHAT.md, docs/RUNBOOK.md, skills/, PROJECT_HANDOFF.md |

Live exercise: a bare Express repository with a lockfile and no Dockerfile read, built on the platform's Node image,
released and recovered as on Python; the same repository with a Dockerfile of its own and with a Procfile through the
buildpacks; a repository with `onnxruntime-node` in its lockfile built with scripts off and its binary loaded at run time;
a lockfile with a planted `MAL-` package stopped by the gate; a start script with `nodemon`, a `listen` on `localhost`
and a `pnpm-lock.yaml` each refused with the fix; a repository without a lockfile locked by the platform; Sophia's
repository read and its contract drafted, with the list of what it still needs (Phase 19 and its own changes).
Requires regeneration of app workflows: yes (the templates change; Python applications see no difference). Terraform
apply: none.
Cost effect: none at idle; the audit adds under a minute to a release.

## Phase 19: what a data-bearing application needs (about two days, plus the trials) → version 1.21.0

Status: planned on 2026-10-19 (research review sections 5.1 to 5.8) and built the same day as 1.21.0, stacked on Phase
18 (code, tests and documents, checked locally; the live exercise below has not run). Sophia keeps a corpus and a model on
disk that can neither live in a public repository nor be fetched by a build that has no credentials, keeps its users'
state in SQLite, and calls Claude. Two of those needs have a platform answer that is the same for any language; the third
(the state) is the application's to move into Firestore, because `state: sqlite` was dropped before it was built (D47).
Checking before building also moved the mounts: the service module leaves the revision template to the release runner
(`ignore_changes = [template, ...]`), so a mount is a `gcloud run deploy` flag, not Terraform.

| Item | Change | Where |
|---|---|---|
| 19.1 | Bucket mounts (D47): `needs.mounts` maps a declared bucket to an absolute path that hides nothing the container needs; every release clears the mounts and volumes it finds and mounts what the contract names, read only, and a mount puts the service in the second-generation environment | labdeploy/models.py, labdeploy/release.py, docs/DEVELOPER-CONTRACT.md |
| 19.2 | Dropped: `state: sqlite` (D47 says why) | none |
| 19.3 | `needs.models: [claude]` (D47): the Vertex AI API and the one-permission role `labDeployModelUser` in the foundation; the grant to the runtime identity in the app's root, behind a precondition that names the foundation apply; `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION=global` on the service; the per-token prices in the cost view; the Model Garden step in the runbook | infra/gcp/modules/project/, infra/gcp/project/outputs.tf, infra/gcp/modules/service/needs.tf, infra/gcp/modules/service/variables.tf, labdeploy/infra.py, labdeploy/models.py, labdeploy/cost.py, docs/RUNBOOK.md |
| 19.4 | Streaming: `timeout_seconds` up to 3600 for a service that streams, with the bake's rules unchanged; the IAP streaming check in the live exercise | labdeploy/models.py, docs/DEVELOPER-CONTRACT.md |
| 19.5 | Documents and the skill: where files, a small database and a model API go on the contract page; the runbook's steps for an app that calls Claude | docs/DEVELOPER-CONTRACT.md, docs/RUNBOOK.md, skills/ |

Live exercise: the foundation applied in staging and Claude Opus 5.5 enabled in Model Garden; the demo application with a
bucket mounted and a file read from it, then the mount removed and gone with the next release; a Vertex call from an
egress-denied service on the global endpoint with the one-permission role, and the same call failing on the `us`
endpoint; an SSE stream read through IAP; the weekly cost report showing the Vertex spend; later, Sophia itself, after its
own changes (data in Firestore), released for the lab's members with the corpus, the model and the embeddings in a bucket.
Requires regeneration of app workflows: no. Terraform apply: the foundation once per project (the API and the role), by
the operator, before any root declares a model; then by the pipeline.
Cost effect: none at idle; a mounted bucket costs storage pennies; model calls are billed per token to the project and sit
inside its budget and cap.

## Decisions the operator makes before Phases 20 to 22

Planned on 2026-10-20 from the research review `docs/RESEARCH-2026-10-20.md` (its section 7 ranks the items), asked for
by the operator: the platform must deploy any Python or Node.js web service as it is, without a change to its code or
repository, and must not read as a thing customised for one application. The review measured the platform on 33 public
repositories (its section 4): the reader takes 3 as they are and refuses 30, two thirds of the refusals being the
platform's own choices rather than Cloud Run's needs; with these three phases the same sample reads as about 20 of 33,
and each of the 13 left needs one answer from a person (a folder, a command, a committed environment file to rotate, a
dead base image in the repository's own Dockerfile). What stays human whatever the decisions: the merge of the
contract as the review, secret values, the folder when a root holds several services, the command when the files do not
say it, and the rule that nothing is public.

| # | Decision | Needed by | Default if nothing is said |
|---|---|---|---|
| D48 | A version file (`.nvmrc`, `.python-version`, `engines`, `requires-python`) naming a line at end of life, for an image the platform builds | Phase 20 | The lowest supported line, with a note in the contract and the pull request; a Dockerfile's own `FROM` stays the developer's and is refused as today |
| D49 | A start the reader cannot rewrite into `node <file>`: a chain (`a && b`), `npm start` with no script, a shell | Phase 22 | Run as written under the init of the platform's image, with a note; `nodemon`, `node-dev`, `pm2` and `forever` are rewritten to `node <file>` (Phase 20); `concurrently` and `npm-run-all` stay refused |
| D50 | A Python service with no application object the reader knows | Phase 20 | The framework or server among the dependencies decides the command (Google's and Railpack's rule): `streamlit run`, `python main.py` with Gradio's variables, `uvicorn main:app`, `gunicorn main:app`, `python -m aiohttp.web`, with the entry file from the frameworks' own discovery lists; as the last rung `python <main file>` for the first of `main.py`, `app.py`, `server.py`, `run.py`, with `PORT` and `HOST` set and the note that the first revision's log decides; `build.entrypoint` overrides all of it |
| D51 | A service that binds `127.0.0.1` or `localhost` and reads no variable for it | Phase 22 | A proxy sidecar (Nginx, as Google documents it) on the ingress port forwarding to the app's port on loopback, drafted into the contract as `proxy: true` with its cost note; off for a service that binds every interface; the developer's one line stays the better fix and the contract says so |
| D52 | A Dockerfile of code nobody reviewed whose final image runs as root (9 of 33 in the survey, Google's own samples among them) | Phase 22 | The platform adds a wrapper stage (`FROM` the built image, `USER 65532`) when the contract says `build.user: 65532`, which the reader drafts for `trust: unknown`; a reviewer removes it to accept root; a service that writes to a root-owned path fails its first revision with `EACCES`, and the status names the fix |
| D53 | Install scripts for a Node.js service | Phase 21 | Still none unless listed; the reader drafts `build.scripts` from the platform's table of packages whose script is needed on Cloud Run, each at its locked version, and leaves out those shipping their binaries; the merge approves the list; name-only and `--all` stay refused |
| D54 | Tests of a repository taken as it is that cannot run on the platform (a browser, a database, a seed, Docker) | Phase 22 | `tests: report`: run, counted, never a stop; drafted when `scripts.test` or the pytest configuration names Playwright, Cypress, Selenium, `db:seed`, `docker`, `testcontainers` or a `DATABASE_URL`; `required` otherwise, as today |
| D55 | A front-end site with no server of its own | Phase 22 | Built with its `build` script and served from the platform's static image (Caddy, pinned by digest, no package manager) behind IAP with `exposure: lab-members`; `public` stays refused; a site whose build needs a secret is refused with the name |
| D56 | A repository root that holds several services | Phase 20 | `check_repository` names them (1.20.8); "deploy everything in REPO" (`onboard_application(url, all=true)`) opens one contract pull request per service, named after its folder; a root with one service in a folder takes that folder without asking |

## Phase 20: read more, refuse less (about four days) → version 1.22.0

Status: planned on 2026-10-20 (research review sections 4, 6.1, 6.4 and 7.1) and built the same day as 1.22.0 with the
defaults of D48, D50 and D56 (code, tests and documents, checked locally; the live exercise below has not run). The
survey of section 4 is now a test (`tests/test_survey.py`, 20.6): the same 33 repositories read as 8 accepted and 42
refusals, from 3 and 60 at 1.21.0; of the 25 still refused, 9 carry a Dockerfile that runs as root (Phase 22's wrapper
user; four of them also name a dead line in that Dockerfile or a real `.env`), 8 need the builds, managers or workspaces
of Phase 21, and 8 need one answer from a person (a real `.env`, a requirements file per hardware, a folder above
several services, a command the files do not say). The cheapest and largest change: the reader's bugs the survey found and the
breadth the source builders have, for both languages. Every item changes the reader and the drafts only; the pipeline,
the gates and the contract's shape are unchanged, and nothing is written into an application repository.

| Item | Change | Where |
|---|---|---|
| 20.1 | The reader's bugs: `.env.<name>.example` is an example, not a secret; a Django project is one with `manage.py` or `django` among its dependencies, never a `settings.py` that mentions the word; `-r` includes and a `requirements/` folder are followed; `main.py`, `app.py` and the frameworks' discovery names are ranked by name at any depth before shallower files; only the entry file and its imports decide the host; a notebook is a note, never the refusal; a root that holds several services names them in every refusal | labdeploy/reader.py, labdeploy/scaffold.py, tests/ |
| 20.2 | Python entries (D50): the dependency that names the framework decides the command and the environment for Streamlit, Gradio, Dash, Panel, Chainlit, Shiny, NiceGUI, Reflex, aiohttp, Sanic, Tornado and Django's ASGI, with the frameworks' own entry-file lists; `python <main file>` as the last rung; several candidates ranked by entry file as the frameworks rank them, refused only on a tie | labdeploy/reader.py, labdeploy/scaffold.py, docs/DEVELOPER-CONTRACT.md |
| 20.3 | Python manifests: `setup.py` and `setup.cfg` compiled with uv; a Poetry project or a Pipfile without a lock locked by the platform in the lock container as `uv.lock` is exported today; `pdm.lock` and `pylock.toml` read; `apt.txt`, `Aptfile` and `packages.txt` installed in the platform's Dockerfile; a build stage with a compiler when the lock holds a source distribution; Django's `collectstatic` at build and `migrate` offered as `needs.migrate` in the review | labdeploy/reader.py, labdeploy/scaffold.py, templates/repo/Dockerfile |
| 20.4 | Node.js: a literal port becomes `port: N` (`--port` sets the container port and `PORT` alike); `nodemon`, `node-dev`, `pm2` and `forever` rewritten to `node <file>`; `main` falls through `app.js`, `src/index.js`, `server/index.mjs` and the rest of the entry list; a stale lockfile locked again by the platform; the lockfile limit raised to 5 MB; `HOST` and `HOSTNAME` set to `0.0.0.0` for the frameworks that read them | labdeploy/reader.py, labdeploy/node.py, labdeploy/release.py |
| 20.5 | Dead lines in version files (D48) and everything in one go (D56): `onboard_application(url, all=true)` opens one contract per service a root holds | labdeploy/reader.py, labdeploy/onboarding.py, labdeploy/server.py |
| 20.6 | The survey as a test: the 33 repositories' trees and the files the reader reads, recorded as fixtures, with the expected contract or refusal for each, so a change to the reader shows its effect on the sample | tests/fixtures/survey/, tests/test_survey.py |
| 20.7 | Documents: the contract page's "what the platform assumes" tables for the new rungs; CLAUDE-CHAT's "Deploy it as it is"; the skill | docs/, skills/ |

Live exercise: a Streamlit app, a Gradio app and an aiohttp app started with `python main.py`, each deployed as it is from
a public repository; a Django project with `requirements/base.txt`, `collectstatic` and `migrate`; a Node.js service on a
literal port; a root with three services onboarded with one sentence.
Requires regeneration of app workflows: no. Terraform apply: no.
Cost effect: none; a wrong guess costs one failed build and its scan (about $0.26), which the gates make safe.

## Phase 21: Node.js builds and managers (about four days) → version 1.23.0

Status: planned on 2026-10-20 (research review sections 4, 6.3 and 7.2) and built the same day as 1.23.0, on 1.22.5
after the live exercise of Phase 20 (code, tests and documents, checked locally; the live exercise below has not run).
The survey (`tests/test_survey.py`) now reads 14 of the 33 repositories as they are and refuses 27 things, from 8 and 42
at 1.22.0: NestJS's starter, three Next.js applications on pnpm or npm, Dify's web package out of its pnpm workspace and
a Yarn 1 Express boilerplate joined; what still stops the Node.js sample is Phase 22's (the root user, Fastify's host, a
static site). `turbo prune` was left out: the whole workspace is installed and one package deployed out of it. The three
items the review of 2026-10-19 ranked 7 to 9, built first because the survey shows them in 8 of 15 Node.js
repositories. Nothing in Phases 13 to 16 changes: the audit, the scans, containment and the bake already handle whatever
the build stage produces.

| Item | Change | Where |
|---|---|---|
| 21.1 | pnpm, Yarn and Bun lockfiles: installed by their own manager (installed by version and checksum, not Corepack, which Node 25 and later no longer ship), frozen (`--frozen-lockfile`, `--immutable`, `bun ci`), scripts off (`allowBuilds`, `enableScripts: false`, `trustedDependencies: []`), every entry checked against the registry as npm's is; OSV-Scanner reads all three | labdeploy/node.py, labdeploy/reader.py, templates/deploy-gcp.yml, .github/workflows/deploy.yml, templates/repo/Dockerfile.node |
| 21.2 | A build stage: development dependencies installed, the `build` script (or the framework's command) run, the output copied into the runtime image without them; the frameworks' outputs and starts built in (Next.js standalone `server.js` with `HOSTNAME=0.0.0.0`, NestJS `dist/main.js`, React Router and Remix `build/server`, SvelteKit `build`, Nuxt `.output/server/index.mjs`, Astro `dist/server/entry.mjs`, AdonisJS `build/bin/server.js`); `NODE_ENV=production`; a build that needs a secret (Strapi, Payload) refused with the name | labdeploy/scaffold.py, labdeploy/node.py, templates/repo/Dockerfile.node, labdeploy/dockerpolicy.py |
| 21.3 | TypeScript: a `.ts` entry runs under type stripping when `tsconfig.json` says `erasableSyntaxOnly`, is built with `tsc` in the build stage otherwise; `tsx` and `ts-node` starts rewritten to the built file | labdeploy/node.py, labdeploy/scaffold.py |
| 21.4 | Workspaces: one service per package with the root lockfile (`npm --workspace`, `pnpm deploy --filter`, `yarn workspaces focus --production`, `turbo prune --docker` when `turbo.json` exists); `workspace:` dependencies resolved from the repository | labdeploy/reader.py, labdeploy/node.py, templates/ |
| 21.5 | Install scripts (D53): the platform's table of packages whose script is needed on Cloud Run, drafted into `build.scripts` at the locked version for the merge to approve | labdeploy/node.py, labdeploy/reader.py |
| 21.6 | Documents: the contract page's section 1b for the three managers, the build stage and workspaces; the roadmap's "later" list trimmed | docs/DEVELOPER-CONTRACT.md, docs/ROADMAP.md, skills/ |

Live exercise: a NestJS starter, a Next.js app on pnpm, a SvelteKit app in a pnpm workspace and an Express service on
Yarn, each deployed as it is; a package with a needed install script (`sharp` below 0.33) drafted, approved and loaded.
Requires regeneration of app workflows: yes (the templates change; Python applications see no difference). Terraform
apply: no.
Cost effect: none at idle; a build stage adds a minute or two of free runner time on a public repository.

## Phase 22: run it, then say what to fix (about three days) → version 1.24.0

Status: planned on 2026-10-20 (research review sections 5 and 7.3). What the source builders do with nothing: try the
convention, and when the first revision fails, say exactly why. The platform reads the candidate's own logs into the
release status already, so it can do the second half better than any of them.

| Item | Change | Where |
|---|---|---|
| 22.1 | A proxy sidecar (D51): Nginx, pinned by digest, as the ingress container forwarding to the app's port on loopback, drafted as `proxy: true` when the reader sees a loopback bind it cannot override by a variable; its allocation shown in the plan's cost note | labdeploy/release.py, labdeploy/models.py, labdeploy/cost.py, labdeploy/reader.py |
| 22.2 | An init in the platform's images (tini, by version and checksum), so a chained start script (D49) runs and still gets `SIGTERM`; the Dockerfile policy's note for shell-form commands names it | templates/repo/Dockerfile, templates/repo/Dockerfile.node, labdeploy/dockerpolicy.py |
| 22.3 | The root user (D52): a wrapper stage over a repository's own image when the contract says `build.user`, built with `--build-context` from the image just built; drafted for `trust: unknown`; the `EACCES` diagnosis in the status | templates/deploy-gcp.yml, .github/workflows/deploy.yml, labdeploy/scaffold.py, labdeploy/status.py |
| 22.4 | Static sites (D55): the platform's static image (Caddy) serving the `build` script's output behind IAP; `runtime: static` in the contract | labdeploy/models.py, labdeploy/reader.py, templates/repo/Dockerfile.static |
| 22.5 | `tests: report` (D54), and the status that says which suite was skipped as a stop | labdeploy/models.py, labdeploy/reader.py, templates/deploy-gcp.yml, labdeploy/status.py |
| 22.6 | The diagnosis: Google's documented failure messages ("failed to start and then listen on the port", "memory limit exceeded", the probe failures, gunicorn's worker timeout, `EACCES`) mapped to the one line the developer changes, and to the contract key the operator changes when it is the platform's to fix (`port`, `env.HOST`, `resources.memory_mib`, `proxy`, `build.user`); the chat answer carries it as the next action | labdeploy/status.py, labdeploy/chat.py, docs/CLAUDE-CHAT.md |

Live exercise: a Fastify service without a host, served through the sidecar; a service started by a chain under the init,
stopped cleanly on a release; Google's own Cloud Run Python sample deployed as it is with the wrapper user; a Vite site
behind IAP; a suite that needs Playwright reported, not stopping the release; a candidate that listens on the wrong port
diagnosed with the contract key to change.
Requires regeneration of app workflows: yes (22.3, 22.5). Terraform apply: no.
Cost effect: none at idle; the sidecar's memory and CPU are billed as the app's, only while requests are served, and the
exercise records the figure.

## Later, unscheduled (research item 13 of 2026-10-14; reviews of 2026-10-16, 2026-10-19 and 2026-10-20)

`cache: valkey` behind operator approval with its floor in every plan (two days plus a trial; Memorystore never scales to zero); `gpu: l4` with one instance, zonal redundancy off, weights on a read-only bucket volume and the cap able to stop it (three days plus a trial; instance-based billing); after Phase 21 (built 2026-10-20 as 1.23.0, which installs pnpm, Yarn and Bun lockfiles, builds and workspaces): `turbo prune --docker` for a pruned workspace install instead of the whole workspace (half a day); Node's permission model as an opt-in, `--permission-audit` first (half a day; it restricts no network before Node 25); a filtered internet egress through Cloud NAT and Cloud NGFW FQDN rules, if a backup model provider ever justifies about $5 a month before traffic and a test (one day); a container-only runtime for a third language (two days); Firebase Hosting for a public front-end only if the lab ever allows public exposure; Cloud Deploy pipelines with approvals and automated canaries, if the runner's split release of Phase 16 ever proves too little (the first pipeline is free, each further one $5 a month); SLSA Build Level 3 by isolating the signing job in the central pipeline; OpenTelemetry when an application needs traces beyond Cloud Run's own.

Not planned, because the evidence is against it for this lab: a second cloud or a cloud-agnostic layer; Score, Radius, the Open Application Model, Dapr, Crossplane or TOSCA; Compose as a deployment unit; multi-architecture image builds; worker pools or Memorystore by default; Kubernetes; model-written Terraform; autonomous remediation; forks or mirrors of application repositories; an LLM that sets up environments without a person's review; Cloud Run domain mapping or the preview custom URLs for production; statistical canary analysis; multi-region failover; Cloud SQL Enterprise Plus; Cloud Armor Enterprise; an always-on instance-based controller or application; chaos experiments in production; image debloating; Cloud Run sandboxes for the application itself; and, from the review of 2026-10-19: generating a lockfile silently at build time or installing without one; running install scripts, or honouring name-only script approvals; relying on the bundled npm's script policy (it only warns on every Node line today); Alpine images for an application with native addons; SQLite on a bucket mount; Secure Web Proxy or Cloud NAT for one backup provider; public exposure for Microsoft-account users; Google's buildpacks for unreviewed Node code; `npm audit` as the gate. SBOMs and Binary Authorization, deferred on 2026-10-14, are now Phase 14.

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
| 8 | about two and a half days | yes | by the pipeline |
| 9 | about three days | yes | yes (foundation, by the operator) |
| 10 | about four and a half days | yes | by the pipeline |
| 11 | about five days | yes | no |
| 12 | about three days | no | no |
| 13 | about six days | no (new central workflows) | by the pipeline |
| 14 | about four days | yes | by the pipeline; the foundation once |
| 15 | about three days | no | the foundation once (the machine) |
| 16 | about six days | yes | the production foundation once, then by the pipeline |
| 17 | about four days, plus the institution's steps | no | the organization and the production foundation once |
| 18 | about six days | yes | no |
| 19 | about two days, plus the trials | no | the foundation once per project, then by the pipeline |
| 20 | about four days | no | no |
| 21 | about four days | yes | no |
| 22 | about three days | yes | no |

About nine and a half days of work for Phases 0 to 5, plus the live exercises, which take an hour or two per phase. Phase 6 adds about eleven days and a trial per resource type. Phases 9 to 12 add about fifteen and a half days, planned on 2026-10-14 from `docs/RESEARCH-2026-10-14.md`. Phases 13 to 17 add about twenty-three days, planned on 2026-10-16 from `docs/RESEARCH-2026-10-16.md`; their idle cost is about $4 a month for the controller's machine and about $0.06 for the production project, everything else being a per-app or per-tier choice shown on the approval block. Phases 18 and 19 add about eight days, planned on 2026-10-19 from `docs/RESEARCH-2026-10-19.md`; they add no idle cost, and model calls through Vertex are billed per token inside the project's budget. Phases 20 to 22 add about eleven days, planned on 2026-10-20 from `docs/RESEARCH-2026-10-20.md`; they add no idle cost, and widen what the platform takes as it is from 3 to about 20 of the 33 repositories the review measured.
