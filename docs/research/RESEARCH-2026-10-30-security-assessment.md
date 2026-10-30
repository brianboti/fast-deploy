# Security assessment of the two services against the standards of 2026, with a plan. 2026-10-30

Written 2026-10-30 UTC as a read-only review of `cloudinfra` 1.37.1 and `appdelivery` 0.1.0 at commit f4c14dd, after
the trusted-build change ([RESEARCH-2026-10-30-trusted-build.md](RESEARCH-2026-10-30-trusted-build.md)). Nothing in
the code was changed. The question: is the project in the best security state it can be, measured against what the
specifications, the benchmarks and the large platforms ask for today, and if not, what must change, in what order.

Method. Five reviews were run in parallel and every finding below was then checked again by hand against the code
or by running the thing (docker, pip-audit, the GitHub API): (1) the current external standards, read from their
primary pages; (2) `cloudinfra`'s authentication, OAuth server, authorization, validation, logging, state and HTTP
layer; (3) `cloudinfra`'s Google adapter and orchestration against the CIS and Google hardening controls;
(4) `appdelivery` end to end, the host script included; (5) the supply chain, CI, images and the repository's
settings. Baseline at the start: `ruff` clean, 724 tests pass, `pip-audit` clean on both requirement locks. Sources
are marked F (read on the page) or S (a secondary source or a search snippet); section 8 lists them.

**Status, 2026-10-30.** Release S1 of the plan (section 7) was built the same day on branch `security-s1`: the four
High findings, M1, M2, M3, M4, M5, M8, M9, L1, L2, the repository-side items of M16 and M17 that live in files, and the
document corrections of section 6, each with a test (`tests/cloudinfra/test_infra_security_s1.py`,
`tests/appdelivery/test_delivery_security_s1.py`). Left to the maintainer's hands: Dependabot alerts and the review
requirements in the repository's settings. Not yet run against a real Google project.

## 1. The short answer

**The design is right and most of its claims hold.** Tokens are hashed and compared in constant time; the OAuth
server does PKCE S256 only, exact callback matching, single-use codes and refresh rotation with family revocation;
every tool and route is authorized and another owner's objects are NOT_FOUND; the contracts carry nothing executable;
only two binaries ever run, with fixed argument lists; the host script is hash-checked and takes base64 only; images
are released by digest only, built only by Cloud Build, with Google's provenance signature verified in code;
dependencies are hash-locked and audited; actions are pinned to commits; the images run as a non-root user. That is
further than most internal platforms of this size get. SECURITY.md and the two architecture documents describe the
code accurately, with the handful of drifts listed in section 6.

**It is not yet in its best state.** No Critical finding, four High, seventeen Medium. The four High findings are
places where the stated boundary does not hold:

| | Finding | Where |
|---|---|---|
| H1 | The environment's full-scope service-account token is sent to every team's machine for the image pull; root on one team's machine can act as the environment on every other team's machines, secrets and services in that environment for 15 minutes | `appdelivery/machine.py:130-133`, `releases.py:244-245`, `host/release.sh:53-57` |
| H2 | A build `context` of the form `github.com/org/repo` makes docker clone an arbitrary public repository, while the signed provenance and the build record still name the linked repository and commit | `appdelivery/models.py:33`, `cloudbuild.py:42` |
| H3 | A workload identity is bound to `PROJECT.svc.id.goog[<request name>/<identity>]`, and request names are not unique; a second principal with the same request name and a cluster gets the first request's identity | `cloudinfra/providers/gcp/identity.py:38`, `cluster.py:99` |
| H4 | "SSH only from the IAP range" holds for the rule the network block makes, but `allowed_ports`/`allowed_sources` (default `0.0.0.0/0`) and `configure_firewall` can open 22 or 3389 to the internet | `cloudinfra/models/request.py:360-363`, `providers/gcp/network.py:41-42` |

Each is a small change with a test (section 7, release S1). The Medium findings are mostly hardening the standards
now expect by default: request body limits, the unbounded OAuth client table, Pod Security and `docker run` flags,
GKE and Cloud SQL settings, flow logs, a cost-ceiling bypass on resize, a build budget per team, the stale `audit.lock`,
Dependabot alerts and review requirements that are off, and the organisation-policy backstops the documents do not
yet list.

## 2. What the standards of 2026 ask for

The reference frame this review measured against, and what in it applies here.

**MCP specification 2026-07-28** (F). Authorization servers MUST implement OAuth 2.1; MCP servers MUST serve RFC 9728
protected resource metadata; clients MUST send RFC 8707 `resource`, and servers MUST validate that a token was issued
for them; tokens only in the `Authorization` header; Origin MUST be validated on every HTTP request; dynamic client
registration (RFC 7591) is now deprecated in favour of Client ID Metadata Documents (CIMD), which the server SHOULD
support; `iss` on authorization responses (RFC 9207) is SHOULD and expected to become MUST; refresh tokens of public
clients MUST rotate; protocol-level sessions and the `Mcp-Session-Id` header are gone (SEP-2567); tools MUST validate
inputs, implement access control, rate limit and sanitize outputs. Anthropic's connector documentation (F) says Claude
still speaks the 2025-11-25 revision at most, uses CIMD only when the metadata advertises
`client_id_metadata_document_supported` and `"none"` in `token_endpoint_auth_methods_supported`, otherwise DCR; the
review criteria require `title` and `readOnlyHint`/`destructiveHint` on every tool and separate read and write tools;
Anthropic's egress is `160.79.104.0/21`. The MCP Python SDK 1.30.0 the service pins is the fix release of three
advisories of 2026-10-27 (audience check, redirect following, `$ref` fetching) and of the stateful-session
accumulation (CVE-2026-59951); the earlier ones (DNS rebinding off by default, CVE-2025-66416; session principal
check, CVE-2026-52869) are all behind it (F, OSV).

**OAuth 2.1 draft-16 (September 2026) and RFC 9700 (BCP 240, January 2025)** (F). PKCE mandatory, `plain` prohibited;
exact redirect URI match; refresh tokens of public clients one-time use or sender-constrained; codes at most ten
minutes; tokens audience-restricted; never in a query string; `iss` against mix-up. ASVS 5.0 chapter V10 encodes the
same with levels.

**Supply chain.** SLSA v1.2 is current (v1.1 retired) (F): Build L3 needs provenance that tenants cannot forge, signing
material out of reach of the build steps, and isolation between builds; a verifier checks the signature, the subject
digest, the builder, the canonical source, the `buildType` and the `externalParameters`, and "any unrecognized
externalParameters SHOULD cause verification to fail". Cloud Build's provenance meets L3 and is signed with Google's
`google-hosted-worker` key (F). OpenSSF Scorecard's twenty checks (F) include Branch-Protection, Code-Review,
Dependency-Update-Tool, Pinned-Dependencies, SAST, SBOM, Signed-Releases and Token-Permissions. GitHub's hardening
guide (F) now also offers workflow dependency lockfiles (October 2026 preview). OWASP Top 10:2025 (F) adds A03 Software
Supply Chain Failures: SBOM, signed packages, provenance, "no single person should be able to write code and promote
it all the way to production". NIST SSDF 1.1 is still the final core; SSDF 1.2 is a draft of December 2025 (F).
Cosign had four verification-bypass advisories in 2026 (F, OSV); the lesson is exact identities, never a regex `.*`.
pip 25.x has five 2026 advisories fixed in 26.x (F, OSV).

