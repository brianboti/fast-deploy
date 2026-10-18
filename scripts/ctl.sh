#!/bin/zsh
# Call the controller's REST API as the operator without ever printing the token: the machine's when config.local.yaml
# names machine_url (Phase 15), else this laptop's; FAST_DEPLOY_URL chooses another.
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
response = httpx.request(method, base() + path, headers={"Authorization": "Bearer " + token}, timeout=70)
print(response.status_code)
try:
    print(json.dumps(response.json(), indent=1))
except ValueError:
    print(response.text[:2000])
PY
