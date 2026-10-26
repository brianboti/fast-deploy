# 1.32.0: company readiness, phases 0 and 2

This release implements phases 0 and 2 of the October 23 readiness plan. Codex started the work and Claude finished it. Phase 1
(company identity) is not part of this release.

Phase 0 adds a control catalog with a dependency map, and verified secrets now block every release path whatever the
code's review status. A contract check in CI runs for every pull request, and a written production-gate specification
describes a future gate without enforcing it.

Phase 2 adds:

- an operation ledger, with intents written off-host before any dispatch;
- a scheduler whose safety work cannot be starved;
- dependency resolution in isolated workers;
- a job broker that reserves quota before a job starts;
- an opt-in supervisor that recovers an interrupted production release through GitHub.

An independent review found fifteen issues, and thirteen are fixed with tests (docs/READINESS-IMPLEMENTATION.md). Every
new part is off until configured. `resolver_local_fallback: true` keeps lockfile generation on the controller until the
worker project exists. Nothing here has been exercised live; docs/READINESS-ROLLOUT.md lists the drills.
