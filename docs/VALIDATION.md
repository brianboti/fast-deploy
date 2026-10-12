# Validation record

Local validation date: 2026-10-12 UTC.

## Completed

- Controller/schema/analyzer/GitHub adapter/REST/MCP/release tests: 58 passed.
- Separate demo app test: 1 passed.
- MCP tests exercise real SDK JSON-RPC initialization, tool listing and a deployment-plan tool call through the ASGI application.
- Controller tests include concurrent duplicate requests, stale commit, expired plan, read-only identity, per-identity plan access, uncertain network outcome, disabled dispatch and unsupported targets/settings.
- Release tests cover successful promotion, candidate failure, post-promotion failure, exact prior traffic restoration, rollback failure and rejection of mutable image references.
- GitHub adapter tests validate the dispatch payload and draft create-only PR behavior using HTTP mocks. They do not call a real app repository.
- CLI example analysis and policy validation passed.
- Python bytecode compilation passed.
- `actionlint` passes for deployment, recovery, and platform CI with one explicit compatibility exclusion: the installed linter does not recognize GitHub's newly documented `concurrency.queue` field. Tests separately require `queue: max` in both templates; all other lint diagnostics remain enabled.
- Terraform formatting passed; backend-free initialization downloaded the signed Google provider and wrote its lockfile.

## Not completed here

Full `terraform validate` could not start the Google provider because this execution environment prohibits the provider's local Unix socket (`socket: operation not permitted`). This is an environment limitation; it is not a successful validation result. Run this on an unrestricted development machine:

```bash
cd infra/gcp
terraform init -backend=false
terraform validate
```

No cloud plan/apply, actual GitHub workflow run, Docker build, image vulnerability audit, hosted Claude connector handshake, or live Cloud Run health/rollback test was executed. Docker and gcloud are not installed in this environment, and the target repository/project/credentials were not supplied.

One dependency deprecation warning appears in the local FastAPI/Starlette TestClient path about a future HTTP client migration; tests still pass. It does not validate live network behavior.

Before the first real release, perform the disposable staging integration described in SETUP.md, including an intentionally unhealthy revision and verification that traffic returns to the earlier revision. Unit tests are evidence about program behavior, not proof of cloud integration.


## Regression checks added in 0.1.1

Status success/failure/cancellation and missing evidence; cloud-target drift rejection; current workflow-attempt identity; generation-checked journal writes; graceful interruption; simulated abrupt loss after traffic promotion followed by independent recovery; failed checkpoint preventing mutation; unrelated traffic/target rejection; and shared workflow queue configuration.

To reproduce the linter compatibility check:

```bash
actionlint -shellcheck='' -ignore 'unexpected key "queue" for "concurrency" section' templates/deploy-gcp.yml templates/recover-gcp.yml .github/workflows/ci.yml
```

The recovery interruption test simulates abrupt process loss and reconstructs recovery from the saved journal. It does not replace a real killed-runner integration test against GCP. No new live-cloud claims are made by this patch.


## 0.1.2 validation

Local validation date: 2026-10-11 America/Chicago, macOS, Python 3.12.12, hash-locked dependencies.

- Platform tests: 72 passed (58 existing plus 14 added for demo mode, plan listing, audit events, bearer scheme handling, host validation and CLI error reporting). Demo app test: 1 passed.
- Both Dockerfiles build with the pinned `python:3.12-slim` digest. The controller image runs as uid 65532 and answered public, unauthenticated and authenticated requests correctly from a container with a mounted config and data directory.
- `python -m labdeploy.demo serve` plus `walkthrough` completed all twelve steps: health, 401 without a token, MCP handshake with instructions, tool listing, list, analyze, plan, deploy (`dispatch: disabled`), status with events, `list_plans`, and two policy refusals.
- Live server probes: public `/healthz`; 401 for a missing or wrong token; 421 for a spoofed Host header; `/docs` disabled.
- All six pinned action commits were confirmed to exist in their upstream repositories through the GitHub API. GitHub lists `2026-03-10` as a supported REST API version, and `concurrency.queue: max` is documented (GitHub changelog, 2026-05-07).

