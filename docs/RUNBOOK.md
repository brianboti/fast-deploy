# Runbook

How Lab Deploy is operated day to day and what to do when something is off. Written 2026-10-12 for the staging
platform (`lab-deploy-staging-f29a`, operated from Claude chat by one person); update it when the setup changes.

## Owner and escalation

| Role | Who | How |
|---|---|---|
| Owner and only operator | Brian Boti | Claude chat (the connector), the terminal on the laptop that runs the controller |
| Alerts | the `alert_email` and optional `alert_webhook_url` in `infra/gcp/project/terraform.tfvars`; GitHub's failed-workflow emails; the `NOTIFY_WEBHOOK_URL` secret on each app repository | arrive without anyone watching |
| Escalation | nobody else today | when a second operator joins, add them to `principals`, `observer_impersonators` and `.github/CODEOWNERS`, and require a reviewer on the `infra` environment |

There is no service level objective: this is a staging platform for a lab. The targets below are what the platform is
built to do, and the watchdog reports when it does not.

## What "up" means

- `GET /healthz` on the controller answers 200 through the tunnel. The platform repository's **Controller watch**
  workflow calls it every thirty minutes and fails loudly when it does not answer (`CONTROLLER_HEALTH_URL`).
- A release reaches `succeeded` or a final failure within about twenty minutes of a plan being dispatched; the status
  tool names the phase it is in and the next action.
- The daily watchdog finds nothing (`get_controller_status` shows its last findings), the nightly backup is less than
  a day old and the copy in the backup bucket matches.
- Every alert policy is enabled in Cloud Monitoring and reaches a channel (section "Prove the alerts work").

## The laptop is off or asleep

What keeps working without the controller: every workflow already dispatched on GitHub (a release finishes or restores
traffic on its own), the recovery workflow every six hours, the weekly right-sizing report, the spending cap in Google
Cloud, every Cloud Monitoring alert. What stops: Claude's tools (the connector reports a connection error), auto-deploy,
the daily watchdog, the nightly backup. Nothing is lost: plans expire, and the next start continues.

1. Wake the machine and run `scripts/start.sh` (or install the launch agent once with
   `scripts/install-launch-agent.sh`, after which the controller starts at login and restarts after a crash).
2. Keep the machine awake while it is the controller: System Settings → Energy, or `caffeinate -s` in the terminal.
3. Ask Claude for `get_controller_status`: the tunnel, the configuration, the schedules and the last backup.

## Restore the controller on another machine

Rehearsed on 2026-10-12 (`docs/VALIDATION.md`). What the backup bucket holds: the nightly database copy and the
configuration file (principals as hashes, targets, settings). What it does not hold, by design: the operator token's
clear text, the GitHub token, the ngrok credentials.

1. Install Python 3.12 or newer, Docker (for foundation applies), the Google Cloud CLI and ngrok; sign in with
   `gcloud auth login` as the project owner.
2. `git clone https://github.com/brianboti/lab-deploy ~/Downloads/lab-deploy && cd ~/Downloads/lab-deploy &&
   python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements.lock`.
3. Fetch the newest copies: `gcloud storage ls gs://PROJECT-lab-deploy-backups/controller/` and
   `gcloud storage cp gs://PROJECT-lab-deploy-backups/controller/deployments-STAMP.db ~/.local/share/lab-deploy/deployments.db`
   plus `.../controller/config.yaml` to `config.local.yaml` in the repository. Check `database:` in the file points at
   the restored copy.
4. Mint a new operator token: `python -m labdeploy.cli token` prints a token and its hash once; put the hash in
   `principals[].token_sha256` (every earlier sign-in ends with the old hash) and keep the token in
   `~/.local/share/lab-deploy/operator-token.json` with mode 600.
5. Run `scripts/set-domain.sh YOUR-DOMAIN.ngrok-free.dev` with the same ngrok domain (the Claude connector keeps
   working) or a new one (edit the connector); `ngrok config add-authtoken` first on a new machine.
6. Run `scripts/start.sh`: it asks for a fine-grained GitHub token (Contents read, Actions write on the app
   repositories; Contents read on the platform repository) at the Keychain's hidden prompt, stores it in the login
   Keychain and starts the controller and the tunnel. `scripts/install-launch-agent.sh` makes that happen at login.
7. In Claude, open the connector and sign in with the new operator token. `list_applications` and `get_controller_status`
   confirm the restored state; `list_plans` shows history up to the last backup.

## The spending cap fired

How you know: the budget emails, the Workflows execution log (`gcloud workflows executions list lab-deploy-spending-cap --location us-central1`),
`get_service_health` reporting ingress `INGRESS_TRAFFIC_INTERNAL_ONLY`, requests failing with 404 from Google's front end,
and, if any service or instance could not be closed, the alert **Lab Deploy: spending-cap workflow failed**.

1. Decide whether the spend is expected. The cost report (`get_cost_report`) and the billing export say what cost
   what; the cap closed ingress, it did not delete anything, and storage keeps billing.