**Google Cloud.** CIS Google Cloud Platform Foundations Benchmark v5.0.0 is current (S: the PDF is behind CIS
WorkBench; control ids below follow v2–v4 numbering, which v5 keeps). Google's enterprise foundations blueprint (F)
names the organisation policies to enforce (`iam.disableServiceAccountKeyCreation`,
`iam.automaticIamGrantsForDefaultServiceAccounts`, `compute.vmExternalIpAccess`, `compute.requireOsLogin`,
`compute.disableSerialPortAccess`, `storage.publicAccessPrevention`, `storage.uniformBucketLevelAccess`,
`sql.restrictPublicIp`, `sql.restrictAuthorizedNetworks`, `iam.allowedPolicyMemberDomains`, …) and the detective
controls (Data Access logs where sensitive, VPC flow logs at 50 percent on every subnet, firewall rule logging,
organisation sinks with Bucket Lock, Security Command Center). Google's service-account guidance (F): avoid keys;
bearer tokens must be protected from leaking; downscope with Credential Access Boundaries; never Token Creator at
project level; enable Data Access logs for IAM and STS so impersonation is recorded. GKE hardening (F, page of
2026-10-26): release channels, private nodes, authorized networks, Shielded nodes, Workload Identity Federation,
NetworkPolicy, Pod Security admission, Binary Authorization. Cloud Run (F): ingress internal by default, invoker
check on, dedicated service account, Binary Authorization.

**Application and API.** ASVS 5.0 (F): V4 (content type, proxy headers, method allow-list), V13 (backend
authentication with short-lived credentials, secrets out of source, no debug endpoints, no version disclosure at L3),
V16 (log who/what/when/where, never credentials, encode against injection, ship logs elsewhere, generic errors).
OWASP API Security Top 10 2023 (F): BOLA, broken authentication, unrestricted resource consumption, BFLA. The REST
cheat sheet (F): size limits with 413, method allow-list with 405, `Cache-Control: no-store`, HSTS. NIST SP 800-63B-4
(F, July 2025): centrally verified secrets should carry "a keyed hashing or encryption step using a verifier-only
secret" kept apart from the store; that is the stronger reference for how principal tokens are hashed.

