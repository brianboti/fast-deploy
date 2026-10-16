# 1.14.0: Lab Deploy is now Fast Deploy

Asked for on 2026-10-16. Everything people see carries the new name: what Claude chat reads when it connects (the
server's name and instructions), the sign-in page, the files and pull requests the platform drafts ("Drafted by Fast
Deploy", "Add the Fast Deploy workflows", "Update Fast Deploy workflows to platform ..."), the watchdog's message, the
release notifications, the weekly cost report issue ("Fast Deploy cost report: SERVICE"), the drift issue ("Fast Deploy:
infrastructure drift"), a spend cap created from now on, the documents people read and the chat playbook skill, now
`skills/fast-deploy-operator`.

What keeps the old name, on purpose, so nothing running had to move: the Python package `labdeploy` and the command
`lab-deploy`, the repository `brianboti/lab-deploy` (renaming it on GitHub is a separate, optional step), and the Google
Cloud resources and their display names (the project, service accounts, roles, buckets, the key ring, the alert
"Lab Deploy: spending-cap workflow failed" and the existing spend cap "Lab Deploy: Cloud Run spend cap"), which the
runbook still names as the console shows them. The history in this file, `docs/VALIDATION.md` and the reviews stays as
written.

The cost report and drift workflows look for their open issue under the new title, then the old one, and rename an old
one on its next update, so no second issue opens. The app repositories' workflow files take the new names at their next
re-pin; nothing changes before then.

For the operator: in Claude, remove the skill `lab-deploy-operator` and upload `fast-deploy-operator`
(docs/CLAUDE-CHAT.md, step 4); optionally rename the connector to Fast Deploy. Restart the controller.


# 1.13.1: every release is followed to its end

Found live on 2026-10-16 while checking the new chat answers: `demo`'s auto-deploy of 15 October had succeeded, but the
application list still showed the release before it, with "1 new commit" waiting, and the auto-deploy pass said the
commit was "already handled ... (dispatched)". The controller records a release's outcome when someone asks for its
status; nobody had asked. A new background job, `follow`, asks every minute about each release still on its way (newest
first, ten at a time): an auto-deploy, an onboarding's first release, or a release started from chat and not followed.
So the application list, the watchdog and the chat's summaries are right without anyone asking. A plan of an
application unregistered since is left as history. The health summary also rounds latency to a tenth of a millisecond.
Nothing to apply or regenerate; restart the controller.


# 1.13.0: clear, consistent answers in Claude chat

Asked for on 2026-10-16: chatting with Lab Deploy should be easy, understandable and professional, with formatted
answers. Until now Claude read each tool's full result (a release status is about 21 KB of JSON, the application list
19 KB) and worded every answer its own way. Nothing here changes what any tool does or costs; no apply or regeneration.

## Added

1. **A summary written by code** (`labdeploy/chat.py`). The main tools' results now start with `summary`: a few Markdown
   lines built from that result alone, never from Claude's words, in one shape: a bold outcome, the facts that matter
   (version, address, checks, time, cost), then one **Next:** line. Releases (planned, started, running, live, stopped
   with the previous version serving, failed at a named step, rolled back, needing a person), onboarding (step N of 9,
   what it waits for, who acts), repository checks and drafts, the application table, health, logs, cost, the
   comparison with the live release, pull requests and the controller status. Plain words throughout: "running the
   tests", "watching the new version with real traffic", "the previous version is still serving". A renderer that fails
   leaves the tool's answer as it was, and a result that already has a `summary` keeps it.
2. **How to answer, in the server's instructions.** What Claude chat reads when it connects now starts with the answer
   rules: build on `summary` and keep its lines as written, one shape for every answer, plain words, short ids, Markdown
   links, a table for three or more items, no raw JSON or secrets, one question at most with the recommendation first, and
   one combined answer when several tools ran.
3. **The playbook skill** (`skills/lab-deploy-operator`): "How to answer" with the same rules and two worked examples;
   its facts brought up to date (a service in a folder, a Procfile instead of a Dockerfile, Postgres by password,
   `check_repository` before onboarding when unsure, 4 to 15 minutes). Re-upload it in Claude (docs/CLAUDE-CHAT.md).
4. **docs/CLAUDE-CHAT.md, "What answers look like"**, with three real answers.

Platform tests: 473 pass (12 new in `tests/test_chat.py`); the tool descriptions are unchanged (`schema/tools.sha256.json`).


# 1.12.3: a repository onboarded again keeps its environment

Found live on 2026-10-16, re-onboarding Sophia's copy from `backend/` under its old name: the step that prepares the GitHub
environment stopped with "GitHub answered HTTP 303". The environment's `main` branch policy already existed from the first
onboarding, and GitHub now answers 303 See Other for an existing policy where it once answered 422. Both mean the policy
is in place; any other answer still stops the step. Nothing to apply or regenerate; restart the controller.


# 1.12.2: the checker drafts for a real project with large files

Found live on 2026-10-16: `propose_configuration` for Sophia's `backend/` stopped with "Repository input exceeds 64 KiB".
Among the up to 40 files it reads for hints (the configuration, the Python files most likely to hold the application, a
few tests) were a 241 KB module and a 190 KB test. Such a file is now skipped and named in the draft's review list ("Not
read, over 64 KiB each"); any other refusal from GitHub still stops the draft. The 64 KiB limit itself is unchanged.
Nothing to apply or regenerate; restart the controller.


# 1.12.1: what the 1.12.0 live exercise found

The live exercise of 1.10.0 to 1.12.0 (`docs/VALIDATION.md`, 2026-10-15) onboarded two services of one repository, a
Postgres database by password, and the checker's draft on a bare repository. It found one real failure and four small
gaps, fixed here. Nothing here costs anything. **Apply the foundation once** (`scripts/apply-foundation.sh PROJECT`: 1 to
change, the custom role); no workflow regeneration is needed.

## Fixed

1. **Postgres by password could not be applied.** The first live apply of a `database_auth: password` root created the
   instance, the database and the user, then failed: Google's provider enables a secret version right after adding it,
   and the custom role `labDeploySecretWriter` lacked `secretmanager.versions.enable`; the workflow's retry then tried to
   replace the failed version and lacked `secretmanager.versions.destroy`. The role now also has `versions.enable`,
   `versions.disable` and `versions.destroy` (state changes only; still no `versions.access`, so no value is ever read
   back). Had the retry succeeded, it would have written a URL with a new password while the user kept the first one: the
   user's `password_wo_version` now follows the URL's secret version number, so every new version (the first, a rotation
   through `password_version`, a retry) sets the user to the same run's password in place. A user is never dropped for
   this, because it may own the application's tables; a new instance writes the URL again.
2. **A Compose file's database reaches the draft.** A web service that depends on a Postgres service in the Compose
   file is drafted with `needs.database: postgres`, and with `database_auth: password` when its `environment` sets
   `DATABASE_URL` (both the list and the mapping forms of `depends_on` and `environment`); the part says why in
   `needs_from`. Before, the live test repository's admin service was drafted with `database: none`.
3. **The infrastructure pull request says who asked.** Its description, written by the controller, ends with
   `Requested by <identity>`, and with `through the onboarding of <application>` when onboarding opened it, so the chain
   reads request, pull request, plan run, apply run (roadmap 12.1). The plan comment's code-rendered block is unchanged.
4. **An open draft is named as possibly stale.** When `propose_configuration` (or any platform pull request) finds a pull
   request already open on its branch, it still leaves it as it is, and now says so: it may hold a person's edits or
   predate the request, and closing it lets the platform open a fresh one. The live run had returned `changed: []` and
   the open 1.10.0 draft that still said `my-service`.
5. A progress message no longer ends with two full stops ("in progress..").

Platform tests: 459 pass; `terraform validate` passes for the foundation and the live `mono-admin` root.


# 1.12.0: legible approvals and the operator's succession (roadmap Phase 12)

Planned in `docs/ROADMAP.md` Phase 12 from the research review (`docs/RESEARCH-2026-10-14.md`, section 4.4, the trust
gaps). Nothing here costs anything at idle.

## Added

1. **Approval blocks rendered by code** (12.1). The infrastructure plan comment opens with a fixed block that
   `labdeploy/infra.py` renders from the Terraform plan, never from Claude's words: resources to add, change, destroy and
   replace, also by type; a reversibility tier per type and for the whole plan (`reversible`; `identity` for grants,
   service accounts and federation; `data-bearing` for the types the policy never lets the pipeline destroy; the highest
   wins); the root's idle floor per month before and after, at list prices (a size it cannot price is named); every
   exposure change; and the pull request, head commit and plan run. `create_deployment_plan` returns an `approval` block
   built the same way from the plan: the commit and its first line, the spec hash, the target and the service's folder,
   the cost bounds, exposure, needs, the number of warnings, a reversibility tier (`data-bearing` when the release runs a
   migration, because a rollback restores traffic only), and the chain of identifiers (plan, commit, who asked, the
   onboarding, and the workflow run once dispatched).
2. **Requester and approver** (12.2, D29). A principal's `may_merge_own_requests` (default true) and an application's
   `auto_merge` (under `applications.<id>` or `application_settings.<id>`; by default it follows `onboarding.auto_merge`).
   Onboarding merges its own pull requests only when both allow it; otherwise it waits for another operator and says so,
   names who asked (`requested_by`), and records who merged (`merged_by`) when someone does. An identity no longer in
   the configuration approves nothing. `.github/CODEOWNERS` names `/infra/gcp/apps/**`, and `docs/SETUP.md` 7l says how
   to require Code Owner review once a second operator exists.
3. **Tool descriptions pinned** (12.3). `schema/tools.sha256.json` holds a SHA-256 of every tool's name, title,
   description, schemas and annotations; a test compares it with what Claude is served, so changing what Claude reads
   about a tool is a deliberate, reviewed commit (`python -m labdeploy.cli tools-checksum --write`). A second test refuses
   any tool that is not wrapped by the audited decorator.
4. **The exit procedure** (12.4). `scripts/export-app.sh APP [DEST_DIR] [--dry-run]` copies what one application needs to
   run elsewhere: the image of every serving revision by digest, the service in Cloud Run's Knative-compatible format,
   the root's Terraform state (read-only), every declared bucket, a Postgres dump or a Firestore export, and a manifest
   with sizes and SHA-256s. The runbook's "Leaving Google Cloud" says how each part runs or restores elsewhere. CI
   validates the foundation and every application root with OpenTofu 1.12.6 too (downloaded by version and SHA-256).
5. **The quarterly restore drill** (12.5). `python -m labdeploy.cli restore-drill` restores the newest controller backup
   from the backup bucket into a temporary folder, read-only, checks its integrity, counts what it holds, and records a
   `restore_drill` event; the daily watchdog says when no drill has passed for 92 days. The runbook gains "Succession"
   and "Quarterly restore drill".

## Changed

1. An unexpected failure in a tool or REST call returns a plain message with what to check before asking again (REST
   answers 500 with the same text), and the commonest refusals name their next action.
2. `get_onboarding_status` puts what the current step waits for into its next action.
3. The policy treats a Cloud SQL database (`google_sql_database`) as data: the pipeline never deletes one.
4. CI installs zsh so the export script's tests run there.

## Upgrade steps

1. Restart the controller. No workflow regeneration and no Terraform apply are needed for this release.
2. Run the first restore drill (`python -m labdeploy.cli restore-drill`); until then the daily watchdog reports it due.
3. Once a second operator exists: `docs/SETUP.md` 7l and the runbook's "Succession".

# 1.11.0: the platform writes the boilerplate (roadmap Phase 11)

## Added

1. **`check_repository`** (11.1), a read-only tool: the contract's checklist for a repository or one service folder of it,
   before anything is registered. Each required and recommended item says whether it holds, why it matters, and whether
   the platform can draft it. Onboarding runs the same check first and stops with the same list, naming what the
   platform can draft. Any CI workflow of the repository counts for CI, and Renovate counts for Dependabot.
2. **The checker writes** (11.2, decisions D26 and D27). `propose_configuration` now opens one draft pull request with
   every file the contract asks for that the service lacks: a Dockerfile (the app's Python slim image by digest,
   dependencies before code, a non-root user, uvicorn or gunicorn on `$PORT`), an allow-list `.dockerignore`,
   `requirements-dev.txt`, a health test, `platform.yaml` with `reviewed: false`, a CI workflow, `SECURITY.md` and
   Dependabot. It detects FastAPI, Starlette, Flask, Django and others, the Python version and the health route from the
   code, read as text and never run. The lock is compiled on the controller's machine with uv in a container of the
   app's own Python; without Docker the pull request names the exact command instead. It works for a repository that is
   not registered yet (an identity with `applications: ['*']`), never changes a file that exists (also when one appears
   on the branch meanwhile), and is never merged by the platform. The pull request explains each file in one line.
3. **Building without a Dockerfile** (11.3, D28). A service with a `Procfile` and no Dockerfile is built with Google's
   buildpacks: `pack` 0.40.9 downloaded by version and SHA-256, the builder and run image pinned by digest. Python 3.11
   and 3.12 use the Ubuntu 22.04 builder because the Ubuntu 24.04 one offers 3.13 and later only (found in the trial).
   pip installs the lock with its hashes when there is one. Measured locally on the demo app, the buildpack image is
   about 394 MB against 52 MB from its Dockerfile, so the Dockerfile stays the default and the checker drafts one.
4. **The contract opens with the three things a developer does** (11.4): a service on `$PORT` that answers its health
   path, pinned requirements, tests. Everything else is under "What the platform writes for you".
5. **Onboarding measures** (11.5): each onboarding records how long it took, how many times a step blocked, which steps,
   and how many times the operator stepped in. `list_applications` shows it, and the weekly cost issue repeats the line
   (the controller writes it to the environment as `ONBOARDING_SUMMARY` when onboarding finishes).

## Changed

1. `propose_configuration` takes a registered `application`, or a `repository` link and `path`; it no longer refuses a
   repository that has a `platform.yaml`, it drafts the other missing files instead.
2. The controller's container image includes `templates/`; `lab-deploy sync-pins` also updates the drafted CI workflow;
   CI lints the drafted workflows and the platform's Dependabot file, which now waits seven days for a new release.

## Upgrade steps

1. Restart the controller. Docker must be running on its machine for the lock to be compiled (otherwise the pull request
   carries the command).
2. Re-pin every application: the deploy workflow gained the buildpack build and the right-sizing workflow the onboarding
   summary.

# 1.10.0: one repository, several services (roadmap Phase 10)

## Added

1. **A service's folder.** An application can live in a folder of its repository (`services/api`, `backend`): the registry
   records it as `path`, `onboard_application` takes it, and the analyzer, the contract check, the plan, the drafted
   configuration, the right-sizing pull request and the weekly cost report all read `platform.yaml` there. The deploy
   workflow runs the tests and the audit in that folder and builds from it. A plan records the folder and workflow names
   it was made for and is refused if they changed (decision D23).
2. **Several services in one repository.** The first service of a repository keeps the plain names; each further one gets
   its own workflows (`deploy-gcp-<service>.yml`, `recover-gcp-<service>.yml`, `rightsize-gcp-<service>.yml`), GitHub
   environment (`staging-<service>`), queue and Terraform root. Its deploy identity trusts only its own workflows in its
   own environment, so one service's workflow cannot release another, and the pipeline publishes its variables to its own
   environment. Re-pinning, rollback, recovery records, cost reports and `lab-deploy decommission` use the service's own
   names; retiring one service keeps the repository's access for the others (D24).
3. **`build: {context: repository}`** in `platform.yaml` for a service that copies shared code from elsewhere in the
   repository: the whole repository is the build context and the Dockerfile stays in the service's folder. The contract
   says how to keep that context small with a `<Dockerfile>.dockerignore`.
4. **Where each part of an app goes** (the contract's new section 4b): every web component its own service, data under
   `needs`, no cache by default, workers through a topic's push delivery or a job, a lab-members front end served by the
   API's own container. The analyzer says so when it sees Redis, Celery or RQ, a `package.json`, or a database driver
   without the Cloud SQL connector.
5. **Compose files are read, never deployed.** `analyze_repository` reads a `compose.yaml` (the service's folder first,
   then the root) and returns one draft `platform.yaml` per web service it builds, with its folder and build context, and
   the rule for every database, cache, queue and worker it names.
6. **Postgres by password** (`needs.database_auth: password`, roadmap item 10.6): for code that only
   speaks a connection URL. The platform creates the database user `app` and the secret `<service>-DATABASE_URL`, which the
   service receives as `DATABASE_URL`. The root generates the password in each run with an ephemeral `random_password` and
   sends it once through write-only arguments, so neither Terraform's state, nor a plan, nor the repository, nor the chat
   ever holds it (checked: a plan of such a root contains no password and passes the policy). The release checks the
   secret has an enabled version before deploying. IAM sign-in stays the default.
7. **Auto-deploy in a shared repository** skips a service whose folder and workflows did not change since its live release
   (unless its build reads the whole repository), so a commit to one service does not rebuild the others.

## Changed

1. **The image is built from exactly the reviewed commit.** The test step kept its virtualenv inside the app's checkout,
   so a Dockerfile that copies everything (`COPY . .`) without a `.dockerignore` shipped the test environment in the
   image. The virtualenv now lives in the runner's temporary folder and the checkout is cleaned back to the commit
   before the build.
2. **Dockerfile lint errors stop the release by default**, as 1.9.0 announced. Warnings never do; the repository variable
   `HADOLINT_BLOCKING=false` keeps error-level findings as annotations for one repository. The four Dockerfiles in use
   have no findings.
3. The onboarding block's `environment` setting accepts `staging` only: the platform names GitHub environments itself.
4. The foundation's lock file pins the `random` provider (3.9.1) for the roots that copy it.

## Upgrade steps

1. Apply the foundation (`scripts/apply-foundation.sh PROJECT`): the plan identity gets `roles/secretmanager.viewer` and
   the apply identity the custom role `labDeploySecretWriter`, which create and describe secrets without reading a value.
2. Restart the controller.
3. Re-pin every application ("re-pin APP" or `lab-deploy update-workflows`): the workflow templates changed. Existing
   roots render unchanged, so no infrastructure pull request is needed.

# 1.9.2: the spend-cap check says whether the cap is recorded

## Fixed

1. `scripts/spend-cap.sh check` ended with a generic "record it" line showing the word AMOUNT even when the cap was
   already recorded. It now compares the cap with `cloud_run_spend_caps` in `config.local.yaml` and says "Nothing to
   do", names the differing amount, or gives the exact line to add.

# 1.9.1: a missing drift report is asked for again

## Fixed

1. Found in the 1.9.0 live exercise: the controller remembered "the drift check has produced no report yet" for an hour,
   so `get_controller_status` kept saying `not_run` after the first manual drift run had already reported. With no report
   it now asks GitHub again on the next call; a report it found is still remembered for an hour.

2. **The health view now sees an outage from outside.** When the native spend cap paused staging in the live exercise,
   every service answered 503 while `get_service_health` still showed open ingress and no server errors: requests the cap
   (or Google's front end) refuses never reach the container, so the service's own metrics do not count them. The view
   now reports `uptime_checks_passed`, the share of Google's uptime-check probes that reached the health path in the
   window, and notes it plainly below 100%, pointing to the spend cap when every app fails at once. Measured during the
   trial: 75% for the demo and Sophia over ten minutes.
3. `scripts/spend-cap.sh` gains `amount` (change the cap and re-arm a lifted one) and `lift`. Changing a spend cap's amount
   through the Budget API needs the mask `amount.specifiedAmount,spendCap.inputState` with the whole budget in the body;
   a mask of `amount` alone is refused as an invalid argument (found while lowering the cap for the live exercise). The
   check prints fractional amounts.

## Upgrade steps

1. Restart the controller.

# 1.9.0: cost and safety layers that cost nothing (roadmap Phase 9)

Planned in `docs/ROADMAP.md` Phase 9 from the research review `docs/RESEARCH-2026-10-14.md`. Every item has zero idle cost.

## Added

1. **A bake after promotion** (decision D22). Once traffic has moved to the new revision and its final probe passed, the
   release step watches that revision's own Cloud Run logs for `bake_seconds` (default 300, at most 900, 0 turns it off)
   before the release counts as done. It restores the previous traffic split, exactly as a failed probe does, on a crash
   (Cloud Run's system log shows the container exiting, stopped for memory or a signal, or a new instance failing its
   startup probe), on more than 5% 5xx answers among at least 20 requests, or, at the end of the window, on a p95 latency
   above three times the previous revisions' p95 over the hour before the switch (when both saw at least 20 requests). The
   health path is left out: the uptime check and the probes call it every minute. Too little traffic is
   `inconclusive` and passes with a note; logs that cannot be read leave the bake `unobserved`, which also passes, loudly.
   The receipt carries the window's numbers (`bake`), the status shows them, a failure reads "failed its bake after
   promotion" with the new revision's last log lines, and a runner lost while baking is recovered like one lost while
   promoting. Observed in staging while building it: Cloud Run logs `Container called exit(1).` at WARNING, so the bake
   matches crash messages rather than severity alone, and ignores harmless sandbox warnings.
   The release phases are now `preparing, (migrating,) deploying, promoting, baking, succeeded`. The setting is per
   application (`bake_seconds` under `applications.<id>` or `application_settings.<id>`); the plan records it and the
   dispatch sends it only when it differs from 300, so workflows pinned before 1.9.0 keep working. The deploy job may now
   run 35 minutes instead of 20.
2. **Google's native spend cap for Cloud Run** (decision D21), generally available since 2026-10-12: a monthly budget on one
   project and the Cloud Run service that pauses every Cloud Run workload in the project when the month's gross estimated
   cost passes it. It does not stop Cloud SQL, buckets or topics, so the platform's own cap workflow keeps that job; the
   native cap is the second layer that needs nothing of this platform to work. `scripts/spend-cap.sh check|create` reads
   and creates it through the Budget API (Terraform's Google provider has no spend-cap argument as of 8.7.0); the
   operator records it under `cloud_run_spend_caps` in the controller configuration. Every plan, `estimate_cost`,
   `get_cost_report` and `get_controller_status` now name both guards, and a project without a recorded cap gets the
   warning finding `no_native_spend_cap`. Docs: `docs/SETUP.md` 7i and the runbook's "The native spend cap tripped".
3. **Dockerfile lint**: hadolint 2.15.1 (downloaded by version and SHA-256, like Cosign) runs in the build job before the
   image is built. Findings become annotations on the run and travel in the release receipt (`dockerfile_lint`); they stop
   a release only when the repository variable `HADOLINT_BLOCKING` is `true`, and then only for error-level findings
   (the default from 1.10.0). The platform's own CI lints its Dockerfile and the example app's with the same pin. The
   four Dockerfiles in use today (the platform, the demo, the Sophia copy, lab-notebook) have no findings.
4. **Data-protection rules in the infrastructure policy**, each with a planted-violation test: Cloud SQL keeps automated
   backups (`database_backups`), accepts encrypted connections only (`encrypted_connections`) and keeps deletion
   protection in Cloud SQL itself as well as in Terraform; a Firestore database is abandoned, never deleted, when Terraform
   lets go of it; no bucket is force-destroyed (`no_force_destroy`); the recovery and backup buckets keep versioning
   (`versioned_checkpoints`); and no resource may store secret material in Terraform's state or plan
   (`no_secret_in_state`: secret versions with `secret_data`, SQL users with `password`, generated passwords and keys;
   the write-only forms are allowed). The modules already met every rule, so no live resource changes; a test keeps a
   trimmed real plan of the demo's root (`tests/fixtures/lab-demo-plan-2026-10-14.json`) passing with every resource
   treated as changing, and another scans every module and rendered root for secret-bearing resources.
5. **The weekly drift check** (decision D30): `.github/workflows/infra-drift.yml` plans every application root against the
   live infrastructure on Mondays (or on demand from `main`) with the read-only plan identity and no state lock, and
   reports what differs from the code: an edit made outside the pipeline, or an apply that did not finish. The result is
   a living issue on the platform repository, "Lab Deploy: infrastructure drift" (closed when everything matches), and a
   `drift-report` artifact of addresses and categories only, never attribute values. The controller reads it:
   `get_controller_status` shows it, and the daily watchdog notifies on drift in IAM, networking or exposure, on a root
   whose plan failed, and on a report older than eight days. A separate workflow keeps the pull-request plan workflow
   unchanged; the plan identity's federation now admits `schedule` and `workflow_dispatch` events only for that file on
   `main`.
6. **Per-event charges in the weekly cost report**: the month's freshly built images (each one is an Artifact Analysis
   scan at about USD 0.26), the repository's billable GitHub Actions minutes when it is private (each job rounded up to
   the minute, USD 0.006 a minute on the standard Linux runner after the account's included minutes), and the bytes the
   billing queries read. Measured on 2026-10-14: the demo built 15 images this month (about USD 3.90 in scans, more than
   its compute), and the private Sophia copy used 873 billable minutes in two days, 709 of them its own CI. The right-sizing
   job gains `actions: read` for this.

## Changed

1. The cost documents and the report say plainly that Google's Cloud Run recommender advises only the billing mode
   (and two security checks); it never sizes CPU or memory, so the weekly measured proposals are the only sizing signal.
2. The living cost-report issue no longer lists each proposal twice.

## Upgrade steps

1. Merge, then apply the foundation (`scripts/apply-foundation.sh PROJECT`): it lets the plan identity run the drift check.
2. Restart the controller and re-pin every application's workflows (the deploy and right-sizing templates changed); after
   that, raise `platform.minimum_commit` to the 1.9.0 commit, so a non-default `bake_seconds` never reaches an old pin.
3. Create the native spend cap (`scripts/spend-cap.sh create PROJECT AMOUNT`, or the console, `docs/SETUP.md` 7i) and
   record it under `cloud_run_spend_caps`.
4. Optional: dispatch the drift workflow once from the Actions tab to see the first report.

# 1.8.8: applications on Python 3.11 to 3.15, audited on their own Python; conda, Poetry and uv

Asked after the Sophia test: people bring different Pythons and package managers. Three gaps, all in the build job; no
cloud change and no cost.

## Added

1. **Python 3.14 and 3.15** for `platform.yaml` `python` (now 3.11 to 3.15; default still 3.12, so existing specs keep
   their hash). Both are on GitHub's Ubuntu 24.04 runners (3.14.8, 3.15.0). 3.10 and older stay unsupported: 3.10's
   security support ends this month.
2. **The analyzer drafts `python` from the Dockerfile's base image** instead of only suggesting it, and says plainly when
   that image is on a Python the platform does not test (3.10 and older).
3. **conda, Poetry and uv**: `docs/DEVELOPER-CONTRACT.md` section 1a gives the exact export commands for the three pip
   files the platform reads, and the analyzer points there when it finds `environment.yml`, `poetry.lock` or `uv.lock`
   without a `requirements.txt`. The uv flow was run end to end on Python 3.14 (hash-locked install and a clean audit).

## Fixed

1. **The dependency audit ran on the platform's Python 3.12 whatever the application used.** Checked on 2026-10-14:
   `numpy==1.24.4`, an ordinary pin for a Python 3.11 project, has no 3.12 build, so the audit failed to install it and
   would have stopped the release; and pip-audit judges version markers for the Python it runs on, so on 3.12 it skipped
   packages only a 3.14 application installs. The audit now runs on the application's own Python (the same
   `setup-python` step as the tests, now also when `tests: none`). With a `requirements.lock` it reads every locked
   package at its exact version without installing or building anything (`--disable-pip`); without one it resolves
   `requirements.txt` as before. The audit tool installs from wheels only, checked on Python 3.11, 3.13, 3.14 and 3.15.

## Upgrade steps

1. Restart the controller and re-pin application workflows (the template changed).

# 1.8.7: an image released under the provenance exception is never reused

## Fixed

1. Found while preparing the exception's live test with Sophia: once the operator removed `provenance: unavailable`, the
   next plan for the same commit reused the image released under the exception. That image has no signed provenance,
   so the reuse path's verification would have stopped the release (safely, but for no reason the operator could see).
   A plan now reuses only an image whose release receipt does not say `provenance: unavailable`: an earlier signed image
   of the same commit if there is one, otherwise the commit is built, tested and signed fresh.

## Upgrade steps

1. Restart the controller.

# 1.8.6: signed provenance for private repositories

Found by deploying the private copy of the lab's Sophia backend: GitHub's artifact attestations cover public repositories
(private ones only on GitHub Enterprise Cloud), so `actions/attest-build-provenance` refused the image and nothing was
deployed. Provenance stays required; the platform now signs with its own trusted path when GitHub cannot.

## Added

1. **Cosign with Cloud KMS for private repositories.** The deploy job picks the path from the repository's visibility:
   public uses GitHub's attestation as before; otherwise Cosign v3.1.3 (downloaded by version and SHA-256) signs a SLSA v1
   statement for the pushed digest with the application's own key, `<service>-provenance` in the foundation's
   `lab-deploy` key ring (EC P-256, software, about USD 0.06 a month per application). Only the application's deployer
   may use the key (`roles/cloudkms.signerVerifier` and `roles/cloudkms.viewer` on that key alone), and the deployer is
   reachable only from its deploy workflows on `main` in `staging`. The signature is stored as the image's
   `sha256-<digest>.att` tag and never sent to a public transparency log, which would publish a private repository's
   name and commits. Before release, Cosign checks the signature with the key and `labdeploy/provenance.py` checks that
   the statement names this image digest, repository, commit and workflow; a reused image is checked the same way.
2. **The operator's exception, `provenance: unavailable`**, under `applications.<id>` or `application_settings.<id>` in the
   controller configuration, for staging or an emergency only: the plan warns, the run shows a warning, the image is
   always built fresh (the workflow refuses the exception with a reused image), and the receipt records it. A plan made
   before the setting changed is refused at dispatch. `platform.yaml` has no such field.
3. Every release receipt records its provenance path (`github`, `cosign-kms` or `unavailable`), and the release step
   refuses to run without one.

## Changed

1. Foundation: Cloud KMS API and the `lab-deploy` key ring (free; Cloud KMS never deletes a ring), administered within
   that ring only by the apply identity and readable by the plan identity; neither can sign. Output `kms_key_ring`.
2. Service module: the key and its two grants, created once the foundation has the ring (application roots read it with
   `try()`, so they plan cleanly before); a registry KEEP rule for `sha256-` tags. The policy check treats a
   `google_kms_crypto_key` as data (never destroyed or replaced by the pipeline), holds its key ring to the allowed
   region and requires its labels.

## Upgrade steps

1. Merge, then apply the foundation (`scripts/apply-foundation.sh PROJECT`): it enables Cloud KMS and creates the ring.
2. Apply each application root again (dispatch the infrastructure apply, or merge its next infrastructure change): this
   creates the application's key. The roots in this release already read the ring.
3. Restart the controller and re-pin application workflows (the template changed).

# 1.8.5: a real lab project's Python and test suite

Found by deploying a private copy of the lab's Sophia backend (a FastAPI app on Python 3.13 with 913 tests).

## Added

1. **`python:` in `platform.yaml`** (3.11, 3.12 or 3.13; default 3.12, so existing specs keep their hash): the release's
   tests run on that version. The platform's own steps keep Python 3.12 (`update-environment: false`). Live, a lock
   compiled for 3.13 omitted greenlet, which SQLAlchemy needs below 3.13 on Linux x86_64, so `pip install
   --require-hashes` failed on the platform's 3.12 (reproduced in python:3.12-slim and 3.13-slim containers).
2. The analyzer notices a Dockerfile on another Python and suggests the `python:` line.

## Changed

1. The build job may run 45 minutes instead of 20: a real suite takes minutes before the image is built.

## Upgrade steps

1. Restart the controller, re-pin application workflows (the template changed), then add `python:` where it differs.

# 1.8.4: a second infrastructure change for the same application

## Fixed

1. **The second infrastructure pull request of an application conflicted.** The controller reused the branch
   `lab-deploy/infra-<app>` when no pull request was open on it; after a squash merge that branch's history no longer
   matches main, so the new pull request conflicted and GitHub ran no plan workflow for it (live: brianboti/lab-deploy#29,
   retiring lab-notebook's database). An existing branch is now reused only when it is exactly the base commit; otherwise
   the pull request uses a new branch named after the base commit. Nothing is deleted or force-pushed.

## Upgrade steps

1. Restart the controller. Optionally turn on "Automatically delete head branches" in the platform repository's settings.

# 1.8.3: a cancel reads as a cancel

## Fixed

1. Re-tested live on 1.8.2, the cancel worked (the deploy job was cancelled, the release restored traffic, the previous
   revision kept serving), but the status read "The database migration failed ... (KeyboardInterrupt: )". The receipt
   now marks a cancelled release, and the status says it was cancelled and in which phase; during a migration it adds
   that the started job runs on in Google Cloud and may still finish.
2. `docs/VALIDATION.md` records the comprehensive test with `brianboti/lab-notebook`.

## Upgrade steps

1. Restart the controller and re-pin application workflows (the runner's receipt changed).

# 1.8.2: a cancel really cancels the release

## Fixed

1. **A cancelled run kept releasing.** Re-tested on 1.8.1, a cancel during the migration again let the release promote.
   The cause was the deploy job's condition, `if: always() && ...`: GitHub does not cancel a job whose condition uses
   `always()`. It is now `!cancelled() && ...`, which still runs the job when the build was skipped (a reused image).
   Correction to 1.8.0: its note blamed the shell for not passing the signal on; that was not the cause. Running the
   release with `exec`, as 1.8.0 does, stays: the signal now reaches the release directly when GitHub sends it.
2. A running controller re-reads the registered applications every minute, so `lab-deploy unregister` reaches it
   without a restart.
3. **A failed migration's cause was still missing on 1.8.1** (re-tested live): the job's traceback became searchable
   seconds after the first read, and as one 76-line entry its cause is the last line, beyond the 500 characters kept. The
   runner now waits up to half a minute for an error line, keeps the end of each entry, and names its last line.

## Upgrade steps

1. Restart the controller, and re-pin every application's workflows to this commit (the deploy job changed).

# 1.8.1: after the late cancel, the billing export and an abandoned onboarding

## Fixed

1. **A release that completed before a late cancel stayed unrecorded.** The plan was stored as `cancelled` from the run's
   conclusion, the controller kept the earlier release as live, and the health view reported traffic changed outside the
   pipeline. The receipt now decides: a run that ended cancelled, failed or timed out after the release succeeded is the
   live release, and its status says how the run ended.
2. **Actual spend read $0 when the billing export had no recent data** (its newest row was 18 days old). A freshness
   query runs first; with nothing in the last two days the report says the spend is unknown, not zero, and how to check.
3. **An onboarded application could not be retired.** `decommission` read only the configuration file ("Unknown
   application"), had no Cloud SQL or migration-job step, and nothing removed an abandoned onboarding, which the
   watchdog reported daily. `decommission` now reads the registry, the checklist covers Postgres, `lab-deploy unregister
   APP` forgets an onboarded application, and the watchdog names an unfinished onboarding as such.

## Upgrade steps

1. Restart the controller. Re-pin application workflows for the billing-export change in the weekly report.

# 1.8.0: what the first complicated application found after its onboarding

`lab-notebook` was onboarded with one sentence, released (Postgres migrations through the app's own IAM identity), used
end to end (24 live checks), updated, rolled back, redeployed from its image, and taken through a failing health check,
a failing migration, a failing test, a cancel, policy refusals and a missing secret. Each finding has a regression test in
`tests/test_comprehensive_findings.py`.

## Added

1. **`application_settings`** in the configuration: the operator's switches (`cost_ceiling_usd`, `auto_deploy`, `paused`,
   `viewers`, `infra`) for an application onboarding registered, laid over its database entry without copying it.
2. **The health view shows the Postgres database**: state, tier, version and whether it runs (the spending cap stops it);
   the observer gets `roles/cloudsql.viewer`. A refusal for one need is a note instead of hiding the others.

## Fixed

1. **A cancel did not stop a running release.** The release ran behind a waiting bash, which never passed GitHub's SIGINT
   and SIGTERM on: a cancel during the migration let the release promote two minutes later. The step now execs the
   release, which restores traffic and ignores further signals while it does.
2. **A failed migration was reported as a failed candidate** with gcloud's cut-off tail. The receipt now keeps the
   execution's own last log lines and the real error (`relation "..." does not exist`), names the failed phase, and the
   status says the migration failed and nothing new was deployed. A pre-check stop (a missing secret) says the same.
3. **Onboarding stayed blocked after the recovery it recommends**: a successful apply on a later commit of main now counts.
4. **A refused GitHub request blamed the wrong permission**; it now names the one the request needs (Administration for
   environments, Workflows for workflow files, ...).
5. **A recovery check still running read "concluded 'None'"**; it now says it is still running, with its link.
6. **An invalid `platform.yaml` came back as pydantic's raw error**; it now names the field and what it must be.

## Upgrade steps

1. Re-apply the foundation (`roles/cloudsql.viewer` for the observer; 1 to add).
2. Restart the controller.
3. Re-pin every application's workflows to this commit (`update_app_workflows`, merge): the release step changed.

# 1.7.3: the first apply of a new application

Found by the first apply of `lab-notebook`'s root (run 38440870710), each with a regression test.

## Fixed

1. **The apply identity could not create the log-error alert.** A log-based alert policy is stored as a Cloud Logging
   notification rule, which `roles/monitoring.admin` does not cover. The foundation adds the custom role
   `labDeployLogAlertWriter` (the five `logging.notificationRules.*` permissions, nothing wider such as sinks) for the
   apply identity. The demo's alert had been created by the operator before the pipeline existed.
2. **The first apply of a new application failed on the uptime check** ("404 Requested entity was not found"): it was
   created four seconds after the brand-new Cloud Run service, which Monitoring did not see yet; the same apply a
   minute later succeeded. The apply step now plans again after a minute, re-checks the policy and applies once more
   when an apply fails; a real problem still fails twice.

## Upgrade steps

1. Re-apply the foundation (`scripts/apply-foundation.sh PROJECT`): the plan adds the role, its grant, and the IAP API
   from 1.7.0 (3 to add, 0 to change, 0 to destroy on staging).

# 1.7.2: fixes from the comprehensive test with a complicated application

A new reference application, `brianboti/lab-notebook` (pages for people, a JSON API, Postgres with Alembic migrations,
an uploads bucket, an events topic read back on a page, an API key in Secret Manager), was taken through the platform
from a repository link. It found these, each with a regression test (`tests/test_comprehensive_findings.py`):

## Fixed

1. **New applications could not be planned** (blocker). The gcs backend creates a missing state, and its lock, even with
   `-lock=false`; the read-only plan identity may not, so the first plan of every new application root failed with a
   403. `infra-plan.yml` now plans a root with no state yet against an empty local state; any other answer stops.
2. **The policy refused the platform's own module** (blocker). The service module grants the deployer
   `roles/containeranalysis.occurrences.viewer` for the scan summary; the role was missing from the allow-list, so
   every new application's plan was `Policy: blocked`. Existing roots never noticed: only changes are judged.
3. **A real migration could not create a table.** Since PostgreSQL 15 an ordinary user may not create objects in
   `public`, and Cloud SQL gives a new IAM user no privileges beyond connecting. The runtime identity now gets
   `database_roles = ["cloudsqlsuperuser"]` on its own instance. The 1.4 trial ran a migration that only printed.
4. **Every new application failed to plan before the foundation was re-applied for 1.7.0**: the renderer read
   `outputs.iap_client_id`, which an older foundation state does not have. It now falls back to `""` (no IAP client).
5. `scripts/start.sh` aborted under zsh (`status` is reserved) exactly when the Keychain had no working token, so the
   prompt for a new one never appeared; the prompt now lists the 7g permissions.
6. The analyzer read only `requirements.txt` and printed false advice for a reproducible repository (lock file ignored)
   and asked for tests that existed. It now reads the repository's file list, and suggests needs (bucket, topic,
   database, migration) in the review questions instead of putting database libraries in the legacy `dependencies`
   field, whose policy message still said databases are not implemented (decision D16).
7. Demo mode registered every directory as `lab-demo`, so any other application's plan was refused; it now uses the
   name in the application's `platform.yaml`, and the walkthrough stops with a plain message on a refusal.

## Upgrade steps

1. Restart the controller. No runner or template change: application workflows keep their pin.

# 1.7.1: a playbook for Claude chat

## Added

1. **`skills/lab-deploy-operator`**, a skill for Claude chat: the operator's playbook as instructions Claude reads whenever a
   request is about deploying, releasing, rolling back, health, logs, cost or access. It sorts the 22 tools into look,
   record and act; says when to ask before acting (and that `onboard_application` goes all the way when a repository
   passes); walks through the seven daily jobs; explains every release state in plain words; lists the hard limits; and
   turns a failed repository check into a message the operator can paste to the developer. `references/developer-contract.md`
   is a short copy of the standard for checking a repository by hand. Upload once under Customize → Skills
   (`docs/CLAUDE-CHAT.md`, step 4); its description stays within claude.ai's 200-character limit.

## Fixed

1. The instructions the controller sends to Claude at connection said databases are refused by policy; they have been
   supported since 1.4.0. They now name what is refused (other clouds, production, public access, GPUs, sizes above the
   lab's limits), how databases, buckets, topics and migrations are added, and the health, logs and rollback path.
2. `docs/CLAUDE-CHAT.md` sent "give demo a bucket" to `propose_configuration`, which refuses an app that already has a
   `platform.yaml`; the row now describes the reviewed `needs` change followed by `propose_infrastructure`.
3. A test keeps the playbook's tool table, the server's instructions and the registered tools in step.

## Upgrade steps

1. Restart the controller for the new instructions (no runner or template change; app workflows keep their pin).
2. Build the zip and upload the skill in Claude (`docs/CLAUDE-CHAT.md`, "Before the first conversation", step 4).

# 1.7.0: pages for people (Phase 8, items 8.2 to 8.4 and 8.6)

## Added

1. **`exposure: lab-members`** in `platform.yaml`: the service gets Google's Identity-Aware Proxy in front of it, directly on Cloud Run (no load balancer, no charge). People open the pages after a Google sign-in; only the listed accounts get through. `authenticated` (programs with identity tokens) stays the default; `public` is still refused by the model and the policy.
2. **Viewers**: `onboarding.default_viewers` (the lab's accounts, applied to every `lab-members` app) and `viewers` per application in the controller's configuration or registry (`user:`, `group:` or `domain:`). The renderer writes them into the app's Terraform root as `roles/iap.httpsResourceAccessor` grants; the operator edits the lists, developers cannot grant themselves. A `lab-members` root without a viewer is refused at render time.
3. **Terraform** (service module): `iap_enabled` on the service, the IAP service agent as invoker, one grant per viewer, the release probe identity as a viewer, and an uptime check that, behind the proxy, accepts the sign-in redirect as "the front door is up" (the release probes prove the page itself). The foundation enables `iap.googleapis.com` and publishes `iap_client_id` (a variable the operator sets after the one-time console step; the id is not a secret). The apply script creates the IAP service agent.
4. **The runner**: a pre-check that refuses a release whose `platform.yaml` disagrees with the service's proxy state (Terraform owns the switch), and probes that mint their identity token for the proxy's OAuth client (`IAP_CLIENT_ID` in the app's GitHub environment, written by onboarding when configured) instead of the service URL.
5. **The health view** shows `access`: the exposure, whether the proxy is on, and who may open the pages; a mismatch between the two is a note.

## Upgrade steps

1. Once, in the console (`docs/SETUP.md`, "Pages for people"): create the project's IAP OAuth client with "Auto generate credentials" on any service, then put its client id in the foundation's `terraform.tfvars` (`iap_client_id`) and in `onboarding.iap_client_id`; add the lab's accounts to `onboarding.default_viewers`.
2. Re-apply the foundation (the IAP API, the service agent, the new output); re-render every application root (two new inputs). Runner and template changed: regenerate the app workflows against this commit and raise `platform.minimum_commit`; set `IAP_CLIENT_ID` in existing apps' `staging` environments.

# 1.6.0: one sentence (Phase 7, items 7.1, 7.2, 7.4 and 7.6)

## Added

1. **`onboard_application`**: from a repository link to a running service, for a repository that follows `docs/DEVELOPER-CONTRACT.md`. The tool checks the contract (the files, a valid and reviewed `platform.yaml`, tests, the exposed port) and stops with a checklist when something is missing; registers the application; renders its Terraform root and opens the pull request on the platform repository; merges it once the plan workflow has run and the policy says passed (and only when every file is under the application's own root); waits for the apply; creates the app's `staging` environment restricted to `main` and writes its eight variables (plus `BILLING_EXPORT_TABLE` when configured); opens and merges the pull request that adds the three workflows; then plans and dispatches the first release. Steps that wait on GitHub or the pipeline end the call; the scheduler continues every minute, and `get_onboarding_status` says where it stands. Calling again resumes, also after a fix.
2. **Applications registered in the database** (decision D13): onboarding writes the application entry to the controller's database and the live configuration shows it next to the file's entries; the file keeps principals and settings. An identity with `applications: ["*"]` sees every application, including the ones registered later.
3. **`onboarding` block** in the configuration: `project_id`, `project_number`, `region`, optional `state_bucket`, `bootstrap_image`, `environment`, `billing_export_table`, and `auto_merge` (default true; false leaves every merge to a person, the tool then waits).

## Changed

- The scheduler runs the onboarding tick every minute; `get_controller_status` shows it with the other jobs.
- The GitHub credential needs more than reading: see `docs/SETUP.md`, "One sentence", for the fine-grained token's permissions (all repositories; contents, pull requests, workflows, actions, environments, variables and administration). A GitHub App (decision D14) stays planned.

## Upgrade steps

1. Restart the controller, add the `onboarding` block and `applications: ["*"]` for the operator, and replace the GitHub token with one that covers every repository and the permissions above (`scripts/start.sh` stores it). No runner change: application workflows keep their pin.

# 1.5.0: the review's fixes

Every finding of the strict review of 2026-10-12 (`docs/REVIEW-2026-10-12.md`), in its order, with a regression test where code changed.

## Fixed

1. **Merging is now the gate** (finding 1): the apply identity's provider accepts only `infra-apply.yml` on `refs/heads/main` in the `infra` environment, the plan identity's provider only `infra-plan.yml` on a `pull_request` event, and each binds through an attribute the other provider never maps. `main` is protected (pull request, the `test` and `terraform` checks, administrators included, no force push), the environment forbids administrator bypass, and `.github/CODEOWNERS` names the owner.
2. **The plan policy fails closed** (findings 2, 15): public members refused everywhere, an allow-list of roles an application may grant (plus the platform's `labDeploy*` custom roles), the platform's identities and the state bucket matched in every string field, authoritative policies refused, the invariants (private database, delete protection, private bucket, labels, region including `location_id`) checked on update too, unknown values refused. The checker runs from a checkout of `main`, not from the pull request, and the comment carries the full plan. The renderer escapes `${` and `%{`, and `migrate` parts reject `,`, `$`, `^` and `"`.
3. **Tools no longer block the controller** (finding 3): every tool runs in a thread pool with the caller's identity carried along; a status wait no longer stalls sign-ins or other callers.
4. **A runner lost during a migration recovers** (finding 4): `migrating` is a recoverable checkpoint state (traffic untouched), the advice names it.
5. **One-word migration commands release** (finding 5): `--args=` resets the job's arguments; `--clear-args` never existed.
6. **The cap is robust** (finding 6): every call retries, each service and instance is handled in its own try block, failures are logged, counted and end the execution, an alert policy watches failed executions, the ratio comes from `costAmount` and `budgetAmount` (so any threshold works), a resource labelled `lab-deploy-cap-lifted=true` is left alone, and `ingress` joined the service's `ignore_changes` so a later apply does not reopen a capped service. The cap's per-service grant is a custom role (`labDeployCapOperator`: read and update a service) instead of `roles/run.developer`.
7. **Lost dispatches end** (finding 7): a plan still `dispatching` or `dispatch_unknown` two minutes later with no visible run becomes `dispatch_lost`, a final state with advice.
8. **Auto-deploy retries** (finding 8): a `planned`, errored or lost attempt is retried after thirty minutes instead of sticking forever.
9. **Firestore cost note corrected** (finding 9): no free quota for named databases, regional storage at 0.15 per GiB-month.
10. **One migration timeout** (finding 10): the job's task timeout is derived from the runner's command timeout; a timeout records the execution as still running and the release fails without a second migration.
11. **The ceiling is named** (finding 11): the policy text says it compares the compute bound plus the needs' floor; the report prices live settings as they are and warns when live `maxScale` exceeds the reviewed bound.
12. **SQLite in WAL mode with guarded writes** (finding 12): `finish` only completes a record in its claimed state; OAuth tables are pruned hourly.
13. **No client secrets** (finding 13): registered clients are public (`token_endpoint_auth_method: none`, PKCE); the access log no longer records query strings. Access tokens and authorization requests are bound to the resource (finding 26).
14. **Narrower pipeline identities** (finding 14): plan has specific viewer roles and security reviewer, no `roles/viewer`; apply has the admin roles a root needs and a custom bucket-manager role, no `roles/editor`, no `roles/storage.admin`; state access is conditioned to the `apps/` prefix (plus read of the foundation's outputs).
15. **Documents match the code** (finding 16): README, ARCHITECTURE, HANDOFF, SETUP and ROADMAP corrected; `LICENSE` (MIT); Dependabot watches the roots that exist; CI runs weekly and audits both lock files.
16. **Disaster recovery** (finding 17): `backup_bucket` sends the nightly copy and the configuration to the foundation's new private versioned bucket (`PROJECT-lab-deploy-backups`); the operator scripts live in `scripts/`; `docs/RUNBOOK.md` covers the laptop-off case, a rehearsed restore, the cap, stuck releases, credentials and a monthly alert exercise.
17. **Container behind a proxy** (finding 18): the Dockerfile documents `FORWARDED_ALLOW_IPS`.
18. Low findings 19 to 25: a re-run gets a fresh revision suffix (`a2`, `a3`); candidate log lines are marked untrusted; names must be 3 to 21 characters without a trailing dash; the scheduler is joined on shutdown; `var.subnet` is used by the migration job; the Pub/Sub API is managed once; the billing dataset is labelled; `install-launch-agent.sh` defines its variable; the foundation apply script shows the plan and asks; `start.sh` never puts the token on a command line; the reload scope is documented.

## Upgrade steps

1. Re-apply the foundation (`scripts/apply-foundation.sh PROJECT_ID`): two providers replace the old one, roles change, the cap's custom role, alert policy and the backup bucket appear.
2. On the platform repository replace `INFRA_WIF_PROVIDER` with `INFRA_PLAN_WIF_PROVIDER` and `INFRA_APPLY_WIF_PROVIDER` from the output; protect `main` and forbid bypass on the `infra` environment.
3. Re-render every application root (the cap's role input) and merge; runner code changed, so regenerate the app workflows against this commit and raise `platform.minimum_commit`.
4. Set `backup_bucket` in the controller configuration to the foundation's `backup_bucket` output and restart; the scripts in `~/.local/share/lab-deploy/` now call the ones in `scripts/`.

# 1.4.2: fix from the live plan

## Fixed

1. **The label policy refused every Cloud SQL instance**: it looked for a top-level `labels` map, and Cloud SQL keeps its labels under `settings.user_labels`. Found by the first plan of a database root, which the policy refused as designed.

# 1.4.1: fix from the live apply

## Fixed

1. **Cloud Workflows limits an expression to 400 characters**; the cap's log line had grown to 445 with the database count. The message is now built in parts, the test checks the limit, and the corrected workflow was deployed and deleted as a throwaway before this release. The foundation re-apply updates the workflow.

# 1.4.0: Cloud SQL, migrations and a cap that stops databases (Phase 6, items 6.5 and 6.6)

## Added

1. **`database: postgres`** under `needs`: the smallest Cloud SQL for PostgreSQL instance (shared core, zonal, 10 GB of HDD that grows to 20, daily backups kept a week), with a private address only and IAM sign-in only: no password exists anywhere. The runtime identity is the database user; the revision gets `DATABASE_INSTANCE` (the connection name), `DATABASE_NAME`, `DATABASE_USER` and `DATABASE_IP_TYPE`, and the service joins the foundation's private network for its egress. It bills every hour it exists, about 9 dollars a month: every plan and estimate says so, the warning names it, and the operator's cost ceiling now counts it.
2. **`migrate`** under `needs`: a command run as the `<service>-migrate` Cloud Run job from the release's own image before the candidate exists, so a schema change lands before any new code serves. A failed migration stops the release with traffic untouched; the receipt gains a `migrating` phase and the job's execution name. Keep migrations compatible with the previous revision (expand first, contract later), so a traffic rollback stays safe.
3. **The foundation's private network**: one VPC with a subnet for Cloud Run egress and Private Service Access for Cloud SQL, free while idle; the Compute, Service Networking and Cloud SQL Admin APIs.
4. **The spending cap stops databases**: at the budget line the cap workflow also sets every managed Cloud SQL instance's activation policy to never, after closing ingress. Restart one with `gcloud sql instances patch NAME --activation-policy=ALWAYS`.
5. Policies: a database instance is created with deletion protection; the plan refuses a public address.

## Upgrade steps

1. Re-apply the foundation (network, subnet, peering, three APIs, the cap's Cloud SQL role) and re-render each application root (the `migrate` field and the network inputs). Runner code changed: regenerate the app workflows against this commit and raise `platform.minimum_commit`.

# 1.3.0: spend by application (Phase 6, item 6.6, part one)

## Added

1. **Actual spend by application.** The billing export query now groups by Google service and by the `lab-deploy-app` label that every managed resource carries, so the weekly report, the living issue and `get_cost_report` show the project's spend by application beside the per-service view, and the app's own labelled spend with its breakdown. Firestore operations and Cloud Run request fees carry no label and appear as unlabelled.

The other half of item 6.6, the spending cap stopping databases, arrives with Cloud SQL (item 6.5).

## Upgrade steps

None beyond the billing export being enabled; the next right-sizing run shows the new breakdown.

# 1.2.0: needs in the health view and the retirement checklist (Phase 6, item 6.7)

## Added

1. **`get_service_health` checks the declared needs**: each bucket, topic, subscription and database with whether it exists and the facts that matter (location, acknowledgement deadline, delete protection), through the observer identity, which gains the bucket, Pub/Sub and Datastore viewer roles in the foundation. A missing need becomes a note that names the fix.
2. **`lab-deploy decommission` lists the needs** with their data-loss warnings and the exact commands, reads them from the app's `platform.yaml` when a GitHub credential is present, and tells the operator to remove them from the state before `terraform destroy`.

## Upgrade steps

1. Re-apply the foundation (three viewer roles for the observer) and restart the controller.

# 1.1.1: fix from the live run

## Fixed

1. **The pipeline's apply identity could not finish the first `needs` apply**: the editor role sets no Pub/Sub IAM policies and creates no Firestore databases (two 403s after the bucket, topic and subscription were created). The foundation now grants it `roles/pubsub.admin` and `roles/datastore.owner`.

## Upgrade steps

1. Re-apply the foundation, then re-run the apply for the affected application root (`workflow_dispatch` on the apply workflow).

# 1.1.0: what an app needs, part one (Phase 6, items 6.1 and 6.2)

## Added

1. **`needs` in `platform.yaml`**: `buckets`, `topics` and `database: firestore`. The platform provisions them through the app's Terraform root and the pipeline, and the release sets the derived variables on every revision: `BUCKET_<NAME>` (`<project>-<service>-<name>`), `TOPIC_<NAME>` and `SUBSCRIPTION_<NAME>` (one pull subscription per topic) and `FIRESTORE_DATABASE` (`<service>`). Names follow fixed conventions, so no output travels anywhere and the variables cannot collide with `env` or `secrets`.
2. **Least privilege per need**: the runtime identity gets object user on its buckets, publisher on its topics, subscriber on its subscriptions and datastore user on its own named database only; the deployer may only look, which the new pre-check uses: a release whose needs do not exist yet stops before deploying and says what to merge first.
3. **Policies**: buckets use uniform access with enforced public access prevention; databases are created with delete protection; topics and subscriptions carry the managed labels. Buckets and databases cannot be destroyed by a plan.
4. **Cost**: every plan and estimate lists the declared needs with their list prices and a monthly floor of zero (they cost by use), and the findings explain the free allowances.

## Changed

- The spec hash leaves default values out, so adding an optional field to the schema no longer changes the hash of a spec that does not use it. This change itself changes every hash once.
- The foundation enables the Pub/Sub and Firestore APIs.
- Per-app infrastructure settings (alert thresholds, image retention) live under `applications.<app>.infra` in the controller configuration; the renderer writes them into the root, which is never the source of truth.

## Upgrade steps

1. The spec hash changed: regenerate the app workflows against this commit **before** deploying and set `platform.minimum_commit` to it; plans created before the upgrade must be created again.
2. Re-apply the foundation (two APIs) and re-render each application root (one `needs` line); the pipeline applies it.

# 1.0.2: fixes from the live run, part two

## Fixed

1. **A failure in the publish step was hidden** by the pipe that also writes the job summary, so the first pipeline apply reported success although the outputs were never published. Both workflows now run their steps with `pipefail`, and `publish` explains a root whose outputs predate the current renderer instead of crashing.
2. The demo app's committed root was rendered before the `github_repository` output existed; it is re-rendered.

# 1.0.1: fixes from the live run

## Fixed

1. **The region policy refused every recovery bucket**: Cloud Storage reports locations in capitals (`US-CENTRAL1`), and the rule compared literally. Found by the first pipeline apply, which refused the demo app's plan exactly as designed; the comparison now ignores case.
2. **Cloud Workflows rejected the spending-cap workflow's discovery step**: its expressions cannot contain map or list literals (`{}`, `[]`). Missing fields are now tested with `in` and ordered switch conditions; the test asserts no such literal appears, and the rendered workflow was deployed and deleted as a throwaway before this release.

## Upgrade steps

1. Re-apply the foundation root (one change: the cap workflow's source).

# 1.0.0: infrastructure as pull requests (Phase 6, item 6.3)

## Added

1. **One Terraform root per application.** `infra/gcp/project` is the foundation root (APIs, observer and pipeline identities, cost controls, alert channels, spending cap), applied by the operator and rarely changed. Each application gets `infra/gcp/apps/<service>`, three generated files with every input written out, its own state (`apps/<service>/<environment>`), and the project-wide values read from the foundation's state. `lab-deploy infra render APP` writes a root from the controller configuration, the app's reviewed `platform.yaml` and GitHub's immutable ids; the new tool `propose_infrastructure` renders it and opens the pull request on the platform repository.
2. **The pipeline.** `infra-plan.yml` plans every root a pull request touches with a read-only identity, checks the plan against the platform's policies and posts one comment per application, updated in place. `infra-apply.yml` applies on merge with a write identity from the protected `infra` environment, checks the policy again right before applying, and publishes the outputs to the application's GitHub environment when the `APP_REPOS_TOKEN` secret exists (otherwise it prints them to copy). Both stay dormant until the three `INFRA_*` repository variables are set from the foundation root's `pipeline` output. `workflow_dispatch` applies named roots by hand.
3. **Policies** (`labdeploy/infra.py`, tested, standard library only): no service account keys, no owner or editor grants, no destruction of resources that hold data, nothing outside the allowed region, no public database addresses, every managed resource labelled `lab-deploy-app`, and no touching of the platform's own identities from an application root.
4. **Managed labels.** Services, registries and recovery buckets carry `lab-deploy-managed` and `lab-deploy-app`; releases merge their revision labels in with `--update-labels` instead of replacing them. The spending-cap workflow now discovers the services to close by label, so adding an application no longer needs a foundation re-apply.
5. **Pipeline identities** in the foundation root: a Workload Identity pool for the platform repository, a plan identity (viewer, security reviewer, state reader) and an apply identity whose roles are listed in `pipeline.tf`; the apply identity is only reachable from the `infra` GitHub environment.

## Changed

- The single root (`infra/gcp/main.tf`, `variables.tf`, `outputs.tf`, `moved.tf`) is gone; `services` is no longer a variable. `~/.local/share/lab-deploy/migrate-state.sh` splits an existing state into the two new ones once, without touching the cloud, and the operator's apply script now applies the foundation root.
- `lab-deploy decommission` tells the operator to run `terraform destroy` in the app's root; the pipeline never destroys.

## Upgrade steps

1. Run the state migration once, then apply the foundation root (it adds the pipeline identities and the cap's list permission).
2. Set `INFRA_WIF_PROVIDER`, `INFRA_PLAN_SERVICE_ACCOUNT` and `INFRA_APPLY_SERVICE_ACCOUNT` on the platform repository and create the `infra` environment restricted to `main`; optionally add `APP_REPOS_TOKEN`.
3. Merge the rendered root of each existing application; the pipeline applies the label changes. Runner code changed (`--update-labels`), so regenerate the app workflows and raise `platform.minimum_commit`.

# 0.9.5: clean plans

## Fixed

1. **Every plan wanted to update the dashboard.** The Monitoring API stores dashboards with an explicit axis on each data set and without zero positions or empty group lists, so Terraform saw a difference on every run. The dashboard is now written in that stored form; a read-only plan against the fully applied staging project reports no changes.

## Upgrade steps

None: the next plan is simply clean.

# 0.9.4: fix from the live apply, part three

## Fixed

1. **The uptime check was rejected again** ("validate_ssl feature is currently only allowed on uptime_url checks"): certificate validation is only offered for plain-URL checks. The option is dropped for the Cloud Run target; the check still uses HTTPS. A throwaway check with the exact final shape was created through the API and deleted again before this release, so the next apply is expected to pass.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check and the health alert.

# 0.9.3: fixes from the live apply, part two

## Fixed

1. **The uptime check was rejected** ("Service agent authentication is not supported for arbitrary URLs"): Google offers the Monitoring service agent's OIDC token only for checks that name a Cloud Run service as a `cloud_run_revision` resource, not for a plain URL. The check now names the service by project, location and name, as Google's own example does, and the health alert filters on that resource type.
2. **The saturation alert was rejected**: the Monitoring API only offers greater-than and less-than comparisons. "n or more instances" is now expressed as "more than n-1", which is the same test for whole numbers.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check, the health alert and the saturation alert that the previous apply could not.

# 0.9.2: fix from the live apply

## Fixed

1. **Cloud Workflows rejected the spending-cap workflow** ("Unterminated expression"): its log messages contain a colon followed by a space inside expressions that were not quoted, which YAML reads as a nested key. The six string-building expressions are now single-quoted, as Google's workflow syntax guide asks, and a test renders the file with Terraform's placeholders, parses it and checks that every expression is a complete scalar.
2. **`get_service_health` reported every service as not ready.** Cloud Run v2 returns the Ready condition as `terminalCondition`; the observer looked for it in `conditions`, which only holds RoutesReady and ConfigurationsReady. Found on the first live call after the observer identity existed.

## Upgrade steps

1. Re-run the Terraform apply: it creates the workflow and the Eventarc trigger that the first apply could not.
2. Restart the controller.

# 0.9.1: fix from the live run

## Fixed

1. **The observer error quoted gcloud's impersonation warning** instead of the reason the token could not be minted; it now quotes gcloud's error line.

# 0.9.0: always on, on a laptop

## Added

1. **Live views.** `get_service_health` shows what Cloud Run serves right now (readiness, revisions with the plan and commit they carry, ingress), the last hour's requests, server errors, p95 latency, instances and memory, and whether it agrees with the release this controller recorded. `get_recent_logs` returns the newest log lines at or above a severity. Both read through a viewer-only **observer identity** created by Terraform: on a machine the identity is attached; on a laptop the operator impersonates it through gcloud (`observer_impersonators`), so the controller never holds a key and never gains write access. Configure `observer.service_account`.
2. **Auto-deploy.** `auto_deploy: true` on an application makes the controller release the tip of main whenever it is neither the live release nor already attempted, as the identity `auto-deploy`, with every guardrail (pause, policy, cost ceiling, platform pin) still applied. Interval `auto_deploy_interval_seconds` (default 300).
3. **Watchdog.** Once a day the controller checks its configuration file, the GitHub credential's expiry, each app's schedules, platform pin and newest release attempt, records the result, and posts problems to `notify_webhook_url`. `lab-deploy watchdog` runs it on demand.
4. **Backups.** A consistent copy of the database every night into `backup_dir` (default next to the database), kept `backup_keep_days` (default 14). `lab-deploy backup` runs it on demand.
5. **Controller watch** from GitHub: the platform repository's `controller-watch.yml` pings the public health address every thirty minutes when the `CONTROLLER_HEALTH_URL` repository variable is set; a failed run means the laptop, the tunnel or the controller is down, and GitHub's failed-workflow email says so.
6. `get_controller_status` reports the observer, the auto-deploy apps and the background jobs' last and next runs.

The always-on machine of the roadmap's Phase 5 is deferred by the operator's choice; everything above also works there with the observer identity attached.

## Upgrade steps

1. Apply the Terraform change (observer identity, its viewer roles and your impersonation grant) and set `observer.service_account` in the configuration.
2. Optionally set `notify_webhook_url`, `auto_deploy: true` per app, and the `CONTROLLER_HEALTH_URL` variable on the platform repository.

# 0.8.0: more kinds of apps

## Added

1. **Configuration and secrets in `platform.yaml`.** `env` holds plain values set on every revision; `secrets` lists names of Secret Manager secrets called `<service>-NAME` that Cloud Run mounts as variables. The reviewed file is the whole truth: values not in it are removed from the revision. Before deploying, the runner checks that every referenced secret exists with an enabled version and names the missing ones. Terraform grants the runtime identity read access, and the deployer existence checks, only to secrets named after the service, through IAM conditions. Values are created by the operator with gcloud and never pass through Claude.
2. **Apps without tests.** `tests: none` in `platform.yaml` skips the unit-test step knowingly: the plan's checks say `skipped_by_config`, every plan carries a warning, and the run annotates itself. An unexpected skip still shows as `not_run`.
3. **Request timeout** `timeout_seconds` (1 to 300) replaces the fixed sixty seconds.
4. **Reproducible builds.** The analyzer flags a `requirements.txt` without hashes and recommends a `requirements.lock`; the test step installs such a lock with `--require-hashes` when the repository has one.
5. **`lab-deploy decommission APP`** prints the ordered checklist that retires an application without leaving a bill or a dangling trust.

## Upgrade steps

1. The specification gained fields, so every plan hash changes: regenerate the app workflows with this platform commit **before** deploying, and set `platform.minimum_commit` to it so the controller refuses older pins.
2. Apply the Terraform change (Secret Manager API and the two conditional grants).

# 0.7.1: fix from the live run

## Fixed

1. **The cost-report issue step failed** because the right-sizing job never checks out the application repository and the GitHub CLI looked for one; every call now names the repository explicitly.

# 0.7.0: real cost control

## Added

1. **Spending cap** (`spending_cap` in Terraform, on by default with a budget): every budget notification reaches a Pub/Sub topic; an Eventarc trigger runs a Cloud Workflow that closes ingress on every platform service once the budget line is crossed, and optionally detaches billing at a higher line. No code to build. Reopen a service with `gcloud run services update SERVICE --ingress=all`; the next release reopens it as well.
2. **Terraform in two modules**: `modules/project` (APIs, scanning, budget, cap, alert channels, tag role, billing dataset) and `modules/service` (one per application, keyed by service name in the `services` map). `moved.tf` migrates the pilot's single-module state in place; a plan against the staging state showed every resource moving and nothing destroyed.
3. **Living cost-report issue**: the weekly right-sizing workflow creates or updates one issue per repository with the summary, proposals, drift and actual spend (`issues: write`).
4. **`propose_rightsizing(application)`** turns the latest report's concrete proposals into a pull request against `platform.yaml`, editing values in place so comments survive, with the savings and the cost bounds before and after. The staging policy and the cost ceiling still apply.
5. **Actual spend**: with `billing_export_dataset` in Terraform, the billing export enabled once in the Billing console, and the `BILLING_EXPORT_TABLE` repository variable, the weekly report and the issue show the project's net spend by Google service next to the list-price figures.
6. **Schedule watchdog**: `list_applications` reports, per app, whether GitHub still runs the recovery and right-sizing schedules, when they last ran and how they concluded. GitHub pauses schedules after sixty idle days.
7. Image scanning is documented as a per-project cost choice (`image_vulnerability_scanning`).

## Upgrade steps

1. Rewrite `terraform.tfvars` into the `services` map (see docs/SETUP.md) and apply; the state migrates automatically.
2. Regenerate the app workflows (the right-sizing workflow changed).
3. Optional: enable the billing export to the new dataset and set `BILLING_EXPORT_TABLE`.

# 0.6.2: two more findings from the live exercise

## Fixed

1. **A reused image's release was reported as `completed_unverified`** because the skipped build job has no test and audit steps. The plan's `passed_in_earlier_release` checks are now kept and count as passed.
2. **The audit trail recorded a final outcome twice** when the record was re-saved with the recovery run's result. An event is now recorded only when the state actually changes.

# 0.6.1: fixes from the live steering exercise

## Fixed

1. **Status of a plan pinned to an older commit** was rejected as a workflow identity mismatch: GitHub reports the run's head as the branch tip, not the plan's commit. The unique plan id in the run title is the identity; the head check now applies only to plans of the tip itself.
2. **Image reuse ignored a rolled-back release.** A rolled-back release's image is still the attested image of that commit, so it now counts for reuse.
3. **Provenance verification failed when the build was skipped**, because the registry login lived in the push step. A dedicated "Configure registry access" step now runs in both modes.
4. **Stale serving tags could not be deleted**: the deployer's writer role creates tags but cannot delete them. Terraform adds a one-permission custom role on the repository, and the reconcile reports a failed deletion instead of abandoning the update.

## Upgrade steps

1. Apply the Terraform change (custom role and binding) and regenerate the app workflows.

# 0.6.0: steer after a release

## Added

1. **`rollback_staging(application)`**: traffic returns to the split that served before the live release, through the recovery workflow's new `rollback_plan_id` input. No build; seconds. The runner refuses when the checkpoint belongs to another release, the release did not complete, or traffic has moved since. The rolled-back release stops counting as live.
2. **`cancel_deployment(plan_id)`** cancels the GitHub run; the recovery workflow restores traffic if the release had started changing it.
3. **Deploy a specific commit**: `create_deployment_plan(application, commit_sha=...)` accepts any commit already on main. The controller checks the history through GitHub's compare, and the workflow checks it again before building.
4. **Image reuse**: a commit released before is redeployed from its already-attested image. The build job is skipped, the configuration comes from the commit itself, provenance is verified again, and the plan's checks say `passed_in_earlier_release`.
5. **`compare_with_live(application)`**: commits and files on main since the live release, what kinds of change they are (code, dependencies, Dockerfile, configuration, workflows), and the cost bounds before and after when `platform.yaml` changed.
6. **`deploy_latest(application)`** plans and dispatches in one call.
7. **Pause switches** honoured without a restart: `paused: true` on the whole configuration or on one application refuses new plans and dispatches; reads keep working. The controller re-reads its configuration file whenever it changes; an invalid file is reported by `get_controller_status` and ignored.
8. **`lab-deploy sessions`** lists Claude's sign-in sessions and ends one or all of them.
9. **Platform pin check**: `list_applications` reports the platform commit each app's workflows pin; with `platform.minimum_commit` set, dispatch refuses older pins and names the fix.
10. **Drift in the weekly report**: live Cloud Run settings compared with the reviewed `platform.yaml`, differences named in the report and its summary.

## Changed

- The explicit Deployments API steps added in 0.5.0 duplicated the record GitHub keeps for the deploy job's environment; the job's environment now carries the service URL instead, and the `deployments` permission is gone.
- CI passes the template files to the workflow linter explicitly; given the directory, it audited only the platform's own workflow.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (`update-workflows --all`); the deploy workflow has a new `image_digest` input and the recovery workflow a `rollback_plan_id` input.
2. Optionally set `platform.minimum_commit` to this platform commit so apps on older pins are refused until regenerated.

# 0.5.0: see every step

## Added

1. **Full timeline in status.** `get_deployment_status` reports every step of both jobs with timing, names the first failed step, fetches the release receipt and vulnerability summary once the run ends and stores them with the plan, shows the release step's own phases with timestamps (preparing, deploying, promoting, succeeded or restored), the gcloud error line, whether traffic was restored and which revision serves, the recovery run that followed a cancelled or lost release, and a plain `next_action` for every state. `wait_seconds` (up to 45) waits for a change before answering.
2. **Release history.** A finished run's outcome, receipt, revision, digest and commit are stored, so history survives GitHub's artifact retention. `list_plans` shows outcome, commit summary and revision; `list_applications` shows the live release this controller recorded and the commits main holds beyond it. Plans carry the commit message, author and date.
3. **Candidate logs.** When a candidate fails, the runner puts its last fifty log lines into the receipt (Terraform grants the deployer `roles/logging.viewer`).
4. **Revision labels, release tags, serving tags.** Every revision is labelled with its plan id and commit; every pushed image also gets an immutable `release-<plan>` tag; the release and right-sizing jobs tag each serving revision's digest `serving-<revision>` and the registry cleanup policy keeps those, so an image that still serves is never deleted.
5. **Notifications.** A final job in the deploy, recovery and right-sizing workflows posts the outcome with the receipt summary and a link to the `NOTIFY_WEBHOOK_URL` repository secret (Slack or Discord incoming webhook). Nothing is sent when the secret is unset; routine recovery runs stay quiet.
6. **GitHub Deployments.** Each release is recorded through the Deployments API (in progress, then success, failure or error, with the service URL), visible on the commit and in the repository's Environments view. The deploy job gains `deployments: write`.
7. **Audit everything.** Every tool and REST call, sign-in, failed-login burst, client registration, token reuse and revocation is recorded with identity, client address, outcome and duration. New `list_events` tool and `GET /events`; deployers see every identity, read-only identities see their own. Optional `log_file` setting writes a rotating controller log.
8. **Runtime alerts in Terraform** (`runtime_alerts`, default on): an authenticated uptime check on the health path, alert policies for the health check, 5xx responses, p95 latency, instances at maximum, memory above 80% and error lines in the log, email and webhook notification channels (`alert_email`, `alert_webhook_url`), and a console dashboard. Outputs `console_url` and `dashboard_url`.
9. **`update_app_workflows` tool and `lab-deploy update-workflows --all`.** Regenerate an app's three workflows against the platform's current (or a given) commit and open one pull request per repository. Needs `platform.repository` in the configuration and a credential with Contents, Pull requests and Workflows write on the app repositories.

## Upgrade steps

1. Add `platform: {repository: OWNER/lab-deploy}` to the configuration; optionally `log_file`.
2. Apply the Terraform change (logging viewer, cleanup policy, alerts, uptime check, dashboard). Set `alert_email` and, if wanted, `alert_webhook_url` and `health_path` in `terraform.tfvars`. In a new project create the Monitoring service agent first: `gcloud beta services identity create --service=monitoring.googleapis.com`.
3. Add the `NOTIFY_WEBHOOK_URL` secret to each app repository if notifications are wanted.
4. Regenerate the three app workflows with the new platform commit, now with `update-workflows --all`.

# 0.4.1: housekeeping and the two cost leaks

## Changed

1. **Recovery schedule** runs every six hours instead of every ten minutes. The completion trigger after each release handles the normal case, and a lost runner leaves traffic on the previous revision, so recovery is not urgent. On a private repository the old schedule consumed roughly 6,000 to 9,000 Actions minutes a month.
2. **Cost estimates list per-release overheads** outside the Cloud Run figures (the image scan fee of about USD 0.26 per release, Actions minutes, registry storage) and warn when the recorded list prices are more than 90 days old.
3. **GitHub failures are explained in plain words** to Claude and to REST callers: expired token, missing permission, rate limit, missing repository or file, refused dispatch, outage. An app without `platform.yaml` is told to run `propose_configuration`.
4. **New `get_controller_status` tool**: version, dispatch and sign-in switches, registered apps, GitHub credential expiry with a two-week warning, pricing freshness and uptime. The MCP handshake reports Lab Deploy's version, and the controller logs a warning at startup when the credential is about to expire.
5. **`lab-deploy sync-pins`** copies action pins from the platform CI workflow into the templates, so a Dependabot update becomes one command. `actions/checkout` and `actions/setup-python` move to v7.0.1 and v7.0.0.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (schedule and action pins changed).
2. Dependabot's Terraform provider update (google 7.x to 8.x) is deferred to the Terraform restructuring planned in `docs/ROADMAP.md`.

# 0.4.0: browser sign-in for Claude chat

## Added

1. **OAuth 2.1 authorization server** (`labdeploy/oauth.py`), enabled by `oauth.public_url`: protected-resource and authorization-server metadata, dynamic client registration restricted to Claude's callback and loopback redirects, PKCE S256, five-minute single-use codes, one-hour access tokens, thirty-day rotating refresh tokens with reuse detection, revocation, and a sign-in page at `/login` that accepts only a configured operator token. Tokens and codes are stored as SHA-256 hashes in the existing SQLite file. Unauthenticated requests now answer `WWW-Authenticate: Bearer resource_metadata="..."` so Claude discovers the sign-in. Rotating an operator's token hash ends its sessions.
2. **Request gate** accepts either a static operator token or a live access token; the OAuth endpoints are rate limited per client address and failed sign-ins count against the existing failed-login budget.

## Upgrade steps

1. Optional: set `oauth.public_url` to the controller's public https origin and list that host in `allowed_hosts`. Without it nothing changes.
2. The database gains four `oauth_*` tables automatically on start.

# 0.3.1: fixes from the live staging trial

## Fixed

1. **Identity federation condition:** GitHub's OIDC `sub` claim now embeds numeric IDs, so the Workload Identity Federation condition matches the `environment` claim plus the immutable repository and owner IDs, the branch and the workflow paths. Re-apply Terraform; the provider updates in place.
2. **Status right after dispatch:** a run that GitHub has not finished creating is reported as not visible yet instead of failing the identity check.
3. **Traffic and tag commands verify the routing state** rather than trusting gcloud's exit code, which is non-zero whenever the service's newest revision is not Ready.
4. **Failed candidates stay in place:** Cloud Run refuses to delete a service's newest revision. The failure path restores traffic, drops the tag, and records `service_ready` with a note; the previous revision keeps serving until the next successful release.
5. **Receipts record the underlying error text** (the command's ERROR line) for the failed step and for a failed restoration.

## Upgrade steps

1. Apply the Terraform change (one in-place update of the identity provider).
2. Regenerate the three app workflows with the new platform commit.

# 0.3.0: hardening against 2026 practice

## Added

1. **Rate limiting** in the authentication layer: per-identity request budgets and per-client failed-login budgets (`rate_limit_per_minute`, `auth_failures_per_minute`), answering 429 with `Retry-After`.
2. **Tool safety hints and structured output**: every MCP tool declares a title, `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, publishes an output schema and returns `structuredContent`. The demo walkthrough shows the hints.
3. **Cloud Run startup probe** from the reviewed `health_path` and port (`--startup-probe`, 60 seconds of grace), so Cloud Run itself gates traffic.
4. **Build provenance**: `actions/attest-build-provenance` signs the pushed image digest and `gh attestation verify` checks it before release; the deployment job gains `attestations: write`.
5. **Image vulnerability summary** (`labdeploy/scan.py`): waits for the Artifact Analysis scan, writes `vulnerability-summary.json` into the release receipt, and blocks the release on critical findings when `BLOCK_ON_CRITICAL_VULNERABILITIES=true`. Terraform enables the Container Scanning API (`image_vulnerability_scanning`) and grants the deployer read access to results.
6. **Repository hygiene**: ruff configuration and a clean lint, zizmor workflow linting (with one documented suppression for the recovery workflow's `workflow_run` trigger), pip-audit of the platform lock and a Terraform validation job in CI, Dependabot for actions and providers, `.github/CODEOWNERS` (placeholder owners), `SECURITY.md`, `.pre-commit-config.yaml`, and tests that the templates' action pins match the platform's own.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit; the deployment workflow has new steps and permissions.
2. Apply the Terraform change (Container Scanning API and the deployer's occurrence-viewer role; set `image_vulnerability_scanning = false` to opt out).
3. Optionally set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES=true` and the controller's rate-limit fields.
4. Replace the CODEOWNERS placeholder with real maintainers and choose a license.

# 0.2.0: cost optimizer

## Added

1. **Cost bounds in every plan** (`labdeploy/cost.py`): idle floor, worst-case compute ceiling and an optional traffic-scenario range computed from Google's published Tier 1 Cloud Run list prices, with assumptions and exclusions listed. New `estimate_cost` tool, `GET /applications/{id}/cost` route and `lab-deploy cost` CLI command.
2. **Operator cost ceiling**: `cost_ceiling_usd` per application in the controller configuration. Plans whose worst-case monthly compute exceeds it are refused at planning and again at dispatch.
3. **Cost findings** from Google's cost-optimization guidance: warm instances with their monthly price, unmeasured memory or CPU, low concurrency, the effect of `scaling.maximum`, and authenticated-only exposure.
4. **Measured right-sizing**: `templates/rightsize-gcp.yml` (weekly, read-only, pinned platform code, no application checkout) reads Cloud Monitoring usage through `labdeploy/telemetry.py`, prices it, proposes `platform.yaml` changes with estimated savings, lists `google.run.service.CostRecommender` recommendations, and uploads `cost-report.json`. New `get_cost_report` tool and `GET /applications/{id}/cost-report` route fetch the newest artifact.
5. **`concurrency`** is now a reviewed `platform.yaml` field (default 80, the Cloud Run default). The analyzer drafts 8 for synchronous frameworks and adds a review question. Existing spec hashes change; create fresh plans after upgrading.
6. **Terraform cost controls**: Artifact Registry cleanup policies (dry run by default), recovery-bucket lifecycle rules, project-level `roles/monitoring.viewer` and `roles/run.viewer` for the deployer identity, the right-sizing workflow in the WIF condition, and an optional monthly billing budget with current and forecast thresholds.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit and commit `rightsize-gcp.yml`.
2. Apply the Terraform change (new APIs, IAM grants, cleanup policies, WIF condition; budget only if configured).
3. Optionally add `cost_ceiling_usd` to each application in the controller configuration and restart the controller.
4. Create fresh plans; the spec hash now includes `concurrency`.

# 0.1.2: local demo mode and hardening

## Added

1. **Local demo mode:** `python -m labdeploy.demo serve` runs the real controller and MCP server against an application directory on disk, with dispatch permanently disabled and a repository adapter that refuses every write. `python -m labdeploy.demo walkthrough` exercises every tool and two policy refusals. `labdeploy.server:create_app` never uses this module, so the running service still substitutes no local or fake GitHub data.
2. **Plan listing and audit trail:** the `list_plans` tool and `GET /plans` return the caller's own plans, newest first. Status responses include the persisted `events` for the plan (`planned`, `dispatching`, `dispatched`, `dispatch_unknown`).
3. **Model instructions:** the MCP server sends usage instructions during the handshake: tool order, plan expiry, what `dispatched` means, and that policy refusals must be relayed rather than worked around.

## Changed

4. **Pinned base images:** both Dockerfiles pin `python:3.12-slim` to its multi-arch index digest, with the refresh command in a comment.
5. **CLI errors:** validation, policy, missing-file and overwrite failures print one plain line and exit non-zero instead of a traceback.
6. **Database connections** are closed after every operation; commit and rollback behaviour is unchanged.
7. **Authentication scheme:** `bearer` is matched case-insensitively per RFC 9110; tokens are still compared by hash.
8. **Configuration** rejects wildcard `allowed_hosts` entries, which the MCP transport would silently never match.

## Upgrade steps

No configuration or workflow changes are required. Rebuild the controller image to pick up the pinned base image.

# 0.1.1: deployment review fixes

## Fixed

1. **Stale status:** the controller now reads jobs from the exact GitHub run attempt. The returned lifecycle state and individual check results reflect the observed steps. Overall workflow success without expected step evidence is `completed_unverified`, never an invented pass. Dispatch bookkeeping stays separate so a completed plan cannot be redispatched by mistake.
2. **Unbound destination:** application registration now requires a typed cloud target. Plans carry it; the controller rejects registry drift; the workflow rejects environment drift before authentication. Project, region, service, artifact repository, deploy/probe service accounts, WIF provider and recovery bucket are included. Older plans without a target cannot deploy.
3. **Pending deployments replaced:** release and recovery workflows use the same concurrency group with `queue: max` and `cancel-in-progress: false`. GitHub currently allows up to 100 pending runs with this setting. It is not an unlimited queue, and dispatch order is not guaranteed. The installed actionlint release does not yet recognize this documented field; all other lint errors remain fatal.
4. **Interrupted releases:** before traffic changes, the runner saves its exact rollback map to a versioned GCS object. Writes use generation preconditions. A graceful interruption attempts immediate traffic restoration. A separate pinned recovery workflow runs after release completion and on a ten-minute schedule, under the same concurrency group. It recovers unfinished checkpoints, refuses target mismatches or unrelated current revisions, and never rolls back a checkpoint marked successful.

The recovery workflow does not check out or execute the triggering app's code. It operates only on the operator-configured service and saved checkpoint. Scheduled GitHub runs may be delayed or disabled; recovery requires working GitHub/GCP APIs and configured credentials. This improves crash recovery without claiming instantaneous or guaranteed availability.

## Upgrade steps

1. Publish this updated platform source and record its full commit SHA.
2. Review and apply the Terraform change: a private versioned recovery bucket, its scoped IAM grant, and the additional recovery workflow in the WIF condition.
3. Set the new `RECOVERY_BUCKET` GitHub staging environment variable from Terraform output.
4. Add each app's `target` block to the controller configuration; values must match the GitHub staging environment exactly.
5. Regenerate and review **both** `deploy-gcp.yml` and `recover-gcp.yml` using the new platform commit. The render command refuses overwrite: write temporary output files and review their diff before replacing existing workflow files.
6. Restart the controller and create fresh plans. Older workflow templates do not accept the new `target_json` dispatch input.
7. Verify a normal release, failed candidate, post-promotion failure, and killed-runner recovery in disposable staging.

## Still outside this patch

Telemetry-driven resource optimization, dollar-cost estimates, AWS support and production readiness are features/integration milestones, not silently completed by these bug fixes. The analyzer still reports conservative defaults and review assumptions. Full live cloud testing remains required.