2. To reopen one service for the rest of the month: `gcloud run services update SERVICE --region us-central1 --ingress=all --update-labels lab-deploy-cap-lifted=true`.
   A stopped database: `gcloud sql instances patch NAME --activation-policy=ALWAYS --update-labels lab-deploy-cap-lifted=true`.
   Without the label the next budget notification closes it again; a release also reopens ingress and is closed again.
3. When the new month starts, remove the labels: `gcloud run services update SERVICE --region us-central1 --remove-labels lab-deploy-cap-lifted`
   (`gcloud sql instances patch NAME --remove-labels lab-deploy-cap-lifted`), or the cap no longer protects that service.
4. If billing was detached (`detach_billing = true`), re-link the project in the Billing console; every API in it stopped.

## A release is stuck

| Status says | Meaning | Do |
|---|---|---|
| `dispatch_lost` | GitHub never showed a run for the dispatch within two minutes (or the plan expired first) | Create a new plan and deploy again; auto-deploy retries on its own after thirty minutes. |
| `migrating` for longer than ten minutes | The migration job is still running or the runner was lost in that phase | `gcloud run jobs executions list --job SERVICE-migrate --region us-central1`; wait for it to finish or cancel it. The recovery workflow marks the checkpoint recovered with traffic untouched; then deploy again. |
| `failed` with `release_failed_health_unconfirmed` | The candidate never became healthy; traffic stayed on the previous revision | Read the candidate's log lines in the status; fix the app; deploy again. The service reports not Ready until a good release lands. |
| `rollback_failed_manual_recovery_required` | Traffic could not be restored | `gcloud run services describe SERVICE --region us-central1` for the live split, then `gcloud run services update-traffic SERVICE --to-revisions=PREVIOUS=100`. |
| recovery workflow failing on schedule | The checkpoint names revisions that no longer match live traffic | Investigate the manual change; `rollback_staging` refuses by design; deploy the wanted commit instead. |

## Someone cannot open an app's pages

Google refuses before the app sees the request. Check, in this order: the person signed in with the listed account
(`get_service_health` shows the viewers); the app's `platform.yaml` says `exposure: lab-members` and `access.iap_enabled`
is true (otherwise render and merge the infrastructure); the IAP OAuth client still exists in the console (APIs &
Services, Credentials). To add someone, add `user:name@example.edu` to the app's `viewers` or to
`onboarding.default_viewers`, render the root, merge.

## Rotate credentials

- **Operator token**: `python -m labdeploy.cli token`, replace the hash in `config.local.yaml`, restart the controller,
  sign in again in Claude. Every session of the old token ends.
- **GitHub token**: `security delete-generic-password -s lab-deploy-github-token`, create a new fine-grained token on
  GitHub, run `start.sh` once by hand (it asks and stores it). The watchdog warns two weeks before expiry.
- **ngrok**: rotate in the ngrok dashboard and `ngrok config add-authtoken`; the domain and the connector stay.
- **Pipeline and app identities** have no keys to rotate: their trust is the federation condition in Terraform.
  Changing a repository's numeric id (a transfer) needs a foundation apply and a root re-render.

## Prove the alerts work

Once a month, or after changing channels:

1. `gcloud workflows run lab-deploy-spending-cap --location us-central1 --data='{"data":{"message":{"data":"eyJjb3N0QW1vdW50IjogMCwgImJ1ZGdldEFtb3VudCI6IDF9"}}}'`
   (a notification with cost 0 of budget 1) must answer `below cap: 0 of the budget`: the trigger path and the
   identity work without closing anything.
2. `gcloud workflows run lab-deploy-spending-cap --location us-central1 --data='{"data":{"message":{"data":"bm90IGpzb24="}}}'`
   (a message that is not JSON) fails on purpose; within about five minutes the alert
   **Lab Deploy: spending-cap workflow failed** must reach the alert email (and the webhook when configured) and
   close itself half an hour later.
3. `gcloud monitoring channels list --format="table(displayName,type,enabled,verificationStatus)"` must show the channels enabled and verified.

## Retire a database deliberately

The pipeline never destroys a resource that holds data. To retire a Firestore database or a Cloud SQL instance: take
a final export (`gcloud firestore export` or `gcloud sql export`), turn delete protection off, delete it, remove it
from the application's state (`terraform state rm` in `infra/gcp/apps/SERVICE` with the pipeline's state backend),
set `database: none` in `platform.yaml`, and merge the re-rendered root. The two scripts that did this during the
1.4 trial are recorded in `docs/VALIDATION.md`.

## Monthly checklist

- `get_controller_status` and the last watchdog findings; the backup bucket has copies from the last day.
- The cost report issue on each app repository: spend against the budget, drift, proposals.
- Any `lab-deploy-cap-lifted` label left from the previous month (`gcloud run services list --format="table(name,labels)"`).
- Dependabot pull requests on the platform repository; `pip-audit` runs every Monday in CI.
- The GitHub token's expiry in the watchdog findings.
