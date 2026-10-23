# Accounts, repositories and deployment from the browser

Open **Configure → Connections** at `/settings?tab=connections`. Only a Fast Deploy operator with access to all applications and permission to deploy can use this screen. Other users keep their existing Monitor access.

## Everyday use

The screen uses plain English and three expandable steps. The relevant step opens after each action. Optional fields and administrator setup stay collapsed until needed.

1. **Choose your Google account** — connect an account, select a project and choose **Check this project**. Projects load automatically after sign-in. Review the selection, then save. A saved project can be reused without signing in again.
2. **Choose GitHub access** — load the available code, select what this app may access and choose **Review and save access**. GitHub account grants still happen on GitHub through the labelled link. The platform repository remains required. Saving offers a button to continue to the app check.
3. **Check and launch** — choose the app code or paste its GitHub link, then **Check app**. The default setup option, “Standard app — initial approval needed”, needs an initial human approval. The app name, app folder and the other setup option, “App already prepared for deployment”, are under **More options**. After a successful check, review the summary and choose **Start app setup**. The status page explains any approval needed and links to the GitHub proposal.

Confirmation and saved-result screens show short summaries with exact configuration changes available under **Technical details**. Provider diagnostics remain available under **View error details**, alongside a plain explanation. They never include provider response bodies or credentials.

“Standard app — initial approval needed” leaves the app's repository unchanged and creates a contract proposal in the platform repository. A person must merge that proposal before deployment continues. “App already prepared for deployment” uses the repository's own reviewed deployment settings, through the existing onboarding process and its auto-merge policy. The browser does not bypass checks, approval separation, paused releases, or the production approval process.

The Google account chooser selects a project; it does not replace the deployment service accounts. Browser Google tokens are read-only, kept only in server memory, and expire within an hour. They are removed on disconnect, sign-out, or server shutdown. A saved destination remains selected after the browser connection expires. Connecting an account here does not make it a viewer of deployed apps; use each application's **Who may open its pages** setting for that.

The destination is for **new staging applications**. Existing applications retain their registered target. Choosing a different project keeps the previous region, unless that project's Terraform state bucket sits in one region, which is then used. Production remains separate. Switching the shared foundation is an administrator operation: finish that setup before choosing a different default project. This page does not migrate existing services or create a billing account, a project, or the foundation.

## One-time Google sign-in setup

This is separate from the existing Claude connector's OAuth server.

1. Use a stable public HTTPS hostname for the controller and configure `oauth.public_url` and `allowed_hosts` as in [SETUP.md](SETUP.md). Register a **Web application** OAuth client in Google Auth Platform. Configure its consent audience and test users or publishing status for the people who will use it.
2. Add this exact authorized redirect URI, replacing the origin with your controller's origin:

   ```text
   https://deploy.example.org/settings/connections/google/callback
   ```

3. Enable the Cloud Resource Manager API for the OAuth client's project. The selected destination's billing, IAM and Storage APIs must also be available. Enter the client ID under **Connections → Connection setup → Google web client ID** and save it.
4. Supply the client secret to the running server as `LAB_GOOGLE_CLIENT_SECRET`, using a protected environment file or the host's secret management. Restart the server. Do not put the secret in `config.yaml`, source control, a browser form, or chat. For a systemd installation, use an administrator-owned environment file with restricted permissions and a service `EnvironmentFile` override; keep the override separate from generated unit files.

The requested scopes are `openid`, `email`, `cloud-platform.read-only`, and `cloud-billing.readonly`. There is no offline refresh token. The account needs permission to see the intended project and its billing-enabled status. Google may require consent-app verification or test-user registration depending on your audience and scopes.

For foundation checks, the **server's existing Google identity** reads the two infrastructure service accounts and Terraform state bucket. IAM's service-account endpoint does not accept the browser's read-only cloud scope, so the browser never requests full cloud management access merely to select a project. The server must already be authorized to read those resources:

- **The service accounts.** The foundation grants the custom role `labDeployFoundationReader` (one permission, `iam.serviceAccounts.get`) on `lab-deploy-infra-plan` and `lab-deploy-infra-apply` only: no keys, no policies, no other account. In the controller's own project it goes to the machine's identity (`lab-deploy-controller`).
- **The state bucket.** The machine's observer roles include `roles/storage.bucketViewer`, which reads the bucket's settings, never its files.

