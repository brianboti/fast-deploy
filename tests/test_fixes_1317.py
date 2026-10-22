"""What the CodeSurgeon deploy found (2026-10-22), fixed in 1.31.7: a new custom role outran the apply's one retry, and a
tool dropped an argument it does not take (`app_id` for `application`), which left a stray onboarding record."""
import hashlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FakeGitHub
from fastapi.testclient import TestClient
from test_audit import MANAGER, call, identities
from test_onboarding import PLATFORM, make

from labdeploy.cli import forget_onboarding, main
from labdeploy.controller import Store
from labdeploy.server import build_app, closest

HIERARCHY = ("Error: Error applying IAM policy for project \"p\": googleapi: Error 400: Role (projects/p/roles/labDeployJobReader) "
             "does not exist in the resource's hierarchy., badRequest")
API_NOT_READY = "Error: googleapi: Error 403: Cloud Run Admin API has not been used in project 123 before or it is disabled."
MESSAGES = {"hierarchy": HIERARCHY, "api": API_NOT_READY, "other": "Error: Error creating Service: googleapi: Error 409: already exists"}


def executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def fake_terraform(bin_):
    """`terraform` (or `docker run ... terraform`): each apply takes the next outcome from the file `outcomes`; every
    call, and every pause, is written to `calls`."""
    executable(bin_ / "fake_terraform", f"#!{sys.executable}\n" + """import os, sys
args = sys.argv[1:]
if "hashicorp/terraform:1.13.5" in args:  # docker run ... IMAGE COMMAND...
    args = args[args.index("hashicorp/terraform:1.13.5") + 1:]
fake = os.environ["FAKE"]
open(os.path.join(fake, "calls"), "a").write(args[0] + "\\n")
if args[0] == "apply":
    outcomes = open(os.path.join(fake, "outcomes")).read().split()
    open(os.path.join(fake, "outcomes"), "w").write("\\n".join(outcomes[1:]))
    if outcomes[0] != "ok":
        sys.stderr.write(os.environ["MESSAGE_" + outcomes[0]] + "\\n")
        sys.exit(1)
    print("Apply complete!")
elif args[0] in ("show", "output"):
    print('{"pipeline": {"value": null}, "backup_bucket": {"value": "b"}, "cost_controls": {"value": "c"}}')
""")
    executable(bin_ / "sleep", "#!/bin/sh\necho \"sleep $1\" >> \"$FAKE/calls\"\n")


def environment(root, bin_, outcomes):
    (root / "calls").write_text("")
    (root / "outcomes").write_text("\n".join(outcomes))
    return {"PATH": f"{bin_}:/usr/bin:/bin", "FAKE": str(root), "HOME": str(root), **{f"MESSAGE_{key}": text for key, text in MESSAGES.items()}}


# ---- the pipeline's apply waits out a custom role that IAM has not spread yet, and nothing else
def apply_steps():
    workflow = yaml.safe_load(Path(".github/workflows/infra-apply.yml").read_text())
    return {job: next(step["run"] for step in workflow["jobs"][job]["steps"] if step.get("name") == "Apply exactly that plan")
            for job in ("apply", "apply-production")}


def test_both_apply_jobs_run_the_same_retry():
    steps = apply_steps()
    assert steps["apply"].replace("infra/gcp/apps/", "infra/gcp/apps-production/") == steps["apply-production"]
    assert 'grep -q "does not exist in the resource\'s hierarchy" apply.log' in steps["apply"]


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("outcomes, succeeds, pauses", [
    (["ok"], True, []),
    (["other", "ok"], True, [60]),                                   # any failure: planned again a minute later, once
    (["other", "other"], False, [60]),
    (["hierarchy", "other"], False, [60]),                           # a different second failure stops at once
    (["hierarchy", "hierarchy", "hierarchy", "hierarchy", "ok"], True, [60, 60, 60, 120]),
    (["hierarchy"] * 5, False, [60, 60, 60, 120]),                   # five minutes, then it fails
    (["other", "hierarchy", "ok"], True, [60, 60]),
])
def test_the_apply_waits_out_a_new_custom_role_and_fails_fast_otherwise(tmp_path, outcomes, succeeds, pauses):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake_terraform(bin_)
    (bin_ / "terraform").symlink_to(bin_ / "fake_terraform")
    # labdeploy.infra check: written down, and the plan passes the policy
    executable(bin_ / "python3", "#!/bin/sh\necho \"check $*\" >> \"$FAKE/calls\"\n")
    for job, run in apply_steps().items():
        root = (tmp_path / job).resolve()
        work = root / ("infra/gcp/apps/demo" if job == "apply" else "infra/gcp/apps-production/demo")
        work.mkdir(parents=True)
        (root / "step.sh").write_text(run)
        env = environment(root, bin_, outcomes) | {"APP": "demo", "GITHUB_WORKSPACE": str(root)}
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(root / "step.sh")], cwd=root, env=env,
                              capture_output=True, text=True, timeout=60)
        calls = (root / "calls").read_text().split("\n")
        assert (done.returncode == 0) is succeeds, done.stdout + done.stderr
        assert [int(line.split()[1]) for line in calls if line.startswith("sleep")] == pauses
        tries = len(pauses) + 1
        assert calls.count("apply") == tries and calls.count("plan") == tries - 1
        # every new plan is checked against the policy before it is applied
        assert calls.count("check -m labdeploy.infra check plan.json") == tries - 1
        for index in [i for i, line in enumerate(calls) if line == "apply"][1:]:
            assert calls[index - 3:index] == ["plan", "show", "check -m labdeploy.infra check plan.json"]
        assert (work / "outputs.json").exists() is succeeds