**AI agents.** OWASP Top 10 for LLM Applications 2025 (F): LLM01 prompt injection, LLM06 excessive agency
("implement authorization in downstream systems rather than relying on an LLM"). OWASP Top 10 for Agentic
Applications 2026 (F, December 2025): ASI03 identity and privilege abuse ("per-agent identity with short-lived,
task-scoped credentials is the baseline"), ASI04 agentic supply chain, ASI09 human-agent trust ("show raw actions at
confirmation time and log what was displayed"). CISA and partners published "Careful adoption of agentic AI services"
on 2026-05-01 (S). The literature on MCP (F: arXiv 2504.03767, 2504.08623, 2512.08290, 2603.18063, 2601.17549):
tool poisoning through descriptions, confused deputies, token passthrough, and the finding that most public servers
have no authorization at all. This service's shape, a typed contract, server-side authorization on every call, no
passthrough, no instructions in tool descriptions, is what those papers recommend.

**Horizon.** EU Cyber Resilience Act reporting duties started 2026-09-11 (F) if the product is placed on the EU
market; NIST IR 8547 final 2026-10-27 (F) sets the post-quantum timeline (deprecate quantum-vulnerable signatures after
2030, disallow after 2035) and Google's Cloud KMS already offers ML-DSA keys (F), which matters for the attestation
key the delivery service trusts.

## 3. What holds

Verified in code, with the strongest evidence. The documents' claims are at INFRASTRUCTURE-ARCHITECTURE.md
"Security boundaries" and DELIVERY-SPEC.md sections 2, 3 and 10.

- **Caller identity.** 256-bit tokens from `secrets.token_urlsafe(32)` (`cloudinfra/cli.py:60`, `appdelivery/cli.py:45`),
  SHA-256 hashes compared with `hmac.compare_digest` (`cloudinfra/security/auth.py:26-29`), read from the
  `Authorization` header only, never from a query string, never logged (`tests/cloudinfra/test_infra_sign_in.py:93`).
  A valid token never touches the failure budget, so the per-address lockout cannot be turned against legitimate
  callers (`auth.py:83`).
- **OAuth 2.1 server.** PKCE S256 only (the SDK refuses `plain`), code lifetime 300 s and bound to the client,
  redirect URI exact-match against the registered one, registration only for URIs string-equal to the configured
  Claude callbacks (`security/oauth.py:176-182`, `config.py:101,121-126`), refresh rotation on every use with
  family revocation on reuse (`oauth.py:200-218`), access tokens 60 minutes, refresh 30 days, bounded in config,
  sign-in ended when the principal is removed or re-keyed; RFC 8414 and RFC 9728 metadata served, 401 with
  `WWW-Authenticate: Bearer resource_metadata=`; the sign-in page escapes every interpolation and sends CSP
  `default-src 'none'`, `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`,
  `Referrer-Policy: no-referrer` (`oauth.py:47-48, 243-254`); off unless configured.
- **Authorization.** All 57 `cloudinfra` tools and the REST routes go through the same ASGI gate and the same
  `require`/`_record` checks; `scope: own` answers NOT_FOUND for another owner; ids are validated before lookup; the
  identity comes from the context variable the gate set, never a parameter. Members may be only `user:`, `group:`
  or `serviceAccount:` with an email (`security/validation.py:24`), so `allUsers`, `allAuthenticatedUsers`,
  `domain:` and federated principals are impossible; the domain allow-list is an exact match; roles come from one
  fixed table and only those roles are ever removed (`providers/gcp/iam.py:13-26, 49-59`); every policy write is
  read-modify-write with the etag and three attempts on conflict; IAP tunnel grants are bound to the instance's
  numeric id. `appdelivery` re-checks owner, READY and the GRANTED admin grant before every rollout, rollback and
  removal and again when the worker starts (`planner.py:35-46`, `releases.py:209-220`).
- **No command from a caller.** `appdelivery/remote.py` is the only module that runs anything, only `gcloud` and
  `cosign`, each with a fixed argv; the boundary tests forbid `subprocess` elsewhere in both packages. The host
  script is sent on stdin each time and checked against its recorded hash over the same bytes (`machine.py:58-62`);
  its inputs are base64 assignments decoded only through one function, with a test that shell metacharacters stay
  literal (`tests/appdelivery/test_delivery_host_script.py:143-152`). Env values refuse newlines and NUL; credential-
  looking literals are refused at the contract; the registry token travels on `--password-stdin`, never argv.
- **Trusted build and provenance.** One composed step, builder pinned by digest, user-managed build account,
  `requestedVerifyOption: VERIFIED`, `CLOUD_LOGGING_ONLY`, no substitutions, push only through `images`
  (`cloudbuild.py:37-51`, exact-body test). A release is refused unless its exact `registry/path@digest` was built by
  this service for this team and SUCCEEDED (`state.py:207-212`). Provenance is verified from the DSSE envelope with
  Google's pinned P-256 key over a correctly built PAE, then the subject digest and repository, the builder id, the
  invocation id of the recorded build and the commit are all held to the record (`provenance.py:50-100`); the
  unsigned copy in the occurrence is never read; forged statements under Google's key id fail in tests.
- **What is created.** Shielded VM (vTPM and integrity monitoring always, Secure Boot unless a GPU image without
  signed drivers), OS Login, no service account unless an identity is asked for, no public address unless asked, no
  metadata but `enable-oslogin` (`providers/gcp/compute.py:30-51`); custom-mode VPC with Private Google Access,
  private subnet ranges only, SSH from `35.235.240.0/20` only in the network block's own rule, health-check rule
  limited to Google's probe ranges on the backend port; Cloud SQL private with `sslMode: ENCRYPTED_ONLY`, backups with
  PITR or binary logs, deletion protection lifted only inside an explicit destroy, password generated with
  `secrets.token_urlsafe(24)`, written only to the request's secret and absent from response, state and log (tests);
  Memorystore with AUTH and TLS on the request's network; buckets with uniform access and public access prevention
  enforced, never deleted while non-empty; GKE with shielded nodes, Secure Boot, workload identity, REGULAR channel,
  minimal node scopes, a per-cluster node account with reader on only the registries the operator allows, tenant RBAC
  limited to namespace-scoped `view`/`edit`, an admission policy that refuses LoadBalancer Services, Ingresses and
  Gateways in tenant namespaces, a front door with a managed certificate and a 301 to HTTPS; Cloud Run with a
  key-less runtime account per service, the invoker check on unless the operator allows public services, VPC egress
  to private ranges by default; workload and node identities never get a key.
- **Supply chain.** Every package in the three locks pinned with hashes; `pip-audit` clean on the runtime and dev
  locks; CI with `permissions: contents: read`, `persist-credentials: false`, both actions pinned to commits that
  match the upstream tags, hadolint by version and checksum, zizmor clean; Dependabot for actions with a seven-day
  cooldown; base image by multi-arch digest, the same in both Dockerfiles and tied by a test; gcloud and Cosign by
  SHA-256 per architecture; `USER 65532`; `COPY` limited to the lock and one package; `.gcloudignore` allow-list
  tested; GitHub secret scanning and push protection on; `main` protected with `enforce_admins`, the `test` check
  required, force-push and deletion blocked; no credential anywhere in the history (searched for key, token and
  private-key patterns across all commits).
- **Agent safety.** Every tool carries `title` and the annotations Anthropic's review requires
  (`cloudinfra/mcp/catalog.py:46-49`, `appdelivery/catalog.py:47-50`), read and write operations are separate
  tools, descriptions carry no instructions to the model, unknown arguments are refused before a tool runs, every
  decision is server-side, and nothing a tool returns is executed.

## 4. Findings

Severity: Critical (a remote caller without a principal reaches something), High (a principal or a team crosses the
boundary the documents promise), Medium (a control the benchmarks expect by default is missing, or a bounded abuse),
Low (hardening and drift), Info (documentation or test gaps). Every finding was reproduced or read in the code by
hand after the reviews; none is speculative.

### High

**H1. The environment's cloud-platform token goes to every team's machine.** `appdelivery/releases.py:244-245`
mints a token for `environment.service_account` and `machine.py:130-133` sends it as `IN_TOKEN` to `sudo bash -s` on
the machine, where `host/release.sh:53-57` uses it for `docker login`. The token has `cloud-platform` scope and lives
15 minutes (`google.py:19-20`); there is one service account per environment, shared by every team, and it holds
`osAdminLogin`, the IAP tunnel and `secretAccessor` on every request that named it, `run.developer` on services,
`edit` in cluster namespaces, and project-wide `cloudbuild.builds.editor` (SETUP.md). A team's `admin` members are
root on the machine by design; root can read the shell's variables or memory while the script runs. So root on team
A's dev machine is `appdelivery-dev@` for 15 minutes: other teams' dev secrets, root on their dev machines, their
services. The spec says the machine learns only env values; it learns the environment's identity too. Standard:
Google's service-account guidance ("bearer tokens must be protected from being leaked", downscope, token broker);
OWASP ASI03; SLSA threat model. **Fix:** never give the machine the environment identity. Mint the pull credential for
a separate per-environment (or per-team) *pull* service account that holds only `artifactregistry.reader` on the
environment's allowed registries, with the shortest lifetime the API allows, and send that; or (the spec's own
"later") give the machine its own service account chosen by the request so it pulls as itself and `IN_TOKEN` goes.
Test: the token the host receives is never the environment account's. Also mint a fresh short token for the pull
rather than the cached 14-minute one, and correct DELIVERY-SPEC.md section 4 ("valid for 60 minutes" is 15).

**H2. A build context that is a git URL.** `appdelivery/models.py:33` `SOURCE_PATH` accepts `github.com/evil/repo`
(letters, dots, slashes), and `cloudbuild.py:42` passes it as the last argument of `docker build`. Reproduced on
2026-10-30 with docker 28: both the legacy builder and BuildKit read `github.com/…` as a remote git source
(`[internal] load git source github.com/…`). Cloud Build still fetches the linked repository at the 40-hex commit, so
the provenance names the right repository and commit and `provenance.problems()` passes, while the image was built
from any public repository's default branch with `-f` resolved inside that clone. The build record, `get_build` and
the signed statement all mislead. Standard: SLSA v1.2 verification ("unrecognized externalParameters SHOULD cause
verification to fail"); the project's own trusted-build requirement. **Fix:** anchor both paths (`"./" + dockerfile`,
`"./" + context`), or put the context in `source.connectedRepository.dir` and pass `.`; refuse URL-looking prefixes in
`SOURCE_PATH` (`github.com/`, `git@`, `://`); and compare the provenance's `externalParameters.buildConfig` with the
body this service composed (steps, args, images, options), which would also catch any future argument drift. Tests
for each.

**H3. Workload identity bound to a non-unique namespace.** `cloudinfra/providers/gcp/identity.py:38` binds the IAM
account to `serviceAccount:PROJECT.svc.id.goog[{ctx.name}/{name}]` and `cluster.py:99` creates the tenant namespace as
`ctx.name`, the caller-chosen request name, which PROVIDER-INTERFACE.md says is not unique. The workload pool is
project-wide: the same namespace/KSA pair in any cluster of the project is the same principal. A second principal with
a request of the same name, its own cluster and `admin` on it (the requester gets admin by default) creates KSA
`<name>` in namespace `<name>` and receives tokens for the first request's identity, with all its grants. Standard:
Google's Workload Identity Federation for GKE warning on namespace/KSA collisions; tenant isolation in the blueprint.
**Fix:** make the namespace and the bound principal unique per request (`f"{ctx.name}-{ctx.request_id[:6]}"`, as the
database and credential secrets already are), or refuse a request whose name is live for another owner; a test that
two requests with the same name never share a bound principal.

