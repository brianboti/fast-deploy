# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability").
Do not open a public issue for security problems, and never include operator tokens, GitHub credentials,
service-account keys, or cloud identifiers in a report. Maintainers acknowledge reports as quickly as they can
and coordinate a fix and disclosure with the reporter.

## Supported versions

Only the current `main` branch is supported. Application repositories pin a platform commit in their
workflows; after a security fix, regenerate those workflows with the new commit (see docs/SETUP.md).

## Scope and design

The trust boundaries, invariants and known limitations are documented in docs/ARCHITECTURE.md. In short:
operator tokens are stored only as hashes and rate limited; GitHub and cloud credentials never leave the
server or the deployment runner; cloud access uses short-lived federated identity; actions are pinned to
commits; dependencies are hash-locked and audited; images are deployed by digest with signed build
provenance; and the model-facing tools are narrow and authorized on every call.
