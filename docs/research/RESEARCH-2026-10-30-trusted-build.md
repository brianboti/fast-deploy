# Building images only inside the trusted Google Cloud project: what the documentation allows. 2026-10-30

Written 2026-10-30 UTC as a read-only review, before any code is changed. The requirement, from the operator: no
developer, GitHub Actions job, local machine or other outside system may build a container image and upload it into
the trusted Google Cloud project. Application teams may only **ask** for a build; the trusted project fetches the source
and builds the image itself. Preparing an image outside and pushing it in is forbidden.

This note records what Google's documentation says the pieces are, which of our current decisions the requirement
reverses, and the design the next change should follow. Each source is marked F (read on the page) or S (a search
snippet or a secondary source). Section 8 lists them.

## 1. The short answer

**Cloud Build can do exactly this, and the pieces fit the two services as they are.** A build is one HTTP request to
Cloud Build's API with the source given as a *connected repository and a revision*, the steps we compose, the image name
to push, and a service account of our choosing (F, section 3). Cloud Build fetches the source itself through a
repository connection whose GitHub token lives in Secret Manager in our project (F, section 4). The pushed image gets
SLSA level 3 provenance, signed, that says which builder, which source and which build produced it, when the build asks
for `requestedVerifyOption: VERIFIED` and pushes through the `images` field (F, section 5). So the delivery service can
accept a *build request* (repository, commit, Dockerfile), order the build, record the digest, and later refuse any
release whose digest it did not build.

Three decisions of the last two days reverse:

1. **The registry's `admin` level** (`roles/artifactregistry.writer` for a team's CI, commit 5cd7cc5) goes. The only
   writer on a team's repository becomes the build service account. Writer is enough to push and tag and cannot delete
   (F, section 6).
2. **A CI job's federated identity** (`principal://`, `principalSet://` in a request's access list) loses its purpose,
   since nothing outside pushes. It is removed, not kept as reader: a reader outside the project is a copy path, not a
   build path, and the requirement is about building, but keeping a feature nobody may use invites its misuse.
3. **The band diagram** "Application teams: code · CI · build · image in a registry" becomes "code, and a build
   request"; the build is the platform's.

Two things the documentation does not settle, so the first real run must (section 7): whether `slsa-verifier` accepts
the provenance of a build started by the API from a connected repository (its documentation describes trigger builds),
and whether BuildKit is available to a `docker build` step (the spike of 2026-10-29 found it was not).

## 2. What the requirement means for the three bands

Today ([DELIVERY-SPEC.md](../DELIVERY-SPEC.md) section 1): teams own code, CI, build and the image; `appdelivery` runs an
image that already exists; `cloudinfra` provisions. The requirement moves the build into the platform:

```
Application teams      code · a build request (repository, commit, Dockerfile) · a release request (digest)
        │
appdelivery            order the build in Cloud Build · record the digest · verify provenance · run · health · roll back
        │
cloudinfra             machines · networks · databases · secrets · clusters · registries (one writer: the build account)
```

The build request and the release request stay two calls. A build produces a digest; a release names it. Keeping them
apart keeps the release contract exactly as it is (an image by digest, no field that could run anything) and lets a
digest be built once and released to dev, then prod, which is what every source behind the delivery decision wanted
("built and packaged once"; [RESEARCH-2026-10-29-delivery-layer.md](RESEARCH-2026-10-29-delivery-layer.md) section 2).

`appdelivery` still builds nothing itself: it sends one HTTP request and reads the result. Cloud Build builds. The
boundary tests change wording, not substance: the release contract keeps its forbidden-word test; the build contract gets
one of its own that allows `repository`, `revision` and `dockerfile` and forbids `steps`, `command`, `script`,
`entrypoint`, `args`, `env`, `substitutions` and `serviceAccount`, since those are the service's to compose, never the
caller's.

## 3. The build request to Cloud Build (F)

