# Delivery service catalog

Generated from `appdelivery/catalog.py` by `app-delivery catalog --write docs/DELIVERY-CATALOG.md`; a test keeps this file and the registered tools equal to the records. 14 tools, served at `/mcp` and as REST routes. Every result is a JSON object with `status`; a failed one is `{"status": "FAILED", "error": {"code": "…", "message": "…", "details": {…}}}`. Release ids are `rel-` plus 12 hex characters. Authentication: a bearer token of a configured principal. Build ids are `bld-` plus 12 hex characters. See [DELIVERY-SPEC.md](DELIVERY-SPEC.md).

## At a glance

| Tool | Purpose | Safety hint |
|---|---|---|
| `build_image` | Build an image | builds |
| `get_build` | Get a build | read only |
| `list_builds` | List builds | read only |
| `plan_release` | Plan a release | read only |
| `deploy_release` | Deploy a release | changes |
| `approve_release` | Approve a release | changes |
| `get_release` | Get a release | read only |
| `list_releases` | List releases | read only |
| `get_running` | What runs where | read only |
| `rollback_release` | Roll back a release | changes |
| `run_release` | Run a job now | changes |
| `get_release_run` | Get a job's run | read only |
| `remove_release` | Remove a release | destructive |
| `get_service_status` | Get the service status | read only |

## Error codes

| Code | Meaning |
|---|---|
| `INVALID_RELEASE` | The release does not fit the contract: a tag instead of a digest, an unknown field, a bad path, port 22. |
| `INVALID_BUILD` | The build request does not fit the contract (not a full commit, a path outside the source, an unknown field), or the environment builds nothing. |
| `INVALID_BINDING` | An environment value with no source or two, a credential given as a literal, or a binding the request does not have. |
| `UNKNOWN_ENVIRONMENT` | No environment of that name in the service's configuration. |
| `PERMISSION_DENIED` | The caller may not do this (can_deploy, can_approve, the environment), or did not authenticate. |
| `LIMIT_EXCEEDED` | The caller made more calls in a minute than the operator allows (rate_limit_per_minute); retry after the Retry-After seconds. |
| `NOT_FOUND` | No such release, request or group is visible to this caller: another team's are not found either. |
| `INVALID_STATE` | The request or release is not in a state that allows this: not READY, another release moving, nothing to roll back to. |
| `NOT_GRANTED` | The request does not name this environment's service account in its access list as admin, GRANTED, on every machine. |
| `UNSUPPORTED_TARGET` | A machine without Docker: ask the infrastructure layer for its Container-Optimized OS profile. |
| `UNREACHABLE_TARGET` | A machine this service cannot reach (no SSH through IAP) or that cannot pull (no public address for a registry on the internet). |
| `REGISTRY_NOT_ALLOWED` | The image is not under the registries the environment allows. |
| `ATTESTATION_FAILED` | No trusted signer of the environment has an attestation for this image digest. |
| `NOT_BUILT_HERE` | The image digest is not one a build of this service produced for the team: images are built only in the trusted project. |
| `PROVENANCE_FAILED` | The environment requires Cloud Build's provenance, and the image has none signed by Google that names the build this service recorded, or Container Analysis could not be read. |
| `BUILD_UNAVAILABLE` | Cloud Build could not be reached, or refused the environment's service account (builds.editor, and serviceAccountUser on the build account). |
| `BUILD_FAILED` | Cloud Build refused the build or ended it without success (failure, timeout, cancelled); its log says why. |
| `BUILD_LIMIT_EXCEEDED` | The team has as many builds queued or working in this environment as the operator allows at once, or as many in 24 hours; wait, or ask for more. |
| `INFRA_UNAVAILABLE` | The infrastructure layer could not be reached or answered something unexpected; retry later. |
| `INFRA_AUTH_FAILED` | The infrastructure layer refused this service's token for the environment. |
| `GOOGLE_AUTH_FAILED` | This service's host could not get a token, or may not act as the environment's service account. |
| `SECRET_UNAVAILABLE` | A bound secret could not be read as the environment's service account. |
| `MACHINE_UNREACHABLE` | The SSH session to a machine failed, after waiting out IAP's passing refusals. |
| `MACHINE_ERROR` | The host script on a machine stopped or ended without a report. |
| `CLUSTER_UNREACHABLE` | The cluster's API could not be reached, or refused this environment's service account (its namespace grant, or container.clusterViewer). |
| `CLUSTER_ERROR` | The cluster's API refused a change to the workload; details carry its answer. |
| `SERVICE_UNREACHABLE` | Cloud Run could not be reached, or refused this environment's service account (its admin grant on the service). |
| `SERVICE_ERROR` | Cloud Run refused a change to the service, or did not finish it in time; details carry its answer. |
| `PULL_FAILED` | A machine could not log in to the registry or pull the image; nothing changed there. |
| `UNHEALTHY` | The container did not answer 2xx on the health path in time; the machine runs its previous container again. |
| `ROLLOUT_FAILED` | The rollout stopped at a machine; the machines done before it were put back, the rest skipped. |
| `ROLLBACK_FAILED` | A machine could not start its previous container; details name it. |
| `REMOVE_FAILED` | The containers could not be removed from some machines; remove again. |
| `INTERNAL_ERROR` | The service stopped on an unexpected exception or was restarted mid-way; nothing was retried. |

