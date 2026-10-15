# Runbook

How Lab Deploy is operated day to day and what to do when something is off. Written 2026-10-12 for the staging
platform (`lab-deploy-staging-f29a`, operated from Claude chat by one person); update it when the setup changes.

## Owner and escalation

| Role | Who | How |
|---|---|---|
| Owner and only operator | Brian Boti | Claude chat (the connector), the terminal on the laptop that runs the controller |
| Alerts | the `alert_email` and optional `alert_webhook_url` in `infra/gcp/project/terraform.tfvars`; GitHub's failed-workflow emails; the `NOTIFY_WEBHOOK_URL` secret on each app repository | arrive without anyone watching |
| Escalation | nobody else today | when a second operator joins, follow "Succession" below: their own principal, `observer_impersonators`, `.github/CODEOWNERS`, required review (`docs/SETUP.md` 7l) and a rehearsal |

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

## Quarterly restore drill

A backup nobody has restored is a hope. Once a quarter (the daily watchdog says "quarterly restore drill due" when no
drill has passed for 92 days, whenever `backup_bucket` is set), run on the controller's machine:

```bash
python -m labdeploy.cli restore-drill        # LAB_CONFIG or --config for another configuration file
```

It needs gcloud signed in as someone who may read the backup bucket. It restores nothing into the live database, which
only gains the drill's audit event. It lists
`gs://BUCKET/controller/`, downloads the newest `deployments-*.db` into a temporary folder (deleted afterwards), opens it
read-only, runs SQLite's integrity check, counts the plans, audit events, plan state changes, registered applications and
onboarding records, downloads `config.yaml` and checks that it loads, prints a short report, and records the outcome as
the audit event `restore_drill` (`list_events` shows it). A copy older than 48 hours passes with a warning: the nightly
backup runs inside the controller, so a laptop that was off for days has an old newest copy.

| The drill says | Do |
|---|---|
| No database copy in the bucket | `python -m labdeploy.cli backup`, then the drill again; check `backup_bucket` in the configuration |
| `gcloud storage ... failed` | `gcloud auth login` as the operator: the bucket has no grants of its own, so reading it takes a project role that may read Cloud Storage (the owner role does) |
| Integrity check or missing tables | Take a fresh backup (`python -m labdeploy.cli backup`) and drill again; if it repeats, the live database needs attention before the next laptop failure does |
| The configuration copy does not load | Compare it with `config.local.yaml` (a field the current version refuses); fix the file, back up, drill again |

The drill checks the copy, not the whole procedure. Every second drill, or whenever the machine or the operator changes,
do the full "Restore the controller on another machine" above on a spare machine instead, and record it in
`docs/VALIDATION.md`.

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

## The native spend cap tripped