**Endpoint.** `POST https://cloudbuild.googleapis.com/v1/projects/PROJECT/locations/REGION/builds`, permission
`cloudbuild.builds.create`, scope `cloud-platform`; the answer is a long-running Operation that carries the build id;
`builds.get` reads the status later. The legacy path `projects/PROJECT/builds` builds only in `global`, and a 2nd-gen
repository source is not supported in `global`, so builds are regional. `us-central1` is a supported region.

**Body** (the `Build` resource; fields we will set):

| Field | What we send | Why |
|---|---|---|
| `source.connectedRepository.repository` | `projects/P/locations/R/connections/C/repositories/NAME` | the source, fetched by Cloud Build through the connection |
| `source.connectedRepository.revision` | a full commit SHA | required; "a branch, tag, commit SHA, or any Git ref" is accepted, we accept only a 40-hex SHA so the digest maps to one commit |
| `source.connectedRepository.dir` | the build context, relative | optional; a subdirectory of a monorepo |
| `steps[0]` | `name: gcr.io/cloud-builders/docker`, `args: [build, -t, IMAGE, -f, DOCKERFILE, --build-arg, TARGETARCH=amd64, CONTEXT]` | the one step; composed by us, never by the caller |
| `images` | `[LOCATION-docker.pkg.dev/P/REPO/NAME:TAG]` | pushed by Cloud Build after the steps with the build account's credentials; the digest lands in `results.images[].digest`; provenance is generated only for images pushed this way |
| `serviceAccount` | `projects/P/serviceAccounts/EMAIL` | the environment's build account (section 6); "the legacy Cloud Build service account isn't allowed" |
| `options.logging` | `CLOUD_LOGGING_ONLY` | a user-specified account "can not store your logs in a Google Cloud-owned log bucket" |
| `options.requestedVerifyOption` | `VERIFIED` | provenance is generated and "builds will only be marked successful if provenance is generated" |
| `options.machineType` | `E2_MEDIUM` or `E2_STANDARD_2` (default) | section 7 for prices; `E2_HIGHCPU_8`, `E2_HIGHCPU_32` exist |
| `options.substitutionOption` | not set; no `substitutions` | the caller's values never enter a shell |
| `timeout` | a duration string, e.g. `1200s` | default 60 minutes, maximum 24 hours; `TIMEOUT` status when exceeded |
| `tags` | `appdelivery`, the team, the environment | up to 64; for filtering in the console |

**Status values**: `PENDING`, `QUEUED`, `WORKING`, `SUCCESS`, `FAILURE`, `INTERNAL_ERROR`, `TIMEOUT`, `CANCELLED`,
`EXPIRED`. If any image push fails the status is `FAILURE`. Limits that matter: 300 steps, 100 `args` per step,
10,000 characters per arg, 700 images per build. Concurrency on the default pool is 10 to 30 builds per project,
global, not raisable; beyond that Google points at private pools (section 7).

**Reading the result.** Poll `builds.get` from `appdelivery`'s background worker, as the lifecycle poller in
`cloudinfra` does. Cloud Build can also publish to a Pub/Sub topic `cloud-builds` (attributes `buildId`, `status`,
body the Build JSON), but the topic must be created by us and adds a subscription to run; polling is enough for a
build that takes minutes.

**Build approval** (`PENDING` until a `roles/cloudbuild.builds.approver` approves) is documented for triggers only, so
we do not rely on it; `appdelivery`'s own `require_approval` already gates the release, and a build request can carry
the same gate if an environment wants it.

## 4. How the source reaches the project: repository connections (F)

- Cloud Build repositories **2nd gen** supersede 1st gen. A *connection* to a host (GitHub, GitHub Enterprise, GitLab,
  GitLab EE, Bitbucket Data Center and Cloud) is created in a region; *repositories* are linked under it. Cloud Source
  Repositories is not supported and is closed to new customers since 2024-06-17.
- GitHub: `gcloud builds connections create github CONNECTION --region=REGION` prints a link to authorize the Cloud Build
  GitHub App; the app is installed on the organization with repository permissions chosen then;
  `gcloud builds connections describe` must show `installationState: COMPLETE`; then
  `gcloud builds repositories create NAME --remote-uri=https://github.com/ORG/REPO.git --connection=CONNECTION --region=REGION`.
