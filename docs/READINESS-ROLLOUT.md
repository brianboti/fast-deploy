# Phases 0 and 2: upgrade, cutover and recovery

The implementation remains a single active controller with SQLite. No step authorizes running two controllers against divergent copies. New workers and recovery services are opt-in; their defaults do not mutate a live installation. Do not mark the company pilot ready from unit tests alone.

## Upgrade order

1. Save a consistent SQLite backup using the existing backup command/API and record its checksum, current version and configuration revision. Keep the previous image and matching backup together. Copying only a live SQLite main file can lose WAL transactions.
2. Merge and require **Contract admission / Validate contracts** in branch protection. Review the first introduction because the base branch initially lacks the validator. Protect workflow/policy changes with independent review. Validate central contracts and their present lockfiles; missing locks may legitimately live in the source repository and are still checked by the release pipeline.
3. Decide `resolver_local_fallback` before updating a controller that has no workers: without it, repositories without a lockfile can no longer be onboarded. Deploy the controller migration to the single writer. Existing plans are imported into the ledger without dispatching them. A schema version newer than the controller is refused. A downgrade requires the matching pre-upgrade backup and reconciliation; never downgrade by deleting ledger tables.
4. Provision immutable intent storage. Enable `durable_intents: true` with `backup_bucket` and `backup_prefix`; give the controller object-create/read access to `<prefix>/intents/v1/`. Deny deletion/overwrite for this writer, configure retention appropriate to the longest supported restore window, and test writes and create-if-absent conflicts. An uncertain launch remains unknown, never automatically resent. Retain job witnesses for every day that an accepted backup can roll back across.
5. Publish a reviewed platform commit and update every application's pinned workflows. Set the platform's existing minimum-supported commit after verifying the new workflows. Central, repository-owned, reuse, promotion and job-image paths must all pass fresh secret admission. Existing pins remain outside the new control until upgraded. Receipts include fresh sanitized source and image evidence; raw findings are not published.
6. Provision isolated workers and recovery in observation mode. Complete the drills below before enabling automatic operation. Enable broker-only IAM only after callers have migrated and existing job executions are accounted for.
7. Record the effective installation evidence, perform a canary, and observe seven days before declaring the pilot ready. The proposed ten-minute recovery, thirty-minute controller restoration and five-minute metadata RPO are drill targets, not measured claims.

## Remote dependency resolution

Instantiate `infra/gcp/modules/resolver` in a dedicated worker project. Its jobs have a resource-unprivileged service account, one task, zero retries, 1 CPU/1 GiB and a 900-second deadline. The controller may invoke fixed jobs and inspect executions, but never creates jobs from a caller's command. An exact image digest, identity, resources, network and proxy are checked before launch.

Generate each build context with `python -m workers.resolver.build_context python-3.12-compile OUTPUT_DIR` (also Python `export`/`export-dev` and `node-24-lock`). Build and scan in a disposable build environment, publish the digest and put only reviewed digests in the module's `images` map. The Node build installs its Python launcher from Debian packages; archive the build provenance/SBOM and review the final image digest. The `mode` and resolver command are image content, not request arguments. Build contexts contain no controller package, repository checkout or credentials.

The module creates a dedicated VPC and an allowlisted HTTPS proxy. Worker egress permits only that proxy; the proxy denies private destinations and allows exact public package/storage hosts. The worker identity has no resource roles. Metadata may still issue identity tokens: do not trust the worker identity at the broker, controller, GitHub, or privileged services. Firewall policy inherited from the organization must be checked for precedence. Proxy failure must fail resolution, not restore direct egress.

Copy the module's `resolver_configuration` output into the controller's `resolver` setting. The controller uses short-lived, generation-bound input GET and create-only output PUT URLs signed through a separate transfer identity. Treat transfer URLs as credentials; do not log execution overrides. The controller validates output size, binding, sources and hashes. A compromised worker may return malicious data; successful execution is not admission evidence by itself.

