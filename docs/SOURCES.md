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
