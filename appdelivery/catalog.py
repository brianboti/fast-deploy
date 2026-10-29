"""The delivery service's catalog: one record per tool, the single source of the registered tools and of
docs/DELIVERY-CATALOG.md. A test holds the registered tools, their parameters and safety hints to these records, and the
document to this file."""
from dataclasses import dataclass

from mcp.types import ToolAnnotations

from .errors import ErrorCode

MEANINGS = {
    ErrorCode.INVALID_RELEASE: "The release does not fit the contract: a tag instead of a digest, an unknown field, a bad path, port 22.",
    ErrorCode.INVALID_BINDING: "An environment value with no source or two, a credential given as a literal, or a binding the request does not have.",
    ErrorCode.UNKNOWN_ENVIRONMENT: "No environment of that name in the service's configuration.",
    ErrorCode.PERMISSION_DENIED: "The caller may not do this (can_deploy, can_approve, the environment), or did not authenticate.",
    ErrorCode.NOT_FOUND: "No such release, request or group is visible to this caller: another team's are not found either.",
    ErrorCode.INVALID_STATE: "The request or release is not in a state that allows this: not READY, another release moving, nothing to roll back to.",
    ErrorCode.NOT_GRANTED: "The request does not name this environment's service account in its access list as admin, GRANTED, on every machine.",
    ErrorCode.UNSUPPORTED_TARGET: "A machine without Docker: ask the infrastructure layer for its Container-Optimized OS profile.",
    ErrorCode.UNREACHABLE_TARGET: "A machine this service cannot reach (no SSH through IAP) or that cannot pull (no public address for a registry on the internet).",
    ErrorCode.REGISTRY_NOT_ALLOWED: "The image is not under the registries the environment allows.",
    ErrorCode.ATTESTATION_FAILED: "No trusted signer of the environment has an attestation for this image digest.",
    ErrorCode.INFRA_UNAVAILABLE: "The infrastructure layer could not be reached or answered something unexpected; retry later.",
    ErrorCode.INFRA_AUTH_FAILED: "The infrastructure layer refused this service's token for the environment.",
    ErrorCode.GOOGLE_AUTH_FAILED: "This service's host could not get a token, or may not act as the environment's service account.",
    ErrorCode.SECRET_UNAVAILABLE: "A bound secret could not be read as the environment's service account.",
    ErrorCode.MACHINE_UNREACHABLE: "The SSH session to a machine failed, after waiting out IAP's passing refusals.",
    ErrorCode.MACHINE_ERROR: "The host script on a machine stopped or ended without a report.",
    ErrorCode.CLUSTER_UNREACHABLE: "The cluster's API could not be reached, or refused this environment's service account (its namespace grant, or container.clusterViewer).",
    ErrorCode.CLUSTER_ERROR: "The cluster's API refused a change to the workload; details carry its answer.",
    ErrorCode.PULL_FAILED: "A machine could not log in to the registry or pull the image; nothing changed there.",
    ErrorCode.UNHEALTHY: "The container did not answer 2xx on the health path in time; the machine runs its previous container again.",
    ErrorCode.ROLLOUT_FAILED: "The rollout stopped at a machine; the machines done before it were put back, the rest skipped.",
    ErrorCode.ROLLBACK_FAILED: "A machine could not start its previous container; details name it.",
    ErrorCode.REMOVE_FAILED: "The containers could not be removed from some machines; remove again.",
    ErrorCode.INTERNAL_ERROR: "The service stopped on an unexpected exception or was restarted mid-way; nothing was retried.",
}

HINTS = {
    "read_only": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    "changes": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
}
HINT_WORDS = {"read_only": "Read-only; runs and changes nothing.", "changes": "Changes what runs on machines.",
              "destructive": "Stops and deletes what runs on machines."}


@dataclass(frozen=True)
class Input:
    name: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    purpose: str
    inputs: tuple[Input, ...]
    output: str
    errors: tuple[str, ...]
    security: str
    hint: str
    follow: str = ""  # what to call next, for a tool that answers before its work is done

    def describe(self, name: str) -> str:
        return next(item.description for item in self.inputs if item.name == name)

    @property
    def model_description(self) -> str:
        """What the model reads: purpose, output, safety rule, and what to call next."""
        parts = [self.purpose, f"Returns {self.output}", f"{HINT_WORDS[self.hint]} {self.security}", self.follow]
        return " ".join(part.rstrip() if part.endswith(".") else part.rstrip() + "." for part in parts if part)

    @property
    def annotations(self) -> ToolAnnotations:
        return HINTS[self.hint]


