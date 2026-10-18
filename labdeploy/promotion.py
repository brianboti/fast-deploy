"""Promotion to production (docs/ROADMAP.md D37, roadmap item 16.2): the exact image a staging release deployed, copied by
digest into production's own registry once its provenance is checked, and released by the same runner after a reviewer
approved it in the platform repository's environment production-<service>. Nothing is ever built for production.

The promote workflow (.github/workflows/promote.yml) runs it from the platform checkout:

    python -m labdeploy.promotion check            refuse malformed dispatch inputs, from the workflow's environment
    python -m labdeploy.promotion summary          the approval block a reviewer reads on the run before approving (Markdown)
    python -m labdeploy.promotion approvals FILE   who approved this run in its environment, from GitHub's approvals list
    python -m labdeploy.promotion verify FILE      check `cosign verify-attestation` output for the staging image
    python -m labdeploy.promotion predicate        the verification summary production's key signs for the copied image
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

from .provenance import COMMIT, DEPLOY_WORKFLOW, PLAN, REPOSITORY, SPEC_HASH, ProvenanceError, verify, workflow_path

# SLSA's verification summary: who checked which provenance against what, with what result. The promotion's own record of
# the approval and the source travels in its lab_deploy section.
PREDICATE_TYPE = "https://slsa.dev/verification_summary/v1"
WORKFLOW = "promote.yml"
SERVICE = re.compile(r"^[a-z][a-z0-9-]{1,19}[a-z0-9]$")
SOURCE_IMAGE = re.compile(r"^(us-central1)-docker\.pkg\.dev/([a-z][a-z0-9-]{4,28}[a-z0-9])/([a-z][a-z0-9-]{2,62})/([a-z][a-z0-9-]{2,20})@(sha256:[a-f0-9]{64})$")
APP_PATH = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7}$")
REVISION = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
STAGING_MODES = ("github", "cosign-kms")  # an image released under the operator's exception carries no provenance: never promoted
MAX_BAKE_SECONDS = 900


class PromotionError(Exception):
    pass


def source_image(value):
    """(project, registry, service, digest) of a staging image reference by digest, or PromotionError."""
    found = SOURCE_IMAGE.fullmatch(value or "")
    if found is None:
        raise PromotionError(f"The staging image must be an Artifact Registry image by digest in us-central1, not {value!r}")
    return found.group(2), found.group(3), found.group(4), found.group(5)


def target_of(env, service):
    try:
        target = json.loads(env.get("TARGET_JSON", ""))
    except ValueError:
        raise PromotionError("The production target is not JSON") from None
    if not isinstance(target, dict) or target.get("service") != service:
        raise PromotionError("The production target is for another service")
    return target


def split_of(text):
    """A traffic split as a receipt records it ({"revision": percent}), checked; None when not given."""
    if not text:
        return None
    try:
        split = json.loads(text)
    except ValueError:
        raise PromotionError("rollback_to is not JSON") from None
    if (not isinstance(split, dict) or not split or sum(split.values()) != 100
            or any(not REVISION.fullmatch(str(name)) or type(share) is not int or not 0 < share <= 100 for name, share in split.items())):
        raise PromotionError("rollback_to is a traffic split of revisions that adds up to 100")
    return split


def check(env):
    """The dispatch inputs, checked before anything else runs. A promotion: the shapes, an image of this service in another
    project than production's, a provenance this pipeline can verify, and the split's steps. A rollback: which promotion
    it undoes. Returns the facts the later steps use."""
    def need(pattern, name, what):
        value = env.get(name, "")
        if not pattern.fullmatch(value):
            raise PromotionError(f"Invalid {what}: {value!r}")
        return value
    operation = env.get("OPERATION") or "promote"
    if operation not in ("promote", "rollback"):
        raise PromotionError(f"The operation is promote or rollback, not {operation!r}")
    plan = need(PLAN, "PLAN_ID", "plan id")
    service = need(SERVICE, "APPLICATION", "application")
    if env.get("GITHUB_REF") != "refs/heads/main":
        raise PromotionError("Promotions and rollbacks run from main only")
    if operation == "rollback":
        undone = need(PLAN, "ROLLBACK_PLAN", "promotion to roll back")
        return {"operation": "rollback", "plan": plan, "service": service, "rollback_plan": undone, "target": target_of(env, service),
                "rollback_to": split_of(env.get("ROLLBACK_TO", ""))}
    source_plan = need(PLAN, "SOURCE_PLAN", "staging plan id")
    commit = need(COMMIT, "COMMIT_SHA", "commit")
    need(SPEC_HASH, "SPEC_HASH", "spec hash")
    repository = need(REPOSITORY, "SOURCE_REPOSITORY", "application repository")
    path = env.get("APP_PATH", "")
    if path and (not APP_PATH.fullmatch(path) or ".." in path):
        raise PromotionError(f"Invalid application folder: {path!r}")
    contract = env.get("CONTRACT_COMMIT", "")
    if contract and not COMMIT.fullmatch(contract):
        raise PromotionError(f"Invalid contract commit: {contract!r}")
    workflow = env.get("STAGING_WORKFLOW", "")
    if not DEPLOY_WORKFLOW.fullmatch(workflow):
        raise PromotionError(f"Invalid staging workflow: {workflow!r}")
    mode = env.get("PROVENANCE_INPUT", "")
    if mode not in STAGING_MODES:
        raise PromotionError("Only an image with signed provenance is promoted (github or cosign-kms); one released under the operator's exception is not")
    if contract and (workflow != "deploy.yml" or mode != "cosign-kms"):
        raise PromotionError("An application whose contract the platform keeps was released by deploy.yml and signed with its Cloud KMS key")
    attested = env.get("ATTESTED_COMMIT", "")
    if attested and (not COMMIT.fullmatch(attested) or mode != "github"):
        raise PromotionError("attested_commit is the full commit GitHub's attestation names, for an image GitHub attested")
    bake = env.get("BAKE_SECONDS", "")
    if not re.fullmatch(r"[0-9]{1,3}", bake) or int(bake) > MAX_BAKE_SECONDS:
        raise PromotionError(f"bake_seconds is a whole number from 0 to {MAX_BAKE_SECONDS}")
    steps = env.get("ROLLOUT_STEPS") or "100"
    try:
        rollout = [int(part) for part in steps.split(",")]
    except ValueError:
        rollout = []
    if not rollout or rollout[-1] != 100 or rollout != sorted(set(rollout)) or any(not 0 < step <= 100 for step in rollout):
        raise PromotionError("rollout is a rising list of traffic percents that ends at 100, such as 10,50,100")
    target = target_of(env, service)
    project, registry, image_service, digest = source_image(env.get("SOURCE_IMAGE", ""))
    if image_service != service:
        raise PromotionError(f"The staging image belongs to {image_service}, not {service}")
    if project == target.get("project_id"):
        raise PromotionError("The staging image is already in the production project: a promotion copies from staging")
    return {"operation": "promote", "plan": plan, "source_plan": source_plan, "service": service, "commit": commit, "repository": repository,
            "contract": contract or None, "workflow": workflow, "mode": mode, "staging_project": project, "staging_registry": registry, "digest": digest,
            "target": target, "bake_seconds": int(bake), "attested_commit": attested or None, "rollout": rollout}


def approvers(approvals, environment):
    """The GitHub accounts that approved this run in `environment`, from GET /repos/{repo}/actions/runs/{id}/approvals."""
    names = []
    for item in approvals if isinstance(approvals, list) else []:
        if not isinstance(item, dict) or item.get("state") != "approved":
            continue
        if not any(isinstance(e, dict) and e.get("name") == environment for e in item.get("environments") or []):
            continue
        login = (item.get("user") or {}).get("login")
        if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login) and login not in names:
            names.append(login)
    return names


def verify_staging(output, env):
    """The signature-verified statement that shows the staging image was built from this commit by the workflow that
    released it: an application's own deploy workflow, or the platform pipeline for one whose contract it keeps."""
    facts = check(env)
    project, registry, service, digest = facts["staging_project"], facts["staging_registry"], facts["service"], facts["digest"]
    image_name = f"us-central1-docker.pkg.dev/{project}/{registry}/{service}"
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    if facts["contract"]:
        # The platform pipeline built it: the builder is this repository, the source the application's (D33).
        return verify(output, image_name=image_name, digest=digest, repository=env.get("GITHUB_REPOSITORY"), commit=facts["commit"], server=server,
                      path=workflow_path(facts["workflow"]), source_repository=facts["repository"], contract_commit=facts["contract"])
    return verify(output, image_name=image_name, digest=digest, repository=facts["repository"], commit=facts["commit"], server=server,
                  path=workflow_path(facts["workflow"]))


