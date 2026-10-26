# Readiness phases 0 and 2 — implementation record

This work implements the October 23, 2026 readiness plan, not the historical roadmap's numbered phases.
Baseline: 1.31.11, commit 570509884cbd43fcc19b7f10a94ecc9af5a2e8ae.

## Delivery checklist

"Code" means implemented in this repository with local tests. "Live" means exercised on a real installation with recorded
evidence (docs/READINESS-ROLLOUT.md, "Required live evidence"). No item is live yet.

| Item | Code | Live |
|---|---|---|
| P0.1 control catalog, evidence inventory, dependency map, current documentation | done | inventory of effective settings pending |
| P0.2 universal verified-secret admission | done | application workflow upgrades and negative drill pending |
| P0.3 independent contract CI | done | required-check setting in branch protection pending |
| P0.4 production-gate specification, observation outcomes, phase-2 invariants | done (specification) | not applicable |
| P2.1 operation ledger, migrations, off-host intents | done | migration of the live database and intent bucket pending |
| P2.2 dispatch and reconciliation | done | crash/lost-response drill pending |
| P2.3 durable bounded scheduling | done | stall and lag measurement pending |
| P2.4 independent production recovery | done | recovery identity, supervisor and runner-loss drill pending |
| P2.5 remote dependency resolution | done | worker project, images and containment drill pending |
| P2.6 authenticated job admission and IAM cutover | done | per-application cutover and direct-IAM denial drill pending |
| P2.7 migrations, failure tests, operational handover | partial: local failure tests | failure campaign, timed restore and seven-day canary pending |

Local tests, infrastructure validation, deployment, and live operational evidence are separate milestones.
No live control is recorded as verified merely because its implementation exists.
The staged rollout includes application workflow upgrades, identity provisioning, a canary, controlled failure drills,
and a seven-day observation period. These gates remain open until measured.

## Review fixes, 2026-10-26

An independent review of the first implementation found fifteen issues. Thirteen are fixed with regression tests. The
contract-check bypass cannot be closed by a workflow alone and is recorded under POL-002. The last one was a decision, not a
defect: with no worker project, a repository without a lockfile could no longer get one generated. Brian chose an opt-in
`resolver_local_fallback` switch that keeps generation on the controller until the workers exist, recorded as an
exception to ISO-001.

- A failed release step with a known outcome (a revision that never became ready, a failed migration, a refused traffic
  change) no longer leaves the checkpoint held: traffic is restored and the next release proceeds. Only unknown outcomes
  hold, and an unknown traffic change is resolved by restoring conditionally on either split it can have left.
- The checkpoint retries a transient Cloud Storage error, accepts its own write after a lost response, and treats only
  another writer's change as a conflict. Each GitHub re-run claims its own attempt.
- Recovery that is refused before sending clears its pending mark. The supervisor escalates a recovery run that ended, or
  has not finished the checkpoint in fifteen minutes, and alerts when it stops being invoked.
- A dispatch being sent is not marked lost by a concurrent follow-up pass, and a dispatch GitHub accepted wins over an
  earlier loss. A failure before sending gives a deployment plan back. Lost and unverified plans close their queue entry,
  so `reconcile resume` can complete.
- A cancellation that failed or lost its answer can be requested again; GitHub's 409 counts as accepted.
- Job and resolver launches that never left the controller, or that Cloud Run refused, free their slot (a job keeps its
  daily charge). A worker output that fails validation ends the resolution instead of holding capacity.
  `reconcile release-job` frees a job slot whose outcome stayed unknown, with evidence.
- Expired provider operations are not re-read once the execution is known; the workflow-run lookup reads only runs
  created since the plan.
- The worker firewall denies IPv4 and IPv6 in two rules, as Google Cloud requires.
