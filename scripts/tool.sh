#!/bin/zsh
# Call one MCP tool on the local controller as the operator, without ever printing the token.
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
response = httpx.post("http://127.0.0.1:8000/mcp", json=body, timeout=70,
                      headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})
result = response.json().get("result", response.json())
if result.get("isError"):
    print("ERROR:", result["content"][0]["text"])
else:
    print(json.dumps(result.get("structuredContent", result), indent=1))
PY
