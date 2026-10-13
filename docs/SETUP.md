# Connect the first real application

Use a disposable, non-production GCP staging project for the first integration run. Do not paste secrets into chat or commit them to Git.

## 1. Publish the platform repository

Push this project to a dedicated GitHub repository using your normal process. Record its full commit SHA. The app deployment workflow checks out this exact platform commit so a later platform change cannot silently change an existing application's release code.

Protect the platform and application `main` branches. Require review of `platform.yaml`, Dockerfiles, dependency files, and `.github/workflows/`. Replace the placeholder team in `.github/CODEOWNERS` with real maintainers before enabling code-owner review; this archive does not invent usernames. Dependabot opens weekly pull requests for pinned actions and Terraform providers; the templates are checked against the platform's own pins by a test, so bump both together. Choose a license before publishing; none is included. Restrict who can modify Actions workflows, repository variables, environment settings, or dispatch workflows.

## 2. Prepare the application

The app repository must have these at its root:

- `Dockerfile`: a Linux amd64 HTTP service, binding to `0.0.0.0` on the configured port.
- `requirements.txt` and `requirements-dev.txt`: pinned dependencies and test dependencies.
- Pytest tests; pytest's no-tests-found exit code fails CI.
- An HTTP health endpoint returning 200 when ready.
- A reviewed `platform.yaml`; start from the included example or use the analyzer.

The `examples/demo-app` directory is a small complete first app. Copy its contents to a separate test app repository if you want to validate the pipeline before onboarding a lab application. It contains no real healthcare data.

Generate the workflow from the platform directory, replacing the example arguments with your real identifiers:

```bash
python -m labdeploy.cli render-workflow \
  --platform-repo YOUR_ORG/lab-deploy \
  --platform-sha FULL_40_CHARACTER_COMMIT_SHA \
  --output /path/to/app/.github/workflows/deploy-gcp.yml
```

Also generate the independent recovery workflow:

```bash
python -m labdeploy.cli render-workflow \
  --platform-repo YOUR_ORG/lab-deploy \
  --platform-sha FULL_40_CHARACTER_COMMIT_SHA \
  --template templates/recover-gcp.yml \
  --output /path/to/app/.github/workflows/recover-gcp.yml
```

And the read-only right-sizing workflow:

```bash
python -m labdeploy.cli render-workflow \
  --platform-repo YOUR_ORG/lab-deploy \
  --platform-sha FULL_40_CHARACTER_COMMIT_SHA \
  --template templates/rightsize-gcp.yml \
  --output /path/to/app/.github/workflows/rightsize-gcp.yml
```