def predicate(env, now=None):
    """What production's key signs for the copied image: that this run verified the staging image's provenance and a
    reviewer approved the promotion, in SLSA's verification summary form."""
    facts = check(env)
    approved = [name for name in (env.get("APPROVED_BY") or "").split(",") if name]
    if not approved:
        raise PromotionError("No approval recorded for this run; a promotion is signed only after a reviewer approved it")
    server, platform, run = env.get("GITHUB_SERVER_URL", "https://github.com"), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
    if not REPOSITORY.fullmatch(platform) or not re.fullmatch(r"[0-9]{1,20}", run):
        raise PromotionError("The run's repository and id are needed for the statement")
    source = f"us-central1-docker.pkg.dev/{facts['staging_project']}/{facts['staging_registry']}/{facts['service']}@{facts['digest']}"
    moment = (now or datetime.datetime.now(datetime.UTC)).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "verifier": {"id": f"{server}/{platform}/.github/workflows/{WORKFLOW}@refs/heads/main"},
        "timeVerified": moment,
        "resourceUri": f"oci://{source}",
        "policy": {"uri": f"{server}/{platform}/blob/main/docs/ROADMAP.md"},
        "inputAttestations": [],
        "verificationResult": "PASSED",
        "verifiedLevels": ["SLSA_BUILD_LEVEL_2"],
        "slsaVersion": "1.0",
        "lab_deploy": {"plan_id": facts["plan"], "application": facts["service"], "commit": facts["commit"], "spec_hash": env["SPEC_HASH"],
                       "source": {"image": source, "plan_id": facts["source_plan"], "provenance": facts["mode"], "repository": facts["repository"],
                                  "workflow": facts["workflow"], "contract_commit": facts["contract"], "attested_commit": facts["attested_commit"]},
                       "approved_by": approved, "environment": f"production-{facts['service']}",
                       "run": f"{server}/{platform}/actions/runs/{run}"},
    }


