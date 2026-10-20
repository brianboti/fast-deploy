# Using Fast Deploy from Claude chat

The platform is operated by talking to Claude. Claude calls the controller's tools over the connector; the controller
decides what is allowed, GitHub Actions does the work, and Google Cloud runs the result. This page is the operator's
daily reference; `docs/SETUP.md` covers the one-time setup and `docs/RUNBOOK.md` the bad days.

## Before the first conversation

1. The controller runs: on its own machine since 1.17.0 (`scripts/machine.sh status` from the laptop), or on the laptop
   with `scripts/start.sh` as the fallback.
2. In Claude: Customize → Connectors → Add custom connector → `https://ADDRESS/mcp` (the machine's address, or its DNS
   name if it has one; the ngrok address while a laptop is the controller), **Sign in now**,
   **Register automatically**, **Connect**. The browser opens the controller's own page; paste the operator token there,
   never into the chat. The sign-in lasts thirty days; when Claude says the connector needs attention, sign in again.
3. Ask: *"What is the controller status?"* Claude calls `get_controller_status` and reports the version, whether
   dispatch is live, what is paused, the GitHub credential's expiry and the last backup.
4. Once, install the playbook skill so Claude chat follows the same rules every time: build the zip with
   `cd skills && zip -r ~/Downloads/fast-deploy-operator.zip fast-deploy-operator`, then in Claude: Customize → Skills →
   upload it and switch it on (code execution must be enabled). Re-upload it after a platform release that changes
   `skills/`. It tells Claude when to ask before acting, how to read each status, what costs to show, and how to write
   a "what is missing" message for a developer.

## What answers look like

Every answer has the same three parts, so it reads at a glance:

1. **One bold line with the outcome**: live, in progress, stopped, ready, not ready, healthy.
2. **A few bullets** with what matters: the version, the address, the checks, how long it took, the cost.
3. **Next:** what happens next, or the one thing you do.

The controller writes the core of it. Most tools return a `summary` built by code from the real result
(`labdeploy/chat.py`), and Claude answers from it, so the wording and the numbers are the same every time and never
invented. Claude adds detail only when you ask for it ("show me the steps", "why did it fail?", "show the log lines").

> **`lab-bare` is live** with `dede786` (Merge pull request #4 from brianboti/buildpack-trial).
>
> - Address: https://lab-bare-42n6s5wp5q-uc.a.run.app (needs a Google sign-in token; not open in a browser)
> - Checks: tests passed · library audit passed · health check passed
> - Took 10 min
>
> **Next:** nothing to do. To undo it, say “roll lab-bare back”.

> **Release stopped for `lab-notebook`: the previous version is still serving.**
>
> - Why: the database migration failed, so nothing new was deployed
> - Error: `relation "experiment_typo" does not exist`
>
> **Next:** fix the migration (check whether part of it ran), then release again.

> **Onboarding `api`, step 4 of 9: plan the cloud setup and check the safety rules.**
>
> - From `brianboti/lab-monorepo`, folder `services/api`
> - Waiting for the infrastructure plan run
>
> **Next:** nothing to do; it continues on its own every minute.

If an answer comes back as a wall of text or raw JSON, the playbook skill is probably missing or old: re-upload it
(step 4 above). claude.ai does not read the connector's own instructions (anthropics/claude-ai-mcp issue #93, open since
March 2026), so the skill, the tool descriptions and each result's `summary` are all the guidance Claude gets; that is why
the skill must be re-uploaded after every release that changes `skills/` (the release note in `docs/CHANGES.md` says when).

## What you can ask for

**Looking** (Claude calls these without asking):

| Ask | Tool | What comes back |
|---|---|---|
| "Which apps do we have and what is live?" | `list_applications` | Each app, its live commit, platform pin, schedules and open proposals |
| "What is waiting on main for demo?" | `compare_with_live` | Commits and files since the live release, kinds of change, cost before and after |
| "How is demo doing right now?" | `get_service_health` | Serving revisions, requests, errors, latency, instances, memory, declared needs |
| "How is demo doing in production?" | `get_service_health` with `environment: production` | The same for production, with the error budget its objective has left |
| "Show me demo's recent logs" | `get_recent_logs` | Last log lines through the read-only observer identity |
| "What would demo cost?" | `estimate_cost` | Floor, compute ceiling, needs' floor, optional traffic scenario, findings |
| "What did we actually spend?" | `get_cost_report` | The weekly right-sizing report with measured usage, proposals and billing export |
| "Where is my release?" | `get_deployment_status` | Every workflow step, the release phases, candidate logs on failure, a next action |
| "What happened today?" | `list_plans`, `list_events` | Plan history and the audit trail of every call and sign-in |