**H4. SSH and RDP can be opened to the internet.** `cloudinfra/models/request.py:360-363` defaults `allowed_sources`
to `["0.0.0.0/0"]`; `allowed_ports` is checked only for range and uniqueness (`security/validation.py:78-86`);
`network.py:41-42` writes `<name>-net-allow-public` from them, and `configure_firewall` (`network.py:70-72`) takes any
ports from any sources, including 22 from everywhere on a network whose own rule was IAP-only. The test at
`tests/cloudinfra/test_infra_gcp_provider.py:94` checks only the three initial rules. OS Login still gates login, but
the port is open and the document's claim is not met. Standard: CIS GCP 3.6 and 3.7 (Level 1). **Fix:** refuse 22
and 3389 (and an operator-extendable list) when any source is outside the private ranges and the IAP range, in
`NetworkRequest` and in `configure_firewall`; keep `0.0.0.0/0` possible for 80 and 443 with the existing warning;
a test for both paths.

### Medium

**M1. REST bodies and the sign-in form are read without a size limit** (`cloudinfra/mcp/server.py:219-262`,
`appdelivery/server.py:174-195`, `oauth.py:267`). The MCP endpoint and the SDK's OAuth routes are capped at 4 MiB by
`RequestBodyLimitMiddleware`; the services' own routes are not, and uvicorn imposes none. Any principal, read-only
ones included, can exhaust memory. Standard: OWASP API4, REST cheat sheet (413). **Fix:** wrap the app in the SDK's
`RequestBodyLimitMiddleware` with a small cap (64 KiB is generous for a request), `request.form(max_fields=2,
max_part_size=4096)` on the sign-in POST; a test that a larger `Content-Length` gets 413.

**M2. Unauthenticated `/register` writes unbounded rows that are never pruned** (`cloudinfra/security/oauth.py:99-101,
176-182`; the same in `appdelivery/oauth.py`). With sign-in on, `/register` is public, budgeted at 120 per minute per
address; the SDK's client metadata has no length caps; every accepted registration is stored whole in `oauth_clients`
in the state file and never expires. Standard: ASVS 10.4.7 (validate DCR metadata), MCP 2026-07-28 (DCR deprecated).
**Fix:** refuse metadata over a few KiB, `jwks`/`jwks_uri`, and any `token_endpoint_auth_method` but `none` (then
advertise `token_endpoint_auth_methods_supported: ["none"]`, which is also what Claude needs for CIMD); give
`/register` its own low per-address budget; prune clients with no live token after N days; add
`client_id_metadata_document_supported` and accept Claude's CIMD client ids so DCR is the fallback, not the path.

**M3. The OS Login SSH key never expires and lives on the data volume** (`appdelivery/remote.py:56-58`: no
`--ssh-key-expire-after`, no `--ssh-key-file`; `Dockerfile.appdelivery:56` `HOME=/data`). gcloud registers the key in
each environment account's OS Login profile without expiry; a copy of the volume (snapshot, backup) is a key that
OS Login accepts for ever as every environment's account. DELIVERY-SPEC.md section 4 says the key "is created per
session and expires with it", which is not what the code does. **Fix:** `--ssh-key-expire-after=15m` and a key file
on tmpfs; correct the spec.

**M4. The Kubernetes Deployment has no pod hardening and no limits** (`appdelivery/kube.py:112-116`): no
`securityContext` (runAsNonRoot, allowPrivilegeEscalation false, drop ALL, readOnlyRootFilesystem, seccomp
RuntimeDefault), no `resources`; the test at `tests/appdelivery/test_delivery_cluster.py:84` locks the absence in.
**M5. `docker run` on the host has no hardening flags** (`host/release.sh:102`): no `--cap-drop ALL`,
`--security-opt no-new-privileges`, `--read-only`, `--pids-limit`, `--memory`, `--log-opt max-size`; `-p PORT:PORT`
binds every interface. Standard: Pod Security "restricted", NSA/CISA Kubernetes guide, Docker CIS. **Fix:** add both
sets, bound from a per-environment default, bump `machine.SCRIPT_SHA256`, update the fakes and tests.

**M6. Tenant namespaces have no Pod Security admission, so `edit` reaches the node** (`cloudinfra/providers/gcp/
kubernetes.py:16-42`, `cluster.py:97-99`): `edit` can create privileged or `hostNetwork` pods, bypass the metadata
concealment and read the node account's token; NodePort Services are not refused. **Fix:** label `TENANT` with
`pod-security.kubernetes.io/enforce: baseline` (or `restricted`; `edit` cannot change namespace labels), extend the
CEL policy to `spec.type == 'NodePort'`.

**M7. GKE control plane and nodes.** `cluster.py:65-66` sets private nodes only on request and always a public
endpoint with no `masterAuthorizedNetworksConfig`; no `binaryAuthorization`, no NetworkPolicy/Dataplane V2, no
`securityPostureConfig`. Standard: CIS GKE 5.6.x, GKE hardening page, Binary Authorization docs. **Fix:** default
`private_nodes: true` with a Cloud NAT made by the network adapter (M10), authorized networks with the operator's
ranges, `binaryAuthorization.evaluationMode: PROJECT_SINGLETON_POLICY_ENFORCE` (a natural fit for the trusted-build
decision: only images from the trusted project), `datapathProvider: ADVANCED_DATAPATH` with network policy,
security posture BASIC.

**M8. The cost ceiling is bypassed by `resize_compute`** (`orchestration/provisioner.py:1271-1272` checks only the
per-machine CPU and memory limits; `planner.cost_problem` runs only at provision). Ten small machines under the
ceiling, each resized to the maximum. The documents list cost controls as a security boundary. **Fix:** estimate the
request as it will be and refuse above the caller's ceiling; a test.

**M9. No build budget per team** (`appdelivery/building.py:78-96`): the only brake is the generic 120 calls per
minute; builds may be `E2_HIGHCPU_32` for hours; Cloud Build's default pool holds 10–30 concurrent builds per
project, so one team queues every other team and the platform's own build. **Fix:** `max_concurrent_builds` per
environment and a daily minute budget per `infra_owner`, checked under the lock before `create`, refused with a code.

**M10. VPC: no flow logs, no firewall logging, all-protocol internal rule, no egress control, no NAT**
(`network.py:18-50`). Standard: CIS 3.8, blueprint detective controls. **Fix:** `logConfig` on the subnet (5-minute
interval, 50 percent sampling, all metadata) and on each rule; Cloud Router and NAT when nodes are private or no
machine has a public address; an operator switch for default-deny egress with Private Google Access allowed.

**M11. Public exposure is gated inconsistently.** Cloud Run `public` and the GKE front door need operator
permission (`provider.py:87-102`); a load balancer (global external address, `loadbalancer.py:26-74`) and
`network.public_access` with `0.0.0.0/0` need only `can_provision`; `compute.public_ip` on the operator's
`default_network` is unchecked (`validator.py:43-44`); the example config points `default_network` at Google's
`default`, which ships `default-allow-ssh` from everywhere (CIS 3.1). **Fix:** one operator setting consulted by the
validator for every public path (`public_ip`, `public_access`, `load_balancer`, `configure_firewall`); the example
config names an operator-made network and says why.

