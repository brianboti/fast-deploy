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

## Live exercise of 0.9.0 (2026-10-12)

Environment as in the exercise above: the controller on the operator's laptop behind the ngrok tunnel, started with the launcher at 16:59:26 UTC with `auto_deploy: true` on the demo application, `auto_deploy_interval_seconds: 300` and `observer.service_account` set; the app repository pinned to the 0.8.0 platform commit (0.9.x changed no templates). The background jobs first run 60, 120 and 300 seconds after launch and then keep their intervals. Tool calls went through the controller's own tool interface, as Claude makes them.

| Scenario | Run | Observed |
|---|---|---|
| Auto-deploy of a pushed commit | 38281323839 | 63 s after launch the auto-deploy job found main at 5e3afed, neither live nor attempted, and dispatched the release as the identity `auto-deploy` (plan 5a53eecf; audit event `auto_deploy` with outcome `dispatched`). Build 50 s, deploy 106 s, notify 5 s: `succeeded` 2 min 51 s after dispatch, with revision `lab-demo-r5a53eecfaa844a28` serving all traffic. The next tick reported the commit already handled by the dispatched plan, the one after that `up to date`. The operator saw the plan through `list_plans` and followed it with `get_deployment_status`. |
| Watchdog | in-process job | Ran about two minutes after launch: configuration loadable, credential 59 days from expiry, schedules and pin in order, no problems, nothing to notify (no webhook configured). `lab-deploy watchdog` on the command line gave the same result. |
| Backup | in-process job | Ran five minutes after launch and wrote `deployments-20261012-170431.db` next to the database, keeping 14 days; `lab-deploy backup` had written `deployments-20261012-165923.db` on demand before the launch. Both copies pass SQLite's integrity check. |
| Controller watch from GitHub | 38282088934 | Dispatched by hand with the `CONTROLLER_HEALTH_URL` variable set: the run fetched `/healthz` through the tunnel, received `{"status":"ok"}` and passed in 8 s. The half-hourly schedule had not fired yet when this was written. |
| Live health before the observer exists | tool call | `get_service_health` failed with the plain reason that no observer token could be obtained and what to do about it (sign in with gcloud; create the Terraform observer identity with the operator as impersonator). Nothing else was touched. |
| Controller status | tool call | `get_controller_status` listed the version, the observer identity, the auto-deploy applications, each background job's last run, result and next run, the credential's expiry and the configuration reload. |
| Live health and logs through the observer | tool call | After the apply created the observer identity and the impersonation grant: `get_recent_logs` returned the last 30 minutes at ERROR and above (none), and `get_service_health` showed the URL, ingress, the serving revision with its plan and commit, the hour's metrics (4 requests, 0 server errors, p95 9.5 ms, 1 instance, 14% memory) and agreement with the recorded live release. The first call, seconds after the grant, was refused for permissions; the refusal cleared within two minutes (IAM propagation). Readiness was reported false for a Ready service (finding 4). |

Terraform apply by the operator (2026-10-12, after the exercise above): the project module's resources were created, namely the observer identity with its three viewer roles and the operator's impersonation grant, the tag-manager custom role, the e-mail notification channel, the billing dataset, the spending-cap service account with its roles, the Pub/Sub topic with the budget publisher grant and the APIs, and the budget was updated to publish to the topic. Cloud Workflows rejected the spending-cap workflow (finding 3), which stopped the apply before the service module's resources. A read-only plan after the fix showed 18 to add, 1 to change, 0 to destroy. The operator's second apply created 16 of them and applied the change: the workflow and its trigger, the dashboard, four of the six alert policies, the logging viewer, the Secret Manager grants, the tag-manager, billing-reader and billing-query grants, the cap's and the probe's invoker grants, and the registry keep policy. Google's API rejected the uptime check (finding 5) and the saturation alert (finding 6); the health alert, which depends on the uptime check, was skipped. A third run creates those three.

Findings that changed the code, each fixed with a regression test:

