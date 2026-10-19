"""1.20.2: what follows a platform release on main by itself (labdeploy/updates.py, docs/RUNBOOK.md "After a release"):
the machine installs main once its checks passed and the delay is over, then the applications' workflows are re-pinned to
the commit the controller runs, merged only where an application's auto_merge allows it."""
import ast
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import chat
from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.scheduler import Scheduler
from labdeploy.server import build_app
from labdeploy.updates import CONTROLLER_PATHS, RUNNER_MODULES, RUNNER_PATHS, PlatformUpdates, running_commit
from labdeploy.workflows import render

PLATFORM = "lab/fast-deploy"
OLD, RUNNING, MAIN = "a" * 40, "c" * 40, "d" * 40
STARTUP = Path("infra/gcp/modules/project/controller-startup.sh")
WORKFLOWS = (".github/workflows/deploy-gcp.yml", ".github/workflows/recover-gcp.yml", ".github/workflows/rightsize-gcp.yml")


def iso(at):
    return datetime.datetime.fromtimestamp(at, datetime.UTC).isoformat().replace("+00:00", "Z")


class Platform(FakeGitHub):
    """The platform repository and one application repository, as far as the updates read and write them."""

    def __init__(self, clock):
        self.clock = clock
        self.main = MAIN
        self.runs = {MAIN: [{"id": 1, "status": "completed", "conclusion": "success", "url": "https://github.com/lab/fast-deploy/actions/runs/1",
                             "updated_at": iso(clock()), "head_sha": MAIN}]}
        self.comparisons = {}
        self.pins = {"lab/demo": OLD}
        self.opened, self.closed, self.merged = [], [], []
        self.prs, self.heads = {}, {}
        self.checks = [{"name": "tests", "status": "completed", "conclusion": "success", "url": "https://github.com/lab/demo/actions/runs/5"}]

    def branch_head(self, repository, branch="main"):
        assert repository == PLATFORM and branch == "main"
        return self.main

    def push_runs(self, repository, workflow, sha):
        assert workflow == "ci.yml"
        return list(self.runs.get(sha, []))

    def comparison(self, repository, base, head):
        return self.comparisons.get((base, head), {"status": "ahead", "files": ["labdeploy/release.py"], "complete": True})

    def contents(self, repository, ref, path):
        if repository == PLATFORM and path.startswith("templates/"):
            return {"sha": "t", "text": Path(path).read_text()}
        if ref in self.heads:  # a pull request's head
            text = self.heads[ref].get(path)
            return None if text is None else {"sha": "h", "text": text}
        if path == ".github/workflows/deploy-gcp.yml" and self.pins.get(repository):
            return {"sha": "w", "text": render(Path("templates/deploy-gcp.yml").read_text(), PLATFORM, self.pins[repository])}
        return None

    def update_workflows(self, app, platform_sha, files):
        number = 20 + len(self.opened)
        head = f"{number:040x}"
        self.opened.append((app.repository, platform_sha, sorted(files)))
        self.heads[head] = dict(files)
        self.prs[number] = {"number": number, "state": "open", "merged": False, "mergeable_state": "clean", "head_sha": head,
                            "url": f"https://github.com/{app.repository}/pull/{number}", "created_at": iso(self.clock() - 600), "merged_by": None}
        return {"number": number, "url": self.prs[number]["url"], "branch": f"lab-deploy/platform-{platform_sha[:12]}", "changed": sorted(files)}

    def close_pull_request(self, repository, number, comment):
        self.closed.append((repository, number, comment))
        self.prs[number]["state"] = "closed"
        return {"number": number, "closed": True}

    def pull_request(self, repository, number):
        return dict(self.prs[number])

    def head_checks(self, repository, sha):
        return list(self.checks)

    def pull_request_files(self, repository, number):
        return sorted(self.heads[self.prs[number]["head_sha"]])

    def merge_pull_request(self, repository, number, title):
        self.merged.append((repository, number, title))
        self.prs[number].update(merged=True, state="closed", merged_by="fast-deploy-ai[bot]")
        return {"merged": True, "sha": "e" * 40, "reason": None}


DEFAULT = {"delay_minutes": 60, "quiet_minutes": 10}