def summary(env, spec=None, cost=None):
    """The approval block a reviewer reads on the run before approving, written by code from the checked inputs."""
    facts = check(env)
    if facts["operation"] == "rollback":
        split = facts["rollback_to"]
        back = ", ".join(f"`{name}` {share}%" for name, share in sorted(split.items(), key=lambda pair: -pair[1])) if split else "the split before it"
        rows = [("Undoes", f"promotion `{facts['rollback_plan'][:8]}`, the release production serves now"),
                ("Traffic returns to", f"{back}, as that promotion's checkpoint recorded"),
                ("How", "traffic only, in seconds; nothing is built or copied, and it is refused if production's traffic moved since"),
                ("Where", f"production project `{facts['target'].get('project_id')}`, service `{facts['service']}`")]
        lines = [f"### Roll `{facts['service']}` back in production", "", "| | |", "|---|---|"]
        lines += [f"| {name} | {value} |" for name, value in rows]
        lines += ["", "Approving moves production's traffic back; data the newer version wrote stays. Rejecting changes nothing."]
        return "\n".join(lines) + "\n"
    short = facts["digest"].removeprefix("sha256:")[:12]
    from_where = (f"the platform's pipeline, from {facts['repository']} through the contract at `{facts['contract'][:7]}`" if facts["contract"]
                  else f"{facts['repository']}'s own {facts['workflow']}")
    rows = [("Application", f"`{facts['service']}`, commit `{facts['commit'][:7]}` of {facts['repository']}"),
            ("Image", f"`sha256:{short}…`, the exact image staging release `{facts['source_plan'][:8]}` deployed, copied by digest and never rebuilt"),
            ("Built by", from_where),
            ("Provenance", "GitHub's attestation" if facts["mode"] == "github" else "signed with the application's Cloud KMS key in staging"),
            ("From", f"staging project `{facts['staging_project']}`"),
            ("To", f"production project `{facts['target'].get('project_id')}`, service `{facts['service']}`"),
            ("Released in steps", " then ".join(f"{step}%" for step in facts["rollout"]) + f" of the traffic, each watched {facts['bake_seconds']} "
                                  "seconds; a crash, errors or slow answers at any step bring the previous version back for everyone")]
    if spec is not None:
        rows.append(("Size", f"{spec.resources.cpu} CPU, {spec.resources.memory_mib} MiB, {spec.scaling.minimum} to {spec.scaling.maximum} instances, "
                             f"exposure {spec.exposure}"))
    if cost is not None:
        rows.append(("Cost in production", f"idle ${cost['idle_floor_usd_month']:.2f}, at most ${cost['compute_ceiling_usd_month']:.2f} a month for "
                                           f"compute, plus ${cost['needs_floor_usd_month']:.2f} for its needs (list prices, not a bill)"))
    lines = [f"### Promote `{facts['service']}` to production", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines += ["", "Approving copies the image into production and releases it there; rejecting changes nothing."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    env = os.environ
    try:
        if args == ["check"]:
            facts = check(env)
            if facts["operation"] == "rollback":
                print(f"Rollback inputs checked: {facts['service']}, undoing promotion {facts['rollback_plan'][:8]}")
            else:
                print(f"Promotion inputs checked: {facts['service']} at {facts['commit'][:12]}, {facts['digest']} from {facts['staging_project']}")
        elif args == ["summary"]:
            spec = cost = None
            if env.get("SPEC_FILE") and os.path.isfile(env["SPEC_FILE"]):
                from pathlib import Path

                from .cost import estimate
                from .models import parse_spec
                spec = parse_spec(Path(env["SPEC_FILE"]).read_text())
                cost = estimate(spec)
            print(summary(env, spec, cost))
        elif len(args) == 2 and args[0] == "approvals":
            from pathlib import Path
            names = approvers(json.loads(Path(args[1]).read_text()), env.get("ENVIRONMENT", ""))
            if not names:
                raise PromotionError(f"Nobody approved this run in {env.get('ENVIRONMENT')!r}: a promotion starts only after a reviewer's approval")
            print(",".join(names))
        elif len(args) == 2 and args[0] == "verify":
            from pathlib import Path
            statement = verify_staging(Path(args[1]).read_text(), env)
            run = statement["predicate"]["runDetails"].get("metadata", {}).get("invocationId", "an earlier run")
            print(f"Staging provenance verified: {env['SOURCE_IMAGE']} was built from {env['SOURCE_REPOSITORY']} at {env['COMMIT_SHA'][:12]} ({run})")
        elif args == ["predicate"]:
            print(json.dumps(predicate(env), indent=2, sort_keys=True))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (PromotionError, ProvenanceError) as exc:
        print(f"::error title=Promotion::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
