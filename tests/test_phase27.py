"""Phase 27: jobs an application starts itself (docs/ROADMAP.md D64 to D70, docs/RESEARCH-2026-10-21-jobs.md)."""
import copy
import datetime
import hashlib
import io
import json
import re
from pathlib import Path

import hcl2
import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub

from labdeploy import jobimages, promotion
from labdeploy.config import Config
from labdeploy.controller import Controller, Store, release_approval
from labdeploy.cost import estimate, jobs_costs
from labdeploy.infra import app_root, check_plan, root_jobs
from labdeploy.jobs import Keeper, judge
from labdeploy.models import draft_dump, enforce, job_env, needs_env, parse_spec, spec_hash
from labdeploy.release import CloudRun, restore_jobs, rollout
from labdeploy.scaffold import base_dockerfile, render_base

CODESURGEON = """
name: codesurgeon
port: 8080
health_path: /health
exposure: lab-members
secrets: [GITHUB_TOKEN, MODEL_KEY]
needs:
  buckets: [runs, state]
jobs:
  agent:
    dockerfile: sandbox/agent.Dockerfile
    base: bettercare
    command: [python, -m, smartdev.sandbox_agent]
    resources: {cpu: 2, memory_mib: 4096}
    timeout_minutes: 60
    buckets: {runs: write}
    models: [claude]
  checker:
    dockerfile: sandbox/checker.Dockerfile
    base: bettercare
    resources: {cpu: 2, memory_mib: 2048}
    timeout_minutes: 15
    buckets: {runs: read}
    secrets: [MODEL_KEY]
    egress: internet
wake: {path: /tick, every_minutes: 5}
reviewed: true
"""
BOOTSTRAP = "us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parent.parent


def spec():
    return parse_spec(CODESURGEON)


# ---- 27.1: the contract, its policy and its cost