def make(tmp_path, updates=DEFAULT, running=RUNNING, unit=True, **extra):
    """A controller on the machine (running RUNNING) whose clock the test moves; `updates` None leaves the block out."""
    now = [time.time()]
    clock = lambda: now[0]  # noqa: E731 - the tests move it
    data = {"live_dispatch": True, "database": str(tmp_path / "plans.db"), "platform": {"repository": PLATFORM}, "platform_updates": updates,
            "onboarding": {"project_id": "test-project", "project_number": "123456789"},
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["*"], "can_deploy": True}], **extra}
    config = Config.model_validate(data)
    github = Platform(clock)
    c = Controller(config, github, Store(config.database))
    unit_path = tmp_path / "fast-deploy-update.path"
    if unit:
        unit_path.write_text("[Path]\n")
    c.updates = PlatformUpdates(c, running=running, started=now[0], result_path=tmp_path / "result" / "result.json", unit_path=unit_path, clock=clock)
    return c, github, now


def request_of(c):
    path = Path(c.config.database).parent / "update-request"
    return path.read_text().strip() if path.exists() else None


def used_at(c, at):
    """A tool call recorded at a moment of the test's clock."""
    with c.store.connect() as db:
        db.execute("INSERT INTO events VALUES (?, 'manager', NULL, 'tool', 'list_applications', NULL, 'ok', 5, NULL)", (at,))


def kinds(c, kind):
    return [(e["outcome"], e["detail"]) for e in reversed(c.store.recent_events(200)) if e["kind"] == kind]


def test_without_the_block_nothing_happens_and_the_status_says_so(tmp_path):
    c, github, _ = make(tmp_path, updates=None)
    assert c.platform_updates_tick() == {"state": "off"}
    assert c.updates.status()["enabled"] is False and "stay manual" in c.updates.status()["note"]
    assert request_of(c) is None and github.opened == []


def test_the_running_commit_comes_only_from_the_machine_s_release_folder(tmp_path):
    release = tmp_path / "opt" / "releases" / RUNNING / "labdeploy"
    release.mkdir(parents=True)
    assert running_commit(release / "updates.py") == RUNNING
    laptop = tmp_path / "lab-deploy" / "labdeploy"
    laptop.mkdir(parents=True)
    assert running_commit(laptop / "updates.py") is None and running_commit() is None  # this checkout is no release folder
    c, github, _ = make(tmp_path, running=None)
    assert c.platform_updates_tick()["state"] == "not_on_the_machine" and github.opened == [] and request_of(c) is None


def test_the_machine_waits_for_main_s_checks_the_delay_and_a_quiet_moment_then_asks_once(tmp_path):
    c, github, now = make(tmp_path)
    github.runs[MAIN] = [{**github.runs[MAIN][0], "status": "in_progress", "conclusion": None}]
    assert c.updates.machine_step().startswith("waits for main's checks on dddddddddddd to finish")
    github.runs[MAIN] = [{**github.runs[MAIN][0], "status": "completed", "conclusion": "failure"}]
    assert "waits for a newer commit on main" in c.updates.machine_step()
    c.updates.machine_step()
    assert [outcome for outcome, _ in kinds(c, "platform_update")] == ["checks_failed"]  # recorded once, not every five minutes
    github.runs[MAIN] = [{**github.runs[MAIN][0], "conclusion": "success", "updated_at": iso(now[0])}]  # re-run and passed
    assert "installs dddddddddddd after" in c.updates.machine_step() and request_of(c) is None
    now[0] += 3601
    used_at(c, now[0] - 60)
    assert "at a quiet moment" in c.updates.machine_step() and request_of(c) is None
    now[0] += 601
    assert c.updates.machine_step().startswith("the machine installs dddddddddddd") and request_of(c) == MAIN
    assert c.updates.machine_step().startswith("the machine installs dddddddddddd")  # waits for the machine; asked once
    Path(c.config.database).parent.joinpath("update-request").unlink()  # the machine's service took it and is installing
    assert c.updates.machine_step().startswith("the machine installs dddddddddddd") and request_of(c) is None  # never asked twice
    now[0] += 7300
    assert "nothing reported for over an hour" in c.updates.machine_step() and request_of(c) is None
    assert [outcome for outcome, _ in kinds(c, "platform_update")] == ["checks_failed", "requested"]


