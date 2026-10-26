# Production admission specification — version 1

Status: specification for the next identity/policy phase, not a claim of deployed enforcement. Phases 0 and 2 introduce universal verified-secret admission and operational safeguards; they do not replace the existing identity system with enterprise identity or a full policy engine.

Every future production decision must bind application, environment, source commit, contract hash, immutable service and job image digests, platform/workflow commit, policy version, requester, independent approver and approval time. A changed binding requires a new decision. A stored `reviewed: true` flag is a source classification, never approval evidence.

| Requirement | Required decision | Existing behavior / gap |
|---|---|---|
| Identity | Named organization identities; requester cannot approve their own promotion; auditable membership and revocation | Configured principals and GitHub environment review exist; external identity-provider integration remains separate work |
| Contract | Enforce production rules from protected platform policy; required trusted contract CI and reviewed infrastructure | Validator and production checks exist; effective branch protection and reviewer settings need installation evidence |
| Secrets | Fresh source and all image scans under SEC-001/v1; verified finding always denies; scanner failure is a hold | Implemented in upgraded release workflows; old pinned workflows are outside the guarantee |
| Tests | Required relevant tests; a justified, expiring exception for an intentional absence | Existing `tests: none` can warn; the stricter exception model is specified, not yet enforced |
| Dependencies | Locked approved sources, integrity and applicable vulnerability policy, bounded evidence age | Existing runtime-specific auditing and lock checks; enforcement modes and exception handling must be verified |
| Image identity | Same immutable image promoted from a successful staging receipt; provenance binds source and builder | Promotion and verification implemented; installations can have exceptions that this future gate must disallow |
| Binary Authorization | Enforced production policy; signed admission bound to digest and approved builder | Infrastructure supports configurable modes; Terraform code does not prove the live mode |
| Capacity and spend | Reviewed service/job limits, quota reservation before execution, effective broker-only IAM, budget response owner | Broker added behind an explicit cutover; budget notifications are delayed and do not prove a hard dollar cap |
| Recovery | Fresh checkpoint, exclusive workflow queue, independent supervisor, verified runner-loss drill; hold on unknown mutation or drift | New code/infrastructure require provisioning and live drills |
| Operational readiness | Healthy backups, restore evidence, monitored alerts with accountable recipient, incident/runbook owner | Evidence remains installation-specific; missing evidence means hold |

The evaluation result is `allow`, `deny`, or `hold`, with stable rule IDs and evidence references. Missing, stale, malformed or unavailable evidence is a hold, never a pass. Verified credentials, an identity mismatch, changed request/image binding, and an unresolved earlier mutation are not ordinary waivable failures.

## Rollout observation outcomes

Every observation step ends in exactly one of three outcomes. Only the first may count as a successful measurement.

| Outcome | Meaning | Required action |
|---|---|---|
| `proceed` | Enough real requests were observed and every rule passed | Advance to the next step |
| `rollback` | A rule failed (crash, error rate, latency) | Restore the recorded previous traffic |
| `hold` | Too little traffic, unreadable logs, or monitoring unavailable | Stay at the current step or roll back by a named rule; never report it as passed |

Today's bake reports too little traffic as `inconclusive` and unreadable logs as `unobserved`, and lets both pass with a warning (docs/ARCHITECTURE.md, invariant 11). Under this gate both become `hold`. That change is specified here, not implemented.

## Phase-2 implementation invariants

These are frozen for the operation ledger, dispatch, scheduler, recovery and job broker. A change that breaks one needs a new version of this section.

1. Persist the operation and its intent before any external mutation.
2. A timeout or lost response means unknown until reconciled.
3. A repeated request with the same identity never causes an uncontrolled second mutation; the same key with a different payload is refused.
4. No automatic recovery while another writer or cloud operation is unresolved.
5. No fresh job launch without a durable quota reservation.
6. Restoring a backup never resets quota or silently replays pending work.
7. Slow or untrusted work cannot use the safety scheduler's execution capacity.

## Exceptions, recovery and acceptance

A future exception must name its rule, application/environment, reason, accountable approver distinct from requester, creation/expiry times and compensating controls. It may not become a global boolean or silently persist after expiry. Policy changes follow protected review and run regression examples for both allow and deny decisions.

Rollback and automatic recovery use their own narrow authorization. They restore a recorded previous state after fencing the previous writer; they do not rebuild, rerun a migration, launch application jobs, or approve a new release. They cannot undo database changes or external side effects. Known exposed credentials require rotation and incident handling even when traffic is restored.

Acceptance for the full gate requires negative integration tests for each row, real effective-IAM/reviewer evidence, and an audited allow/deny/hold record. The current implementation must not be described as having passed this future gate.