RELEASE = Input("release", "The release: environment, request_id (the infrastructure layer's request), target ({group} for every machine of a compute group, or "
                           "{cluster: true, workload, replicas} for the request's cluster), image (host/path@sha256:digest; a tag is refused), "
                           "port, health {path, timeout_seconds}, env (NAME: {value} or {endpoint} or {secret}), note. No source, command or mount is accepted.", True)
RELEASE_ID = Input("release_id", "The release id this service issued (rel-<12 hex>).", True)
WAIT = Input("wait_seconds", "Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the work running.")
RELEASE_OUT = ("ReleaseResponse: release_id, status, environment, request_id, group, image, previous, can_roll_back, requested_by, approved_by, env (each name and "
               "its source, never a value), warnings, targets (each machine with its status, times, error and the failed container's scrubbed log), error.")
FOLLOW = "It answers at once with the work running, or waits up to wait_seconds; follow it with get_release until the status is RUNNING, FAILED, ROLLED_BACK or REMOVED."
PLAN_ERRORS = ("INVALID_RELEASE", "INVALID_BINDING", "UNKNOWN_ENVIRONMENT", "PERMISSION_DENIED", "NOT_FOUND", "INVALID_STATE", "NOT_GRANTED", "UNSUPPORTED_TARGET",
               "UNREACHABLE_TARGET", "REGISTRY_NOT_ALLOWED", "INFRA_UNAVAILABLE", "INFRA_AUTH_FAILED")
RUN_ERRORS = ("ATTESTATION_FAILED", "SECRET_UNAVAILABLE", "GOOGLE_AUTH_FAILED", "MACHINE_UNREACHABLE", "MACHINE_ERROR", "CLUSTER_UNREACHABLE", "CLUSTER_ERROR",
              "PULL_FAILED", "UNHEALTHY", "ROLLOUT_FAILED")

