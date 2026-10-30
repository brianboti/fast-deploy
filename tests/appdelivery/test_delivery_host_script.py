"""The host script, run for real against a fake docker and curl, with the input text machine.compose sends."""
import base64
import re

import pytest
from delivery_fakes import DIGEST
from host_fakes import FakeHost

from appdelivery import machine

OLD = f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{'a' * 64}"
NEW = f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}"


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path)


def deploy(host, image=NEW, release="rel-000000000002", env="LOG_LEVEL=info\nDB_PASSWORD=s3cr3t\n", token=None, timeout="1"):
    inputs = {"RELEASE": release, "IMAGE": image, "PORT": "8080", "HEALTH_PATH": "/healthz", "TIMEOUT": timeout, "REGISTRY": "us-central1-docker.pkg.dev", "ENV": env}
    if token:
        inputs["TOKEN"] = token
    return host.report("deploy", inputs)


def test_a_first_deploy_starts_the_image_with_its_environment_and_leaves_no_file(host):
    report = deploy(host, image=OLD, release="rel-000000000001")
    assert report.result == "healthy" and report.fields["image"] == OLD and report.fields["previous"] == ""
    app = host.container("app")
    assert {key: value for key, value in app.items() if key != "flags"} == {"image": OLD, "running": "true", "restart": "always", "release": "rel-000000000001",
                                                                            "env": "LOG_LEVEL=info\nDB_PASSWORD=s3cr3t"}
    assert host.container("app-previous") is None
    assert list(host.run_dir.iterdir()) == [], "the env file must be gone when the script ends"
    assert (host.state / "health_urls").read_text().strip() == "http://127.0.0.1:8080/healthz"


def test_a_second_deploy_demotes_the_running_one_without_a_restart_policy(host):
    deploy(host, image=OLD, release="rel-000000000001")
    report = deploy(host)
    assert report.result == "healthy" and report.fields["previous"] == OLD
    assert host.container("app")["image"] == NEW
    previous = host.container("app-previous")
    assert previous["image"] == OLD and previous["running"] == "false"
    assert previous["restart"] == "no", "a stopped container with restart=always would take the port back at the next boot"


def test_a_third_deploy_keeps_only_one_previous(host):
    deploy(host, image=OLD, release="rel-000000000001")
    deploy(host)
    third = f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{'c' * 64}"
    deploy(host, image=third, release="rel-000000000003")
    assert host.container("app")["image"] == third and host.container("app-previous")["image"] == NEW


@pytest.mark.parametrize("failure", ["unhealthy_image", "crash_image"])
def test_an_unhealthy_image_is_replaced_by_the_previous_one(host, failure):
    deploy(host, image=OLD, release="rel-000000000001")
    host.mark(failure, NEW)
    report = deploy(host)
    assert report.result == "unhealthy" and report.fields["restored"] == "yes" and report.fields["image"] == OLD
    assert f"log line from {NEW}" in report.log()
    app = host.container("app")
    assert app["image"] == OLD and app["running"] == "true" and app["restart"] == "always" and app["release"] == "rel-000000000001"
    assert host.container("app-previous") is None
    assert list(host.run_dir.iterdir()) == []


def test_an_unhealthy_first_deploy_leaves_nothing_running(host):
    host.mark("unhealthy_image", NEW)
    report = deploy(host)
    assert report.result == "unhealthy" and report.fields["restored"] == "none"
    assert host.container("app") is None


def test_a_container_that_cannot_start_counts_as_unhealthy(host):
    deploy(host, image=OLD, release="rel-000000000001")
    host.mark("fail_run")
    report = deploy(host)
    assert report.result == "unhealthy" and report.fields["restored"] == "yes" and host.container("app")["image"] == OLD


def test_a_failed_pull_leaves_the_running_container_alone(host):
    deploy(host, image=OLD, release="rel-000000000001")
    host.mark("fail_pull")
    report = deploy(host)
    assert report.result == "pull_failed" and report.fields["reason"] == "pull"
    app = host.container("app")
    assert (app["image"], app["running"], app["restart"]) == (OLD, "true", "always")
    assert "docker stop" not in host.calls() and "docker rename" not in host.calls()


def test_the_token_reaches_docker_login_on_stdin_only_and_its_config_is_removed(host):
    report = deploy(host, token="ya29.short-lived-token")
    assert report.result == "healthy"
    assert (host.state / "login_password").read_text() == "ya29.short-lived-token"
    config = (host.state / "login_config").read_text().strip()
    assert (host.state / "pull_config").read_text().strip() == config and config.startswith(str(host.run_dir))
    assert list(host.run_dir.iterdir()) == [], "the login directory must be gone"
    assert "ya29" not in host.calls(), "the token must never be on a command line"
    assert "login -u oauth2accesstoken --password-stdin https://us-central1-docker.pkg.dev" in host.calls()


