"""The command line: a token, a configuration check, and a plan read from the infrastructure layer."""
import hashlib
import json

import httpx
import yaml
from delivery_fakes import config_dict, infra_response, infra_transport, release_dict

from appdelivery import cli
from appdelivery.infra import InfraClient


def files(tmp_path, release=None, config=None):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config or config_dict()))
    (tmp_path / "release.yaml").write_text(yaml.safe_dump(release or release_dict()))
    return str(tmp_path / "config.yaml"), str(tmp_path / "release.yaml")


def test_token_prints_a_token_and_its_hash(capsys):
    assert cli.main(["token"]) == 0
    lines = dict(line.split(": ", 1) for line in capsys.readouterr().out.strip().splitlines())
    assert hashlib.sha256(lines["token"].encode()).hexdigest() == lines["token_sha256"]


def test_check_config_names_environments_and_principals(tmp_path, capsys):
    config, _ = files(tmp_path)
    assert cli.main(["check-config", config]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "environments": ["dev", "prod"], "principals": ["shop-team", "shop-oncall"]}


def test_a_bad_configuration_is_reported_as_problems(tmp_path, capsys):
    data = config_dict()
    data["environments"]["prod"]["service_account"] = "not-an-account"
    config, _ = files(tmp_path, config=data)
    assert cli.main(["check-config", config]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_plan_reads_the_request_and_prints_the_plan(tmp_path, capsys, monkeypatch):
    config, release = files(tmp_path)
    monkeypatch.setenv("APPDELIVERY_PROD_INFRA_TOKEN", "infra-token")
    original = InfraClient.for_environment
    monkeypatch.setattr(InfraClient, "for_environment", classmethod(lambda klass, cfg, env: original.__func__(klass, cfg, env, httpx.Client(transport=infra_transport()))))
    assert cli.main(["plan", release, "--config", config, "--as", "shop-team"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "PLANNED" and [item["name"] for item in planned["machines"]] == ["shop-web-1", "shop-web-2"]


def test_plan_refusals_are_structured(tmp_path, capsys, monkeypatch):
    config, release = files(tmp_path)
    monkeypatch.setenv("APPDELIVERY_PROD_INFRA_TOKEN", "infra-token")
    original = InfraClient.for_environment
    answer = infra_response(access=[])
    monkeypatch.setattr(InfraClient, "for_environment", classmethod(lambda klass, cfg, env: original.__func__(klass, cfg, env, httpx.Client(transport=infra_transport(answer)))))
    assert cli.main(["plan", release, "--config", config, "--as", "shop-team"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "NOT_GRANTED"


def test_a_malformed_release_file_never_reaches_the_layer(tmp_path, capsys):
    config, release = files(tmp_path, release=release_dict(image="shop:latest"))
    assert cli.main(["plan", release, "--config", config, "--as", "shop-team"]) == 1
    assert json.loads(capsys.readouterr().out)["problems"][0]["code"] == "INVALID_RELEASE"


def test_the_example_configuration_is_valid_once_the_hashes_are_filled_in(tmp_path, capsys):
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "appdelivery.example.yaml").read_text()
    text = text.replace("REPLACE_WITH_THE_HASH_FROM_app-delivery_token", "0" * 64).replace("REPLACE_WITH_ANOTHER_HASH", "1" * 64)
    (tmp_path / "example.yaml").write_text(text)
    assert cli.main(["check-config", str(tmp_path / "example.yaml")]) == 0
    assert json.loads(capsys.readouterr().out)["environments"] == ["dev", "prod"]