- **Where the credential lives**: "authorization creates a token stored as a secret in Secret Manager in your project";
  the Cloud Build service agent `service-PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com` reads it with
  `roles/secretmanager.secretAccessor`. **Corrected by the run** ([DELIVERY-SPIKE.md](../DELIVERY-SPIKE.md) section 14):
  creating the connection needs the service agent to hold `roles/secretmanager.admin`, since it creates the secret
  and sets its policy. A classic personal access token (`repo`, `read:user`, `read:org`; fine-grained
  tokens are not supported) can stand in for the app flow, with `--authorizer-token-secret-version` and
  `--app-installation-id`. So the GitHub credential is the platform's, in the trusted project, and no team or CI job
  holds one for this purpose.
- The repository resource is `projects/P/locations/R/connections/C/repositories/NAME`; the v2 API has `create`, `get`,
  `list`, `delete`, `batchCreate`, `fetchGitRefs`, `accessReadToken`, `accessReadWriteToken`.
- Roles: `roles/cloudbuild.connectionAdmin` manages connections and repositories; `roles/cloudbuild.connectionViewer`
  reads them. Linking a repository is the **platform team's act, once per repository**; the build request then names a
  repository the operator has listed for that team, never an arbitrary URL. (`gitSource.url` exists in the API and would
  take any public URL; we do not expose it.)
- Developer Connect is the newer alternative with the same hosts, usable as `source.developerConnectConfig`; the
  repositories page says it does not support manual builds, so we start with Cloud Build connections.

Branch or tag names are resolvable with `fetchGitRefs` (`refType` `BRANCH` or `TAG`), which lets the service offer "the
head of main" as a convenience while still recording and building a commit SHA.

## 5. Provenance: proving the image was built here (F)

- Cloud Build generates provenance for images pushed through the `images` field; it "can't be generated if you use an
  explicit `docker push` step". With `requestedVerifyOption: VERIFIED` the build fails if provenance is not produced.
  It meets SLSA level 3 and is stored in both SLSA v0.1 and v1.0 forms (`predicateType`
  `https://slsa.dev/provenance/v1`, `buildType` `https://cloud.google.com/build/gcb-buildtypes/google-worker/v1`).
- It is stored as an Artifact Analysis occurrence of kind `BUILD` attached to the image (note name like
  `projects/P/notes/intoto_slsa_v1_BUILD_ID`), deleted with the image, exempt from cleanup policies. Reading it:
  `gcloud artifacts docker images describe IMAGE@sha256:… --show-provenance --format json`, or
  `GET https://containeranalysis.googleapis.com/v1/projects/P/occurrences?filter=…` with
  `containeranalysis.occurrences.list` (`roles/containeranalysis.occurrences.viewer`). Requires the Cloud Build,
  Artifact Registry and Container Analysis APIs.
- It is signed: a DSSE envelope; for v0.1 two signatures, `provenanceSigner` (DSSE-conformant, the one to use) and
  `builtByGCB` (legacy); the public key is under Google's `verified-builder` project, keyring `attestor`
  (`gcloud kms keys versions get-public-key 1 --location global --keyring attestor --key builtByGCB --project verified-builder`).
- **Verifying it**: `slsa-verifier verify-image IMAGE@DIGEST --provenance-path provenance.json
  --source-uri github.com/ORG/REPO --builder-id=https://cloudbuild.googleapis.com/GoogleHostedWorker` prints
  `PASSED: Verified SLSA provenance`. Latest release v2.7.1; the project says `--source-uri` matches `github.com/ORG/REPO`
  "when the build used a Cloud Build GitHub trigger", otherwise the source is the Cloud Storage tarball. Whether an
  API-started build from a connected repository records the GitHub URI is the first thing the real run checks
  (section 7). If it does not, `appdelivery` reads the occurrence itself, checks the signature with Google's key and
  compares `invocationId`/build id, builder and digest with what it recorded.
