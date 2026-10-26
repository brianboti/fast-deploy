# Control catalog — readiness baseline 2026-10-26

This catalog describes enforcement in this working tree. **No live installation was verified for this change.** Code, local tests, declared infrastructure, effective installation state and measured operational evidence are different evidence classes. Named accountable people and evidence locations must replace the role-based owners below before the company pilot.

`controls.json` is the structured catalog; `current-evidence.json` inventories local registry/root/workflow declarations. It contains no secrets. Configuration flags and generated Terraform do not prove effective IAM, GitHub protection, scanner mode, alert delivery or recovery performance.

| Control | Rule and enforcement | Strength / limitation | Owner and evidence |
|---|---|---|---|
| SEC-001/v1 | A verified credential denies new admission for every trust classification, source checkout, service/job image, reuse and promotion. Failed/malformed scans deny. | Blocking in upgraded workflows; old pinned application workflows need upgrades. No raw credential evidence. Recovery is a distinct action. | Security/platform; `leaks.py`, `secret_admission.py`, workflow jobs, sanitized admission artifacts and readiness policy tests |
| SEC-002 | Provenance binds pushed digests; release verifies GitHub or Cosign/KMS provenance. | Implemented; operator exceptions and effective installation modes must be recorded. | Security; provenance receipts, workflow settings and KMS/IAM evidence |
| SEC-003 | Dependency/image/configuration scanning and SBOM production. | Implemented; some scans are informational or conditional. A scan's presence is not proof of blocking. | Security; scanner reports, workflow variables, SBOM and vulnerability policy |
| SEC-004 | Binary Authorization uses reviewed attestors. | Configurable; live enforcement is unknown. | Security/cloud; effective policy export and denial drill |
| POL-001 | Contracts constrain runtime, resource size, scaling, needs, exposure and permitted destinations. | Blocking in planning/runners; protected policy review is an installation requirement. | Platform; models/enforce, production rules and negative tests |
| POL-002/v1 | Independent read-only CI validates central contract data and present locks; retirement requires a reviewed record. | Implemented workflow; required-check configuration is unknown. A pull_request workflow runs the pull request's own copy, so a pull request that edits contracts.yml can weaken it: the check only binds with required code-owner review of .github/workflows/ (or a ruleset-required workflow). Source-owned locks remain checked by their pipeline. | Platform/repository admin; Contract admission check and branch-protection evidence |
| ID-001 | Per-principal application authorization and deployment capability. | Local principals/OAuth implemented; enterprise identity lifecycle is deferred. | Identity owner; effective principal configuration, access tests and revocation drill |
| ID-002 | Cloud workload federation binds repository IDs, owner ID, main, environment and exact workflow. | Terraform implemented; inherited access and actual trust must be inspected. | Cloud/security; WIF and IAM exports |
| REL-001 | Production copies immutable staging images, uses separate project/environment review and observed rollout steps. | Implemented; full production gate is specified in PRODUCTION-GATE.md, not claimed as enforced. | Release owner; staging/promotion receipts and reviewer settings |
| OPS-001/v1 | Durable operation identity/events/outbox precede dispatch; one sender; stale local updates cannot overwrite newer plans. | Single SQLite writer only; off-host intent enforcement requires `durable_intents`. No automatic resend after uncertainty. | Platform operations; ledger tests, witnessed intents and restart drill |
| OPS-002/v1 | Restore imports off-host intents as unknown and preserves job quota; admission remains fenced until reconciliation. | CLI and import implemented; complete listing, old-writer fence and measured backup freshness require a drill. | Platform operations; restore evidence, consistent backup checksum and intent retention policy |
| OPS-003/v1 | Persistent due times and bounded scheduler lanes reserve follow-up, job guard, watchdog and backup capacity. | In-process lanes; thread timeout cannot forcibly stop code. Provider calls and reconciliation pages are bounded. | Platform operations; scheduler lag/status, stall tests and pilot load measurements |
| REC-001/v1 | Checkpoint generations, heartbeat, exact request identity, pending mutations and conditional traffic updates constrain recovery. | Unknown mutation/manual drift holds; cannot undo migrations or external side effects. etags are not a cross-resource fence. | Release operations; checkpoint, receipt and interrupted-run tests |
| REC-002/v1 | Independent supervisor waits for original runner completion and dispatches the same production queue. | Opt-in infrastructure/workflow; GitHub outage requires manual recovery. Live timing and alert delivery unverified. | Release operations; supervisor settings, environment/WIF evidence and runner-loss drill |
| ISO-001/v1 | Dependency hooks execute only in disposable remote workers; controller Docker fallback disabled. | Workers must be provisioned in separate project. Network/IAM containment needs adversarial live tests. Exception: `resolver_local_fallback: true` runs resolution in the controller's Docker until workers exist; off by default. | Platform/security; pinned worker images, build provenance, effective firewall/IAM and resolver tests |
| JOB-001/v1 | Registered workload identity, released image/spec, atomic UTC-day/concurrency reservation and idempotency key precede execution. | Broker implemented; quota is only authoritative after callers lose all direct launch IAM. Unknown outcomes remain charged. | Application/platform owners; broker tests, IAM-denial drill and quota reconciliation |
| COST-001 | Compute estimates and optional ceilings reject excessive planned bounds; budget response closes managed workloads. | Bounds exclude some costs. Notification delay, storage and already-running work prevent a hard zero-overshoot dollar guarantee. | FinOps/platform; measured bill, cap drill and alert owner |
| DATA-001 | Infrastructure policy guards destructive changes; backups and database recovery settings are declared. | A traffic rollback does not restore data. Restore correctness and RPO are measured, not inferred. | Data owner; retention, backup/restore drills and change review |
| OBS-001 | Audit events, release receipts, runtime checks, logs, alerts and operational status expose outcomes. | Alert recipients/delivery and retention unverified; not a compliance certification or achieved SLO. | Platform/on-call; alert tests, evidence retention and incident records |

