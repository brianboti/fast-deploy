#!/bin/zsh
# Call one MCP tool on the controller as the operator, without ever printing the token: the machine's when
# config.local.yaml names machine_url (Phase 15), else this laptop's; FAST_DEPLOY_URL chooses another.
# Usage: scripts/tool.sh TOOL_NAME '{"application": "demo"}'
# The operator token is read from $LAB_DEPLOY_HOME/operator-token.json (default ~/.local/share/lab-deploy).
set -euo pipefail
NAME="$1"; ARGS="${2:-{\}}"
cd "$(dirname "$0")/.."
export LAB_DEPLOY_HOME="${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}"
exec .venv/bin/python - "$NAME" "$ARGS" <<'PY'
import json, os, sys, httpx
token = json.load(open(os.path.join(os.environ["LAB_DEPLOY_HOME"], "operator-token.json")))["token"]
name, arguments = sys.argv[1], json.loads(sys.argv[2])
body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
def base():
    """The controller to call: FAST_DEPLOY_URL, else the machine (machine_url in config.local.yaml), else this laptop."""
    if os.environ.get("FAST_DEPLOY_URL"):
        return os.environ["FAST_DEPLOY_URL"].rstrip("/")
    try:
        import yaml
        url = (yaml.safe_load(open("config.local.yaml")) or {}).get("machine_url")
    except OSError:
        url = None
    return (url or "http://127.0.0.1:8000").rstrip("/")
response = httpx.post(base() + "/mcp", json=body, timeout=70,
                      headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
result = response.json().get("result", response.json())
if result.get("isError"):
    print("ERROR:", result["content"][0]["text"])
else:
    print(json.dumps(result.get("structuredContent", result), indent=1))
PY