def test_the_contract_takes_jobs_and_a_clock_and_gives_each_job_only_what_it_was_given():
    s = spec()
    enforce(s)
    assert set(s.jobs) == {"agent", "checker"} and s.wake.path == "/tick" and s.wake.every_minutes == 5
    env = needs_env(s, "p")
    assert env["JOB_AGENT"] == "projects/p/locations/us-central1/jobs/codesurgeon-agent"
    assert env["JOB_CHECKER"] == "projects/p/locations/us-central1/jobs/codesurgeon-checker"
    # The agent gets its one bucket and the Vertex variables; nothing of the service's other bucket or its jobs.
    assert job_env(s, "agent", "p") == {"BUCKET_RUNS": "p-codesurgeon-runs", "ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "global"}
    assert job_env(s, "checker", "p") == {"BUCKET_RUNS": "p-codesurgeon-runs"}
    # Defaults: one run of at most ten minutes, thirty a day (D65), private egress unless the contract says internet.
    plain = parse_spec("name: demo\nreviewed: true\njobs: {sweep: {dockerfile: Dockerfile.sweep}}\n")
    assert plain.jobs["sweep"].timeout_minutes == 10 and plain.jobs["sweep"].runs_per_day == 30 and plain.jobs["sweep"].egress is None


@pytest.mark.parametrize("extra, words", [
    ("jobs: {migrate: {dockerfile: D}}", "migration job"),
    ("jobs: {wake: {dockerfile: D}}", "platform uses"),
    ("jobs: {a: {dockerfile: D}}", "not a job name"),
    ("jobs: {Agent: {dockerfile: D}}", "not a job name"),
    ("jobs: {agent: {dockerfile: ../D}}", "dockerfile"),
    ("jobs: {agent: {dockerfile: sandbox/../../D}}", "should match pattern"),
    ("jobs: {agent: {dockerfile: D, buckets: {nope: read}}}", "not under needs.buckets"),
    ("jobs: {agent: {dockerfile: D, buckets: {runs: delete}}}", "read"),
    ("jobs: {agent: {dockerfile: D, secrets: [TOKEN]}}", "not under secrets"),
    ("jobs: {agent: {dockerfile: D, env: {BUCKET_RUNS: x}, buckets: {runs: read}}}", "the platform sets"),
    ("jobs: {agent: {dockerfile: D, resources: {cpu: 1, memory_mib: 8192}}}", "4 GiB a CPU"),
    ("jobs: {agent: {dockerfile: D, resources: {cpu: 1, memory_mib: 1000}}}", "multiple of 256"),
    ("jobs: {agent: {dockerfile: D, runs_per_day: 31}}", "less than or equal to 30"),
    ("jobs: {agent: {dockerfile: D, timeout_minutes: 61}}", "less than or equal to 60"),
    ("jobs: {agent: {dockerfile: D, base: demo-service-name-xx}}", "own test image"),
    ("jobs: {agentlongname: {dockerfile: D}}", "30 characters"),
    ("jobs: {a1: {dockerfile: D}, a2: {dockerfile: D}, a3: {dockerfile: D}, a4: {dockerfile: D}, a5: {dockerfile: D}}", "at most 4 jobs"),
    ("jobs: {agent: {dockerfile: D}}\nenv: {JOB_AGENT: x}", "set by the platform"),
    ("wake: {path: tick}", "pattern"),
    ("wake: {path: /tick, every_minutes: 0}", "greater than or equal to 1"),
])
def test_the_contract_refuses_jobs_it_cannot_run_safely(extra, words):
    with pytest.raises(ValueError, match=re.escape(words)):
        parse_spec("name: demo-service-name-xx\nreviewed: true\nsecrets: [MODEL_KEY]\nneeds: {buckets: [runs]}\n" + extra)


def test_a_static_site_starts_no_jobs_and_the_policy_bounds_a_job_at_two_cpus_and_4096_mib():
    with pytest.raises(ValueError, match="runtime: static has no server"):
        parse_spec("name: site\nruntime: static\nnode: '24'\nexposure: lab-members\nbuild: {recipe: platform, entrypoint: x, output: dist}\n"
                   "reviewed: true\nwake: {path: /t}\n")
    with pytest.raises(ValueError, match="at most 2 CPUs and 4096 MiB"):
        enforce(parse_spec("name: demo\nreviewed: true\njobs: {agent: {dockerfile: D, resources: {cpu: 4, memory_mib: 8192}}}\n"))
    enforce(parse_spec("name: demo\nreviewed: true\njobs: {agent: {dockerfile: D, resources: {cpu: 2, memory_mib: 4096}}}\n"))  # D67


def test_contracts_without_jobs_hash_and_draft_as_they_always_did():
    text = "name: demo\nreviewed: true\nneeds: {buckets: [files]}\n"
    plain = parse_spec(text)
    assert "jobs" not in draft_dump(plain) and "wake" not in draft_dump(plain)
    assert "jobs" not in plain.model_dump(exclude_defaults=True) and "wake" not in plain.model_dump(exclude_defaults=True)
    assert spec_hash(plain) == hashlib.sha256(json.dumps(plain.model_dump(exclude_defaults=True), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert "jobs" in draft_dump(spec()) and "wake" in draft_dump(spec())


def test_every_plan_prices_each_job_and_its_worst_case():
    items = {item["job"]: item for item in jobs_costs(spec())}
    # 2 vCPU and 4 GiB at instance-based rates: 2 * 0.000018 + 4 * 0.000002 = 0.000044 a second.
    assert items["agent"]["per_minute_usd"] == round(0.000044 * 60, 4)
    assert items["agent"]["per_run_max_usd"] == round(0.000044 * 3600, 4)
    assert items["agent"]["worst_day_usd"] == round(0.000044 * 3600 * 30, 4) and items["agent"]["worst_month_usd"] == round(0.000044 * 3600 * 30 * 30, 4)
    assert items["checker"]["per_run_max_usd"] == round((2 * 0.000018 + 2 * 0.000002) * 900, 4)
    result = estimate(spec())
    assert result["jobs_ceiling_usd_month"] == round(sum(item["worst_month_usd"] for item in items.values()), 4)
    assert any("instance-based rates for at least a minute" in line for line in result["assumptions"])
    assert "jobs" not in estimate(parse_spec("name: demo\nreviewed: true\n"))
    # A run is billed for at least a minute, whatever its timeout.
    one = parse_spec("name: demo\nreviewed: true\njobs: {tiny: {dockerfile: D, timeout_minutes: 1}}\n")
    assert jobs_costs(one)[0]["per_run_max_usd"] == round(60 * (0.000018 + 0.5 * 0.000002), 4)


def test_the_schema_file_is_the_model():
    from labdeploy.models import AppSpec
    assert json.loads(Path("schema/platform.schema.json").read_text()) == AppSpec.model_json_schema()


# ---- 27.2: identities, jobs and grants

def root(**extra):
    s = spec()
    return app_root("codesurgeon", project_id="lab-proj-123", region="us-central1", state_bucket="lab-proj-123-tfstate", github_repository="o/r",
                    github_repository_id="1", github_owner_id="2", bootstrap_image=BOOTSTRAP, needs=s.needs.model_dump(), exposure="lab-members",
                    viewers=["user:ann@example.org"], **({"jobs": root_jobs(s), "wake": True} | extra))


def test_a_root_creates_the_jobs_and_the_clock_and_reads_the_controller_from_the_foundation():
    main = root()["main.tf"]
    module = hcl2.load(io.StringIO(main))["module"][0]["service"]
    assert module["jobs"]["agent"] == {"cpu": 2, "memory_mib": 4096, "timeout_minutes": 60, "buckets": {"runs": "write"}, "models": ["claude"], "secrets": []}
    assert module["jobs"]["checker"]["secrets"] == ["MODEL_KEY"] and module["wake"] is True
    assert "try(data.terraform_remote_state.project.outputs.controller_members, [])" in main
    assert "try(data.terraform_remote_state.project.outputs.job_guard_role, \"\")" in main
    assert "model_user_role" in main  # a job's model needs the foundation's role even when the service has none
    # Without jobs or a clock the root reads exactly as before.
    plain = root(jobs=None, wake=False)["main.tf"]
    assert not any(word in plain for word in ("jobs ", "wake", "controller_members", "job_guard_role", "model_user_role"))


@pytest.mark.parametrize("jobs, words", [
    ({"migrate": {"cpu": 1, "memory_mib": 512, "timeout_minutes": 1, "buckets": {}, "models": [], "secrets": []}}, "Invalid job name"),
    ({"agent": {"cpu": 4, "memory_mib": 512, "timeout_minutes": 1, "buckets": {}, "models": [], "secrets": []}}, "cpu (1 or 2)"),
    ({"agent": {"cpu": 1, "memory_mib": 512, "timeout_minutes": 1, "buckets": {"other": "read"}, "models": [], "secrets": []}}, "declared buckets"),
    ({"agent": {"cpu": 1, "memory_mib": 512, "timeout_minutes": 1, "buckets": {}, "models": ["gpt"], "secrets": []}}, "models"),
    ({"agent": {"cpu": 1, "memory_mib": 512, "timeout_minutes": 1, "buckets": {}, "models": [], "secrets": ["a b"]}}, "variable names"),
])
def test_a_root_refuses_jobs_the_module_would_refuse(jobs, words):
    with pytest.raises(ValueError, match=re.escape(words)):
        root(jobs=jobs)


def test_the_service_module_grants_each_identity_only_its_part():
    text = Path("infra/gcp/modules/service/jobs.tf").read_text()
    for block in ('resource "google_service_account" "job"', 'resource "google_cloud_run_v2_job" "job"', '"roles/run.jobsExecutorWithOverrides"',
                  'resource "google_cloud_run_v2_job_iam_member" "job_guard"', 'resource "google_cloud_run_v2_job_iam_member" "job_cap"',
                  '"roles/storage.objectUser" : "roles/storage.objectViewer"', '"roles/iam.serviceAccountOpenIdTokenCreator"',
                  'resource "google_iap_web_cloud_run_service_iam_member" "wake"'):
        assert block in text, block
    job = text[text.index('resource "google_cloud_run_v2_job" "job"'):text.index('resource "google_cloud_run_v2_job_iam_member" "job_runtime"')]
    assert "max_retries     = 0" in job and "task_count  = 1" in job and "ignore_changes = [template, client, client_version]" in job
    assert "lab-deploy-job = each.key" in job  # the cap finds the jobs applications start by this label
    project = Path("infra/gcp/modules/project/main.tf").read_text()
    guard = project[project.index('resource "google_project_iam_custom_role" "job_guard"'):]
    assert 'permissions = ["run.executions.cancel", "run.operations.get"]' in guard[:guard.index("\n}")]
    assert '"run.executions.cancel"]' in project[project.index('role_id     = "labDeployCapOperator"'):][:600]
    outputs = Path("infra/gcp/modules/project/outputs.tf").read_text()
    assert 'output "controller_members"' in outputs and "var.viewer_members" in outputs and 'output "job_guard_role"' in outputs


def change(kind, role, member, **after):
    return {"address": f"module.service.{kind}.x", "type": kind, "change": {"actions": ["create"], "after": {"role": role, "member": member, **after}, "after_unknown": {}}}


def test_the_plan_policy_allows_the_new_grants_and_no_others():
    project = "lab-proj-123"
    good = [
        change("google_cloud_run_v2_job_iam_member", "roles/run.jobsExecutorWithOverrides", f"serviceAccount:codesurgeon-runtime@{project}.iam.gserviceaccount.com",
               location="us-central1"),
        change("google_cloud_run_v2_job_iam_member", f"projects/{project}/roles/labDeployCapOperator", f"serviceAccount:lab-deploy-spending-cap@{project}.iam.gserviceaccount.com",
               location="us-central1"),
        change("google_cloud_run_v2_job_iam_member", f"projects/{project}/roles/labDeployJobGuard", f"serviceAccount:lab-deploy-controller@{project}.iam.gserviceaccount.com",
               location="us-central1"),
        change("google_service_account_iam_member", "roles/iam.serviceAccountOpenIdTokenCreator", f"serviceAccount:lab-deploy-controller@{project}.iam.gserviceaccount.com",
               service_account_id=f"projects/{project}/serviceAccounts/codesurgeon-wake@{project}.iam.gserviceaccount.com"),
    ]
    assert check_plan({"resource_changes": good}) == []
    bad = [
        # The controller may cancel through the guard's role only, and mint the wake identity's token only.
        change("google_cloud_run_v2_job_iam_member", "roles/run.developer", f"serviceAccount:lab-deploy-controller@{project}.iam.gserviceaccount.com", location="us-central1"),
        change("google_service_account_iam_member", "roles/iam.serviceAccountOpenIdTokenCreator", f"serviceAccount:lab-deploy-controller@{project}.iam.gserviceaccount.com",
               service_account_id=f"projects/{project}/serviceAccounts/codesurgeon-runtime@{project}.iam.gserviceaccount.com"),
        change("google_cloud_run_v2_job_iam_member", f"projects/{project}/roles/labDeployJobGuard", f"serviceAccount:lab-deploy-observer@{project}.iam.gserviceaccount.com",
               location="us-central1"),
    ]
    found = check_plan({"resource_changes": bad})
    assert [item["rule"] for item in found] == ["platform_identities_untouched"] * 3
    unlabelled = {"address": "module.service.google_cloud_run_v2_job.job", "type": "google_cloud_run_v2_job",
                  "change": {"actions": ["create"], "after": {"location": "us-central1", "labels": {}}, "after_unknown": {}}}
    assert [item["rule"] for item in check_plan({"resource_changes": [unlabelled]})] == ["labels_present"]


# ---- 27.3 and 27.4: images

def test_the_test_image_is_made_from_the_applications_own_contract_and_lock(tmp_path):
    base = parse_spec("name: bettercare\nruntime: node-http\nnode: '24'\nreviewed: true\nbuild: {scripts: [onnxruntime-node@1.30.0]}\n")
    text = base_dockerfile(base, "package-lock.json")
    assert "FROM node:24-trixie-slim@sha256:" in text and "WORKDIR /deps" in text and "COPY package.json package-lock.json ./" in text
    assert "npm ci --ignore-scripts --no-audit --no-fund" in text and "npm rebuild --ignore-scripts=false onnxruntime-node@1.30.0" in text
    assert "USER 65532:65532" in text and "WORKDIR /work" in text and "chown 65532:65532 /work" in text
    assert "rm -rf /usr/local/lib/node_modules/npm" not in text  # npm stays: the tests run with it
    with pytest.raises(ValueError, match="not a Node.js web service"):
        base_dockerfile(parse_spec("name: pyapp\nreviewed: true\n"), "package-lock.json")
    with pytest.raises(ValueError, match="npm lockfile"):
        base_dockerfile(base, "pnpm-lock.yaml")
    app = tmp_path / "app"
    (app / "server").mkdir(parents=True)
    (app / "package.json").write_text('{"name": "bettercare"}')
    (app / "package-lock.json").write_text("{}")
    (app / "server" / "secret.mjs").write_text("code")
    (app / ".npmrc").write_text("//registry/:_authToken=x")
    assert render_base(app, tmp_path / "out", base) == "package-lock.json"
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == ["Dockerfile", "package-lock.json", "package.json"]
    (app / "package-lock.json").unlink()
    platform = tmp_path / "platform-lock.json"
    platform.write_text('{"lockfileVersion": 3}')
    render_base(app, tmp_path / "out2", base, platform_lock=platform)
    assert (tmp_path / "out2" / "package-lock.json").read_text() == '{"lockfileVersion": 3}'
    with pytest.raises(ValueError, match="npm lockfile"):
        render_base(app, tmp_path / "out3", base)


def test_the_plans_base_must_name_the_application_the_jobs_build_on():
    s = spec()
    assert jobimages.base_of(s) == "bettercare"
    good = json.dumps({"service": "bettercare", "repository": "lab/bettercare", "path": "", "commit": "b" * 40})
    assert jobimages.check_base(s, good)["commit"] == "b" * 40
    with pytest.raises(ValueError, match="release bettercare first"):
        jobimages.check_base(s, "")
    with pytest.raises(ValueError, match="contract's jobs build on bettercare"):
        jobimages.check_base(s, good.replace("bettercare\"", "other\"", 1))
    with pytest.raises(ValueError, match="unexpected path"):
        jobimages.check_base(s, json.dumps({"service": "bettercare", "repository": "lab/b", "path": "../x", "commit": "b" * 40}))
    two = parse_spec("name: demo\nreviewed: true\njobs: {a1: {dockerfile: D, base: one}, a2: {dockerfile: D, base: two}}\n")
    with pytest.raises(ValueError, match="one application's test image at most"):
        jobimages.base_of(two)
    assert jobimages.check_base(parse_spec("name: demo\nreviewed: true\n"), "") is None


def test_the_read_step_prints_outputs_for_the_workflow(tmp_path, capsys):
    path = tmp_path / "platform.yaml"
    path.write_text(CODESURGEON)
    base = json.dumps({"service": "bettercare", "repository": "lab/bettercare", "path": "app", "commit": "b" * 40})
    assert jobimages.main(["read", str(path), "--base", base]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert lines == {"has_jobs": "true", "has_base": "true", "base_service": "bettercare", "base_repository": "lab/bettercare", "base_owner": "lab",
                     "base_name": "bettercare", "base_commit": "b" * 40, "base_path": "app"}
    path.write_text("name: demo\nreviewed: true\n")
    assert jobimages.main(["read", str(path)]) == 0 and capsys.readouterr().out.split() == ["has_jobs=false", "has_base=false"]


def make_checkout(tmp_path, agent="FROM ${BASE}\nUSER 65532\n"):
    app = tmp_path / "app"
    (app / "sandbox").mkdir(parents=True)
    (app / "sandbox" / "agent.Dockerfile").write_text("ARG BASE\n" + agent)
    (app / "sandbox" / "checker.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nUSER 65532\n")
    return app


class Recorder:
    def __init__(self, tmp_path):
        self.calls, self.tmp_path = [], tmp_path

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["docker", "save"]:
            Path(args[-1]).write_bytes(b"image " + args[2].encode())
        if args[1:2] == ["image"]:  # trivy writes the parts list
            Path(args[args.index("--output") + 1]).write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []}))
        return ""


def test_the_build_job_builds_each_job_on_the_test_image_and_checks_it_like_the_service(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    app, out = make_checkout(tmp_path), tmp_path / "bundle" / "jobs"
    runner = Recorder(tmp_path)
    base = {"service": "bettercare", "repository": "lab/bettercare", "path": "", "commit": "b" * 40}
    refused = jobimages.build(spec(), app, ".", out, "unknown", base_image="lab-base:" + "c" * 40, commit="c" * 40, tools=tmp_path, runner=runner, base=base)
    assert refused == 0
    builds = [call for call in runner.calls if call[:2] == ["docker", "build"]]
    assert len(builds) == 2 and all("--no-cache" in call and "--platform" in call and f"BASE=lab-base:{'c' * 40}" in call for call in builds)
    assert builds[0][-3:] == ["-t", f"lab-job-agent:{'c' * 40}", str(app / "sandbox")]
    for name in ("agent", "checker"):
        folder = out / name
        assert (folder / "image.sha256").read_text().strip() == hashlib.sha256((folder / "image.tar").read_bytes()).hexdigest()
        inputs = json.loads((folder / "inputs.json").read_text())
        assert inputs["job"] == name and inputs["base"]["commit"] == "b" * 40 and inputs["dockerfile_sha256"]
        assert json.loads((folder / "dockerfile-policy.json").read_text())["trust"] == "unknown"
    # 14.2: a context without a .dockerignore of its own gets the platform's deny-list for this build.
    assert ".git" in (app / "sandbox" / ".dockerignore").read_text()
    assert any(call[1:2] == ["docker"] and call[0].endswith("trufflehog") for call in runner.calls)
    # The build network follows the contract (build.network: none).
    runner = Recorder(tmp_path)
    jobimages.build(spec(), make_checkout(tmp_path / "again"), ".", tmp_path / "again-out", "reviewed", network="none", base_image="lab-base:x",
                    commit="c" * 40, tools=tmp_path, runner=runner, base=base)
    assert all("--network=none" in call for call in runner.calls if call[:2] == ["docker", "build"])


def test_the_build_job_refuses_a_job_whose_dockerfile_breaks_the_policy_before_building_it(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    app = make_checkout(tmp_path, agent="FROM ${BASE}\nRUN curl https://example.org/x.sh | sh\nUSER 65532\n")
    runner = Recorder(tmp_path)
    refused = jobimages.build(spec(), app, ".", tmp_path / "out", "unknown", base_image="lab-base:x", commit="c" * 40, tools=tmp_path, runner=runner,
                              base={"service": "bettercare", "repository": "lab/b", "path": "", "commit": "b" * 40})
    assert refused == 1 and not any(f"lab-job-agent:{'c' * 40}" in call for call in runner.calls)
    missing = make_checkout(tmp_path / "missing")
    (missing / "sandbox" / "checker.Dockerfile").unlink()
    assert jobimages.build(spec(), missing, ".", tmp_path / "out2", "reviewed", base_image="lab-base:x", commit="c" * 40, tools=tmp_path,
                           runner=Recorder(tmp_path), base={"service": "bettercare", "repository": "lab/b", "path": "", "commit": "b" * 40}) == 1


def deploy_env(**extra):
    return {"COMMIT_SHA": "c" * 40, "PLAN_ID": "d" * 32, "SPEC_HASH": "e" * 64, "GITHUB_REPOSITORY": "lab/codesurgeon", "GITHUB_RUN_ID": "9",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY_ID": "1", "GITHUB_REPOSITORY_OWNER_ID": "2",
            "GITHUB_WORKFLOW_REF": "lab/codesurgeon/.github/workflows/deploy-gcp.yml@refs/heads/main", "DEPLOY_WORKFLOW": "deploy-gcp.yml",
            "COSIGN": "cosign", **extra}


def test_the_deploy_job_pushes_signs_and_verifies_every_job_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    for name in ("agent", "checker"):
        folder = tmp_path / "bundle" / "jobs" / name
        folder.mkdir(parents=True)
        (folder / "image.tar").write_bytes(name.encode())
        (folder / "image.sha256").write_text(hashlib.sha256(name.encode()).hexdigest())
        (folder / "inputs.json").write_text(json.dumps({"recipe": "repository", "job": name}))
        (folder / "sbom.cdx.json").write_text("{}")
    calls, digests = [], {"agent": "1" * 64, "checker": "2" * 64}

    def runner(args, **kwargs):
        calls.append(args)
        if args[:4] == ["gcloud", "artifacts", "docker", "images"]:
            return "sha256:" + digests[args[5].split(":")[0].rsplit("-", 1)[1]] + "\n"
        return ""
    verified = []
    monkeypatch.setattr("labdeploy.provenance.verify", lambda output, **kw: verified.append(kw) or {})
    prefix = "us-central1-docker.pkg.dev/lab-proj-123/codesurgeon/codesurgeon"
    images = jobimages.push(spec(), tmp_path / "bundle", prefix, "github", key="gcpkms://key", runner=runner, env=deploy_env())
    assert images == {"agent": f"{prefix}-agent@sha256:{'1' * 64}", "checker": f"{prefix}-checker@sha256:{'2' * 64}"}
    assert json.loads(Path("job-images.json").read_text()) == images
    assert [call[:3] for call in calls if call[0] == "docker" and call[1] == "push"] == [["docker", "push", f"{prefix}-agent:{'c' * 40}"],
            ["docker", "push", f"{prefix}-agent:release-{'d' * 32}"], ["docker", "push", f"{prefix}-checker:{'c' * 40}"], ["docker", "push", f"{prefix}-checker:release-{'d' * 32}"]]
    attests = [call for call in calls if call[:2] == ["cosign", "attest"]]
    assert len(attests) == 4 and {call[call.index("--type") + 1] for call in attests} == {"slsaprovenance1", "cyclonedx"}
    assert all("--tlog-upload=false" in call for call in attests)  # a private repository's names stay out of a public log
    assert [item["image_name"] for item in verified] == [f"{prefix}-agent", f"{prefix}-checker"]
    # The operator's exception pushes without signing; an image the build did not save is refused.
    calls.clear()
    jobimages.push(spec(), tmp_path / "bundle", prefix, "unavailable", runner=runner, env=deploy_env())
    assert not any(call[0] == "cosign" for call in calls)
    (tmp_path / "bundle" / "jobs" / "agent" / "image.tar").write_bytes(b"changed")
    with pytest.raises(ValueError, match="not the one the build job saved"):
        jobimages.push(spec(), tmp_path / "bundle", prefix, "github", key="k", runner=runner, env=deploy_env())
    with pytest.raises(ValueError, match="image prefix"):
        jobimages.push(spec(), tmp_path / "bundle", "docker.io/x", "github", key="k", runner=runner, env=deploy_env())


def test_each_job_image_gets_its_own_vulnerability_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("job-images.json").write_text(json.dumps({"agent": "img-agent", "checker": "img-checker"}))
    seen = []

    def fake_main(argv):
        import os
        seen.append((os.environ["IMAGE_DIGEST"], os.environ["SUMMARY_FILE"]))
        if os.environ["IMAGE_DIGEST"] == "img-agent":
            raise SystemExit("Fixable critical findings stop this release")
    monkeypatch.setattr("labdeploy.scan.main", fake_main)
    stopped = jobimages.scan()
    assert seen == [("img-agent", "vulnerability-summary-agent.json"), ("img-checker", "vulnerability-summary-checker.json")]
    assert stopped == ["the job agent's image: Fixable critical findings stop this release"]


def test_the_release_workflow_builds_signs_and_scans_the_jobs_images():
    template = Path("templates/deploy-gcp.yml").read_text()
    for step in ("- name: Read the jobs", "- name: Token for the base application's repository", "- name: Build the test image the jobs build on",
                 "- name: Build and check the jobs' images", "- name: Push, sign and verify the jobs' images", "- name: Summarize the jobs' image vulnerabilities"):
        assert step in template, step
    assert template.count("- name: Read the jobs") == 2  # the build job and the deploy job
    assert "permission-contents: read" in template and "path: base" in template and "persist-credentials: false" in template
    # The jobs are built after the service's own image and before the bundle is uploaded; pushed after the service's provenance.
    assert template.index("Build and check the jobs' images") < template.index("name: release-bundle")
    # The base application's repository is read, with the App's key, only in the job that runs none of this application's code.
    import yaml as yaml_module
    jobs = yaml_module.safe_load(template)["jobs"]
    base_steps = " ".join(json.dumps(step) for step in jobs["base"]["steps"])
    build_steps = " ".join(json.dumps(step) for step in jobs["build"]["steps"])
    assert "APPS_APP_PRIVATE_KEY" in base_steps and "APPS_APP_PRIVATE_KEY" not in build_steps and "path\": \"base\"" in base_steps
    assert jobs["build"]["needs"] == "base" and "needs.build.result == 'success'" in jobs["deploy"]["if"]
    assert template.index("Verify build provenance with Cloud KMS") < template.index("Push, sign and verify the jobs' images") < template.index("Deploy, probe, promote")
    assert "A release with jobs builds its images; it never reuses an earlier one" in template
    from labdeploy.workflows import central_deploy
    assert Path(".github/workflows/deploy.yml").read_text() == central_deploy(template)


# ---- 27.5: releases

class FakeRun(CloudRun):
    def __init__(self, fail_at=None):
        super().__init__("lab-proj-123", "us-central1", "codesurgeon", "codesurgeon-probe@lab-proj-123.iam.gserviceaccount.com",
                         runner=self.record, iap_client_id="1-a.apps.googleusercontent.com")
        self.commands, self.fail_at, self.images = [], fail_at, {"agent": BOOTSTRAP, "checker": BOOTSTRAP}
        self.traffic_calls = []

    def record(self, args):
        self.commands.append(args)
        return ""

    def describe(self):
        return {"status": {"traffic": [{"revisionName": "codesurgeon-old", "percent": 100}], "url": "https://codesurgeon-x.a.run.app"}}

    def traffic(self, allocations):
        self.traffic_calls.append(allocations)

    def job_image(self, name):
        return self.images[name]

    def update_job(self, name, image, spec, labels=None):
        if self.fail_at == ("update", name):
            raise RuntimeError("update refused")
        self.images[name] = image

    def restore_job(self, name, image):
        self.images[name] = image

    def deploy(self, image, spec, suffix, labels=None):
        if self.fail_at == "deploy":
            raise RuntimeError("deploy refused")

    def require_secrets(self, spec):
        pass

    def require_needs(self, spec):
        pass


def job_images():
    prefix = "us-central1-docker.pkg.dev/lab-proj-123/codesurgeon/codesurgeon"
    return {"agent": f"{prefix}-agent@sha256:{'1' * 64}", "checker": f"{prefix}-checker@sha256:{'2' * 64}"}


def test_a_release_points_the_jobs_at_their_images_before_the_candidate_and_back_on_failure(tmp_path):
    adapter = FakeRun(fail_at="deploy")
    image = "us-central1-docker.pkg.dev/lab-proj-123/codesurgeon/codesurgeon@sha256:" + "3" * 64
    with pytest.raises(RuntimeError, match="Release failed"):
        rollout(adapter, image, spec(), "d" * 32, tmp_path / "receipt.json", job_images=job_images())
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert [phase["state"] for phase in receipt["phases"]][:3] == ["preparing", "updating_jobs", "deploying"]
    assert receipt["jobs"]["agent"] == {"image": job_images()["agent"], "previous": BOOTSTRAP, "updated": True, "restored": True}
    assert receipt["jobs_restored"] == ["agent", "checker"] and adapter.images == {"agent": BOOTSTRAP, "checker": BOOTSTRAP}
    # A job that failed to update is not "restored"; the ones before it are.
    adapter = FakeRun(fail_at=("update", "checker"))
    with pytest.raises(RuntimeError):
        rollout(adapter, image, spec(), "d" * 32, tmp_path / "receipt2.json", job_images=job_images())
    receipt = json.loads((tmp_path / "receipt2.json").read_text())
    assert receipt["failed_phase"] == "updating_jobs" and receipt["jobs_restored"] == ["agent"] and "updated" not in receipt["jobs"]["checker"]


def test_a_release_refuses_job_images_that_do_not_match_the_contract(tmp_path):
    image = "us-central1-docker.pkg.dev/lab-proj-123/codesurgeon/codesurgeon@sha256:" + "3" * 64
    with pytest.raises(ValueError, match="declares agent, checker"):
        rollout(FakeRun(), image, spec(), "d" * 32, tmp_path / "r.json", job_images={"agent": job_images()["agent"]})
    wrong = {**job_images(), "checker": job_images()["agent"]}
    with pytest.raises(ValueError, match="checker's image must be codesurgeon-checker"):
        rollout(FakeRun(), image, spec(), "d" * 32, tmp_path / "r.json", job_images=wrong)
    with pytest.raises(ValueError, match="declares none"):
        rollout(FakeRun(), image, parse_spec("name: codesurgeon\nreviewed: true\n"), "d" * 32, tmp_path / "r.json", job_images=job_images())


def test_a_job_is_updated_with_its_own_settings_and_nothing_of_the_services(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    commands = []
    adapter = CloudRun("lab-proj-123", "us-central1", "codesurgeon", "p@lab-proj-123.iam.gserviceaccount.com", runner=lambda args: commands.append(args) or "",
                       pin_secrets=True)
    adapter.secret_version = lambda secret: "7"
    s = spec()
    adapter.update_job("agent", job_images()["agent"], s, labels={"lab-deploy-plan": "d" * 32})
    adapter.update_job("checker", job_images()["checker"], s)
    agent, checker = commands
    assert agent[:4] == ["gcloud", "run", "jobs", "update"] and agent[4] == "codesurgeon-agent"
    for flag in ("--tasks=1", "--parallelism=1", "--max-retries=0", "--task-timeout=3600s", "--cpu=2", "--memory=4096Mi",
                 "--command=python,-m,smartdev.sandbox_agent", "--args=", "--clear-secrets", "--network=lab-deploy", "--vpc-egress=all-traffic",
                 "--network-tags=lab-deploy-contained", f"--update-labels=lab-deploy-plan={'d' * 32}"):
        assert flag in agent, flag
    assert json.loads(Path("job-env-agent.yaml").read_text()) == {"BUCKET_RUNS": "lab-proj-123-codesurgeon-runs", "ANTHROPIC_VERTEX_PROJECT_ID": "lab-proj-123",
                                                                  "CLOUD_ML_REGION": "global"}
    # The checker: internet egress clears the network, its one secret pinned (production pins every version, D38).
    assert "--clear-network" in checker and "--set-secrets=MODEL_KEY=codesurgeon-MODEL_KEY:7" in checker and "--command=" in checker
    assert adapter.pinned["MODEL_KEY"] == {"secret": "codesurgeon-MODEL_KEY", "version": "7"}


def test_recovery_and_rollback_put_the_jobs_back(tmp_path):
    adapter = FakeRun()
    record = {"jobs": {"agent": {"image": "new", "previous": "old", "updated": True}, "checker": {"image": "new", "previous": "old"}}}
    assert restore_jobs(adapter, record) == ["agent"] and adapter.images["agent"] == "old" and record["jobs"]["agent"]["restored"]
    assert restore_jobs(adapter, record) == []  # once only
    source = Path("labdeploy/release.py").read_text()
    assert source.count("restore_jobs(adapter, saved)") == 2  # recover() and rollback()
    assert '"updating_jobs"' in source[source.index("RECOVERABLE ="):][:200]


def test_the_release_reads_the_job_images_the_deploy_job_verified(tmp_path):
    from labdeploy.release import job_images_from
    from labdeploy.target import Target
    target = Target.model_validate({**TARGET, "project_id": "lab-proj-123", "service": "codesurgeon", "registry": "codesurgeon",
                                    "deploy_service_account": "codesurgeon-deploy@lab-proj-123.iam.gserviceaccount.com",
                                    "probe_service_account": "codesurgeon-probe@lab-proj-123.iam.gserviceaccount.com", "recovery_bucket": "lab-proj-123-codesurgeon-recovery"})
    path = tmp_path / "job-images.json"
    path.write_text(json.dumps(job_images()))
    assert job_images_from({"JOB_IMAGES_FILE": str(path)}, target) == job_images() and job_images_from({}, target) == {}
    path.write_text(json.dumps({"agent": "us-central1-docker.pkg.dev/other/codesurgeon/codesurgeon-agent@sha256:" + "1" * 64}))
    with pytest.raises(ValueError, match="reviewed artifact destination"):
        job_images_from({"JOB_IMAGES_FILE": str(path)}, target)


# ---- 27.6: plans, the clock, the guard, and following the base

def controller(tmp_path, **extra):
    config = Config.model_validate({
        "live_dispatch": True, "database": str(tmp_path / "plans.db"),
        "applications": {
            "codesurgeon": {"repository": "lab/codesurgeon", "service": "codesurgeon", "target": {**TARGET, "service": "codesurgeon", "registry": "codesurgeon"}},
            "bettercare": {"repository": "lab/bettercare", "service": "bettercare", "target": {**TARGET, "service": "bettercare", "registry": "bettercare"}},
        },
        "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["*"], "can_deploy": True}],
        **extra,
    })
    github = FakeGitHub()
    github.spec = yaml.safe_load(CODESURGEON)
    return Controller(config, github, Store(config.database)), config.principals[0], github


def release(store, app_id, sha, spec_dict, **extra):
    store.add("m", {"id": hashlib.md5(f"{app_id}{sha}".encode()).hexdigest(), "application": app_id, "sha": sha, "spec": spec_dict,  # noqa: S324 - an id, not a secret
                    "target": TARGET, **extra}, "succeeded")


def test_a_plan_with_jobs_names_the_base_release_never_reuses_an_image_and_shows_the_worst_case(tmp_path):
    c, principal, github = controller(tmp_path)
    with pytest.raises(ValueError, match="bettercare has no successful staging release yet"):
        c.plan(principal, "codesurgeon")
    bettercare = {"name": "bettercare", "runtime": "node-http", "node": "24", "reviewed": True}
    release(c.store, "bettercare", "b" * 40, bettercare)
    # An earlier release of the same commit would be reused, but not for a service with jobs.
    release(c.store, "codesurgeon", github.sha, yaml.safe_load(CODESURGEON),
            receipt={"image": f"us-central1-docker.pkg.dev/test-project/codesurgeon/codesurgeon@sha256:{'9' * 64}", "provenance": "cosign-kms"})
    plan = c.plan(principal, "codesurgeon")
    assert "reuse_image" not in plan and plan["checks"]["tests"] == "pending_in_ci"
    assert plan["base"] == {"service": "bettercare", "repository": "lab/bettercare", "path": "", "commit": "b" * 40}
    assert plan["jobs"]["agent"]["runs_per_day"] == 30 and plan["jobs"]["agent"]["egress"] == "private" and plan["jobs"]["checker"]["egress"] == "internet"
    assert any("Starts its own jobs (agent, checker)" in line for line in plan["warnings"])
    assert any("test image of bettercare at bbbbbbbbbbbb" in line for line in plan["warnings"])
    block = plan["approval"]
    assert block["jobs"] == plan["jobs"] and block["base"] == plan["base"] and block["cost"]["jobs_ceiling_usd_month"] == plan["cost"]["jobs_ceiling_usd_month"]
    assert "jobs" not in release_approval({"spec": {}, "cost": {}, "target": {}})
    # The base must be a Node.js web service.
    release(c.store, "bettercare", "f" * 40, {**bettercare, "runtime": "python-http", "node": None})
    with pytest.raises(ValueError, match="not a Node.js web service"):
        c.plan(principal, "codesurgeon")


def test_the_dispatch_sends_the_base_only_when_there_is_one():
    from labdeploy.github import GitHub as GitHubClient
    sent = {}

    class Recording(GitHubClient):
        def __init__(self):
            pass

        def request(self, method, path, **kwargs):
            sent.update(kwargs["json"]["inputs"])
            return httpx.Response(204)
    app = type("App", (), {"repository": "lab/codesurgeon", "workflow": "deploy-gcp.yml", "branch": "main", "central": False, "service": "codesurgeon", "path": ""})()
    plan = {"sha": "a" * 40, "spec_hash": "b" * 64, "id": "c" * 32, "target": {}, "base": {"service": "bettercare", "commit": "b" * 40}}
    Recording().dispatch(app, plan)
    assert json.loads(sent["base"]) == plan["base"]
    sent.clear()
    Recording().dispatch(app, {**plan, "base": None})
    assert "base" not in sent


NOW = datetime.datetime(2026, 10, 21, 18, 0, tzinfo=datetime.UTC).timestamp()


def iso(seconds_ago):
    return datetime.datetime.fromtimestamp(NOW - seconds_ago, datetime.UTC).isoformat().replace("+00:00", "Z")


def test_the_guard_cancels_runs_beyond_the_contracts_limits():
    job = spec().jobs["checker"]  # 15 minutes, 30 a day
    executions = [
        {"name": "e/1", "createTime": iso(120), "startTime": iso(120), "taskCount": 1},
        {"name": "e/2", "createTime": iso(60), "startTime": iso(60), "taskCount": 50},
        {"name": "e/3", "createTime": iso(3600), "startTime": iso(3600), "taskCount": 1},
        {"name": "e/4", "createTime": iso(3600), "startTime": iso(3600), "taskCount": 1, "completionTime": iso(100)},
        {"name": "e/5", "createTime": iso(7200), "startTime": iso(7200), "cancelledCount": 1},
    ]
    verdicts = dict(judge(executions, job, NOW))
    assert set(verdicts) == {"e/2", "e/3"} and "50 tasks" in verdicts["e/2"] and "past its 15" in verdicts["e/3"]
    # More runs today than the day allows: the newest running ones go.
    few = spec().jobs["agent"].model_copy(update={"runs_per_day": 2})
    today = [{"name": f"r/{i}", "createTime": iso(600 - i), "startTime": iso(600 - i), "taskCount": 1, **({"completionTime": iso(10)} if i < 2 else {})}
             for i in range(4)]
    assert [name for name, _ in judge(today, few, NOW)] == ["r/2", "r/3"]
    yesterday = [{"name": "y/1", "createTime": iso(86_400 * 2), "startTime": iso(60), "taskCount": 1}]
    assert judge(yesterday, few, NOW) == []


class FakeObserver:
    def __init__(self, ingress="INGRESS_TRAFFIC_ALL", executions=(), jobs=("agent", "checker"), paged=False):
        self.ingress, self.executions, self.got, self.jobs, self.paged = ingress, list(executions), [], list(jobs), paged

    def token(self):
        return "machine-token"

    def service(self, target):
        return {"ingress": self.ingress, "url": "https://x.a.run.app"}

    def get(self, url, **params):
        self.got.append((url, params.get("pageToken")))
        if "/services/" in url:
            return {"ingress": self.ingress}
        if url.endswith("/jobs"):
            labels = {"lab-deploy-managed": "true", "lab-deploy-app": "codesurgeon"}
            return {"jobs": [{"name": f"projects/p/locations/us-central1/jobs/codesurgeon-{job}", "labels": {**labels, "lab-deploy-job": job}}
                             for job in self.jobs] + [{"name": "projects/p/locations/us-central1/jobs/codesurgeon-migrate", "labels": labels},
                                                      {"name": "projects/p/locations/us-central1/jobs/other-agent",
                                                       "labels": {"lab-deploy-app": "other", "lab-deploy-job": "agent"}}]}
        if self.paged and params.get("pageToken") is None:
            return {"executions": [], "nextPageToken": "next"}
        return {"executions": self.executions}


def keeper(tmp_path, observer, handler, **extra):
    c, principal, github = controller(tmp_path, onboarding={"project_id": "test-project", "project_number": "123456789",
                                                              "iap_client_id": "123-abc.apps.googleusercontent.com"}, **extra)
    release(c.store, "codesurgeon", "a" * 40, yaml.safe_load(CODESURGEON), receipt={"url": "https://codesurgeon-x.a.run.app"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return c, Keeper(c, observer, c.config, client=client, clock=lambda: NOW, spawn=lambda work: work())


def test_the_clock_wakes_a_lab_members_app_through_iap_as_its_wake_identity(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        if "generateIdToken" in str(request.url):
            return httpx.Response(200, json={"token": "id-token"})
        return httpx.Response(204)
    c, k = keeper(tmp_path, FakeObserver(), handler)
    assert k.wake_tick() == {"woken": 1, "applications": ["codesurgeon (staging)"]}
    mint, call = seen
    assert str(mint.url).endswith("/codesurgeon-wake@test-project.iam.gserviceaccount.com:generateIdToken")
    assert json.loads(mint.content) == {"audience": "123-abc.apps.googleusercontent.com", "includeEmail": True}
    assert mint.headers["Authorization"] == "Bearer machine-token"
    assert str(call.url) == "https://codesurgeon-x.a.run.app/tick" and call.method == "POST" and call.headers["Authorization"] == "Bearer id-token"
    assert k.snapshot()["wakes"]["codesurgeon (staging)"]["last_status"] == "204"
    assert k.wake_tick()["woken"] == 0  # not before its five minutes
    assert c.store.newest_event("wake")["outcome"] == "204"


def test_the_clock_does_not_wake_a_service_the_cap_closed_and_backs_off_after_failures(tmp_path):
    calls = []
    c, k = keeper(tmp_path, FakeObserver(ingress="INGRESS_TRAFFIC_INTERNAL_ONLY"), lambda request: calls.append(request) or httpx.Response(500))
    k.wake_tick()
    assert calls == [] and k.snapshot()["wakes"]["codesurgeon (staging)"]["last_status"] == "closed by the spending cap"
    entry = k.wakes[("codesurgeon", "staging")]
    intervals = []
    for _ in range(3):
        entry["next"] = 0  # due now
        k.wake_tick()
        intervals.append(entry["next"] - NOW)
    # Every five minutes while it fails, then after three failures in a row half an hour.
    assert entry["failures"] == 4 and intervals == [300, 300, 1800]


def test_the_guard_cancels_and_records_each_cancel(tmp_path):
    executions = [{"name": "projects/p/locations/us-central1/jobs/codesurgeon-agent/executions/x1", "createTime": iso(60), "startTime": iso(60), "taskCount": 3}]
    posted = []
    c, k = keeper(tmp_path, FakeObserver(executions=executions), lambda request: posted.append(str(request.url)) or httpx.Response(200, json={}))
    result = k.guard_tick()
    assert result["checked"] == 2 and result["cancelled"] == 2  # the same listing stands in for both jobs here
    assert posted[0] == "https://run.googleapis.com/v2/projects/p/locations/us-central1/jobs/codesurgeon-agent/executions/x1:cancel"
    assert k.snapshot()["cancels"][0]["reason"] == "it was started with 3 tasks; the contract allows one"
    assert c.store.newest_event("job_guard")["outcome"] == "cancelled"
    (tmp_path / "refused").mkdir()
    _, refused = keeper(tmp_path / "refused", FakeObserver(executions=executions), lambda request: httpx.Response(403))
    assert refused.guard_tick()["cancelled"] == 2 and refused.snapshot()["cancels"][0]["outcome"].startswith("refused: the guard's role")
    assert k.cancel("not/a/name") == "refused: not an execution name"


def test_the_guard_watches_every_labelled_job_reads_every_page_and_counts_a_day_over_its_runs(tmp_path):
    # A job the released contract does not declare (its root made it first, or a rollback left it): every run goes.
    executions = [{"name": "projects/p/locations/us-central1/jobs/codesurgeon-sweep/executions/s1", "createTime": iso(30), "startTime": iso(30), "taskCount": 1}]
    observer = FakeObserver(executions=executions, jobs=("sweep",), paged=True)
    posted = []
    c, k = keeper(tmp_path, observer, lambda request: posted.append(str(request.url)) or httpx.Response(200, json={}))
    result = k.guard_tick()
    assert result["checked"] == 1 and result["cancelled"] == 1  # the migration job and another service's job are not this one's
    assert k.snapshot()["cancels"][0]["reason"] == "the application's released contract declares no such job"
    assert ("https://run.googleapis.com/v2/projects/p/locations/us-central1/jobs/codesurgeon-sweep/executions", "next") in observer.got
    # A run that ended between the listing and the cancel is not reported as a cancel.
    (tmp_path / "ended").mkdir()
    _, ended = keeper(tmp_path / "ended", FakeObserver(executions=executions, jobs=("sweep",)), lambda request: httpx.Response(400))
    assert ended.guard_tick()["cancelled"] == 0 and ended.snapshot()["cancels"] == []
    # More runs started today than the contract allows, even finished ones: recorded once for the digest.
    many = [{"name": f"projects/p/locations/us-central1/jobs/codesurgeon-checker/executions/m{i}", "createTime": iso(600 - i), "startTime": iso(600 - i),
             "completionTime": iso(500 - i), "taskCount": 1} for i in range(31)]
    (tmp_path / "many").mkdir()
    c2, busy = keeper(tmp_path / "many", FakeObserver(executions=many, jobs=("checker",)), lambda request: httpx.Response(200, json={}))
    busy.guard_tick()
    busy.guard_tick()
    over = c2.store.find_events(kind="job_over_limit")
    assert len(over) == 1 and "more than 30 runs" in over[0]["detail"]


def test_jobs_follow_the_application_they_build_on(tmp_path):
    c, principal, github = controller(tmp_path)
    bettercare = {"name": "bettercare", "runtime": "node-http", "node": "24", "reviewed": True}
    release(c.store, "bettercare", "b" * 40, bettercare)
    release(c.store, "codesurgeon", "a" * 40, yaml.safe_load(CODESURGEON), base={"service": "bettercare", "repository": "lab/bettercare", "path": "", "commit": "b" * 40})
    assert c.base_tick()["codesurgeon"] == "up to date"
    locks = {"b" * 40: '{"v": 1}', "f" * 40: '{"v": 2}'}
    github.file = lambda app, sha, filename: locks[sha] if filename == "package-lock.json" else (_ for _ in ()).throw(FileNotFoundError())
    release(c.store, "bettercare", "f" * 40, bettercare)
    result = c.base_tick()["codesurgeon"]
    assert result.startswith("behind: bettercare changed package-lock.json at ffffffffffff")
    assert c.store.newest_event("jobs_base")["outcome"] == "behind"
    locks["f" * 40] = '{"v": 1}'
    assert c.base_tick()["codesurgeon"] == "up to date: bettercare's packages are the same at ffffffffffff"
    release(c.store, "bettercare", "e" * 40, {**bettercare, "node": "26"})
    assert "moved Node.js from 24 to 26" in c.base_tick()["codesurgeon"]


# ---- 27.7: the cap

def test_the_cap_cancels_every_running_execution_of_a_managed_job():
    workflow = Path("infra/gcp/modules/project/cap-workflow.yaml").read_text()
    steps = [next(iter(step)) for step in yaml.safe_load(workflow)["main"]["steps"]]
    assert steps.index("stop_instances") < steps.index("list_jobs") < steps.index("cancel_runs") < steps.index("compose")
    assert "next: list_jobs" in workflow  # a project without databases still reaches the jobs
    assert '"lab-deploy-job" in job.labels' in workflow and '":cancel"' in workflow and '"completionTime" in execution' in workflow
    assert "job runs cancelled" in workflow


# ---- 27.8: production

def promotion_env(**extra):
    return {"OPERATION": "promote", "PLAN_ID": "d" * 32, "APPLICATION": "codesurgeon", "GITHUB_REF": "refs/heads/main", "SOURCE_PLAN": "e" * 32,
            "COMMIT_SHA": "c" * 40, "SPEC_HASH": "f" * 64, "SOURCE_REPOSITORY": "lab/codesurgeon", "STAGING_WORKFLOW": "deploy-gcp.yml",
            "PROVENANCE_INPUT": "cosign-kms", "BAKE_SECONDS": "300", "ROLLOUT_STEPS": "10,50,100",
            "SOURCE_IMAGE": f"us-central1-docker.pkg.dev/lab-staging-123/codesurgeon/codesurgeon@sha256:{'3' * 64}",
            "TARGET_JSON": json.dumps({**TARGET, "project_id": "lab-prod-456", "service": "codesurgeon", "registry": "codesurgeon"}),
            "GCP_PROJECT_ID": "lab-prod-456", "GCP_REGION": "us-central1", "SERVICE": "codesurgeon", "REGISTRY_NAME": "codesurgeon",
            "APPROVED_BY": "brian", "GITHUB_REPOSITORY": "lab/fast-deploy", "GITHUB_RUN_ID": "77", **extra}


def test_a_promotion_takes_the_jobs_images_beside_the_services_only(monkeypatch):
    monkeypatch.setattr(promotion, "target_of", lambda env, service: {"project_id": "lab-prod-456"})
    staging = "us-central1-docker.pkg.dev/lab-staging-123/codesurgeon/codesurgeon"
    env = promotion_env(SOURCE_JOBS=json.dumps({"agent": f"{staging}-agent@sha256:{'1' * 64}"}))
    assert promotion.job_sources(env) == {"agent": f"{staging}-agent@sha256:{'1' * 64}"}
    assert promotion.job_sources(promotion_env()) == {}
    for wrong in ({"agent": f"{staging}-checker@sha256:{'1' * 64}"}, {"agent": f"us-central1-docker.pkg.dev/elsewhere-123/codesurgeon/codesurgeon-agent@sha256:{'1' * 64}"},
                  {"Agent": f"{staging}-Agent@sha256:{'1' * 64}"}):
        with pytest.raises(promotion.PromotionError):
            promotion.job_sources(promotion_env(SOURCE_JOBS=json.dumps(wrong)))
    statement = promotion.job_predicate(env, f"{staging}-agent@sha256:{'1' * 64}", now=datetime.datetime(2026, 10, 21, tzinfo=datetime.UTC))
    assert statement["resourceUri"] == f"oci://{staging}-agent@sha256:{'1' * 64}" and statement["lab_deploy"]["job"] == "codesurgeon-agent"
    assert statement["lab_deploy"]["approved_by"] == ["brian"]


def test_production_verifies_copies_and_signs_each_job_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(promotion, "target_of", lambda env, service: {"project_id": "lab-prod-456"})
    monkeypatch.setattr(promotion, "verify_job", lambda output, env, image: {})
    staging = "us-central1-docker.pkg.dev/lab-staging-123/codesurgeon/codesurgeon"
    env = promotion_env(SOURCE_JOBS=json.dumps({"agent": f"{staging}-agent@sha256:{'1' * 64}"}), COSIGN="cosign", STAGING_KEY="gcpkms://staging",
                        PROVENANCE_KEY="gcpkms://production")
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return "sha256:" + "1" * 64 if args[0] == "gcloud" else ""
    prefix = "us-central1-docker.pkg.dev/lab-prod-456/codesurgeon/codesurgeon"
    assert promotion.promote_jobs(prefix, runner=runner, env=env) == {"agent": f"{prefix}-agent@sha256:{'1' * 64}"}
    assert calls[0][:4] == ["cosign", "verify-attestation", "--key", "gcpkms://staging"]
    assert calls[1][:4] == ["docker", "buildx", "imagetools", "create"] and calls[1][-1] == f"{staging}-agent@sha256:{'1' * 64}"
    assert any(call[:2] == ["cosign", "attest"] and "gcpkms://production" in call for call in calls)
    assert json.loads(Path("job-images.json").read_text()) == {"agent": f"{prefix}-agent@sha256:{'1' * 64}"}
    with pytest.raises(promotion.PromotionError, match="not .*nothing was deployed"):
        promotion.promote_jobs(prefix, runner=lambda args, **kw: "sha256:" + "9" * 64 if args[0] == "gcloud" else "", env=env)
    workflow = (ROOT / ".github/workflows/promote.yml").read_text()
    assert "source_jobs:" in workflow and "python -m labdeploy.promotion jobs --image-prefix" in workflow
    assert workflow.index("Sign the promotion with production's key") < workflow.index("Verify, copy and sign the jobs' images") < workflow.index("Deploy, probe, promote")


def test_a_promotion_plan_carries_the_staging_releases_job_images():
    source = Path("labdeploy/controller.py").read_text()
    assert 'plan["source"]["jobs"] = jobs' in source and "The staging release carries no image for every job its contract declares" in source
    github = Path("labdeploy/github.py").read_text()
    assert 'inputs["source_jobs"] = json.dumps(source["jobs"], sort_keys=True)' in github


def test_the_scheduler_runs_the_clock_the_guard_and_the_base_check():
    text = Path("labdeploy/scheduler.py").read_text()
    assert '"wake": (controller.wake_tick, lambda: 60, 20)' in text and '"job_guard": (controller.job_guard_tick, lambda: 60, 25)' in text
    assert '"jobs_base": (controller.base_tick, lambda: 3600, 600)' in text


def test_the_runner_modules_include_the_job_images_module():
    from labdeploy.updates import RUNNER_MODULES
    assert "jobimages" in RUNNER_MODULES and "promotion" not in RUNNER_MODULES


def test_the_foundation_publishes_what_a_root_reads():
    outputs = Path("infra/gcp/project/outputs.tf").read_text()
    assert 'output "controller_members" { value = module.project.controller_members }' in outputs
    assert 'output "job_guard_role" { value = module.project.job_guard_role }' in outputs
    variables = Path("infra/gcp/modules/service/variables.tf").read_text()
    for name in ("jobs", "wake", "controller_members", "job_guard_role"):
        assert f'variable "{name}"' in variables


def test_copy_of_the_contract_example_parses():
    # The developer contract's example must stay a valid contract.
    text = Path("docs/DEVELOPER-CONTRACT.md").read_text()
    block = text[text.index("## 4c."):]
    block = block[block.index("```yaml") + 7:]
    example = block[:block.index("```")]
    parsed = parse_spec("name: codesurgeon\nreviewed: true\nsecrets: [GITHUB_TOKEN]\nneeds: {buckets: [runs, state]}\n" + example)
    assert set(parsed.jobs) == {"agent", "checker"} and parsed.wake is not None
    copy.deepcopy(parsed)


def test_the_digest_names_each_cancelled_run_and_each_application_the_clock_could_not_wake(tmp_path):
    from labdeploy import chat
    c, principal, github = controller(tmp_path)
    c.store.record_event("job_guard", name="codesurgeon-agent", application="codesurgeon", outcome="cancelled",
                         detail="staging: cancelled x1: it was started with 3 tasks; the contract allows one")
    c.store.record_event("wake", name="/tick", application="codesurgeon", outcome="closed by the spending cap", detail="staging: closed by the spending cap")
    c.store.record_event("wake", name="/tick", application="bettercare", outcome="204", detail="staging: 204")
    record = c.build_digest()
    assert record["attention"] and record["jobs"]["cancels"][0]["job"] == "codesurgeon-agent"
    assert [item["application"] for item in record["jobs"]["wake_failures"]] == ["codesurgeon"]
    text = chat.digest(record)
    assert "the guard cancelled a run of `codesurgeon-agent`" in text and "the clock could not wake it" in text
    (tmp_path / "quiet").mkdir()
    assert "jobs" not in controller(tmp_path / "quiet")[0].build_digest()


def test_the_previous_image_is_read_from_gclouds_json_and_a_job_without_one_is_never_changed(tmp_path):
    v1 = {"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": BOOTSTRAP}]}}}}}}
    v2 = {"template": {"template": {"containers": [{"image": BOOTSTRAP}]}}}
    for shape in (v1, v2):
        adapter = CloudRun("lab-proj-123", "us-central1", "codesurgeon", "p@lab-proj-123.iam.gserviceaccount.com", runner=lambda args, s=shape: json.dumps(s))
        assert adapter.job_image("agent") == BOOTSTRAP
    assert CloudRun("lab-proj-123", "us-central1", "codesurgeon", "p@x.iam.gserviceaccount.com", runner=lambda args: "{}").job_image("agent") == ""
    adapter = FakeRun()
    adapter.images["agent"] = ""
    image = "us-central1-docker.pkg.dev/lab-proj-123/codesurgeon/codesurgeon@sha256:" + "3" * 64
    with pytest.raises(RuntimeError):
        rollout(adapter, image, spec(), "d" * 32, tmp_path / "r.json", job_images=job_images())
    receipt = json.loads((tmp_path / "r.json").read_text())
    assert receipt["failed_phase"] == "updating_jobs" and "cannot be read" in receipt["error"] and adapter.images["checker"] == BOOTSTRAP


