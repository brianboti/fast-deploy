# Using Lab Deploy from Claude chat

The platform is operated by talking to Claude. Claude calls the controller's tools over the connector; the controller
decides what is allowed, GitHub Actions does the work, and Google Cloud runs the result. This page is the operator's
daily reference; `docs/SETUP.md` covers the one-time setup and `docs/RUNBOOK.md` the bad days.

## Before the first conversation

1. The laptop that runs the controller is awake and `scripts/start.sh` is running (or the launch agent is installed).
2. In Claude: Customize → Connectors → Add custom connector → `https://YOUR-DOMAIN.ngrok-free.dev/mcp`, **Sign in now**,
   **Register automatically**, **Connect**. The browser opens the controller's own page; paste the operator token there,
   never into the chat. The sign-in lasts thirty days; when Claude says the connector needs attention, sign in again.
3. Ask: *"What is the controller status?"* Claude calls `get_controller_status` and reports the version, whether
   dispatch is live, what is paused, the GitHub credential's expiry and the last backup.

## What you can ask for

**Looking** (Claude calls these without asking):

| Ask | Tool | What comes back |
|---|---|---|
| "Which apps do we have and what is live?" | `list_applications` | Each app, its live commit, platform pin, schedules and open proposals |
| "What is waiting on main for demo?" | `compare_with_live` | Commits and files since the live release, kinds of change, cost before and after |
| "How is demo doing right now?" | `get_service_health` | Serving revisions, requests, errors, latency, instances, memory, declared needs |
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
| "Onboard github.com/ORG/REPO" | `analyze_repository`, `propose_configuration` | A draft `platform.yaml` with review questions, as a pull request on the app repository |
| "Give demo a bucket called uploads" | `propose_configuration` with `needs` | The same, for buckets, topics, a database or a migration command |
| "Render demo's infrastructure" | `propose_infrastructure` | The app's Terraform root as a pull request on the platform repository; the plan and policy verdict appear as a comment, merging applies it |
| "Re-pin demo to the current platform" | `update_app_workflows` | The three workflow files regenerated against a platform commit, as a pull request |
| "Apply the right-sizing proposals" | `propose_rightsizing` | A pull request editing `platform.yaml` with the measured recommendations |

## A normal release, as Claude reports it

`deploy_latest` returns a plan id and `dispatched`. Asking for the status with a wait of 45 seconds a few times shows the
workflow steps (tests, dependency audit, build, provenance, scan, deploy, probes), then the release phases
`preparing, deploying, promoting, succeeded` (with `migrating` before `deploying` when the app declares a migration).
The demo takes about three to four minutes. A failed candidate never takes traffic: the status says
`release_failed_health_unconfirmed`, shows the candidate's last log lines, and the next action is to fix and deploy
again. `dispatch_lost` means GitHub never showed the run; deploy again.

## Guardrails Claude cannot talk its way around

- Every identity has an allowlist of applications and a read-only or deployer role; the operator token is the identity.
- A plan binds a full commit, the reviewed `platform.yaml` hash and the exact cloud target; the runner re-checks all three.
- `cost_ceiling_usd` refuses plans whose compute bound plus the needs' floor is too high; estimates come from list prices.
- Infrastructure changes are pull requests with a fail-closed policy; nothing that holds data is ever destroyed by the pipeline.
- Secret values never pass through Claude: the operator creates them with `gcloud`, `platform.yaml` only names them.
- The spending cap closes ingress and stops databases at the budget line, whatever anyone asks for.

## When something is off

| Claude says | Meaning | Do |
|---|---|---|
| The connector cannot be reached | The laptop is asleep or the tunnel is down | Wake it, run `scripts/start.sh`; `docs/RUNBOOK.md` "The laptop is off or asleep" |
| Sign in required | The session ended or the operator token changed | Sign in again on the controller's page |
| Platform pin unsupported | The app's workflows pin a platform commit older than `platform.minimum_commit` | "Re-pin APP to the current platform", merge the pull request |
| Policy: blocked | An infrastructure plan violates a rule | Read the rule in the pull request comment; change `platform.yaml` or the configuration, render again |
| Cost policy exceeds limit | The app's worst case is above `cost_ceiling_usd` | Lower resources or raise the ceiling deliberately in the configuration |

## Verified

On 2026-10-12 this path was exercised end to end over the public URL exactly as Claude uses it: discovery, client
registration, the sign-in page with a wrong and then the right token, the token exchange, twenty tools listed, a real
release of the demo driven by `deploy_latest` and followed with `get_deployment_status`, health, logs and the cost
report, then refresh, revocation and reuse detection (`docs/VALIDATION.md`, "Readiness for Claude chat").
