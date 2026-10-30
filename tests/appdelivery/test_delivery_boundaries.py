"""The lines the delivery service must not cross, held by tests (docs/DELIVERY-SPEC.md section 10)."""
import ast
from pathlib import Path

from appdelivery.models import Binding, BuildRequest, Health, ReleaseRequest, Target

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


def test_cloudinfra_does_not_import_this_package():
    for path in modules(ROOT / "cloudinfra"):
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
    assert not (TESTS / "conftest.py").exists(), "this directory keeps its own fakes, imported by name, and no conftest"


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


def test_the_old_workflow_is_gone_and_stays_gone():
    """The Fast Deploy workflow (labdeploy) was removed on 2026-10-30; nothing may bring its name or its directory back."""
    assert not (ROOT / "labdeploy").exists()
    assert "labdeploy" not in (ROOT / "pyproject.toml").read_text()


def test_the_build_contract_carries_paths_and_a_name_and_nothing_that_runs():
    """Images are built only in the trusted project, and only as this service composes the build: a caller names a linked
    source, a commit, two paths in it and an image name. No step, command, argument, secret, account or registry."""
    words = {"steps", "step", "command", "cmd", "args", "entrypoint", "script", "shell", "build_args", "substitutions", "secrets", "secret", "env",
             "service_account", "serviceAccount", "registry", "repository", "url", "pool", "machine_type", "options", "images", "volumes"}
    assert not set(BuildRequest.model_fields) & words, set(BuildRequest.model_fields) & words
    assert BuildRequest.model_config.get("extra") == "forbid"


def test_the_package_builds_nothing_itself():
    """A build runs in Cloud Build; this package only sends it requests, from one module. remote.py runs gcloud and cosign
    only, so no docker, buildah or kaniko can run here."""
    from appdelivery import remote
    assert remote.COMMANDS == ("gcloud", "cosign")
    for path in modules(PACKAGE):
        if "cloudbuild.googleapis.com" in path.read_text():
            assert path.name == "cloudbuild.py", f"{path.relative_to(ROOT)} calls Cloud Build"