@pytest.mark.parametrize("name", ["x-runtime", "x-deploy", "x-probe", "x-wake", "x-migrate"])
def test_a_job_name_never_reads_as_another_applications_identity(name):
    with pytest.raises(ValueError, match="ends in a name the platform uses"):
        parse_spec(f"name: foo\nreviewed: true\njobs: {{{name}: {{dockerfile: D}}}}\n")
    with pytest.raises(ValueError, match="Invalid job name"):
        root(jobs={name: {"cpu": 1, "memory_mib": 512, "timeout_minutes": 1, "buckets": {}, "models": [], "secrets": []}})


def test_a_dependant_whose_newest_plan_names_no_base_is_not_released_every_hour(tmp_path):
    c, principal, github = controller(tmp_path)
    bettercare = {"name": "bettercare", "runtime": "node-http", "node": "24", "reviewed": True}
    release(c.store, "bettercare", "b" * 40, bettercare)
    release(c.store, "codesurgeon", "a" * 40, yaml.safe_load(CODESURGEON), base={"service": "bettercare", "repository": "lab/bettercare", "path": "", "commit": "b" * 40})
    release(c.store, "bettercare", "f" * 40, {**bettercare, "node": "26"})
    c.store.add("m", {"id": "9" * 32, "application": "codesurgeon", "sha": "a" * 40, "spec": {"name": "codesurgeon", "reviewed": True}, "target": TARGET}, "failed")
    assert "builds on no application's test image" in c.base_tick()["codesurgeon"]