How you know: every Cloud Run service in the project answers 5xx at once (Google's own "503 Server Error" page), the
Cloud Run console's overview shows an enforcement banner, `get_service_health` notes that the uptime check passes less
than 100% of its probes while the service's own metrics look normal, and Cloud Billing emailed at 50%, 80% and 100% of
the cap. `scripts/spend-cap.sh check PROJECT` says `ENFORCED`. In the live exercise the pause reached every service within
five minutes of the cap being exceeded. Releases fail too: the candidate cannot
start. Nothing was deleted, and Cloud SQL, buckets and topics kept running (and billing).

1. Decide whether the spend is real: `get_cost_report` and the billing export say what cost what. A runaway service is
   better stopped by lowering its `scaling.maximum` or pausing it before lifting the cap.
2. To resume before the month ends: `scripts/spend-cap.sh lift PROJECT`, or in Cloud Billing, Budgets & alerts, the budget
   "Lab Deploy: Cloud Run spend cap", **Lift spend cap**. Services can take up to an hour to recover. A lifted cap does
   not trip again this month until it is re-armed with `scripts/spend-cap.sh amount PROJECT N` (an amount above this
   month's spend); it re-arms on the first of the next month by itself.
3. If the platform's own cap also fired, follow the section above for the services it closed.

## Drift reported

The issue **Lab Deploy: infrastructure drift** on the platform repository, or a watchdog notification, names the root,
the resources and a category. "An apply that did not finish" is as likely as an edit by hand: check the newest
Infrastructure apply run for the root first.

1. For IAM, networking or exposure, find out who changed what before anything else: Cloud Audit Logs in the console
   (Logging, `protoPayload.methodName:"SetIamPolicy"` for grants), and the issue's list of addresses.
2. To put the code's version back, dispatch **Infrastructure apply** with the root's name (or merge any change to it). The
   plan comment on that run shows exactly what it reverts.
3. If the change was deliberate, make it in the code instead: render the root (`propose_infrastructure`) after editing
   `platform.yaml` or the controller configuration, and merge; the next drift check is clean and closes the issue.

## A release is stuck

| Status says | Meaning | Do |
|---|---|---|
| `dispatch_lost` | GitHub never showed a run for the dispatch within two minutes (or the plan expired first) | Create a new plan and deploy again; auto-deploy retries on its own after thirty minutes. |
| `migrating` for longer than ten minutes | The migration job is still running or the runner was lost in that phase | `gcloud run jobs executions list --job SERVICE-migrate --region us-central1`; wait for it to finish or cancel it. The recovery workflow marks the checkpoint recovered with traffic untouched; then deploy again. |
| `failed` with `release_failed_health_unconfirmed` | The candidate never became healthy; traffic stayed on the previous revision | Read the candidate's log lines in the status; fix the app; deploy again. The service reports not Ready until a good release lands. |
| `baking` for longer than twenty minutes | The runner was lost while the new revision was being watched | The recovery workflow restores the previous split from the checkpoint, as for `promoting`; deploy again once it has. |
| `traffic_restored` after `baking` | The new revision served everyone, then crashed or answered too many 5xx; the previous split is back | Read the bake's numbers and the new revision's log lines in the status; fix the app; deploy again. |
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

## Leaving Google Cloud

The exit procedure, for the day there is a concrete reason (a Cloud Run retirement notice, a grant or data-residency
requirement, a need this cloud cannot meet; `docs/RESEARCH-2026-10-14.md` 4.3). The decision is the operator's. One
application at a time, first as a dry run:

```bash
scripts/export-app.sh APP --dry-run                 # every command it would run; nothing runs, nothing is written
scripts/export-app.sh APP [DEST_DIR]                # default folder: ~/.local/share/lab-deploy/exports/APP-STAMP
```

`APP` is the application id (`list_applications`); the target comes from the configuration file or, for an onboarded
application, from the controller's database, read-only, and the declared needs from the rendered root on this checkout.
It needs gcloud signed in as the project owner, Docker running and able to read Artifact Registry
(`gcloud auth configure-docker us-central1-docker.pkg.dev` once). It deletes no data and no resource, refuses a folder that
is not empty, and makes the folder readable by your account only.

| In the folder | What it is | How it runs or restores elsewhere |
|---|---|---|
| `image/SERVICE-REVISION.tar` | The image every serving revision runs, pulled by digest and saved with `docker save` (an OCI-compatible archive) | `docker load -i image/SERVICE-REVISION.tar` gives `SERVICE:REVISION`; `docker run -p 8080:8080 -e PORT=8080 SERVICE:REVISION` runs it, and `docker tag` plus `docker push` moves it to any registry |
| `service/service.yaml` | The service in Cloud Run's export format, a Knative Serving `serving.knative.dev/v1` Service: image, port, environment, CPU and memory, concurrency, timeout, scaling annotations | On a cluster with Knative Serving: point the image at the new registry, drop the `run.googleapis.com/*` annotations, the Cloud Run labels and `serviceAccountName`, then `kubectl apply -f`. Any other platform reads the same fields |
| `terraform/terraform.tfstate` | The application root's state, pulled read-only (`terraform state pull`) | A reference, not something to apply: it describes Google resources. Read the names, settings and grants that existed (`terraform show -json`) to rebuild them elsewhere |
| `buckets/BUCKET/` | Every object of each declared bucket (`gcloud storage cp -r`); an empty bucket is noted, not copied | Plain files: upload them to any object store with its own tool, or back to Cloud Storage with `gcloud storage cp -r` |
| `database/postgres-app.sql.gz` | A Postgres need: Cloud SQL's SQL dump of the database `app` (`gcloud sql export sql`) | `gunzip -c database/postgres-app.sql.gz \| psql "postgresql://USER@HOST/app"` into an empty PostgreSQL 17 database. It may name Cloud SQL's own roles (`cloudsqlsuperuser`, the runtime's IAM user `SERVICE-runtime@PROJECT.iam`): create roles of those names first, or remove those statements |
| `database/firestore/` | A Firestore need: the managed export (`gcloud firestore export`) | Google's own format: it imports into Firestore in any project (`gcloud storage cp -r` to a bucket there, then `gcloud firestore import gs://BUCKET/firestore --database=NAME`). Outside Google it needs a conversion, so read the data out through the Firestore API as well before the project closes |
| `manifest.json` | What was exported and skipped, each file's size and SHA-256, the image digests, the times, the platform commit | Check the copy against it after moving the folder |

Not in the folder: secret values (Secret Manager; the operator keeps them, decision D5: list them with
`gcloud secrets list --filter=name:SERVICE- --project PROJECT`), Pub/Sub topics and subscriptions (messages in flight only;
their names are in the state), logs, metrics and alert history, and the controller's own database (the backup bucket,
above).

What it costs: a Postgres export runs on the instance, which must be running (the script refuses a stopped one and says
how to start it; starting it bills by the hour), takes minutes even for a small database, and the dump bills as storage in
the recovery bucket and as egress when copied down. Firestore bills an export as one document read per document. Both
exports stay in `gs://RECOVERY_BUCKET/exports/STAMP/` until you remove them. The one change the script makes in the cloud
is for Postgres: Cloud SQL writes the dump as its own identity, so the script grants that identity object creation on the
recovery bucket for the export and removes the grant afterwards (also when the export fails).

Terraform and OpenTofu: CI validates the foundation and every application root with OpenTofu 1.12.6 as well as
Terraform 1.13.5 (`.github/workflows/ci.yml`), so the same roots can be planned and applied with `tofu` if Terraform's
licence or price ever matters. `tofu init` rewrites a root's `.terraform.lock.hcl` for its own registry; commit that change
deliberately on the day of a switch, not before. Keep the roots free of features only Terraform 1.14 or later has.

## Succession

The platform's biggest risk is that one person runs it (`docs/RESEARCH-2026-10-14.md` 4.4). Naming the second operator is
the owner's decision; until then nothing in the configuration names anyone. When that person exists:

1. **Their own identity.** They run `python -m labdeploy.cli token` in their own checkout, keep the token (it is their
   password for the Claude connector's sign-in page) and send the owner only the `token_sha256` line. The owner adds a
   principal with that hash, `applications: ["*"]`, `can_deploy: true` and `may_merge_own_requests: false`, sets the same
   switch on their own principal (`docs/SETUP.md` 7l), and the controller picks it up on the next request.
2. **Cloud access.** Add `user:their-account` to `observer_impersonators` in the foundation's `terraform.tfvars` and
   apply the foundation (`scripts/apply-foundation.sh`), for live health and logs. For the procedures below they also need
   a project role that may manage Cloud Run, Cloud SQL, buckets and budgets; until the lab decides on narrower roles that
   is the project owner role, granted by the owner in the console.
3. **GitHub.** Write access to the platform repository and the application repositories, a line in `.github/CODEOWNERS`,
   required review on `main` and the `infra` environment (`docs/SETUP.md` 7l).
4. **The machine.** The controller runs on the owner's laptop today; the second operator restores it on their own
   machine from the backup bucket when needed ("Restore the controller on another machine"), with their own GitHub token.

What they must be able to do alone: sign in to the connector and read the state; deploy, follow, cancel and roll back a
release; review and merge an infrastructure pull request someone else asked for (the comment's approval block first);
start, restore and pause the controller; lift and re-arm both spend caps; rotate the credentials; run the restore drill and
the exit procedure; read this runbook's sections on drift, stuck releases and the caps.

Rehearsal, done once by the new person alone, the owner watching without touching, and recorded in `docs/VALIDATION.md`
with anything that misled them (then fix this runbook):

- [ ] Signs in to the connector with their own token; `get_controller_status` and `list_applications` answer.
- [ ] Deploys the demo from chat, follows `get_deployment_status` to `succeeded`, then rolls it back with `rollback_staging`.
- [ ] Reviews and merges an infrastructure pull request the other operator asked for, reading its approval block first.
- [ ] Restores the controller on their own machine from the backup bucket, up to `list_plans` showing the history.
- [ ] Runs `python -m labdeploy.cli restore-drill` and explains its report.
- [ ] Finds both spend caps (`scripts/spend-cap.sh check PROJECT`, the budget's cap workflow) and says how to lift each.
- [ ] Runs `scripts/export-app.sh demo --dry-run` and explains what the real run would copy.
- [ ] Pauses and resumes one application through the configuration file.

## Monthly checklist

- `get_controller_status` and the last watchdog findings; the backup bucket has copies from the last day.
- Every quarter: the restore drill (`python -m labdeploy.cli restore-drill`; the watchdog reminds).
- The cost report issue on each app repository: spend against the budget, drift, proposals.
- Any `lab-deploy-cap-lifted` label left from the previous month (`gcloud run services list --format="table(name,labels)"`).
- The native spend cap still exists (`scripts/spend-cap.sh check PROJECT`) and the drift issue on the platform repository is closed.
- Dependabot pull requests on the platform repository; `pip-audit` runs every Monday in CI.
- The GitHub token's expiry in the watchdog findings.