**M12. The load balancer is plain HTTP, no Cloud Armor, no SSL policy, no logging** (`loadbalancer.py:54-64`); the
connect note and the planner warn, so it is a documented gap. The GKE front door already does HTTPS with a managed
certificate and a redirect, the right shape to copy. **Fix:** with `public_domains`, a certificate, an HTTPS proxy
and a 301; an `sslPolicy` (TLS 1.2, MODERN); a default Cloud Armor policy; backend logging.

**M13. Cloud SQL flags and identity** (`database.py:24-35`): no CIS database flags (`log_connections`,
`log_disconnections`, `log_min_messages`, `cloudsql.enable_pgaudit`; MySQL `local_infile: off`), no
`cloudsql.iam_authentication`, no CMEK option; `rootPassword` is never set, so the state of MySQL's default user
must be checked on a real instance. **Fix:** a per-engine flags table in `translate.py`, IAM authentication on, an
optional KMS key from operator settings.

**M14. Compute: serial port, deletion protection, identity flow-through.** `compute.py:46-51` sets no
`serial-port-enable: FALSE` (CIS 4.5) and no `deletionProtection`; a machine with `compute.identity` gets that
account with `cloud-platform` scope (`compute.py:47-48`), so any `use`-level OS Login member reads its token and
inherits the identity's level, which may be `admin`; the architecture document still says machines get "no service
account". **Fix:** both metadata items; `deletionProtection: true` lifted only in delete, as the database does; cap an
identity attached to a machine at the lowest level of the machine's members or refuse `admin` identities on groups
with `use` members; narrow the scope; correct the document.

**M15. The node-account grant is project IAM, and the documents say "never project IAM."** `nodeaccount.py:38-68`
read-modify-writes the project policy to grant `container.defaultNodeServiceAccount`; SETUP.md asks for
`resourcemanager.projectIamAdmin` with a condition. The code comment is honest; `iam.py:1-3` and the architecture
table are not. **Fix:** either one operator-made node account reused by every cluster, or state the conditioned role
as a hard prerequisite and check it at start; correct the document.

**M16. Repository settings and locks.** Dependabot alerts and security updates are disabled on the repository
(`gh api …/vulnerability-alerts` → 404); branch protection requires a pull request but zero approving reviews,
no code-owner review, `strict: false`, no signed commits, while CODEOWNERS says every boundary change is reviewed;
`audit.lock` (regenerated 2026-10-20) installs `filelock 4.1.1` with seventeen open advisories (one High, all fixed
in 4.2.1) and is the one lock CI never audits; the Python base image digest is of 2026-10-11 and nothing refreshes
it (Docker Hub moved on 2026-10-28). Standard: Scorecard Branch-Protection, Code-Review, Dependency-Update-Tool,
Vulnerabilities; OWASP A03 "no single person promotes to production". **Fix:** enable Dependabot alerts; add a
`docker` ecosystem to `dependabot.yml`; regenerate `audit.lock` and audit it in CI; require one review and code-owner
review (with a documented admin bypass while there is one maintainer), `strict: true`, a ruleset for signed commits;
record the actual settings in DEVELOPMENT.md.

**M17. The platform's own images get weaker guarantees than the teams' images.** `cloudbuild.yaml` produces
provenance and nothing else: no vulnerability scan gate, no SBOM, no Cosign signature, so `require_attestation`
cannot gate them; `cloudbuild.yaml` has no `serviceAccount:` field, so a submit without the flag runs as the default
build account (`test_platform_build.py` checks `CLOUD_LOGGING_ONLY` as a proxy); REQUIREMENTS.md lists only five
organisation policies and no Data Access logs, sinks, uptime checks or alerts for the services themselves. Standard:
OWASP A03, Scorecard SBOM and Signed-Releases, CISA bad practices, blueprint. **Fix:** `serviceAccount:` in
`cloudbuild.yaml` and in its test; `containerscanning.googleapis.com` required in SETUP.md with a scan result recorded
before "run it by digest"; a `cosign sign` step keyless as `platform-build@` plus an SBOM step; the organisation
policies and logging of section 7, S3.

### Low

- **L1.** Single-use of sign-in links, codes and refresh tokens is a SELECT then a DELETE/UPDATE on separate
  connections (`oauth.py:114-120, 136-142`): two concurrent `/token` calls can both succeed and reuse detection can
  miss. Use `DELETE … RETURNING` and `UPDATE … WHERE revoked=0` with a rowcount check.
- **L2.** State file, its lock, the OAuth tables and the log file are created with the process umask, never 0600
  (no `chmod`/`umask` in either package); they hold the inventory, client addresses, token hashes and PKCE
  challenges. `os.umask(0o077)` at start or `chmod` after creation; a test for the mode.
- **L3.** A confidential client's `client_secret` is stored in clear in `oauth_clients` (`oauth.py:101`), against the
  module's "only hashes" sentence; refusing any auth method but `none` (M2) removes it.
- **L4.** Every per-address budget stands or falls with `FORWARDED_ALLOW_IPS`, which only the Dockerfile comments
  mention; behind a proxy without it every caller is one address and one client can exhaust the shared public budget
  for everyone; with `*` the header is caller-controlled and the audit `client` column is poisoned. A "Behind a proxy"
  paragraph in SETUP.md and both example files; a per-path public budget.
- **L5.** The sign-in capability travels as `?login=` in a URL (`oauth.py:188`): browser history and proxy logs;
  `no-referrer` is set. Shorten `LOGIN_SECONDS`, strip the parameter from access logs, or carry it in a `__Host-`
  cookie set on the 302.
- **L6.** Redaction is by key name only (`observability/logging.py:18-29`); `internal_error` logs `str(exc)[:300]`
  and provider messages reach `details`. Add a value-level pass (`ya29.`, `Bearer `, `-----BEGIN`, long base64 runs)
  and log only the exception class at INFO.
- **L7.** Principal token hashes are plain SHA-256; adequate for 256-bit random tokens, but NIST 800-63B-4 asks for a
  keyed step with a verifier-only key kept apart from the store. An optional pepper from the environment (HMAC-SHA-256)
  with a migration path is cheap insurance against a copied config plus state file.
- **L8.** `GcpApi.request` turns a read timeout on a create into `PROVIDER_UNAVAILABLE` without a retry or a lookup
  (`api.py:101-104`); every adapter records the resource only after `post` returns, so a create Google accepted but
  did not answer in time is an orphan the cleanup cannot see (machines, disks, caches, clusters, services). GET the
  planned name once before raising, or a labelled-orphan sweep.
- **L9.** Cloud Run `ingress` defaults to `all` (`request.py:273`), no `binaryAuthorization`, and the runtime account
  is granted `secretAccessor` on every secret of the request (`run.py:52-68`), credentials included. Default
  `internal_and_load_balancer`, `useDefault` Binary Authorization, name the secrets a service needs.
- **L10.** GCS: no versioning or soft-delete statement, retention, access logging or CMEK (`storage.py:30-31`);
  Secret Manager: automatic replication, no CMEK or rotation, `getIamPolicy` without
  `requestedPolicyVersion=3` (`iam.py:93`); Artifact Registry: no cleanup policy and no note to enable scanning.
