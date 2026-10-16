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
WORKFLOW_PATH = ".github/workflows/deploy-gcp.yml"  # a repository's first service; a further one has deploy-gcp-<service>.yml
# deploy.yml is the platform repository's own pipeline, which builds applications whose contract it keeps (docs/ROADMAP.md D33).
DEPLOY_WORKFLOW = re.compile(r"^deploy(-gcp(-[a-z][a-z0-9-]{1,19}[a-z0-9])?)?\.yml$")
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


def workflow_path(name=None):
    """The deploy workflow's path in its repository, from the rendered workflow's DEPLOY_WORKFLOW; the plain name by default."""
    if not name:
        return WORKFLOW_PATH
    if not DEPLOY_WORKFLOW.fullmatch(name):
        raise ProvenanceError(f"Unexpected deploy workflow name {name!r}")
    return f".github/workflows/{name}"


INPUT_KEYS = {"recipe": re.compile(r"^(repository|platform|buildpacks)$"), "dockerfile_sha256": re.compile(r"^[a-f0-9]{64}$"),
              "lock_sha256": re.compile(r"^[a-f0-9]{64}$"), "lock_source": re.compile(r"^(repository|platform)$"),
              "builder": re.compile(r"^gcr\.io/buildpacks/builder@sha256:[a-f0-9]{64}$")}


def build_inputs(data):
    """The build job's record of what the image was made from, kept only where every value has the expected form."""
    if not isinstance(data, dict):
        return None
    inputs = {key: value for key, value in data.items() if key in INPUT_KEYS and isinstance(value, str) and INPUT_KEYS[key].fullmatch(value)}
    return inputs or None


def predicate(*, repository, commit, plan_id, spec_hash, run_id, run_attempt, repository_id, owner_id, workflow_ref,
              server="https://github.com", event_name="workflow_dispatch", runner_environment="github-hosted", path=WORKFLOW_PATH,
              source_repository=None, contract_commit=None, application=None, inputs=None):
    """What the release claims about the image: which repository and commit, through which workflow and run. When the
    platform's own pipeline builds an application (docs/ROADMAP.md D33), the builder is the platform repository and the
    source is the application's: `source_repository` names it, `contract_commit` the platform commit the contract came from."""
    _require(REPOSITORY, repository, "repository")
    _require(COMMIT, commit, "commit")
    _require(PLAN, plan_id, "plan id")
    _require(SPEC_HASH, spec_hash, "spec hash")
    _require(SERVER, server, "server URL")
    for value, what in ((run_id, "run id"), (run_attempt, "run attempt"), (repository_id, "repository id"), (owner_id, "owner id")):
        _require(NUMBER, value, what)
    # The statement only ever names the workflow that is really running, so it can never claim a builder it is not.
    if workflow_ref != f"{repository}/{path}@{REF}":
        raise ProvenanceError(f"Provenance is signed only by {path} on main; this run is {workflow_ref!r}")
    builder = f"{server}/{repository}"
    source = builder
    lab_deploy = {"plan_id": plan_id, "spec_hash": spec_hash}
    if source_repository is not None:
        _require(REPOSITORY, source_repository, "source repository")
        _require(COMMIT, contract_commit, "contract commit")
        _require(NAME, application, "application")
        source = f"{server}/{source_repository}"
        lab_deploy.update(application=application, contract={"repository": builder, "commit": contract_commit,
                                                              "path": f"apps/{application}/platform.yaml"})
    inputs = build_inputs(inputs)
    if inputs:
        lab_deploy["inputs"] = inputs  # docs/ROADMAP.md D33: the recipe, the Dockerfile's and the lock's hashes
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {"workflow": {"ref": REF, "repository": builder, "path": path}},
            "internalParameters": {
                "github": {"event_name": event_name, "repository_id": repository_id, "repository_owner_id": owner_id,
                           "runner_environment": runner_environment},
                "lab_deploy": lab_deploy,
            },
            "resolvedDependencies": [{"uri": f"git+{source}@{REF}", "digest": {"gitCommit": commit}}],
        },
        "runDetails": {
            "builder": {"id": f"{builder}/{path}@{REF}"},
            "metadata": {"invocationId": f"{builder}/actions/runs/{run_id}/attempts/{run_attempt}"},
        },
    }


def _mismatch(statement, *, image_name, digest, source, commit, path=WORKFLOW_PATH, builder=None, contract_commit=None):
    """Why a statement does not describe this image from this commit and workflow, or None when it does."""
    builder = builder or source
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
    if (build.get("externalParameters") or {}).get("workflow") != {"ref": REF, "repository": builder, "path": path}:
        return "built by a different repository, workflow or branch"
    dependencies = build.get("resolvedDependencies") if isinstance(build.get("resolvedDependencies"), list) else []
    if not any(isinstance(d, dict) and d.get("uri") == f"git+{source}@{REF}" and isinstance(d.get("digest"), dict)
               and d["digest"].get("gitCommit") == commit for d in dependencies):
        return "built from a different commit"
    if (run.get("builder") or {}).get("id") != f"{builder}/{path}@{REF}":
        return "a different builder"
    if contract_commit is not None:
        contract = ((build.get("internalParameters") or {}).get("lab_deploy") or {}).get("contract") or {}
        if contract.get("commit") != contract_commit or contract.get("repository") != builder:
            return "made from a different contract"
    return None