A proposal that needs a lock returns `state: resolving` and `operation_id`. Use the existing `status` tool (or `/plans/<id>`) to inspect it, then repeat the proposal. A changed source commit or worker digest creates a new binding. At most two unresolved workers are admitted. Unknown executions consume capacity until reconciled. No dependency entry point falls back to local Docker on its own; without workers, supply a reviewed lockfile. An installation that onboarded lockfile-less repositories before this release may set `resolver_local_fallback: true` until its workers exist: the controller then runs package resolution in its own Docker as before, which ISO-001 records as an exception. Remove the setting once `resolver` is configured; with both set, the workers are used. Private registries need a separate reviewed credential design.

## Application job admission

Configure `job_broker.audience` as the exact audience callers mint, and `job_broker.subjects` as immutable numeric Google service-account IDs mapped to one application/environment. Human controller tokens are not accepted on broker routes. Never register worker identities as callers.

Call `POST /job-broker/runs` with a Google OIDC bearer token and JSON such as `{"job":"worker","idempotency_key":"issue-123-attempt-1","arguments":["123"]}`. Preserve the key across transport retries. A different payload under the same key is refused. `GET /job-broker/runs/<id>` reports the scoped state. The broker chooses image, identity, task count, resource limits and timeout from released evidence; callers cannot supply those fields. Arguments are not included in off-host witnesses or public status.

Reservations are atomic across the UTC day and current executions. A provider creation operation finishing does not mean the job finished. Unknown or interrupted launches stay charged; there is no automatic refund or blind retry. A launch that never left the controller (the intent write or a check failed first) or that Cloud Run refused outright frees its concurrency slot and keeps the day's charge. A launch whose outcome stays unknown holds its slot until an operator has confirmed in Cloud Run that no execution carries its id, then runs `python -m labdeploy.reconcile --config /secure/config.yaml release-job --operation <id> --evidence <file>` with `operator`, `operation` and `no_execution_found: true`. The legacy cancellation guard remains a backstop, not the primary admission limit. The spending cap's closed ingress blocks new admission.

Cut over one application at a time: stop new calls, inventory existing executions, drain them, deploy callers using the broker, then set `job_broker_enabled: true` in the reviewed service module settings. This removes the application's direct job-execution/act-as grants and adds the broker's job-scoped execution grant. Test that direct `jobs.run` is denied and broker admission succeeds. Inspect inherited project, folder and organization grants too: removing a module grant alone does not prove broker-only access. Reverting IAM to direct execution reopens quota bypass and must be recorded as an explicit rollback of the guarantee.

## Independent production recovery

For each production service, enable `automatic_recovery_enabled` only through reviewed production infrastructure. Populate a separate `production-recovery-<service>` GitHub environment. Copy the original target variables from `production-<service>` unchanged, plus the recovery output variables. Authentication uses `RECOVERY_SERVICE_ACCOUNT`/`RECOVERY_WIF_PROVIDER`; the original target fields remain the checkpoint binding. Restrict the environment to main and the dedicated workflow; automatic recovery must not wait for the ordinary forward-deployment approval.

Provision `infra/gcp/modules/recovery-supervisor` independently of the controller with a pinned platform image, an alert recipient, and `observe_only: true`. Use a separate GitHub App installation restricted to the platform repository with Actions write and Contents read; its Secret Manager key must not be the onboarding App's key. The supervisor can read release checkpoints and write only its recovery-request markers. It has no Cloud Run mutation permissions. Cloud Scheduler invokes its IAM-protected `/tick` once a minute. Do not grant `allUsers` or `allAuthenticatedUsers` invoker.

