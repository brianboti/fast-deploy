"""The images of the jobs a service starts itself (docs/ROADMAP.md D64, 27.3, 27.4), built and checked like the service's.

The release workflow calls it in three places:

    python -m labdeploy.jobimages read SPEC [--base JSON]
        the jobs as KEY=value lines for $GITHUB_OUTPUT: whether there are any, and the application whose test image they
        build on (D69), checked against the plan's `base` input
    python -m labdeploy.jobimages build SPEC --app-dir DIR --app-path PATH --out DIR --trust T [--network none] [--base-image TAG]
        the build job, without cloud credentials: for each job, the Dockerfile policy and hadolint, the image built with
        no cache (BASE set to the test image when it has one), saved into the bundle, scanned for keys and given its parts list
    python -m labdeploy.jobimages push SPEC --bundle DIR --image-prefix PREFIX --mode MODE [--key KEY]
        the deploy job: each image's data files inspected, pushed as <service>-<job> under its commit and release tags,
        signed with the application's Cloud KMS key (provenance and parts list), verified, and written to
        job-images.json, which the release reads
    python -m labdeploy.jobimages scan
        the vulnerability summary of each image in job-images.json, under the contract's trust, as for the service's

Every tool is the one the service's own steps use, downloaded by those steps and found under $RUNNER_TEMP.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .models import AppSpec, parse_spec
from .provenance import ProvenanceError

BASE_INPUT = {"service": re.compile(r"^[a-z][a-z0-9-]{1,19}[a-z0-9]$"), "repository": re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
              "commit": re.compile(r"^[a-f0-9]{40}$"), "path": re.compile(r"^([A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7})?$")}
MAX_CONTEXT_KIB = 1048576  # 14.2: a build context over 1 GiB is refused; data belongs in a bucket
IMAGES_FILE = "job-images.json"
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


def read_spec(path):
    """The reviewed specification the workflow already validated: platform.yaml, or the contract's copy."""
    return parse_spec(Path(path).read_text())


def base_of(spec: AppSpec):
    """The one application the jobs build on, or None. Every job that names a base names the same one (D69)."""
    bases = sorted({job.base for job in spec.jobs.values() if job.base})
    if len(bases) > 1:
        raise ValueError(f"The jobs build on {', '.join(bases)}: a service's jobs build on one application's test image at most")
    return bases[0] if bases else None


def check_base(spec, text):
    """The plan's `base` input (JSON): the application named, its repository, folder and the commit its last release deployed.
    Empty when no job has a base; refused when it names another application than the contract does."""
    wanted = base_of(spec)
    if not text:
        if wanted:
            raise ValueError(f"The jobs build on {wanted}'s test image, and the plan names no released commit of it: release {wanted} first")
        return None
    base = json.loads(text)
    if not isinstance(base, dict) or set(base) != set(BASE_INPUT):
        raise ValueError("The plan's base names service, repository, path and commit")
    for key, pattern in BASE_INPUT.items():
        if not isinstance(base[key], str) or not pattern.fullmatch(base[key]) or ".." in base[key].split("/"):
            raise ValueError(f"The plan's base has an unexpected {key}")
    if base["service"] != wanted:
        raise ValueError(f"The plan's base is {base['service']}, and the contract's jobs build on {wanted or 'nothing'}")
    return base


def jobs_of(spec, app_dir, app_path="."):
    """Each job with its Dockerfile in the checkout, the folder that is its build context, and its path as the repository names it."""
    items = []
    for name, job in spec.jobs.items():
        dockerfile = Path(app_dir) / job.dockerfile
        shown = os.path.normpath(os.path.join(app_path or ".", job.dockerfile))
        items.append({"name": name, "dockerfile": dockerfile, "context": dockerfile.parent, "shown": shown, "base": bool(job.base)})
    return items


def context_kib(folder):
    total = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = Path(root) / name
            if not path.is_symlink():
                total += path.stat().st_size
    return total // 1024


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def summary_line(text):
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text + "\n")