All three workflows must use the same pinned platform commit. After the first onboarding, `python -m labdeploy.cli update-workflows --all` (or Claude's `update_app_workflows`) regenerates them against the platform's current commit and opens one pull request per app; set `platform: {repository: OWNER/lab-deploy}` in the controller configuration first. Deployment and recovery share a concurrency group; right-sizing is read-only and queues separately. The generator refuses placeholder SHAs and will not overwrite files. If the platform repository is private, add `PLATFORM_READ_TOKEN` as a read-only Contents credential scoped only to that repository. GitHub's app-repository token cannot read a different private repository. An organization-managed GitHub App installation token can replace the development token. Do not use this credential for cloud access.

Create a GitHub environment named **staging**. Restrict deployment branches to `main`. Add a reviewer during initial integration if supported by your GitHub plan; routine staging releases can run without manual approval after validation. No production environment is used by this implementation.

## 3. Bootstrap GCP once

Prerequisites: Google Cloud CLI, Terraform 1.6 or newer, an existing GCP project with billing, and an administrator identity permitted to create the specific resources in `infra/gcp`. The app deployer will not receive those administrator permissions.

Create a dedicated GCS state bucket before `terraform init`. Use uniform bucket-level access, public-access prevention, versioning, and narrowly scoped state access. The bucket must already exist because Terraform initializes its backend before creating resources. State and saved plans must stay out of Git. GCS backend state locking is supported.

Obtain numeric GitHub owner and repository IDs from GitHub's repository API. Names alone can be reassigned; the trust policy checks both immutable IDs and the expected repository name.

Provide an approved, working bootstrap HTTP image, pinned as `...@sha256:...`. It must listen on 8080 and be pullable by the Cloud Run service agent. For example, build the included demo app and push it to a reviewed registry using an administrator's normal bootstrap procedure. The bootstrap service establishes a rollback target before the first automated release. The platform intentionally does not auto-pick an arbitrary public bootstrap image.

Terraform is split into a **foundation root** (`infra/gcp/project`: APIs, identities, cost controls, alert channels, the spending cap) that the operator applies, and **one root per application** (`infra/gcp/apps/<service>`) that `lab-deploy infra render` generates and the pipeline applies (section 7f). Create `infra/gcp/project/terraform.tfvars` locally:

```hcl
project_id = "YOUR_STAGING_PROJECT"
region     = "us-central1"

# Optional cost controls and alerts; see section 7.
# monthly_budget_usd     = 50
# billing_account        = "XXXXXX-XXXXXX-XXXXXX"
# budget_alert_email     = "lab-admin@example.edu"
# alert_email            = "lab-admin@example.edu"
# billing_export_dataset = "billing_export"
# observer_impersonators = ["user:you@example.edu"]

# The infrastructure pipeline (section 7f); omit until the platform repository exists on GitHub.
# pipeline = { platform_repository = "YOUR_ORG/lab-deploy", platform_repository_id = "NUMERIC_ID", platform_owner_id = "NUMERIC_OWNER_ID" }
```

Then, using your administrator login:

```bash
gcloud auth login
scripts/apply-foundation.sh YOUR_STAGING_PROJECT staging
```

The script runs Terraform in its official container against the state bucket `YOUR_STAGING_PROJECT-tfstate`, shows the plan, waits for a typed `yes`, applies, and prints the four `INFRA_*` values the pipeline needs and the backup bucket's name. Without Docker, run the same `terraform init`, `plan`, `apply` and `output` commands in `infra/gcp/project` with `-backend-config="bucket=..."` and `-backend-config="prefix=project/staging"`.

The first application's root comes from the controller configuration (its `applications` entry with the `target` block) and GitHub's numeric ids, which the renderer fetches. The target values exist before the root does, because the module names them deterministically; fill the block with these and the apply confirms them in its outputs:

| `target` field | Value for service `SERVICE` in project `PROJECT` (number `NUMBER`) |
|---|---|
| `project_id`, `region`, `service` | `PROJECT`, `us-central1`, `SERVICE` |
| `registry` | `us-central1-docker.pkg.dev/PROJECT/SERVICE` |
| `deploy_service_account` | `SERVICE-deploy@PROJECT.iam.gserviceaccount.com` |
| `probe_service_account` | `SERVICE-probe@PROJECT.iam.gserviceaccount.com` |
| `wif_provider` | `projects/NUMBER/locations/global/workloadIdentityPools/SERVICE-gh/providers/github` |
| `recovery_bucket` | `PROJECT-SERVICE-recovery` |

With the pipeline in place (section 7f) the normal path is `python -m labdeploy.cli infra render YOUR_APP --pull-request` and a merge; the commands below are the manual path for a project without the pipeline yet:

```bash
python -m labdeploy.cli infra render YOUR_APP --write --bootstrap-image "APPROVED_IMAGE@sha256:REAL_DIGEST"
cd infra/gcp/apps/YOUR_SERVICE
terraform init
terraform plan -out=app.tfplan && terraform apply app.tfplan
terraform output -json github_environment_variables
```

Commit the generated root. From then on the pipeline plans and applies it on every pull request that touches it; the operator only needs `terraform` locally for the foundation. Inspect every plan before applying. It creates billable resources. Keep `.terraform.lock.hcl` in Git. Never grant the MCP server GCP administrator access.

Terraform owns the foundation and IAM. The workflow owns the Cloud Run revision template and traffic after bootstrap. `ignore_changes` makes that ownership explicit; change workload settings through reviewed app config. Database schemas and data are outside this release's rollback contract.

The pipeline publishes the application root's outputs to **GitHub Settings → Environments → staging → Environment variables** when the `APP_REPOS_TOKEN` secret exists (section 7f); otherwise copy them by hand:

| Variable | Purpose |
|---|---|
| `GCP_PROJECT_ID` | Staging project |
| `GCP_REGION` | `us-central1` |
| `CLOUD_RUN_SERVICE` | Must match `platform.yaml` name |
| `ARTIFACT_REPOSITORY` | Docker repository created by Terraform |
| `WIF_PROVIDER` | Full workload identity provider resource name |
| `DEPLOY_SERVICE_ACCOUNT` | Restricted deployment service account email |
| `PROBE_SERVICE_ACCOUNT` | Invoker identity for health checks |
| `RECOVERY_BUCKET` | Private versioned bucket for durable release checkpoints |

The WIF condition only accepts the named app's `deploy-gcp.yml`, `recover-gcp.yml` and `rightsize-gcp.yml` on `main` in `staging`, matched through the token's immutable repository and owner IDs, branch, `environment` claim and workflow path. GitHub's `sub` claim now embeds numeric IDs (`repo:OWNER@ID/NAME@ID:environment:ENV`), which is why the condition does not hard-code the subject string. The deployer identity also receives project-level read-only `roles/monitoring.viewer` and `roles/run.viewer` so the right-sizing job can read usage metrics and Google's cost recommendations. No service-account JSON key goes into GitHub. The runtime identity has no app-specific project roles by default. Add separate, reviewed IAM bindings for legitimate runtime access later.

Confirm the bootstrap service is healthy and that no project-level or inherited IAM grant makes Cloud Run public. The release runner also checks service IAM and invoker-check configuration; it cannot fully inspect inherited organization policy. The service is reachable over the internet but requires IAM authentication. "Authenticated" is not the same as internal-network-only.

Copy those same values into the application's `target` block in `config.local.yaml` (see `config.example.yaml`). Plans pin that exact destination, and the deployment job compares it to environment settings before cloud authentication. Do not reuse one target service across independently controlled app repositories.

The recovery workflow runs after every completed release workflow and, as a safety net, every six hours (a tighter schedule costs Actions minutes on private repositories). Configure staging environment protection rules so this recovery identity can run when needed; mandatory manual reviewers will delay recovery. Schedules are not an availability SLA. Ensure both workflows are enabled and monitor recovery failures.

## 4. GitHub credentials for the controller

Prefer a GitHub App installed only on allowed app repositories. Set these environment variables on the controller host:

- `GITHUB_APP_ID`
- `GITHUB_INSTALLATION_ID`
- `GITHUB_APP_PRIVATE_KEY_FILE`: a secret-mounted private key path

Minimum repository permissions for analyze/plan/dispatch/status: **Contents read**, **Actions write**, and metadata read. If enabling `propose_configuration` or `update_app_workflows`, additionally allow **Contents write**, **Pull requests write** and, for workflow files, **Workflows write**. Branch protection must prevent the App from bypassing review on `main`; the proposal tool only creates a new branch and draft PR.

For a local pilot, `GITHUB_TOKEN` may hold a fine-grained token with the same restricted repository permissions. It takes precedence over App credentials. Never return this token to Claude or reuse it as the MCP bearer token. App installation tokens refresh in the server; no client credential is passed through to GitHub.

## 5. Host MCP and connect Claude

Start locally first as described in README. For Claude Chat, host the controller on a reachable **HTTPS** endpoint behind your organization's trusted TLS proxy. A localhost URL is not reachable by Claude's hosted connector.

- Keep one controller instance for this MVP, backed by persistent storage for SQLite.
- In a container, mount the config read-only at `/config/config.yaml`, the database directory at `/data`, and the GitHub private key as a secret. Set the config `database: /data/deployments.db`.
- Add the exact external host to `allowed_hosts`. Preserve the original Host header through your proxy. Do not set a wildcard hostname.
- Configure the proxy to enforce HTTPS, size/time limits, and rate limits; do not log Authorization headers. The MCP body limit is supplied by the SDK.
- Give each operator a separate generated token and config principal. Do not use one shared team credential if you need individual attribution.
- The controller budgets requests per identity (`rate_limit_per_minute`, default 120) and failed logins per client address (`auth_failures_per_minute`, default 20). Behind a proxy the client address is the proxy's, so keep a proxy-level limit as well.

Enable the browser sign-in by adding the controller's public origin to the configuration and restarting:

```yaml
oauth:
  public_url: https://YOUR_HOST
```

Then, in Claude (Customize > Connectors > Add custom connector), enter `https://YOUR_HOST/mcp`, choose **Sign in now** and **Register automatically**, and click **Connect**. The browser opens `https://YOUR_HOST/login`; paste your operator token there, never in chat. Claude keeps a one-hour access token and a thirty-day refresh token, both stored hashed in the controller's database. The sign-in page shows which client is asking and where it returns; only `https://claude.ai/api/mcp/auth_callback` and loopback callbacks (Claude Code) can register. Claude's fixed **Request headers** option (beta, limited organizations) also works when your organization has it: choose **No sign-in** and add `authorization` with the value `Bearer <your-operator-token>`.

To end sessions, replace the operator's `token_sha256` and restart (every session signed in with that token ends), run `python -m labdeploy.cli sessions --revoke ID`, or call `POST /revoke` with `token`, `client_id` and a `client_secret` field (empty for the public clients the controller registers: the SDK's endpoint requires the field to be present). Lower `oauth.access_token_seconds` or `oauth.refresh_token_days` to shorten lifetimes. Sign-in with the university identity provider is not implemented; the sign-in page is where it would plug in.