**Another project** needs the same access before it can be chosen: add the controller machine's identity, `serviceAccount:lab-deploy-controller@CONTROLLER_PROJECT.iam.gserviceaccount.com`, to that project's `viewer_members` (its foundation's tfvars) and apply its foundation (`scripts/apply-foundation.sh`). That grants both. Until then **Check this project** says that the server's own Google identity has no access to that project; the browser account is not the problem.

Before accepting a different default project, Fast Deploy also verifies all four platform repository variables against that project's foundation:

- `INFRA_PLAN_SERVICE_ACCOUNT`
- `INFRA_APPLY_SERVICE_ACCOUNT`
- `INFRA_PLAN_WIF_PROVIDER`
- `INFRA_APPLY_WIF_PROVIDER`

This needs **Variables: read** on the platform repository. An existing GitHub App without that permission can still discover repositories and deploy to its current destination; an administrator must grant it before changing the default project in this screen. The check verifies billing, foundation resources, and destination variables; each deployment still verifies the complete target, workflow trust and runtime checks.

## GitHub setup and the local repository selection

The controller continues to use the GitHub App described in [SETUP.md, section 4](SETUP.md#4-github-credentials-for-the-controller). The account and repository grant is made on GitHub's own installation screen. Once created, its App ID and Secret Manager key resource can be entered under **Connection setup**, without editing YAML. Changing the App requires a controller restart. The form never accepts a private key.

`GITHUB_TOKEN` on the host still overrides the GitHub App. The screen labels that mode and lists repositories visible to that token. Seeing a repository does not prove permission to change workflows or create environments; the existing per-operation checks still apply.

Repository discovery is paginated. A GitHub App inventory uses short-lived metadata-only installation tokens, revokes them after discovery, and never reuses them for release operations. Refresh also clears cached installation grants so newly installed repositories can be used immediately.

The optional `connections.repositories` list is a second restriction on the controller, not a replacement for GitHub permissions:

- Absent or `null`: preserve the existing credential's access, including public repository reads.
- A list: permit only those repositories, plus the configured platform repository, which remains required.
- `[]`: permit the platform repository only.

The restriction stops new work in a repository that is not chosen: checking it, onboarding it, importing its contract, and new plans, releases, promotions and workflow-update pull requests for an application registered from it, including applications whose pipeline lives in the platform repository. An application already registered keeps working for everything else: its status, the dashboard, the follow job that records how a release ended, cancelling a run, and rolling back in staging or production. The controller still reads that repository for those. It applies after saving without restarting. It does not cancel running GitHub workflows, stop deployed services, or revoke grants on GitHub. Use GitHub's **Manage access** link to change the upstream grant itself.

## Review and session boundaries

Settings changes use the existing validated review, atomic save, configuration-conflict detection, history, backup and audit mechanism. Application setup has its own ten-minute, single-use review bound to the operator's browser session and the configuration hash. Forged repository, project or automation values submitted to the start button are ignored; it uses the values that were reviewed. A changed configuration requires another repository check.

A failed new check also invalidates the previous review. The review shows the resolved application name and setup option. Continuing an existing setup keeps its saved automatic-release preference and registered destination; it does not create a second setup. After editing the form, check it again before starting. Google project discovery runs immediately after sign-in, with a manual refresh available if the provider is temporarily unavailable.

The Google authorization request uses a random state and PKCE. A short-lived, signed, HttpOnly, Secure, SameSite=Lax callback cookie binds the Google return to the existing session. The normal settings cookie remains SameSite=Strict. The callback checks that the original operator session is still valid and authorized. Its completion page links back to Connections so browsers can resume that Strict session. Provider errors are shown without response bodies, tokens, authorization codes or secret contents.

## Verification and references

`tests/test_connections.py` exercises mocked Google and GitHub HTTP flows, pagination, partial consent, expired/replayed state, session isolation, repository restrictions, forged forms, configuration changes during review, and starting an application only once. Settings and onboarding tests cover the existing save and deployment paths. These tests do not grant real account permissions or deploy cloud resources.

Provider protocols: [Google web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server), [Google project search](https://docs.cloud.google.com/resource-manager/reference/rest/v3/projects/search), [billing status and scopes](https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/getBillingInfo), [IAM service-account read scopes](https://docs.cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/get), and [GitHub installation repository discovery](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation).
