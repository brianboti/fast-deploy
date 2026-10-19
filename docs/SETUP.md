# Connect the first real application

Use a disposable, non-production GCP staging project for the first integration run. Do not paste secrets into chat or commit them to Git.

## 1. Publish the platform repository

Push this project to a dedicated GitHub repository using your normal process. Record its full commit SHA. The app deployment workflow checks out this exact platform commit so a later platform change cannot silently change an existing application's release code.

Protect the platform and application `main` branches. Require review of `platform.yaml`, Dockerfiles, dependency files, and `.github/workflows/`. Replace the placeholder team in `.github/CODEOWNERS` with real maintainers before enabling code-owner review; this archive does not invent usernames. Dependabot opens weekly pull requests for pinned actions and Terraform providers; the templates are checked against the platform's own pins by a test, so bump both together. The other pins (the tools the pipeline downloads, the base and buildpacks images, uv, npm, Caddy and Litestream) arrive in the weekly pin refresh's pull request (section 7o). Choose a license before publishing; none is included. Restrict who can modify Actions workflows, repository variables, environment settings, or dispatch workflows.

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

All three workflows must use the same pinned platform commit. After the first onboarding, `python -m labdeploy.cli update-workflows --all` (or Claude's `update_app_workflows`) regenerates them against the platform's current commit and opens one pull request per app; set `platform: {repository: OWNER/lab-deploy}` in the controller configuration first. On the machine, `platform_updates` does this by itself after each release (`docs/RUNBOOK.md`, "After a release"). Deployment and recovery share a concurrency group; right-sizing is read-only and queues separately. The generator refuses placeholder SHAs and will not overwrite files. If the platform repository is private, add `PLATFORM_READ_TOKEN` as a read-only Contents credential scoped only to that repository. GitHub's app-repository token cannot read a different private repository. An organization-managed GitHub App installation token can replace the development token. Do not use this credential for cloud access.

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
# pipeline = { platform_repository = "YOUR_ORG/fast-deploy", platform_repository_id = "NUMERIC_ID", platform_owner_id = "NUMERIC_OWNER_ID" }
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

The controller's machine (section 7m) signs in to GitHub as a **GitHub App** (decision D14): an installation token per
repository, minted for an hour at a time with the App's private key, which the controller reads from Secret Manager
through the machine's own identity and keeps in memory only. A laptop can use the same App (its key read with your gcloud
sign-in) or a fine-grained token.

Create the App once, on the account that owns the repositories. The quickest way is GitHub's form with everything
filled in through its URL (name, homepage, no webhook, only on this account, and exactly the permissions below):

```text
https://github.com/settings/apps/new?name=fast-deploy-YOUR-ACCOUNT&url=https%3A%2F%2Fgithub.com%2FOWNER%2Ffast-deploy&public=false&webhook_active=false&actions=write&administration=write&contents=write&environments=write&metadata=read&pull_requests=write&workflows=write
```

Check the page and click **Create GitHub App**. By hand it is:

1. Name: anything unique, for example `fast-deploy-YOUR-ACCOUNT`; Homepage URL: the platform repository's address.
2. Webhook: clear **Active**. The controller asks GitHub; GitHub never calls it.
3. Repository permissions, exactly what the controller uses and nothing else: **Actions** read and write,
   **Administration** read and write (environments are created through it), **Contents** read and write,
   **Environments** read and write (its variables too), **Metadata** read-only, **Pull requests** read and write,
   **Workflows** read and write. No **Variables** (the controller never writes repository-level variables), and no
   organization or account permissions.