## `build_image`

**Purpose.** Build an image in the trusted Google Cloud project from one commit of a source the operator linked: Cloud Build fetches the source itself, runs docker build with the given Dockerfile and context as the environment's build account, pushes the image to the team's registry and records signed provenance for it. Images are never built or uploaded anywhere else: a release names only the digest a build reports here.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `build` | yes | The build: environment, source (a repository the operator linked, by its name), revision (the full 40-character commit SHA), image (its name under the team's registry, such as web), dockerfile (path in the source, default Dockerfile), context (path in the source, default .), note. No build step, command, argument, secret or account is accepted: the service composes the build itself. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** BuildResponse: build_id, status (QUEUED, WORKING, SUCCEEDED, FAILED), environment, source, revision, dockerfile, context, image (pushed by tag), built (the same image by digest, once it succeeded: what a release names), requested_by, cloud_build_id, log_url, error.

**Possible errors.** `INVALID_BUILD`, `UNKNOWN_ENVIRONMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `REGISTRY_NOT_ALLOWED`, `BUILD_LIMIT_EXCEEDED`, `GOOGLE_AUTH_FAILED`, `BUILD_UNAVAILABLE`, `BUILD_FAILED`.

**Security.** Builds and pushes an image in the trusted project; runs nothing on machines. Needs can_build in the environment, and a source of the caller's own team; a team's builds are bounded at once and per day.

**After the call.** It answers once Cloud Build has the build, or waits up to wait_seconds; follow it with get_build until the status is SUCCEEDED or FAILED, then release the image named in built.

## `get_build`

**Purpose.** A build as Cloud Build reports it now, with the digest to release once it succeeded.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `build_id` | yes | The build id this service issued (bld-<12 hex>). |

**Output.** BuildResponse: build_id, status (QUEUED, WORKING, SUCCEEDED, FAILED), environment, source, revision, dockerfile, context, image (pushed by tag), built (the same image by digest, once it succeeded: what a release names), requested_by, cloud_build_id, log_url, error.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`.

**Security.** Read-only; runs and changes nothing. Any principal of the team that asked for it.

## `list_builds`

**Purpose.** The team's builds, newest first, optionally of one environment or source.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `environment` | no | Only builds in this environment. |
| `source` | no | Only builds of this source. |
| `limit` | no | At most this many builds (1 to 200, default 50). |

**Output.** {count, builds: [BuildResponse]}.

**Possible errors.** `PERMISSION_DENIED`.

**Security.** Read-only; runs and changes nothing. Any principal; it sees its own team's builds only.

## `plan_release`

**Purpose.** Check a release against the infrastructure layer's request as it is now and say what deploying it would do: the machines, where each environment value comes from, whether an attestation and an approval are needed, and which running release it would replace.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release` | yes | The release of an image this service built (build_image): environment, request_id (the infrastructure layer's request), target ({group} for every machine of a compute group, or {cluster: NAME, workload, replicas, expose: {hostname, path}} for a workload on one of the request's clusters, cluster: true for its only one, expose to reach it through the cluster's front door; {service: NAME} for a managed container service, {job: NAME} for a managed job, each true for the request's only one), image (host/path@sha256:digest; a tag is refused), port and health {path, timeout_seconds} (not used for a job, which serves nothing), env (NAME: {value} or {endpoint} or {secret}), note. No source, command or mount is accepted. |

**Output.** PlanResponse: status PLANNED, machines, env sources, attestation_required, provenance_required, approval_required, replaces, build_id, warnings.

**Possible errors.** `INVALID_RELEASE`, `INVALID_BINDING`, `UNKNOWN_ENVIRONMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATE`, `NOT_GRANTED`, `UNSUPPORTED_TARGET`, `UNREACHABLE_TARGET`, `REGISTRY_NOT_ALLOWED`, `NOT_BUILT_HERE`, `INFRA_UNAVAILABLE`, `INFRA_AUTH_FAILED`.

**Security.** Read-only; runs and changes nothing. Needs can_deploy in the release's environment. Every problem is reported at once, each with a code.

## `deploy_release`

**Purpose.** Record a release and roll it out. On machines: one at a time (pull, start, health check); the first machine that fails stops the rollout and the machines done before it are put back, so a group never runs two versions. On a cluster: a Deployment, a Service and a Secret of the release in the request's namespace, rolled by Kubernetes without dropping below the wanted replicas, and the previous release put back if it does not become healthy. In an environment that requires approval it waits for approve_release.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release` | yes | The release of an image this service built (build_image): environment, request_id (the infrastructure layer's request), target ({group} for every machine of a compute group, or {cluster: NAME, workload, replicas, expose: {hostname, path}} for a workload on one of the request's clusters, cluster: true for its only one, expose to reach it through the cluster's front door; {service: NAME} for a managed container service, {job: NAME} for a managed job, each true for the request's only one), image (host/path@sha256:digest; a tag is refused), port and health {path, timeout_seconds} (not used for a job, which serves nothing), env (NAME: {value} or {endpoint} or {secret}), note. No source, command or mount is accepted. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `INVALID_RELEASE`, `INVALID_BINDING`, `UNKNOWN_ENVIRONMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATE`, `NOT_GRANTED`, `UNSUPPORTED_TARGET`, `UNREACHABLE_TARGET`, `REGISTRY_NOT_ALLOWED`, `NOT_BUILT_HERE`, `INFRA_UNAVAILABLE`, `INFRA_AUTH_FAILED`, `ATTESTATION_FAILED`, `PROVENANCE_FAILED`, `SECRET_UNAVAILABLE`, `GOOGLE_AUTH_FAILED`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `CLUSTER_UNREACHABLE`, `CLUSTER_ERROR`, `SERVICE_UNREACHABLE`, `SERVICE_ERROR`, `PULL_FAILED`, `UNHEALTHY`, `ROLLOUT_FAILED`.

**Security.** Changes what runs on machines. Needs can_deploy in the release's environment.

**After the call.** It answers at once with the work running, or waits up to wait_seconds; follow it with get_release until the status is RUNNING, FAILED, ROLLED_BACK or REMOVED.

## `approve_release`

**Purpose.** Approve a release waiting for approval and start its rollout; everything is checked again as it is when the rollout starts.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `ATTESTATION_FAILED`, `PROVENANCE_FAILED`, `SECRET_UNAVAILABLE`, `GOOGLE_AUTH_FAILED`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `CLUSTER_UNREACHABLE`, `CLUSTER_ERROR`, `SERVICE_UNREACHABLE`, `SERVICE_ERROR`, `PULL_FAILED`, `UNHEALTHY`, `ROLLOUT_FAILED`.

**Security.** Changes what runs on machines. Needs can_approve in the environment, as a principal of the same team other than the one that asked.

**After the call.** It answers at once with the work running, or waits up to wait_seconds; follow it with get_release until the status is RUNNING, FAILED, ROLLED_BACK or REMOVED.

## `get_release`

**Purpose.** A release as recorded now, with each machine's status.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`.

**Security.** Read-only; runs and changes nothing. Any principal of the team that owns the request.

## `list_releases`

**Purpose.** The team's releases, newest first, optionally of one request or environment.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | no | Only releases on this infrastructure request (32 hex characters). |
| `environment` | no | Only releases in this environment. |
| `limit` | no | At most this many releases (1 to 200, default 50). |

**Output.** {count, releases: [ReleaseResponse]}.

**Possible errors.** `PERMISSION_DENIED`.

**Security.** Read-only; runs and changes nothing. Any principal; it sees its own team's releases only.

## `get_running`

**Purpose.** The release running on each group or cluster workload of a request, and with live, what each machine or the cluster reports running now and whether it matches.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | yes | The infrastructure request (32 hex characters). |
| `live` | no | Ask each machine what it runs now (slower; reaches every machine). |

**Output.** {request_id, groups: [{group, release_id, image, environment, machines: [{resource, name, status, running, image, matches}]}]}.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `MACHINE_UNREACHABLE`, `CLUSTER_UNREACHABLE`, `SERVICE_UNREACHABLE`.

**Security.** Read-only; runs and changes nothing. Any principal of the team that owns the request.

## `rollback_release`

**Purpose.** Start the previous release again on every machine of the group, or apply it again on the cluster. Offered only for the release running on its target while the previous one is kept: not for a target's first release, and not for one a rollback restored.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `ROLLBACK_FAILED`.

**Security.** Changes what runs on machines. Needs can_deploy in the release's environment.

**After the call.** It answers at once with the work running, or waits up to wait_seconds; follow it with get_release until the status is RUNNING, FAILED, ROLLED_BACK or REMOVED.

## `run_release`

**Purpose.** Start the job a release is on, as it holds that release: one run of its tasks, now. A job's schedule, when the infrastructure layer made one, starts runs by itself as well.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** RunResponse: release_id, execution, status (RUNNING, SUCCEEDED, FAILED, CANCELLED), tasks, succeeded, failed, running, started_at, finished_at, log_url.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `SERVICE_UNREACHABLE`, `SERVICE_ERROR`.

**Security.** Changes what runs on machines. Needs can_deploy in the release's environment; the release must be the one its job holds.

**After the call.** It answers once the run has started, or waits up to wait_seconds for it to finish; follow it with get_release_run until the status is not RUNNING.

## `get_release_run`

**Purpose.** How one run of a job went: its tasks' counts, when it started and finished, and where its log is.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |
| `execution` | yes | The run's name, as run_release returned it. |

**Output.** RunResponse: release_id, execution, status (RUNNING, SUCCEEDED, FAILED, CANCELLED), tasks, succeeded, failed, running, started_at, finished_at, log_url.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `SERVICE_UNREACHABLE`.

**Security.** Read-only; runs and changes nothing. Any principal of the team that owns the request.

## `remove_release`

**Purpose.** Stop and delete the release's containers, and the previous ones, from every machine of the group, or its Deployment, Service and Secrets from the cluster; the machines and the cluster stay as the infrastructure layer made them.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release_id` | yes | The release id this service issued (rel-<12 hex>). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `REMOVE_FAILED`.

**Security.** Stops and deletes what runs on machines. Needs can_deploy in the release's environment.

**After the call.** It answers at once with the work running, or waits up to wait_seconds; follow it with get_release until the status is RUNNING, FAILED, ROLLED_BACK or REMOVED.

## `get_service_status`

**Purpose.** The service's version, the infrastructure layer it reads, and its environments with their rules.

**Inputs.** None.

**Output.** {status, version, cloudinfra, environments: {name: {service_account, allowed_registries, require_attestation, require_provenance, require_approval, build}}, moving, building}.

**Possible errors.** none beyond authentication.

**Security.** Read-only; runs and changes nothing. Any principal.
