# Runbook

How Fast Deploy (formerly Lab Deploy) is operated day to day and what to do when something is off. Written 2026-10-12 for the staging
platform (`lab-deploy-staging-f29a`, operated from Claude chat by one person); update it when the setup changes.

## Owner and escalation

| Role | Who | How |
|---|---|---|
| Owner and only operator | Brian Boti | Claude chat (the connector); `scripts/machine.sh` on the laptop for the controller's machine |
| Alerts | the `alert_email` and optional `alert_webhook_url` in `infra/gcp/project/terraform.tfvars`; GitHub's failed-workflow emails; the `NOTIFY_WEBHOOK_URL` secret on each app repository | arrive without anyone watching |
| Escalation | nobody else today | when a second operator joins, follow "Succession" below: their own principal, `observer_impersonators`, `.github/CODEOWNERS`, required review (`docs/SETUP.md` 7l) and a rehearsal |
| Incidents | the operator | section "An incident" below: detect, mitigate first, communicate, review |

The platform itself has no service level objective; each application in production has one (section Production, "The
objective and its error budget"). The targets below are what the platform is built to do, and the watchdog reports when
it does not.

## The platform repository is the single control point

Since 1.15.0 the platform repository (`brianboti/fast-deploy`) holds the contracts of the repositories taken as it is
(`apps/<service>/platform.yaml`) and runs their releases (`deploy.yml`, `recover.yml`, `rightsize.yml`, each in the
environment `staging-<service>`). Whoever can merge to its `main` can deploy those applications, so **its protection
never loosens**: `main` keeps requiring a pull request with the `test` and `terraform` checks, administrators included;
no environment gets a bypass; `.github/CODEOWNERS` covers `/apps/`. Nothing in the repository of such an application
can release it, and nothing in its own pipeline can reach another application's identity.

- **A contract pull request** (`Contract for NAME, taken as it is from ORG/REPO`): read its table of choices and the
  "Please check" lines, fill in settings the code reads under `env` and `secrets` (names only; values go into Secret
  Manager with `gcloud`), then merge. The controller never merges one. Merging is the review, and the first release
  follows once the cloud setup is applied.
- **A release pull request** (`Release NAME at <commit>`): it moves `source.commit` and nothing else. The controller
  merges it itself only under `auto_merge` for that application, when the requester may merge their own requests and
  the checks have passed; a newer one closes the older one. Merging any change to a contract releases it within a
  minute (`contract_release` events in `list_events`).
- **Code nobody has reviewed is contained** (1.16.0): its services send all traffic into the lab's network, which has
  Private Google Access and no Cloud NAT, so they reach Google's APIs and the lab's private addresses, never the
  internet. The infrastructure policy refuses a NAT, a router or an internet route in an application root, because one
  would open every contained service at once. Keep it that way; an app that must reach the internet says
  `egress: internet` in its contract, through a reviewed pull request.
- **A private application repository** is refused while this repository is public: the build logs and the image the
  build uploads would be readable by anyone. Making this repository private changes that, and moves the Actions minutes
  to the lab's bill.
- **Reading private repositories later** (Phase 15): set the repository variable `APPS_APP_ID` and the secret
  `APPS_APP_PRIVATE_KEY` of a GitHub App installed on those repositories with contents read; each run then gets a token
  narrowed to the one repository, valid for an hour. Until then the runs read public repositories with their own token.

## Production

Since 1.18.0 (Phase 16, decision D37) an application can have a production: a project of its own under the organization,
with its own foundation, state, identities, budget and spending cap. Staging stays where releases are tried first.

- **Production infrastructure is merged by a person.** `add_production` opens each root as a pull request titled
  `Production infrastructure for NAME`, and the controller never merges it, whatever `may_merge_own_requests` says.
  Read its plan comment like any other, merge, then approve the apply in the environment `infra-production`. That
  approval is the safety net: nothing applies to production unless a person agreed, whoever merged.
- **Who approves a promotion** is the configuration's `production.reviewers`: the required reviewers of every
  `production-<service>` environment on the platform repository, with self-review prevented. To change them, edit the
  list (`scripts/machine.sh config-get` and `config-put`) and ask for `add_production` again for each application; it
  rewrites the environment's reviewers. While no reviewer is around, nothing reaches production. That is intended.
- **The foundation** is applied by the operator: `scripts/apply-foundation.sh PROD_PROJECT production`, from
  `infra/gcp/project/production.tfvars`. Staging's settings are in `staging.tfvars`. Never put a `terraform.tfvars`
  back: Terraform would load it into both, and the script refuses production while one exists.
- **The drift check** covers production roots too and names them `production-<service>`.

### Approve or reject a promotion

`promote_to_production` (since 1.18.1) starts the platform repository's **Promote to production** run, which waits in
`production-<service>`. Open it from the link Claude gives. Its summary holds the approval block, written by code from the
checked inputs: the commit, the exact image by digest, the staging release it came from, where it goes, its size and its
cost bounds in production. **Review deployments**, then approve or reject. Rejecting changes nothing. Whoever started the
run may not approve it; the controller starts runs as the GitHub App, so your approval counts. If the run waits too long
(GitHub ends an unapproved run after 30 days), ask for the promotion again.

If **Review deployments** does nothing, check which GitHub account the browser is signed in with: only a required reviewer
can approve, and an approval that did not arrive leaves the run waiting with nothing in its approvals (found on
2026-10-18). From a terminal signed in to `gh` as the reviewer, after reading the approval block:

```bash
gh api -X POST repos/OWNER/fast-deploy/actions/runs/RUN_ID/pending_deployments -F "environment_ids[]=$(gh api repos/OWNER/fast-deploy/environments/ENVIRONMENT --jq .id)" -f state=approved -f comment="approved after reading the approval block"
```

The same works for an apply waiting in `infra-production`. The promote job checks the approval GitHub recorded before it
does anything.

### How a promotion reaches everyone

Since 1.18.3 (roadmap item 16.3, decision D38) a promotion moves production's traffic in steps: 10, 50, then 100 percent
of the visitors (`production.rollout`), each step watched for `production.bake_seconds` (300 by default) by the bake's
rules. A crash, more than 5 percent of answers being server errors, or latency above three times the earlier version's,
at any step, brings the earlier split back for everyone. Too little traffic to judge passes with a note, as in staging.
Each secret runs at the version that was newest when the promotion released it; a new version reaches production only
through a promotion.

### Undo a promotion

Ask Claude to "roll APP back in production" (`rollback_production`). It runs as the rollback of `promote.yml`, the one
workflow production's deployer trusts, so it waits for a production reviewer's approval like everything in production.
Once approved, traffic returns in seconds to the split before the live promotion, as its checkpoint recorded; nothing is
built. The runner refuses if the checkpoint belongs to another promotion or if production's traffic moved since. Data the
newer version wrote stays.

If the tool cannot help (the controller is down, or traffic was moved by hand), do it by hand. The promotion's receipt
(`get_deployment_status`, or the run's `release-receipt` artifact) names `previous_traffic`:

```bash
gcloud run services update-traffic SERVICE --to-revisions PREVIOUS_REVISION=100 --project PROD_PROJECT --region us-central1
```

A promotion that stopped halfway restores production's traffic itself; if its runner was lost, the next promotion
restores the checkpoint before anything else.

### Release tags

Since 1.18.8 every promotion that succeeds is tagged in the application's repository: `production/SERVICE/DAY-PLAN`
(for example `production/lab-demo/2026-10-19-1a2b3c4d`), an annotated tag at the promoted commit that names the plan,
the staging release, the image's digest, who approved it and the run. `git tag -l 'production/*'` lists what reached
production and when, and `git diff` between two tags shows what a promotion changed. The tag is written once and never
moved. One that cannot be written (a repository the App is not installed on, a rule there) is named in the promotion's
status and in `list_events` (`release_tag`); the release itself is not affected. Creating a tag starts any workflow of
that repository that runs on every push without a branch filter; the platform's own workflows never do.

### The objective and its error budget

Since 1.18.5 (roadmap item 16.4, decision D39) every production application has one objective: 99.5 percent of its
requests answered without a server error over 30 days. The 0.5 percent it may miss is the error budget. Since 1.26.0
(decision D58) it counts the users' requests only, from two log-based counters that read Cloud Run's request log with the
health path left out; before, Cloud Run's request metric counted the uptime check's calls too, so a quiet application's
budget measured mostly the uptime check, and the rollback drill's 18 planted errors spent it for a month. The uptime check
calls the health path every minute from three US regions and keeps its own alert ("health check failing") and its own
line in the health view (its 30-day pass rate). Three alerts reach the alert channels, each only when its long and its
short window both burn and at least 100 user requests came in the long window, so a few errors on a quiet application
never page:

- **"error budget burning fast"** (CRITICAL): ten times the sustainable rate over the last hour and the last five
  minutes, which is an outage. Ask Claude for "demo's health in production" and its errors. If the newest promotion
  caused it, roll production back (`rollback_production`); otherwise follow the error lines.
- **"error budget burning slowly"** (ERROR): twice the sustainable rate over six hours and the last half hour, which is a
  leak. Look at the errors today and fix it before the budget runs out.
- **"error budget used steadily"** (WARNING): at the sustainable rate for a whole day. Look this week.

`get_service_health` with `environment: production` shows how much of the budget is left, "no user requests" when there
were none, and for a spent budget the day its errors leave the 30-day window at the latest. Below a quarter it says so:
keep risky changes out of production until it recovers. An application's first promotion carries a readiness checklist
in its approval block, covering probes, alerts, runbook, backup, capacity and the rollback path, with what to do where
one falls short. A production root made before 1.18.5 has no objective yet: ask for `add_production` again, which
proposes the root again, and merge it.

### Production's data

A production database is recoverable to any moment of the last seven days: Postgres keeps a week of write-ahead log beside
its daily backups, Firestore a week of versions. Deletion protection stays on, and nothing in the pipeline destroys a
resource that holds data. To recover after a bad migration or a mistake, restore into a new instance or database. Never
overwrite the one serving: Postgres with `gcloud sql instances clone SERVICE NEW --point-in-time TIME --project PROD_PROJECT`,
then move the application to it through its root.

**The data restore drill**, twice a year, for each application with a database in production:
`scripts/data-drill.sh PROD_PROJECT SERVICE` restores the Postgres database as it was fifteen minutes ago into a throwaway
clone, checks that it runs and holds the `app` database, then deletes the clone. It costs the clone's minutes, about a
cent. Record the result in `docs/VALIDATION.md`. A Firestore database is not drilled yet.

### A custom domain

Since 1.18.9 an application's contract may name a domain of the lab's own (`domain:`, production only,
docs/DEVELOPER-CONTRACT.md section 6b): a global load balancer with a Google-managed certificate and Cloud Armor. Before
the first one, apply production's foundation once (`scripts/apply-foundation.sh PROD_PROJECT production`): its apply
identity gains the load balancer role and a Cloud Armor role, and its plan identity two read permissions. Staging's
foundation does not change.

1. **The budget first.** The domain alone bills about $25.25 a month. Raise `monthly_budget_usd` in
   `infra/gcp/project/production.tfvars` by at least that and apply the foundation, or the spending cap closes production
   before the month ends; raise the application's `cost_ceiling_usd` too, if it has one.
2. **The root.** Once the contract names the domain, ask Claude for `add_production`: it opens the production root's pull
   request. Its plan comment shows the floor and, as an exposure change, the name the load balancer answers at. Merge it
   and approve the apply in `infra-production`.
3. **The name.** Point the domain's A record at the apply's output `domain_address`. Google issues the certificate once the
   name resolves there, usually within an hour: `gcloud compute ssl-certificates list --project PROD_PROJECT` shows it
   ACTIVE.
4. **Check.** `curl -sI https://DOMAIN/` answers 403 without a token (a `lab-members` app shows the sign-in page), and
   `curl -sI http://DOMAIN/` redirects to HTTPS.

Since 1.30.0 the domain has its own uptime check through the load balancer, with its certificate validated, and the alert
**"SERVICE: DOMAIN unreachable"** (ERROR) when it fails from more than one region for ten minutes or the certificate has
under 14 days left (Google renews it about 30 days before). It fires right after the domain is added, until step 3 is
done. When it fires later: check that the A record still points at `domain_address`, then the certificate's status in
`gcloud compute ssl-certificates list`; a certificate that is not ACTIVE says why in its `managed.domainStatus`.

The `run.app` address keeps answering behind the same sign-in, since the release probes it there: the domain adds a name
and a rate limit, not a new way in. To stop paying, remove `domain:` and ask for `add_production` again: the root's next
apply removes the load balancer. When the spending cap fires it closes the service, so the domain stops answering too,
but the load balancer bills on until it is removed.

### A new secret version

Add the version with `gcloud secrets versions add SERVICE-NAME --project PROD_PROJECT --data-file=-`, then ask Claude to
"promote APP to production" again. The same staging release is copied again (nothing is rebuilt), and its new revision
takes the new version, through the same split and watch as any promotion. Until then production keeps the version it
was released with, even when instances restart.

### The rollback drill

Twice a year, and after a change to the release: a planted fault the probes cannot see must be caught at 10 percent.

1. In the demo application's `platform.yaml`, set `env: FAULT_EVERY: "2"` through a pull request: every second request
   to `/` then answers 500, while `/health` stays fine. Release it to staging (the bake there sees no traffic and passes).
2. Promote it, and before approving start the traffic in a terminal signed in with `gcloud`:
   `scripts/promotion-traffic.sh PROD_PROJECT SERVICE SERVICE-rPLANID16 400`, where the revision is the service's name,
   `-r` and the first 16 characters of the promotion's plan id. It waits for the 10 percent step and sends 400 requests
   with your sign-in token, about 40 of which reach the new revision: enough for the bake's minimum of 20.
3. Expected: the bake fails on errors at 10 percent, the earlier split serves everyone again, and the script says the
   earlier split came back. `get_deployment_status` names the share it failed at.
4. Remove `FAULT_EVERY` the same way, release, and promote with the script sending 300 requests at each step: every
   step's bake passes on real traffic. Then ask for `rollback_production` and approve it: traffic returns to the earlier
   revision in seconds. Promote again to end on the fixed version.
5. Record the results in `docs/VALIDATION.md`. Done first on 2026-10-18.

The drill spends error budget: its planted errors count against production's objective for 30 days. On a quiet
application a few dozen of them exceed the 0.5 percent it may miss, so the health view shows the budget spent until they
leave the window (found on 2026-10-19). Run the drill when a spent budget for a month is acceptable, or send more good
traffic around it.

### The cap-fire drill

Twice a year, and after a change to the spending cap: a budget notification at the cap's line must close production's
services, and they must open again afterwards.

1. Pick a quiet moment: the drill closes every managed service in the project for a minute or two.
2. In a terminal signed in with `gcloud` as the project's owner, run `scripts/cap-drill.sh PROD_PROJECT`. It reads the cap
   the foundation deployed and refuses a project whose cap could detach billing (a drill must never reach that line) or
   whose cap is in force already. It lists what it will close and asks you to type **yes**.
3. It publishes a made-up budget notification to the budget topic, the path a real one takes (Pub/Sub, Eventarc, the
   workflow): a cost of 100.01 against a budget of 100 at a cap of 1.0. No money moves, and the real budget is not read.
4. Expected: every managed service turns internal-only within a minute and answers 404 from outside, and the workflow's
   execution says `cap enforced at 1.0001 of the budget: N services closed`. The script then reopens each service with
   the call the cap itself makes (no new revision, and no `lab-deploy-cap-lifted` label, so the cap stays armed), starts
   any database it stopped, and looks again a minute later in case Pub/Sub delivered the message twice. If anything fails
   on the way, it still reopens what it closed.
5. Record the result in `docs/VALIDATION.md`.

The uptime alert waits for five minutes of failures, so a drill normally sends none; one that arrives closes itself.
Closed services answer 404, not a server error, so the drill spends no error budget. The same script drills staging's
cap (`scripts/cap-drill.sh STAGING_PROJECT`), where it closes every application at once.

## What "up" means

- `GET /healthz` on the controller answers 200 at its address. A Cloud Monitoring uptime check calls it every minute from
  three US locations and alerts when more than one fails for five minutes; the platform repository's **Controller
  watch** workflow calls it every thirty minutes and fails loudly when it does not answer (`CONTROLLER_HEALTH_URL`).
- A release reaches `succeeded` or a final failure within about twenty minutes of a plan being dispatched; the status
  tool names the phase it is in and the next action.
- The daily watchdog finds nothing (`get_controller_status` shows its last findings), the nightly backup is less than
  a day old and the copy in the backup bucket matches.
- Every alert policy is enabled in Cloud Monitoring and reaches a channel (section "Prove the alerts work").

## An incident

Anything that hurts the people using an application, or puts data, a credential or money at risk: production down or
wrong, an alert that the error budget burns fast, a credential seen where it should not be, a change nobody made through
the pipeline. The procedure is NIST SP 800-61 Rev. 3 (April 2025) in the shape that fits one operator: detect, mitigate
first, communicate, review. Note the time of each step as you go, in a private note: never in the public platform
repository, where an incident's details help an attacker.

1. **Detect and decide.** What arrives on its own, each alert's subject starting with its environment and its severity
   in the email: `SERVICE: error budget burning fast` (CRITICAL, act now), `... burning slowly` (ERROR, today) and `...
   used steadily` (WARNING, this week), `SERVICE: health check failing` (CRITICAL), `SERVICE: server errors` (ERROR),
   `[ENV] the spending cap could not act` (CRITICAL), the budget emails, the daily digest and a release report that says
   "worse" (both on the issue **Fast Deploy: today**), the drift issue, GitHub's Dependabot and secret-scanning alerts,
   and Google's own emails. Ask Claude for the application's health in production and its deployment status for the facts.
   It is an incident when production is affected or something is exposed; anything else is one of the sections below.
2. **Is it Google?** An alert "Google reports an incident" or the digest's line, or every application failing at once
   (section "Google's own incidents"): then nothing of yours needs rolling back.
3. **Mitigate first**, before looking for the cause:
   - A bad release in production: ask for `rollback_production` and approve it (seconds); in staging, `rollback`.
   - An application doing harm or running up cost: close it from outside as the cap does,
     `gcloud run services update SERVICE --region us-central1 --project PROJECT --ingress=internal`, and reopen it with
     `--ingress=all` once fixed.
   - A credential exposed: replace it first and look for its use after (section "Rotate credentials"). An application's
     secret: revoke it where it was issued, add the new value as a version and promote again (production keeps the
     version it was released with, section "A new secret version"), then disable the old version
     (`gcloud secrets versions disable VERSION --secret SERVICE-NAME --project PROJECT`). Disabled first, it would stop
     production's instances from starting.
   - A change nobody made through the pipeline: section "Drift reported", step 1, then put the code's version back.
   - The controller doing something wrong: `scripts/machine.sh stop` holds it. Production keeps serving without it, and
     nothing reaches production without a reviewer's approval anyway.
   - Data damaged or deleted: close ingress so nothing writes more, then restore into a new instance (section
     Production, "Production's data"). Never overwrite the one serving.
4. **Communicate.** Tell the people who use the application what is affected and when you will say more, then keep to
   it. When a credential or personal data may have been exposed, tell the institution's information security office the
   same day. With protected health information (Phase 17), the institution's privacy office decides on notifications:
   HIPAA asks for them without unreasonable delay and within 60 days of discovering a breach.
5. **Review**, within a week: a short write-up without blame (what happened, the timeline, how it was found, what made it
   better or worse) and one issue or pull request per fix, starting with whatever would have caught it sooner. Change
   this runbook where a step was missing or wrong.

## After a release: the report and the daily digest

Since 1.26.0 (roadmap Phase 23) the platform speaks first, once a day, and at once when a release did worse. Both are
written by code from the controller's records and the observer's reads; neither changes anything.

- **The release report** (decision D57). A day after a release or promotion succeeded (sooner when a newer one replaced
  it), the controller reads the new version's requests besides the health path, its server errors and how fast 95
  percent were answered, against the previous version's week before the switch, plus the uptime check. "Worse" is a
  server-error share above 1 percent and above twice the week before, a 95 percent latency above twice the week before,
  or the uptime check passing less than 99 percent of the time; it is posted at once with its numbers. "Too little
  traffic to compare" means fewer than 100 requests on a side: only the probes and the uptime check vouch for that
  release. The verdict shows in the release's status, `list_applications` and `get_service_health`. On "worse": ask
  Claude for the errors, and roll back if the release caused them (sections "An incident", "Undo a promotion").
- **The daily digest** (decision D60), at `digest_hour_utc` (13 UTC by default): what shipped, each report, the
  applications with server errors, error lines in their log or missed uptime checks, drift, re-pins and drills waiting,
  the watchdog's problems, the trust numbers on Mondays, or "all quiet". It is a comment on the issue **Fast Deploy:
  today** of the platform repository, sent to `notify_webhook_url` too, and `get_digest` shows it in chat. The watchdog's
  own webhook post is folded into it. When it reaches no one (the App lacks the Issues permission, no webhook) or stops,
  the watchdog says so, and the Controller watch workflow fails after two days without it.
- **The cost line** (since 1.27.0, decision D63): every day the digest gives each project's spend this month from the
  billing export, its forecast (the month so far scaled to the month) and its recorded budget, and says when a forecast
  is above its budget. On Mondays it also counts each project's alert policies and uptime executions against Google's
  free allotments, and names the services nobody but the health checks called in 30 days: candidates to retire with
  `scripts/decommission.sh`, which a person decides. The weekly cost report sets the window before beside the window and
  names what moved by more than a fifth and a dollar.
- **The trust numbers** (decision D61) in `get_controller_status`: releases that reached users, went back on their own,
  were rolled back by a person; the change failure rate; the median time back to a good release; how many watches after
  a switch had too little traffic; restores that may have been for nothing (the same commit passed later). They are for
  reading, never a threshold.

## Requests refused

Alert **"SERVICE: requests refused"** (ERROR, since 1.29.0), the digest's "requests refused (429)", or a health-view note.
A request was answered 429. When its log line says "The request was aborted because there was no available instance",
every instance the service may run was busy for 30 seconds and a person or a program was turned away; Cloud Run's request
metric leaves those requests out, so neither the server-errors alert nor the objective counts them. Otherwise the
application refused it itself (a rate limit of its own).

1. Ask Claude for the service's health and its WARNING lines: how many, since when, and whether the line names "no
   available instance".
2. Turned away at the limit: raise `scaling.maximum` in `platform.yaml` (the cost bound rises with it; the policy allows 3)
   or `concurrency` when the code serves requests in parallel safely; release. A burst that will not come back needs
   nothing.
3. "At maximum instances" (WARNING) now fires at the application's own maximum (2 by default), from the root's next render;
   an application limited to one instance is watched by this alert alone.

## Slow starts

The digest names each application whose slowest start of the day passed 10 seconds. Since 1.29.0 the start-up probe tries
every second (it was every five, which added about four seconds to every start of every application) and startup CPU
boost gives a starting instance twice its CPU. What remains is the application's own work at start. The developer
shortens it: load heavy libraries lazily or once, one gunicorn worker with threads (`--workers 1 --threads 8 --timeout
0`), start Node.js with `node file.js` rather than `npm start`, and read data computed before the release from a mounted
bucket rather than computing it at each start (`docs/DEVELOPER-CONTRACT.md`). A warm instance (`scaling.minimum: 1`,
about $9.72 a month) removes the first start for good; the uptime check already keeps most services warm between calls.

## Google's own incidents

Alert **"[ENV] Google reports an incident in us-central1"** (WARNING, since 1.29.0) or the digest's "Google reports an
incident". Google's Personalized Service Health wrote an incident that may touch this project. Errors and slowness that
start with it are likely Google's: a rollback will not help, and a release now may fail for Google's reasons.

1. Read it in the console (Service Health) or at status.cloud.google.com: which products, which locations, Google's next
   update.
2. Hold releases and promotions until it closes (`paused: true` in the configuration, or simply wait).
3. Afterwards, look at the next day's release reports and the digest: anything still wrong is the application's.

## A dependency's alert

Since 1.30.0 a topic or a Postgres database the contract declares under `needs` has an alert of its own, under the same
`runtime_alerts` switch as the service's.

- **"SERVICE: messages waiting on TOPIC"** (WARNING): the oldest unacknowledged message on the subscription
  `SERVICE-TOPIC` is over an hour old. The code that reads it stopped, fails before it acknowledges, or nothing reads it by
  design. Ask Claude for the service's health and its error lines. When nothing is meant to read it yet, the alert tells
  you once and stays open until the messages are taken or expire with the subscription's retention (seven days).
- **"SERVICE: database near a limit"** (ERROR): the disk is over 90 percent full, or over 80 percent of the connections
  are in use (25 on the smallest instance, 100 on the `sla` tier). A full disk stops writes; at the limit new connections
  are refused. The disk grows by itself up to 20 GB; past that, raise the limit in the module or clean up. Too many
  connections is nearly always a pool per request or a leak: one small pool per instance (`max_size` around 5) is enough
  at the platform's three instances.
- **"SERVICE: database backup failed"** (ERROR): Cloud SQL's automated backup failed, read from its audit log, since no
  metric exists for it. Until one succeeds, a restore goes back to the last good backup. Look at the instance's Backups
  tab; a backup on demand (`gcloud sql backups create --instance SERVICE --project PROJECT`) shows whether it fails again.
  An instance the spending cap stopped takes no backup and raises nothing.

## A serving image's findings

The Monday digest (since 1.30.0) reads what Artifact Analysis holds for every image that serves, staging and production,
and names the critical and high findings that have a fix, with what is new since last Monday. Google publishes new
findings every day, so an image released clean collects them while it serves. The digest also pulls each image from the
machine afterwards: Artifact Analysis keeps analysing an image only while it was pulled within 30 days, and Cloud Run
never pulls a serving image again after its deploy. The pull stays in one region and costs nothing.

1. Ask Claude for the application's health or its last release's scan to see the findings; the fix is nearly always a
   newer base image or newer packages.
2. Release the fix the usual way. The digest's line goes away the Monday after.
3. "A known malicious package" in a serving image: release a fixed build at once. Until it is out, close the service as
   the spending cap does, `gcloud run services update SERVICE --ingress internal --project PROJECT`, and open it again
   with `--ingress all` after the release.
4. "Not pulled": the machine could not read the image (its roles, or an image over 4 GiB). Apply the foundation again if
   the observer's roles are missing; a very large image belongs in a bucket anyway.

Staging's analysis exists only while `image_vulnerability_scanning` is on in its settings; with it off the lines say
nothing was found, because nothing was looked at.

## Personal data in a data file

Since 1.30.0 each release reads the data files its image carries (64 KiB or more, outside the system's and the packages'
folders) and sends the first 256 KiB of each text file to Sensitive Data Protection before the image is pushed. The
summary of the run's "Inspect data files for personal data" step lists what it found, by kind and count, never the
values.

- **Code nobody has reviewed** with a likely social security, passport, driver's licence, card, bank or medical record
  number stops before the push. Move the data to a bucket the lab controls (`needs.buckets`) or a database, or review the
  code and set `trust: reviewed`. Made-up values that look real count too: Sensitive Data Protection cannot tell them
  apart.
- **The lab's own code** is reported with a warning and goes on. Read the summary: real people's data in an image is
  readable by anyone with the registry's reader role.
- **"not inspected: ... not set up for this deployer yet"**: the foundation (it enables the API) or the application's
  root (the deployer's role) has not been applied since 1.30.0. The release goes on uninspected, with this warning, until
  both are. Any other unanswered inspection stops code nobody has reviewed and lets the lab's own go on.

Binary files (SQLite, Parquet, Excel, pickles) are listed in the summary, not inspected. The first GiB a month of the
billing account is free, then $3 a GiB; a release sends at most 2 MiB.

## Binary Authorization

Since 1.30.0 every application's service opts in to its project's Binary Authorization policy, and every release and
promotion attests its image with the project's attestor key once it has verified the image's provenance. The policy
starts in dry run: Cloud Run deploys an unattested image and writes "would have refused" to the audit log.

1. **Check the dry run** after a few releases of each application:

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" logName:"cloudaudit.googleapis.com%2Fsystem_event" "dry run"' --project PROJECT --freshness 14d --limit 20
   ```

   Nothing found: every release was attested. A line names the revision and the image that would have been refused. A
   release without an attestation says so in its "Attest the image for Binary Authorization" step: the application's root
   was not applied since 1.30.0, or its environment lacks `BINAUTHZ_ATTESTOR`. In dry run a failed attestation is a
   warning and the release goes on; once enforced it stops the release, since Cloud Run would refuse the image.
2. **Enforce**: set `binary_authorization = "enforced"` in the project's settings file and apply the foundation. From then
   on Cloud Run refuses a new revision of an unattested image, whoever deploys it, with `ContainerImageUnauthorized`.
3. **An emergency** (a fix must go out and the attestation fails): `gcloud run deploy SERVICE --image IMAGE
   --breakglass "why"` deploys once and is logged; the next deploy clears it. Fix the cause, then release normally.

Cloud Run checks each new revision, never one already serving, so a rollback is never refused and nothing that serves
stops when the policy is enforced. The policy binds only services that opt in, and anyone with `run.admin` could take a
service out; in this lab only the pipeline holds it. Free on Cloud Run; the key costs about $0.06 a month.

## The controller's machine

Since 1.17.0 (Phase 15, decision D36) the controller runs on its own machine in the staging project: `fast-deploy`, one
Always Free `e2-micro` in us-central1-a with a 30 GB standard disk, at its own reserved address (or a DNS name, when one
is set). Caddy answers HTTPS with a Let's Encrypt certificate for that address (six days long, renewed every few days)
and hands requests to the controller on 127.0.0.1:8000. Litestream copies the database to
`gs://PROJECT-lab-deploy-backups/machine/litestream/`, at most a minute behind, and the nightly backup puts a copy and the
configuration beside it in `machine/`. The machine signs in to GitHub as the GitHub App and to Google as its own identity,
`lab-deploy-controller` (the observer's viewer roles, its own folder of the backup bucket and the App's key): no token or
key file is on its disk. It costs the external IPv4 address, about $3.65 a month, plus a few cents of Secret Manager and
Cloud Storage operations; the machine and its disk are inside the free tier (one `e2-micro` per billing account). There
is no open SSH port: `scripts/machine.sh` goes through Identity-Aware Proxy, and gcloud signs you in with OS Login.

| To | Run on the laptop, in the repository |
|---|---|
| See it | `scripts/machine.sh status`: the three services, the commit it runs, disk, memory, `/healthz` |
| Read its logs | `scripts/machine.sh logs` (the controller), `logs litestream`, `logs caddy`, `logs startup` (the boot script), `logs update` (its own updates) |
| Restart the controller | `scripts/machine.sh restart` |
| Update the platform it runs | after the merge, `scripts/machine.sh update $(git rev-parse origin/main)`: only a commit on main installs, and the previous one comes back if the new one does not answer within a minute. With `platform_updates` on, the machine does this by itself (section "After a release") |
| Run its startup script again | `scripts/machine.sh startup`, after a foundation apply changed the script: what is already right stays, and Caddy restarts for a second |
| Change its configuration | In the browser at `https://MACHINE/settings` (below). The fallback, when the page cannot be reached: `scripts/machine.sh config-get /tmp/machine.yaml`, edit, `scripts/machine.sh config-put /tmp/machine.yaml`: checked before it goes; principals, applications and settings apply at once, `oauth` and `github_app` changes need `restart` |
| Back up now, or drill | `scripts/machine.sh backup`, `scripts/machine.sh drill` |
| Forget a retired application | `scripts/machine.sh unregister APP`, after `scripts/decommission.sh APP --yes` (section "Retire an application") |
| Hold it while a laptop takes over | `scripts/machine.sh stop` (not even a reboot starts it); `scripts/machine.sh start` releases the hold |

**Watch it.** `https://MACHINE/settings` opens on its Monitor section (since 1.28.0): Overview (what needs you first,
then services healthy, releases this week, the change failure rate, this month's spend per project and one card per
application), Health (one application's traffic, errors, latency and saturation over 1 h, 24 h, 7 or 30 days with its
releases marked, 30 days of uptime, production's error budget, its newest error lines when you ask), Releases (the DORA
numbers, a timeline, every release with its verdict and run), Cost (the month against budgets and caps, where the money
goes, right-sizing proposals, each application's bounds) and Activity (the audit trail, events per day, the background
jobs, backups, drift, the GitHub credential). Any operator token signs in, and Monitor shows the applications that token
may see, as Claude's tools do. Live numbers are a few minutes behind, kept five minutes for everyone, and spend is as of
the daily digest; nothing on these views changes anything, and they read only what the controller keeps and a few
Monitoring queries of the observer identity (free: far below the million time series a month Google does not charge for).

**Change its configuration.** Open `https://MACHINE/settings` (the machine's address or DNS name, as in Claude's
connector) and sign in with an operator token; its Configure section opens only for a principal with `applications: ["*"]`
and `can_deploy`, and refuses anyone else. The sign-in lasts an hour (`settings_session_seconds`), and a restart, a rotated
token or a removed principal ends it. Five tabs: Applications (a list, and each application's own page with its cost ceiling, auto-deploy, auto-merge, bake,
viewers, infrastructure settings and links to where its contract, Terraform root and secrets live; for one onboarding
registered the switches go under `application_settings`), Platform (the auto-deploy interval, the
watchdog webhook, the spend caps you recorded, backups, `platform_updates`), People (principals, a new token shown once),
Production (reviewers, rollout, bake) and Advanced (the whole file; the only place for `database`, `oauth` and
`github_app`, which need `restart`). Every save shows the change as a diff first and writes nothing until you confirm;
pausing, provenance `unavailable`, rotating a token and removing a person also ask you to type the name. The confirm step
checks the file exactly as the controller does, replaces it in one step, keeps the previous one in
`/var/lib/fast-deploy/history/` (the last twenty), copies it to `gs://BUCKET/machine/config.yaml` and records a
`configuration` event; the controller applies it at once. The page does not touch contracts, Terraform roots, GitHub
environments or secrets: it links to them. Comments in the file are not kept, and a `config-put` made while a change waits
for its confirm step makes that step refuse.

Docker is on the machine only for the lock files onboarding compiles (decision D27); it starts when first used, and its
containers can reach neither the metadata server (the machine's identity) nor any private address.

What restarts by itself: systemd restarts the controller, Litestream and Caddy when one stops; Google restarts the machine
after a host failure and moves it live for maintenance; a security update that needs a reboot gets one at 09:00 UTC, and
the controller is back within a minute.

### The machine is gone or broken

The machine keeps nothing that is not also in its folder of the backup bucket, and a fresh one restores itself: the
configuration from `machine/config.yaml`, the commit it ran from `machine/installed-commit`, the database from the
Litestream replica (or, failing that, the newest nightly copy, up to a day old). It never starts the controller with an
empty database, which would forget every plan and release.

1. Replace it: `gcloud compute instances delete fast-deploy --zone us-central1-a --project PROJECT` (a deletion: your
   call), then `scripts/apply-foundation.sh PROJECT` creates a new one with the same address.
2. About five minutes later `scripts/machine.sh logs startup` says where the database came from and that the controller
   runs; `scripts/machine.sh status` and `get_controller_status` in Claude confirm it. The connector keeps working: the
   address is the same and the sessions are in the restored database.

When Google Cloud itself is out, the laptop is the fallback:

1. `scripts/machine.sh stop` if the machine still answers: two controllers would release the same applications twice.
2. Bring the newest database to the laptop: `gcloud storage ls gs://PROJECT-lab-deploy-backups/machine/` and
   `gcloud storage cp gs://PROJECT-lab-deploy-backups/machine/deployments-STAMP.db ~/.local/share/lab-deploy/deployments.db`,
   or the replica with `litestream restore -o ~/.local/share/lab-deploy/deployments.db gs://PROJECT-lab-deploy-backups/machine/litestream/deployments.db`
   (`brew install litestream`; at most a minute old).
3. `scripts/start.sh` (it stays off while the machine answers) and point the connector at the ngrok address.
4. When the machine is back: stop the laptop, then `scripts/install-machine.sh APP_ID --replace` takes the laptop's
   database back to the machine.

If Google ends the free tier (it gives thirty days' notice), the machine costs about $6.11 a month more. The fallback
design, Cloud Run with a scheduler job (`docs/RESEARCH-2026-10-16.md`, section 5.3, design B), is not built.

## After a release

Since 1.20.4 the two chores that follow every merge into the platform repository's `main` can run by themselves: the
machine installs the new code, and each application's three workflow files are re-pinned to it. They run only once
`platform_updates` is in the machine's configuration; without it both stay manual (`scripts/machine.sh update`,
`update_app_workflows`).

```yaml
platform_updates:        # in the machine's configuration: scripts/machine.sh config-get, edit, config-put
  machine: true          # the machine installs main by itself
  workflows: true        # applications are re-pinned to the commit the controller runs
  delay_minutes: 60      # the window in which a release can still be stopped
  quiet_minutes: 10      # the controller restarts only after this long without a tool call
```

What happens after each merge, in this order:

1. **Main's checks.** The controller waits for the **Platform tests** run that the merge itself started on `main`. When it
   fails, nothing is installed until a newer commit lands and passes.
2. **The machine**, only when the merge changed something the controller runs (`labdeploy/`, `templates/`, `schema/`,
   `requirements.lock`); documents, contracts under `apps/`, scripts and infrastructure never restart it. An hour after
   main's checks passed on that change (a newer change to those files starts the hour again), at a moment when nobody has
   used Claude's tools for ten minutes and no release is on its way to GitHub, the controller writes the commit into a
   request file next to its database. The controller cannot restart itself, so the machine's own update service
   (`fast-deploy-update`, run as root) takes the request: it installs the commit with `fast-deploy-install` (only a
   commit on `main`, and never one older than the commit that runs), waits for `/healthz`, watches the controller two more
   minutes, and puts the previous commit back if it stops answering or restarts. It asks for one commit at a time and
   waits for the machine's report before the next. Claude's tools are away while the controller restarts (seconds, up
   to a minute when a background job is finishing); releases already running carry on on GitHub.
3. **The workflows.** An hour after the controller started on the new commit (never less than ten minutes, whatever the
   delay, and never while the machine is installing), each application whose workflows pin an
   older runner gets the usual pull request, "Update Fast Deploy workflows to platform X", and the one it opened for an
   earlier commit is closed. An older runner means a change to the templates, the lock files or the modules the workflows
   run (the list in `labdeploy/updates.py`, checked by a test); a release that changes only the controller re-pins
   nothing. The controller merges the pull request itself only where it merges an application's own pull requests: the
   application's `auto_merge` allows it (its own setting, else `onboarding.auto_merge`), its checks have passed, and it
   changes exactly the three workflow files as the platform rendered them. Otherwise it waits for you. Merging releases an
   application with `auto_deploy`, as a re-pin by hand does. An application whose contract the platform keeps has no pin,
   and nothing happens for it.
4. **The record.** Every step is in `list_events` ("platform update", "workflow update"), and `get_controller_status` says
   what happens next and when. `platform.minimum_commit` never moves by itself: raising it stays your decision.

To stop it:

| To stop | Do |
|---|---|
| Everything, releases included | `paused: true` in the machine's configuration |
| The machine's own updates | `platform_updates.machine: false` |
| The re-pins | `platform_updates.workflows: false` |
| One application's automatic merge | `application_settings.APP.auto_merge: false`: its re-pins wait for you |
| One re-pin | close its pull request: it is not opened again for that commit |
| The controller itself | `scripts/machine.sh stop`: nothing is installed while it is held |

A change through `config-put` reaches the background jobs within fifteen seconds; they read a changed configuration on
their own. An update the machine has already started finishes, with its own way back.

When something goes wrong:

- **"went back from X"**: the new commit did not answer or restarted. `scripts/machine.sh logs` (the controller) and
  `scripts/machine.sh logs update` say why. The machine does not try X again, nor a later commit that leaves the
  controller's code as X had it (a contract release or a document on top of it); fix it on `main`, and the next commit
  that changes what the controller runs and passes is installed.
- **"could not install X yet"** (`not_installed`): nothing changed on the machine (GitHub could not be reached, the
  controller was held, or another install held the machine); it is asked for again half an hour later.
- **"nothing reported for over an hour"**: `scripts/machine.sh status` shows the update service and its last result,
  `scripts/machine.sh logs update` what it did. After two hours without a report the controller stops waiting: the commit
  it runs by then counts as installed, or the commit is asked for again.
- **A re-pin waiting for you**: `get_controller_status` names the pull request and why (its checks failed, it changes more
  than the three files, it has conflicts, or `auto_merge` is off).

Setting it up, once, after the merge that brings 1.20.4:

1. Apply the foundation: `scripts/apply-foundation.sh PROJECT`. The machine's startup script changes in its metadata, in
   place (one change, if nothing else is pending); nothing restarts.
2. `scripts/machine.sh startup` runs that script again on the machine: it adds `fast-deploy-update` and the path unit that
   starts it, and the folder its results go to. Caddy restarts for a second.
3. `scripts/machine.sh update $(git rev-parse origin/main)` installs 1.20.4 by hand, the last time.
4. Add `platform_updates: {}` (or the block above) to the machine's configuration with `config-get` and `config-put`.

Cost: nothing at idle, a few GitHub API calls every five minutes. A re-pin costs what one by hand costs: the
application's checks (free minutes for a public repository) and, with `auto_deploy`, one staging release.

## The laptop is off or asleep

Only while the laptop is the controller: before the move to the machine, or as its fallback. What keeps working without the controller: every workflow already dispatched on GitHub (a release finishes or restores
traffic on its own), the recovery workflow every six hours, the weekly right-sizing report, the spending cap in Google
Cloud, every Cloud Monitoring alert. What stops: Claude's tools (the connector reports a connection error), auto-deploy,
the daily watchdog, the nightly backup. Nothing is lost: plans expire, and the next start continues.

1. Wake the machine and run `scripts/start.sh` (or install the launch agent once with
   `scripts/install-launch-agent.sh`, after which the controller starts at login and restarts after a crash).
2. Keep the machine awake while it is the controller: System Settings → Energy, or `caffeinate -s` in the terminal.
3. Ask Claude for `get_controller_status`: the tunnel, the configuration, the schedules and the last backup.

## Restore the controller on another machine

This is the laptop's procedure; the machine restores itself ("The machine is gone or broken" above). Rehearsed on 2026-10-12 (`docs/VALIDATION.md`). What the backup bucket holds: the nightly database copy and the
configuration file (principals as hashes, targets, settings). What it does not hold, by design: the operator token's
clear text, the GitHub token, the ngrok credentials.

1. Install Python 3.12 or newer, Docker (for foundation applies), the Google Cloud CLI and ngrok; sign in with
   `gcloud auth login` as the project owner.
2. `git clone https://github.com/brianboti/fast-deploy ~/Downloads/lab-deploy && cd ~/Downloads/lab-deploy &&
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
drill has passed for 92 days, whenever `backup_bucket` is set), run it where the controller runs. For the machine, from
the laptop:

```bash
scripts/machine.sh drill
```

For a laptop that is the controller:

```bash
python -m labdeploy.cli restore-drill        # LAB_CONFIG or --config for another configuration file
```

It needs gcloud signed in as someone who may read the backup bucket (on the machine, its own identity). It restores
nothing into the live database, which only gains the drill's audit event. It lists the host's folder,
`gs://BUCKET/controller/` or `gs://BUCKET/machine/` (`backup_prefix`), downloads the newest `deployments-*.db` into a temporary folder (deleted afterwards), opens it
read-only, runs SQLite's integrity check, counts the plans, audit events, plan state changes, registered applications and
onboarding records, downloads `config.yaml` and checks that it loads, prints a short report, and records the outcome as
the audit event `restore_drill` (`list_events` shows it). A copy older than 48 hours passes with a warning: the nightly
backup runs inside the controller, so a laptop that was off for days has an old newest copy. On the machine it also
restores the live copy with Litestream, checks it the same way and says how many minutes it is behind the database (a
warning past ten).

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
   A stopped database stays stopped through later infrastructure applies (since 1.15.0): only that command starts it.
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

The issue **Fast Deploy: infrastructure drift** (named Lab Deploy: … before 1.14.0) on the platform repository, or a watchdog notification, names the root,
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

## A release stopped by a safety gate

Since 1.16.0 (docs/DEVELOPER-CONTRACT.md 5a) a release can stop before anything changes in the cloud; the run's
annotations and the step summary name the file, the line and the fix, and Claude's status answer repeats them.

| The run says | Meaning | Do |
|---|---|---|
| `Dockerfile policy: ...` | The Dockerfile breaks a rule (a download piped into a shell, `sudo`, disabled TLS, a copied key, root for unknown code) | The developer fixes the Dockerfile; for a repository taken as it is, nothing can be added on its behalf: say what the line needs |
| `Secret scan: ... a key that works right now` | TruffleHog found a credential and its provider accepted it | Treat it as leaked: rotate it at the provider first, then have it removed from the repository and its history; values go to Secret Manager |
| `... critical or high finding with a fix available, in code nobody has reviewed` | The vulnerability gate (14.4) | Update the base image or the packages; or a person reviews the code and merges `trust: reviewed` in its contract |
| `known malicious package` | A dependency is a known malicious package (OSV's MAL- advisories; for a Node.js lockfile, also GitHub's malware advisories, CWE-506) | Remove it; check whether the build machine or any key it saw may be affected |
| `Dependency audit: ...` (a Node.js service, since 1.20.0) | OSV-Scanner found, in the lockfile and before anything was installed, a known malicious package, a critical or high finding with a fixed version, or an entry from outside the npm registry, or the audit did not finish | `dependency-audit.json` in the release bundle names each package; the developer updates them and commits the lockfile. A malicious one: nothing of it ran (install scripts are off), but remove it at once. An audit that did not finish: run the release again |
| `Buildpacks: ... reviewed code only` | A Node.js service without a Dockerfile asked for Google's buildpacks, which run install scripts, while nobody has reviewed its code | Leave it on the platform's Node.js image (`build.recipe: platform`), or a person reviews it and merges `trust: reviewed` |
| `The build context is ... MiB` | Over 1 GiB | Move the data to a bucket (`needs.buckets`) |

Threat detection for unknown code (14.6) is the operator's switch: Security Command Center Premium at project level
detects reverse shells, credential hunting, cryptomining and malicious Python inside running Cloud Run services (second
generation only, which unknown code always uses). It bills about $0.0071 per vCPU-hour of running instances, close to
nothing for services that scale to zero; turn it on in the console (Security Command Center, project-level activation)
and read its findings there.

## Node.js lines and their end of life

A Node.js service (`runtime: node-http`, since 1.20.0) names its line in `node`, and every release refuses a line from
the day it reaches its end of life (Node's release schedule, read 2026-10-19; `labdeploy/models.py` NODE_END_OF_LIFE):

| Line | Status on 2026-10-19 | Refused from |
|---|---|---|
| 22 | Maintenance LTS | **2027-04-30** |
| 24 | Active LTS until 2026-10-20, then Maintenance | **2028-04-30** |
| 26 | Current; Active LTS from 2026-10-28, and from then the default of a new contract | 2029-04-30 |

A month before each date, ask Claude which applications run that line (the contracts say `node: "22"`), and have each one
move: its contract's `node` (and its Dockerfile's `FROM node:X`) to the newer line, through a reviewed pull request. From
the date, a plan or a promotion on the old line is refused with the reason (the pipeline's "Validate reviewed
specification" refuses it too); what already serves keeps serving.

The weekly pin refresh (next section) keeps the `node:<line>-trixie-slim` index digests, OSV-Scanner, the npm a Node 22 lock is
made with (`LOCK_NPM` in `labdeploy/scaffold.py`; it goes when 22 does) and, since 1.20.6, `actions/setup-node` current:
setup-node is pinned by commit in `templates/deploy-gcp.yml` and `templates/repo/ci-node.yml`, and the refresh writes it
there and in `.github/workflows/deploy.yml` at once. After a template change by hand, regenerate
`.github/workflows/deploy.yml` with `labdeploy.workflows.central_deploy` (a test keeps them equal).

## The weekly pin refresh

Since 1.20.5, `.github/workflows/pins.yml` runs on Mondays at 05:23 UTC and keeps current the pins Dependabot does not
follow; since 1.20.6 these include the GitHub Actions the templates use. Dependabot reads only `.github/workflows`, so it
would move such an action in the workflows and never in the templates they are made from; it now follows only the
Terraform providers and the actions no template uses (`hashicorp/setup-terraform`). `labdeploy/pins.py` lists each pin
with its official source, the line of releases the platform follows, and every place it is written:

| Pins | Written in | Read from |
|---|---|---|
| TruffleHog, Trivy, OSV-Scanner, hadolint, pack and Cosign: version and SHA-256 | `templates/deploy-gcp.yml`; Cosign also `.github/workflows/promote.yml`, hadolint also `.github/workflows/ci.yml` | GitHub's release API and each project's checksum list |
| The buildpacks builders and run images, by digest | `templates/deploy-gcp.yml` | gcr.io's list of images, with their upload times |
| The `python:X.Y-slim` and `node:<line>-trixie-slim` index digests, with the release and the Debian each carries | `labdeploy/scaffold.py`; 3.12 also the platform's and the demo app's Dockerfiles | Docker Hub's tag API |
| uv and its wheel hashes; the npm for Node 22 locks and its integrity | `labdeploy/scaffold.py` | PyPI's index API; the npm registry |
| OpenTofu: version and SHA-256 | `.github/workflows/ci.yml` | GitHub's release API and OpenTofu's checksum list |
| Caddy (SHA-512) and Litestream (SHA-256) | `infra/gcp/modules/project/controller-startup.sh` | GitHub's release API and each project's checksum list |
| The actions the templates use (`actions/checkout`, `setup-python`, `setup-node`, `upload-artifact`, `download-artifact`, `attest-build-provenance`, `attest-sbom`, `create-github-app-token`; `google-github-actions/auth`, `setup-gcloud`), by commit, written `owner/name@<commit> # vX.Y.Z` | everywhere each appears: `templates/*.yml`, `templates/repo/*.yml`, `labdeploy/workflows.py` and every file in `.github/workflows` | GitHub's release API and the git refs API of the action's own repository |

The rules. A release is proposed only once it is seven days old, counted from the later of its publication and its files'
upload, and only from the line the platform follows (TruffleHog 3, Cosign 3, uv 0.12, npm 11, an action's major version,
and so on); a newer line is listed in the pull request and never proposed. Each checksum is read from official release
metadata, never computed from a download: for a file on GitHub, the SHA-256 GitHub recorded at upload, which the project's
own checksum list must repeat (Caddy's list is SHA-512, and that is its pin); for an image, its registry's index digest
(Docker Hub's tag must have pointed to it for seven days; Google uploads buildpacks images about weekly, so the newest one
Google published that is seven days old is taken, with the line's stack label); for uv and npm, the hashes PyPI and the
registry publish; for an action, the commit its release's tag names in the action's own repository, as GitHub's
documentation asks (a commit from the action's repository, never a fork's). A newer npm must ask for the same Node.js
versions and run no install script. Each week the job also checks that each pinned action's release tag still names the
pinned commit: a release's tag never needs to move, so a moved one holds that action and says so. Then the job downloads
each new file and checks it against its new pin, without running it, writes every place, regenerates
`.github/workflows/deploy.yml`, writes each new action commit wherever the action appears (`labdeploy/workflows.py` and
`deploy.yml` included), and opens one pull request, **Weekly pin refresh, DATE: ...**, or replaces last week's if it is
still open. It never merges, and nothing in it touches Google Cloud.

Reviewing it. The pull request lists each change with its release notes, GitHub's record of the file and the checksum
list (for an action, its tag and the changes since the pinned commit), then what waits for its seven days, the newer
lines, and anything that needs a look (sources that disagree, a pinned file that changed upstream, an image gone from its
registry, an action's tag that moved). Read the release notes of the scanners for changed options and output: the pipeline
reads TruffleHog's, Trivy's, OSV-Scanner's and hadolint's JSON. For an action, look for an input or output renamed or
removed. CI is the gate: the full suite checks that every place agrees and that `.github/workflows/deploy.yml` equals its
template. CI does not run a new tool or action on a real release, so look at the first release after the merge. Merge it
like any pull request; revert it to undo.

After the merge, a change arrives:
- in the pipeline's tools and the buildpacks images: at the next release of each service under `apps/`, and for an
  application with its own workflows once they are re-pinned (`update_app_workflows`);
- in Cosign: also at the next promotion;
- in CI's hadolint and OpenTofu: at once;
- in the base images, uv and npm: in the controller's drafts and lock containers once the machine runs the merged commit
  (`scripts/machine.sh update $(git rev-parse origin/main)`);
- in Caddy and Litestream: on the controller's machine only after you apply the foundation (`scripts/apply-foundation.sh`)
  and its startup script runs again (`scripts/machine.sh startup`, or a restart), since that script installs them;
- in an action: in each of this repository's workflows at its next run (CI on the merge itself), in an application's own
  workflows once Claude re-pins them, and in the CI the controller drafts once it runs the merged commit. When it is an
  action `.github/workflows/infra-apply.yml` uses (checkout, Google's auth), merging also starts **Infrastructure apply**
  for every application root, as Dependabot's bumps of them did before: that workflow runs when a push to main changes it,
  and counts a change to an infrastructure workflow as one to every root. The pull request says so. Each apply goes
  through its environment's protection and finds nothing to change while the drift check is clean; look at the drift
  issue before you merge.

With `platform_updates` on (section "After a release"), the machine update and the re-pins happen by themselves.

By hand: `python -m labdeploy.pins check` shows what a refresh would propose today and writes nothing; `python -m
labdeploy.pins list` shows the pins as the files hold them. To follow a newer line (uv 0.13, or `actions/upload-artifact`
7 and `actions/download-artifact` 8 together, say), change its `line` in `PINS` in a reviewed pull request; the next run
proposes it (or run **Weekly pin refresh** by hand). A new pin goes into `PINS` with its places: `tests/test_pins.py`
fails while a download held to a checksum, or an image digest, in the files the job reads is missing from it, while a
template uses an action that `PINS` and the ignore list in `.github/dependabot.yml` do not both name, and while a test
spells out a pinned value instead of reading it.

A red run means a place could not be found (someone reworded a pinned line: change the place in `PINS`; for an action,
write it `owner/name@<commit> # vX.Y.Z` with the same commit everywhere), or a download did not match the pin read from its
metadata (nothing is proposed; look at that release before anything else). A source
that cannot be read leaves its pins as they are and says so in the pull request. The job needs the pins App (docs/SETUP.md
section 7o); without it the proposal stays in the run's summary. It costs nothing: the repository is public, so
GitHub-hosted runner minutes are free, and the job creates no cloud resources.

## An application that calls Claude

A contract with `needs.models: [claude]` (since 1.21.0) has its service call Claude through Vertex AI. Three things come
first, in this order, once per project (staging, then production when the app is promoted):

1. **The foundation.** 1.21.0 turns on the Vertex AI API (`aiplatform.googleapis.com`) and adds the custom role
   `labDeployModelUser`, whose one permission is `aiplatform.endpoints.predict`: calling a model, never creating anything
   that bills by the hour. Apply it with `scripts/apply-foundation.sh PROJECT ENVIRONMENT`. Until then the app's root
   refuses to plan, and says so.
2. **Model Garden.** In the console, open Vertex AI's Model Garden, find each Claude model the app uses (Claude Opus 5.5,
   Claude Sonnet 5.5) and click **Enable**. Google requires a person to do this once per model and project; a billing
   account managed by a reseller Anthropic does not allow cannot accept the terms.
3. **The app's root.** Merge the pull request that renders it with the grant to the app's runtime identity; the pipeline
   applies it.

The calls bill per token to the project, appear in the billing export as Vertex AI, and count toward the budget, so the
spending cap stops them with the rest. The weekly cost report shows them at project level, not per app. The prices the
platform shows are in `labdeploy/cost.py` (`NEEDS_PRICING["claude"]`, read from the Billing Catalog on 2026-10-19); read
them again when they matter. Only the global endpoint is reachable from an app with private egress: Private Google Access
does not serve the `rep.googleapis.com` endpoints.

## Someone cannot open an app's pages

Google refuses before the app sees the request. Check, in this order: the person signed in with the listed account
(`get_service_health` shows the viewers); the app's `platform.yaml` says `exposure: lab-members` and `access.iap_enabled`
is true (otherwise render and merge the infrastructure); the IAP OAuth client still exists in the console (APIs &
Services, Credentials). To add someone, add `user:name@example.edu` to the app's `viewers` or to
`onboarding.default_viewers`, render the root, merge.

## Rotate credentials

- **Operator token**: on the People tab of `https://MACHINE/settings`, rotate it (the new token is shown once); or
  `python -m labdeploy.cli token` and replace the hash in the configuration (on the machine through
  `scripts/machine.sh config-get` and `config-put`; on a laptop in `config.local.yaml`). Sign in again in Claude. Every
  session of the old token ends.
- **GitHub App key** (the machine): on the App's page generate a new private key, then
  `gcloud secrets versions add FAST_DEPLOY_GITHUB_APP_KEY --project PROJECT --data-file=KEY.pem`, delete the file, and
  delete the old key on the App's page. The controller reads the new version within the hour, or at once when GitHub
  refuses the old key.
- **GitHub token** (a laptop): `security delete-generic-password -s lab-deploy-github-token`, create a new fine-grained
  token on GitHub, run `start.sh` once by hand (it asks and stores it). The watchdog warns two weeks before expiry.
- **ngrok**: rotate in the ngrok dashboard and `ngrok config add-authtoken`; the domain and the connector stay.
- **Pipeline and app identities** have no keys to rotate: their trust is the federation condition in Terraform.
  Changing a repository's numeric id (a transfer) needs a foundation apply and a root re-render.

## Prove the alerts work

Once a month (the watchdog reminds), or after changing channels: `scripts/alert-drill.sh PROJECT` for each project. It
runs the three steps below, asks whether the email arrived, and records the drill on the machine
(`scripts/machine.sh record-drill alerts ok`), so the reminder stops for a month. By hand:

1. `gcloud workflows run lab-deploy-spending-cap --location us-central1 --data='{"data":{"message":{"data":"eyJjb3N0QW1vdW50IjogMCwgImJ1ZGdldEFtb3VudCI6IDF9"}}}'`
   (a notification with cost 0 of budget 1) must answer `below cap: 0 of the budget`: the trigger path and the
   identity work without closing anything. Production's cap is its own: the same with `--project PROD_PROJECT`. The
   cap-fire drill (section Production) is the version that closes and reopens.
2. `gcloud workflows run lab-deploy-spending-cap --location us-central1 --data='{"data":{"message":{"data":"bm90IGpzb24="}}}'`
   (a message that is not JSON) fails on purpose; within about five minutes the alert **[ENV] the spending cap could not
   act** must reach the alert email (and the webhook when configured) and close itself half an hour later.
3. The project's notification channels must be enabled (the script reads them; the console's Alerting, Edit
   notification channels, shows the same).

The rollback and cap-fire drills of section Production are recorded the same way (`record-drill rollback ok`,
`record-drill cap_fire ok`); a production rollback that succeeded counts as the rollback path proven.

## Retire an application

`scripts/decommission.sh SERVICE` shows what retiring one application deletes, and `--yes` does it, in the order
`lab-deploy decommission` prints (since 1.22.4; it ran for the five test services of Phase 20's live exercise): the Cloud
Run service and its migration job, the recovery bucket with every version of its objects (it is versioned) and the image
registry, each only when it is still there and each stopping the script when it fails, then `terraform destroy`
of `infra/gcp/apps/SERVICE` in the official container with the pipeline's state, after those resources are taken out of
the state (the service's deletion protection and the bucket's `prevent_destroy` would refuse the destroy otherwise), then
the GitHub environment `staging-SERVICE` of a contract the platform keeps. It refuses an application whose root declares
data (a database, buckets, topics): retire that deliberately first (below), merge the root without it, then run it. What
it cannot do it prints at the end: remove `infra/gcp/apps/SERVICE/` (and `apps/SERVICE/`) in a pull request, and
`scripts/machine.sh unregister SERVICE` so the controller forgets the application (its plans stay as history). Cloud KMS
keeps the provenance key by name with its versions scheduled for destruction, and the Workload Identity pool stays
soft-deleted for thirty days, so an application of the same name within that time imports both rather than creating them.
Needs Docker running, gcloud signed in as the project owner and gh signed in.

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

## Rename the platform repository

Done once on 2026-10-16 (`brianboti/lab-deploy` became `brianboti/fast-deploy`, 1.14.1). GitHub keeps the repository's
numeric id and redirects the old name, and the pipeline's sign-in is anchored on the ids, but the workflow paths it checks
and the apps' pins carry the name, so the order matters:

1. In `infra/gcp/project/terraform.tfvars`, set `pipeline.platform_repository` to the new name and list the old one under
   `pipeline.former_repositories`; `scripts/apply-foundation.sh PROJECT` (2 to change: the two sign-in providers). The
   pipeline now signs in under either name.
2. Rename the repository on GitHub (`gh repo rename NEW --repo OWNER/OLD`), then on this machine
   `git remote set-url origin https://github.com/OWNER/NEW.git`.
3. In `config.local.yaml`, set `platform.repository` to the new name and `platform.former_repositories: [OWNER/OLD]`;
   restart the controller. Apps still pinned under the old name stay supported.
4. Re-pin every application (`update_app_workflows`, or `python -m labdeploy.cli update-workflows --all`) and merge the
   pull requests; prove one release and one run of the drift workflow.
5. Remove the old name from both `former_repositories` and apply the foundation again (2 to change).

## Monthly checklist

- `get_controller_status` and the last watchdog findings; the backup bucket has copies from the last day.
- `scripts/machine.sh status`: the three services active, the disk under half full, the commit the one you expect, and
  the last automatic update; `get_controller_status` names any re-pin that waits for you.
- Every quarter: the restore drill (`scripts/machine.sh drill`; the watchdog reminds).
- The alert **"access changed outside the pipeline"** fires for every IAM change not made by the pipeline, your own foundation
  applies included. To stop hearing about your own, add your address to `access_change_quiet_members` in the
  foundation's tfvars and apply it; anyone else's change still raises it.
- Twice a year, for production: the rollback drill, the cap-fire drill and, for each database there, the data restore
  drill (section Production). Each is recorded with `scripts/machine.sh record-drill`; the watchdog reminds when one is due.
- The daily digest issue **Fast Deploy: today**: still hearing from the controller every day.
- Google's alert pricing: no sooner than 2027-09-01, $0.35 a month per metric reference in an alert policy (uptime,
  billing and quota alerts excepted). Before then, retire test services nobody uses (`scripts/decommission.sh`): each
  takes five alert policies and an uptime check.
- The cost report issue on each app repository: spend against the budget and against the period before, drift, proposals.
  The budgets recorded under `monthly_budgets_usd` still match Cloud Billing's, and Google's Cost Anomaly Detection is on
  (`docs/SETUP.md` 7q). An application with a
  production shows production's spend beside staging's once `add_production` has run for it with 1.18.8.
- Any `lab-deploy-cap-lifted` label left from the previous month (`gcloud run services list --format="table(name,labels)"`).
- The native spend cap still exists (`scripts/spend-cap.sh check PROJECT`) and the drift issue on the platform repository is closed.
- Dependabot pull requests and the weekly pin refresh's pull request on the platform repository (section "The weekly
  pin refresh"); `pip-audit` runs every Monday in CI.
- Node.js lines within a month of their end of life (section "Node.js lines and their end of life"): 22 on 2027-04-30,
  24 on 2028-04-30.
- The GitHub token's expiry in the watchdog findings, while a laptop uses one.