1. Plans created by the auto-deploy identity were invisible to the human deployers of the application; deployers now see every plan of their applications (0.9.0, found while building).
2. The observer error quoted gcloud's impersonation warning instead of its error line (0.9.1).
3. Cloud Workflows rejected the spending-cap workflow with "Unterminated expression": its log messages contain a colon followed by a space inside unquoted expressions, which YAML reads as a nested key, and no test parsed the file (0.9.2).
4. `get_service_health` reported every service as not ready: Cloud Run v2 returns the Ready condition as `terminalCondition`, and the observer looked for it in `conditions` (0.9.2).
5. The uptime check used the Monitoring service agent's OIDC token against a plain URL; Google offers that only for checks that name a Cloud Run service as a `cloud_run_revision` resource (0.9.3).
6. The saturation alert used a greater-or-equal comparison; the Monitoring API only offers greater-than and less-than (0.9.3).
7. The uptime check asked for certificate validation, which the API only offers for plain-URL checks (0.9.4). The third apply created the saturation alert and refreshed the dashboard; the final check shape was confirmed by creating and deleting a throwaway check through the API before the fourth run.
8. Every plan wanted to update the dashboard, because the API stores it with an explicit axis and without zero positions or empty group lists (0.9.5).

The operator's fourth apply (0.9.4) completed: uptime check `lab-demo-health` created with the health alert, dashboard refreshed; Terraform reported 2 added, 1 changed, 0 destroyed. The staging project now carries the six alert policies, the uptime check, the dashboard, the spending-cap workflow and trigger, the observer identity and every grant. A read-only plan after 0.9.5 reports no changes. The uptime check's first probe, signed with the Monitoring service agent's identity token against the IAM-protected `/health` path, passed from the usa-iowa checker about five minutes after creation.

The controller was restarted on 0.9.2 after the second apply; `get_service_health` then reported the service ready with full agreement.

Not performed: a notification delivery (no webhook secret), a scheduled controller-watch run, an alert firing, the spending cap end to end, and the always-on machine (deferred by the operator). Nothing of the Terraform change remains pending.

Platform tests: 195 passed; ruff and zizmor clean.

## Rehearsal of the 1.0.0 state migration (2026-10-12, read-only)

Version 1.0.0 splits the staging state into a foundation state and one state per application. Before asking the operator to run the migration, it was rehearsed on a pulled copy: the 54-resource state was split locally into 35 foundation resources and 35 for the demo application, and each new root was planned against its split state through a local backend override, with the demo root reading a hand-written copy of the foundation's outputs. Foundation: 20 to add (the pipeline's pool, provider, two identities and their grants; the cap's list permission), 1 to change (the cap workflow now discovers services by label), 0 to destroy. Demo application: 0 to add, 3 to change (the managed labels on the service, registry and recovery bucket), 0 to destroy. The rehearsal found that moving a module inside one state file with an explicit output path makes Terraform write the source last and lose the move; the migration script was corrected before anyone ran it. Platform tests: 213 passed; ruff and zizmor clean; both roots validate.

## Live exercise of 1.0.0 to 1.0.2: the infrastructure pipeline (2026-10-12)

| Scenario | Run | Observed |
|---|---|---|
| State migration | operator script | `migrate-state.sh` pulled the 54-resource state, split it into 35 foundation and 35 demo resources, pushed both new states and kept the old object; a read-only plan of the foundation against the real new state matched the rehearsal exactly (20 to add, 1 to change, 0 to destroy). |
| Foundation apply | operator script | All 20 additions created: the platform pool and provider, both pipeline identities with their roles and federation, the state-bucket read grant, the cap's list permission. The cap workflow's update was rejected by Cloud Workflows (finding 9); a throwaway deploy of the corrected source succeeded and was deleted, and the re-apply is pending. |
| First pipeline apply, by hand | 38292740794 | Dispatched with `apps=lab-demo` after the three variables were set: the job authenticated through the apply identity from the `infra` environment, planned with the state lock (0 to add, 3 to change, 0 to destroy) and **refused** the plan: the region rule read the bucket's location `US-CENTRAL1` literally (finding 10). The refusal worked as designed; nothing was applied. |
| Pipeline apply after 1.0.1 | 38292960201 | 0 violations; applied the three managed labels on the service, registry and recovery bucket. The publish step crashed on an output the committed root did not have yet, and the pipe to the job summary hid the failure (finding 11). |
| Pipeline apply on push, 1.0.2 | 38293174529 | Triggered by the push that re-rendered the root: no resource changes, outputs refreshed, and the eight variables printed for the operator with the note that no `APP_REPOS_TOKEN` exists. Labels confirmed on the service and the registry with gcloud; the release labels from the last deploy sit beside them. |
| Plan on a pull request | 38293202671 | brianboti/lab-deploy#4 changes the saturation threshold of the demo app: the plan job ran with the read-only identity and without the lock, and posted one comment: `0 to add, 1 to change, 0 to destroy, 0 to replace`, the one alert policy, `Policy: passed`. Merging it applies from the `infra` environment (pending the operator). |