def test_main_moving_starts_its_checks_and_the_delay_again(tmp_path):
    c, github, now = make(tmp_path)
    c.updates.machine_step()
    now[0] += 3000
    newer = "e" * 40
    github.main = newer
    github.runs[newer] = [{"id": 2, "status": "completed", "conclusion": "success", "url": "u", "updated_at": iso(now[0]), "head_sha": newer}]
    now[0] += 1000  # the first tip's delay would be over by now; the new tip's is not
    assert "installs eeeeeeeeeeee after" in c.updates.machine_step() and request_of(c) is None
    # A contract release or a document landing behind the change keeps the change's clock: it waits for its own checks only.
    contract = "f" * 40
    github.main = contract
    github.comparisons[(newer, contract)] = {"status": "ahead", "files": ["apps/sophia/platform.yaml"], "complete": True}
    github.runs[contract] = [{"id": 4, "status": "in_progress", "conclusion": None, "url": "u", "updated_at": iso(now[0]), "head_sha": contract}]
    assert "waits for main's checks on ffffffffffff" in c.updates.machine_step()
    github.runs[contract] = [{**github.runs[contract][0], "status": "completed", "conclusion": "success", "updated_at": iso(now[0] + 2000)}]
    now[0] += 2700  # an hour after the change to the controller passed, though only 700 seconds after the contract's checks
    assert c.updates.machine_step().startswith("the machine installs ffffffffffff") and request_of(c) == contract


def test_only_what_the_controller_runs_restarts_it_and_it_never_goes_back(tmp_path):
    c, github, now = make(tmp_path)
    now[0] += 3700
    github.comparisons[(RUNNING, MAIN)] = {"status": "ahead", "files": ["apps/sophia/platform.yaml", "docs/RUNBOOK.md", "infra/gcp/apps/x/main.tf"], "complete": True}
    assert "keeps cccccccccccc" in c.updates.machine_step() and request_of(c) is None
    assert set(CONTROLLER_PATHS) == {"labdeploy/", "templates/", "schema/", "requirements.lock"}
    for status in ("behind", "diverged"):
        c.store.save_platform_update("machine", {})
        github.comparisons[(RUNNING, MAIN)] = {"status": status, "files": ["labdeploy/server.py"], "complete": True}
        assert "never goes back" in c.updates.machine_step() and request_of(c) is None
    c.store.save_platform_update("machine", {})
    github.comparisons[(RUNNING, MAIN)] = {"status": "ahead", "files": ["docs/a.md"] * 300, "complete": False}  # cannot rule a change out
    assert c.updates.machine_step().startswith("the machine installs") and request_of(c) == MAIN


def test_a_release_on_its_way_to_github_or_a_missing_update_service_holds_the_request(tmp_path):
    c, github, now = make(tmp_path, unit=False)
    now[0] += 3700
    plan = {"id": "p1", "application": "demo", "sha": "1" * 40, "expires_at": time.time() + 900}
    c.store.add("manager", plan, state="dispatching")
    assert "once the release being dispatched" in c.updates.machine_step()
    with c.store.connect() as db:
        db.execute("UPDATE plans SET state='dispatched' WHERE id='p1'")
    assert "update service is not installed yet" in c.updates.machine_step() and request_of(c) is None


def test_the_machine_s_result_is_recorded_once_and_a_commit_that_went_back_is_not_asked_for_again(tmp_path):
    c, github, now = make(tmp_path, updates={"delay_minutes": 0, "quiet_minutes": 0})
    c.platform_updates_tick()
    assert request_of(c) == MAIN
    Path(c.config.database).parent.joinpath("update-request").unlink()  # the machine's service takes the request first
    result = c.updates.result_path
    result.parent.mkdir()
    result.write_text(json.dumps({"commit": MAIN, "previous": RUNNING, "outcome": "rolled_back", "detail": "cccccccccccc still runs: did not answer",
                                  "started_at": "2026-10-19T10:00:00Z", "finished_at": "2026-10-19T10:03:00Z"}))
    c.platform_updates_tick()
    c.platform_updates_tick()
    outcomes = [outcome for outcome, _ in kinds(c, "platform_update")]
    assert outcomes == ["running", "requested", "rolled_back"]  # once each
    assert request_of(c) is None and "did not work on the machine" in c.updates.status()["next"]
    newer = "e" * 40  # a fix on main is asked for
    github.main = newer
    github.runs[newer] = [{"id": 3, "status": "completed", "conclusion": "success", "url": "u", "updated_at": iso(now[0]), "head_sha": newer}]
    c.platform_updates_tick()
    assert request_of(c) == newer
    # After the update the controller runs the new commit: the result and the new commit are recorded, nothing is asked.
    Path(c.config.database).parent.joinpath("update-request").unlink()
    result.write_text(json.dumps({"commit": newer, "previous": RUNNING, "outcome": "installed", "detail": "answered", "finished_at": "2026-10-19T11:00:00Z"}))
    c.updates.running = newer
    c.platform_updates_tick()
    events = kinds(c, "platform_update")
    assert [outcome for outcome, _ in events][-3:] == ["requested", "installed", "running"] and "was cccccccccccc" in events[-1][1]
    assert request_of(c) is None and c.updates.status()["next"].startswith("nothing: the controller runs the tip of main")
    garbage = {"commit": "../../etc/passwd", "outcome": "installed", "finished_at": "x"}
    result.write_text(json.dumps(garbage))
    assert c.updates.collect_result() is None  # only a full commit with a known outcome is read