Anthropic's connector permission prompts are additional user-interface controls. The server's identity/app allowlists and GitHub/GCP permissions enforce the actual access boundary.

Day-to-day use from the chat, with the prompts and the tools behind them, is in `docs/CLAUDE-CHAT.md`.

## 6. First release

1. With dispatch disabled, ask Claude to list applications, analyze the registered app, and create a deployment plan. Confirm the repository, full SHA, resource defaults, and checks still marked pending.
2. For a new app with no `platform.yaml`, enable writes after the GitHub configuration is ready and ask it to propose configuration. Inspect the draft PR, fix assumptions, mark the config reviewed, and merge normally. Do not mark an unknown app reviewed just to pass validation.
3. After the staging environment and Terraform bootstrap are verified, set `live_dispatch: true` and restart the controller.
4. Ask: **"Create a plan for demo on GCP staging and deploy that plan."**
5. Check the returned workflow status. `dispatched` means GitHub accepted the job; only the workflow's actual conclusion and release receipt indicate the result.
6. Download the workflow's `release-receipt` artifact to inspect the image digest, previous traffic split, revision, and result. If the runner disappeared before artifact upload, the checkpoint remains at `gs://RECOVERY_BUCKET/releases/SERVICE.json`; the independent workflow emits a `recovery-receipt` artifact.