CATALOG = (
    ToolSpec("plan_release", "Plan a release",
             "Check a release against the infrastructure layer's request as it is now and say what deploying it would do: the machines, where each environment "
             "value comes from, whether an attestation and an approval are needed, and which running release it would replace.",
             (RELEASE,), "PlanResponse: status PLANNED, machines, env sources, attestation_required, approval_required, replaces, warnings.", PLAN_ERRORS,
             "Needs can_deploy in the release's environment. Every problem is reported at once, each with a code.", "read_only"),
    ToolSpec("deploy_release", "Deploy a release",
             "Record a release and roll it out. On machines: one at a time (pull, start, health check); the first machine that fails stops the rollout and "
             "the machines done before it are put back, so a group never runs two versions. On a cluster: a Deployment, a Service and a Secret of the "
             "release in the request's namespace, rolled by Kubernetes without dropping below the wanted replicas, and the previous release put back if "
             "it does not become healthy. In an environment that requires approval it waits for approve_release.",
             (RELEASE, WAIT), RELEASE_OUT, (*PLAN_ERRORS, *RUN_ERRORS), "Needs can_deploy in the release's environment.", "changes", FOLLOW),
    ToolSpec("approve_release", "Approve a release",
             "Approve a release waiting for approval and start its rollout; everything is checked again as it is when the rollout starts.",
             (RELEASE_ID, WAIT), RELEASE_OUT, ("NOT_FOUND", "PERMISSION_DENIED", "INVALID_STATE", *RUN_ERRORS),
             "Needs can_approve in the environment, as a principal of the same team other than the one that asked.", "changes", FOLLOW),
    ToolSpec("get_release", "Get a release", "A release as recorded now, with each machine's status.", (RELEASE_ID,), RELEASE_OUT, ("NOT_FOUND", "PERMISSION_DENIED"),
             "Any principal of the team that owns the request.", "read_only"),
    ToolSpec("list_releases", "List releases", "The team's releases, newest first, optionally of one request or environment.",
             (Input("request_id", "Only releases on this infrastructure request (32 hex characters)."), Input("environment", "Only releases in this environment."),
              Input("limit", "At most this many releases (1 to 200, default 50).")),
             "{count, releases: [ReleaseResponse]}.", ("PERMISSION_DENIED",), "Any principal; it sees its own team's releases only.", "read_only"),
    ToolSpec("get_running", "What runs where", "The release running on each group or cluster workload of a request, and with live, what each machine or "
             "the cluster reports running now and whether it matches.",
             (Input("request_id", "The infrastructure request (32 hex characters).", True), Input("live", "Ask each machine what it runs now (slower; reaches every machine).")),
             "{request_id, groups: [{group, release_id, image, environment, machines: [{resource, name, status, running, image, matches}]}]}.",
             ("NOT_FOUND", "PERMISSION_DENIED", "MACHINE_UNREACHABLE", "CLUSTER_UNREACHABLE"), "Any principal of the team that owns the request.", "read_only"),
    ToolSpec("rollback_release", "Roll back a release",
             "Start the previous release again on every machine of the group, or apply it again on the cluster. Offered only for the release running on its "
             "target while the previous one is kept: not for a target's first release, and not for one a rollback restored.",
             (RELEASE_ID, WAIT), RELEASE_OUT, ("NOT_FOUND", "PERMISSION_DENIED", "INVALID_STATE", "MACHINE_UNREACHABLE", "MACHINE_ERROR", "ROLLBACK_FAILED"),
             "Needs can_deploy in the release's environment.", "changes", FOLLOW),
    ToolSpec("remove_release", "Remove a release", "Stop and delete the release's containers, and the previous ones, from every machine of the group, or "
             "its Deployment, Service and Secrets from the cluster; the machines and the cluster stay as the infrastructure layer made them.",
             (RELEASE_ID, WAIT), RELEASE_OUT, ("NOT_FOUND", "PERMISSION_DENIED", "INVALID_STATE", "MACHINE_UNREACHABLE", "MACHINE_ERROR", "REMOVE_FAILED"),
             "Needs can_deploy in the release's environment.", "destructive", FOLLOW),
    ToolSpec("get_service_status", "Get the service status", "The service's version, the infrastructure layer it reads, and its environments with their rules.",
             (), "{status, version, cloudinfra, environments: {name: {service_account, allowed_registries, require_attestation, require_approval}}, moving}.", (),
             "Any principal.", "read_only"),
)
BY_NAME = {spec.name: spec for spec in CATALOG}


def render_markdown(catalog=CATALOG) -> str:
    """docs/DELIVERY-CATALOG.md, from the records."""
    lines = ["# Delivery service catalog", "",
             "Generated from `appdelivery/catalog.py` by `app-delivery catalog --write docs/DELIVERY-CATALOG.md`; a test keeps this file and the registered tools "
             f"equal to the records. {len(catalog)} tools, served at `/mcp` and as REST routes. Every result is a JSON object with `status`; a failed one is "
             '`{"status": "FAILED", "error": {"code": "…", "message": "…", "details": {…}}}`. Release ids are `rel-` plus 12 hex characters. Authentication: '
             "a bearer token of a configured principal. See [DELIVERY-SPEC.md](DELIVERY-SPEC.md).", "",
             "## At a glance", "", "| Tool | Purpose | Safety hint |", "|---|---|---|"]
    lines += [f"| `{spec.name}` | {spec.title} | {spec.hint.replace('_', ' ')} |" for spec in catalog]
    lines += ["", "## Error codes", "", "| Code | Meaning |", "|---|---|"]
    lines += [f"| `{code.value}` | {MEANINGS[code]} |" for code in ErrorCode]
    lines.append("")
    for spec in catalog:
        lines += [f"## `{spec.name}`", "", f"**Purpose.** {spec.purpose}", ""]
        lines.append("**Inputs.**" + (" None." if not spec.inputs else ""))
        if spec.inputs:
            lines += ["", "| Field | Required | Description |", "|---|---|---|"]
            lines += [f"| `{item.name}` | {'yes' if item.required else 'no'} | {item.description} |" for item in spec.inputs]
        lines += ["", f"**Output.** {spec.output}", "", f"**Possible errors.** {', '.join('`' + code + '`' for code in spec.errors) or 'none beyond authentication'}.",
                  "", f"**Security.** {HINT_WORDS[spec.hint]} {spec.security}", ""]
        if spec.follow:
            lines += [f"**After the call.** {spec.follow}", ""]
    return "\n".join(lines)