- **L11.** `get_run`'s `execution` name reaches a Cloud Run URL with only a prefix check (`appdelivery/releases.py:
  449-453`); regex it. Values from `cloudinfra`'s answer (machine name, zone, cluster endpoint, CA) reach gcloud's
  argv and a TLS endpoint unvalidated (`infra.py:45-82`, `remote.py:57`); `cloudinfra.url` accepts `http://`.
  Validators on each, `https` required unless localhost.
- **L12.** Provenance pins one key version with no rotation drill; `buildType` and `buildConfig` are not checked
  (see H2). Cosign inherits the whole process environment, the `cloudinfra` tokens included (`attest.py:34`); pass an
  allow-list. Cosign v3.1.3 is pinned; v3.1.4 (2026-10-20) carries verify fixes, and the binary's own Sigstore
  signature is not verified at build.
- **L13.** `get_running(live=True)` runs the host `status` action without the "before every action" re-check
  (`releases.py:573-597`); route it through `_prepare` or amend the spec.
- **L14.** `appdelivery/auth.py` has drifted from `cloudinfra/security/auth.py` (rate-limit code `PERMISSION_DENIED`
  versus `LIMIT_EXCEEDED`, `failed()`, internal-error logging); a test that diffs the two modulo names, or one shared
  module when the "later" arrives.
- **L15.** The apt packages in `Dockerfile.appdelivery:36` are unpinned and hadolint runs at `error` only; no
  `HEALTHCHECK` in either image; `tar`, `gzip` and `apt-get` remain in the delivery image; `--read-only --tmpfs /tmp
  --cap-drop ALL --security-opt no-new-privileges` is feasible for both images and documented nowhere; the base
  image's pip is 25.x with five 2026 advisories fixed in 26.x, so `pip install --require-hashes pip==26.2.2` before
  the lock.
- **L16.** SECURITY.md has no acknowledgement or fix time frames, no second contact, no scope list; no signed tags, no
  GitHub Releases, the one tag is `v1.5.0` against version 1.37.1, so a deployed digest cannot be mapped to a tag
  without Cloud Build; CHANGES.md has no "Security" convention. REQUIREMENTS.md still lists `setup-python v7.0.0`
  after Dependabot moved CI to v7.1.0.
- **L17.** `output/` (3.9 MB of presentations) is untracked and not ignored; `.dockerignore` does not exclude
  `infra/` (three `terraform.tfstate` and `*.tfvars` with project ids, a billing account and an alert email),
  `output/` or `.claude/`; the stale `infra/` tree belongs to the retired workflow. Real sandbox identifiers (project
  id and number, build id, an OS Login SA id) are in the provenance fixture and DELIVERY-SPIKE.md of a public
  repository; not secrets, but worth stating as deliberate or retiring the sandbox project.
- **L18.** CI has no CodeQL, no `ossf/scorecard-action`, no `dependency-review-action`, no image scan; tests have
  no sweep of tracked files for credential patterns, no pin on the dependency set, and no property-based tests for
  `security/validation.py`, `looks_like_credential` and `host_allowed`.
- **L19.** The service account that runs `cloudinfra` holds `roles/storage.admin` and `roles/secretmanager.admin`
  (SETUP.md:45-46), which include reading objects and secret payloads it never needs; the Cloud Build service agent
  gets `secretmanager.admin` unconditioned. Custom roles without the data permissions; a condition on the agent's.
- **L20.** RFC 8707 `resource` is stored but never checked (`oauth.py:236, 167-170`); not exploitable, since tokens
  are opaque and local, but a `resource == self.resource` check at `/authorize` fails a mis-pointed client early and
  matches the specification's MUST on audience. `iss` (RFC 9207) is not returned on authorization responses; the SDK
  may add it, otherwise it is a future MUST.

### Info

- The gate guards only `http` scopes (`auth.py:67`); websocket scopes pass through, there are none today.
- `get_service_status` returns project id, regions, limits, domains and registry-writer accounts to any principal;
  `/healthz` returns the version unauthenticated (ASVS 13.4.6 at L3 asks for no version disclosure).
- Audit events are plain rows in the same file as everything else; not tamper-evident. The JSON log lines already
  emitted are the thing to ship to an append-only sink.
- Approval separation is by principal name only; two principals held by one person satisfy it. The example config's
  approver has no `can_deploy`, which is the right shape to require.
- Documentation drift to correct alongside the fixes: "no service account" on machines (M14), "never project IAM"
  (M15), the SSH key "expires with the session" (M3), the token "valid for 60 minutes" (H1), CODEOWNERS' review claim
  (M16), REQUIREMENTS.md's `setup-python` row (L16).

## 5. Where the project stands against each framework

| Framework | Holds | Short of |
|---|---|---|
| MCP 2026-07-28 authorization | OAuth 2.1, RFC 9728, RFC 8414, Origin check, header-only tokens, rotation | audience check on `resource` (L20), `iss` (L20), CIMD support with `none` advertised (M2), DCR hardening (M2) |
| MCP 2026-07-28 transport | stateless, no sessions, DNS-rebinding protection on, 421/403 | nothing; the SDK's move to the new headers is the SDK's |
| Anthropic connector review | titles, annotations, read/write split, 401 with `resource_metadata`, Claude callbacks only | CIMD (M2); an egress note for `160.79.104.0/21` (L4) |
| OAuth 2.1 / RFC 9700 / ASVS V10 | PKCE S256, exact redirect, single-use codes, rotation, 300 s codes | atomic single use (L1), DCR metadata validation (M2), body limits on `/sign-in` (M1) |
| ASVS 5.0 V4, V13, V16 | strict models, structured errors, JSON logs, no docs endpoints | size limits (M1), proxy guidance (L4), value-level redaction (L6), file modes (L2), version banner (Info) |
| OWASP API Top 10 2023 | BOLA and BFLA covered on every route | API4 resource consumption (M1, M2, M9) |
| OWASP Top 10:2025 A03 / SLSA v1.2 / Scorecard | hash locks, pinned actions, L3 provenance verified in code, digest-only releases | `buildConfig`/`externalParameters` check (H2, L12), SBOM and signature of platform images (M17), review requirement and Dependabot alerts (M16), CodeQL/Scorecard (L18) |
| CIS GCP v5 §3, §4, §5, §6 | Shielded VM, OS Login, no public IP by default, private subnets, IAP-only SSH rule, Cloud SQL private and TLS, buckets locked down | 3.6/3.7 via `allowed_ports` (H4), 3.8 flow logs (M10), 4.5 serial port and deletion protection (M14), 6.x flags (M13), 3.1 default network in the example (M11) |
| CIS GKE 1.9 / GKE hardening | shielded nodes, workload identity, release channel, scoped node account, namespace RBAC, admission policy | Pod Security (M6), private endpoint and authorized networks, Binary Authorization, NetworkPolicy (M7), workload-identity uniqueness (H3) |
| Google SA best practices | no keys ever, host identity, 15-minute impersonation, per-environment accounts | the token handed to the machine (H1), the never-expiring OS Login key (M3), Data Access logs for IAM/STS (M17) |
| OWASP LLM 2025 / Agentic 2026 | server-side authorization on every call, no passthrough, no instructions in descriptions, destructive tools annotated | per-agent identity is per-principal, which is the right unit here; nothing further required |
| NIST 800-63B-4 | 256-bit CSPRNG tokens, hashed, constant-time | keyed hash with a verifier-only key (L7) |
| EU CRA / PQC | a VDP exists; one signature algorithm (ECDSA P-256 for provenance, KMS for attestations) | time frames in SECURITY.md (L16); a crypto-agility note and ML-DSA attestor keys when Binary Authorization accepts them |