4. Where can this GitHub App be installed: **Only on this account**. Its **App ID** is at the top of its General page.
5. **Install App** on the account: all repositories, or the platform repository and every application repository.
6. **The key**, once the foundation apply has created the secret: in the App's settings, **Credentials**, **Key pairs**,
   **New key** (GitHub's name since 2026; it was "Generate a private key"). A `.pem` file downloads; this puts it into
   the secret and deletes the file, so the key never goes into the repository, Terraform or a chat:

   ```bash
   KEY=$(find ~/Downloads -maxdepth 1 -name '*.pem' -mmin -30 -print | head -1); if [ -n "$KEY" ]; then gcloud secrets versions add FAST_DEPLOY_GITHUB_APP_KEY --project PROJECT --data-file="$KEY" && rm "$KEY"; else echo "No new .pem file in Downloads yet"; fi
   ```

7. The configuration names the App and the secret (`scripts/install-machine.sh APP_ID` writes this into the machine's own):

   ```yaml
   github_app:
     app_id: "123456"
     private_key_secret: projects/PROJECT/secrets/FAST_DEPLOY_GITHUB_APP_KEY
   ```

A repository the App is not installed on can still be read (public data only); anything else there is refused with what
to install. The secret's name starts with an uppercase letter on purpose: application runtime identities may read the
secrets named `<service>-NAME`, and no service name can reach this one. `main` stays protected as before: the App merges
only what the required checks passed, and it never merges a contract (merging one is the review, D32).

On a laptop without the App, `GITHUB_TOKEN` may hold a fine-grained token with the same permissions (section 7g;
`scripts/start.sh` keeps it in the Keychain). It takes precedence over the App. A container can mount a key file instead
and set `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY_FILE`. Never return any of these to Claude or
reuse one as the MCP bearer token.

## 5. Host MCP and connect Claude

Start locally first as described in README. For Claude Chat, host the controller on a reachable **HTTPS** endpoint: the controller's own machine (section 7m) is the standard host since 1.17.0, and a laptop with the ngrok tunnel (`scripts/start.sh`) is the fallback. A localhost URL is not reachable by Claude's hosted connector.

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
- **Watchdog**: set `notify_webhook_url` (Slack or Discord) to receive the daily findings: an expiring GitHub credential, a paused or stale schedule, an unsupported platform pin, a newest release attempt that failed, a configuration file that could not be loaded, or, with `backup_bucket` set, a quarterly restore drill that is due (`python -m labdeploy.cli restore-drill`, `docs/RUNBOOK.md`). `python -m labdeploy.cli watchdog` runs it now.
- **Backups**: the database is copied nightly next to itself (`backup_dir` to change), fourteen copies kept, and, with `backup_bucket` set to the foundation's `backup_bucket` output (`PROJECT-lab-deploy-backups`), the copy and the configuration file go to that private versioned bucket as well, using the operator's own gcloud sign-in. `python -m labdeploy.cli backup` runs it now; `get_controller_status` shows the last run. Restoring is `docs/RUNBOOK.md`, "Restore the controller on another machine", rehearsed on 2026-10-12.
- **Is the controller up?** Set the repository variable `CONTROLLER_HEALTH_URL` on the platform repository to `https://YOUR_HOST/healthz`; its controller-watch workflow pings it every thirty minutes and fails loudly when it does not answer.

## 7m. The controller's own machine (Phase 15)

One Always Free `e2-micro` in the staging project runs the controller around the clock behind Caddy and Let's Encrypt,
with Litestream copying its database to the backup bucket (decision D36; the runbook's "The controller's machine" is
the day-to-day side). It costs the external IPv4 address, about $3.65 a month. No DNS name is needed: Claude reaches the
machine at that reserved address, and Let's Encrypt certifies the address itself (a free certificate six days long, which
Caddy renews every few days; the uptime check alerts if one ever runs out). A DNS name stays possible (decision D6) for
anyone who has one. The steps, once:

1. **The foundation apply** with the machine in `infra/gcp/project/terraform.tfvars`, then `scripts/apply-foundation.sh PROJECT`:

   ```hcl
   controller = {
     install_commit = "FULL-40-CHARACTER-COMMIT-ON-MAIN"  # what a fresh disk installs first
     acme_email     = "you@example.edu"                    # optional: Let's Encrypt's certificate notices
     # domain       = "deploy.lab.example.edu"             # optional: a DNS name instead of the address
   }
   ```

   It creates the machine, its own network and firewall (HTTPS and HTTP for Let's Encrypt; SSH only through
   Identity-Aware Proxy), the reserved address, the machine's identity `lab-deploy-controller`, the empty secret
   `FAST_DEPLOY_GITHUB_APP_KEY`, the uptime check on `/healthz` and its alert. `terraform output controller` (or the apply's
   output) shows the address and the connector's `url`.
2. **With a DNS name only**, its A record to that address, at the registrar; or set `dns_zone` to a Cloud DNS zone of the
   project and the apply writes it.
3. **The GitHub App** (section 4), with its key in that secret.
4. **The move**: stop the laptop's controller, then `scripts/install-machine.sh APP_ID` (add `--domain NAME` with a DNS
   name). It copies the database and the configuration rewritten for the machine through Identity-Aware Proxy, imports
   them, waits for `/healthz` to answer over HTTPS, records `machine_url` in `config.local.yaml` (so `start.sh` keeps the
   laptop off while the machine answers) and points `CONTROLLER_HEALTH_URL` at the machine.