**Acting** (Claude's client asks you to confirm; each one is bound to an exact commit and target):

| Ask | Tool | What happens |
|---|---|---|
| "Deploy the latest demo" | `deploy_latest` | Plans the tip of main and dispatches the release in one call |
| "Deploy commit abc123 of demo" | `create_deployment_plan` then `deploy_staging` | A plan for that commit (must already be on main), then its dispatch; a commit released before is redeployed from its attested image |
| "Roll demo back" | `rollback_staging` | Traffic returns to the split that served before the live release, in seconds, no build |
| "Cancel that release" | `cancel_deployment` | Stops the run; the recovery workflow restores traffic if it had started changing |

**Changing things through pull requests** (nothing applies on its own; you merge):

| Ask | Tool | Result |
|---|---|---|
| "Deploy github.com/ORG/REPO on GCP" | `onboard_application`, `get_onboarding_status` | The whole onboarding, unattended, for a repository that follows the contract (next section) |
| "Deploy the service in services/admin of github.com/ORG/REPO" | `onboard_application` with `path` | The same for one service of a repository that holds several; one sentence per service |
| "Is github.com/ORG/REPO ready?" | `check_repository` | The contract's checklist, read-only: what is there, what is missing and why, and what the platform can draft |
| "Draft the files for github.com/ORG/REPO" | `propose_configuration` | One draft pull request with every contract file the service lacks (Dockerfile, lock, test tools, a health test, `platform.yaml` with `reviewed: false`, CI, security policy, Dependabot), never changing an existing file; works before onboarding |
| "Give demo a bucket called uploads" | A reviewed `needs` change, then `propose_infrastructure` | The developer (or you) adds it to `platform.yaml` in a pull request on the app repository; after that merge, the infrastructure pull request provisions it and the next release uses it. The same for topics, a database or a migration command. `propose_configuration` refuses an app that already has a `platform.yaml` |
| "Render demo's infrastructure" | `propose_infrastructure` | The app's Terraform root as a pull request on the platform repository; the plan and policy verdict appear as a comment, merging applies it |
| "Add production for demo" | `add_production` | Step by step, asked again after each: the production root as a pull request that you merge yourself (the controller never merges production infrastructure), its apply approved in `infra-production`, the platform repository's environment `production-demo` where your reviewers approve every promotion, then a second pull request giving production's deployer read access to staging's images |
| "Promote demo to production" | `promote_to_production` | The newest staging release that succeeded (or the one you name) is copied by digest into production once you approve the run on GitHub, then takes 10, 50 and 100 percent of the traffic, each step watched; `get_deployment_status` follows it |
| "Roll demo back in production" | `rollback_production` | Once you approve it on GitHub, production's traffic returns in seconds to the version before its live promotion; no build |
| "Re-pin demo to the current platform" | `update_app_workflows` | The three workflow files regenerated against a platform commit, as a pull request |
| "Apply the right-sizing proposals" | `propose_rightsizing` | A pull request editing `platform.yaml` with the measured recommendations |

## Deploy a repository with one sentence

Not sure the repository is ready? Ask *"Is https://github.com/ORG/REPO ready?"* first: Claude checks it, and can draft the
missing files as a pull request for a person to review. For a repository that follows `docs/DEVELOPER-CONTRACT.md`, say:
*"Deploy https://github.com/ORG/REPO on GCP."*
Claude calls `onboard_application`, which does everything the five onboarding steps did by hand:

1. Checks the repository against the contract and stops with a checklist if something is missing (nothing is created before the contract holds).
2. Registers the application with the controller.
3. Opens the infrastructure pull request on the platform repository, waits for the plan and the policy verdict, merges when it passes, and waits for the apply.
4. Creates the app's `staging` environment on GitHub, restricted to `main`, with its variables.
5. Opens and merges the pull request that adds the three platform workflows.
6. Plans and dispatches the first release.

The first answer comes back within seconds with the step reached; the controller continues on its own every minute,
usually 4 to 15 minutes in all. *"Where is REPO's onboarding?"* calls `get_onboarding_status`; when it says complete, *"Where is
my release?"* follows the first deployment. If a step blocks, the status names the reason and the fix, and the same
sentence continues from that step once it is fixed. Every merge is a pull request the controller opened itself, on the
application's own files only, after the policy and the checks passed; set `auto_merge: false` in the configuration to
keep every merge for a person.

**A repository with several services** (`docs/DEVELOPER-CONTRACT.md` section 4a): name the folder, one sentence per
service: *"Deploy the service in services/api of https://github.com/ORG/REPO"*, then *"... the one in services/admin"*.
The first keeps the plain workflow names and the `staging` environment; the second gets its own
(`deploy-gcp-admin.yml`, `staging-admin`), and the onboarding status says which. When the repository has a
`compose.yaml`, *"Analyze REPO"* turns it into one draft per web service and says where the database, cache and
workers go; nothing from Compose is deployed as such.

## Deploy it as it is

For a repository the lab should not change (someone else's project, a paper's code, a repository with a messy layout),
say: *"Deploy https://github.com/ORG/REPO as it is."* Nothing is added to that repository (since 1.15.0):

1. The platform reads it: how it is built, which Python or Node.js, its packages, what starts it, its port and health
   check, its tests. Each choice names the file it came from; what it cannot read safely, it refuses, and says the file
   and the fix. Since 1.22.0 it reads more and refuses less (roadmap Phase 20): a Streamlit, Gradio or Dash app, an
   aiohttp server or a plain `python main.py` is started the way its framework documents when no application object is
   found, with a note that the first revision's log decides; a Poetry project or a Pipfile without a lock, a `setup.py`,
   a `requirements/` folder and `apt.txt` are read or locked by the platform; a Node.js service on a literal port, or
   started by `nodemon` or `pm2`, is taken as it is; a `.nvmrc` or `.python-version` naming a line at its end of life
   gets the lowest supported line with a note. A root that holds several services names them, and *"Deploy everything
   in REPO"* opens one contract pull request per folder. The repository is read from its own default branch, whatever
   its name (since 1.22.1); only a repository that takes the platform's own workflows needs `main`.
2. It opens one pull request on the platform repository with the contract (`apps/NAME/platform.yaml`) and the locks it
   made. **A person reviews and merges it: that merge is the review.** The controller never merges a contract.
3. Then the usual steps run on their own: the cloud setup, the GitHub environment (`staging-NAME`, in the platform
   repository), the first release by the platform's own pipeline.

*"Is REPO ready?"* also says whether it can be taken as it is, and *"Import the contract of REPO"* opens only the pull
request. Later releases work like this: *"Deploy the latest NAME"* opens a pull request that moves the contract's commit
and nothing else; it merges itself when the app's auto-merge is on and the checks pass, otherwise a person merges it,
and the merge starts the release within a minute. A change to any other line of the contract is reviewed the same way,
and merging it releases it too.

**A Node.js repository** (since 1.20.0) goes the same way, with the same sentence. The answer names the runtime ("Node.js:
24 (from `.nvmrc`)") and reads it like a Python one, with its own rules (`docs/DEVELOPER-CONTRACT.md`, "What the platform
assumes" and section 1b):

- It needs `package.json`; its `package-lock.json` is checked entry by entry, and without one the pull request carries
  `apps/NAME/package-lock.json`, made by the platform with every install script off and nothing younger than three days.
- No package's install script runs, except those the contract lists under `build.scripts`. The pull request says why its
  list is what it is ("none run: onnxruntime-node@1.30.0: its postinstall only downloads the CUDA libraries..."), and a
  person who adds one is approving that package's code.
- What it refuses comes with the fix: a start script with `nodemon` or `tsx`, a server listening on `localhost`, a
  `yarn.lock` or `pnpm-lock.yaml` (convert it to `package-lock.json`), a project that must be built first. A host the code
  reads from a variable that defaults to `127.0.0.1` gets `HOST: 0.0.0.0` in the contract instead.
- Every release runs a dependency audit before anything is installed: a known malicious package, or a serious
  vulnerability with a fix, stops it whoever wrote the code; the release's answer says how many packages it checked.
- Google's buildpacks (a Procfile without a Dockerfile) build a Node.js service only once the lab has reviewed its code
  (`trust: reviewed`), because they run install scripts; otherwise the platform's own Node.js image runs the Procfile's
  command.

*"Analyze REPO"* and *"Draft the files for REPO"* work for a Node.js repository that follows the contract too: the draft
holds a Dockerfile on `node:<line>-trixie-slim`, a `node --test` health check, CI on `actions/setup-node` and Dependabot for npm.

## Pages for people

An app whose `platform.yaml` says `exposure: lab-members` gets a Google sign-in page in front of it. Who may open it is
the lab's list (`onboarding.default_viewers` in the controller configuration) plus the app's own `viewers` list; both
are yours to edit. To let someone in: add `user:name@example.edu` to the list, then *"Render APP's infrastructure"* and
merge the pull request; the grant exists a minute later. *"How is APP doing?"* shows `access`: the exposure, whether the
proxy is on, and the viewers. A browser opening the service URL without being listed is refused by Google, not by the
app; programs that must call the app need a token for the proxy's client id (`docs/DEVELOPER-CONTRACT.md`, 6a).

## A normal release, as Claude reports it

A plan carries an `approval` block that code builds from the plan, never from Claude's words: the commit, where it goes
(and the service's folder), the cost bounds, who may reach it, what it needs, how far a rollback undoes it (`reversible`,
or `data-bearing` when a migration runs), and the identifiers that tie your request to the plan and the run. The
infrastructure pull request's comment opens with the same kind of block for Terraform changes.

`deploy_latest` returns a plan id and `dispatched`. Asking for the status with a wait of 45 seconds a few times shows the
workflow steps (tests, dependency audit, Dockerfile lint, build, provenance, scan, deploy, probes), then the release phases
`preparing, deploying, promoting, baking, succeeded` (with `migrating` before `deploying` when the app declares a
migration). The bake watches the new revision's real traffic for five minutes after it takes over, so the demo takes about
eight to nine minutes. A failed candidate never takes traffic: the status says `release_failed_health_unconfirmed`, shows
the candidate's last log lines, and the next action is to fix and deploy again. A revision that took over and then
crashed or answered too many requests with 5xx "failed its bake after promotion": the previous traffic is back, and the
bake's numbers and the revision's log lines are in the status. A bake with too little traffic to judge says
`inconclusive`; the release still counts. `dispatch_lost` means GitHub never showed the run; deploy again.

## Guardrails Claude cannot talk its way around

- Every identity has an allowlist of applications and a read-only or deployer role; the operator token is the identity.
- A plan binds a full commit, the reviewed `platform.yaml` hash and the exact cloud target; the runner re-checks all three.
- `cost_ceiling_usd` refuses plans whose compute bound plus the needs' floor is too high; estimates come from list prices.
- Infrastructure changes are pull requests with a fail-closed policy; nothing that holds data is ever destroyed by the pipeline.
- Secret values never pass through Claude: the operator creates them with `gcloud`, `platform.yaml` only names them.
- The spending cap closes ingress and stops databases at the budget line, whatever anyone asks for; Google's own spend
  cap pauses Cloud Run in the project as a second, independent layer once the operator has created it.
- Every release is watched for five minutes after it takes over; a crash or 5xx answers bring the previous version back.
- A weekly drift check reports anything changed outside the pipeline; `get_controller_status` shows it.
- After a platform release (since 1.20.4, once the operator turns `platform_updates` on), the machine installs the new
  code an hour after its checks pass and puts the old one back if it fails; then each app's workflows are re-pinned, and
  merged only where its `auto_merge` allows. `get_controller_status` says what happens next and what waits for a person.
- Code nobody has reviewed (a repository taken as it is) is built and run under stricter rules (since 1.16.0): a working
  key in it or a fixable serious vulnerability stops it, and it reaches no internet unless its reviewed contract says
  `egress: internet` (`docs/DEVELOPER-CONTRACT.md` 5a).
- A Node.js service never runs a package's install script unless its reviewed contract names that package and version,
  and a known malicious package in its lockfile stops every release (since 1.20.0).

## When something is off

| Claude says | Meaning | Do |
|---|---|---|
| The connector cannot be reached | The machine's controller is down (on a laptop: asleep, or the tunnel is down) | `scripts/machine.sh status` and `logs`; `docs/RUNBOOK.md` "The controller's machine" (a laptop: "The laptop is off or asleep") |
| Sign in required | The session ended or the operator token changed | Sign in again on the controller's page |
| Platform pin unsupported | The app's workflows pin a platform commit older than `platform.minimum_commit` | "Re-pin APP to the current platform", merge the pull request |
| Policy: blocked | An infrastructure plan violates a rule | Read the rule in the pull request comment; change `platform.yaml` or the configuration, render again |
| Cost policy exceeds limit | The app's worst case is above `cost_ceiling_usd` | Lower resources or raise the ceiling deliberately in the configuration |
| Failed its bake after promotion | The new version crashed or answered too many 5xx once it served everyone; the previous version is back | Read the bake's numbers and the log lines in the status, fix, deploy again |
| `no_native_spend_cap` in a plan or estimate | Google's spend cap for Cloud Run is not recorded for the project | `docs/SETUP.md` 7i: create it, then record it under `cloud_run_spend_caps` |
| Infrastructure drift in the controller status | Something in an app's infrastructure differs from the code | `docs/RUNBOOK.md` "Drift reported" |

## Verified

On 2026-10-12 this path was exercised end to end over the public URL exactly as Claude uses it: discovery, client
registration, the sign-in page with a wrong and then the right token, the token exchange, twenty tools listed, a real
release of the demo driven by `deploy_latest` and followed with `get_deployment_status`, health, logs and the cost
report, then refresh, revocation and reuse detection (`docs/VALIDATION.md`, "Readiness for Claude chat").