def build(spec, app_dir, app_path, out, trust, network="default", base_image="", commit="", tools=None, runner=run, base=None):
    """The build job's part (27.3): every job's image built and checked like the service's. Returns the number of refusals."""
    from . import dockerpolicy, leaks, sbom
    from .scaffold import deny_dockerignore
    tools = tools or Path(os.environ.get("RUNNER_TEMP", "."))
    commit = commit or os.environ.get("COMMIT_SHA", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("COMMIT_SHA is the full commit")
    refused = 0
    for job in jobs_of(spec, app_dir, app_path):
        name, dockerfile, context = job["name"], job["dockerfile"], job["context"]
        folder = Path(out) / name
        folder.mkdir(parents=True, exist_ok=True)
        if dockerfile.is_symlink() or not dockerfile.is_file():
            print(f"::error title=Job {name}::{job['shown']} does not exist in the repository at this commit")
            refused += 1
            continue
        if not context.resolve().is_relative_to(Path(app_dir).resolve()):
            print(f"::error title=Job {name}::{job['shown']} lies outside the service's folder")
            refused += 1
            continue
        # 14.1: the Dockerfile held against the platform's policy before anything of it runs.
        if dockerpolicy.main([str(dockerfile), "--trust", trust, "--shown", job["shown"], "--json", str(folder / "dockerfile-policy.json")]):
            refused += 1
            continue
        hadolint = tools / "hadolint"
        if hadolint.is_file():
            findings = subprocess.run([str(hadolint), "--no-fail", "--format", "json", str(dockerfile)], text=True, capture_output=True).stdout or "[]"
            (folder / "hadolint.json").write_text(findings)
        if not (context / ".dockerignore").is_file() and not dockerfile.with_name(dockerfile.name + ".dockerignore").is_file():
            # 14.2: without a .dockerignore of its own, this build (never the repository) gets the platform's deny-list.
            (context / ".dockerignore").write_text(deny_dockerignore())
        kib = context_kib(context)
        if kib > MAX_CONTEXT_KIB:
            print(f"::error title=Job {name}::the build context is {kib // 1024} MiB, over the 1 GiB the platform builds: keep data in a bucket")
            refused += 1
            continue
        tag = f"lab-job-{name}:{commit}"
        args = ["docker", "build", "--no-cache", "--platform", "linux/amd64"]
        if network == "none":
            args.append("--network=none")
        if job["base"]:
            if not base_image:
                raise ValueError(f"The job {name} builds on a test image, and none was built")
            args += ["--build-arg", f"BASE={base_image}"]
        runner([*args, "-f", str(dockerfile), "-t", tag, str(context)])
        runner(["docker", "save", tag, "-o", str(folder / "image.tar")])
        (folder / "image.sha256").write_text(sha256(folder / "image.tar") + "\n")
        inputs = {"recipe": "repository", "dockerfile_sha256": sha256(dockerfile), "lock_sha256": None, "lock_source": None, "builder": None,
                  "job": name, "base": base if job["base"] else None}
        (folder / "inputs.json").write_text(json.dumps(inputs, sort_keys=True) + "\n")
        # 14.2: what reached the image, scanned again for keys; 14.7: its parts list.
        report = folder / "secrets.jsonl"
        report.write_text(runner([str(tools / "trufflehog"), "docker", f"--image=file://{(folder / 'image.tar').resolve()}",
                                  "--results=verified,unknown,unverified", "--json", "--no-update"]))
        if leaks.main([str(report), "--trust", trust, "--where", "image", "--json", str(folder / "secrets-image.json")]):
            refused += 1
        runner([str(tools / "trivy"), "image", "--input", str(folder / "image.tar"), "--format", "cyclonedx", "--output",
                str(folder / "sbom.cdx.json"), "--quiet", "--skip-version-check"])
        if sbom.main([str(folder / "sbom.cdx.json")]):
            refused += 1
        summary_line(f"Job {name}: image built from {job['shown']}" + (f" on the test image {base_image}" if job["base"] else ""))
    return refused


def push(spec, bundle, prefix, mode, key="", runner=run, env=None):
    """The deploy job's part (27.3): each job's image inspected, pushed, signed and verified; job-images.json for the release."""
    from . import provenance
    env = dict(os.environ if env is None else env)
    if not re.fullmatch(r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+", prefix):
        raise ValueError("The image prefix is REGION-docker.pkg.dev/PROJECT/REGISTRY/SERVICE")
    commit, plan = env.get("COMMIT_SHA", ""), env.get("PLAN_ID", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{32}", plan):
        raise ValueError("COMMIT_SHA and PLAN_ID are the release's")
    images = {}
    for name in spec.jobs:
        folder = Path(bundle) / "jobs" / name
        tar = folder / "image.tar"
        if sha256(tar) != (folder / "image.sha256").read_text().strip():
            raise ValueError(f"The job {name}'s image is not the one the build job saved")
        if env.get("GOOGLE_ACCESS_TOKEN"):
            # 14.10: the data files the image carries, inspected before it reaches the registry.
            from . import datafiles
            if datafiles.main([str(tar), "--project", env["GCP_PROJECT_ID"], "--region", env["GCP_REGION"], "--json", str(folder / "datafiles.json")]):
                raise ValueError(f"The job {name}'s image carries data files that stop this release")
        tag = f"lab-job-{name}:{commit}"
        image = f"{prefix}-{name}"
        runner(["docker", "load", "-i", str(tar)])
        for suffix in (commit, f"release-{plan}"):
            runner(["docker", "tag", tag, f"{image}:{suffix}"])
            runner(["docker", "push", f"{image}:{suffix}"])
        digest = runner(["gcloud", "artifacts", "docker", "images", "describe", f"{image}:{commit}", "--format=value(image_summary.digest)"]).strip()
        if not DIGEST.fullmatch(digest):
            raise ValueError(f"The registry named no digest for {image}:{commit}")
        reference = f"{image}@{digest}"
        if mode == "unavailable":
            print(f"::warning title=Provenance not recorded::the operator's exception is set, so the job {name}'s image is deployed without signed provenance")
        else:
            # Every job image is signed with the application's Cloud KMS key, whatever the repository's visibility: GitHub's
            # attestation actions take one subject per call, and Cosign takes them in a loop.
            if not key:
                raise ValueError("No Cloud KMS key to sign the jobs' images with: apply the foundation, then this application's root")
            statement = folder / "provenance.json"
            statement.write_text(json.dumps(provenance.predicate(
                repository=env.get("GITHUB_REPOSITORY"), commit=commit, plan_id=plan, spec_hash=env.get("SPEC_HASH"), run_id=env.get("GITHUB_RUN_ID"),
                run_attempt=env.get("GITHUB_RUN_ATTEMPT"), repository_id=env.get("GITHUB_REPOSITORY_ID"), owner_id=env.get("GITHUB_REPOSITORY_OWNER_ID"),
                workflow_ref=env.get("GITHUB_WORKFLOW_REF"), server=env.get("GITHUB_SERVER_URL", "https://github.com"),
                event_name=env.get("GITHUB_EVENT_NAME", "workflow_dispatch"), runner_environment=env.get("RUNNER_ENVIRONMENT", "github-hosted"),
                path=provenance.workflow_path(env.get("DEPLOY_WORKFLOW")), source_repository=env.get("SOURCE_REPOSITORY") or None,
                contract_commit=env.get("CONTRACT_COMMIT") or None, application=env.get("APPLICATION") or None,
                inputs=provenance.read_inputs(str(folder / "inputs.json"))), indent=2, sort_keys=True))
            cosign = env.get("COSIGN", "cosign")
            flags = ["--yes", "--key", key, "--new-bundle-format=false", "--use-signing-config=false", "--tlog-upload=false"]
            runner([cosign, "attest", *flags, "--type", "slsaprovenance1", "--predicate", str(statement), reference])
            runner([cosign, "attest", *flags, "--type", "cyclonedx", "--predicate", str(folder / "sbom.cdx.json"), reference])
            verified = runner([cosign, "verify-attestation", "--key", key, "--type", "slsaprovenance1", "--insecure-ignore-tlog=true", reference])
            provenance.verify(verified, image_name=image, digest=digest, repository=env.get("GITHUB_REPOSITORY"), commit=commit,
                              server=env.get("GITHUB_SERVER_URL", "https://github.com"), path=provenance.workflow_path(env.get("DEPLOY_WORKFLOW")),
                              source_repository=env.get("SOURCE_REPOSITORY") or None, contract_commit=env.get("CONTRACT_COMMIT") or None)
            summary_line(f"Job {name}: {reference} pushed, signed and verified")
        images[name] = reference
    Path(IMAGES_FILE).write_text(json.dumps(images, indent=2, sort_keys=True) + "\n")
    return images


def scan(env=None):
    """The vulnerability summary of each job's image, under the contract's trust (labdeploy/scan.py). Returns the reasons a
    job's image stops the release, one per image."""
    from . import scan as scanner
    env = os.environ if env is None else env
    images = json.loads(Path(IMAGES_FILE).read_text()) if Path(IMAGES_FILE).is_file() else {}
    stopped = []
    for name, image in sorted(images.items()):
        saved = {key: os.environ.get(key) for key in ("IMAGE_DIGEST", "SUMMARY_FILE")}
        os.environ.update(IMAGE_DIGEST=image, SUMMARY_FILE=f"vulnerability-summary-{name}.json")
        try:
            scanner.main([])
        except SystemExit as stop:
            if stop.code:
                stopped.append(f"the job {name}'s image: {stop.code}")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return stopped


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.jobimages")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("spec")
    read.add_argument("--base", default="")
    make = commands.add_parser("build")
    make.add_argument("spec")
    make.add_argument("--app-dir", required=True)
    make.add_argument("--app-path", default=".")
    make.add_argument("--out", required=True)
    make.add_argument("--trust", choices=("reviewed", "unknown"), required=True)
    make.add_argument("--network", choices=("default", "none"), default="default")
    make.add_argument("--base-image", default="")
    make.add_argument("--base", default="")
    send = commands.add_parser("push")
    send.add_argument("spec")
    send.add_argument("--bundle", required=True)
    send.add_argument("--image-prefix", required=True)
    send.add_argument("--mode", choices=("github", "cosign-kms", "unavailable"), required=True)
    send.add_argument("--key", default="")
    commands.add_parser("scan")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            stopped = scan()
            if stopped:
                print("\n".join(f"::error title=Vulnerabilities::{reason}" for reason in stopped))
                return 1
            return 0
        spec = read_spec(args.spec)
        if args.command == "read":
            base = check_base(spec, args.base)
            lines = {"has_jobs": "true" if spec.jobs else "false", "has_base": "true" if base else "false"}
            if base:
                owner, name = base["repository"].split("/", 1)
                lines.update(base_service=base["service"], base_repository=base["repository"], base_owner=owner, base_name=name,
                             base_commit=base["commit"], base_path=base["path"] or ".")
            for key, value in lines.items():
                print(f"{key}={value}")
            return 0
        if args.command == "build":
            base = check_base(spec, args.base)
            refused = build(spec, args.app_dir, args.app_path, args.out, args.trust, args.network, args.base_image, base=base)
            return 1 if refused else 0
        push(spec, args.bundle, args.image_prefix, args.mode, args.key)
        return 0
    except (ValueError, subprocess.CalledProcessError, OSError, ProvenanceError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(f"::error title=Job images::{str(detail).strip()[:500]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