## 6. What the documents say that the code does not do

1. INFRASTRUCTURE-ARCHITECTURE.md:379 "machines get … no service account": true unless `compute.identity` (M14).
2. INFRASTRUCTURE-ARCHITECTURE.md:378, `iam.py:1-3` "never on the project": the node-account role is project IAM
   (M15).
3. INFRASTRUCTURE-ARCHITECTURE.md:379 "SSH only from the identity-aware proxy range": only for the network block's
   own rule (H4).
4. DELIVERY-SPEC.md section 4: the key "expires with the session" (M3); the token "valid for 60 minutes" (H1,
   it is 15); the machine learns only env values (it learns the environment's token, H1).
5. SECURITY.md "codes and tokens kept as hashes": a confidential client's secret is kept in clear (L3).
6. CODEOWNERS "every change to the services' trust boundaries is reviewed": nothing enforces it (M16).
7. DELIVERY-SPEC.md section 2 "before every action": `get_running(live=True)` skips the re-check (L13).

## 7. The plan

Three releases, each shippable on its own, each item with the finding it closes, the files and the test that holds
it. Effort: S under half a day, M one to two days, L three days or more. The order inside a release is the order to
do them.

### S1. Close the four High findings and the cheap Mediums (one release, about a week)

| # | Change | Closes | Files | Test |
|---|---|---|---|---|
| 1 | Anchor `dockerfile` and `context` as `./…`; refuse URL-looking prefixes in `SOURCE_PATH`; compare provenance `externalParameters.buildConfig` with the composed body | H2, L12 | `appdelivery/cloudbuild.py`, `models.py`, `provenance.py` | `github.com/x/y` refused; composed args anchored; a statement whose buildConfig differs fails (S) |
| 2 | A pull identity per environment (`environments.*.pull_service_account`, reader on `allowed_registries` only), minted fresh per deploy with the shortest lifetime; the environment token never leaves the service; spec section 4 corrected | H1 | `appdelivery/config.py`, `releases.py`, `google.py`, `machine.py`, SETUP.md, DELIVERY-SPEC.md | the host's `IN_TOKEN` is the pull account's and never the environment's (M) |
| 3 | Namespace and bound principal unique per request | H3 | `cloudinfra/providers/gcp/identity.py`, `cluster.py`, `kubernetes.py` | two requests of one name never share a principal (S) |
| 4 | Refuse 22/3389 from non-private, non-IAP sources in `NetworkRequest` and `configure_firewall`; operator-extendable list | H4 | `cloudinfra/security/validation.py`, `models/request.py`, `orchestration/provisioner.py` | both paths refused; 80/443 still allowed (S) |
| 5 | `RequestBodyLimitMiddleware` on both apps; form limits on `/sign-in` | M1 | `cloudinfra/mcp/server.py`, `appdelivery/server.py`, both `oauth.py` | 413 above the cap (S) |
| 6 | DCR hardening: size cap, `none` only, own budget, pruning; advertise `token_endpoint_auth_methods_supported: ["none"]`; accept Claude's CIMD client ids | M2, L3 | both `oauth.py`, `config.py` | oversized or confidential registration refused; CIMD client accepted (M) |
| 7 | `--ssh-key-expire-after=15m`, key on tmpfs | M3 | `appdelivery/remote.py`, `Dockerfile.appdelivery` | argv carries the flag (S) |
| 8 | `docker run` and Deployment hardening with bounded resources; bump `SCRIPT_SHA256` | M4, M5 | `appdelivery/host/release.sh`, `machine.py`, `kube.py`, the fakes | flags present; securityContext restricted (M) |
| 9 | Resize checked against the ceiling | M8 | `cloudinfra/orchestration/provisioner.py` | a resize above the ceiling refused (S) |
| 10 | Build budget per team and environment | M9 | `appdelivery/config.py`, `building.py`, `state.py` | the N+1th concurrent build refused with a code (M) |
| 11 | Repository: enable Dependabot alerts, `docker` ecosystem in `dependabot.yml`, regenerate and audit `audit.lock`, require one review and code-owner review, `strict: true`, signed-commit ruleset; record in DEVELOPMENT.md | M16 | `.github/`, `audit.lock`, CI | CI audits `audit.lock` (S; the GitHub settings are the maintainer's clicks) |
| 12 | `serviceAccount:` in `cloudbuild.yaml` and its test; `pip==26.2.2` before the lock in both images; `HEALTHCHECK` | M17 (part), L15 | `cloudbuild.yaml`, both Dockerfiles, `test_platform_build.py` | the test requires the field (S) |
| 13 | Atomic single use (`RETURNING`, rowcount); file modes 0600; umask at start | L1, L2 | both `oauth.py`, `state_manager.py`, `state.py` | concurrent exchange yields one token; mode asserted (S) |
| 14 | The seven document corrections of section 6 | Info | docs | the catalog tests already fail on stale generated docs; the rest by review |

Acceptance: the four High findings have a failing test before and a passing test after; `ruff`, `pytest`, both
catalogs regenerated; a real run on the sandbox project for items 2, 7 and 8 recorded in DELIVERY-SPIKE.md.

### S2. The provider defaults the benchmarks expect (the next release)

| # | Change | Closes | Effort |
|---|---|---|---|
| 1 | Pod Security `baseline` on tenant namespaces; NodePort refused | M6 | S |
| 2 | GKE: private nodes by default with NAT, authorized networks, Binary Authorization enforce, Dataplane V2 with network policy, security posture | M7, M10 | L |
| 3 | Subnet flow logs, firewall rule logging, optional default-deny egress | M10 | M |
| 4 | One `allow_public_exposure` setting over every public path; example config on an operator-made network | M11 | M |
| 5 | Load balancer: HTTPS with a managed certificate and redirect, SSL policy, Cloud Armor, logging | M12 | L |
| 6 | Cloud SQL flags per engine, IAM authentication, optional CMEK; verify MySQL's default user on a real instance | M13 | M |
| 7 | `serial-port-enable: FALSE`, `deletionProtection`, identity level capped and scope narrowed | M14 | S |
| 8 | Node account: one operator-made account, or a startup check for the conditioned role | M15 | M |
| 9 | Cloud Run `ingress` default internal, Binary Authorization `useDefault`, named secrets only | L9 | S |
| 10 | GCS versioning and logging, Secret Manager policy version 3 and CMEK option, registry cleanup policy and a scanning note | L10 | S |
| 11 | Validators on every value from `cloudinfra`'s answer and on `execution`; `https` required for `cloudinfra.url` | L11 | S |
| 12 | Create timeouts: look the planned name up before raising | L8 | M |
| 13 | Optional HMAC pepper for principal tokens with a migration | L7 | S |
| 14 | Value-level redaction; exception class only at INFO | L6 | S |
| 15 | Per-path public budgets; the "Behind a proxy" paragraph; `resource` and `iss` checks | L4, L20 | S |
| 16 | A shared-gate drift test; route `get_running` through `_prepare` | L13, L14 | S |

### S3. Operations and governance (outside the code, with the operator)

1. Organisation policies on the project, added to REQUIREMENTS.md section 6 and SETUP.md section 2:
   `iam.disableServiceAccountKeyCreation`, `iam.disableServiceAccountKeyUpload`,
   `iam.automaticIamGrantsForDefaultServiceAccounts`, `iam.allowedPolicyMemberDomains`, `compute.requireShieldedVm`,
   `compute.disableSerialPortAccess`, `compute.skipDefaultNetworkCreation`, `storage.publicAccessPrevention`,
   `storage.uniformBucketLevelAccess`, `sql.restrictAuthorizedNetworks`, alongside the five already listed.
2. Data Access audit logs for IAM, STS (impersonation), Secret Manager, Cloud SQL and Cloud Storage; an
   organisation sink to a locked bucket; Security Command Center; an uptime check on each service's `/healthz` and
   alert policies on `auth_burst`, `token_reuse` and `internal_error` events; the JSON log lines shipped to Cloud
   Logging as the append-only audit record.
3. Artifact Registry scanning (`containerscanning.googleapis.com`) on, a vulnerability gate before a platform image is
   run; an SBOM and a keyless Cosign signature of the platform images from `cloudbuild.yaml`, so
   `require_attestation` can gate them too; Binary Authorization on the GKE and Cloud Run targets once S2 lands.
4. Least privilege for the service identity: custom roles in place of `storage.admin` and `secretmanager.admin`; a
   condition on the Cloud Build agent's secret role; the IAM recommender pass the documents promise.
5. CI additions: CodeQL (`security-extended`), `ossf/scorecard-action`, `dependency-review-action`, an image scan of
   both Dockerfiles with a pinned, checksummed scanner; a credential sweep of tracked files and a pin on the dependency
   set as tests; `hypothesis` for the validators.
6. Repository hygiene: `output/`, `.claude/` ignored; `infra/`, `output/`, `.claude/` in `.dockerignore`; the stale
   `infra/` tree removed; signed tags `vX.Y.Z` and a GitHub Release naming the two image digests per release; a
   "Security" convention in CHANGES.md; SECURITY.md with acknowledgement and fix time frames, a second contact and a
   scope list; the sandbox identifiers in the public repository stated as deliberate.
7. Horizon items, recorded rather than built: a crypto-agility note (ECDSA P-256 provenance key, KMS attestor key,
   TLS) with NIST IR 8547's dates and ML-DSA attestor keys when Binary Authorization accepts them; CRA reporting
   duties if the product is ever placed on the EU market; CIMD as the primary client registration once Claude's
   client advertises it, with DCR kept as the fallback.

### What this review did not do

It did not run anything against a real Google project (H1, H3 and M13 deserve a real run before and after the fix),
did not fuzz the parsers, did not read CIS GCP v5.0.0's own text (members-only), and did not review the earlier
research documents for things they promised and this code never built; section 5's table is from the code as it is.

## 8. Sources

Primary (F):
MCP specification 2026-07-28, authorization, security considerations, client registration, streamable HTTP, security
best practices, tools and changelog, https://modelcontextprotocol.io/specification/2026-07-28/ ·
Anthropic, building connectors, authentication, review criteria, testing, enterprise managed auth,
https://claude.com/docs/connectors/building · MCP Python SDK advisories on OSV and GitHub (GHSA-w4fh-qvv9-3v23,
GHSA-5h93-6whr-6q8j, GHSA-rwrf-2pqf-9j8j, GHSA-84m7-p3x7-pcfv, GHSA-jpw9-pfvf-9f58, GHSA-vj7q-gjh5-988w,
GHSA-9h52-p55h-vw2f) · OAuth 2.1 draft-ietf-oauth-v2-1-16, https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/ ·
RFC 9700, https://www.rfc-editor.org/rfc/rfc9700.html · RFC 9728, RFC 8707, RFC 9207, RFC 9449 ·
OWASP ASVS 5.0.0, https://github.com/OWASP/ASVS/tree/master/5.0/en · OWASP Top 10:2025, https://top10.owasp.org/2025 ·
OWASP API Security Top 10 2023, https://api-security.owasp.org/editions/2023/en/0x11-t10 · OWASP REST Security and
Password Storage cheat sheets, https://cheatsheetseries.owasp.org/ · OWASP Top 10 for LLM Applications 2025 and Top 10
for Agentic Applications 2026, https://genai.owasp.org/ · SLSA v1.2 requirements, build requirements, verifying
artifacts, https://slsa.dev/spec/v1.2/ · Cloud Build provenance, https://docs.cloud.google.com/build/docs/securing-builds/view-build-provenance ·
NIST SP 800-218 and the Rev. 1 draft, https://csrc.nist.gov/projects/ssdf · OpenSSF Scorecard checks,
https://github.com/ossf/scorecard/blob/main/docs/checks.md · GitHub Actions security hardening and workflow dependency
lockfiles, https://docs.github.com/en/actions/security-for-github-actions/ · zizmor audits, https://docs.zizmor.sh/audits/ ·
PEP 751 and pip advisories on OSV · Cosign and sigstore-go advisories on OSV (CVE-2026-22703, -24122, -39395,
-49834, -54787) · Google distroless, https://github.com/GoogleContainerTools/distroless · Artifact Analysis container
scanning and Binary Authorization overview, https://docs.cloud.google.com/ · Google enterprise foundations blueprint,
preventative and detective controls, https://docs.cloud.google.com/architecture/security-foundations/ · Secure-by-
default organisations, https://docs.cloud.google.com/resource-manager/docs/secure-by-default-organizations · Service
account best practices, IAM Credentials `generateAccessToken`, IAP TCP forwarding, Cloud Build service account,
Secret Manager best practices, GKE hardening (2026-10-26), Cloud Run security, https://docs.cloud.google.com/ ·
uvicorn deployment, https://uvicorn.dev/deployment/ · h11, Starlette, python-multipart, PyJWT, cryptography, anyio
advisories on OSV · NIST SP 800-63-4 and 63B-4, https://pages.nist.gov/800-63-4/ · NIST IR 8547 (2026-10-27) and the
Google Cloud PQC roadmap · EU CRA reporting, https://digital-strategy.ec.europa.eu/en/policies/cra-reporting ·
CISA Product Security Bad Practices v2.0 · Backstage threat model, HashiCorp Boundary and Vault documentation,
Microsoft Entra Agent ID and Google agent identity pages · arXiv 2504.03767, 2504.08623, 2512.08290, 2603.18063,
2601.17549, 2604.07551, 2510.16558, 2509.24272; Invariant Labs on tool poisoning.

Secondary (S): CIS Google Cloud Platform Foundations Benchmark v5.0.0 and CIS GKE 1.9.0 (listing pages; the PDFs are
members-only), CISA "Careful adoption of agentic AI services" (2026-05-01, via CSA's note), CNSA 2.0 dates, ISO/IEC
27001:2022 and SOC 2 control summaries, the Cycode summary of the OWASP Agentic Top 10 categories.
