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

## Comprehensive test with a complicated application (2026-10-13)

A reference application, [brianboti/lab-notebook](https://github.com/brianboti/lab-notebook), was written for this test:
FastAPI pages for people and a JSON API, Postgres through the Cloud SQL Python connector with IAM sign-in, Alembic
migrations as `needs.migrate`, an uploads bucket, an events topic read back on a page, an API key from Secret Manager,
JSON logs with trace, 26 tests, a hash-locked image running as a non-root user. It was taken from a repository link to
a running service and through every release path the platform offers. Twenty-six findings were fixed on the way
(1.7.2 to 1.8.2, pull requests #23 to #27), each with a regression test in `tests/test_comprehensive_findings.py`.

| Scenario | Plan or run | Observed |
|---|---|---|
| Offline: analyzer, policy, cost, demo walkthrough | local | 16 walkthrough steps pass once demo mode uses the app's own name; the analyzer sees the lock file and tests and suggests the four needs |
| One-sentence onboarding | brianboti/lab-deploy#22, lab-notebook#2 | Contract checks pass; the first plan failed (new root without state, then the policy refused the module's own role), the first apply failed (log alert permission, uptime check created four seconds after the service): all fixed, then environment, workflows and first release ran unattended |
| First release with migrations | 9111fd55 | `preparing, migrating, deploying, promoting, succeeded`; Alembic 0001 and 0002 ran on Postgres 17 as the runtime IAM user (with `cloudsqlsuperuser`) |
| The app end to end | live_app_test.py | 24 of 24: anonymous caller refused (403), identity seen, Postgres rows, bucket upload and byte-identical download, Pub/Sub events read back, API key enforced, every page |
| Update with a migration (1.1.0) | f0a3dff4 | Migration 0003 on existing data; earlier rows and files kept; 24 of 24 |
| Rollback | 106b48b4 | 53 seconds, no build; 1.0.0 serves and writes against the new column (expand first) |
| Redeploy an earlier commit | edc0ee51 | No build job; tests and audit `passed_in_earlier_release` |
| Health check failing only on Cloud Run | c97ed438 | Startup probe failed, traffic never moved, candidate's log lines in the receipt |
| Migration failing only on Postgres | a225036c, 8e1af163 | Stopped before deploying; on 1.8.2 the status names `relation "experiment_typo" does not exist` and the execution |
| Failing unit test | 65acdc8d | Stopped at the tests in 47 seconds; nothing built |
| 4 CPUs, public exposure, a missing secret | tool calls, 2b601d56 | Refused at plan time in plain words; the missing secret stopped the release before deploying |
| Cost ceiling, pause, auto-deploy | application_settings | Ceiling refused a $141.28 worst case over $50; pause refused; auto-deploy released main twice, within five minutes |
| Cancel during the migration | d5fafac2, 215f609e, 0062147b | On 1.8.0 and 1.8.1 the release went on and promoted (the deploy job used `always()`); on 1.8.2 the job was cancelled, the release restored traffic and the previous revision kept serving |
| Health, logs, events, cost | tools | The health view shows the database (`RUNNABLE`, `db-f1-micro`, `POSTGRES_17`) and agreement; JSON log lines with severity; 76 audit events; estimate $0 idle plus $8.57 for the database; weekly report run on demand; no rightsizing proposal |

Not performed here: pages for people through Identity-Aware Proxy (needs the one-time console step, docs/SETUP.md 7h),
a real budget event closing ingress and stopping the instance, and actual spend from the billing export: its newest
row is from 2026-09-25 although the table is written daily, so the report now says the spend is unknown. Platform tests:
279 passed; ruff and zizmor clean; Terraform validates.

## A real lab project: Sophia's backend (2026-10-13 and 2026-10-14)

A private copy, [brianboti/sophia](https://github.com/brianboti/sophia), of the lab's Sophia (UTHealthScribe) clinical
documentation project; the original folder and repository were not changed. Only the FastAPI backend is deployed, in
the project's own no-PHI demo mode (`DEMO_NO_PHI_ACK=true`, a throwaway SQLite database inside the container, the
project's published demo accounts, no patients), with three secrets in Secret Manager. The copy gained only platform
files: a root Dockerfile (the backend's, with `backend/` paths), an allow-list `.dockerignore`, a root `pytest.ini`
(the backend's options; four tests that need the gitignored MedCPT models deselected, with the reason), `platform.yaml`
(`python: "3.13"`) and the requirement files. The project's suite is 917 tests; 913 run.

| Scenario | Plan or run | Observed |
|---|---|---|
| Onboarding | brianboti/lab-deploy#32, sophia#7 | Root, environment, workflows: no new finding |
| First release | fd7ea104 | Stopped at the tests: the lock was compiled on Python 3.13, where SQLAlchemy needs no greenlet; the platform tested on 3.12 (reproduced in python:3.12-slim and 3.13-slim). Fixed in 1.8.5 (`python:` in platform.yaml) with a 45-minute build job |
| Dependency audit | ae00a166 | 66 known advisories in cryptography, PyJWT and pypdf; upgraded in the copy (cryptography 50.0.0, PyJWT 2.15.0, pypdf 6.20.1), lock recompiled in the image's own Python, every test passes, pip-audit clean |
| Build provenance | b4559ff3 | Stopped at *Attest build provenance*: GitHub attests private repositories only on Enterprise Cloud. Fixed in 1.8.6: Cosign with the application's own Cloud KMS key |
| Release with Cosign and Cloud KMS | 030d25c7 | Tests, audit, build; path `cosign-kms`; signed with `sophia-provenance`, verified (signature, then digest, repository, commit and workflow), deployed; 936 seconds; receipt `provenance: cosign-kms`; the signature stored as `sha256-<digest>.att` |
| The app end to end | sophia_live_test.py | 10 of 10: anonymous caller refused (403), health and readiness through `X-Serverless-Authorization`, wrong password refused, clinician and admin sign in, the admin API refuses a clinician, demo users listed, no patients |
| The stored signature attacked | local Cosign | The key's public half verifies it; another key: `no matching attestations`; a wrong commit or repository: refused by the statement check |
| Redeploy the same commit | 57705946 | No build, push or signing; the reused image's signature verified again; 150 seconds |
| The operator's exception | c874d314 | `provenance: unavailable` under `application_settings.sophia` for one dispatch, then removed: the plan warned and built fresh although a signed image of the commit existed; the run shows *Provenance path: unavailable* and the *Provenance not recorded* warning; every signing step skipped, no signature stored; receipt `provenance: unavailable`; 10 of 10 |
| After the exception | c992ed1d | Before 1.8.7 the next plan for the commit would have reused the unsigned image and stopped at verification; on 1.8.7 it reused the earlier signed image (from 57705946), verified it and released it in 136 seconds; 10 of 10, the signed digest serving all traffic |

What the platform does not offer yet, found with this project: an application in a subfolder (the repository root is
the build context; the copy needed a root Dockerfile, and the onboarding checklist does not say so), a Postgres reached
with a password `DATABASE_URL` (only IAM sign-in through the connector), Redis, and a way for the project's web and
mobile front-ends or its own sign-in to reach an `authenticated` service (public exposure is refused by design). For
the project itself: four tests need the gitignored MedCPT models, so they fail on any clean checkout; the 66 advisories
above (the upgrades pass every test); its own CI's security audit and ask-sophia typecheck were already failing. Platform
tests: 294 passed; ruff and zizmor clean; Terraform validates.

## Live exercise of 1.8.8: other Pythons (2026-10-14)

Before the change, checked in containers: `numpy==1.24.4`, an ordinary pin for a Python 3.11 project, failed the old
audit on the platform's 3.12 (no 3.12 build, so pip-audit could not install it) and passes the new step on 3.11;
pip-audit judged a `python_version >= "3.14"` marker for its own interpreter (skipped on 3.12, audited on 3.14); the
audit tool installs from wheels only on 3.11, 3.13, 3.14 and 3.15; the documented uv export flow installs with
`--require-hashes` and audits clean on 3.14.

| Scenario | Plan or run | Observed |
|---|---|---|
| Public app moved to Python 3.15 (lab-demo-app#16, with the workflows pin) | 9c4f02f6, auto-deploy | Tests on CPython 3.15.0; audit on 3.15 reading `requirements.lock` with `--disable-pip`, clean; image `python:3.15-slim` by digest, hash-locked install; provenance path `github`, attested and verified; 175 seconds; the service answers `"python": "3.15.0"`, anonymous callers get 403 |
| Private app on Python 3.13 with a lock (sophia#10, workflows pin) | c71f828d | Tests on CPython 3.13.16 (892 passed, 21 skipped, 4 deselected); the audit, now reading `requirements.lock`, stopped the release: 10 known advisories in aiohttp 3.14.1, multidict 6.7.1 and urllib3 2.7.0, pinned only in the lock (they arrive through other packages). The old audit resolved `requirements.txt` to the newest versions and never saw what the image installs, so the image released earlier the same day carried them |
| The same app after the upgrade (sophia#11) | ed87c7e4 | aiohttp 3.14.3, multidict 6.9.1, urllib3 2.8.0 (only those changed, recompiled in the image's Python); tests pass again, audit clean, provenance `cosign-kms` signed and verified, released; 10 of 10 |

Platform tests: 299 passed; ruff and zizmor clean.

## 1.9.0 before the merge: what was checked against the real system (2026-10-14)

Read-only checks against staging and GitHub with the operator's own credentials while building Phase 9; nothing was
applied, deployed or created. The live exercise (a bake that restores traffic, a planted public bucket refused, a drift
reported, the native cap tripped and lifted) follows the merge, the foundation apply and the operator's spend cap.

| Check | Observed |
|---|---|
| The bake's log reads on the live demo (revision `lab-demo-r9c4f02f614fb4c0c`, after seven authenticated requests) | 19 request lines in ten minutes: 13 to `/health` (the uptime check and one probe) and 6 others (five 200, one 404); p95 3.2 ms; verdict `inconclusive` (fewer than 20 requests besides the health path), as designed for an idle staging app |
| Crash detection on real system logs | The 2026-10-12 crash of `lab-demo-r65462e46bf8e4bc9` found from its line `Container called exit(1).`, which Cloud Run writes at WARNING; startup-probe failures are ERROR. Both are bake failures; sandbox warnings are not |
| The deployer identities' Logging viewer role | Present for `lab-demo-deploy`, `lab-notebook-deploy` and `sophia-deploy`: the bake needs no new grant |
| Read-only plans of the three application roots (`-lock=false`, nothing applied) | `lab-demo` and `lab-notebook`: 3 to add each, the provenance key and its two grants that 1.8.6 left for each root's next apply; `sophia`: no changes. The new policy rules pass all three plans, and pass the demo's with every resource treated as changing; the drift classifier names the pending three as `data` and `iam` and reports `sophia` clean. A trimmed copy of the demo's plan is the regression fixture `tests/fixtures/lab-demo-plan-2026-10-14.json` |
| `scripts/spend-cap.sh check lab-deploy-staging-f29a` | No native spend cap; the account's two budgets (the USD 50 alert and the project's USD 20 budget) are alerts only, which the Budget API confirms |
| The month's per-event charges, counted from the registries and the Actions API | Demo (public): 15 freshly built images, about USD 3.90 in scans, Actions free. Sophia copy (private): 4 images, about USD 1.04; 873 billable Actions minutes in two days, 709 of them the project's own CI |
| hadolint 2.15.1 on the four Dockerfiles in use (platform, demo, Sophia copy, lab-notebook) | No findings |
| The foundation and the demo root with the 1.9.0 modules | `terraform validate` and `terraform fmt -check` clean |

Platform tests: 359 passed; ruff and zizmor clean.

Found on the way, for the operator: `lab-demo` and `lab-notebook` still lack the provenance key 1.8.6 planned for their
roots' next apply. Dispatching **Infrastructure apply** for both closes that difference, which the first drift report will
otherwise list.

## Live exercise of 1.9.0 (2026-10-14 evening, Central time; 2026-10-15 UTC)

Run after the operator merged brianboti/lab-deploy#39 and applied the foundation; everything else by Claude through the
platform's own tools, scripts and workflows.

| Step | Run, pull request or plan | Observed |
|---|---|---|
| Foundation apply (operator) | `scripts/apply-foundation.sh` | 2 to add, 1 to change: the plan provider's new condition (Google accepted the `in [...]` list) and the federation grants for `schedule` and `workflow_dispatch`; the four `INFRA_*` variables unchanged |
| Controller on 1.9.0 | `scripts/start.sh` in a terminal tab | Version 1.9.0 locally and through the tunnel; the status listed the project as missing a native spend cap and the drift check as not yet run |
| The native spend cap | `scripts/spend-cap.sh create` | The operator had already created it with the script (USD 20, Cloud Run, the staging project, `CONFIGURED`); the second run found it and created nothing; recorded under `cloud_run_spend_caps`, the status's `missing` list then empty |
| Drift found | A hand edit (`lab-deploy-managed=drift-test` on the demo's uploads bucket), then run 38676874089 from `main` | The plan identity signed in through the new federation; issue brianboti/lab-deploy#41 opened listing the demo's bucket (`data`) and the provenance key and its two grants still missing from `lab-demo` and `lab-notebook` (`data`, `iam`); Sophia clean. The restarted controller's status showed both apps as drifted, and its watchdog's first pass named both ("infrastructure drift in data, iam"); no webhook is configured, so nothing was posted |
| Drift repaired | Infrastructure apply run 38677116709 (`apps=lab-demo,lab-notebook`) | Both applies succeeded: the keys `lab-demo-provenance` and `lab-notebook-provenance` and their grants created, the bucket label back to `true` |
| Drift clean | Run 38677243349 | "No drift"; the issue closed itself as completed |
| Planted violations refused | Draft brianboti/lab-deploy#40 (a public, force-destroyable bucket, a secret version with `secret_data`, a Cloud SQL instance without backups, encryption-only connections or deletion protection) | **Policy: blocked** with six violations: `no_secret_in_state`, `protected_database`, `database_backups`, `encrypted_connections`, `private_bucket`, `no_force_destroy`; the plan job failed as designed; closed unmerged, branch deleted |
| Re-pin | lab-demo-app#17, lab-notebook#9, sophia#12 (platform c7fb636) | Merged; Sophia's two failing checks are the ones already failing on its `main` (its security audit and the ask-sophia typecheck); `platform.minimum_commit` raised to c7fb636, every pin still supported |
| The bake passes | Plan e60a9a36 (auto-deploy of the re-pin, run 38677620624), 85 authenticated page requests sent during the bake | Phases `preparing, deploying, promoting, baking, succeeded`; baking 03:44:52 to 03:49:51; 5 checks; 85 requests, none 5xx, p95 5.7 ms; 7 health requests left out; latency not compared (the previous revision served no requests besides the health path in the hour before, which the receipt says); `dockerfile_lint`: no findings |
| The bake restores | lab-demo-app#18 (`FAULT_EVERY: "4"`, every 4th page request answers 500), plan ac2975c0, run 38678400332 | Promoted 03:53:37, baking 03:53:52; at the first check, 72 seconds after promotion, 4 of 20 requests answered 5xx (20%, above 5%): `traffic_restored` with `failed_phase: baking` at 03:55:05, 100% back on `lab-demo-re60a9a36c53347fc`, the new revision's last 50 log lines in the receipt; the status said "failed its bake after promotion"; the recovery run that followed found nothing to do. The client saw 5 of 75 requests fail: the faulty revision served for about a minute |
| The bake with no traffic | lab-demo-app#19 (the switch removed), plan 7cdb7009 | `bake inconclusive: 0 requests in 300 s, too few to judge` (5 health requests left out); the release succeeded and the status said only the probes vouch for it |
| The native spend cap trips | The cap lowered to USD 0.01 at 04:07:37 UTC (October's gross Cloud Run cost: USD 0.05) | `ENFORCED` by 04:09:55; the demo and Sophia answered Google's "503 Server Error" page by 04:11:56, lab-notebook by 04:12:47; an edit while enforced refused ("Only spend cap state can be updated when spend cap is enforced"). The controller's health view still showed open ingress and no server errors: found, and fixed in 1.9.1 (below) |
| Lifted and re-armed | `scripts/spend-cap.sh lift` at 04:13:12, `scripts/spend-cap.sh amount ... 20` at 04:17:31 | `AWAITING_NEXT_PERIOD`; a 500 at 04:15:20, every service 200 by 04:17:21 (about four minutes); back to USD 20, `CONFIGURED` |
| The health view on 1.9.1 | `get_service_health` for the demo, an hour that included the trial | `uptime_checks_passed: 0.915` and the note that the uptime check reached the health path in only 92% of its probes although the service's own metrics may look normal |

What the exercise found, fixed in 1.9.1 the same night: the controller remembered "no drift report yet" for an hour
(its status said `not_run` after the first report existed); the health view could not see an outage that stops requests
before the container (it now reports the uptime checks' pass rate); and the Budget API refuses an amount change unless it
uses the mask `amount.specifiedAmount,spendCap.inputState` with the whole budget (the script's new `amount` and `lift`
commands do this; both ran in the trial).

What it measured about cost, from the billing export for October (gross, before credits; credits brought every line to
zero this month):

| Service | Gross this month |
|---|---|
| Image vulnerability scanning (Artifact Analysis) | USD 9.88, 38 scans |
| Cloud SQL (the September and October trials) | USD 0.11 |
| Cloud Run, every staging service | USD 0.05 |

Scanning is nearly all of the project's gross cost: each fresh build is a USD 0.26 scan, so a busy release week costs
more in scans than a year of this lab's Cloud Run. Turning automatic scanning off for staging (`image_vulnerability_scanning
= false` in the foundation), or releasing reused images more often, is the operator's choice: the scan is also a
security control.

Platform tests on 1.9.1: 364 passed; ruff and zizmor clean.


## 1.10.0 to 1.12.0 before the merges: what was checked (2026-10-15)

Built and checked in one night; nothing was applied, deployed or created in the cloud. The live exercises (a two-service
repository onboarded with one sentence each, Sophia's copy from `backend/`, Postgres by password, the checker's draft on a
bare repository, a buildpack release, an approval block on a real infrastructure pull request) follow the merges of
brianboti/lab-deploy#44 and #45 and the foundation apply 1.10.0 needs.

| Check | Observed |
|---|---|
| A root with `database_auth: password` planned against the real foundation state (local backend, `-refresh=false`, nothing applied) | 46 to add; the platform's policy: 0 violations; the plan's JSON holds no `postgresql://` URL, and the user's `password` and the secret version's `secret_data` are null (only the write-only versions, 1, appear) |
| The same root with `-lockfile=readonly` | `terraform init` and `validate` pass with the foundation's lock (the `random` provider 3.9.1 now pinned there); OpenTofu 1.12.6 validates it too |
| The live demo and Sophia roots with the 1.10.0 module (read-only plans) | No changes: a first service of a repository renders exactly as before |
| The two-service test repository, locally (scratch copy, not yet on GitHub) | Both services' tests pass (the admin service's with the repository root on the import path, as the workflow sets it); hadolint finds nothing; both images build for `linux/amd64`; the admin image, built from the repository root with its `Dockerfile.dockerignore`, holds only `app.py`, `migrate.py`, `lib/` and the lock, and answers `/health` and `/` with the shared code |
| Google's buildpacks on the demo app, locally under emulation (`pack` 0.40.9 by SHA-256, builder and run image by digest) | The Ubuntu 24.04 builder refuses Python 3.12 (it offers 3.13.0 to 3.15.0 only); the Ubuntu 22.04 builder built it on 3.12.15 in 58 seconds, pip installing the lock with its hashes; the image runs as uid 1000 and answers `/health`. Size as Docker reports it: 394 MB, against 52 MB for the demo's own Dockerfile image. Decision D28: the Dockerfile stays the default |
| The scaffold, end to end in scratch copies | The lock compiled in a container of Python 3.12 in about 4 seconds (14 packages, 163 hashes); drafted Dockerfiles for FastAPI, Flask, Django, Bottle and Litestar apps build and serve `/health` as uid 65532; the drafted CI steps pass in a clean container; hadolint and zizmor find nothing in the drafts |
| `scripts/export-app.sh --dry-run` for the demo (from the configuration file) and Sophia (from the controller's database, read-only) | Every command printed, none run. A real export of the demo was not run: the auto-mode classifier declined it, and it is left for the operator |
| OpenTofu 1.12.6 on the four committed roots (the foundation, lab-demo, lab-notebook, sophia) | All validate, with the official image and with the pinned amd64 binary the CI step downloads |

Read-only checks against the real system on the same night, with the branch code:

| Check | Observed |
|---|---|
| The checker on the new private test repository brianboti/lab-monorepo (two services: `services/api`, and `services/admin` built from the repository root with Postgres by password) | Given `brianboti/Lab-Monorepo`, it resolved GitHub's own spelling `brianboti/lab-monorepo`; both services ready; recommended items missing: `SECURITY.md`, CI, Dependabot, all of which the platform drafts; a folder that does not exist is refused in plain words. It also showed the scaffold would draft a `.dockerignore` beside `services/admin/Dockerfile.dockerignore` and a second health test: fixed before the merge, after which it drafts only the three missing files |
| The checker on Sophia's copy, folder `backend` | Not ready: only `backend/platform.yaml` is missing, which the platform drafts; it found FastAPI (`app.main:app`), Python 3.13 from the Dockerfile, port 8000 and `/health`, and would also draft `requirements-dev.txt`, a health test, `SECURITY.md` and Dependabot; the lock is left to the compile step or its command |
| The approval block on a real plan | The pipeline's comments on brianboti/lab-deploy#46 open with it: for the demo root, 0 to add, change or destroy, `reversible`, idle floor USD 0.06 before and after (the provenance key), no exposure change, the pull request, head commit and plan run |
| The restore drill | `python -m labdeploy.cli restore-drill` passed: the newest backup (1.1 hours old, 0.9 MB) opened read-only with integrity ok, 56 plans, 822 audit events, 213 plan state changes, 2 registered applications and 2 onboarding records; the configuration copy loads; recorded as the event `restore_drill` (ok), next due by 2027-01-15 |
| The foundation with the 1.10.0 module (read-only plan, `-lock=false`) | 3 to add, 0 to change, 0 to destroy: the custom role `labDeploySecretWriter`, its grant to the apply identity, and `roles/secretmanager.viewer` for the plan identity |
| The checker's draft on a bare repository (brianboti/lab-bare, private: only `app.py`, `requirements.txt` and one test), through the controller's own `propose_config` with GitHub reached by `gh` | Draft pull request brianboti/lab-bare#1 with nine files: the Dockerfile, an allow-list `.dockerignore`, `requirements.lock` compiled in a Python 3.12 container on this machine, `requirements-dev.txt`, a health test, `platform.yaml` with `reviewed: false`, CI, `SECURITY.md` and Dependabot. On GitHub the drafted CI passed in 14 seconds and GitHub accepted the Dependabot file. Locally the drafted Dockerfile passed hadolint, built, answered `/health` as uid 65532, and its 52 MB image held only `app.py` and the two requirement files. Found: a root service drafted before registration was named `my-service`; it now takes the repository's name |

Platform tests on 1.12.0: 454 passed; ruff and zizmor clean.


## Live exercise of 1.10.0 to 1.12.0 (2026-10-15)

Run after the operator merged brianboti/lab-deploy#44, #45 and #46 (each squash made the next stacked pull request
conflict; they were replayed onto `main` with identical files and the tests run again) and applied the foundation.
Everything else by Claude through the platform's own tools, scripts and workflows; the merges of the platform's own pull
requests and the review flag of a draft the platform wrote stay with the operator.

| Step | Run, pull request or plan | Observed |
|---|---|---|
| Foundation apply (operator) | `scripts/apply-foundation.sh lab-deploy-staging-f29a` | The first attempt changed nothing (Docker was not running, and the script needs it); the second created `labDeploySecretWriter`, its grant, and `roles/secretmanager.viewer` for the plan identity |
| Controller on 1.12.0 | `scripts/start.sh` in a terminal tab | 1.12.0 locally and through the tunnel; Docker running for the lock compile |
| Re-pin | lab-demo-app#20, lab-notebook#10, sophia#13 (platform 0e7802a) | Merged; Sophia's four failing checks are its own, the same as on its previous re-pin; `platform.minimum_commit` raised to 0e7802a and the configuration reloaded |
| The checker on a bare repository | `check_repository` on brianboti/lab-bare (only `app.py`, `requirements.txt`, a test) | Not ready: Dockerfile and `platform.yaml` missing, both draftable; seven recommended items, five draftable |
| The checker's draft | `propose_configuration`: brianboti/lab-bare#2 (the 1.10.0-era #1, still `my-service`, closed) | Nine files; `name: lab-bare`, FastAPI on 8080 found, `/health` route found in `app.py`, Python 3.12 (the default), lock compiled in Docker; its CI passed. Found: a second call while #1 was open answered `changed: []` and left the stale draft as it was without saying so (1.12.1 says so). Merging waits for a person to set `reviewed: true` |
| Both monorepo services ready | `check_repository` on brianboti/lab-monorepo, `services/api` and `services/admin` | Both ready; `SECURITY.md`, CI and Dependabot recommended |
| One sentence, the first service | `onboard_application` (`services/api`): lab-deploy#49, apply run 38785212259, lab-monorepo#1, plan ff711e05 | Registered as `api` (service `mono-api`, environment `staging`, `deploy-gcp.yml`); the infrastructure pull request planned (37 to add), merged by the controller once the checks passed, applied; environment variables published; the workflows pull request merged; first release `succeeded` at 18:33 UTC (bake inconclusive: no traffic); measures "Onboarded in 3.9 minutes, with no step blocked" (to the first release's dispatch; serving 11.6 minutes after the sentence) |
| One sentence, the second service | `onboard_application` (`services/admin`): lab-deploy#50, apply run 38785364285 | Registered as `admin` with the suffixed names (`staging-mono-admin`, `deploy-gcp-mono-admin.yml`); plan 48 to add, idle floor USD 8.63 per month, `data-bearing`; the apply created the Cloud SQL instance (6 min 18 s), the database and both users, then **failed** on the URL's secret version: Google's provider enables a version right after adding it, and `labDeploySecretWriter` lacked `secretmanager.versions.enable`; the workflow's retry lacked `versions.destroy`. Onboarding stopped as `blocked`, naming the run and what to do. Fixed in 1.12.1 |
| What the retry would have done | The plan of brianboti/lab-deploy#52 | With the permission, the retry would have written a URL holding a new password while the user kept the first: 1.12.1 ties the user's `password_wo_version` to the URL's version number. The plan on #52: `mono-admin` replaces the tainted version and updates the `app` user in place (0 to add, 1 to change, 1 to replace); the four other roots no change |
| Compose drafts | `analyze_repository` for `api` (the repository's `compose.yaml`) | Two web drafts (`services/api`; `services/admin` with `build.context: repository`), Postgres advice for `db`, the cache rule for `redis` (no cache by default; Memorystore about USD 36 to 47 a month, never scales to zero). Found: the admin draft said `database: none` although Compose gives it `DATABASE_URL` and `depends_on: [db]` (1.12.1 drafts Postgres by password) |
| Approval blocks on real pull requests | The plan comments on lab-deploy#49 and #50 | Rendered by the pipeline from the plan: resources by type and reversibility, idle floor before and after (USD 0.06 for `mono-api`'s provenance key; USD 8.63 for `mono-admin`), three exposure changes each, the pull request, head commit and plan run. Found: nothing on the pull request said who asked (1.12.1 adds it to the description the controller writes) |
| A tool-description change | Draft brianboti/lab-deploy#51 (one word added to `propose_configuration`'s description) | CI failed: `test_every_tool_matches_its_pinned_description_checksum` named `propose_configuration` and the command to run; closed unmerged |
| The exit dry run | `scripts/export-app.sh demo DEST --dry-run` on `main` | Every command printed (the Knative-format service, the serving image by digest, the state pull, the uploads bucket), none run |
| The restore drill | `python -m labdeploy.cli restore-drill` | Passed: newest backup 2.0 hours old (0.9 MB), integrity ok, 56 plans, 835 audit events, 2 registered applications, 2 onboarding records; the configuration copy loads (1 application in the file, the others registered in the database); next due by 2027-01-15 |

The second service after 1.12.1 (2026-10-16 UTC; the operator merged brianboti/lab-deploy#52 and applied the foundation:
the custom role 1 to change):

| Step | Run, pull request or plan | Observed |
|---|---|---|
| The merge's apply | Infrastructure apply run 38845880542 (all five roots) | `mono-admin`'s first attempt at 03:31:09 was still refused `secretmanager.versions.destroy` (the role change had not yet reached the apply identity); the workflow's retry 60 seconds later destroyed the failed version, created a new one and updated the `app` user in place (1 added, 1 changed, 1 destroyed): the user and the URL got the same run's password, which 1.12.1 makes certain. The four other roots: no changes |
| Onboarding continues | `onboard_application` (`services/admin`) again | It found the successful apply on its own, published `staging-mono-admin`'s variables, opened and merged lab-monorepo#2 (pinned to 65bde30, 1.12.1) and dispatched the first release |
| First release with a migration | Plan fad21c53, run 38846088674 | `preparing, migrating, deploying, promoting, baking, succeeded`; the migration job `mono-admin-migrate` ran from 03:35:23 to 03:37:19; the approval block said `data-bearing` ("the migration changes the database; a rollback restores traffic only") and the needs floor USD 8.57 a month; bake inconclusive (no traffic) |
| The service reads its database | Authenticated requests to `mono-admin` | `/` answered with the shared code from `lib/` (the repository-root build); `POST /visits` inserted a row through `DATABASE_URL` and counted 1, then 2 (the migration's table, the password from the secret); without a token, 403 |
| The trial database stopped | `gcloud sql instances patch mono-admin --activation-policy=NEVER` | `STOPPED`: from about 03:48 UTC only its 10 GB of storage is billed (about USD 1 a month). Cloud SQL ran about 9 hours of the trial, about USD 0.11 at list price. Retiring it follows the runbook's "Retire a database deliberately", which deletes data and stays with the operator |

The last three, on 2026-10-16 (UTC), after the operator reviewed the drafts:

| Step | Run, pull request or plan | Observed |
|---|---|---|
| The bare repository, one sentence | `onboard_application` (brianboti/lab-bare): lab-deploy#54, lab-bare#3, plan 3a51a703 | The operator set `reviewed: true` and merged the checker's draft (#2); onboarding took 4.0 minutes with no step blocked; the first release served `/` and `/health` signed in, 403 without a token; the image (Dockerfile) is 53.1 MB in Artifact Registry |
| The same repository without a Dockerfile | lab-bare#4 (the Dockerfile and `.dockerignore` removed, a one-line `Procfile` added), `deploy_latest`: plan 8295ca7e | The lint step said there was no Dockerfile to lint; Google's buildpacks built it (`google.python.runtime`); released, `/` and `/health` answered. The image is 203.0 MB, about 3.8 times the Dockerfile's (D28 stands: the Dockerfile stays the default), and its scan found 2 critical and 11 high findings, all fixable |
| The checker on a real project | `propose_configuration` for Sophia's `backend/` | Stopped with "Repository input exceeds 64 KiB": a 241 KB module and a 190 KB test were among the files it reads for hints. Fixed in 1.12.2 (lab-deploy#55): such a file is skipped and named |
| Sophia's copy from `backend/` | sophia#14: `platform.yaml` moved to `backend/` with the same values (reviewed again by the operator), the demo's start command in `backend/Dockerfile`, `backend/requirements.lock` (the backend's own `requirements-prod.txt`), the test tools and four deselects in `backend/pytest.ini`; the seven root files removed. Locally on Python 3.13: 892 passed, 21 skipped, 4 deselected | `lab-deploy unregister sophia`, then `onboard_application` with `path: backend` and the same name: the same service, identities and root ("the root is already on main"); it stopped at the environment step with "GitHub answered HTTP 303": GitHub now answers 303 See Other for a branch policy that exists (it once answered 422). Fixed in 1.12.3 (lab-deploy#56); continued; sophia#15 re-pinned the workflows to `backend/`; 14.2 minutes with 1 block |
| Sophia's first release from `backend/` | Plan a72d97fb | `preparing, deploying, promoting, baking, succeeded` (14:12 to 14:18 UTC after the tests and the build); bake inconclusive (no traffic) |
| lab-notebook brought up to date | `deploy_latest`: plan 6ed47f7d | Its last releases were the 2026-10-13 failure scenarios; the release of its current `main` (Postgres retired, `database: none`) succeeded, so the watchdog's "newest release attempt failed" clears |
| Claude chat can reach the controller | `https://<tunnel>/.well-known/oauth-authorization-server` | 200 through the tunnel, with the controller on 1.12.3 |

Still to do: the review by a second person (Phase 12), once there is one.


## The rename to Fast Deploy and brianboti/fast-deploy (2026-10-16)

Asked for by the operator. 1.14.0 renamed what people see; 1.14.1 prepared the repository's move, done in the order of the
runbook's "Rename the platform repository", every step with the operator's approval of the exact command.

| Step | Observed |
|---|---|
| Sign-in accepts both names | `terraform.tfvars`: `platform_repository = "brianboti/fast-deploy"`, `former_repositories = ["brianboti/lab-deploy"]`; the foundation plan showed 2 to change (the two providers), applied; the apply provider's condition listed both workflow paths |
| The rename | `gh repo rename fast-deploy`: the same repository id (1415112615); `brianboti/lab-deploy` answers with `brianboti/fast-deploy`; the local remote set to the new URL |
| The controller | `platform.repository: brianboti/fast-deploy` with the old name under `former_repositories`; restarted on 1.14.1; all six apps' pins still read and supported through the former name |
| Re-pin under the new name | lab-demo-app#21, lab-notebook#11, sophia#16, lab-monorepo#3 and #4, lab-bare#5 at 65b325a, merged; their workflows check out `brianboti/fast-deploy` |
| The plan side signs in | The drift workflow run by hand (run 38927173627): all six plans and the report succeeded |
| An app release | `lab-bare` (plan 7a35596f) succeeded; its log shows `Syncing repository: brianboti/fast-deploy` |
| Cleanup | The former name removed from the controller and from `terraform.tfvars`; the foundation applied again (2 to change); both providers now name only `brianboti/fast-deploy`; `platform.minimum_commit` raised to 65b325a, every pin supported |
| The apply side signs in | The apply workflow run by hand for `lab-bare` (run 38929179969): "No changes", "Apply complete! Resources: 0 added, 0 changed, 0 destroyed" |


## Live exercise of 1.15.0: a repository as it is (2026-10-16)

Asked for by the operator after the merge of #64; every contract pull request was merged by the operator (the merge is the
review, D32), every infrastructure pull request by the controller under `auto_merge` once its plan passed the policy.

| Step | What was done | Observed |
|---|---|---|
| The merge changes nothing that runs | #64 merged (345b663) | The apply planned all six roots: "No changes" each; `mono-admin`'s database stayed `STOPPED`/`NEVER` (the module's fix in the same PR); the controller restarted on 1.15.0 with the new `contracts` job |
| The reader on real repositories, read-only | `check_repository` (and, before the merge, the reader alone through `gh`) on six of the operator's repositories | `llm-debate-agent`: refused at the root ("the service lives in backend"), ready from `backend/` as `llm-debate-backend`; `track-a-text-rag` and `sequential-finetuning-pipeline`: refused, no web application (batch pipelines); `Conversational-AI-Agent`: refused for its committed `env.prod` and `env.yaml` (never opened; the operator was told to check and rotate); `lab-bare`: ready with its own workflows, refused as it is because it is private while the platform repository is public; `lab-demo-two`: ready, its `platform.yaml` imported as the draft |
| Before the merge, locally | The platform's locks, Dockerfile and test step for `llm-debate-agent/backend` on the operator's Docker | Both locks compiled in containers (1,507 and 1,785 hashes); the platform's Dockerfile built a 199 MB image that started `uvicorn src.app:app` as user 65532 and answered `/health`; the 9 tests passed from the platform's test lock |
| A real repository as it is | `onboard_application` with `brianboti/llm-debate-agent`, `path: backend`, `as_is: true` | Contract pull request #65 in 24 seconds (the contract and both locks); after the operator's merge, infrastructure #68 (37 to add, policy passed, merged by the controller) applied; the environment `staging-llm-debate-backend` created in the platform repository; 9.6 minutes, no block |
| Its first release on the platform's pipeline | Plan 753d9731, `deploy.yml` run 38952366953 | The application checked out from its repository at 884b8c2 beside the platform; tests and audit from the platform's locks; the platform's Dockerfile; **signed in to Google Cloud from the platform repository's `deploy.yml` in `staging-llm-debate-backend`** (the new trust condition); provenance signed with the application's Cloud KMS key and verified as built from `brianboti/llm-debate-agent` by the platform's `deploy.yml`; scan 5 high, none fixable; released and baked; live in 10 minutes; the receipt's origin names the platform run, the application commit and the contract commit (2b9be3a) |
| A repository with a Dockerfile, as it is | `onboard_application` with `brianboti/lab-demo-two`, `as_is: true` | Contract #66 (its bucket and topic carried over), infrastructure #69, environment `staging-lab-demo-two`; 9.4 minutes; the first release (plan 00929401, run 38952511701) built its own Dockerfile (hadolint: no findings), `Provenance verified: ... built from brianboti/lab-demo-two at b09b399490db by brianboti/fast-deploy/.github/workflows/deploy.yml on main`; live in 8 minutes |
| Merging a contract change is a release | #70: `build.recipe: buildpacks` and `build.entrypoint` in `apps/lab-demo-two/platform.yaml`, merged by the operator at 19:13:38 | Within a minute the `contracts` job planned and dispatched it by itself (event `contract_release`, identity `release-on-merge`, plan 4cf00a8a, run 38953456446); a fresh build, since the recipe changed: "Using entrypoint from environment variable GOOGLE_ENTRYPOINT: uvicorn app:app --host 0.0.0.0 --port 8080"; signed and verified; live in 10 minutes, the receipt naming the contract commit of the merge (457fda8). The image's `/workspace` holds no `test_app.py` and no `platform.yaml` (the descriptor applied), but the repository's `Dockerfile` and `.dockerignore`, which 1.15.1 adds to the deny-list; its scan: 2 critical and 11 high, all fixable (the buildpacks' run image, as on lab-bare: D28 stands) |
| Recovery per service | `recover.yml`, started by each deploy's completion | One job per service under `apps/`; `llm-debate-backend`'s ran at once; `lab-demo-two`'s waited in that service's queue behind its running release (`queue: max`), never beside it |

Not exercised live: a release pull request that moves `source.commit` (neither repository's `main` moved), `uv.lock`
export and Compose (no public repository of the operator has them; covered by tests), and the GitHub App token (not set
up; public repositories are read with the run's own token).


## Live exercise of 1.16.0: code nobody has reviewed (2026-10-16)

Asked for by the operator after the merge of #71 (828881d). No contract changed with the merge, so nothing released on
its own: the controller read both kept contracts at the merge commit and left them.

| Trial | What was done | Observed |
|---|---|---|
| The plan says it | `create_deployment_plan` for `lab-demo-two` (a contract the platform keeps) | Two notes ("Code nobody has reviewed: ...", "No internet: ...") and the approval block's `containment`: trust `unknown`, egress `private`, `gen2`, "blocks fixable critical and high findings" |
| The vulnerability gate stops it | Plan faf8bba0, run 38960957930: lab-demo-two's buildpacks build | Every build gate passed (scanners checked against their SHA-256, the checkout's and the image's secret scans, the parts list); the scan found critical 2, high 11, all fixable, and the gate stopped the release before any change: "13 critical or high findings with a fix available, in code nobody has reviewed". The previous revision kept serving |
| It runs contained | Plan f1b2ddbc, run 38960985349: `llm-debate-backend` | Its new revision: `run.googleapis.com/execution-environment: gen2`, `vpc-access-egress: all-traffic`, network `lab-deploy`, subnet `lab-deploy-run`, tag `lab-deploy-contained` (the revision before it had none of these) |
| It reaches no internet | A one-off Cloud Run job `egress-proof` from the same image and runtime identity, with the same network settings, opening `https://example.com/` and `https://storage.googleapis.com/` | "internet: NOT reached (URLError: Network is unreachable)" at once; "Google API (Private Google Access): reached, HTTP 400". The job stays in the project for the next proof; it costs nothing at rest |
| A Dockerfile that breaks the rules is refused | `check_repository` of `brianboti/Conversational-AI-Agent` | As it is: "Dockerfile:1 the final image runs as root" with its fix, beside its committed `env.prod` and `env.yaml` |
| The contained release finishes | Plan f1b2ddbc | Live in 11 minutes; the receipt's `containment` (unknown, private, gen2) and `build_checks`: no working key in the checkout or the image, 14 unconfirmed patterns in the image, all in installed packages and system files (now counted apart, 1.16.1), a parts list of 138 components |
| Every gate on a repository's own Dockerfile | #72 (`apps/lab-demo-two/platform.yaml` back to its Dockerfile), merged by the operator at 20:20:53; the `contracts` job released it at 20:21:57 (plan 693801c2, run 38961646093) | "Dockerfile policy (unknown): passed"; "Secret scan of the checkout (unknown): 0 findings, 0 verified"; "Secret scan of the image (unknown): 6 findings, 0 verified"; "Parts list (SBOM): 103 components, CycloneDX 1.7", attested with the application's Cloud KMS key; provenance verified; the scan: high 5, none fixable, so the gate let it through; released contained in 8 minutes |

Found on the way, fixed in 1.16.1: every first release after the upgrade built again (the reuse check compared raw
dumps that 1.16.0's `build.network` had grown); the answer said "it blocked the release" for releases the gate had let
through; installed packages' patterns were listed one by one; two refusals with the same fix repeated it.

Not exercised: Binary Authorization, a project of its own, data-file inspection and threat detection (Phase 14's open
items, and the operator's switch), and a working key in a build: the gate was checked locally with TruffleHog's own
canary key, which cannot go into a repository.

## 1.17.0 before the merge: what was checked (2026-10-16)

The controller's machine is not created yet: the live move waits for the GitHub App and the foundation apply. What was
checked without them:

| Check | Observed |
|---|---|
| Tests and lint | 583 pass (19 new for Phase 15); ruff clean |
| The foundation root | `terraform fmt -check` and `validate` (Terraform 1.13.5, google provider 7.46.1): valid. The `controller` variable takes `deploy.lab.example.edu` with a full commit (zone defaults to us-central1-a) and refuses a name with an underscore, a short commit and a zone outside us-central1 |
| The pinned tools | Caddy 2.11.7: the SHA-512 in Caddy's own checksum list. Litestream 0.5.17: the SHA-256 in its checksum list, equal to the digest GitHub reports for the asset. Both released more than ten days ago; Litestream's archive has its binary at the top, as the script extracts it |
| Debian 13 on Compute Engine | `debian-13-trixie-v20261014` in the `debian-13` family (Python 3.13); the controller's `requirements.lock` resolves with its hashes for Linux x86-64 on Python 3.13 (`uv pip install --dry-run`) |
| Litestream 0.5.17's configuration | read from its source at the tag: `replica.url` with `gs://` (Google's default credentials, so the machine's metadata server), `sync-interval`, `levels`, `snapshot`, `l0-retention`, `l0-retention-check-interval`; `restore -if-replica-exists -integrity-check quick -o` |
| The copy the move takes | A `VACUUM INTO` copy of a WAL database is in rollback-journal mode and passes `sqlite3 -readonly ... PRAGMA quick_check` without creating files, so the machine checks it before it replaces anything |
| No DNS name (2026-10-17) | Caddy 2.11.7 (`caddy adapt`) takes both Caddyfiles the script writes: for an address, an automation policy for `34.123.45.67` with the `acme` issuer and `profile: shortlived`, `default_sni` set on the connection policies, HTTP/3 off and the admin endpoint disabled. Its certificate library (certmagic v0.25.6) allows IP certificates from Let's Encrypt. Google's automatic name for an address can not be used: `googleusercontent.com` has CAA `issue "pki.goog"` only. The foundation's `controller` variable takes no `domain` and refuses `dns_zone` without one |
| `main`'s protection and the App | `main` requires the `test` and `terraform` checks and no approvals, with no bypass for admins: the controller's merges never relied on an admin's rights, so the App can make them too |

## Live exercise of 1.17.0: the controller off the laptop (2026-10-18 UTC)

Asked for by the operator after the merge of #74 (d2f4bfe), with no DNS name: Claude reaches the machine at its own
address. The operator made the GitHub App and its key; the rest ran from the laptop.

| Trial | What was done | Observed |
|---|---|---|
| The free tier is still free | Every project on the billing account listed | No machine anywhere: the `e2-micro` is the account's one free machine |
| The foundation apply | `controller = { install_commit = d2f4bfe }` in tfvars; `scripts/apply-foundation.sh` | 19 added, 1 changed (the bucket's Litestream rule), 0 destroyed; `fast-deploy` at 34.133.114.245 |
| The machine sets itself up | Its startup script, read from the serial console | About four minutes: Caddy 2.11.7 and Litestream 0.5.17 installed after their checksums, the controller from d2f4bfe, then "the controller waits for its configuration and database" |
| A certificate for the address itself | `openssl s_client` without a server name; `curl` with the system's trust store | Let's Encrypt `YE1`, `IP Address:34.133.114.245`, valid Oct 18 to Oct 24 (160 hours); verified; Google's uptime checker connected without a server name too |
| The GitHub App | `fast-deploy-ai` (App ID 5363822), on all of the account's repositories; key version 1 added by the operator | On the machine: its metadata token, then Secret Manager 200, installation 172563532, access tokens 201; `get_controller_status`: `github_app`, no warning. GitHub's settings now say Credentials, Key pairs, New key |
| The move | `scripts/install-machine.sh 5363822` with the laptop's controller stopped | A 1.4 MB copy with 71 plans and 8 applications, imported through Identity-Aware Proxy; `/healthz` answered; `machine_url` recorded; `CONTROLLER_HEALTH_URL` updated |
| Claude through the address | The connector at `https://34.133.114.245/mcp` | The first attempt failed: the pasted address ended in `)` (Claude asked `/mcp%29`). Added again exactly: registration, sign-in (the audit's `sign_in` from the operator's own address, not 127.0.0.1: Caddy's forwarded address reaches the limits), connected |
| The live copy | The bucket's `machine/` folder | Litestream's files within minutes; the first nightly copy `deployments-20261018-021027.db` |
| The restore drill | `scripts/machine.sh drill` | **Failed, and found a bug**: `gcloud storage ls` lists a folder by its name without the slash (`prefix=machine`), which the bucket condition refused (403; `machine/` gave 200). 1.17.2 matches the prefix without the slash; the drill runs again after its apply |
| A power cycle | `gcloud compute instances reset fast-deploy` | `/healthz` answered again 50 seconds after the reset began; all three services active; the startup script ran again and changed nothing |
| Noise | The serial console | The guest agent's writes to Cloud Logging were refused (no log writer role); 1.17.2 grants `roles/logging.logWriter` |

Not yet: a tool call from Claude through the new connector (the sign-in passed), an onboarding with an App token, and a
restore on a fresh machine.

## Live exercise of 1.17.2: the last checks of Phase 15 (2026-10-18 UTC)

Asked for by the operator, who allowed the machine to be deleted and made again for it.

| Trial | What was done | Observed |
|---|---|---|
| Claude's own call through the address | The operator asked Claude for the controller status | `get_controller_status` from 160.79.106.160 (Anthropic's addresses) at 02:37; the answer named 1.17.2 and the GitHub App |
| The restore drill after the 1.17.2 apply | Foundation apply (2 added, 1 replaced), `scripts/machine.sh update a4e5af3`, `scripts/machine.sh drill` | Passed: the nightly copy (12 minutes old, 71 plans, 1,412 audit events) and the live copy restored with Litestream, integrity ok, 0.0 minutes behind; no refused log writes since the apply |
| An onboarding through the GitHub App | `lab-deploy unregister lab-bare` on the machine (a backup first), then `onboard_application` for `brianboti/lab-bare` | Complete in 0.3 minutes: registered again (the same entry), the root already on main, the `staging` environment and its variables written, the workflows pull request lab-bare#6 opened and merged, all by `app/fast-deploy-ai`; the release it dispatched went live in 11 minutes on the current pipeline (no working keys, 221-part SBOM, the scan's findings reported, as for reviewed code) |
| **What the onboarding showed** | The timing of lab-bare#6 | Merged at 02:53:30 while its `tests` ran until 02:53:42 (they passed): with no required checks GitHub called it mergeable. Fixed in 1.17.3: the controller waits for the pull request's runs and stops at a failed one |
| A restore on a fresh machine | Controller held at 03:05:06; Litestream's last upload 03:05:41; `gcloud compute instances delete fast-deploy`; the foundation apply made it again (1 to add) | The machine was up 43 seconds into the apply and the controller answered 252 seconds later, its startup log saying "restored the configuration from gs://.../machine/config.yaml", "restored the database from the Litestream replica" and the commit from `machine/installed-commit` (a4e5af3). Before and after: 72 plans, the newest the lab-bare release, its onboarding complete; the newest audit event kept was written at 03:05:02, after the snapshot. A new Let's Encrypt certificate for the same address; all three services active |

Phase 15's live exercise is complete: Claude through the address, a power cycle (50 seconds), a restore from Litestream on a
fresh machine, and an onboarding with a GitHub App token.

## Live exercise of 1.18.0 and 1.18.1: production and the first promotion (2026-10-18 UTC)

Phase 16's first two items with the defaults of D37, on the operator's "start them". Production is the project
`fast-deploy-prod-c602` (number 765798408878), created under the organization `mail-mboti-org` on the same billing
account. The organization's own policies were read first: no domain-restricted sharing, so the staging machine's identity
may view production; the others (no service account keys, uniform bucket access, protocol forwarding, zonal DNS, no
automatic grants to default identities) match what the platform does anyway.

| Trial | What was done | Observed |
|---|---|---|
| Staging untouched | `staging.tfvars` in place of `terraform.tfvars`, then a plan of the staging foundation; the plan comments of #78 and #80 | No changes in the foundation and in all eight staging roots. (The staging foundation applied meanwhile also added three pieces of 1.10.0 never applied before: the secret writer role and its two grants.) |
| The production foundation's plan | `scripts/apply-foundation.sh fast-deploy-prod-c602 production`, read-only | **Failed, and found a bug**: the root reads its project through Cloud Resource Manager, which a new project does not have. Enabled by hand once; 1.18.1 makes the script switch it on with Service Usage before planning. Then 104 to add |
| The production foundation | The same script, applied | **Stopped after 95 of 104**: 6 resources (5 custom roles, the key ring's grant) used the IAM and KMS APIs seconds after the same apply enabled them ("has not been used in project ... or it is disabled"). Applied again minutes later: those 6 and the 3 that waited on them, complete. 1.18.2 waits that error out once by itself |
| The pipeline for production | The four `INFRA_PRODUCTION_*` variables; `infra-production` with the operator as required reviewer | The first production plan (#81) ran with production's read-only identity in 26 seconds |
| The machine | `scripts/machine.sh update` to 2b4dae9, then 95d0c0e; the `production:` block through `config-get` and `config-put` | 1.18.1, the configuration loaded without an error |
| `add_production` for `demo`, step 1 | Called on the machine | #81, the production root of `lab-demo`: 43 to add, idle floor +$0.06 a month (its signing key), the only access changes the probe's and the uptime check's invoker grants; policy passed. The controller did not merge it; the operator did |
| Step 2: the apply in `infra-production` | Run 39099847870 waited for its reviewer | The operator's two approvals in the browser never reached GitHub (no approval recorded, while `current_user_can_approve` was true for `brianboti` through `gh`). Approved through the API at the operator's request; `apply-production (lab-demo)` succeeded: the production service at its own address, the placeholder image serving |
| Step 3: the gate | `production-lab-demo` created by the controller, with the eight target variables | Required reviewer `brianboti`, self-review prevented, `main` only. **Found**: administrators could skip the approval, GitHub's default. Closed by hand at once; 1.18.2 sets it, and a promotion refuses while it is open. #82, the staging root's read access for production's deployer: 3 to add (the registry's reader, the key's viewer and public-key viewer), no cost, no access change; merged by the operator, applied by staging's pipeline |
| Step 4 | Called again | "`demo` has its production" |
| The promotion | `promote_to_production` for `demo`: plan `2294ddde`, staging release `25bc6a80` (`0612505`, GitHub's attestation from `lab-demo-app`'s `deploy-gcp.yml`) | Run 39100988038: the describe job passed (inputs, both commits on main, the exact specification read and held to production's limits, the approval block in the summary); the promote job waited in `production-lab-demo`. The browser approval again did not arrive; approved through the API at the operator's request, recorded as `brianboti` |
| What the promote job did | Every step | The approval found on the run; the target equal to the environment's variables; signed in as `lab-demo-deploy@fast-deploy-prod-c602`; no checkpoint to recover; the staging attestation verified (`--source-ref refs/heads/main --source-digest`); the carbon copy kept `sha256:c64563e8…af8`; the verification summary signed with production's key and verified; the release: candidate, probes, 100 percent, bake inconclusive (no traffic). 14 minutes with the wait |
| Production afterwards | `gcloud run services describe`, the registry's tags, `/health` | 100 percent on `lab-demo-r2294ddde545f40e0`; its image is the production registry's copy with staging's digest; tags `release-2294ddde…`, `serving-lab-demo-r2294ddde545f40e0` and `sha256-….att`; `/health` 200 with a token, 403 without |
| The controller's view | `get_deployment_status`, `list_applications` | Succeeded with approval, provenance, copy and health passed; the receipt names the staging release, its image and `brianboti`; the application list shows production's release beside staging's, which stays `25bc6a80` |

**What reading the template showed**: an application's own deploy workflow builds an older commit of main on a run that
started at main's tip, and GitHub's attestation names that tip. A promotion of such a release would have failed its
check (closed, but wrongly). 1.18.2 checks the commit the image's build run started on, following a reused image back to
the run that built it.

Not yet (16.3 to 16.7): the split release and a production rollback by tool, the SLO and its burn-rate alerts, a
domain, the cap-fire drill for production.

## The rollback drill of 1.18.3 (2026-10-18 UTC)

Roadmap item 16.3's live exercise, on the operator's "run the rollback drill", with the procedure of `docs/RUNBOOK.md`.
The demo's own switch made the fault: `FAULT_EVERY: "2"` answers 500 to every second request to `/` while `/health` stays
fine. Every production run was approved through the API with the operator's `gh` login, at the operator's request
(recorded as `brianboti`).

| Trial | What was done | Observed |
|---|---|---|
| The fault in staging | lab-demo-app#22 adds `FAULT_EVERY: "2"`; the auto-deploy released it (plan `0d88b073`) | Succeeded: the probes ask `/health`, and the bake saw no traffic, so nothing in staging could notice |
| Caught at 10 percent | `promote_to_production` (plan `6a842817`, run 39108482628); 400 requests with a sign-in token once the split appeared | The split was 90 to 10; 382 answers 200 and 18 answered 500. The bake failed **69 seconds after the switch: 16 of 31 requests answered 5xx (52%, above 5%)**, and the earlier split came back for everyone. The release step took 1 minute 46 seconds in all, never reached 50 percent, and Claude's answer said "the new version failed its watch with 10% of the traffic" |
| The fix through every step | lab-demo-app#23 removes the fault (the file as it was before #22); staging plan `a3714498`; promotion `947514a7` (run 39168841573) with 300 requests at each step | 10 percent at 21:54:56, 50 at 22:00:01, 100 at 22:05:12; every bake **passed on real traffic** (29, 145 and 300 requests, no server errors); all 900 requests answered 200; 16 minutes for the steps |
| The rollback by tool | `rollback_production` (record `2ef9b87e`, run 39171116225) | Its approval block named both versions by commit (back to `0612505`, away from `dcdbf68`). Approved, the traffic moved back in **16 seconds**: 100 percent on `lab-demo-r2294ddde545f40e0`, the receipt `rolled_back`, and the controller again shows the earlier promotion as production's live release |
| Ending on the fix | Promotion `58bf13c4` (run 39171246733) of the same staging release | Succeeded through 10, 50 and 100 percent (no traffic this time: each step inconclusive, which passes); production serves the fix, and `/` answers 200 |

What the drill showed (1.18.4): a bake that fails at a step of the split said "after promotion"; it now names the switch
("after the switch to 10 percent of the traffic"). The traffic for the drill became `scripts/promotion-traffic.sh`.

## Live exercise of 1.18.5: production's objective (2026-10-19 UTC)

| Trial | What was done | Observed |
|---|---|---|
| The objective proposed | `scripts/machine.sh update 88b9f3a`, then `add_production` for `demo` | #87 adds three lines to the production root (`slo_goal = 0.995`, `uptime_period = "60s"`, the three US regions); its plan: 4 to add (the custom service, the objective, the fast and slow alerts), the uptime check changed in place, reversible, no cost, no access change, policy passed |
| The applies | #86's merge had started its own apply (the shared module changed), whose production job waited in `infra-production` and held #87's in the queue; both approved through the API at the operator's request | #86's run applied every root with nothing to change; #87's applied the objective |
| In production | The Monitoring API | The objective `availability` with goal 0.995 over 2592000 s; the alerts "lab-demo: error budget burning fast" (above 10, `60m`) and "burning slow" (above 2, `1440m`), both enabled and sent to the alert email; the uptime check every 60 seconds from Oregon, Iowa and Virginia |
| The health view | `get_service_health` with `environment: production` | Healthy (72 requests in the hour, no server errors, uptime checks 100 percent), and **"Error budget: spent, 99.36% of requests fine so far"**: the 30-day window holds the rollback drill's planted errors, more than the 0.5 percent a quiet application may miss. The note read "Only -28% ... is left"; 1.18.6 says "is spent, 28% beyond it" |

## Operations of 1.18.8 and the first cap-fire drill (2026-10-19 UTC)

| Trial | What was done | Observed |
|---|---|---|
| The drift check with a production | `infra-drift.yml` dispatched on main (run 39195261059) | `plan-production (lab-demo)` planned with production's own read-only identity: present, no changes. Every staging root clean; the drift issue stayed closed |
| The cap-fire drill, first run | `scripts/cap-drill.sh fast-deploy-prod-c602` at the operator's request, from #90's branch: production's cap at 1.0 of the budget, billing detach off, `lab-demo` open and managed, no database | The made-up notification (100.01 of 100) reached the workflow through Pub/Sub and Eventarc in about a second. The workflow **could not close `lab-demo`**: Cloud Run answered 403, "Permission 'iam.serviceaccounts.actAs' denied on service account lab-demo-runtime@…". The execution failed, as a cap that cannot act must, and `lab-demo` stayed open (403 without a token). The script then stopped on a 503 from the Cloud Run API without running its exit trap |

What it showed: the platform's own cap had never closed a service. The earlier checks ran only its below-cap and failure
paths (sections above). Staging's cap identity holds the same grants, so staging's cap could not close anything either;
Google's native spend cap, tripped live in staging, was the backstop that worked. 1.18.8 lets the cap act as each
application's runtime identity, and makes the drill reopen whatever happens after it publishes.

After the fix:

| Trial | What was done | Observed |
|---|---|---|
| The fix applied | #90 merged (`cd6ad5c`); Infrastructure apply run 39263701581 | Every staging root gained the cap's grant on its own runtime identity. Production's job was approved through the API at the operator's request: one grant, policy passed, applied. An older unapproved run from #89 (no changes) held production's queue and was cancelled |
| The cap-fire drill, again | `scripts/cap-drill.sh fast-deploy-prod-c602` from main, the machine on `cd6ad5c` | **Passed.** The notification went out at 13:55:51, and the cap's own identity changed `lab-demo`'s ingress at 13:55:52 (audit log). The drill saw it internal-only at 13:55:56 and 404 from outside. The execution SUCCEEDED: "cap enforced at 1.0001 of the budget: 1 services closed, 0 databases stopped". The drill reopened it at 13:55:58 (closed about six seconds), and it answered 403 without a token again. No new revision (100 percent on `lab-demo-r58bf13c4ade44389`), and no second delivery a minute later |
| Production in the weekly cost report | `add_production` for `demo` once more | `PRODUCTION_PROJECT_ID = fast-deploy-prod-c602` on `lab-demo-app`'s staging environment, nothing else changed. lab-demo-app#24 re-pins its workflows to `cd6ad5c`; the report shows production's spend once that is merged |

Not yet: a release tag (written at the next promotion), and the failure alert's email from the first drill's failed
execution (13:25 UTC), which is the operator's to confirm.

