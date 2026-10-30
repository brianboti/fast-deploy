# Delivery service catalog

Generated from `appdelivery/catalog.py` by `app-delivery catalog --write docs/DELIVERY-CATALOG.md`; a test keeps this file and the registered tools equal to the records. 9 tools, served at `/mcp` and as REST routes. Every result is a JSON object with `status`; a failed one is `{"status": "FAILED", "error": {"code": "…", "message": "…", "details": {…}}}`. Release ids are `rel-` plus 12 hex characters. Authentication: a bearer token of a configured principal. See [DELIVERY-SPEC.md](DELIVERY-SPEC.md).

## At a glance

| Tool | Purpose | Safety hint |
|---|---|---|
| `plan_release` | Plan a release | read only |
| `deploy_release` | Deploy a release | changes |
| `approve_release` | Approve a release | changes |
| `get_release` | Get a release | read only |
| `list_releases` | List releases | read only |
| `get_running` | What runs where | read only |
| `rollback_release` | Roll back a release | changes |
| `remove_release` | Remove a release | destructive |
| `get_service_status` | Get the service status | read only |

## Error codes

| Code | Meaning |
|---|---|
| `INVALID_RELEASE` | The release does not fit the contract: a tag instead of a digest, an unknown field, a bad path, port 22. |
| `INVALID_BINDING` | An environment value with no source or two, a credential given as a literal, or a binding the request does not have. |
| `UNKNOWN_ENVIRONMENT` | No environment of that name in the service's configuration. |
| `PERMISSION_DENIED` | The caller may not do this (can_deploy, can_approve, the environment), or did not authenticate. |
| `NOT_FOUND` | No such release, request or group is visible to this caller: another team's are not found either. |
| `INVALID_STATE` | The request or release is not in a state that allows this: not READY, another release moving, nothing to roll back to. |
| `NOT_GRANTED` | The request does not name this environment's service account in its access list as admin, GRANTED, on every machine. |
| `UNSUPPORTED_TARGET` | A machine without Docker: ask the infrastructure layer for its Container-Optimized OS profile. |
| `UNREACHABLE_TARGET` | A machine this service cannot reach (no SSH through IAP) or that cannot pull (no public address for a registry on the internet). |
| `REGISTRY_NOT_ALLOWED` | The image is not under the registries the environment allows. |
| `ATTESTATION_FAILED` | No trusted signer of the environment has an attestation for this image digest. |
| `INFRA_UNAVAILABLE` | The infrastructure layer could not be reached or answered something unexpected; retry later. |
| `INFRA_AUTH_FAILED` | The infrastructure layer refused this service's token for the environment. |
| `GOOGLE_AUTH_FAILED` | This service's host could not get a token, or may not act as the environment's service account. |
| `SECRET_UNAVAILABLE` | A bound secret could not be read as the environment's service account. |
| `MACHINE_UNREACHABLE` | The SSH session to a machine failed, after waiting out IAP's passing refusals. |
| `MACHINE_ERROR` | The host script on a machine stopped or ended without a report. |
| `CLUSTER_UNREACHABLE` | The cluster's API could not be reached, or refused this environment's service account (its namespace grant, or container.clusterViewer). |
| `CLUSTER_ERROR` | The cluster's API refused a change to the workload; details carry its answer. |
| `PULL_FAILED` | A machine could not log in to the registry or pull the image; nothing changed there. |
| `UNHEALTHY` | The container did not answer 2xx on the health path in time; the machine runs its previous container again. |
| `ROLLOUT_FAILED` | The rollout stopped at a machine; the machines done before it were put back, the rest skipped. |
| `ROLLBACK_FAILED` | A machine could not start its previous container; details name it. |
| `REMOVE_FAILED` | The containers could not be removed from some machines; remove again. |
| `INTERNAL_ERROR` | The service stopped on an unexpected exception or was restarted mid-way; nothing was retried. |

## `plan_release`

**Purpose.** Check a release against the infrastructure layer's request as it is now and say what deploying it would do: the machines, where each environment value comes from, whether an attestation and an approval are needed, and which running release it would replace.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release` | yes | The release: environment, request_id (the infrastructure layer's request), target ({group} for every machine of a compute group, or {cluster: NAME, workload, replicas, expose: {hostname, path}} for a workload on one of the request's clusters, cluster: true for its only one, expose to reach it through the cluster's front door), image (host/path@sha256:digest; a tag is refused), port, health {path, timeout_seconds}, env (NAME: {value} or {endpoint} or {secret}), note. No source, command or mount is accepted. |

**Output.** PlanResponse: status PLANNED, machines, env sources, attestation_required, approval_required, replaces, warnings.

**Possible errors.** `INVALID_RELEASE`, `INVALID_BINDING`, `UNKNOWN_ENVIRONMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATE`, `NOT_GRANTED`, `UNSUPPORTED_TARGET`, `UNREACHABLE_TARGET`, `REGISTRY_NOT_ALLOWED`, `INFRA_UNAVAILABLE`, `INFRA_AUTH_FAILED`.

**Security.** Read-only; runs and changes nothing. Needs can_deploy in the release's environment. Every problem is reported at once, each with a code.

## `deploy_release`

**Purpose.** Record a release and roll it out. On machines: one at a time (pull, start, health check); the first machine that fails stops the rollout and the machines done before it are put back, so a group never runs two versions. On a cluster: a Deployment, a Service and a Secret of the release in the request's namespace, rolled by Kubernetes without dropping below the wanted replicas, and the previous release put back if it does not become healthy. In an environment that requires approval it waits for approve_release.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `release` | yes | The release: environment, request_id (the infrastructure layer's request), target ({group} for every machine of a compute group, or {cluster: NAME, workload, replicas, expose: {hostname, path}} for a workload on one of the request's clusters, cluster: true for its only one, expose to reach it through the cluster's front door), image (host/path@sha256:digest; a tag is refused), port, health {path, timeout_seconds}, env (NAME: {value} or {endpoint} or {secret}), note. No source, command or mount is accepted. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running. |

**Output.** ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.

**Possible errors.** `INVALID_RELEASE`, `INVALID_BINDING`, `UNKNOWN_ENVIRONMENT`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_STATE`, `NOT_GRANTED`, `UNSUPPORTED_TARGET`, `UNREACHABLE_TARGET`, `REGISTRY_NOT_ALLOWED`, `INFRA_UNAVAILABLE`, `INFRA_AUTH_FAILED`, `ATTESTATION_FAILED`, `SECRET_UNAVAILABLE`, `GOOGLE_AUTH_FAILED`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `CLUSTER_UNREACHABLE`, `CLUSTER_ERROR`, `PULL_FAILED`, `UNHEALTHY`, `ROLLOUT_FAILED`.

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

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `ATTESTATION_FAILED`, `SECRET_UNAVAILABLE`, `GOOGLE_AUTH_FAILED`, `MACHINE_UNREACHABLE`, `MACHINE_ERROR`, `CLUSTER_UNREACHABLE`, `CLUSTER_ERROR`, `PULL_FAILED`, `UNHEALTHY`, `ROLLOUT_FAILED`.

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

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `MACHINE_UNREACHABLE`, `CLUSTER_UNREACHABLE`.

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

**Output.** {status, version, cloudinfra, environments: {name: {service_account, allowed_registries, require_attestation, require_approval}}, moving}.

**Possible errors.** none beyond authentication.

**Security.** Read-only; runs and changes nothing. Any principal.
