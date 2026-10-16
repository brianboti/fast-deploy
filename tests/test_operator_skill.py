import hashlib
import re
from pathlib import Path

import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Config
from labdeploy.server import INSTRUCTIONS, build_app

SKILL = Path("skills/fast-deploy-operator")
STATES = {"rollback_failed_manual_recovery_required"}  # release states the playbook explains, not tools
TOOL_PREFIX = re.compile(r"\b(?:list|get|create|propose|update|deploy|rollback|cancel|onboard|analyze|compare|estimate)_[a-z_]+\b")


def registered_tools(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "database": str(tmp_path / "db"), "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, FakeGitHub())) as client:
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    return {tool["name"] for tool in listed}


def test_playbook_lists_exactly_the_registered_tools_and_names_no_other(tmp_path):
    tools = registered_tools(tmp_path)
    text = (SKILL / "SKILL.md").read_text()
    table = set(re.findall(r"^\| `([a-z_]+)` \| (?:Look|Record|Act) \|", text, re.M))
    assert table == tools
    mentioned = set(TOOL_PREFIX.findall(text)) | set(TOOL_PREFIX.findall((SKILL / "references/developer-contract.md").read_text()))
    assert mentioned - STATES <= tools, mentioned - STATES - tools


def test_server_instructions_name_only_registered_tools_and_no_longer_refuse_databases(tmp_path):
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", INSTRUCTIONS))
    assert named and named <= registered_tools(tmp_path), named
    assert "databases, GPUs" not in INSTRUCTIONS and "Databases, buckets, topics and migrations are supported" in INSTRUCTIONS


def test_playbook_fits_claude_ai_upload_rules():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert set(front) == {"name", "description"} and front["name"] == SKILL.name
    assert len(front["description"]) <= 200 and "<" not in front["description"]
    assert len(text.splitlines()) < 500