5. **Claude**: remove the old connector and add the `url` from the `controller` output (`https://ADDRESS/mcp`), then sign
   in with your operator token.
6. **After a release, by itself** (optional, 1.20.4): add `platform_updates: {}` to the machine's configuration
   (`scripts/machine.sh config-get`, edit, `config-put`). After each merge into `main`, the machine then installs the new
   code once its checks have passed and an hour has gone by, and re-pins the applications' workflows an hour after that;
   `docs/RUNBOOK.md`, "After a release", says how to stop either. A machine created before 1.20.4 needs the foundation
   applied and `scripts/machine.sh startup` once first.

## 7n. Production (Phase 16)

Production is a second Google Cloud project with its own foundation, state, identities and budget (decision D37). It is
created under the organization from the start, because the tier for protected health information (Phase 17) needs it
there; staging stays where it is. A promotion will copy the exact image staging released, and every promotion waits for
a reviewer's approval on GitHub. The steps, once:

1. **The project**, under the organization and on the same billing account (this needs Project Creator on the
   organization and Billing Account User on the account):

   ```bash
   gcloud projects create PROD_PROJECT --organization ORG_ID --name "Fast Deploy production"
   gcloud billing projects link PROD_PROJECT --billing-account BILLING_ACCOUNT
   ```

2. **Its state bucket**, as in section 3:

   ```bash
   gcloud storage buckets create gs://PROD_PROJECT-tfstate --project PROD_PROJECT --location us-central1 --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets update gs://PROD_PROJECT-tfstate --versioning
   ```

3. **Each environment its own settings file.** Rename `infra/gcp/project/terraform.tfvars` to `staging.tfvars`: Terraform
   loads `terraform.tfvars` by itself, so staging's values would reach production, and `apply-foundation.sh` refuses
   production while one exists. Then write `infra/gcp/project/production.tfvars`:

   ```hcl
   project_id = "PROD_PROJECT"
   region     = "us-central1"

   image_vulnerability_scanning = false  # a promotion copies a digest staging already scanned
   monthly_budget_usd           = 20
   billing_account              = "XXXXXX-XXXXXX-XXXXXX"
   spending_cap                 = { enabled = true, threshold = 1.0 }
   alert_email                  = "lab-admin@example.edu"

   # Production's own pipeline identities, applied only from the environment infra-production.
   pipeline = {
     platform_repository    = "OWNER/fast-deploy"
     platform_repository_id = "NUMERIC_ID"
     platform_owner_id      = "NUMERIC_OWNER_ID"
     environment            = "infra-production"
   }

   # The controller's machine stays in staging and reads production with the observer's viewer roles.
   viewer_members = ["serviceAccount:lab-deploy-controller@STAGING_PROJECT.iam.gserviceaccount.com"]
   ```

   There is no `controller` block and no `billing_export_dataset`: the machine and the billing account's one export stay
   in staging. The spending cap closes production's services at the budget, as in staging; turn it off only when a
   production that stops is worse than the bill.
4. **The foundation**: `scripts/apply-foundation.sh PROD_PROJECT production`, typing **yes** at its prompt. It first switches
   on the two APIs a new project lacks (Cloud Resource Manager and Service Usage), and at the end it prints four
   `INFRA_PRODUCTION_*` values.
