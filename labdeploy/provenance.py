"""Build provenance where GitHub cannot attest: a SLSA v1 statement Cosign signs with the application's Cloud KMS key, and
the check that a signature-verified statement names this image, this repository, this commit and this workflow.

GitHub's artifact attestations cover public repositories (private ones only on GitHub Enterprise Cloud). A private
repository's release signs instead with a key in the foundation's ``lab-deploy`` key ring that only its own deployer may
use, and that deployer is reachable only from the deploy workflows on main in the staging environment (the service
module's federation condition). Standard library only: the deploy job runs it from the platform checkout.

    python -m labdeploy.provenance key           the signing key's Cosign URI, from GCP_PROJECT_ID, GCP_REGION and SERVICE
    python -m labdeploy.provenance predicate     the SLSA predicate for this run, from the workflow's environment
    python -m labdeploy.provenance verify FILE   check `cosign verify-attestation` output against IMAGE_NAME, IMAGE_SHA256, COMMIT_SHA
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
from pathlib import Path

# GitHub's own build type for workflow runs, so a reader of either kind of provenance sees the same fields.
BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
STATEMENT_TYPES = ("https://in-toto.io/Statement/v0.1", "https://in-toto.io/Statement/v1")
PAYLOAD_TYPE = "application/vnd.in-toto+json"
WORKFLOW_PATH = ".github/workflows/deploy-gcp.yml"
REF = "refs/heads/main"
KEY_RING = "lab-deploy"
# github: GitHub's attestation (public repositories). cosign-kms: this module's path. unavailable: the operator's exception.
MODES = ("github", "cosign-kms", "unavailable")

COMMIT = re.compile(r"^[a-f0-9]{40}$")
PLAN = re.compile(r"^[a-f0-9]{32}$")
SPEC_HASH = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SERVER = re.compile(r"^https://[A-Za-z0-9.-]+$")
NAME = re.compile(r"^[a-z][a-z0-9-]+$")
NUMBER = re.compile(r"^[0-9]{1,20}$")
IMAGE_NAME = re.compile(r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]+/[a-z][a-z0-9-]+/[a-z][a-z0-9-]+$")


class ProvenanceError(Exception):
    pass


def _require(pattern, value, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProvenanceError(f"Invalid {what}: {value!r}")
    return value


def key_uri(project, region, service):
    """The application's signing key: one per app in the foundation's key ring, created and granted by the service module.
    Version 1 is the version the key is created with; pinning it keeps signing and verifying on the same key material."""
    for value, what in ((project, "project"), (region, "region"), (service, "service")):
        _require(NAME, value, what)
    return f"gcpkms://projects/{project}/locations/{region}/keyRings/{KEY_RING}/cryptoKeys/{service}-provenance/cryptoKeyVersions/1"


def predicate(*, repository, commit, plan_id, spec_hash, run_id, run_attempt, repository_id, owner_id, workflow_ref,
              server="https://github.com", event_name="workflow_dispatch", runner_environment="github-hosted"):
    """What the release claims about the image: which repository and commit, through which workflow and run."""
    _require(REPOSITORY, repository, "repository")
    _require(COMMIT, commit, "commit")
    _require(PLAN, plan_id, "plan id")
    _require(SPEC_HASH, spec_hash, "spec hash")
    _require(SERVER, server, "server URL")
    for value, what in ((run_id, "run id"), (run_attempt, "run attempt"), (repository_id, "repository id"), (owner_id, "owner id")):
        _require(NUMBER, value, what)
    # The statement only ever names the workflow that is really running, so it can never claim a builder it is not.
    if workflow_ref != f"{repository}/{WORKFLOW_PATH}@{REF}":
        raise ProvenanceError(f"Provenance is signed only by {WORKFLOW_PATH} on main; this run is {workflow_ref!r}")
    source = f"{server}/{repository}"
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {"workflow": {"ref": REF, "repository": source, "path": WORKFLOW_PATH}},
            "internalParameters": {
                "github": {"event_name": event_name, "repository_id": repository_id, "repository_owner_id": owner_id,
                           "runner_environment": runner_environment},
                "lab_deploy": {"plan_id": plan_id, "spec_hash": spec_hash},
            },
            "resolvedDependencies": [{"uri": f"git+{source}@{REF}", "digest": {"gitCommit": commit}}],
        },
        "runDetails": {
            "builder": {"id": f"{source}/{WORKFLOW_PATH}@{REF}"},
            "metadata": {"invocationId": f"{source}/actions/runs/{run_id}/attempts/{run_attempt}"},
        },
    }


def _mismatch(statement, *, image_name, digest, source, commit):
    """Why a statement does not describe this image from this commit and workflow, or None when it does."""
    if not isinstance(statement, dict) or statement.get("_type") not in STATEMENT_TYPES:
        return "not an in-toto statement"
    if statement.get("predicateType") != PREDICATE_TYPE:
        return "not SLSA v1 provenance"
    subjects = statement.get("subject") if isinstance(statement.get("subject"), list) else []
    if not any(isinstance(s, dict) and s.get("name") == image_name and isinstance(s.get("digest"), dict)
               and s["digest"].get("sha256") == digest.removeprefix("sha256:") for s in subjects):
        return "names a different image"
    body = statement.get("predicate") if isinstance(statement.get("predicate"), dict) else {}
    build = body.get("buildDefinition") if isinstance(body.get("buildDefinition"), dict) else {}
    run = body.get("runDetails") if isinstance(body.get("runDetails"), dict) else {}
    if build.get("buildType") != BUILD_TYPE:
        return "an unknown build type"
    if (build.get("externalParameters") or {}).get("workflow") != {"ref": REF, "repository": source, "path": WORKFLOW_PATH}:
        return "built by a different repository, workflow or branch"
    dependencies = build.get("resolvedDependencies") if isinstance(build.get("resolvedDependencies"), list) else []
    if not any(isinstance(d, dict) and d.get("uri") == f"git+{source}@{REF}" and isinstance(d.get("digest"), dict)
               and d["digest"].get("gitCommit") == commit for d in dependencies):
        return "built from a different commit"
    if (run.get("builder") or {}).get("id") != f"{source}/{WORKFLOW_PATH}@{REF}":
        return "a different builder"
    return None


def verify(output, *, image_name, digest, repository, commit, server="https://github.com"):
    """The statement from `cosign verify-attestation` output that describes this image, repository and commit.

    Cosign prints only envelopes whose signature the application's key verified; this checks what they say. One matching
    statement is enough (a reused image can carry several, all signed by the same key); none raises."""
    _require(IMAGE_NAME, image_name, "image name")
    _require(DIGEST, digest, "image digest")
    _require(REPOSITORY, repository, "repository")
    _require(COMMIT, commit, "commit")
    _require(SERVER, server, "server URL")
    source = f"{server}/{repository}"
    reasons = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict) or envelope.get("payloadType") != PAYLOAD_TYPE:
                reasons.append("not an in-toto envelope")
                continue
            statement = json.loads(base64.b64decode(envelope["payload"], validate=True))
        except (ValueError, KeyError, TypeError, binascii.Error):
            reasons.append("unreadable")
            continue
        reason = _mismatch(statement, image_name=image_name, digest=digest, source=source, commit=commit)
        if reason is None:
            return statement
        reasons.append(reason)
    found = "; ".join(sorted(set(reasons))) if reasons else "Cosign returned no verified attestation"
    raise ProvenanceError(f"No signed provenance shows {image_name}@{digest} was built from {repository} at {commit[:12]} by {WORKFLOW_PATH} on main ({found})")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    env = os.environ
    try:
        if args == ["key"]:
            print(key_uri(env.get("GCP_PROJECT_ID"), env.get("GCP_REGION"), env.get("SERVICE")))
        elif args == ["predicate"]:
            print(json.dumps(predicate(
                repository=env.get("GITHUB_REPOSITORY"), commit=env.get("COMMIT_SHA"), plan_id=env.get("PLAN_ID"),
                spec_hash=env.get("SPEC_HASH"), run_id=env.get("GITHUB_RUN_ID"), run_attempt=env.get("GITHUB_RUN_ATTEMPT"),
                repository_id=env.get("GITHUB_REPOSITORY_ID"), owner_id=env.get("GITHUB_REPOSITORY_OWNER_ID"),
                workflow_ref=env.get("GITHUB_WORKFLOW_REF"), server=env.get("GITHUB_SERVER_URL", "https://github.com"),
                event_name=env.get("GITHUB_EVENT_NAME", "workflow_dispatch"), runner_environment=env.get("RUNNER_ENVIRONMENT", "github-hosted"),
            ), indent=2, sort_keys=True))
        elif len(args) == 2 and args[0] == "verify":
            statement = verify(Path(args[1]).read_text(), image_name=env.get("IMAGE_NAME"), digest=env.get("IMAGE_SHA256"),
                               repository=env.get("GITHUB_REPOSITORY"), commit=env.get("COMMIT_SHA"),
                               server=env.get("GITHUB_SERVER_URL", "https://github.com"))
            run = statement["predicate"]["runDetails"].get("metadata", {}).get("invocationId", "an earlier run")
            print(f"Provenance verified: {env['IMAGE_NAME']}@{env['IMAGE_SHA256']} was built from {env['GITHUB_REPOSITORY']} at "
                  f"{env['COMMIT_SHA'][:12]} by {WORKFLOW_PATH} on main ({run}); signed with the application's Cloud KMS key")
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except ProvenanceError as exc:
        print(f"::error title=Provenance::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
