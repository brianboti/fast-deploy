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

All three workflows must use the same pinned platform commit. Deployment and recovery share a concurrency group; right-sizing is read-only and queues separately. The generator refuses placeholder SHAs and will not overwrite files. If the platform repository is private, add `PLATFORM_READ_TOKEN` as a read-only Contents credential scoped only to that repository. GitHub's app-repository token cannot read a different private repository. An organization-managed GitHub App installation token can replace the development token. Do not use this credential for cloud access.

Create a GitHub environment named **staging**. Restrict deployment branches to `main`. Add a reviewer during initial integration if supported by your GitHub plan; routine staging releases can run without manual approval after validation. No production environment is used by this implementation.

## 3. Bootstrap GCP once

Prerequisites: Google Cloud CLI, Terraform 1.6 or newer, an existing GCP project with billing, and an administrator identity permitted to create the specific resources in `infra/gcp`. The app deployer will not receive those administrator permissions.

Create a dedicated GCS state bucket before `terraform init`. Use uniform bucket-level access, public-access prevention, versioning, and narrowly scoped state access. The bucket must already exist because Terraform initializes its backend before creating resources. State and saved plans must stay out of Git. GCS backend state locking is supported.

Obtain numeric GitHub owner and repository IDs from GitHub's repository API. Names alone can be reassigned; the trust policy checks both immutable IDs and the expected repository name.

Provide an approved, working bootstrap HTTP image, pinned as `...@sha256:...`. It must listen on 8080 and be pullable by the Cloud Run service agent. For example, build the included demo app and push it to a reviewed registry using an administrator's normal bootstrap procedure. The bootstrap service establishes a rollback target before the first automated release. The platform intentionally does not auto-pick an arbitrary public bootstrap image.

Create `infra/gcp/terraform.tfvars` locally:

```hcl
project_id           = "YOUR_STAGING_PROJECT"
region               = "us-central1"
service              = "lab-demo"
github_repository    = "YOUR_ORG/YOUR_APP"
github_repository_id = "NUMERIC_REPOSITORY_ID"
github_owner_id      = "NUMERIC_OWNER_ID"
bootstrap_image      = "APPROVED_IMAGE@sha256:REAL_DIGEST"

# Optional cost controls; see section 7.
# monthly_budget_usd       = 50
# billing_account          = "XXXXXX-XXXXXX-XXXXXX"
# budget_alert_email       = "lab-admin@example.edu"
# registry_cleanup_dry_run = false
```

Then, using your administrator login:

```bash
gcloud auth application-default login
cd infra/gcp
terraform init -backend-config="bucket=YOUR_STATE_BUCKET" -backend-config="prefix=lab-demo/staging"
terraform plan -out=bootstrap.tfplan
terraform show bootstrap.tfplan
terraform apply bootstrap.tfplan
terraform output -json github_environment_variables
```

Inspect the concrete plan before applying. It creates billable resources; no apply was run while developing this archive. Keep `.terraform.lock.hcl` in Git. Never grant the MCP server GCP administrator access.

Terraform owns the foundation and IAM. The workflow owns the Cloud Run revision template and traffic after bootstrap. `ignore_changes` makes that ownership explicit; change workload settings through reviewed app config. Database schemas and data are outside this release's rollback contract.

Copy the Terraform output values into **GitHub Settings → Environments → staging → Environment variables**:

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

Minimum repository permissions for analyze/plan/dispatch/status: **Contents read**, **Actions write**, and metadata read. If enabling `propose_configuration`, additionally allow **Contents write** and **Pull requests write**. Branch protection must prevent the App from bypassing review on `main`; the proposal tool only creates a new branch and draft PR.

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

To end sessions, replace the operator's `token_sha256` and restart (every session signed in with that token ends), or call `POST /revoke` with the refresh token. Lower `oauth.access_token_seconds` or `oauth.refresh_token_days` to shorten lifetimes. Sign-in with the university identity provider is not implemented; the sign-in page is where it would plug in.