Still not performed: `terraform validate`, `actionlint`, any real GitHub workflow run, and any real Cloud Run deployment or recovery.


## 0.2.0 validation

Local validation date: 2026-10-11 America/Chicago, macOS, Python 3.12.12, hash-locked dependencies.

- Platform tests: 94 passed (72 existing plus 22 for the cost model, telemetry queries, ceiling policy, cost tools and routes, artifact download, and the right-sizing template). Demo app test: 1 passed.
- Cost model checks are exact against the recorded list prices: a 1 vCPU / 512 MiB / 2-instance service bounds at $132.71 per month worst case and $0 idle; one warm instance costs $9.86 per month idle; 100,000 requests of 0.2 s cost between $0.05 and $0.55 per month before the free tier.
- Telemetry queries are verified against mocked Monitoring API responses: scoped filters, the aligner and reducer per metric kind, whole-window alignment for totals and percentiles, 5-minute alignment for instance counts, pagination, and `null` for missing data.
- The GitHub artifact fetch is verified with a mocked 302 redirect: the GitHub credential is sent only to `api.github.com`, never to the signed download host; oversized artifacts are refused.
- `terraform fmt -check`, `terraform init -backend=false` with the pinned Google provider 7.46.1, and `terraform validate` all passed inside the official `hashicorp/terraform:1.13.5` container. This completes the provider-schema validation that earlier releases could not run.
- The demo walkthrough runs fifteen steps including `estimate_cost` with and without a traffic scenario and the honest `get_cost_report` refusal in demo mode.

Still not performed: a live right-sizing run against a real Cloud Run service (the Monitoring queries, the recommender listing and the artifact round-trip are mocked), `terraform plan` or `apply`, and any real deployment. Prices in `labdeploy/cost.py` must be re-verified against the Cloud Run pricing page when upgrading.


## 0.3.0 validation

Local validation date: 2026-10-12 America/Chicago, macOS, Python 3.12.12, hash-locked dependencies.