def test_a_refused_login_is_reported_and_nothing_is_changed(host):
    deploy(host, image=OLD, release="rel-000000000001")
    host.mark("fail_login")
    report = deploy(host, token="expired")
    assert report.result == "pull_failed" and report.fields["reason"] == "login" and host.container("app")["image"] == OLD
    assert list(host.run_dir.iterdir()) == []


def test_without_a_token_the_pull_uses_no_login(host):
    deploy(host)
    assert "login" not in host.calls() and (host.state / "pull_config").read_text().strip() == "none"


def test_rollback_starts_the_previous_container_again(host):
    deploy(host, image=OLD, release="rel-000000000001")
    deploy(host)
    report = host.report("rollback", {"PORT": "8080", "HEALTH_PATH": "/healthz", "TIMEOUT": "1"})
    assert report.result == "rolled_back" and report.fields == {"image": OLD, "release": "rel-000000000001"}
    app = host.container("app")
    assert app["image"] == OLD and app["running"] == "true" and app["restart"] == "always"
    assert host.container("app-previous") is None


def test_rollback_without_a_previous_one_says_so_and_changes_nothing(host):
    deploy(host)
    assert host.report("rollback", {"PORT": "8080", "HEALTH_PATH": "/", "TIMEOUT": "1"}).result == "no_previous"
    assert host.container("app")["image"] == NEW


def test_remove_and_status(host):
    deploy(host, image=OLD, release="rel-000000000001")
    deploy(host)
    status = host.report("status", {})
    assert status.result == "status" and status.fields == {"image": NEW, "release": "rel-000000000002", "running": "yes", "previous": OLD}
    assert host.report("remove", {}).result == "removed"
    assert host.container("app") is None and host.container("app-previous") is None
    assert host.report("status", {}).fields == {"image": "", "release": "", "running": "no", "previous": ""}


def test_values_stay_values_even_if_they_look_like_shell(host, tmp_path):
    """Inputs are validated before they are sent, and the script quotes every one anyway: a value never runs."""
    planted = tmp_path / "pwned"
    evil = f"$(touch {planted})`touch {planted}`;touch {planted}"
    text = "\n".join(f"IN_{name}={base64.b64encode(value.encode()).decode()}" for name, value in (
        ("ACTION", "deploy"), ("RELEASE", evil), ("IMAGE", NEW), ("PORT", "8080"), ("HEALTH_PATH", "/"), ("TIMEOUT", "1"), ("REGISTRY", "r"),
        ("ENV", f"X={evil}\n"))) + "\n" + machine.script()
    done = host.run("deploy", {}, raw=text)
    assert done.returncode == 0 and not planted.exists()
    assert host.container("app")["release"] == evil


def test_an_unknown_action_is_a_fault_of_the_script(host):
    text = f"IN_ACTION={base64.b64encode(b'format-disk').decode()}\n" + machine.script()
    done = host.run("deploy", {}, raw=text)
    assert done.returncode == 2 and machine.parse(done.stdout).result == "bad_action"


def test_the_script_has_no_eval_no_sudo_and_reads_inputs_only_through_decode():
    text = "\n".join(line for line in machine.script().splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\beval\b|\bsudo\b|\bsource\b", text)
    for use in re.findall(r".{0,12}\$\{?IN_[A-Z_]+[^\s\"]*", text):
        assert "decode" in use or '-n "' in use, f"input used outside decode: {use!r}"


# ---- the review of 2026-10-30: the container runs under bounds with a reduced capability set
def test_the_container_runs_without_privilege_escalation_under_the_bounds_the_service_sends(host):
    deploy(host, image=OLD, release="rel-000000000001")  # no bounds sent: the script's own defaults
    assert host.container("app")["flags"] == ("--security-opt=no-new-privileges --cap-drop=ALL --cap-add=CHOWN --cap-add=DAC_OVERRIDE --cap-add=FOWNER --cap-add=FSETID "
                                              "--cap-add=SETGID --cap-add=SETUID --cap-add=NET_BIND_SERVICE --cap-add=KILL --cpus=1 --memory=1024m --pids-limit=512 "
                                              "--log-opt=max-size=50m --log-opt=max-file=3")
    inputs = {"RELEASE": "rel-000000000002", "IMAGE": NEW, "PORT": "8080", "HEALTH_PATH": "/healthz", "TIMEOUT": "1", "REGISTRY": "us-central1-docker.pkg.dev",
              "ENV": "", "CPUS": "2.5", "MEMORY_MB": "4096", "PIDS": "1024"}
    assert host.report("deploy", inputs).result == "healthy"
    flags = host.container("app")["flags"]
    assert "--cpus=2.5 --memory=4096m --pids-limit=1024" in flags and "--privileged" not in flags and "--network=host" not in flags