Findings that changed the code, each fixed with a regression test:

9. Cloud Workflows expressions take no map or list literals; the discovery step now tests fields with `in` and ordered switch conditions (1.0.1).
10. Cloud Storage reports locations in capitals; the region policy compares without regard to case (1.0.1).
11. A step that pipes its output into the job summary did not fail on an error before the pipe; both workflows run with `pipefail`, and `publish` explains a stale root instead of crashing (1.0.2).

Not performed: the apply-on-merge run (awaiting the operator's merge of the test pull request), the automatic publishing of outputs (no `APP_REPOS_TOKEN`), and a release with the merged label flag (awaiting the operator's merge of the demo app's pin). Platform tests: 213 passed; ruff and zizmor clean.

## Live exercise of 1.1.0 and 1.1.1: buckets, topics and Firestore (2026-10-12)

The demo app declared `needs: {buckets: [uploads], topics: [events], database: firestore}` in brianboti/lab-demo-app#10 after its workflows were pinned to 1.1.0 (brianboti/lab-demo-app#9) and the controller restarted on 1.1.0 with `platform.minimum_commit` raised.

| Scenario | Run | Observed |
|---|---|---|
| Release before the needs exist | 38295574837 | Auto-deploy released the merged spec on its own; the runner stopped before deploying with `Missing needs: topic lab-demo-events, subscription lab-demo-events, Firestore database lab-demo. Ask for propose_infrastructure, merge the pull request and let the pipeline apply it` (the bucket already existed from a partial apply); traffic was untouched. Fail-closed as designed. |
| Infrastructure pull request | 38295619710 | brianboti/lab-deploy#5, rendered by `infra render demo`, kept the app's `instance_alert_count: 4` from the controller configuration and added the needs; the plan comment listed 12 to add (bucket, topic, subscription, database and eight grants), `Policy: passed`. |
| Apply on merge | 38295713744 | Created the bucket, topic, subscription, the bucket grants and the conditional Firestore grants, then failed with two 403s: the apply identity could neither set Pub/Sub IAM policies nor create a Firestore database (finding 12). |
| Apply after 1.1.1 | 38296059673 | 5 to add, 0 violations, applied: the topic and subscription grants and the database. gcloud confirmed the bucket (uniform access, public access prevention enforced), the subscription (20 s ack deadline, never expiring), the database (Native, regional, delete protection on, no point-in-time recovery) and the runtime's `roles/datastore.user` limited by condition to `projects/lab-deploy-staging-f29a/databases/lab-demo`. |
| Release with the needs | plan f570dd73 | `deploy_latest` showed the four derived variables and the warning in the plan; the release succeeded in about two and a half minutes and revision `lab-demo-rf570dd737c7846b3` carries `BUCKET_UPLOADS`, `TOPIC_EVENTS`, `SUBSCRIPTION_EVENTS`, `FIRESTORE_DATABASE` and the app's own `GREETING`. The service kept the managed labels beside the new release labels (`--update-labels`). |

Findings that changed the code, each fixed with a regression test or a documented grant:

12. The pipeline's apply identity needs `roles/pubsub.admin` and `roles/datastore.owner` on top of editor to grant on topics and create databases (1.1.1).

| Needs in the health view (1.2.0) | tool call | After the foundation granted the observer the bucket, Pub/Sub and Datastore viewer roles (three additions), `get_service_health` listed the four needs with their facts: bucket in US-CENTRAL1, topic, subscription with a 20 s acknowledgement deadline, database in us-central1 with delete protection; no notes. In the minute before the roles propagated the view said `Needs not checked: the observer identity is not allowed to read this` and kept the rest intact. |
| Retirement checklist (1.2.0) | command | `lab-deploy decommission demo` read the app's `platform.yaml` and printed twelve steps, including the bucket, the subscription and topic, and the database with its delete-protection switch, before the `terraform state rm` and `terraform destroy` step. Nothing was run. |

Not performed: a read or write by the application itself against the bucket, topic or database (the demo app does not use them yet); removal of a need (refused by design); Cloud SQL, jobs and migrations (roadmap 6.5); the spend-by-application query against a real export table (none exists yet; covered by a unit test with a mocked response). Platform tests: 221 passed; ruff and zizmor clean; both roots validate.

## Readiness for Claude chat (2026-10-12)

The last run before daily use from Claude chat, over the public URL, exactly as Claude's connector works: a script registered a client, signed in on the controller's page and drove the tools with the OAuth access token. The demo app served as the test application; nothing new was built for it beyond a one-line README change (brianboti/lab-demo-app#15) so the release was real.

| Step | Observed |
|---|---|
| Discovery | `/mcp` without a token answers 401 with `WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`; the protected-resource and authorization-server documents name the issuer, `S256`, the `authorization_code` and `refresh_token` grants and the registration endpoint. |
| Registration | A public client with a loopback redirect was registered (`token_endpoint_auth_method: none`, no secret); a registration with a foreign redirect was refused (`invalid_redirect_uri`). |
| Sign-in | `/authorize` with PKCE and the resource redirected to the sign-in page, which named the client and sent a strict Content-Security-Policy; without the `resource` parameter the request was refused; a wrong operator token answered 401 with the error shown on the page; the right one redirected to the loopback callback with the code and the same `state`. |
| Tokens | The code exchange returned a one-hour bearer token and a refresh token; the code could not be used twice (`invalid_grant`). |
| Tools | `tools/list` returned the twenty tools; `get_controller_status` (1.5.0, dispatch live), `list_applications` (demo live at 5eb2680, pin supported), `compare_with_live` (one commit waiting) and `estimate_cost` with a traffic scenario (ceiling policy `within_limit`) answered with the OAuth token. |
| Release | `deploy_latest` planned and dispatched f937329 (plan c6b14fc5, run 38319196132) with auto-deploy paused for the test; `get_deployment_status` with 45-second waits reported `running`, `queued`, then `succeeded` after 179 seconds: revision `lab-demo-rc6b14fc5abb84e2b` serves all traffic. `get_service_health` showed the service ready on that revision with ingress open; `get_recent_logs` found no error lines; `get_cost_report` returned the weekly report; `list_plans` listed 28 plans and `list_events` the audit trail with the signed-in identity and its address. Auto-deploy was turned back on afterwards. |
| Token lifecycle | A refresh rotated the refresh token and issued a new access token; reuse of the rotated refresh token was refused (`invalid_grant`) and ended the whole family, so the earlier access token answered 401 afterwards (`token_reuse` in the audit trail). Revocation through `/revoke` works when the request carries `client_id`, `token` and a `client_secret` field, empty for a public client. |

Findings:

30. The SDK's revocation endpoint requires the `client_secret` field to be present even for public clients (`client_secret: Field required`); documented in `docs/SETUP.md`, not changed (the SDK version is pinned).
31. The controller-watch schedule on the hour and half-hour never ran in five hours (GitHub delays and drops schedules that land on those minutes); moved to 13 and 43 minutes past the hour.

The operator's own Claude session from earlier in the day is revoked in the database (`python -m labdeploy.cli sessions`); the first conversation starts with a fresh sign-in, which this exercise proved.

## Live exercise of 1.5.0: the review's fixes (2026-10-12)

Every finding of `docs/REVIEW-2026-10-12.md` was implemented in two commits (c3e04fe, 08cc697) plus 1b881df and 69b16e9, with 234 platform tests, ruff and zizmor clean and both Terraform roots validating. The steps below ran against the real GitHub and Google Cloud APIs; the foundation apply is the operator's (the assistant's tooling refuses infrastructure applies).

| Step | Evidence | Observed |
|---|---|---|
| Foundation plan (read-only) | `terraform plan -lock=false` in the official container | 27 to add, 2 to change, 7 to destroy: the single provider `github` replaced by `github-plan` and `github-apply`; `roles/viewer`, `roles/editor` and `roles/storage.admin` removed from the pipeline identities and 18 specific roles added; the plan federation binding re-keyed to `attribute.event_name/pull_request`; both state grants conditioned to prefixes; custom roles `labDeployBucketManager` and `labDeployCapOperator`; the cap workflow updated; the failure alert policy; the backup bucket; the dataset's label; the duplicate Pub/Sub API entry dropped without disabling the API (`disable_on_destroy = false`). |
| Foundation apply | operator script | The operator's first run (typed `yes`) created or changed 35 of the 36 resources and stopped on the last: Google limits a Workload Identity provider's display name to 32 characters and the apply provider's was 44 (finding 27, brianboti/lab-deploy#11). The second run added the provider: 1 to add, 0 to change, 0 to destroy, and `terraform output` printed the two providers and two identities exactly as the repository variables `INFRA_PLAN_WIF_PROVIDER`, `INFRA_APPLY_WIF_PROVIDER`, `INFRA_PLAN_SERVICE_ACCOUNT` and `INFRA_APPLY_SERVICE_ACCOUNT` hold them, plus the backup bucket's name. |
| Review gate | `gh api` | `main` protected: pull request required, status checks `test` and `terraform` required, administrators included, no force pushes, no deletions, conversations resolved; environment `infra` with administrator bypass off and protected branches only; `.github/CODEOWNERS` names the owner. |
| Plan identity on a pull request | brianboti/lab-deploy#10 | The plan job authenticated through `github-plan` with no basic role, read the application's state and the foundation's outputs under the prefix-conditioned grant, and planned exactly the expected change: the cap's grant replaced (`roles/run.developer` to `labDeployCapOperator`). The strengthened policy then refused that very grant (`platform_identities_untouched`), because every root grants the cap its role on the application's service (finding 28, brianboti/lab-deploy#12: the one allowed reference is that grant, with that role, on a Cloud Run service). With the corrected rules on `main`, run 38313687040 planned the same single replacement and reported `Policy: passed. Merging applies exactly this plan.` |
| Apply identity on merge | run 38314916418 | Merging brianboti/lab-deploy#10 (d8e6098) ran the apply job from the `infra` environment through `github-apply`: the identity without `roles/editor` or `roles/storage.admin` planned again with the lock, passed policy, applied `1 added, 0 changed, 1 destroyed` in 25 seconds and printed the eight outputs for the operator. Cloud Run confirms the cap's grant on `lab-demo` is now `labDeployCapOperator` and ingress stayed `all`. |
| Demo re-pinned to 1.5.0 | brianboti/lab-demo-app#14 (merge 5eb2680) | Three workflows regenerated against 69b16e9; `platform.minimum_commit` raised; the controller restarted through `scripts/start.sh` (token read from the Keychain, never on a command line) and reported version 1.5.0 with the pin supported. |
| Release through the 1.5.0 runner | run 38312028136 (auto-deploy) | Auto-deploy released the re-pinned commit on its own through the 1.5.0 runner: build, deploy and notify jobs succeeded, revision `lab-demo-r98018194fa054bb3` serves all traffic, status `succeeded`. |
| Restore rehearsal | local copy, second port | `python -m labdeploy.cli backup` wrote `deployments-20261012-213947.db`; the copy and a configuration pointing at it (paused, dispatch off) started a second controller on port 8010, which answered `/healthz`, listed the applications and returned the 26 plans of history to the same operator token. With `backup_bucket` set, `python -m labdeploy.cli backup` uploaded `controller/deployments-20261012-215200.db` to `gs://lab-deploy-staging-f29a-lab-deploy-backups` with the operator's gcloud sign-in; the configuration file was missing from the upload because the CLI passed no path (finding 29, fixed and tested in brianboti/lab-deploy#12). |
| Cap workflow and its failure alert | `gcloud workflows run` | Execution `ca8fdfc9` with a synthetic notification of cost 0 of budget 1 returned `below cap: 0 of the budget` (trigger path, identity and the amounts-based ratio, nothing closed). Execution `fdc461d9` with a message that is not JSON failed by design at 21:52:28 UTC; the metric `finished_execution_count{status=FAILED}` showed the point at 21:53:38 and the alert **Lab Deploy: spending-cap workflow failed** is wired to the email channel. Delivery to the inbox is the operator's to confirm; the policy, the channel and the metric point are verified. |
| Dependabot | brianboti/lab-deploy#8, #9 | The new `directories` list immediately proposed `hashicorp/google` 8.6.0 for both roots, a major beyond the `~> 7.0` constraint; closed, and majors are now ignored (minor and patch bumps still arrive). |

Findings that changed the code, each fixed with a regression test or a validated plan:

27. A Workload Identity provider's display name may hold at most 32 characters (brianboti/lab-deploy#11).
28. The platform-identity rule must allow the service module's own cap grant, and only that (brianboti/lab-deploy#12).
29. The CLI backup passed no configuration path, so only the database copy was uploaded (brianboti/lab-deploy#12).

Not performed: a migration that changes a schema under the new timeout handling (no application with a database today); a budget event closing ingress for real (the cap was exercised with synthetic notifications); the container path behind a reverse proxy (`FORWARDED_ALLOW_IPS` documented, laptop path verified earlier). Platform tests: 235 passed; ruff and zizmor clean; both roots validate.

## Live exercise of 1.4.0 to 1.4.2: Cloud SQL, the migration job and the cap (2026-10-12)

The foundation gained the private network (VPC, subnet, Private Service Access, three APIs, the cap's Cloud SQL role) in one operator apply; the demo app was pinned to 1.4.0 (brianboti/lab-demo-app#11) and then declared `database: postgres` with `migrate: [python, -c, "print('no migrations yet')"]` (brianboti/lab-demo-app#12). The instance existed for about twenty-five minutes.

| Scenario | Run | Observed |
|---|---|---|
| Cap workflow with the database stop | operator apply | Cloud Workflows rejected the first version: an expression may hold at most 400 characters (finding 13). Built in parts, deployed as a throwaway and deleted, then applied. |
| Release before the instance exists | 38299168507 (auto-deploy) | Stopped before deploying with `Missing needs: Cloud SQL instance lab-demo, migration job lab-demo-migrate`; traffic untouched. |
| Changing the database type | plan 38298942168 | The root moved from `firestore` to `postgres`, which means destroying the Firestore database; the plan was refused with `Instance cannot be destroyed`, as designed. The operator retired the empty database deliberately with `retire-demo-firestore.sh` (delete protection off, delete, `terraform state rm`); only then did the plan proceed. |
| Policy on the first database plan | plan 38298942168 (re-run) | 8 to add and 2 to destroy (the Firestore grants), then **blocked**: the label rule looked for a top-level `labels` map while Cloud SQL keeps them under `settings.user_labels` (finding 14). After 1.4.2 and a rebase: `Policy: passed`. |
| Apply with the instance | 38301546097 | 8 added, 2 destroyed, 0 changed in 14 min 33 s, of which the instance took 14 min 5 s: instance, `app` database, the runtime identity as IAM user, the client, instance-user and viewer grants, the migration job and the deployer's grant on it. gcloud confirmed: `db-f1-micro`, zonal, 10 GB HDD, deletion protection on, no public address, attached to the foundation's network, `cloudsql.iam_authentication=on`, managed labels, the IAM user beside Cloud SQL's built-in `postgres`. |
| Release with the migration step | plan 73beb134 | Phases `preparing, migrating, deploying, promoting, succeeded`; execution `lab-demo-migrate-5vlzj` succeeded with "no migrations yet" in its log; revision `lab-demo-r73beb134006f4ded` carries `DATABASE_INSTANCE`, `DATABASE_NAME`, `DATABASE_USER` and `DATABASE_IP_TYPE` and routes its egress through `lab-deploy`/`lab-deploy-run` with `private-ranges-only`. |
| Retiring the instance | operator script | `retire-demo-postgres.sh` turned deletion protection off, deleted the instance and removed the instance, database and user from the state. The spec went back to `database: none` (brianboti/lab-demo-app#13); the rendered root's plan destroyed only the migration job and five grants (brianboti/lab-deploy#7, run 38304242421), `Policy: passed`. |
| Release after the trial | 38304101475 (auto-deploy) | The reverted spec released on its own; revision `lab-demo-r0cdbc1ea08d849e6` carries no `DATABASE_*` variable and no network interface (`--clear-network`), and serves all traffic. |

Findings that changed the code, each fixed with a regression test:

13. Cloud Workflows limits an expression to 400 characters (1.4.1).
14. The label policy must read Cloud SQL labels from `settings.user_labels` (1.4.2).

Not performed: an application actually connecting to the instance (the demo has no database code; the connector path is documented), a migration that changes a schema, the cap stopping an instance (no budget event), a stopped instance's restart. Platform tests: 224 passed; ruff and zizmor clean; both roots validate. Every item of the Phase 6 roadmap is implemented and exercised on staging; the staging project holds no Cloud SQL instance after the trial.

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
