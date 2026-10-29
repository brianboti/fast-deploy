"""The lines the delivery service must not cross, held by tests (docs/DELIVERY-SPEC.md section 10)."""
import ast
import os
import subprocess
import sys
from pathlib import Path

from appdelivery.models import Binding, Health, ReleaseRequest, Target

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "appdelivery"
TESTS = Path(__file__).resolve().parent
FORBIDDEN_IMPORTS = ("cloudinfra", "labdeploy", "github", "templates", "workers")


def modules(directory):
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def imported(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_package_imports_neither_layer_nor_the_old_workflow():
    for path in modules(PACKAGE):
        crossing = imported(path) & set(FORBIDDEN_IMPORTS)
        assert not crossing, f"{path.relative_to(ROOT)} imports {crossing}"


def test_neither_cloudinfra_nor_labdeploy_imports_this_package():
    for directory in (ROOT / "cloudinfra", ROOT / "labdeploy"):
        for path in modules(directory):
            assert "appdelivery" not in imported(path), f"{path.relative_to(ROOT)} imports appdelivery"


def test_nothing_of_this_service_names_the_old_workflow():
    for path in [*modules(PACKAGE), *modules(TESTS)]:
        if path.name == Path(__file__).name:
            continue  # this file names it in order to forbid it
        assert "labdeploy" not in path.read_text().lower(), f"{path.relative_to(ROOT)} names labdeploy"


def test_the_tests_use_no_shared_helper():
    shared = {path.stem for path in (ROOT / "tests").glob("*.py")} | {"conftest", "gcp_fakes"}
    for path in modules(TESTS):
        assert not imported(path) & shared, f"{path.relative_to(ROOT)} imports a shared test helper"
    assert not (TESTS / "conftest.py").exists(), "a conftest.py here would shadow the root one the old tests import as conftest"


def test_only_the_remote_module_runs_commands():
    for path in modules(PACKAGE):
        if path.name == "remote.py":
            continue
        names = imported(path)
        assert not names & {"subprocess", "pty", "pexpect"}, f"{path.relative_to(ROOT)} runs commands"
        assert "os.system" not in path.read_text() and "os.popen" not in path.read_text(), f"{path.relative_to(ROOT)} runs commands"


def test_the_release_contract_has_no_field_that_could_run_something():
    words = {"command", "cmd", "args", "entrypoint", "script", "shell", "source", "repository", "repo", "git", "dockerfile", "build", "volume", "volumes", "mount",
             "privileged", "capabilities", "user", "network_mode", "pid", "device", "devices", "workdir"}
    for model in (ReleaseRequest, Target, Health, Binding):
        assert not set(model.model_fields) & words, f"{model.__name__} has {set(model.model_fields) & words}"
        assert model.model_config.get("extra") == "forbid"


def test_the_tests_pass_with_the_old_workflow_gone():
    """Run this directory again in a fresh interpreter where importing labdeploy fails, without the root conftest."""
    if os.environ.get("APPDELIVERY_WITHOUT_LABDEPLOY"):
        return  # the inner run
    code = ("import sys; sys.modules['labdeploy'] = None; import pytest; "
            f"sys.exit(pytest.main([{str(TESTS)!r}, '--confcutdir', {str(TESTS)!r}, '-q', '-p', 'no:cacheprovider', '--rootdir', {str(TESTS)!r}]))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "APPDELIVERY_WITHOUT_LABDEPLOY": "1"}, capture_output=True, text=True,
                          timeout=300, check=False)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