The release creates a candidate revision, probes its tagged URL with an audience-bound identity token, promotes it, and probes the service URL. On failure it restores the prior concrete revisions. It reports `rollback_failed_manual_recovery_required` if that restoration fails. Cleanup removes the candidate tag on both success and failure. It does not claim that restored traffic guarantees healthy downstream data or dependencies.

## 6a. Configuration, secrets, tests and timeouts

- **Plain values**: list them under `env` in `platform.yaml` (uppercase names, single-line values, at most 32). They are set on every revision and are part of the reviewed configuration, so a change is a release.
- **Secrets**: list names under `secrets`; the value lives in Secret Manager as `<service>-NAME` and reaches the container as the variable `NAME`. Create and rotate values yourself, never through Claude:

```bash
printf '%s' 'the value' | gcloud secrets create lab-demo-API_KEY --replication-policy=automatic --data-file=- --project YOUR_PROJECT
printf '%s' 'the new value' | gcloud secrets versions add lab-demo-API_KEY --data-file=- --project YOUR_PROJECT
```

  The next release picks up the newest enabled version. A release whose secret is missing stops before anything changes and names the secret. Only the app's runtime identity can read its own `<service>-` secrets.
- **Apps without tests**: set `tests: none` knowingly. Every plan then warns that nothing checks the app's behaviour before a release; the health probe still guards traffic. Prefer adding one pytest that imports the app and calls the health path.
- **Request timeout**: `timeout_seconds`, up to 300 in staging (default 60).
- **Reproducible builds**: a `requirements.txt` without hashes makes the same commit build differently over time. Generate `requirements.lock` with `pip-compile --generate-hashes requirements.txt -o requirements.lock`, install it with `--require-hashes` in the Dockerfile, and commit both; the analyzer and the test step recognise it.

## 6b. Buckets, topics and a database

Declare them under `needs` in `platform.yaml`; the platform creates them, grants the app's runtime identity the narrowest role for each, and sets the variables the code reads:

```yaml
needs:
  buckets: [uploads]        # BUCKET_UPLOADS = PROJECT-SERVICE-uploads
  topics: [events]          # TOPIC_EVENTS = projects/PROJECT/topics/SERVICE-events, SUBSCRIPTION_EVENTS = .../subscriptions/SERVICE-events
  database: firestore       # FIRESTORE_DATABASE = SERVICE (a named database in Native mode, delete protection on)
```

Names are short lowercase words; the real resource names follow the conventions in the comments, so nothing has to be copied anywhere. After merging the change, ask Claude for `propose_infrastructure` (or run `python -m labdeploy.cli infra render APP --write`): the pull request on the platform repository shows the bucket, topic, subscription, database and grants the apply will create. A release whose needs have not been applied yet stops before deploying and names them. Removing a bucket or database from `needs` is refused by the plan (data is never destroyed by the pipeline); retire them through `lab-deploy decommission`.

Cost: these cost by use and nothing while idle. Storage is about 2 cents per GiB-month after the account's free 5 GB, messages 40 dollars per TiB after the free 10 GiB each month, and Firestore cents per 100,000 document operations after the free daily quota; `estimate_cost` lists them. Google allows one free Firestore database per project.

**PostgreSQL and migrations.** `database: postgres` creates the smallest Cloud SQL instance with a private address and IAM sign-in only; the app's runtime identity is the database user and no password exists. The revision gets `DATABASE_INSTANCE` (`PROJECT:REGION:SERVICE`), `DATABASE_NAME` (`app`), `DATABASE_USER` (`SERVICE-runtime@PROJECT.iam`) and `DATABASE_IP_TYPE` (`private`); connect with the Cloud SQL Python Connector (`cloud-sql-python-connector[pg8000]`) using `enable_iam_auth=True` and `ip_type=IPTypes.PRIVATE`, or any client that understands those four values. `migrate: ["alembic", "upgrade", "head"]` runs that command as the `SERVICE-migrate` job from the release's image before the candidate exists; a failure stops the release and names the job's log. Make every migration keep the previous revision working (add columns first, drop them in a later release) so a rollback stays a traffic change. Unlike everything else here the instance bills every hour it exists, about 9 dollars a month at the smallest size; the spending cap stops it at the budget line, and `estimate_cost` counts it toward the cost ceiling. Declare it only when SQL is really needed.

