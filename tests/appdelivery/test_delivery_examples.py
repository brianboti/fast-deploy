"""The example files under examples/ fit the contracts as they are now."""
import re
from pathlib import Path

import yaml

from appdelivery.config import load_config
from appdelivery.models import BuildRequest, ReleaseRequest

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_release_fits_the_contract():
    release = ReleaseRequest.model_validate(yaml.safe_load((ROOT / "examples" / "release.yaml").read_text()))
    assert release.target.group == "web" and release.repository().endswith("/shop-images/web")
    assert {name: binding.source() for name, binding in release.env.items()} == {
        "LOG_LEVEL": "value", "DB_HOST": "endpoint database_host", "DB_PORT": "endpoint database_port", "DB_PASSWORD": "secret db-password",
        "REDIS_AUTH": "secret cache-auth", "API_KEY": "secret api-key"}


def test_the_example_build_fits_the_contract_and_the_example_configuration():
    build = BuildRequest.model_validate(yaml.safe_load((ROOT / "examples" / "build.yaml").read_text()))
    text = re.sub(r'"REPLACE_WITH[A-Z_a-z-]+"', '"' + "0" * 64 + '"', (ROOT / "appdelivery.example.yaml").read_text())
    config = yaml.safe_load(text)
    source = config["sources"][build.source]
    assert source["infra_owner"] == "shop-team" and build.environment in config["environments"]
    release = yaml.safe_load((ROOT / "examples" / "release.yaml").read_text())
    assert release["image"].startswith(f"{source['registry']}/{build.image}@sha256:"), "the example release names what the example build pushes"


def test_the_example_configuration_loads(tmp_path):
    """The example holds placeholders where token hashes go; with hashes in their place it is a valid configuration."""
    text = re.sub(r'"REPLACE_WITH[A-Z_a-z-]+"', '"' + "0" * 64 + '"', (ROOT / "appdelivery.example.yaml").read_text())
    (tmp_path / "appdelivery.yaml").write_text(text)
    config = load_config(tmp_path / "appdelivery.yaml")
    assert sorted(config.environments) == ["dev", "prod"] and config.environments["prod"].require_attestation
    assert all(environment.build is not None for environment in config.environments.values()) and list(config.sources) == ["shop-web"]