# ---- the foundation, which makes custom roles and grants some of them in one apply, waits them out too
@pytest.mark.skipif(not shutil.which("zsh"), reason="needs zsh")
@pytest.mark.parametrize("outcomes, succeeds, pauses", [
    (["other"], False, []),
    (["api", "ok"], True, [60]),
    (["api", "api"], False, [60]),                                   # Google's "API not ready" is waited out once
    (["api", "hierarchy", "ok"], True, [60, 60]),
    (["hierarchy", "hierarchy", "hierarchy", "hierarchy", "ok"], True, [60, 60, 60, 120]),
    (["hierarchy"] * 5, False, [60, 60, 60, 120]),
])
def test_the_foundation_waits_out_a_new_custom_role_too(tmp_path, outcomes, succeeds, pauses):
    root = tmp_path.resolve()
    bin_ = root / "bin"
    bin_.mkdir()
    fake_terraform(bin_)
    (bin_ / "docker").symlink_to(bin_ / "fake_terraform")
    executable(bin_ / "gcloud", "#!/bin/sh\n[ \"$2\" = print-access-token ] && echo token\nexit 0\n")
    executable(bin_ / "curl", "#!/bin/sh\nexit 0\n")
    (root / "scripts").mkdir()
    (root / "infra" / "gcp" / "project").mkdir(parents=True)
    shutil.copy("scripts/apply-foundation.sh", root / "scripts" / "apply-foundation.sh")
    (root / "infra" / "gcp" / "project" / "staging.tfvars").write_text('project_id = "lab-test-1234"\n')
    env = environment(root, bin_, outcomes) | {"LAB_DEPLOY_APPLY_CONFIRMED": "yes", "PATH": f"{bin_}:{Path(sys.executable).parent}:/usr/bin:/bin"}
    done = subprocess.run(["zsh", str(root / "scripts" / "apply-foundation.sh"), "lab-test-1234"], env=env, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)
    calls = (root / "calls").read_text().split("\n")
    assert (done.returncode == 0) is succeeds, done.stdout + done.stderr
    assert [int(line.split()[1]) for line in calls if line.startswith("sleep")] == pauses
    assert calls.count("apply") == len(pauses) + 1 and calls.count("plan") == len(pauses) + 1  # the first plan, then one per try
    assert ("Done. Foundation applied" in done.stdout) is succeeds


# ---- a tool refuses an argument it does not take
def test_closest_names_what_a_misnamed_argument_meant():
    takes = ["repository", "application", "auto_deploy", "path", "as_is", "all"]
    assert closest("app_id", takes) == "application" and closest("applicaton", takes) == "application"
    assert closest("repo", takes) == "repository" and closest("repo_url", takes) == "repository"
    assert closest("commit", ["application", "commit_sha"]) == "commit_sha" and closest("plan", ["plan_id"]) == "plan_id"
    assert closest("xyz", takes) is None and closest("id", takes) is None


