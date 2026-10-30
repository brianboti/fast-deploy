"""The example files under examples/ fit the contracts as they are now."""
import re
from pathlib import Path

import yaml

from appdelivery.config import load_config
from appdelivery.models import ReleaseRequest

ROOT = Path(__file__).resolve().parents[2]


def test_the_example_release_fits_the_contract():
    release = ReleaseRequest.model_validate(yaml.safe_load((ROOT / "examples" / "release.yaml").read_text()))
    assert release.target.group == "web" and release.repository().endswith("/shop-images/web")
    assert {name: binding.source() for name, binding in release.env.items()} == {
        "LOG_LEVEL": "value", "DB_HOST": "endpoint database_host", "DB_PORT": "endpoint database_port", "DB_PASSWORD": "secret db-password",
        "REDIS_AUTH": "secret cache-auth", "API_KEY": "secret api-key"}


def test_the_example_configuration_loads(tmp_path):
    """The example holds placeholders where token hashes go; with hashes in their place it is a valid configuration."""
    text = re.sub(r'"REPLACE_WITH[A-Z_a-z-]+"', '"' + "0" * 64 + '"', (ROOT / "appdelivery.example.yaml").read_text())
    (tmp_path / "appdelivery.yaml").write_text(text)
    config = load_config(tmp_path / "appdelivery.yaml")
    assert sorted(config.environments) == ["dev", "prod"] and config.environments["prod"].require_attestation