No ordinary exception may admit a verified credential or turn an unknown mutation into an assumed failure. Existing exceptions such as absent tests, informational vulnerability modes and provenance availability must be explicit in release evidence. A richer expiring exception model and enterprise policy engine belong to the future gate.

The repository's historical ROADMAP and VALIDATION entries describe their original dates. They are not fresh proof that these new controls are installed or that earlier limitations still apply. Current claims should link to this catalog and the implementation/rollout record.

## Dependency map

What each part needs in order to work, and what keeps working when it is gone.

| Component | Depends on | When it is unavailable |
|---|---|---|
| Controller (one VM, SQLite) | GitHub App key, observer identity, backup bucket, off-host intent prefix when `durable_intents` is on | New plans, dispatches, resolutions and job admissions pause; deployed services keep serving |
| Release runners (GitHub Actions) | GitHub, the pinned platform workflows, WIF trust per environment, Artifact Registry, Cloud Run | No new release; an interrupted one is restored by `recover.yml` (staging) or the supervisor plus `recover-production.yml` (production) |
| Release checkpoints (recovery bucket) | GCS with generation preconditions | Recovery holds; a release stops before its first cloud change |
| Production supervisor | Cloud Scheduler, its own Cloud Run service, its own GitHub App installation, read access to checkpoints | No automatic production recovery; the missing-invocation alert fires; manual recovery per READINESS-ROLLOUT.md |
| Dependency workers | Separate worker project, allowlisted HTTPS proxy, signed transfer URLs | No lock is generated; a reviewed lockfile is required |
| Job broker | Controller, Google OIDC, released job contract, Cloud Run jobs API | New job launches pause; running jobs finish under their cloud limits |
| Backups | Controller backup API, backup bucket | Restore point ages; OPS-002 restore cannot meet its RPO |
| Alerts | Cloud Monitoring email channel and its named recipient | Failures are recorded but nobody is told |
| Application job identities | Broker-only IAM after cutover | Before cutover, direct `jobs.run` bypasses the quota (JOB-001) |