def test_the_misnamed_onboarding_is_refused_before_anything_is_recorded(tmp_path):
    config = identities(tmp_path)
    with TestClient(build_app(config, FakeGitHub())) as client:
        refused = call(client, "onboard_application", repository="brianboti/codesurgeon", path="demo", app_id="codesurgeon")
        assert refused["isError"]
        assert refused["content"][0]["text"] == ("onboard_application has no argument app_id (did you mean application?); it takes repository, "
                                                 "application, auto_deploy, path, as_is, all. Nothing was done: ask again with those names.")
        several = call(client, "create_deployment_plan", applicaton="demo", sha="x")["content"][0]["text"]
        assert several.startswith("create_deployment_plan has no arguments applicaton (did you mean application?) and sha; it takes application, cloud,")
        events = call(client, "list_events")["structuredContent"]["events"]
    assert Store(config.database).onboardings() == []
    refusal = next(event for event in events if event["name"] == "onboard_application")
    assert refusal["outcome"] == "error" and refusal["identity"] == "manager" and "app_id" in refusal["detail"]


def test_every_tool_refuses_an_unknown_argument_and_says_so_in_its_schema(tmp_path):
    with TestClient(build_app(identities(tmp_path), FakeGitHub())) as client:
        tools = client.post("/mcp", headers=MANAGER, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
        assert len(tools) >= 28
        for tool in tools:
            assert tool["inputSchema"]["additionalProperties"] is False, tool["name"]
            refused = call(client, tool["name"], unexpected=1)
            assert refused["isError"] and refused["content"][0]["text"].startswith(f"{tool['name']} has no argument unexpected; it takes "), tool["name"]
        assert not call(client, "list_applications").get("isError")  # the arguments a tool takes still work


# ---- a stray onboarding record can be dropped, and only one that registered nothing
def stray(tmp_path):
    """The incident: a repository onboarded under the name of an application that exists (demo, for lab/demo)."""
    c, brian, gh = make(tmp_path)
    blocked = c.onboard(brian, "lab/newapp", "demo")
    assert blocked["state"] == "blocked" and "already registered for lab/demo" in blocked["error"]
    return c, brian


def test_the_watchdog_points_at_forget_onboarding_for_a_stray_record_and_still_checks_the_application(tmp_path):
    c, _ = stray(tmp_path)
    problems = c.watchdog_problems()
    assert ("demo: an onboarding of lab/newapp stopped under this name (blocked at registering) and registered nothing; drop it with "
            "`lab-deploy forget-onboarding demo`") in problems
    assert not any("unregister demo" in problem for problem in problems)  # that would retire the application demo


def test_a_stray_onboarding_record_is_forgotten_and_the_application_kept(tmp_path):
    c, brian = stray(tmp_path)
    said = forget_onboarding(c.store, c.config.applications, "demo")
    assert said == ("Forgot the onboarding record demo (lab/newapp, blocked at registering); it had registered nothing. "
                    "The application demo (lab/demo) is untouched.")
    assert c.store.onboarding("demo") is None and "demo" in c.config.applications
    assert not any("demo:" in problem and "onboarding" in problem for problem in c.watchdog_problems())
    with pytest.raises(ValueError, match="No onboarding record called demo"):
        forget_onboarding(c.store, c.config.applications, "demo")
    assert c.onboard(brian, "lab/newapp", "newapp")["state"] == "planning"  # the repository onboards under its own name


def test_an_onboarding_that_registered_its_application_is_not_forgotten(tmp_path):
    c, brian, gh = make(tmp_path)
    c.onboard(brian, "lab/newapp")
    with pytest.raises(ValueError, match="registered its application: `lab-deploy decommission newapp` lists what it made, and `lab-deploy unregister newapp`"):
        forget_onboarding(c.store, c.config.applications, "newapp")
    record = c.store.onboarding("newapp")
    del record["steps"]["registering"]  # an entry registered without the step written down: still its own
    c.store.save_onboarding("newapp", record)
    with pytest.raises(ValueError, match="registered its application"):
        forget_onboarding(c.store, c.config.applications, "newapp")
    assert c.store.onboarding("newapp") is not None and "newapp" in c.store.registered_applications()


def test_forget_onboarding_from_the_command_line(tmp_path, monkeypatch, capsys):
    c, _ = stray(tmp_path / "x")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"database": c.config.database, "platform": {"repository": PLATFORM},
                                    "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": c.config.applications["demo"].target.model_dump()}},
                                    "principals": [{"name": "brian", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["*"]}]}))
    monkeypatch.setattr("sys.argv", ["lab-deploy", "forget-onboarding", "demo", "--config", str(path)])
    main()
    assert "Forgot the onboarding record demo (lab/newapp, blocked at registering)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main()
    assert "No onboarding record called demo" in capsys.readouterr().err
    script = Path("scripts/machine.sh").read_text()
    assert 'as_controller "forget-onboarding $APP"' in script and "scripts/machine.sh forget-onboarding APP" in script