def verify(output, *, image_name, digest, repository, commit, server="https://github.com", path=WORKFLOW_PATH,
           source_repository=None, contract_commit=None):
    """The statement from `cosign verify-attestation` output that describes this image, repository and commit.

    Cosign prints only envelopes whose signature the application's key verified; this checks what they say. One matching
    statement is enough (a reused image can carry several, all signed by the same key); none raises. With a
    `source_repository`, the builder is `repository` (the platform) and the image must come from the source at `commit`
    through the contract at `contract_commit`."""
    _require(IMAGE_NAME, image_name, "image name")
    _require(DIGEST, digest, "image digest")
    _require(REPOSITORY, repository, "repository")
    _require(COMMIT, commit, "commit")
    _require(SERVER, server, "server URL")
    builder = f"{server}/{repository}"
    source = builder
    if source_repository is not None:
        _require(REPOSITORY, source_repository, "source repository")
        _require(COMMIT, contract_commit, "contract commit")
        source = f"{server}/{source_repository}"
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
        reason = _mismatch(statement, image_name=image_name, digest=digest, source=source, commit=commit, path=path, builder=builder,
                           contract_commit=contract_commit if source_repository is not None else None)
        if reason is None:
            return statement
        reasons.append(reason)
    found = "; ".join(sorted(set(reasons))) if reasons else "Cosign returned no verified attestation"
    raise ProvenanceError(f"No signed provenance shows {image_name}@{digest} was built from {source_repository or repository} at {commit[:12]} "
                          f"by {repository}/{path} on main ({found})")


def read_inputs(name):
    """bundle/inputs.json from the build job, or None (a reused image has no bundle; its first release signed them)."""
    if not name or not Path(name).is_file():
        return None
    try:
        return json.loads(Path(name).read_text())
    except ValueError:
        return None


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    env = os.environ
    try:
        path = workflow_path(env.get("DEPLOY_WORKFLOW"))
        if args == ["key"]:
            print(key_uri(env.get("GCP_PROJECT_ID"), env.get("GCP_REGION"), env.get("SERVICE")))
        elif args == ["predicate"]:
            print(json.dumps(predicate(
                repository=env.get("GITHUB_REPOSITORY"), commit=env.get("COMMIT_SHA"), plan_id=env.get("PLAN_ID"),
                spec_hash=env.get("SPEC_HASH"), run_id=env.get("GITHUB_RUN_ID"), run_attempt=env.get("GITHUB_RUN_ATTEMPT"),
                repository_id=env.get("GITHUB_REPOSITORY_ID"), owner_id=env.get("GITHUB_REPOSITORY_OWNER_ID"),
                workflow_ref=env.get("GITHUB_WORKFLOW_REF"), server=env.get("GITHUB_SERVER_URL", "https://github.com"),
                event_name=env.get("GITHUB_EVENT_NAME", "workflow_dispatch"), runner_environment=env.get("RUNNER_ENVIRONMENT", "github-hosted"),
                path=path, source_repository=env.get("SOURCE_REPOSITORY") or None, contract_commit=env.get("CONTRACT_COMMIT") or None,
                application=env.get("APPLICATION") or None, inputs=read_inputs(env.get("INPUTS_FILE"))), indent=2, sort_keys=True))
        elif len(args) == 2 and args[0] == "verify":
            statement = verify(Path(args[1]).read_text(), image_name=env.get("IMAGE_NAME"), digest=env.get("IMAGE_SHA256"),
                               repository=env.get("GITHUB_REPOSITORY"), commit=env.get("COMMIT_SHA"),
                               server=env.get("GITHUB_SERVER_URL", "https://github.com"), path=path,
                               source_repository=env.get("SOURCE_REPOSITORY") or None, contract_commit=env.get("CONTRACT_COMMIT") or None)
            run = statement["predicate"]["runDetails"].get("metadata", {}).get("invocationId", "an earlier run")
            built_from = env.get("SOURCE_REPOSITORY") or env["GITHUB_REPOSITORY"]
            print(f"Provenance verified: {env['IMAGE_NAME']}@{env['IMAGE_SHA256']} was built from {built_from} at "
                  f"{env['COMMIT_SHA'][:12]} by {env['GITHUB_REPOSITORY']}/{path} on main ({run}); signed with the application's Cloud KMS key")
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except ProvenanceError as exc:
        print(f"::error title=Provenance::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