- **Binary Authorization** can require the `built-by-cloud-build` attestor (`projects/P/attestors/built-by-cloud-build`,
  created at the project's first build; Cloud Build and Binary Authorization must be in the same project) with
  `evaluationMode: REQUIRE_ATTESTATION`, `enforcementMode: ENFORCED_BLOCK_AND_AUDIT_LOG` (or `DRYRUN_AUDIT_LOG_ONLY`).
  It enforces on GKE, Cloud Run (services and jobs: `--binary-authorization=default`), Cloud Service Mesh and
  Distributed Cloud. **Not on Compute Engine machines.** So Binary Authorization is a second lock for the cluster,
  service and job targets, and `appdelivery` must do the check itself for the machine target anyway. The check is
  therefore `appdelivery`'s in every environment: a release's digest must be one this service built (its own record)
  and, where the environment asks, carry verified Cloud Build provenance for that build.

What this replaces: today's `require_attestation` verifies a Cosign attestation signed by a key the team's CI holds
(`trusted_signers`). That is the outside-signs-inside model; the new rule is that the builder's own provenance is the
proof. The Cosign path can stay for the operator who also wants a human signer, but it no longer stands in for "built
inside".

## 6. Identities and grants (F)

| Who | Needs | Granted where |
|---|---|---|
| `appdelivery`'s host account (the caller of Cloud Build) | `cloudbuild.builds.create`, `cloudbuild.builds.get` (`roles/cloudbuild.builds.editor` has them; a custom role with these two is tighter); `iam.serviceAccounts.actAs` on each build account (`roles/iam.serviceAccountUser`) | the project; each build account |
| a build account per environment, user-managed | `roles/logging.logWriter`; `roles/artifactregistry.writer` on the team's repository | the project; **each repository** the account may push to |
| the Cloud Build service agent `service-NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com` | `roles/secretmanager.admin` to create the connection (found by the run; the page says `secretAccessor`), `roles/secretmanager.secretAccessor` on the connection's secret | created by the connection flow |
| `appdelivery`'s environment accounts (unchanged) | `roles/artifactregistry.reader` on the repositories in `allowed_registries`; `containeranalysis.occurrences.list` to read provenance | each repository; the project |

Facts behind the table:

- **The default build account changed in May and June 2024.** Projects whose first build ran after that use the
  Compute Engine default account; older ones keep the legacy `NUMBER@cloudbuild.gserviceaccount.com`. Google calls a
  user-specified account for every build "the most secure option"; the constraints are
  `constraints/cloudbuild.useBuildServiceAccount`, `constraints/cloudbuild.useComputeServiceAccount` and
  `constraints/cloudbuild.disableCreateDefaultServiceAccount`. We always set `serviceAccount`, so the defaults never run
  a build of ours. The spike of 2026-10-29 already built this way ([DELIVERY-SPIKE.md](../DELIVERY-SPIKE.md) section 10).
- **Cross-project build accounts** are blocked by `iam.disableCrossProjectServiceAccountUsage`, enforced by default, and
  the project must belong to an organization to use a user-specified account at all. Build account, Cloud Build and
  the registry stay in one project.
- **`roles/artifactregistry.writer`** holds `artifactregistry.repositories.uploadArtifacts`, `tags.create`,
  `tags.update` and not `tags.delete`, `versions.delete` or `repositories.deleteArtifacts`; deletion needs
  `roles/artifactregistry.repoAdmin`. `roles/artifactregistry.reader` holds `downloadArtifacts` and `attachments.get`
  and `list` (the provenance attachment). So the build account can add images and never remove one, and the team's
  members (level `use`) keep reading.
- **A Docker repository with `--immutable-tags`** keeps a tag on one digest for good, and tagged artifacts cannot be
  deleted by cleanup policies. Releases go by digest anyway; immutable tags stop a `latest` that moves. `cloudinfra`'s
  `registry` kind already has `immutable_tags`.
- **What a build can reach.** The Dockerfile is the team's; its `RUN` lines execute on Cloud Build's worker during
  `docker build`. The build account's rights are therefore what a malicious Dockerfile could use: writer on the team's
  own repository and log writer, nothing else. That bounds the damage to the team's own images, which is the same bound
  the team's CI had. No secret of a request is given to a build (`availableSecrets` stays unset).

## 7. Costs, limits and what the real run must check

**Prices** (default pool, us-central1, per build-minute, in force since 2025-11-01; F): `e2-medium` $0.003,
`e2-standard-2` $0.006, `e2-highcpu-8` $0.0156, `e2-highcpu-32` $0.0624; other regions differ for all but
`e2-standard-2`. Private pools are billed per machine type too (`e2-standard-2` $0.006 in us-central1 after the change).
A free tier of 2,500 build-minutes a month on `e2-standard-2` per billing account was reported by a search snippet (S)
and not read on the page. The cost estimate in `appdelivery`'s build plan uses the machine type's price times the
timeout as the ceiling, as `cloudinfra`'s `pricing.py` does for resources.

**Private pools** (F): dedicated workers, `gcloud builds worker-pools create --region --worker-machine-type
--worker-disk-size --peered-network --no-public-egress`; `options.pool.name` is
`projects/P/locations/R/workerPools/ID`, and the caller needs `roles/cloudbuild.workerPoolUser`. `NO_PUBLIC_EGRESS`
removes the public address and "will restrict your private pool from accessing resources in the public internet"; how
a pool then reaches GitHub is not stated on the pages read. Private pools also do not generate attestations unless
`requestedVerifyOption: VERIFIED` is set. **Not in the first change**: the default pool, with regional builds and
`VERIFIED`, meets the requirement; a private pool is for an operator who wants builds without public addresses or more
than 30 concurrent builds.

**The docker step** (F): `gcr.io/cloud-builders/docker` "may not support the most recent features or versions of
Docker" and has no pinned versions; Google recommends the official `docker` image, tagged by version, as the step
(`name: docker:VERSION`). Neither page mentions BuildKit; the spike found Cloud Build's docker step had none, so the
build passes `--build-arg TARGETARCH=amd64` as both Dockerfiles expect. The run checks whether a versioned `docker`
step image (pinned by digest, like everything else in [REQUIREMENTS.md](../REQUIREMENTS.md)) builds with BuildKit on the
worker's daemon; if so, the `TARGETARCH` argument becomes unnecessary.

**Checked before building, to be confirmed on the run:**

| Claim | Source | What it decides |
|---|---|---|
| A build from a connected repository can be started by the v1 API with `connectedRepository {repository, revision}` | F: "Submit a build via CLI and API", the v1 `Build` reference | the build request needs no trigger and no `gcloud` |
| `revision` accepts a commit SHA | F: the `Build` reference and `gcloud builds submit --help` | the service accepts only SHAs |
| `requestedVerifyOption: VERIFIED` + `images` gives signed SLSA v1 provenance readable with `--show-provenance` | F: "View build provenance" | the release gate has something to verify |
| `slsa-verifier` v2.7.1 verifies Cloud Build image provenance with builder id `https://cloudbuild.googleapis.com/GoogleHostedWorker` | F: slsa-verifier README | which tool the delivery image pins; whether `--source-uri` matches for API builds is **open** |
| A user-specified build account needs `CLOUD_LOGGING_ONLY` or a user bucket | F: "Configure user-specified service accounts" | the options block |
| `roles/artifactregistry.writer` cannot delete | F: IAM roles reference | the build account is the single writer |
| Binary Authorization covers GKE and Cloud Run, not Compute Engine | F: Binary Authorization overview | `appdelivery` verifies for machines itself |
| Default pool: 10 to 30 concurrent builds, global, not raisable | F: Cloud Build quotas | a per-team build queue in the service, and a refusal with the reason when Cloud Build queues |

## 8. The design to build, in order

Status, 2026-10-30: the real run (step 6) is done for builds and the gate ([DELIVERY-SPIKE.md](../DELIVERY-SPIKE.md)
section 14). It answered section 7's open question: the v1 provenance of an API build from a connected repository names
the GitHub URI and the commit, so `slsa-verifier --source-uri github.com/ORG/REPO` should apply. Step 2's provenance half is built against the run's real answer
(`appdelivery/provenance.py`): Google's signature over DSSE's encoding with the `google-hosted-worker` key, then the
signed statement held to the record; `slsa-verifier` was not needed, so the image gains no binary. Steps 1 and 3 are built, and step 2's first half (a release names only a digest a build of this
service recorded for the same team). Step 3 took the operator's list of writers rather than a per-provider setting:
`access.registry_writers`, provider-independent, applied in the grant loop. Provenance verification waits for the real
run, so that it is written against Cloud Build's actual occurrence JSON rather than the documentation's examples. Steps
4 to 6 remain.

