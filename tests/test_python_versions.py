"""Applications on different Pythons and package managers: 3.11 to 3.15, an audit on the app's own Python, exports."""

import re
from pathlib import Path
from typing import get_args

import pytest
import yaml

from labdeploy.analyzer import analyze
from labdeploy.models import AppSpec, spec_hash
from labdeploy.workflows import lay_out

PYTHONS = get_args(AppSpec.model_fields["python"].annotation)


def build_steps():
    return yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))["jobs"]["build"]["steps"]


def step(name=None, id=None):
    return next(s for s in build_steps() if (name and s.get("name") == name) or (id and s.get("id") == id))


def test_python_3_11_to_3_15_and_nothing_older():
    assert PYTHONS == ("3.11", "3.12", "3.13", "3.14", "3.15")
    for version in ("3.14", "3.15"):
        assert AppSpec(name="demo", python=version).python == version
    for version in ("3.10", "3.16", "3"):
        with pytest.raises(ValueError):
            AppSpec(name="demo", python=version)
    assert spec_hash(AppSpec(name="demo")) == spec_hash(AppSpec(name="demo", python="3.12"))  # existing apps keep their hash


def test_the_workflow_accepts_exactly_the_versions_the_model_does():
    policy = step(name="Read the application's test policy")["run"]
    allowed = re.search(r'\[\[ "\$PYTHON" =~ \^3\\\.\(([0-9|]+)\)\$ \]\]', policy).group(1)
    assert tuple(f"3.{minor}" for minor in allowed.split("|")) == PYTHONS


def test_the_audit_runs_on_the_app_s_python_and_reads_the_lock_without_installing():
    # Live finding (2026-10-14): the audit always ran on the platform's 3.12, so numpy==1.24.4, a normal pin on 3.11 that
    # has no 3.12 build, failed to install there and stopped the release; and markers were judged for 3.12, not the app's.
    setup = step(id="app-python")
    # Python services only (a Node.js service has its own steps), whatever the tests policy: tests: none audits too.
    assert setup["if"] == "steps.policy.outputs.runtime == 'python-http'" and setup["with"]["python-version"] == "${{ steps.policy.outputs.python }}"
    audit = step(name="Audit Python dependencies")
    assert audit["env"]["APP_PYTHON"] == "${{ steps.app-python.outputs.python-path }}"
    run = audit["run"]
    assert '"$APP_PYTHON" -m venv "$RUNNER_TEMP/audit-venv"' in run
    assert '"$RUNNER_TEMP/audit-venv/bin/pip" install --require-hashes --only-binary=:all: -r "$GITHUB_WORKSPACE/platform/audit.lock"' in run
    lock, plain = run.split("else")
    assert "if [ -f requirements.lock ]" in lock and 'pip_audit --disable-pip -r requirements.lock' in lock
    assert '"$RUNNER_TEMP/audit-venv/bin/python" -m pip_audit -r requirements.txt' in plain
    assert "\npython -m pip_audit" not in run and not run.lstrip().startswith("python -m")  # never the platform's own Python
    steps = build_steps()
    assert steps.index(setup) < steps.index(step(name="Run unit tests")) < steps.index(audit)


def test_the_analyzer_drafts_the_dockerfile_s_python_or_says_it_is_unsupported():
    newer = analyze({"requirements.txt": "fastapi==0.1\n", "Dockerfile": "FROM python:3.14-slim@sha256:abc\n"}, "demo", [])
    assert newer["draft_spec"]["python"] == "3.14" and any('the draft sets python: "3.14"' in line for line in newer["evidence"])
    older = analyze({"requirements.txt": "fastapi==0.1\n", "Dockerfile": "FROM python:3.10-slim\n"}, "demo", [])
    assert older["draft_spec"]["python"] == "3.12"
    assert any("Python 3.10, which the platform does not test or audit on" in line and "3.11, 3.12, 3.13, 3.14, 3.15" in line
               for line in older["review_required"])
    plain = analyze({"requirements.txt": "fastapi==0.1\n", "Dockerfile": "FROM python:3.12-slim\n"}, "demo", [])
    assert plain["draft_spec"]["python"] == "3.12" and not any("Python 3." in line for line in plain["review_required"])


def test_conda_poetry_and_uv_projects_are_told_how_to_export():
    for lock, manager in (("environment.yml", "conda"), ("poetry.lock", "Poetry"), ("uv.lock", "uv")):
        result = analyze({"pyproject.toml": "[project]\nname = 'x'\n"}, "demo", [lock, "pyproject.toml"])
        line = next(line for line in result["review_required"] if "files found but no requirements.txt" in line)
        assert line.startswith(manager) and "section 1a" in line
    exported = analyze({"requirements.txt": "fastapi==0.1\n"}, "demo", ["uv.lock", "requirements.txt"])
    assert not any("no requirements.txt" in line for line in exported["review_required"])
    contract = Path("docs/DEVELOPER-CONTRACT.md").read_text()
    section = contract[contract.index("## 1a."):contract.index("## 2.")]
    for command in ("uv export --format requirements.txt --no-dev --no-emit-project -o requirements.lock",
                    "poetry export -f requirements.txt --output requirements.lock",
                    "python -m pip list --format=freeze --exclude-editable > requirements.txt",
                    "uv pip compile requirements.txt --universal --python-version 3.12 --generate-hashes -o requirements.lock"):
        assert command in section, command