The supervisor checks schema, exact target, release identity, heartbeat and GitHub workflow identity. It requests cancellation of a stale runner, waits for a later confirmed terminal status, then dispatches `recover-production.yml`. Recovery uses exactly the same `gcp-production-<application>` queue as promotion and refuses a checkpoint for another plan. A successful cancellation response alone is not a fence. Duplicate scheduler deliveries use a generation-checked dispatch marker; an uncertain recovery dispatch is held for reconciliation.

Cloud Run does not expose a traffic-only IAM permission. The recovery identity's service-scoped update permission is broader than traffic; compensate with exact WIF workflow/environment binding, protected platform code, no image-publishing authority, and effective-IAM review. Conditional traffic updates compare the exact split and use an etag. This is not an external fencing token across all resources. Unknown mutations, active provider reconciliation, manual traffic/job drift, conflicting checkpoints, or incomplete job restoration require manual review. Jobs are restored to recorded images; migrations and external side effects are never undone or rerun.

After a successful observation drill, set `observe_only: false`. Verify delivery of both module alerts: supervisor failures/manual holds, and no supervisor invocation for ten minutes (scheduler or service stopped). GitHub unavailability is a manual recovery case: the supervisor does not become a competing cloud writer. Keep admission paused, establish that the previous runner and provider operations cannot resume, inspect the checkpoint and actual cloud state, then perform a reviewed restoration and record evidence. Never clear a pending mutation merely because a timeout expired.

## Restore a controller

Fence the old controller host and its dispatch credentials before restoring. Restore a consistent backup to an isolated host with no automatic scheduler startup. Run:

```
python -m labdeploy.reconcile --config /secure/config.yaml hold
python -m labdeploy.reconcile --config /secure/config.yaml import-intents
python -m labdeploy.reconcile --config /secure/config.yaml status
```

The import reads every retained off-host intent with generation binding. Incomplete listings, incompatible schemas and conflicting identities fail closed. Missing release requests, resolver launches and job reservations are reconstructed as unknown, never as a new allowance. Job arguments are not recoverable and cannot be relaunched from a witness. Restored resolver outputs must be revalidated. Reconcile GitHub runs, Cloud Run operations/executions and quota reservations before resuming. Do not delete an unknown record to make the queue empty.

Resume requires an evidence JSON file with `operator`, `old_writer_fenced: true`, `intent_listing_complete: true`, `provider_reconciliation` and `quota_reconciliation`. `python -m labdeploy.reconcile --config /secure/config.yaml resume --evidence /secure/restore-evidence.json` refuses unresolved prepared/sending/unknown outbox records and audits the evidence hash. A configured `operations_recovery_mode: true` remains an additional hold and must be cleared through reviewed configuration. Start only one controller, then verify follow-up and backup freshness.

## Required live evidence

Record operator, UTC times, application/project, platform/workflow/image revisions, observed state, expected result, evidence links and cleanup for each drill:

- Secret finding in reviewed/unreviewed checkout, reused image, promoted image and job image; scanner outage/malformed output stops admission without publishing the credential.
- Kill controller before/after dispatch; retry the same key; restore an older backup; lose the response after cloud acceptance. No duplicate mutation and no erased quota.
- Kill a production runner during a stable bake; verify independent recovery latency, same queue, exact restored traffic/job images and alert delivery. Separately kill during an unknown mutation and prove the manual hold.
- Change traffic manually and race a conditional update; prove recovery does not overwrite the edit. Lose GitHub/API access and verify hold plus alert.
- A malicious dependency hook cannot read controller secrets, use a Docker socket, reach private/control-plane destinations, publish another operation's output or starve safety loops. Test firewall bypass and inherited IAM, not merely environment variables.
- One hundred concurrent broker requests, UTC day boundary, caller identity/audience failures, cap closure, direct-IAM denial and old-backup reservation recovery.
- Deliberately stall lifecycle/reporting work and verify follow-up/job-guard/backup scheduling and measured lag. Complete and time a full restore.

Archive the drill evidence with the control catalog. Until it exists, implementation and local tests remain distinct from live verification.