- Platform tests: 106 passed (94 existing plus 12 for rate limiting, safety hints and structured output, the startup probe arguments, the scan summary, the deployment template's provenance and scan steps, and template pin consistency). Demo app test: 1 passed.
- `ruff check` is clean with the configured rule set; `zizmor` reports no findings on the three templates and the CI workflow; `pip-audit --require-hashes` finds no known vulnerabilities in the platform lock; the regenerated dev lock changed only by adding ruff and zizmor.
- `terraform fmt -check` and `terraform validate` pass with the scanning switch and reader role added, inside the official `hashicorp/terraform:1.13.5` container.
- The MCP SDK in use (1.30.0) accepts the annotations and emits `outputSchema` and `structuredContent`; verified through the real JSON-RPC path in tests.

Still not performed: a live run of the attestation, verification and scan steps against a real registry (those steps use official actions and gcloud commands but have not executed here), and everything listed as not performed for 0.2.0.


## Live exercise of 0.5.0 to 0.6.2 (2026-10-12)

Environment as in the staging trial below, with the app repository pinned to each platform commit through the pull requests opened by the new workflow-update command (merged by the operator's GitHub CLI session). Every call went through the controller's own tool interface, as Claude makes it.

| Scenario | Run | Observed |
|---|---|---|
| Healthy release | 38270039237 | `succeeded`; 40 timeline steps with timing; phases preparing, deploying, promoting, succeeded in 27 s; receipt and vulnerability summary stored; revision labelled with plan and commit; `release-<plan>` and `serving-<revision>` tags present; the notify job ran and stayed quiet without a webhook secret. |
| Traffic rollback | 38271691497 | `rollback_staging` dispatched the recovery workflow with the rollback input; 70 s later traffic was back on the previous revision, the checkpoint said `rolled_back`, Cloud Run agreed, and the rolled-back release stopped counting as live. |
| Deploy an older commit | 38271915895 | `create_deployment_plan` with `commit_sha` for a commit two behind main: history check passed in the controller and in the workflow; `succeeded`. |
| Redeploy a released commit | 38272860779 | Build job skipped, provenance verified for the existing digest, release in about 90 s instead of about eight minutes. |
| Cancel during the build | 38273142134 | `cancelled`; status named the recovery run that followed (`no_recovery_needed`) and said nothing had changed. |
| Commit that breaks its own tests | 38273468357 | `failed` at "Run unit tests" with the matching next action; traffic untouched. |
| Configuration from `platform.yaml` (0.8.0) | 38279769779 | `env` value and `timeout_seconds: 120` shown in the plan; the revision Cloud Run created carries the variable and the 120 s timeout. |
| Living cost-report issue (0.7.1) | 38278826507 | The right-sizing run created brianboti/lab-demo-app#6 with the summary; `propose_rightsizing` correctly reported nothing to propose. |
| Candidate that crashes only inside Cloud Run | 38273603111 | `failed` at the release step; receipt `traffic_restored` with the gcloud error line and phases preparing, deploying, traffic_restored in 21 s; service reported not ready with the explanatory note; the recovery run that followed found nothing to do; traffic stayed on the healthy revision. |

Findings that changed the code, each fixed with a regression test and re-verified live:

1. A plan pinned to an older commit failed the workflow identity check, because GitHub reports the run's head as the branch tip (0.6.1).
2. Image reuse ignored a rolled-back release's image (0.6.1).
3. Provenance verification failed when the build was skipped, because the registry login lived in the push step (0.6.1).
4. The deployer's writer role cannot delete stale serving tags; a custom role is added in Terraform and the reconcile reports instead of failing (0.6.1).
5. A reused image's release was reported as `completed_unverified` (0.6.2).
6. The audit trail recorded a final outcome twice (0.6.2).

Pending the operator's Terraform apply: candidate logs in the receipt (`roles/logging.viewer`), stale serving-tag removal (custom role), the uptime check, the alert policies and the dashboard. Not performed: a notification delivery (no webhook secret yet) and an alert firing.

## Live staging trial (2026-10-12)

Environment: a new Google Cloud project `lab-deploy-staging-f29a` linked to billing with a USD 20 monthly budget alert; the platform repository `brianboti/lab-deploy` and the application repository `brianboti/lab-demo-app`, both public; Terraform 1.13.5 in the official container with the pinned Google provider 7.46.1 and a versioned GCS state bucket; a GitHub `staging` environment restricted to `main` holding the eight Terraform outputs; and the controller on a workstation with a fine-grained token (Contents read, Actions write) and `live_dispatch: true`. Every step below ran against the real GitHub and Google Cloud APIs. Google's public Cloud Run sample image, pinned by digest, served as the bootstrap image.

Bootstrap: `terraform plan` showed 29 resources to add and `terraform apply` created all of them, including the budget. The bootstrap service was Ready, routed 100% to a concrete revision, scaled to zero, and answered anonymous requests with 403.

Findings that changed the code, each fixed, covered by a regression test, and re-verified live:

1. **Identity token subject format.** GitHub's OIDC `sub` claim is now `repo:OWNER@ID/NAME@ID:environment:ENV`; the trust condition expected the older `repo:OWNER/NAME:environment:ENV` and rejected every token. A temporary diagnostic workflow printed the claims; the condition now matches the dedicated `environment` claim together with the numeric IDs, branch and workflow paths. Applied as an in-place update of the provider.
2. **Status immediately after dispatch.** GitHub returns the run id before the run's title and commit are populated, so the identity check raised for about a second. A run that has not started is now reported as not visible yet; a started or finished run must still prove its identity.
3. **Traffic commands and readiness.** When a candidate fails its startup probe, `gcloud run services update-traffic` applies the requested split and still exits non-zero because the service's newest revision is not Ready. Trusting the exit code reported a successful rollback as failed and made every recovery run fail the same way. Traffic and tag commands now verify the routing state.
4. **Newest revision cannot be deleted.** Cloud Run refuses to delete a service's newest revision, so a failed candidate cannot be retired; it stays with no traffic and the service reports not Ready until the next successful release. Receipts record `service_ready` and say so.
5. **Receipt error text.** Receipts now carry the command's ERROR line rather than gcloud's trailing troubleshooting hint.

| Scenario | GitHub run | Observed |
|---|---|---|
| First release attempt | 38209448641 | Build job passed (tests, dependency audit, image). Deploy job failed at federated authentication (finding 1). Controller reported `failed` with tests, dependency scan and target `passed`, health `not_run`. |
| Release after the trust-condition fix | 38209818454 | Every deploy step passed: validation, authentication, recovery check, push and digest, provenance attestation, attestation verification, vulnerability summary, candidate deploy, probe, promotion, final probe. Receipt `succeeded` with the previous split recorded. Traffic 100% on the new revision, candidate tag removed, startup probe configured from `health_path`. An IAM identity-token probe of `/health` returned 200. GCS checkpoint `succeeded`. GitHub holds one SLSA v1 provenance attestation for the exact digest, signed by `deploy-gcp.yml@refs/heads/main` at the released commit. Scan `FINISHED_SUCCESS`: 0 critical, 5 high (none fixable), 6 medium. The automatic recovery run that followed reported no recovery needed. |
| Unhealthy candidate, original code | 38210212231 | The candidate never passed its startup probe; traffic never left the previous revision and the service kept answering. Controller: `failed`, health `release_failed_health_unconfirmed`, recovery hint set. The receipt wrongly said `rollback_failed_manual_recovery_required` (finding 3) and the automatic recovery run failed for the same reason. |
| Recovery with the fixed code, manual dispatch | 38211014088 | Checkpoint moved to `recovered`; traffic verified unchanged on the good revision. |
| Unhealthy candidate, fixed code | 38211230331 | Receipt `traffic_restored` with the command error recorded, no restore error, `service_ready: false` with the explanatory note; checkpoint `traffic_restored`; automatic recovery: no recovery needed. |
| Healthy release after the failure | 38211728097 | `succeeded`; the service reported Ready again with 100% on the new revision. |
| Right-sizing | 38210931979 | Fourteen-day window: 6 requests, 63 vCPU-seconds, 32 GiB-seconds, p95 CPU 5%, p95 memory 14%, at most 2 instances; USD 0.0034 per month at list price; no proposals because the service already runs at the smallest allowed size; Google's Cloud Run cost recommender returned none. The controller fetched the report through the GitHub artifact API (`get_cost_report`). |
| Cancellation during promotion | 38212354902 | The cancel request was submitted one second after the checkpoint reached `promoting`, but promotion and the final probe completed three seconds later, before the runner stopped the step. GitHub concluded `cancelled`; the controller reported `cancelled` with health `cancelled` and the recovery hint; the receipt and checkpoint say `succeeded` with traffic on the new revision; the automatic recovery run reported no recovery needed, last release succeeded. Honest reporting held: dispatch bookkeeping, workflow conclusion and recovery evidence were shown separately rather than merged into a guess. |
| Cancellation while the candidate is being created | 38212799331 | The cancel request was submitted eight seconds into `deploying`. GitHub stopped the runner hard: the release step shows only "The operation was canceled", no graceful rollback ran, and the uploaded receipt still says `promoting`. This is the runner-loss case. The automatic recovery workflow that GitHub triggered on the cancelled run read the durable checkpoint, restored 100% of traffic to the previous revision, and marked it `recovered` with the service Ready; the final routing confirmed it. The controller reported `cancelled` with the recovery hint. |

Cost tools: `estimate_cost` read the committed `platform.yaml` from the real repository and the plan carried the bounds with the operator ceiling (`within_limit` at USD 150).

Platform tests after the trial fixes: 111 passed; ruff, zizmor and the platform CI on GitHub are clean for every trial commit.

Not performed: an abrupt runner kill (only a graceful cancellation), Binary Authorization enforcement, and anything production-related. The staging project remains available for further exercises and costs nothing while idle.