1. **`appdelivery`: the build request.** `BuildRequest {environment, repository (an operator-listed name), revision
   (40 hex), dockerfile (relative path, default `Dockerfile`), context (relative dir, default `.`), image (a name under the
   team's registry), note}`. Configuration per environment: `build: {service_account, machine_type, timeout_seconds,
   repositories: {NAME: projects/…/repositories/…}}`, with each repository mapped to the `infra_owner` allowed to build
   it. A new `build.py` composes the Cloud Build body (section 3), submits it over HTTPS with the host's token, and the
   worker polls `builds.get`; a `builds` table records who asked, repository, revision, build id, status, image and
   digest. Tools `build`, `get_build`, `list_builds`; CLI `app-delivery build`. The plan step prices the ceiling.
2. **`appdelivery`: the release gate.** A release's digest must match a `SUCCESS` build of the same `infra_owner` in a
   `builds` row (every environment), and in an environment with `require_provenance: true` the image's Cloud Build
   provenance must verify (`slsa-verifier`, pinned by SHA-256 in `Dockerfile.appdelivery` beside Cosign) and name that
   build. The Cosign `require_attestation` stays as an additional, optional signer check.
3. **`cloudinfra`: one writer.** Registry `admin` grants nothing to members; the build account is named by the operator
   in `providers.gcp.build_accounts` and granted writer on each registry the request creates (or the team names it at
   `admin` and the adapter checks it is one of the operator's build accounts). Federated members and
   `access.workload_identity_pools` are removed. The connect block drops `login` and `push` and says "built by the
   delivery service". The catalog, the request examples and the tests follow.
4. **The platform's own images.** A `cloudbuild.yaml` for `Dockerfile.cloudinfra` and `Dockerfile.appdelivery` with the
   same options, run by the platform team; SETUP.md stops offering Docker Desktop as the way to produce a deployable
   image; GitHub Actions keeps linting and testing and never builds.
5. **Documents.** README and the band diagram, DELIVERY-SPEC (a section for builds, the corrected ownership table),
   INFRASTRUCTURE-ARCHITECTURE ("A team's image registry" rewritten), REQUIREMENTS (Cloud Build and Container Analysis
   APIs, the new roles, slsa-verifier's version and sums), SETUP (the connection, once), this note's results.
6. **The real run**, recorded in DELIVERY-SPIKE: connection and one linked repository, a build by SHA, provenance read
   and verified, a release that is refused for a digest pushed by hand into the same repository, then the same digest
   accepted after a build; both platform images built the same way.

## 9. Sources

All read 2026-10-30 unless marked S.

- F https://docs.cloud.google.com/build/docs/api/reference/rest/v1/projects.builds — the `Build` resource: `source`
  (`storageSource`, `gitSource`, `connectedRepository`, `developerConnectConfig`), `steps`, `images`, `serviceAccount`,
  `options`, `timeout`, `results.images`, status values.
- F https://docs.cloud.google.com/build/docs/api/reference/rest/v1/projects.locations.builds/create — the regional
  create path, `cloudbuild.builds.create`, the Operation answer.
- F https://docs.cloud.google.com/build/docs/running-builds/submit-build-via-cli-api — a build from a 2nd-gen repository
  by API: `connectedRepository {repository, revision}`, and from a Cloud Storage archive.
- F https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit — `SOURCE` as a repository resource, `--revision`
  (a commit SHA among others), `--region` required for 2nd-gen sources.
- F https://docs.cloud.google.com/build/docs/build-config-file-schema — field limits, `logging`, `requestedVerifyOption`,
  `machineType`, `timeout` default and maximum, `serviceAccount` and the legacy account.
- F https://docs.cloud.google.com/build/docs/repositories — 1st and 2nd gen, hosts, regions, Developer Connect.
- F https://docs.cloud.google.com/build/docs/automating-builds/github/connect-repo-github — the connection flow, the
  token in Secret Manager, the service agent's `roles/secretmanager.secretAccessor`, the token alternative.
- F https://docs.cloud.google.com/build/docs/api/reference/rest/v2/projects.locations.connections.repositories — the
  Repository resource and its methods.
- F https://docs.cloud.google.com/build/docs/cloud-build-service-account — the two default accounts,
  `roles/cloudbuild.builds.builder`, `iam.serviceAccounts.actAs`.
- F https://docs.cloud.google.com/build/docs/cloud-build-service-account-updates — the 2024 change and the three
  constraints.
- F https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts — the
  `serviceAccount` format, `CLOUD_LOGGING_ONLY`, cross-project constraints, triggers ignoring the config's account.
- F https://docs.cloud.google.com/build/docs/iam-roles-permissions — the Cloud Build roles.
- F https://docs.cloud.google.com/build/docs/securing-builds/view-build-provenance — when provenance is generated,
  where it is stored, SLSA v0.1 and v1, the signatures and keys, `--show-provenance`, `slsa-verifier`.
- F https://docs.cloud.google.com/build/docs/securing-builds/use-provenance-and-binary-authorization — the
  `built-by-cloud-build` attestor, the policy YAML, the `images`-field condition, private pools needing `VERIFIED`.
- F https://docs.cloud.google.com/binary-authorization/docs/overview — the platforms it enforces on.
- F https://docs.cloud.google.com/binary-authorization/docs/run/enabling-binauthz-cloud-run — services and jobs,
  `--binary-authorization=default`, the denial message, breakglass.
- F https://github.com/slsa-framework/slsa-verifier — `verify-image` for Cloud Build, the builder id, `--source-uri`
  behaviour; releases page: v2.7.1 latest.
- F https://docs.cloud.google.com/iam/docs/roles-permissions/artifactregistry — the permissions of reader, writer and
  repoAdmin.
- F https://docs.cloud.google.com/artifact-registry/docs/access-control — the predefined roles, repository-level grants.
- F https://docs.cloud.google.com/artifact-registry/docs/repositories/create-repos — `--immutable-tags`.
- F https://docs.cloud.google.com/artifact-registry/docs/docker/manage-images — tags, digests, deletion and roles.
- F https://docs.cloud.google.com/artifact-analysis/docs/access-control — `roles/containeranalysis.occurrences.viewer`.
- F https://docs.cloud.google.com/artifact-analysis/docs/reference/rest/v1/projects.occurrences and `/list` — the
  Occurrence resource, `resourceUri`, `envelope`, the list method.
- F https://docs.cloud.google.com/build/quotas — concurrency and resource limits.
- F https://cloud.google.com/build/pricing-update — the prices in force since 2025-11-01, by region and pool.
- S web search, 2026-10-30 — the 2,500 free build-minutes a month on `e2-standard-2`; not read on the page.
- F https://docs.cloud.google.com/build/docs/private-pools/private-pools-overview, `/create-manage-private-pools`,
  `/run-builds-in-private-pool`, `/use-in-private-network` — pools, flags, `options.pool.name`,
  `roles/cloudbuild.workerPoolUser`, `NO_PUBLIC_EGRESS`.
- F https://docs.cloud.google.com/build/docs/locations — the regions.
- F https://docs.cloud.google.com/build/docs/securing-builds/gate-builds-on-approval — approval is for triggers.
- F https://docs.cloud.google.com/build/docs/subscribe-build-notifications — the `cloud-builds` topic.
- F https://docs.cloud.google.com/build/docs/building/build-containers — the docker step, `-f`, the `images` field.
- F https://github.com/GoogleCloudPlatform/cloud-builders/blob/master/docker/README.md — no pinned Docker versions;
  the official `docker` image recommended.
