# 1.5.0: the review's fixes

Every finding of the strict review of 2026-10-12 (`docs/REVIEW-2026-10-12.md`), in its order, with a regression test where code changed.

## Fixed

1. **Merging is now the gate** (finding 1): the apply identity's provider accepts only `infra-apply.yml` on `refs/heads/main` in the `infra` environment, the plan identity's provider only `infra-plan.yml` on a `pull_request` event, and each binds through an attribute the other provider never maps. `main` is protected (pull request, the `test` and `terraform` checks, administrators included, no force push), the environment forbids administrator bypass, and `.github/CODEOWNERS` names the owner.
2. **The plan policy fails closed** (findings 2, 15): public members refused everywhere, an allow-list of roles an application may grant (plus the platform's `labDeploy*` custom roles), the platform's identities and the state bucket matched in every string field, authoritative policies refused, the invariants (private database, delete protection, private bucket, labels, region including `location_id`) checked on update too, unknown values refused. The checker runs from a checkout of `main`, not from the pull request, and the comment carries the full plan. The renderer escapes `${` and `%{`, and `migrate` parts reject `,`, `$`, `^` and `"`.
3. **Tools no longer block the controller** (finding 3): every tool runs in a thread pool with the caller's identity carried along; a status wait no longer stalls sign-ins or other callers.
4. **A runner lost during a migration recovers** (finding 4): `migrating` is a recoverable checkpoint state (traffic untouched), the advice names it.
5. **One-word migration commands release** (finding 5): `--args=` resets the job's arguments; `--clear-args` never existed.
6. **The cap is robust** (finding 6): every call retries, each service and instance is handled in its own try block, failures are logged, counted and end the execution, an alert policy watches failed executions, the ratio comes from `costAmount` and `budgetAmount` (so any threshold works), a resource labelled `lab-deploy-cap-lifted=true` is left alone, and `ingress` joined the service's `ignore_changes` so a later apply does not reopen a capped service. The cap's per-service grant is a custom role (`labDeployCapOperator`: read and update a service) instead of `roles/run.developer`.
7. **Lost dispatches end** (finding 7): a plan still `dispatching` or `dispatch_unknown` two minutes later with no visible run becomes `dispatch_lost`, a final state with advice.
8. **Auto-deploy retries** (finding 8): a `planned`, errored or lost attempt is retried after thirty minutes instead of sticking forever.
9. **Firestore cost note corrected** (finding 9): no free quota for named databases, regional storage at 0.15 per GiB-month.
10. **One migration timeout** (finding 10): the job's task timeout is derived from the runner's command timeout; a timeout records the execution as still running and the release fails without a second migration.
11. **The ceiling is named** (finding 11): the policy text says it compares the compute bound plus the needs' floor; the report prices live settings as they are and warns when live `maxScale` exceeds the reviewed bound.
12. **SQLite in WAL mode with guarded writes** (finding 12): `finish` only completes a record in its claimed state; OAuth tables are pruned hourly.
13. **No client secrets** (finding 13): registered clients are public (`token_endpoint_auth_method: none`, PKCE); the access log no longer records query strings. Access tokens and authorization requests are bound to the resource (finding 26).
14. **Narrower pipeline identities** (finding 14): plan has specific viewer roles and security reviewer, no `roles/viewer`; apply has the admin roles a root needs and a custom bucket-manager role, no `roles/editor`, no `roles/storage.admin`; state access is conditioned to the `apps/` prefix (plus read of the foundation's outputs).
15. **Documents match the code** (finding 16): README, ARCHITECTURE, HANDOFF, SETUP and ROADMAP corrected; `LICENSE` (MIT); Dependabot watches the roots that exist; CI runs weekly and audits both lock files.
16. **Disaster recovery** (finding 17): `backup_bucket` sends the nightly copy and the configuration to the foundation's new private versioned bucket (`PROJECT-lab-deploy-backups`); the operator scripts live in `scripts/`; `docs/RUNBOOK.md` covers the laptop-off case, a rehearsed restore, the cap, stuck releases, credentials and a monthly alert exercise.
17. **Container behind a proxy** (finding 18): the Dockerfile documents `FORWARDED_ALLOW_IPS`.
18. Low findings 19 to 25: a re-run gets a fresh revision suffix (`a2`, `a3`); candidate log lines are marked untrusted; names must be 3 to 21 characters without a trailing dash; the scheduler is joined on shutdown; `var.subnet` is used by the migration job; the Pub/Sub API is managed once; the billing dataset is labelled; `install-launch-agent.sh` defines its variable; the foundation apply script shows the plan and asks; `start.sh` never puts the token on a command line; the reload scope is documented.

## Upgrade steps

1. Re-apply the foundation (`scripts/apply-foundation.sh PROJECT_ID`): two providers replace the old one, roles change, the cap's custom role, alert policy and the backup bucket appear.
2. On the platform repository replace `INFRA_WIF_PROVIDER` with `INFRA_PLAN_WIF_PROVIDER` and `INFRA_APPLY_WIF_PROVIDER` from the output; protect `main` and forbid bypass on the `infra` environment.
3. Re-render every application root (the cap's role input) and merge; runner code changed, so regenerate the app workflows against this commit and raise `platform.minimum_commit`.
4. Set `backup_bucket` in the controller configuration to the foundation's `backup_bucket` output and restart; the scripts in `~/.local/share/lab-deploy/` now call the ones in `scripts/`.

# 1.4.2: fix from the live plan

## Fixed

1. **The label policy refused every Cloud SQL instance**: it looked for a top-level `labels` map, and Cloud SQL keeps its labels under `settings.user_labels`. Found by the first plan of a database root, which the policy refused as designed.

# 1.4.1: fix from the live apply

## Fixed

1. **Cloud Workflows limits an expression to 400 characters**; the cap's log line had grown to 445 with the database count. The message is now built in parts, the test checks the limit, and the corrected workflow was deployed and deleted as a throwaway before this release. The foundation re-apply updates the workflow.

# 1.4.0: Cloud SQL, migrations and a cap that stops databases (Phase 6, items 6.5 and 6.6)

## Added

1. **`database: postgres`** under `needs`: the smallest Cloud SQL for PostgreSQL instance (shared core, zonal, 10 GB of HDD that grows to 20, daily backups kept a week), with a private address only and IAM sign-in only: no password exists anywhere. The runtime identity is the database user; the revision gets `DATABASE_INSTANCE` (the connection name), `DATABASE_NAME`, `DATABASE_USER` and `DATABASE_IP_TYPE`, and the service joins the foundation's private network for its egress. It bills every hour it exists, about 9 dollars a month: every plan and estimate says so, the warning names it, and the operator's cost ceiling now counts it.
2. **`migrate`** under `needs`: a command run as the `<service>-migrate` Cloud Run job from the release's own image before the candidate exists, so a schema change lands before any new code serves. A failed migration stops the release with traffic untouched; the receipt gains a `migrating` phase and the job's execution name. Keep migrations compatible with the previous revision (expand first, contract later), so a traffic rollback stays safe.
3. **The foundation's private network**: one VPC with a subnet for Cloud Run egress and Private Service Access for Cloud SQL, free while idle; the Compute, Service Networking and Cloud SQL Admin APIs.
4. **The spending cap stops databases**: at the budget line the cap workflow also sets every managed Cloud SQL instance's activation policy to never, after closing ingress. Restart one with `gcloud sql instances patch NAME --activation-policy=ALWAYS`.
5. Policies: a database instance is created with deletion protection; the plan refuses a public address.

## Upgrade steps

1. Re-apply the foundation (network, subnet, peering, three APIs, the cap's Cloud SQL role) and re-render each application root (the `migrate` field and the network inputs). Runner code changed: regenerate the app workflows against this commit and raise `platform.minimum_commit`.

# 1.3.0: spend by application (Phase 6, item 6.6, part one)

## Added

1. **Actual spend by application.** The billing export query now groups by Google service and by the `lab-deploy-app` label that every managed resource carries, so the weekly report, the living issue and `get_cost_report` show the project's spend by application beside the per-service view, and the app's own labelled spend with its breakdown. Firestore operations and Cloud Run request fees carry no label and appear as unlabelled.

The other half of item 6.6, the spending cap stopping databases, arrives with Cloud SQL (item 6.5).

## Upgrade steps

None beyond the billing export being enabled; the next right-sizing run shows the new breakdown.

# 1.2.0: needs in the health view and the retirement checklist (Phase 6, item 6.7)

## Added

1. **`get_service_health` checks the declared needs**: each bucket, topic, subscription and database with whether it exists and the facts that matter (location, acknowledgement deadline, delete protection), through the observer identity, which gains the bucket, Pub/Sub and Datastore viewer roles in the foundation. A missing need becomes a note that names the fix.
2. **`lab-deploy decommission` lists the needs** with their data-loss warnings and the exact commands, reads them from the app's `platform.yaml` when a GitHub credential is present, and tells the operator to remove them from the state before `terraform destroy`.

## Upgrade steps

1. Re-apply the foundation (three viewer roles for the observer) and restart the controller.

# 1.1.1: fix from the live run

## Fixed

1. **The pipeline's apply identity could not finish the first `needs` apply**: the editor role sets no Pub/Sub IAM policies and creates no Firestore databases (two 403s after the bucket, topic and subscription were created). The foundation now grants it `roles/pubsub.admin` and `roles/datastore.owner`.

## Upgrade steps

1. Re-apply the foundation, then re-run the apply for the affected application root (`workflow_dispatch` on the apply workflow).

# 1.1.0: what an app needs, part one (Phase 6, items 6.1 and 6.2)

## Added

1. **`needs` in `platform.yaml`**: `buckets`, `topics` and `database: firestore`. The platform provisions them through the app's Terraform root and the pipeline, and the release sets the derived variables on every revision: `BUCKET_<NAME>` (`<project>-<service>-<name>`), `TOPIC_<NAME>` and `SUBSCRIPTION_<NAME>` (one pull subscription per topic) and `FIRESTORE_DATABASE` (`<service>`). Names follow fixed conventions, so no output travels anywhere and the variables cannot collide with `env` or `secrets`.
2. **Least privilege per need**: the runtime identity gets object user on its buckets, publisher on its topics, subscriber on its subscriptions and datastore user on its own named database only; the deployer may only look, which the new pre-check uses: a release whose needs do not exist yet stops before deploying and says what to merge first.
3. **Policies**: buckets use uniform access with enforced public access prevention; databases are created with delete protection; topics and subscriptions carry the managed labels. Buckets and databases cannot be destroyed by a plan.
4. **Cost**: every plan and estimate lists the declared needs with their list prices and a monthly floor of zero (they cost by use), and the findings explain the free allowances.

## Changed

- The spec hash leaves default values out, so adding an optional field to the schema no longer changes the hash of a spec that does not use it. This change itself changes every hash once.
- The foundation enables the Pub/Sub and Firestore APIs.
- Per-app infrastructure settings (alert thresholds, image retention) live under `applications.<app>.infra` in the controller configuration; the renderer writes them into the root, which is never the source of truth.

## Upgrade steps

1. The spec hash changed: regenerate the app workflows against this commit **before** deploying and set `platform.minimum_commit` to it; plans created before the upgrade must be created again.
2. Re-apply the foundation (two APIs) and re-render each application root (one `needs` line); the pipeline applies it.

# 1.0.2: fixes from the live run, part two

## Fixed

1. **A failure in the publish step was hidden** by the pipe that also writes the job summary, so the first pipeline apply reported success although the outputs were never published. Both workflows now run their steps with `pipefail`, and `publish` explains a root whose outputs predate the current renderer instead of crashing.
2. The demo app's committed root was rendered before the `github_repository` output existed; it is re-rendered.

# 1.0.1: fixes from the live run

## Fixed

1. **The region policy refused every recovery bucket**: Cloud Storage reports locations in capitals (`US-CENTRAL1`), and the rule compared literally. Found by the first pipeline apply, which refused the demo app's plan exactly as designed; the comparison now ignores case.
2. **Cloud Workflows rejected the spending-cap workflow's discovery step**: its expressions cannot contain map or list literals (`{}`, `[]`). Missing fields are now tested with `in` and ordered switch conditions; the test asserts no such literal appears, and the rendered workflow was deployed and deleted as a throwaway before this release.

## Upgrade steps

1. Re-apply the foundation root (one change: the cap workflow's source).

# 1.0.0: infrastructure as pull requests (Phase 6, item 6.3)

## Added

1. **One Terraform root per application.** `infra/gcp/project` is the foundation root (APIs, observer and pipeline identities, cost controls, alert channels, spending cap), applied by the operator and rarely changed. Each application gets `infra/gcp/apps/<service>`, three generated files with every input written out, its own state (`apps/<service>/<environment>`), and the project-wide values read from the foundation's state. `lab-deploy infra render APP` writes a root from the controller configuration, the app's reviewed `platform.yaml` and GitHub's immutable ids; the new tool `propose_infrastructure` renders it and opens the pull request on the platform repository.
2. **The pipeline.** `infra-plan.yml` plans every root a pull request touches with a read-only identity, checks the plan against the platform's policies and posts one comment per application, updated in place. `infra-apply.yml` applies on merge with a write identity from the protected `infra` environment, checks the policy again right before applying, and publishes the outputs to the application's GitHub environment when the `APP_REPOS_TOKEN` secret exists (otherwise it prints them to copy). Both stay dormant until the three `INFRA_*` repository variables are set from the foundation root's `pipeline` output. `workflow_dispatch` applies named roots by hand.
3. **Policies** (`labdeploy/infra.py`, tested, standard library only): no service account keys, no owner or editor grants, no destruction of resources that hold data, nothing outside the allowed region, no public database addresses, every managed resource labelled `lab-deploy-app`, and no touching of the platform's own identities from an application root.
4. **Managed labels.** Services, registries and recovery buckets carry `lab-deploy-managed` and `lab-deploy-app`; releases merge their revision labels in with `--update-labels` instead of replacing them. The spending-cap workflow now discovers the services to close by label, so adding an application no longer needs a foundation re-apply.
5. **Pipeline identities** in the foundation root: a Workload Identity pool for the platform repository, a plan identity (viewer, security reviewer, state reader) and an apply identity whose roles are listed in `pipeline.tf`; the apply identity is only reachable from the `infra` GitHub environment.

## Changed

- The single root (`infra/gcp/main.tf`, `variables.tf`, `outputs.tf`, `moved.tf`) is gone; `services` is no longer a variable. `~/.local/share/lab-deploy/migrate-state.sh` splits an existing state into the two new ones once, without touching the cloud, and the operator's apply script now applies the foundation root.
- `lab-deploy decommission` tells the operator to run `terraform destroy` in the app's root; the pipeline never destroys.

## Upgrade steps

1. Run the state migration once, then apply the foundation root (it adds the pipeline identities and the cap's list permission).
2. Set `INFRA_WIF_PROVIDER`, `INFRA_PLAN_SERVICE_ACCOUNT` and `INFRA_APPLY_SERVICE_ACCOUNT` on the platform repository and create the `infra` environment restricted to `main`; optionally add `APP_REPOS_TOKEN`.
3. Merge the rendered root of each existing application; the pipeline applies the label changes. Runner code changed (`--update-labels`), so regenerate the app workflows and raise `platform.minimum_commit`.

# 0.9.5: clean plans

## Fixed

1. **Every plan wanted to update the dashboard.** The Monitoring API stores dashboards with an explicit axis on each data set and without zero positions or empty group lists, so Terraform saw a difference on every run. The dashboard is now written in that stored form; a read-only plan against the fully applied staging project reports no changes.

## Upgrade steps

None: the next plan is simply clean.

# 0.9.4: fix from the live apply, part three

## Fixed

1. **The uptime check was rejected again** ("validate_ssl feature is currently only allowed on uptime_url checks"): certificate validation is only offered for plain-URL checks. The option is dropped for the Cloud Run target; the check still uses HTTPS. A throwaway check with the exact final shape was created through the API and deleted again before this release, so the next apply is expected to pass.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check and the health alert.

# 0.9.3: fixes from the live apply, part two

## Fixed

1. **The uptime check was rejected** ("Service agent authentication is not supported for arbitrary URLs"): Google offers the Monitoring service agent's OIDC token only for checks that name a Cloud Run service as a `cloud_run_revision` resource, not for a plain URL. The check now names the service by project, location and name, as Google's own example does, and the health alert filters on that resource type.
2. **The saturation alert was rejected**: the Monitoring API only offers greater-than and less-than comparisons. "n or more instances" is now expressed as "more than n-1", which is the same test for whole numbers.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check, the health alert and the saturation alert that the previous apply could not.

# 0.9.2: fix from the live apply

## Fixed

1. **Cloud Workflows rejected the spending-cap workflow** ("Unterminated expression"): its log messages contain a colon followed by a space inside expressions that were not quoted, which YAML reads as a nested key. The six string-building expressions are now single-quoted, as Google's workflow syntax guide asks, and a test renders the file with Terraform's placeholders, parses it and checks that every expression is a complete scalar.
2. **`get_service_health` reported every service as not ready.** Cloud Run v2 returns the Ready condition as `terminalCondition`; the observer looked for it in `conditions`, which only holds RoutesReady and ConfigurationsReady. Found on the first live call after the observer identity existed.

## Upgrade steps

1. Re-run the Terraform apply: it creates the workflow and the Eventarc trigger that the first apply could not.
2. Restart the controller.

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