def test_paused_switched_off_or_a_fixed_platform_commit_keeps_everything_manual(tmp_path):
    c, github, now = make(tmp_path, updates={"delay_minutes": 0, "quiet_minutes": 0}, paused=True)
    assert c.platform_updates_tick() == {"state": "paused"}
    (tmp_path / "off").mkdir()
    c2, github2, _ = make(tmp_path / "off", updates={"machine": False, "workflows": False, "delay_minutes": 0})
    assert c2.platform_updates_tick() == {"machine": "off", "workflows": "off"} and request_of(c2) is None and github2.opened == []
    (tmp_path / "pinned").mkdir()
    c3, _, _ = make(tmp_path / "pinned", updates={"delay_minutes": 0}, platform={"repository": PLATFORM, "commit": OLD})
    assert c3.platform_updates_tick()["state"] == "pinned"


def test_the_scheduler_runs_the_job_and_reads_a_changed_configuration_file_without_a_request(tmp_path):
    path = tmp_path / "config.yaml"
    data = {"database": str(tmp_path / "plans.db"), "platform": {"repository": PLATFORM}, "platform_updates": {},
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]}
    path.write_text(yaml.safe_dump(data))
    live = LiveConfig(Config.model_validate(data), str(path))
    c = Controller(live, FakeGitHub(), Store(live.database))
    scheduler = Scheduler(c, live)
    assert scheduler.jobs["platform_updates"][1]() == 300
    path.write_text(yaml.safe_dump({**data, "paused": True}))
    os.utime(path, (time.time() + 5, time.time() + 5))
    scheduler.refresh()
    assert live.paused is True and scheduler.run_job("platform_updates")["last_result"] == {"state": "paused"}


# ---- the applications' workflows
def test_apps_are_re_pinned_to_the_controller_s_commit_after_the_delay_and_merged_where_auto_merge_allows(tmp_path):
    c, github, now = make(tmp_path, updates={"machine": False, "delay_minutes": 60})
    assert c.updates.workflows_step()["demo"].startswith("waits until")  # the controller runs its commit for the delay first
    assert github.opened == []
    now[0] += 3601
    github.checks = [{"name": "tests", "status": "in_progress", "conclusion": None, "url": "u"}]
    assert c.updates.workflows_step() == {"demo": "open"}
    assert github.opened == [("lab/demo", RUNNING, sorted(WORKFLOWS))] and github.merged == []
    record = c.store.platform_update("workflows:demo")
    assert record["waiting"].startswith("its checks") and record["from"] == OLD and set(record["files"]) == set(WORKFLOWS)
    github.checks = [{"name": "tests", "status": "completed", "conclusion": "success", "url": "u"}]
    assert c.updates.workflows_step() == {"demo": "merged"}
    assert github.merged == [("lab/demo", 20, "Update Fast Deploy workflows to platform cccccccccccc")]
    assert c.updates.workflows_step() == {"demo": "merged"} and len(github.opened) == 1  # settled for this commit
    events = kinds(c, "workflow_update")
    assert [outcome for outcome, _ in events] == ["opened", "merged"] and "by the controller" in events[1][1]
    assert c.store.recent_events(1)[0]["identity"] == "platform-updates"


