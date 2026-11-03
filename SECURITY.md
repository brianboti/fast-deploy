# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Do not open
a public issue for security problems, and never include principal tokens, service-account keys, secret values or cloud
identifiers in a report.

What a reporter can expect, in calendar days from the report:

| Step | Within |
|---|---|
| Acknowledgement | 3 days |
| A first assessment: accepted or not, and its severity (CVSS v4) | 7 days |
| A fix on `main` for a critical finding | 14 days |
| A fix for a high finding | 30 days |
| A fix for a medium finding | 90 days |
| A low finding | the next release that touches its code |

Disclosure is coordinated with the reporter: a GitHub security advisory is published with the fix, crediting the
reporter unless they ask otherwise, and no later than 90 days after the report unless both sides agree to wait. Fixes are
listed under "Security" in `docs/CHANGES.md`.

## Scope

In scope:

- the two services on `main`: `cloudinfra/` and `appdelivery/`, their HTTP and MCP interfaces and the Claude chat
  sign-in;
- the images built from `Dockerfile.cloudinfra` and `Dockerfile.appdelivery`, and the host script
  `appdelivery/host/release.sh`;
- what the services create in a cloud project, against the defaults this file and the documents promise;
- this repository's workflows, locks and pins.

Out of scope:

- Google Cloud itself, and an operator's project beyond what `docs/SETUP.md` asks for;
- an attack that needs the operator's own accounts, the services' host, or a principal token already;
- volume-based denial of service;
- a vulnerability in a dependency that does not reach this code (report it upstream; `pip-audit` and the image scan in
  CI follow the published ones);
- `examples/`, which only shows the contracts.

## Supported versions

Only the current `main` branch is supported. Operators run the services from their images or from a checkout of
`main`; after a security fix, rebuild the image or update the checkout and restart.

## Scope and design

The trust boundaries and invariants are documented in `docs/INFRASTRUCTURE-ARCHITECTURE.md` ("Security boundaries")
and `docs/DELIVERY-SPEC.md` (sections 2 and 10). In short:

- Callers are principals identified by bearer tokens stored only as SHA-256 hashes, or as HMAC-SHA-256 with a pepper
  kept outside the configuration, and compared in constant time, with budgets per identity, per failing address and per
  public path. The Claude chat sign-in is an OAuth 2.1 flow the service runs itself,
  with PKCE, codes and tokens kept as hashes, refresh tokens used once, and registration only for Claude's callbacks
  and only for public clients, small, few per address and forgotten after a day without a sign-in; a token is issued
  only for the service itself (RFC 8707) and the answer names its issuer (RFC 9207). Request bodies are bounded (413
  above 64 KiB).
- No cloud credential lives in any file: the Google adapter takes a token from the host (the metadata server, or
  gcloud with impersonation), and the delivery service acts as each environment's service account through 15-minute
  tokens from the IAM Credentials API. A machine receives only a five-minute token of a pull-only account; the
  environment's own token never leaves the service. The SSH key gcloud registers for OS Login expires after fifteen minutes.
- The contracts are strict and carry nothing executable: no script, command, key, repository, Dockerfile, build,
  volume or privilege; unknown fields are refused. Commands run only in one module of the delivery service, with fixed
  argument lists (`gcloud`, `cosign`), and nowhere in the infrastructure service but its token helper.
- Access is granted only on resources a request created, through each resource's own policy, to members in the
  operator's domains, with a fixed list of roles; never on the project.
- Secret values never reach a response, the state file or a log; generated credentials go to secrets the request
  owns, and log fields under secret-looking keys are redacted, as is credential-shaped text anywhere in a log line.
- What the infrastructure service makes follows the CIS benchmarks and Google's hardening guides by default: nothing
  reachable from the internet unless the operator allows public exposure; private cluster nodes with Dataplane V2,
  Binary Authorization and Pod Security baseline on each team's namespace; networks with flow logs, logged rules and a
  NAT, or with egress denied; HTTPS with a modern TLS policy and the operator's Cloud Armor policy on load balancers;
  databases with the CIS flags and IAM authentication; machines without serial ports and protected from deletion;
  Cloud Run reading only the secrets it names; the operator's KMS keys where given. The one project IAM write, a
  cluster node account's role, is made only by an account limited to granting that role.
- Images are deployed by digest only, from registries the environment allows, and only digests this service had Cloud
  Build build from a linked repository at one commit; Google's signed provenance is held to the one build step the
  service composed, with an optional Cosign attestation check per environment; the host script is checked against its
  recorded hash before it is sent, and runs every container without privilege escalation, with a reduced capability set
  and CPU, memory and process bounds; Kubernetes workloads get the same. A team's builds are bounded at once and per day.
- Dependencies are hash-locked and audited weekly, and both images are scanned as built; actions are pinned to
  commits; the images' inputs are pinned by digest or checksum. CodeQL, OpenSSF Scorecard and a dependency review run on
  the repository, and property tests generate input for the contracts' validators.

## Identifiers in this repository

The test fixtures name one Google Cloud project, `project-3e2167e6-db58-4ab7-a22` (number `1071494834779`), and its
build account. It is a sandbox used only for the recorded runs on a real project (`docs/DELIVERY-SPIKE.md`), emptied
after each, and its identifiers are published on purpose, so that the fixtures are Google's own output. An identifier
is not a credential: nothing in this repository grants access to that project. No tracked file holds a credential; a
test sweeps every tracked file for the published shapes of keys and tokens (`tests/test_repository.py`).