An app has one `database`. Changing its type means retiring the old database first (the retirement checklist's data steps, then `terraform state rm` for it); until then the plan is refused with `Instance cannot be destroyed`, because a plan never deletes data.

## 7a. Watch it run

- **Status in Claude** shows every workflow step with timing, the release phases from the receipt, the candidate's last log lines when it fails, the recovery run that followed, and a next action. Ask for `get_deployment_status` with `wait_seconds: 45` to wait for a change instead of asking repeatedly.
- **Notifications**: add a repository secret `NOTIFY_WEBHOOK_URL` holding a Slack or Discord incoming webhook. The deploy, recovery and right-sizing workflows post their outcome; without the secret they stay silent. Turn on GitHub's own failed-workflow emails as well.
- **GitHub Deployments**: every release appears on its commit and under the repository's Environments, with the service URL, from the record GitHub keeps for the deploy job's environment.
- **Runtime alerts** (Terraform, `runtime_alerts = true` by default): an uptime check that signs in to the service, alerts for the health check, 5xx responses, p95 latency, instances at maximum, memory above 80% and error lines in the log, and a dashboard (`dashboard_url` output). Set `alert_email` and optionally `alert_webhook_url` in the foundation's `terraform.tfvars`; `runtime_alerts` and the thresholds (`error_alert_count`, `latency_alert_ms`, `instance_alert_count`) live under `applications.<app>.infra` in the controller configuration and reach the root at the next render; the health path is the one in `platform.yaml`. The uptime check runs as the Monitoring service agent `service-PROJECT_NUMBER@gcp-sa-monitoring-notification.iam.gserviceaccount.com`; in a project where it does not exist yet, run `gcloud beta services identity create --service=monitoring.googleapis.com` once before `terraform apply`.
- **Audit trail**: `list_events` (or `GET /events`) lists every tool and REST call, sign-in, throttled burst, client registration, token reuse and revocation with identity, client address, outcome and duration. `log_file` in the configuration adds a rotating controller log.
- **Serving images are protected**: the release job tags each serving revision's image `serving-<revision>` and the registry cleanup keeps those tags, so a rollback target never loses its image. The weekly right-sizing job reconciles the tags.

## 7e. Always on, on a laptop

- **Live views**: add `observer: {service_account: lab-deploy-observer@PROJECT.iam.gserviceaccount.com}` to the configuration and list yourself in Terraform's `observer_impersonators` (for example `["user:you@example.edu"]`). The controller then asks gcloud for short-lived tokens of that viewer-only identity for `get_service_health` and `get_recent_logs`; on a Google Cloud machine it uses the attached identity instead. Nothing here can change anything in the cloud.
- **Auto-deploy**: `auto_deploy: true` on an application. Every few minutes the controller compares the tip of main with the live release and releases it once, as the identity `auto-deploy`, unless the commit was already attempted, the app or the controller is paused, or a policy refuses it. Notifications come from the workflow as usual.
- **Watchdog**: set `notify_webhook_url` (Slack or Discord) to receive the daily findings: an expiring GitHub credential, a paused or stale schedule, an unsupported platform pin, a newest release attempt that failed, or a configuration file that could not be loaded. `python -m labdeploy.cli watchdog` runs it now.
- **Backups**: the database is copied nightly next to itself (`backup_dir` to change), fourteen copies kept, and, with `backup_bucket` set to the foundation's `backup_bucket` output (`PROJECT-lab-deploy-backups`), the copy and the configuration file go to that private versioned bucket as well, using the operator's own gcloud sign-in. `python -m labdeploy.cli backup` runs it now; `get_controller_status` shows the last run. Restoring is `docs/RUNBOOK.md`, "Restore the controller on another machine", rehearsed on 2026-10-12.
- **Is the controller up?** Set the repository variable `CONTROLLER_HEALTH_URL` on the platform repository to `https://YOUR_HOST/healthz`; its controller-watch workflow pings it every thirty minutes and fails loudly when it does not answer.

## 7b. Steer after a release

- **Roll back**: ask Claude for `rollback_staging`. Traffic returns to the split that served before the live release through the recovery workflow, in seconds, with no build. The runner refuses if traffic has moved since or the checkpoint belongs to another release; deploy the wanted commit instead in that case.
- **Cancel**: `cancel_deployment` with the plan id. The recovery workflow restores traffic if the release had started changing it.
- **Deploy an older commit**: `create_deployment_plan` with `commit_sha`. Only commits already on `main` are accepted, checked by the controller and again by the workflow. A commit released before is redeployed from its attested image without a rebuild.
- **What is waiting on main**: `compare_with_live` lists the commits and files since the live release, the kinds of change, and the cost bounds before and after when `platform.yaml` changed. `deploy_latest` plans and dispatches in one call.
- **Pause**: set `paused: true` on the configuration (everything) or on one application; the controller re-reads the file within the next request, refuses new plans and dispatches, and keeps answering reads. Remove the line to resume. `get_controller_status` reports a configuration file it could not load.
- **Sessions**: `python -m labdeploy.cli sessions` lists Claude's sign-in sessions; `--revoke-all` or `--revoke ID` ends them.
- **Platform pins**: `list_applications` shows which platform commit each app's workflows pin. Set `platform.minimum_commit` to refuse dispatch for apps on older pins until `update_app_workflows` regenerates them.
- **Drift**: the weekly cost report compares the live Cloud Run settings with the reviewed `platform.yaml` and names any difference.

## 7c. Terraform layout for several applications

Two kinds of root, each with its own state in the same bucket:

- `infra/gcp/project` (state prefix `project/<environment>`): everything applications share, through `modules/project`. Applied by the operator with `scripts/apply-foundation.sh PROJECT_ID` (section 3). Its outputs (project number, alert channels, the tag-manager role, the cap and observer identities, the billing dataset) are read by every application root through `terraform_remote_state`.
- `infra/gcp/apps/<service>` (state prefix `apps/<service>/<environment>`): one application, through `modules/service`. Generated by `python -m labdeploy.cli infra render APP --write` or the `propose_infrastructure` tool, with every input written out as a literal so a reviewer sees exactly what will exist. Applied by the pipeline (section 7f). Google's guidance is one directory per application and a few dozen resources per state; a failing resource for one application then never blocks another.

A state written by versions up to 0.9.x holds both kinds in one object. Run `scripts/migrate-state.sh PROJECT_ID` once: it pulls the old state, splits it locally, pushes the two new states and leaves the old object as a backup. Nothing in the cloud changes; the next plans show only what 1.0.0 adds (the pipeline identities and the managed labels).

## 7. Cost controls and right-sizing

- **Before deploying**, ask Claude to run `estimate_cost` for the application, optionally with expected monthly requests and average request duration. The reply gives the zero-traffic floor, the worst-case compute ceiling and the findings. Set `cost_ceiling_usd` for each application in `config.local.yaml` to refuse plans whose worst case is too high.
- **After the first week of traffic**, the right-sizing workflow (weekly, or `workflow_dispatch` with a `window_days` input) uploads `cost-report.json`. Ask Claude for `get_cost_report`. Apply accepted proposals by editing `platform.yaml` through a reviewed pull request; nothing changes on its own.
- **Image cleanup** starts in dry-run mode. Review the Artifact Registry audit logs for what would be deleted, then set `registry_cleanup_dry_run: false` under `applications.<app>.infra` in the controller configuration and render the root again (a pull request the pipeline applies). The newest `image_versions_to_keep` versions are always kept as rollback targets.
- **Spending cap**: with a budget set, `spending_cap` (default `{ enabled = true, threshold = 1.0 }`) makes every budget notification run a Cloud Workflow that closes ingress on every platform service once spend reaches the threshold of the budget. Requests then fail and compute stops; stored data and images are untouched; managed Cloud SQL instances are stopped as well. Each service and instance is handled on its own with retries, and an execution that could not close everything fails and raises the alert `Lab Deploy: spending-cap workflow failed`. Notifications repeat all month, so a service a release reopened is closed again at the next one unless it carries the lift label: `gcloud run services update SERVICE --region REGION --ingress=all --update-labels lab-deploy-cap-lifted=true` (for an instance, `gcloud sql instances patch NAME --activation-policy=ALWAYS --update-labels lab-deploy-cap-lifted=true`); remove the label when the new month starts. The ratio is computed from the notification's amounts, so `threshold` and `detach_threshold` may be any value. `detach_billing = true` with `detach_threshold` also detaches billing from the project at a higher line, which stops everything including the export, so keep it off unless the project is disposable. The budget notification itself still emails billing administrators.
- **Actual spend**: set `billing_export_dataset` (for example `billing_export`) and apply, then enable the Cloud Billing export to BigQuery into that dataset in the Billing console (Billing export, standard usage cost). Once the table exists, set the repository variable `BILLING_EXPORT_TABLE` to `PROJECT.DATASET.TABLE`; the weekly report, the issue and `get_cost_report` then show net spend by Google service.
- **Weekly issue and proposals**: the right-sizing workflow keeps one issue per repository up to date. Ask Claude for `propose_rightsizing` to turn the concrete proposals into a pull request against `platform.yaml`.
- **Budget alert**: set `monthly_budget_usd` and `billing_account` (and optionally `budget_alert_email`) in `terraform.tfvars`. The administrator identity needs Billing Account Costs Manager or equivalent on that billing account. If `terraform apply` answers 403 for the budget, add `user_project_override = true` and `billing_project = var.project_id` to the provider block so the Billing Budgets API is called with a quota project. Budgets notify at 50%, 90% and 100% of actual spend and at 100% forecast spend; they do not stop spending.
- **Keep the cheap defaults**: `scaling.minimum: 0`, 512 MiB, one vCPU, authenticated access, Tier 1 region. Raise anything only with a measurement from the report.

## 7d. Retire an application

`python -m labdeploy.cli decommission APP` prints the ordered checklist with the app's own names: pause it, warn its owners, delete the service, empty and delete the recovery bucket, delete the images and secrets, delete the data of each declared need deliberately (buckets and the database are never destroyed by a plan), run `terraform state rm` for those and `terraform destroy` in the app's root as the operator, delete the root directory in a pull request (the pipeline never destroys), remove it from the controller configuration (an onboarded application: `python -m labdeploy.cli unregister APP`, which also drops its onboarding record), and disable its workflows and credential access. Plans stay in the database as history. For a Postgres need the checklist turns deletion protection off and deletes the instance (its name stays reserved for about a week) and deletes the migration job.

## 7f. Infrastructure as pull requests

Every change to an application's infrastructure is a pull request on the platform repository: the controller or the operator renders the root, the **Infrastructure plan** workflow posts what an apply would do, a human merges, and the **Infrastructure apply** workflow applies exactly that. Nobody runs `terraform apply` for applications by hand once this is set up.

Set it up once:

1. Add the `pipeline` block to the foundation's `terraform.tfvars` (the platform repository's name and numeric ids, from `gh api repos/OWNER/lab-deploy`) and apply the foundation. `terraform output pipeline` prints two Workload Identity providers and two identities. `lab-deploy-infra-plan` (specific viewer roles, security reviewer, state reads under `apps/` and `project/`) is reachable only from `infra-plan.yml` on a pull request; `lab-deploy-infra-apply` (the admin roles an application root needs, listed in `infra/gcp/modules/project/pipeline.tf`, no basic role, state writes under `apps/` only) is reachable only from `infra-apply.yml` on `main` in the `infra` environment. The apply identity can change the project's IAM, which is why merging to `main` must be the only way to reach it.
2. On the platform repository, set the repository variables `INFRA_PLAN_WIF_PROVIDER`, `INFRA_APPLY_WIF_PROVIDER`, `INFRA_PLAN_SERVICE_ACCOUNT` and `INFRA_APPLY_SERVICE_ACCOUNT` from that output. Both workflows stay dormant until they exist.
3. Create the GitHub environment `infra`, restrict its deployment branches to `main` and turn off "allow administrators to bypass"; protect `main` itself: a pull request with the `test` and `terraform` checks is required, administrators included, no force pushes, no deletion (`.github/CODEOWNERS` names the owner of `infra/`, the policy and the workflows). Add required reviewers on the environment when more than one person can merge.
4. Optionally add the secret `APP_REPOS_TOKEN`: a fine-grained token with **Variables: read and write** on the application repositories. With it, the apply job writes the eight outputs into each application's `staging` environment; without it, the job prints them for the operator to copy.