def test_auto_merge_off_or_anything_but_the_rendered_files_waits_for_a_person(tmp_path):
    c, github, now = make(tmp_path, updates={"machine": False, "delay_minutes": 0},
                          application_settings={}, applications={"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "auto_merge": False}})
    assert c.updates.workflows_step() == {"demo": "open"} and github.merged == []
    assert c.store.platform_update("workflows:demo")["waiting"].startswith("a person to merge https://github.com/lab/demo/pull/20: auto_merge is off")
    (tmp_path / "gates").mkdir()
    c, github, now = make(tmp_path / "gates", updates={"machine": False, "delay_minutes": 0})
    github.checks = [{"name": "tests", "status": "in_progress", "conclusion": None, "url": "u"}]
    c.updates.workflows_step()
    head = github.prs[20]["head_sha"]
    github.checks = [{"name": "tests", "status": "completed", "conclusion": "success", "url": "u"}]
    for change, expected in ((lambda: github.heads[head].update({"app.py": "print('hi')"}), "changes more than the three workflow files"),
                             (lambda: github.heads[head].update({WORKFLOWS[0]: "name: replaced"}), "differs from what the platform rendered"),
                             (lambda: github.prs[20].update(mergeable_state="dirty"), "has conflicts"),
                             (lambda: github.checks.__setitem__(0, {"name": "tests", "status": "completed", "conclusion": "failure", "url": "u"}), "tests ended failure")):
        saved = (dict(github.heads[head]), dict(github.prs[20]), list(github.checks))
        change()
        c.updates.workflows_step()
        waiting = c.store.platform_update("workflows:demo")["waiting"]
        assert waiting.startswith("a person") and expected in waiting and github.merged == [], (expected, waiting)
        github.heads[head], github.prs[20], github.checks = saved
    assert [outcome for outcome, _ in kinds(c, "workflow_update")].count("waiting") >= 1
    c.updates.workflows_step()
    assert github.merged and c.store.platform_update("workflows:demo")["state"] == "merged"


def test_an_app_whose_runner_did_not_change_or_that_pins_a_newer_commit_is_left_alone(tmp_path):
    c, github, now = make(tmp_path, updates={"machine": False, "delay_minutes": 0})
    github.comparisons[(OLD, RUNNING)] = {"status": "ahead", "files": ["docs/RUNBOOK.md", "apps/x/platform.yaml", "scripts/machine.sh"], "complete": True}
    assert c.updates.workflows_step() == {"demo": "current"} and github.opened == []
    assert "nothing its workflows use changed" in c.store.platform_update("workflows:demo")["note"]
    assert "labdeploy/release.py" in RUNNER_PATHS and "labdeploy/server.py" not in RUNNER_PATHS and "templates/" in RUNNER_PATHS
    c.store.save_platform_update("workflows:demo", {})
    github.comparisons[(OLD, RUNNING)] = {"status": "behind", "files": [], "complete": True}
    assert c.updates.workflows_step() == {"demo": "ahead"} and github.opened == []
    c.store.save_platform_update("workflows:demo", {})
    github.pins["lab/demo"] = RUNNING
    assert c.updates.workflows_step() == {"demo": "current"}
    c.store.save_platform_update("workflows:demo", {})
    github.pins["lab/demo"] = None
    assert c.updates.workflows_step() == {"demo": "no_workflows"} and github.opened == []


def imported(module):
    """The platform modules one module imports, at any depth of its code (an import inside a function counts)."""
    names = set()
    for node in ast.walk(ast.parse(Path(f"labdeploy/{module}.py").read_text())):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names |= {node.module.split(".")[0]} if node.module else {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("labdeploy."):
            names.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            names |= {alias.name.split(".")[1] for alias in node.names if alias.name.startswith("labdeploy.")}
    return {name for name in names if Path(f"labdeploy/{name}.py").is_file()}


def test_the_runner_modules_are_exactly_what_the_workflow_templates_import():
    found = set()
    for template in ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml"):
        text = Path("templates", template).read_text()
        found |= set(re.findall(r"python -m labdeploy\.([a-z_]+)", text)) | set(re.findall(r"from labdeploy\.([a-z_]+) import", text))
    closure, todo = set(), sorted(found)
    while todo:
        module = todo.pop()
        if module not in closure:
            closure.add(module)
            todo += sorted(imported(module))
    assert closure == set(RUNNER_MODULES)
    # The package's own file holds the version only: a version number alone is no reason to re-pin every application.
    package = ast.parse(Path("labdeploy/__init__.py").read_text()).body
    assert [type(node).__name__ for node in package] == ["Expr", "Assign"] and package[1].targets[0].id == "__version__"


def test_a_newer_re_pin_closes_the_one_it_replaces_and_a_closed_one_is_not_opened_again(tmp_path):
    c, github, now = make(tmp_path, updates={"machine": False, "delay_minutes": 0})
    github.checks = [{"name": "tests", "status": "in_progress", "conclusion": None, "url": "u"}]
    c.updates.workflows_step()
    assert [pr[1] for pr in github.opened] == [RUNNING] and github.closed == []
    newer, newest = "e" * 40, "f" * 40
    c.updates.running = newer  # the machine moved on before #20 was merged
    github.comparisons[(OLD, newer)] = {"status": "ahead", "files": ["templates/deploy-gcp.yml"], "complete": True}
    c.updates.workflows_step()
    assert [number for _, number, _ in github.closed] == [20] and "Superseded" in github.closed[0][2]  # only its own, never a person's
    assert [pr[1] for pr in github.opened] == [RUNNING, newer] and "closed the older #20" in kinds(c, "workflow_update")[-1][1]
    github.prs[21]["state"] = "closed"  # a person closes the re-pin
    assert c.updates.workflows_step() == {"demo": "closed"}
    assert c.updates.workflows_step() == {"demo": "closed"} and len(github.opened) == 2  # not opened again for this commit
    assert "#21 was closed" in c.store.platform_update("workflows:demo")["note"]
    c.updates.running = newest  # the next release opens a new one, and leaves the closed one closed
    github.comparisons[(OLD, newest)] = {"status": "ahead", "files": ["labdeploy/release.py"], "complete": True}
    c.updates.workflows_step()
    assert [pr[1] for pr in github.opened] == [RUNNING, newer, newest] and [number for _, number, _ in github.closed] == [20]


def test_central_paused_and_onboarding_apps_are_skipped_and_writes_off_writes_nothing(tmp_path):
    central = {"repository": "lab/sophia", "service": "sophia", "target": {**TARGET, "service": "sophia", "registry": "sophia"}, "contract": "platform",
               "pipeline_repository": PLATFORM, "workflow": "deploy.yml", "environment": "staging-sophia"}
    paused = {"repository": "lab/paused", "service": "paused", "target": {**TARGET, "service": "paused", "registry": "paused"}, "paused": True}
    c, github, _ = make(tmp_path, updates={"machine": False, "delay_minutes": 0},
                        applications={"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}, "sophia": central, "paused": paused})
    c.store.save_onboarding("demo", {"application": "demo", "state": "applying"})
    assert c.updates.workflows_step() == {"demo": "onboarding (applying) adds its workflows", "paused": "paused"} and github.opened == []
    (tmp_path / "quiet").mkdir()
    c, github, _ = make(tmp_path / "quiet", updates={"machine": False, "delay_minutes": 0}, live_dispatch=False)
    assert "writes are disabled" in c.updates.workflows_step() and github.opened == []


def test_a_failure_for_one_app_is_recorded_once_and_tried_again(tmp_path):
    c, github, _ = make(tmp_path, updates={"machine": False, "delay_minutes": 0})

    def broken(repository, base, head):
        raise ValueError("GitHub said no")
    github.comparison = broken
    assert c.updates.workflows_step()["demo"].startswith("error: GitHub said no")
    c.updates.workflows_step()
    assert [outcome for outcome, _ in kinds(c, "workflow_update")] == ["error"]
    del github.comparison
    assert c.updates.workflows_step() == {"demo": "merged"} and c.store.platform_update("workflows:demo")["error"] is None  # opened and merged


def test_get_controller_status_and_its_chat_answer_say_what_happens_next(tmp_path):
    c, github, now = make(tmp_path, updates={"delay_minutes": 60, "quiet_minutes": 10})
    c.platform_updates_tick()
    view = c.updates.status()
    assert view["enabled"] and view["runs"] == RUNNING and view["main"] == MAIN and view["main_checks"] == "passed" and "installs dddddddddddd after" in view["next"]
    status = {"version": "1.20.2", "live_dispatch": True, "platform_updates": view}
    text = chat.controller(status)
    assert "After a release: the controller runs `cccccccccccc`; next, installs dddddddddddd after" in text
    view["last_result"] = {"outcome": "rolled_back", "commit": MAIN, "detail": "cccccccccccc still runs", "finished_at": "2026-10-19T10:03:00Z"}
    view["applications"] = {"demo": {"state": "open", "waiting": "a person to merge https://github.com/lab/demo/pull/20: auto_merge is off for demo"}}
    text = chat.controller({**status, "platform_updates": view})
    assert "went back from `dddddddddddd`" in text and "Re-pins waiting for a person: `demo`" in text and "ask me about any item above" in text
    assert chat.after_release({"enabled": False}) == ([], False)
    with TestClient(build_app(c.config, github)) as client:
        headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "get_controller_status", "arguments": {}}}).json()["result"]
        assert result["structuredContent"]["platform_updates"]["enabled"] is True


def test_the_configuration_block_is_checked(tmp_path):
    base = {"applications": {}, "principals": []}
    assert Config.model_validate({**base, "platform_updates": {}}).platform_updates.model_dump() == {"machine": True, "workflows": True, "delay_minutes": 60, "quiet_minutes": 10}
    assert Config.model_validate(base).platform_updates is None
    for wrong in ({"delay_minutes": -1}, {"quiet_minutes": 241}, {"delay_minutes": 10_081}, {"later": True}):
        with pytest.raises(ValueError):
            Config.model_validate({**base, "platform_updates": wrong})


# ---- the machine's side: the startup script's update service
def test_the_startup_script_installs_the_update_service_started_by_the_request_file():
    text = STARTUP.read_text()
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(STARTUP)], check=True)
    path_unit = re.search(r"cat > /etc/systemd/system/fast-deploy-update\.path <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    assert "PathExists=/var/lib/fast-deploy/update-request" in path_unit and "WantedBy=multi-user.target" in path_unit
    service = re.search(r"cat > /etc/systemd/system/fast-deploy-update\.service <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    assert "Type=oneshot" in service and "ExecStart=/usr/local/sbin/fast-deploy-update" in service and "User=" not in service  # root
    assert "systemctl enable --quiet --now fast-deploy-update.path" in text and "install -d -m 0755 /var/lib/fast-deploy-update" in text
    install = re.search(r"cat > /usr/local/sbin/fast-deploy-install <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    assert 'exec 9>/run/fast-deploy-install.lock' in install and "flock -w 900 9" in install and 'FAST_DEPLOY_INSTALL_LOCKED' in install
    assert 'merge-base --is-ancestor "$COMMIT" origin/main' in install  # still: only merged code runs here
    assert 'rm -rf "$environment"' in install and '!= "$(readlink "$OPT/venv")"' in install  # the environment in use is never removed
    update = script_text()
    assert update.index('rm -f "$REQUEST"') < update.index("exec 9>")  # the request leaves first, whatever happens next
    assert '! -L "$REQUEST"' in update and "head -c 100" in update


def script_text():
    return re.search(r"cat > /usr/local/sbin/fast-deploy-update <<'EOF'\n(.*?)\nEOF", STARTUP.read_text(), re.S).group(1)


def executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def machine(tmp_path):
    """The update service run against a folder that stands for the machine, with git, systemd and curl replaced."""
    if not shutil.which("bash") or not shutil.which("python3"):
        pytest.skip("needs bash and python3")
    root = tmp_path / "machine"
    for folder in ("data", "opt/releases", "opt/repo", "out", "bin"):
        (root / folder).mkdir(parents=True)
    (root / "opt" / "releases" / RUNNING).mkdir()
    (root / "opt" / "current").symlink_to(root / "opt" / "releases" / RUNNING)
    (root / "invocation").write_text("first\n")
    script = root / "fast-deploy-update"
    executable(script, script_text())
    bin_ = root / "bin"
    # git: commits are ordered by their first letter; nothing starting with 9 is on main.
    executable(bin_ / "git", "#!/usr/bin/env python3\nimport sys\nargs = sys.argv[1:]\n"
               "if 'merge-base' in args:\n    a, b = args[-2], args[-1]\n    b = 'f' * 40 if b == 'origin/main' else b\n"
               "    sys.exit(0 if not a.startswith('9') and a <= b else 1)\nsys.exit(0)\n")
    executable(bin_ / "flock", "#!/bin/sh\nexit 0\n")
    executable(bin_ / "sleep", "#!/bin/sh\n[ -f \"$FAKE/restart-during-watch\" ] && echo second > \"$FAKE/invocation\"\nexit 0\n")
    executable(bin_ / "curl", "#!/bin/sh\n[ -f \"$FAKE/unhealthy\" ] && exit 7\nexit 0\n")
    executable(bin_ / "systemctl", "#!/bin/sh\ncase \"$*\" in\n  *InvocationID*) cat \"$FAKE/invocation\" ;;\n  *is-active*) exit 0 ;;\nesac\nexit 0\n")
    # fast-deploy-install: switches `current`, or fails when told to; every call is written down.
    executable(root / "install", "#!/bin/sh\necho \"$1\" >> \"$FAKE/installs\"\n[ -f \"$FAKE/fail-$1\" ] && { echo 'The controller did not answer.'; exit 1; }\n"
               "mkdir -p \"$FAKE/opt/releases/$1\" && ln -sfn \"$FAKE/opt/releases/$1\" \"$FAKE/opt/current\"\necho \"The controller runs $1.\"\n")

    def run(content, soak=10):
        request = root / "data" / "update-request"
        if content is not None:
            request.write_text(content)
        env = {"PATH": f"{bin_}:{os.environ['PATH']}", "FAKE": str(root), "FAST_DEPLOY_DATA": str(root / "data"), "FAST_DEPLOY_OPT": str(root / "opt"),
               "FAST_DEPLOY_UPDATE_OUT": str(root / "out"), "FAST_DEPLOY_INSTALL": str(root / "install"), "FAST_DEPLOY_LOCK": str(root / "lock"),
               "FAST_DEPLOY_SOAK_SECONDS": str(soak)}
        done = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        assert not request.exists(), "the request must leave the folder whatever happens"
        result = json.loads((root / "out" / "result.json").read_text())
        installs = (root / "installs").read_text().split() if (root / "installs").exists() else []
        return result, installs, done
    run.root = root
    return run


def test_the_update_service_installs_a_newer_commit_on_main_and_watches_it(machine):
    result, installs, done = machine(MAIN + "\n")
    assert result["outcome"] == "installed" and result["commit"] == MAIN and result["previous"] == RUNNING and installs == [MAIN], done.stdout + done.stderr
    assert result["finished_at"].endswith("Z") and oct(stat.S_IMODE((machine.root / "out" / "result.json").stat().st_mode)) == "0o644"


def test_the_update_service_refuses_anything_but_a_newer_commit_on_main(machine):
    for content, expected in (("not a commit", "full 40-character commit"), ("b" * 40, "does not contain the commit that runs"),
                              ("9" * 40, "not on main"), (RUNNING, "runs it already")):
        result, installs, _ = machine(content)
        assert expected in result["detail"] and installs == [], (content, result)
    assert result["outcome"] == "current"
    (machine.root / "data" / "held").write_text("")
    result, installs, _ = machine(MAIN)
    assert result["outcome"] == "refused" and "held" in result["detail"] and installs == []


def test_a_symlinked_request_is_never_read(machine):
    secret = machine.root / "secret"
    secret.write_text(MAIN)
    (machine.root / "data" / "update-request").symlink_to(secret)
    result, installs, _ = machine(None)
    assert result["outcome"] == "refused" and result["commit"] == "" and installs == [] and secret.exists()


def test_the_update_service_goes_back_when_the_new_commit_does_not_answer_or_restarts(machine):
    (machine.root / f"fail-{MAIN}").write_text("")
    result, installs, _ = machine(MAIN)
    assert result["outcome"] == "rolled_back" and "cccccccccccc still runs" in result["detail"] and installs == [MAIN]
    (machine.root / f"fail-{MAIN}").unlink()
    (machine.root / "installs").unlink()
    (machine.root / "restart-during-watch").write_text("")  # it answers at first, then systemd restarts it during the watch
    result, installs, _ = machine(MAIN)
    assert result["outcome"] == "rolled_back" and installs == [MAIN, RUNNING] and "runs again" in result["detail"]
    assert os.readlink(machine.root / "opt" / "current").endswith(RUNNING)


def test_the_operator_can_run_the_startup_script_again_and_read_the_update_s_log_and_result():
    text = Path("scripts/machine.sh").read_text()
    assert "  startup)\n    iap 'sudo google_metadata_script_runner startup' ;;" in text
    assert '[[ "$UNIT" == "update" ]] && UNIT=fast-deploy-update' in text and "systemctl is-active fast-deploy-update.path" in text
    assert "cat /var/lib/fast-deploy-update/result.json" in text
    usage = subprocess.run(["awk", "NR > 1 && /^#/ {print; next} NR > 1 {exit}", "scripts/machine.sh"], capture_output=True, text=True, check=True).stdout
    assert usage.splitlines()[-1].startswith("#   scripts/machine.sh ssh") and "set -euo" not in usage and "scripts/machine.sh startup" in usage