Anthropic's connector permission prompts are additional user-interface controls. The server's identity/app allowlists and GitHub/GCP permissions enforce the actual access boundary.

## 6. First release

1. With dispatch disabled, ask Claude to list applications, analyze the registered app, and create a deployment plan. Confirm the repository, full SHA, resource defaults, and checks still marked pending.
2. For a new app with no `platform.yaml`, enable writes after the GitHub configuration is ready and ask it to propose configuration. Inspect the draft PR, fix assumptions, mark the config reviewed, and merge normally. Do not mark an unknown app reviewed just to pass validation.
3. After the staging environment and Terraform bootstrap are verified, set `live_dispatch: true` and restart the controller.
4. Ask: **"Create a plan for demo on GCP staging and deploy that plan."**
5. Check the returned workflow status. `dispatched` means GitHub accepted the job; only the workflow's actual conclusion and release receipt indicate the result.
6. Download the workflow's `release-receipt` artifact to inspect the image digest, previous traffic split, revision, and result. If the runner disappeared before artifact upload, the checkpoint remains at `gs://RECOVERY_BUCKET/releases/SERVICE.json`; the independent workflow emits a `recovery-receipt` artifact.

The release creates a candidate revision, probes its tagged URL with an audience-bound identity token, promotes it, and probes the service URL. On failure it restores the prior concrete revisions. It reports `rollback_failed_manual_recovery_required` if that restoration fails. Cleanup removes the candidate tag on both success and failure. It does not claim that restored traffic guarantees healthy downstream data or dependencies.

## 7. Cost controls and right-sizing

- **Before deploying**, ask Claude to run `estimate_cost` for the application, optionally with expected monthly requests and average request duration. The reply gives the zero-traffic floor, the worst-case compute ceiling and the findings. Set `cost_ceiling_usd` for each application in `config.local.yaml` to refuse plans whose worst case is too high.
- **After the first week of traffic**, the right-sizing workflow (weekly, or `workflow_dispatch` with a `window_days` input) uploads `cost-report.json`. Ask Claude for `get_cost_report`. Apply accepted proposals by editing `platform.yaml` through a reviewed pull request; nothing changes on its own.
- **Image cleanup** starts in dry-run mode. Review the Artifact Registry audit logs for what would be deleted, then set `registry_cleanup_dry_run = false`. The newest `image_versions_to_keep` versions are always kept as rollback targets.
- **Budget alert**: set `monthly_budget_usd` and `billing_account` (and optionally `budget_alert_email`) in `terraform.tfvars`. The administrator identity needs Billing Account Costs Manager or equivalent on that billing account. If `terraform apply` answers 403 for the budget, add `user_project_override = true` and `billing_project = var.project_id` to the provider block so the Billing Budgets API is called with a quota project. Budgets notify at 50%, 90% and 100% of actual spend and at 100% forecast spend; they do not stop spending.
- **Keep the cheap defaults**: `scaling.minimum: 0`, 512 MiB, one vCPU, authenticated access, Tier 1 region. Raise anything only with a measurement from the report.

## 8. Provenance, scanning and dependency updates

- **Build provenance**: the deployment job signs an attestation for the pushed image digest and verifies it with `gh attestation verify` before release. Nothing to configure. If the verification step fails in the first trial, confirm the runner can read the private registry through `docker-credential-gcloud`; the step fails closed by design.
- **Image scanning**: Terraform enables Artifact Analysis (`image_vulnerability_scanning`, about USD 0.26 per new image digest). The deployment job writes `vulnerability-summary.json` into the release receipt and the job summary. To block releases with critical findings, set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES` to `true`; an unfinished scan then also blocks.
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
- **Controller restart while dispatching:** the persisted record remains in `dispatching`; status lookup can locate a run by its unique title. An operator must reconcile an ambiguous result. This MVP does not implement an exactly-once distributed job queue.

Before adopting for production, add an external rollout controller/monitor, enterprise identity, hardened image supply-chain verification, organization-specific policies, and recovery exercises.

