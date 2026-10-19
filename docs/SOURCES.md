# Official documentation consulted

Read on 2026-10-12 UTC before implementation; check again when upgrading dependencies. Architecture choices below are project decisions, not claims that a vendor prescribes this entire platform.

| Official source | What it informed |
|---|---|
| [Claude remote connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) | Hosted remote MCP, request-header credentials (beta) and individual access |
| [Add a custom connector](https://claude.com/docs/connectors/custom/add-unlisted) | Connector dialog: sign-in options, request headers beta, callback URL |
| [Authentication for connectors](https://claude.com/docs/connectors/building/authentication) | Claude's OAuth client requirements: 401 with resource metadata, DCR or CIMD, PKCE S256, callback URLs, refresh rules |
| [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and [v1 docs](https://py.sdk.modelcontextprotocol.io/v1/) | FastMCP, Streamable HTTP, explicit v1 compatibility pin |
| [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) | Distinguishing a full OAuth resource server from this pilot's fixed-header authentication |
| [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event) | Branch reference, typed inputs, Actions write permission and returned run identifiers |
| [GitHub PR API](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request) | Draft configuration pull requests |
| [GitHub file contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents) and [Git refs API](https://docs.github.com/en/rest/git/refs#create-a-reference) | Create-only config proposals on new branches |
| [GitHub OIDC for GCP](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform) | Short-lived cloud authentication from Actions |
| [GitHub secure use reference](https://docs.github.com/en/actions/reference/security/secure-use) | Commit-pinned actions, least privilege and safe input handling |
| [Google's auth action](https://github.com/google-github-actions/auth) | Workload federation, checkout order, credential-file exclusions |
| [GCP WIF deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) | Restricting trust using numeric repository and owner IDs |
| [Cloud Run deployment](https://docs.cloud.google.com/run/docs/deploying) | Container image deployment and digest identity |
| [Cloud Run rollout and rollback](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration) | Candidate tags, no-traffic deployment and explicit revision allocation |
| [Cloud Run service authentication](https://docs.cloud.google.com/run/docs/authenticating/service-to-service) | Audience-bound identity tokens and invoker role |
| [Generate ID token API](https://docs.cloud.google.com/iam/docs/reference/credentials/rest/v1/projects.serviceAccounts/generateIdToken) | Short-lived health-probe token without a service-account key |
| [Cloud Run IAM inspection](https://docs.cloud.google.com/sdk/gcloud/reference/run/services/get-iam-policy) | Rejecting service-level public grants before release |
| [Terraform GCS backend](https://developer.hashicorp.com/terraform/language/backend/gcs) | Remote state, existing bucket, locking and versioning |

Action commit references in the workflow were resolved from the corresponding official repositories through GitHub's Git reference API during implementation. Terraform 1.13.5 was obtained from HashiCorp's official release site and verified against its SHA-256 checksum file. Google provider 7.46.1 was downloaded and verified by Terraform and is recorded in `.terraform.lock.hcl`.


Additional official references checked for the fixes:

- [Workflow run jobs/attempts](https://docs.github.com/en/rest/actions/workflow-jobs)
- [GitHub pending-run queue behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [GCS object insert and generation preconditions](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert)
- [GCS object metadata and generation-specific reads](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/get)


Official references for the cost optimizer (0.2.0), read on 2026-10-11:

- [Cloud Run pricing](https://cloud.google.com/run/pricing): Tier 1 request-based rates, idle minimum-instance rates, instance-based rates and the monthly free tier recorded in `labdeploy/cost.py`
- [Best practices for cost-optimized Cloud Run services](https://docs.cloud.google.com/run/docs/tips/services-cost-optimization): billing-mode choice, sizing from utilization, concurrency, maximum instances, authentication, Tier 1 regions, budgets and quotas
- [About maximum concurrent requests per instance](https://docs.cloud.google.com/run/docs/about-concurrency): default 80, maximum 1,000, when to lower or raise
- [Optimize with Recommender](https://docs.cloud.google.com/run/docs/recommender) and [Recommenders](https://docs.cloud.google.com/recommender/docs/recommenders): `google.run.service.CostRecommender`
- [gcloud recommender recommendations list](https://docs.cloud.google.com/sdk/gcloud/reference/recommender/recommendations/list)
- [Analyze your application using metrics (Cloud Deploy)](https://docs.cloud.google.com/deploy/docs/deploy-app-analysis): Google's own uptime check for a Cloud Run service with service-agent OIDC authentication, naming the service as a `cloud-run-revision` resource with `project_id`, `location` and `service_name`; the shape `infra/gcp/modules/service` uses
- [Google Cloud metrics, `run.googleapis.com`](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z): metric kinds, value types and units used by `labdeploy/telemetry.py`
- [Monitoring API `projects.timeSeries.list`](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list) and [Aligner/Reducer reference](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies#Aligner)
- [Terraform `google_artifact_registry_repository`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/artifact_registry_repository): cleanup policies
- [Terraform `google_billing_budget`](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/billing_budget)
- [Cloud Run IAM roles](https://docs.cloud.google.com/run/docs/reference/iam/roles): `roles/run.viewer` includes the Cloud Run recommender permissions, confirmed with `gcloud iam roles describe`


Official and primary references for the 0.3.0 hardening review, read on 2026-10-12:

- [MCP specification: Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools): annotations, structured output, and the server requirement to rate limit tool invocations
- [OWASP MCP Top 10](https://owasp.org/www-project-mcp-top-10/) and the [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/resources/)
- [Microsoft: Least privilege for AI agents](https://www.microsoft.com/en-us/security/blog/2026/07/16/least-privilege-for-ai-agents-identity-access-and-tool-binding/)
- [AWS Builders' Library: Automating safe, hands-off deployments](https://builder.aws.com/content/3ErTKQOTKc5NIw031UePBPxTQ6I/automating-safe-hands-off-deployments)
- [Google SRE Workbook: Canarying releases](https://sre.google/workbook/canarying-releases/)
- [Microsoft Azure: Advancing safe deployment practices](https://azure.microsoft.com/en-us/blog/advancing-safe-deployment-practices/)
- [Cloud Run container health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks): `--startup-probe` syntax and defaults
- [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds) and [gh attestation verify](https://cli.github.com/manual/gh_attestation_verify)
- [Binary Authorization for Cloud Run](https://docs.cloud.google.com/binary-authorization/docs/run/enabling-binauthz-cloud-run): the production enforcement step not yet adopted
- [Artifact Analysis automatic scanning](https://docs.cloud.google.com/artifact-analysis/docs/scan-os-automatically) and [pricing](https://cloud.google.com/artifact-analysis/pricing)
- [zizmor](https://docs.zizmor.sh/) and [Grafana's adoption write-up](https://grafana.com/blog/2025/06/25/how-to-detect-vulnerable-github-actions-at-scale-with-zizmor/)
- [Litestream](https://litestream.io/how-it-works/): the planned control-plane backup approach

References checked during the live staging trial (2026-10-12):

- [GitHub: OpenID Connect token claims](https://docs.github.com/en/actions/concepts/security/openid-connect): the `sub`, `environment`, `repository_id`, `repository_owner_id` and `workflow_ref` claims used by the trust condition; the subject format observed live embeds numeric IDs
- [Cloud Run: managing revisions](https://docs.cloud.google.com/run/docs/managing/revisions): the newest revision of a service cannot be deleted
- [Cloud Run troubleshooting: container failed to start](https://cloud.google.com/run/docs/troubleshooting#container-failed-to-start): the startup-probe failure observed for the deliberately unhealthy candidate

References read for the Phase 6 research, "everything an app needs" (2026-10-12):

- [Score specification reference](https://docs.score.dev/docs/score-specification/score-spec-reference/): the `resources` block, placeholders and provisioning order the `platform.yaml` design borrows
- [Application Design Center: design application templates](https://docs.cloud.google.com/application-design-center/docs/design-application-templates?hl=en) and [Cloud Run connections](https://docs.cloud.google.com/application-design-center/docs/configure-cloud-run): Google's component model and the role and variable each connection sets
- [Infrastructure Manager overview](https://docs.cloud.google.com/infrastructure-manager/docs/overview): Google's managed Terraform apply, evaluated and not chosen for the pipeline
- [Terraform best practices: root modules](https://docs.cloud.google.com/docs/terraform/best-practices/root-modules), [operations](https://docs.cloud.google.com/docs/terraform/best-practices/operations) and [general](https://docs.cloud.google.com/docs/terraform/best-practices-for-terraform): state size, one directory per application, plan before apply, pipelines with Workload Identity Federation, deletion protection
- [Terraform policy validation](https://docs.cloud.google.com/docs/terraform/policy-validation/validate-policies): `gcloud beta terraform vet` in the pipeline
- [Connect to Cloud SQL from Cloud Run](https://docs.cloud.google.com/sql/docs/postgres/connect-run): connectors, the Cloud SQL Client role, Secret Manager for credentials, connection limits
- [Start, stop and restart a Cloud SQL instance](https://docs.cloud.google.com/sql/docs/postgres/start-stop-restart-instance): what a stopped instance still costs
- [Cloud Run jobs](https://docs.cloud.google.com/run/docs/create-jobs): the migration and worker shape
- [Cloud Storage volume mounts for Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts): the file-storage option and its limits

References read for 1.1.0, buckets, topics and Firestore (2026-10-12):

- [Cloud Storage pricing](https://cloud.google.com/storage/pricing): Standard storage in us-central1 and the Always Free 5 GB in US regions, in `labdeploy/cost.py`
- [Pub/Sub pricing](https://cloud.google.com/pubsub/pricing): the free 10 GiB a month and the per-TiB delivery rate
- [Firestore pricing](https://cloud.google.com/firestore/pricing): Standard edition per-operation prices, the daily free quota and the one free database per project

References read for 1.4.0, Cloud SQL and migrations (2026-10-12):

- [Cloud SQL pricing](https://cloud.google.com/sql/pricing) and the [db-f1-micro listing](https://www.bytebase.com/dbcost/cloudsql/instance/db-f1-micro/): the shared-core instance, HDD storage and backup rates in `labdeploy/cost.py`
- [Connect from Cloud Run to Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/connect-run): connectors, private IP through VPC egress, the Cloud SQL Client role, the 100-connection limit per instance
- [Cloud Run jobs](https://docs.cloud.google.com/run/docs/create-jobs): the migration job, `--max-retries` and task timeouts
- [Private Service Access](https://docs.cloud.google.com/vpc/docs/configure-private-services-access) and [Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc): the network the foundation creates

References read for the strict review and its fixes, 1.5.0 (2026-10-12):

- [Workload Identity Federation attribute conditions](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) and [GitHub's OIDC token claims](https://docs.github.com/en/actions/concepts/security/openid-connect): `event_name`, `ref`, `environment` and `workflow_ref` pinned per provider in `infra/gcp/modules/project/pipeline.tf`
- [IAM Conditions for Cloud Storage](https://docs.cloud.google.com/storage/docs/access-control/iam#conditions): `resource.name` prefixes for objects and `resource.type` for bucket-level operations, used to scope the pipeline's state access
- [Cloud Storage IAM roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles) and [basic roles guidance](https://docs.cloud.google.com/iam/docs/understanding-roles#basic): why `roles/viewer`, `roles/editor` and `roles/storage.admin` left the pipeline identities
- [Workflows syntax: retries, try/except, loops and expressions](https://docs.cloud.google.com/workflows/docs/reference/syntax) and [Workflows metrics](https://docs.cloud.google.com/workflows/docs/monitoring): `http.default_retry`, per-resource error handling and the `finished_execution_count` metric behind the cap's failure alert
- [Budget notification format](https://docs.cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications): `costAmount` and `budgetAmount` carry the ratio; `alertThresholdExceeded` is only the highest budget rule reached
- [gcloud run jobs update](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/update): `--args` with an empty value resets the arguments; there is no `--clear-args`
- [Firestore pricing](https://cloud.google.com/firestore/pricing): no free quota for named databases; regional storage price
- [SQLite write-ahead logging](https://www.sqlite.org/wal.html): concurrent readers with one writer for the controller's database
- [uvicorn settings: proxy headers](https://uvicorn.dev/settings/): `FORWARDED_ALLOW_IPS` for the container path behind a reverse proxy
- [GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches), [deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) and [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners): the review gate the pipeline's apply identity relies on
- [Dependabot `directories`](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference#directories-or-directory--) and [scheduled workflows](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#schedule): the Terraform roots watched and the weekly audit

References read for the repository standard, `docs/DEVELOPER-CONTRACT.md` (2026-10-13):

- [Cloud Run container runtime contract](https://docs.cloud.google.com/run/docs/container-contract), [general development tips](https://docs.cloud.google.com/run/docs/tips/general), [Python tips](https://docs.cloud.google.com/run/docs/tips/python), [health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks) and [logging](https://docs.cloud.google.com/run/docs/logging): what a container must do on Cloud Run
- [The Twelve-Factor App](https://12factor.net/): the behavioural rules behind sections 2 and 3 of the standard
- [Docker: building best practices](https://docs.docker.com/build/building/best-practices/) and [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md): the repository and image hygiene the standard requires or recommends
- [GitHub community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file): the files a repository should carry for people


References read for 1.9.0, cost and safety layers that cost nothing (2026-10-14):

- [Budget spend caps (Cloud Billing)](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps) and [the Budget API for spend caps](https://docs.cloud.google.com/billing/docs/how-to/budget-api-spend-cap): eligible services, one project and one service per budget, monthly only, gross estimated cost, what is paused and what keeps running, lifting, `spendCap.inputState`; [Cloud Run billing settings, spend-cap section](https://docs.cloud.google.com/run/docs/configuring/cpu-allocation): services answer 5xx, VPC connector VMs not covered; [Cloud Run release notes](https://docs.cloud.google.com/run/docs/release-notes): Preview 2026-07-27, generally available 2026-10-12. The Terraform Google provider's release notes to 8.7.0 carry no spend-cap argument.
- [Cloud Run logging](https://docs.cloud.google.com/run/docs/logging): the request log (`run.googleapis.com/requests`, `httpRequest.status`, `latency`, `requestUrl`) and the system log (`run.googleapis.com/varlog/system`) the bake reads; the message severities were read from the staging project's own logs.
- [Making Deployments Safe at Meta: Health Checks for Continuous Change-Safety](https://arxiv.org/abs/2608.20513) (ISSRE 2026 industry track): default checks on crash counts and error ratios, the false-positive rate a year of tuning brought from 12.1% to 2.7%.
- [hadolint releases](https://github.com/hadolint/hadolint/releases): v2.15.1, the `hadolint-linux-x86_64` asset's SHA-256 as GitHub records it, checked against a download.
- [Terraform: sensitive data in state](https://developer.hashicorp.com/terraform/language/state/sensitive-data): state and plans hold secrets in plain text; write-only arguments and ephemeral resources keep them out.
- [Assessing the adoption of security policies by developers in Terraform](https://arxiv.org/abs/2308.03952) (Empirical Software Engineering, 2025): encryption and data protection are the most neglected settings; [Security smells in IaC: a taxonomy update](https://arxiv.org/abs/2509.18761).
- [GitHub Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing) and [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): USD 0.006 a minute on the standard Linux runner, each job rounded up to the minute, 2,000 included minutes a month on GitHub Free, public repositories free.
- [Workload Identity Federation with deployment pipelines](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) and [GitHub's OIDC token claims](https://docs.github.com/en/actions/concepts/security/openid-connect): the `event_name`, `ref` and `workflow_ref` claims that admit the drift workflow from `main` only.

References read for 1.20.4, after a release (2026-10-19):

- [systemd.path](https://www.freedesktop.org/software/systemd/man/latest/systemd.path.html): `PathExists=` starts the service when the request file exists, also when it existed before; when the service ends the paths are checked again and it starts again while the condition holds (hence the request is removed first), with start and trigger limits against a loop.
- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) and [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html): `Type=oneshot`; with the default `RestartMode=` a restart passes through a failed or inactive state, and "A new ID is assigned each time the unit changes from an inactive state into an activating or active state" (the update's watch); `ProtectSystem=strict` and `NoNewPrivileges=` keep the controller from restarting itself.
- [Compute Engine: Linux startup scripts](https://docs.cloud.google.com/compute/docs/instances/startup-scripts/linux): run as root at each boot; `sudo google_metadata_script_runner startup` runs one again; the log in `journalctl -u google-startup-scripts.service`.
- [Identity-Aware Proxy TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding): `--tunnel-through-iap`, the range 35.235.240.0/20, no SSH open to the internet.
- [GitHub: list workflow runs for a workflow](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow): the `head_sha` and `event` filters that pick the run the merge itself started; [GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token): pull requests opened with a GitHub App installation token start the repository's workflows.
- [Google SRE workbook, Canarying releases](https://sre.google/workbook/canarying-releases/): a small part first, time to judge it, automatic rollback, one change at a time (the controller before the applications).
- [Dependabot `cooldown`](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference#cooldown-): delaying updates on purpose (three days by default for third-party packages; an hour here for the lab's own reviewed code).

References read for 1.20.5, the weekly pin refresh (2026-10-19):

- [GitHub REST API: release assets](https://docs.github.com/en/rest/releases/assets) and [releases expose digests for release assets](https://github.blog/changelog/2025-06-03-releases-now-expose-digests-for-release-assets/) (2025-06-03): each asset's `digest`, a SHA-256 GitHub computes at upload, with its `state`, `updated_at` and `browser_download_url`; a release's `published_at`, `draft`, `prerelease` and `immutable`. Checked on 2026-10-19: every pinned SHA-256 equals GitHub's digest, and each project's own checksum list (TruffleHog, Trivy, OSV-Scanner, hadolint, pack, Cosign, OpenTofu, Caddy's SHA-512, Litestream) says the same.
- [Docker Hub API](https://docs.docker.com/reference/api/hub/latest/): reading a repository tag and listing tags (`digest`, `tag_last_pushed`, `images`, as answered on 2026-10-19); [`docker buildx imagetools inspect`](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/): the index digest is the top "Digest:" line; [Docker Hub pulls](https://docs.docker.com/docker-hub/usage/pulls/): a GET of a manifest counts as a pull and a HEAD does not.
- [Google Cloud's buildpacks builders](https://docs.cloud.google.com/docs/buildpacks/builders): `google-22`, `google-24` and `latest` (= `google-24`) and the languages each offers; gcr.io's `tags/list` answers with each image's tags and upload time, and each builder's configuration carries `io.buildpacks.stack.id` (`google.22`, `google.24`).
- [PyPI Index API](https://docs.pypi.org/api/index-api/) (PEP 691 JSON: `hashes.sha256`, `upload-time`, `yanked`) and [PyPI JSON API](https://docs.pypi.org/api/json/) (its `releases` key is deprecated in favour of the index); [uv's versioning policy](https://docs.astral.sh/uv/reference/policies/versioning/): the minor version carries breaking changes.
- [The npm registry's package metadata](https://github.com/npm/registry/blob/main/docs/responses/package-metadata.md): `time` per version, `dist.integrity`, and the abbreviated document's lack of `time`.
- [Dependabot's supported ecosystems](https://docs.github.com/en/code-security/dependabot/ecosystems-supported-by-dependabot/supported-ecosystems-and-repositories) and [options reference](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference): no custom or regular-expression dependencies; `cooldown` (three days by default).
- [Renovate's regex manager](https://docs.renovatebot.com/modules/manager/regex/), [`minimumReleaseAge` and `postUpgradeTasks`](https://docs.renovatebot.com/configuration-options/) and [the github-release-attachments source code](https://github.com/renovatebot/renovate/blob/main/lib/modules/datasource/github-release-attachments/index.ts): Renovate could follow these pins and wait seven days, but finds a new checksum by downloading small assets as checksum lists or hashing the file itself, never by reading GitHub's digest; post-upgrade commands stay blocked by default on Mend's hosted app.
- [Triggering a workflow from a workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow), [Actions settings for a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository) and [a GitHub App in a workflow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow): a pull request opened with `GITHUB_TOKEN` starts its runs only after approval, a personal account's repository does not let Actions create pull requests by default, and an App's installation token (with `client-id` in `actions/create-github-app-token` v3) starts CI normally; [variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables): environment-level variables exist only once the job runs.
- [Trivy's advisory GHSA-69fq-xp46-6x23](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23): the malicious 0.69.4 of 2026-03-19, up for about three hours, the case for a seven-day wait.
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): free on standard runners for public repositories; 2,000 included minutes a month on GitHub Free for private ones.