How a change flows: `python -m labdeploy.cli infra render APP --pull-request` (or Claude's `propose_infrastructure`) opens the pull request; the plan job runs with the read-only identity and `-lock=false`, checks the plan with `python -m labdeploy.infra check` and posts one comment per application that it updates on every push; merging runs the apply job from the `infra` environment, which plans again with the lock, refuses any plan the policy rejects, applies, and publishes the outputs. Policies, all fail-closed and judged by the rules on `main` rather than the pull request's own copy: no service account keys; only roles from an allow-list of what an application needs (never owner, editor or an IAM-admin role); no `allUsers` or `allAuthenticatedUsers` anywhere; no authoritative IAM policies; no destruction of resources that hold data (buckets, databases, datasets, secrets, registries); nothing outside `us-central1`; databases private and delete-protected, buckets uniform and non-public, on creation and on every update; the `lab-deploy-app` label on every managed resource; no reference to the platform's own identities or the state bucket from an application root; and a value the plan cannot know before applying is refused rather than assumed. The comment on the pull request carries the full plan text. A change to `modules/service` plans and applies every root. `workflow_dispatch` on the apply workflow applies named roots by hand, for example after the foundation first exists.

Retiring an application is the one case the pipeline does not handle: the operator runs `terraform destroy` in the root (section 7d) and removes the directory in a pull request.

## 7g. One sentence: onboarding from a repository link

`onboard_application` turns a repository that follows `docs/DEVELOPER-CONTRACT.md` into a running service without the manual steps of section 3. It needs three things:

1. An `onboarding` block in the controller configuration:

```yaml
onboarding:
  project_id: YOUR_STAGING_PROJECT
  project_number: "123456789012"     # gcloud projects describe YOUR_STAGING_PROJECT --format='value(projectNumber)'
  region: us-central1
  # state_bucket: YOUR_STAGING_PROJECT-tfstate   (the default)
  # billing_export_table: PROJECT.billing_export.gcp_billing_export_v1_XXXXXX   (written to each app's environment when set)
  # auto_merge: true   (false keeps every merge for a person; the onboarding then waits)
```

2. The operator's identity with `applications: ["*"]`, so it sees the applications onboarding registers (they live in the controller's database, not in this file).
3. A GitHub credential that may write. The fine-grained token covers **all repositories** of the account (or the organization's) with these permissions: Contents read and write, Pull requests read and write, Workflows read and write, Actions read and write, Environments read and write, Variables read and write, Administration read and write (environments are created through it), Metadata read. Store it with `scripts/start.sh` as before. A GitHub App with the same permissions is the planned replacement (roadmap, decision D14).

What the tool does, in order: contract check (blocks with a checklist), registration, infrastructure pull request (merged once the plan workflow's policy verdict is `passed` and only when every file sits under `infra/gcp/apps/<service>/`), the apply, the `staging` environment with its variables, the workflows pull request (merged when GitHub reports it mergeable), the first release. Long waits end the call and the scheduler continues every minute; `get_onboarding_status` reports the step, and a blocked onboarding resumes from its step when the same tool is called again.

The operator's switches for an onboarded application (a cost ceiling, auto-deploy, a pause, viewers, infrastructure settings) go under `application_settings: <app id>:` in `config.local.yaml`; they are laid over the registered entry on the next request, with no restart and nothing copied (`config.example.yaml` shows the shape).

## 7h. Pages for people (Identity-Aware Proxy)

Applications with `exposure: lab-members` get Google's Identity-Aware Proxy directly on their Cloud Run service. One
step is manual because a project without an organization cannot create the proxy's OAuth client programmatically:

1. In the console, open any Cloud Run service of the project (the demo), **Security**, choose **Require authentication**
   and **Identity-Aware Proxy (IAP)**, then **Auto generate credentials** and deploy the change. This creates the
   project's OAuth brand (External) and client and applies them to IAP at the project level. Then switch that service back
   (`gcloud run services update SERVICE --region REGION --no-iap`) unless its `platform.yaml` says `lab-members`.
2. Read the client id (APIs & Services, Credentials; the id ends in `.apps.googleusercontent.com`; the secret stays where
   it is) and put it in the foundation's `terraform.tfvars` as `iap_client_id` and in the controller configuration under
   `onboarding.iap_client_id`; list the lab's accounts under `onboarding.default_viewers` (`user:name@example.edu`).
3. Apply the foundation (`scripts/apply-foundation.sh`): it enables the IAP API, creates the IAP service agent and
   publishes the client id to every application root through the remote state.
4. For an existing application: set `IAP_CLIENT_ID` in its `staging` environment (onboarding does this for new ones),
   render its root again and merge; the pipeline switches the proxy on, grants the viewers and the release probe, and
   changes the uptime check to accept the sign-in redirect. The next release's probes sign in through the proxy.

What the runner enforces: a release whose `platform.yaml` disagrees with the service's proxy state stops before anything
changes ("render and merge the infrastructure first"), so the switch is only ever flipped by the pipeline.

## 8. Provenance, scanning and dependency updates

- **Build provenance**: the deployment job signs an attestation for the pushed image digest and verifies it with `gh attestation verify` before release. Nothing to configure. If the verification step fails in the first trial, confirm the runner can read the private registry through `docker-credential-gcloud`; the step fails closed by design.
- **Image scanning**: Terraform enables Artifact Analysis (`image_vulnerability_scanning`, about USD 0.26 per new image digest), often more than a small staging app's compute for the month; it is a per-project cost choice, and the estimate names it. The deployment job writes `vulnerability-summary.json` into the release receipt and the job summary. To block releases with critical findings, set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES` to `true`; an unfinished scan then also blocks.
- **Platform dependencies** are hash-locked. Refresh them deliberately, review the diff, and let CI's pip-audit and tests judge the result:

```bash
uv pip compile --generate-hashes --upgrade pyproject.toml -o requirements.lock
```

```bash
uv pip compile --generate-hashes --upgrade pyproject.toml --extra dev -o requirements-dev.lock
```


## Failure handling

- **Config policy rejected:** resolve the reported setting in Git. Do not add a conversational exception.
- **Branch moved or plan expired:** create a fresh plan.
- **Dispatch outcome unknown:** inspect Actions before requesting another deployment. An HTTP timeout may occur after GitHub accepted the request. There is no unsafe automatic dispatch retry.
- **Build/test/audit failure:** fix the app before retrying; the deployment job will not receive credentials.
- **Cloud permission failure:** compare the concrete permission with Terraform; do not substitute Owner or Editor roles.
- **Failed candidate:** a candidate that never becomes ready stays as the service's newest revision with no traffic, because Cloud Run does not allow deleting the newest revision. The console shows the service as not ready until the next successful release while the previous revision keeps serving; the receipt says so (`service_ready: false`). Fix the app and release again.
- **Rollback failure or terminated runner:** inspect the independent recovery workflow and the GCS checkpoint. The next recovery run attempts to restore the previous traffic map. If the API is unavailable, the target changed, or outside traffic changes are detected, recovery fails visibly for operator review. Use an administrator-approved recovery command if necessary.
- **Controller restart while dispatching:** status lookup locates the run by its unique title; a record still `dispatching` or `dispatch_unknown` two minutes later with no visible run becomes `dispatch_lost`, a final state whose advice is to create a new plan. Auto-deploy retries a lost or errored attempt after thirty minutes. This MVP does not implement an exactly-once distributed job queue.

Before adopting for production, add an external rollout controller/monitor, enterprise identity, hardened image supply-chain verification, organization-specific policies, and recovery exercises.

