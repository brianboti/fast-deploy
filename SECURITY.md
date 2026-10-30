# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Do not open
a public issue for security problems, and never include principal tokens, service-account keys, secret values or cloud
identifiers in a report. Maintainers acknowledge reports as quickly as they can and coordinate a fix and disclosure
with the reporter.

## Supported versions

Only the current `main` branch is supported. Operators run the services from their images or from a checkout of
`main`; after a security fix, rebuild the image or update the checkout and restart.

## Scope and design

The trust boundaries and invariants are documented in `docs/INFRASTRUCTURE-ARCHITECTURE.md` ("Security boundaries")
and `docs/DELIVERY-SPEC.md` (sections 2 and 10). In short:

- Callers are principals identified by bearer tokens stored only as SHA-256 hashes and compared in constant time, with
  budgets per identity and per failing address. The Claude chat sign-in is an OAuth 2.1 flow the service runs itself,
  with PKCE, codes and tokens kept as hashes, refresh tokens used once, and registration only for Claude's callbacks.
- No cloud credential lives in any file: the Google adapter takes a token from the host (the metadata server, or
  gcloud with impersonation), and the delivery service acts as each environment's service account through 15-minute
  tokens from the IAM Credentials API.
- The contracts are strict and carry nothing executable: no script, command, key, repository, Dockerfile, build,
  volume or privilege; unknown fields are refused. Commands run only in one module of the delivery service, with fixed
  argument lists (`gcloud`, `cosign`), and nowhere in the infrastructure service but its token helper.
- Access is granted only on resources a request created, through each resource's own policy, to members in the
  operator's domains, with a fixed list of roles; never on the project.
- Secret values never reach a response, the state file or a log; generated credentials go to secrets the request
  owns, and log fields under secret-looking keys are redacted.
- Images are deployed by digest only, from registries the environment allows, with an optional Cosign attestation
  check per environment; the host script is checked against its recorded hash before it is sent.
- Dependencies are hash-locked and audited weekly; actions are pinned to commits; the images' inputs are pinned by
  digest or checksum.
