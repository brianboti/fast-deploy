#!/bin/zsh
# Call the local controller's REST API as the operator without ever printing the token.
# Usage: scripts/ctl.sh GET /applications | scripts/ctl.sh POST /applications/demo/plans | scripts/ctl.sh GET "/plans/ID?wait_seconds=45"
# The operator token is read from $LAB_DEPLOY_HOME/operator-token.json (default ~/.local/share/lab-deploy), a file
# {"token": "..."} with mode 600 that `python -m labdeploy.cli token` output once.
set -euo pipefail
METHOD="$1"; URL_PATH="$2"
cd "$(dirname "$0")/.."
export LAB_DEPLOY_HOME="${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}"
exec .venv/bin/python - "$METHOD" "$URL_PATH" <<'PY'
import json, os, sys, httpx
token = json.load(open(os.path.join(os.environ["LAB_DEPLOY_HOME"], "operator-token.json")))["token"]
method, path = sys.argv[1], sys.argv[2]
response = httpx.request(method, "http://127.0.0.1:8000" + path, headers={"Authorization": "Bearer " + token}, timeout=70)
print(response.status_code)
try:
    print(json.dumps(response.json(), indent=1))
except ValueError:
    print(response.text[:2000])
PY