5. **On the platform repository**: set the repository variables `INFRA_PRODUCTION_PLAN_WIF_PROVIDER`,
   `INFRA_PRODUCTION_APPLY_WIF_PROVIDER`, `INFRA_PRODUCTION_PLAN_SERVICE_ACCOUNT` and
   `INFRA_PRODUCTION_APPLY_SERVICE_ACCOUNT` from that output; the production jobs stay dormant until they exist. Create
   the environment `infra-production` like `infra` (section 7f: deployments from `main` only, no administrator bypass)
   and add yourself as its required reviewer, leaving "Prevent self-review" off because you usually merge the change
   yourself. Every production apply then waits for your approval, so nothing applies to production unless a person
   agreed, whoever merged.
6. **The controller's configuration** (`scripts/machine.sh config-get FILE`, edit, then `scripts/machine.sh config-put FILE`):

   ```yaml
   production:
     project_id: PROD_PROJECT
     project_number: "123456789012"   # gcloud projects describe PROD_PROJECT --format 'value(projectNumber)'
     reviewers: [your-github-login]   # who approves every promotion, one to six GitHub accounts
   ```

7. **Each application**: ask Claude "add production for APP" (`add_production`), and again after each step:
   1. A pull request with the production root, `infra/gcp/apps-production/APP`, with its objective, its error-budget
      alerts and an uptime check every minute (docs/RUNBOOK.md, Production). Read its plan comment and merge it yourself;
      the controller never merges production infrastructure.
   2. Approve its apply in `infra-production` on GitHub; the controller waits until that apply has succeeded.
   3. The controller creates the environment `production-APP` on the platform repository, with your reviewers as
      required reviewers, self-review prevented, deployments from `main` only, and the eight target variables. It names the
      production project in the app's staging environment too (`PRODUCTION_PROJECT_ID`), so the weekly cost report shows
      production's spend. It also opens a second pull request on the app's staging root: read access for the production
      deployer to the images staging released and to the public half of their signing key, nothing else. Merge it too.
   4. Asked once more, it answers that production is ready.
8. **A promotion**: after a staging release you are happy with, ask Claude "promote APP to production"
   (`promote_to_production`; name a plan to promote an older staging release). The platform repository's **Promote to
   production** run starts and waits. Open it, read the approval block in its summary (the commit, the exact image by digest,
   the staging release it came from, the size and cost in production), then choose **Review deployments** and approve or
   reject it. Approved, it checks the image's provenance, copies it by digest into production's registry (never a rebuild),
   signs the promotion with production's key and releases it with the same runner as staging: a candidate with no traffic,
   the probes, then 10, 50 and 100 percent of the traffic, each step watched by the bake, restoring the previous version on a
   failure at any step. "Roll APP back in production" (`rollback_production`) undoes the live promotion, after an approval
   too.
9. **A name of the lab's own** (optional, about $25 a month per application): `domain:` in the contract, then the steps in
   docs/RUNBOOK.md, Production, "A custom domain". The first one needs production's foundation applied once more, and a
   budget that allows it.

At idle the production project costs a few cents a month (the roadmap's estimate is $0.06); its budget, alerts and
spending cap work as in staging.

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
- **Native spend cap**: Google's own cap for Cloud Run is the second layer under the spending cap above, and the one that
  needs nothing of this platform to work; set it up once per project as section 7i says.
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

How a change flows: `python -m labdeploy.cli infra render APP --pull-request` (or Claude's `propose_infrastructure`) opens the pull request; the plan job runs with the read-only identity and `-lock=false`, checks the plan with `python -m labdeploy.infra check` and posts one comment per application that it updates on every push; merging runs the apply job from the `infra` environment, which plans again with the lock, refuses any plan the policy rejects, applies, and publishes the outputs. Policies, all fail-closed and judged by the rules on `main` rather than the pull request's own copy: no service account keys; only roles from an allow-list of what an application needs (never owner, editor or an IAM-admin role); no `allUsers` or `allAuthenticatedUsers` anywhere; no authoritative IAM policies; no destruction of resources that hold data (buckets, databases, datasets, secrets, registries); nothing outside `us-central1`; databases private and delete-protected, buckets uniform and non-public, on creation and on every update; the `lab-deploy-app` label on every managed resource; no reference to the platform's own identities or the state bucket from an application root; and a value the plan cannot know before applying is refused rather than assumed. The comment on the pull request opens with an approval block the code renders from the plan, never from Claude's words: resources to add, change, destroy and replace, by type; a reversibility tier (`reversible`, `identity` for grants and identities, `data-bearing` for anything that holds data, the highest one for the whole plan); the root's idle floor per month before and after at list prices (a Cloud SQL instance, a provenance key); every change to who can reach the application (ingress, the invoker check, Identity-Aware Proxy, invoker and accessor grants); and the pull request, head commit and plan run it was rendered for. Every resource line and the full plan text follow it, then the policy verdict. A change to `modules/service` plans and applies every root. `workflow_dispatch` on the apply workflow applies named roots by hand, for example after the foundation first exists.

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
3. A GitHub credential that may write. The fine-grained token covers **all repositories** of the account (or the organization's) with these permissions: Contents read and write, Pull requests read and write, Workflows read and write, Actions read and write, Environments read and write, Variables read and write, Administration read and write (environments are created through it), Metadata read. Store it with `scripts/start.sh` as before. On the controller's machine the GitHub App (section 4, decision D14) replaces it since 1.17.0; this token stays the laptop's fallback.

What the tool does, in order: contract check (blocks with a checklist), registration, infrastructure pull request (merged once the plan workflow's policy verdict is `passed` and only when every file sits under `infra/gcp/apps/<service>/`), the apply, the `staging` environment with its variables, the workflows pull request (merged when GitHub reports it mergeable), the first release. Long waits end the call and the scheduler continues every minute; `get_onboarding_status` reports the step, and a blocked onboarding resumes from its step when the same tool is called again.

The operator's switches for an onboarded application (a cost ceiling, auto-deploy, a pause, viewers, infrastructure settings, `auto_merge`) go under `application_settings: <app id>:` in `config.local.yaml`; they are laid over the registered entry on the next request, with no restart and nothing copied (`config.example.yaml` shows the shape). `auto_merge: false` there keeps that one application's pull requests for a person even when `onboarding.auto_merge` is on, and `auto_merge: true` does the opposite.

## 7l. A second operator: who asks and who approves

With one operator, onboarding merges the pull requests it opened once the policy passes (decision D12), so the person who
asks in chat is also the one who approves. Once a second operator exists, separate the two (decision D29):

1. Give the new person their own identity (`docs/RUNBOOK.md`, "Succession"); never share a token.
2. On GitHub, give them write access to the platform repository, and in a pull request add their account next to the
   owner in `.github/CODEOWNERS` (at least on `/infra/gcp/apps/**`, which the controller writes on people's behalf).
3. In the branch protection of `main` (Settings, Branches): **Require a pull request before merging**, **Require
   approvals** (one), **Require review from Code Owners**, and keep administrators included. On the `infra` environment,
   add both people as required reviewers. Turn these on only when there are two people: GitHub never lets the author of a
   pull request approve it, and the controller's pull requests are authored by its GitHub credential's account.
4. In the controller configuration, set `may_merge_own_requests: false` on each person's principal:

   ```yaml
   principals:
     - name: brian
       token_sha256: ...
       applications: ["*"]
       can_deploy: true
       may_merge_own_requests: false
   ```

From then on onboarding still opens the pull requests and checks the plan's policy verdict, then waits at its merging step:
`get_onboarding_status` says "waiting for another operator to review and merge PR #N"; when someone merges it, the
onboarding continues within a minute. What GitHub cannot see is who asked in chat (every pull request the controller
opens carries the credential's account), so the last half of the rule is the operators' agreement: the person who asked
does not approve. A GitHub App or a bot account as the controller's credential (decision D14) keeps both people able to
approve. `onboarding.auto_merge: false` keeps every merge for a person, whoever asked.

**Before onboarding** (since 1.11.0): `check_repository` reads a repository against the contract without creating anything, and `propose_configuration` with a repository link (and `path`) opens one draft pull request with the files it lacks, for a person to review and merge. Both need the onboarding identity (`applications: ["*"]`). The checker compiles `requirements.lock` with uv in a container of the app's own Python on the controller's machine, so Docker must be running there; without it the pull request names the command to run instead.

**A repository with several services** (since 1.10.0; `docs/DEVELOPER-CONTRACT.md` section 4a). Give `onboard_application` the service's folder as `path`, one call per service. The registry records the folder; the first service of a repository keeps `deploy-gcp.yml` and the `staging` environment, and each further one gets `deploy-gcp-<service>.yml`, `recover-gcp-<service>.yml`, `rightsize-gcp-<service>.yml` and the environment `staging-<service>`, created by the onboarding with its own variables. Its root says so (`github_environment`, `workflow_files`), and its deploy identity's federation admits only those workflows in that environment, so one service's workflow cannot release another. `lab-deploy decommission` names the service's own files and keeps the repository's access while other services still deploy from it. A service onboarded before 1.10.0 keeps its root folder: to move it into a folder, unregister it (`lab-deploy unregister APP`), move its files in a pull request, and onboard it again with `path` (its cloud resources are kept, since the names do not change).

**Postgres by password** (`needs.database_auth: password`, since 1.10.0) needs the foundation applied with 1.10.0: the apply identity gets the custom role `labDeploySecretWriter` (create and describe secrets and add versions, never read a value) and the plan identity `roles/secretmanager.viewer`. The root generates the password in each run with an ephemeral `random_password` and the module writes it through write-only arguments to the database user `app` and to the secret `<service>-DATABASE_URL`, once; neither the state nor a plan holds it. The foundation's lock file pins the `random` provider for every root that copies it. To rotate the password, raise `local.password_version` in `infra/gcp/modules/service/needs.tf` (1 today) and let the pipeline apply: the user and the secret get the same new password in one run, and the next release picks up the new version. If the user is ever created again, the secret is written again in the same run.

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

## 7i. Google's native spend cap for Cloud Run

Since 2026-10-12 Cloud Billing can cap a budget instead of only alerting: a monthly budget scoped to one project and one
eligible service (Cloud Run is one) that, once the month's gross estimated cost passes the amount, pauses every Cloud Run
workload in the project. Services answer 5xx, jobs stop, in-flight requests finish; nothing is deleted. It does not stop
Cloud SQL, buckets or topics ("persistent resources keep running"), which is why the platform's own spending cap stays; the
native cap is the layer that still works if the controller, the workflow or the budget notification does not. It acts on
estimates within hours, not instantly; overage is billed. It lifts by itself on the first of the next month, or by hand
in Cloud Billing (up to an hour to recover); while it is enforced the budget cannot be edited.

1. Pick the amount: the same as `monthly_budget_usd` is the default (decision D21).
2. Create it, either with the script, which shows the request and waits for a typed `yes`, from an account that may manage
   budgets on the project's billing account (Billing Account Administrator, or Costs Manager with Editor on the project):

   ```bash
   scripts/spend-cap.sh create PROJECT 20
   ```

   or in the console: Billing, Budgets & alerts, Create budget, **Spend cap enforcement**, the project, the Cloud Run
   service, the amount, Finish. Terraform cannot do it yet: the Google provider has no spend-cap argument.
3. Check it: `scripts/spend-cap.sh check PROJECT` lists the caps on the project's Cloud Run. `scripts/spend-cap.sh amount
   PROJECT N` changes the amount (and re-arms a cap lifted this month); `scripts/spend-cap.sh lift PROJECT` lifts an
   enforced cap. Both wait for a typed `yes`.
4. Record it in the controller configuration so plans, `estimate_cost`, `get_cost_report` and `get_controller_status`
   name it (until then they carry the warning `no_native_spend_cap`):

   ```yaml
   cloud_run_spend_caps:
     PROJECT: 20
   ```

## 7j. The bake after promotion

Every release watches the new revision's own Cloud Run logs for five minutes after traffic moves to it, and restores the
previous traffic split on a crash, on more than 5% 5xx answers among at least 20 requests, or on a p95 latency more than
three times the previous revisions' (decision D22; `labdeploy/bake.py`). The health path is not counted. The deployer's
Logging viewer role, which every application root grants, is all it needs. Per application, the operator sets
`bake_seconds` (0 to 900) under `applications.<id>` or `application_settings.<id>`; 0 turns it off, which every plan then
says. A staging app with no users sees no traffic in five minutes, so its bake is `inconclusive` and passes; to judge a
release, send it traffic during the bake.

## 7k. The weekly drift check

`.github/workflows/infra-drift.yml` plans every application root against the live infrastructure on Mondays at 07:37 UTC
with the read-only plan identity, without the state lock, and reports each difference from the code on `main`: an edit
made outside the pipeline, or an apply that did not finish. It needs only the foundation applied with 1.9.0 (the plan
identity admits the drift workflow from `main`); the `INFRA_*` variables of section 7f already exist. It keeps one issue
on the platform repository, **Fast Deploy: infrastructure drift**, open while something differs, and uploads the
`drift-report` artifact the controller reads: `get_controller_status` shows it, and the daily watchdog notifies on drift in
IAM, networking or exposure, on a root whose plan failed, and when the report is more than eight days old. To put the
code's version back, merge any change to the root or dispatch **Infrastructure apply** with the root's name. Run it by
hand from the Actions tab (Run workflow, on `main`). A grant someone adds next to the platform's own is invisible to it:
Terraform's member resources check only their own members.

## 7o. The weekly pin refresh

`.github/workflows/pins.yml` (since 1.20.5) proposes, every Monday at 05:23 UTC, the new releases of the pins Dependabot
does not follow, in one pull request; `docs/RUNBOOK.md` ("The weekly pin refresh") says what it follows and how to review
it. Its first job reads the official release metadata with the workflow's read-only token and needs nothing set up. The
pull request needs a small GitHub App of its own: a pull request opened with the workflow's own token starts no CI until
someone approves the run, that token may not change workflow files, and this repository does not let Actions create pull
requests. Until the App exists, each run leaves its proposal in the run's summary and says so with a warning.

Create the App once, on the account that owns the platform repository, with GitHub's form filled in through its URL (no
webhook, only on this account, and exactly the permissions it uses):

```text
https://github.com/settings/apps/new?name=fast-deploy-pins-YOUR-ACCOUNT&url=https%3A%2F%2Fgithub.com%2FOWNER%2Ffast-deploy&public=false&webhook_active=false&contents=write&metadata=read&pull_requests=write&workflows=write
```

1. Check the page and click **Create GitHub App**: **Contents**, **Pull requests** and **Workflows** read and write,
   **Metadata** read-only, nothing else. Its **Client ID** is on its General page (the workflow takes the Client ID, not
   the App ID).
2. **Install App**, on the platform repository only.
3. In the App's settings, **Credentials**, **Key pairs**, **New key**: a `.pem` file downloads.
4. Give the workflow the key in an environment only `main` can use (no required reviewers: the run is scheduled), and
   the Client ID as a repository variable (it is not a secret, and a job's condition reads it before the job starts).
   The third command deletes the key file once the secret holds it:

   ```bash
   gh api -X PUT repos/OWNER/fast-deploy/environments/pins -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
   ```

   ```bash
   gh api -X POST repos/OWNER/fast-deploy/environments/pins/deployment-branch-policies -f name=main -f type=branch
   ```

   ```bash
   KEY=$(find ~/Downloads -maxdepth 1 -name 'fast-deploy-pins*.pem' -mmin -30 -print | head -1); if [ -n "$KEY" ]; then gh secret set PINS_APP_PRIVATE_KEY --env pins --repo OWNER/fast-deploy < "$KEY" && rm "$KEY"; else echo "No new .pem file in Downloads yet"; fi
   ```

   ```bash
   gh variable set PINS_APP_CLIENT_ID --repo OWNER/fast-deploy --body CLIENT_ID
   ```

5. Run it once by hand to see it work: `gh workflow run pins.yml --repo OWNER/fast-deploy` (or Actions, **Weekly pin
   refresh**, **Run workflow** on `main`). Its pull request is authored by the App (`fast-deploy-pins-YOUR-ACCOUNT[bot]`),
   CI runs on it as on yours, and only you merge it.

What it costs: nothing on a public repository, where GitHub-hosted runner minutes are free: about 3 runner minutes a
week for the job, plus the CI its pull request starts (about 8). On a private repository that would be about 2% of GitHub
Free's 2,000 included minutes. It creates no cloud resource and never signs in to Google Cloud.

## 8. Provenance, scanning and dependency updates

- **Build provenance**: the deployment job signs SLSA provenance for the pushed image digest and verifies it before release; the release deploys only that verified digest, and a reused image is verified again. Which signer depends on the repository:
  - **Public repository**: GitHub's artifact attestation (`actions/attest-build-provenance`, checked with `gh attestation verify`). Nothing to configure.
  - **Private repository** (GitHub attests these only on Enterprise Cloud): Cosign signs with the application's own Cloud KMS key, `<service>-provenance` in the foundation's `lab-deploy` key ring (EC P-256, software protection, about USD 0.06 a month per application). Only the application's deployer may use the key, and the deployer is reachable only from its deploy workflows on `main` in the `staging` environment. The signature is stored next to the image as its `sha256-<digest>.att` tag, which the registry keeps; nothing goes to a public transparency log, which would publish the private repository's name and commits. Verification checks the signature with the key, then that the statement names this image digest, repository, commit and workflow. Setup: apply the foundation once (it enables Cloud KMS and creates the key ring), then let the pipeline apply each application's root (it creates the key). Until then a private repository's release stops at signing and says so.
  - **The operator's exception**: `provenance: unavailable` under `applications.<id>` or `application_settings.<id>` in the controller configuration releases without signed provenance, for staging or an emergency only. The plan warns, the run shows a warning, the receipt records `provenance: unavailable`, the image is always built fresh, and the workflow refuses the exception together with a reused image. Developers cannot set it: `platform.yaml` has no such field. Remove it once the cause is fixed.
  - If a verification step fails, it fails closed by design: confirm the runner can read the private registry through `docker-credential-gcloud`, and for a private repository that the key exists (`gcloud kms keys list --keyring lab-deploy --location us-central1 --project PROJECT`).
- **Dockerfile lint**: hadolint 2.15.1 checks each application's Dockerfile in the build job before the image is built.
  Findings are annotations on the run, a line in its summary and `dockerfile_lint` in the release receipt; they stop a
  release when they are error-level (since 1.10.0; warnings never do). Set the application repository's variable
  `HADOLINT_BLOCKING` to `false` to keep them as annotations only for that repository.
- **Image scanning**: Terraform enables Artifact Analysis (`image_vulnerability_scanning`, about USD 0.26 per new image digest), often more than a small staging app's compute for the month; it is a per-project cost choice, and the estimate names it. The deployment job writes `vulnerability-summary.json` into the release receipt and the job summary. To block releases with critical findings, set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES` to `true`; an unfinished scan then also blocks.
- **Tool descriptions are pinned**: what Claude reads about each MCP tool steers what it does, so `schema/tools.sha256.json`
  holds a SHA-256 of every tool's name, title, description, schemas and safety hints, and a test fails when one differs.
  After a deliberate change, run `python -m labdeploy.cli tools-checksum --write` and commit the reviewed diff with the
  change; an upgrade of the MCP SDK or pydantic that changes a schema shows up the same way.
- **Pins Dependabot does not follow** (the pipeline's tools, the base and buildpacks images, uv and npm for the lock containers, CI's hadolint and OpenTofu, the machine's Caddy and Litestream): the weekly pin refresh (section 7o) proposes each release once it is seven days old, with its checksum from official release metadata, in one pull request a person merges.
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
- **Failed bake:** the new revision served all traffic, then crashed or answered too many requests with 5xx (or became
  much slower), so the release restored the previous traffic split. The status and the receipt carry the bake's numbers
  and the new revision's last log lines; fix the application and release again.
- **Rollback failure or terminated runner:** inspect the independent recovery workflow and the GCS checkpoint. The next recovery run attempts to restore the previous traffic map. If the API is unavailable, the target changed, or outside traffic changes are detected, recovery fails visibly for operator review. Use an administrator-approved recovery command if necessary.
- **Controller restart while dispatching:** status lookup locates the run by its unique title; a record still `dispatching` or `dispatch_unknown` two minutes later with no visible run becomes `dispatch_lost`, a final state whose advice is to create a new plan. Auto-deploy retries a lost or errored attempt after thirty minutes. This MVP does not implement an exactly-once distributed job queue.

Before adopting for production, add an external rollout controller/monitor, enterprise identity, hardened image supply-chain verification, organization-specific policies, and recovery exercises.

