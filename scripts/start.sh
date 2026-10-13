#!/bin/zsh
# Lab Deploy on a Mac for Claude chat: starts the controller and the ngrok tunnel. Ctrl+C stops both.
# The first run asks for the fine-grained GitHub token at the Keychain's own hidden prompt (the token never appears on
# a command line or in a shell history) and saves it in the login Keychain as item "lab-deploy-github-token"; later
# runs read it from there. To forget it:  security delete-generic-password -s lab-deploy-github-token
# Paths: the repository is this script's parent directory; logs go to $LAB_DEPLOY_HOME (default ~/.local/share/lab-deploy).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}"
mkdir -p "$HOME_DIR"
cd "$REPO"
DOMAIN=$(sed -n 's|^ *public_url: *https://||p' config.local.yaml | tr -d '"'"'"' ' | head -1)
if [[ -z "$DOMAIN" ]]; then
  echo "Set oauth.public_url in config.local.yaml first: scripts/set-domain.sh YOUR-DOMAIN.ngrok-free.app"
  exit 1
fi
source .venv/bin/activate
check_token() {  # exit 0 when the Keychain token can read the first registered application's repository
  python - <<'PY'
import datetime, os, subprocess, sys, httpx, yaml
token = subprocess.run(["security", "find-generic-password", "-s", "lab-deploy-github-token", "-w"], capture_output=True, text=True).stdout.strip()
if not token.startswith("github_pat_"):
    sys.exit(2)
config = yaml.safe_load(open("config.local.yaml"))
app = sorted(config["applications"].values(), key=lambda a: a["repository"])[0]
r = httpx.get(f"https://api.github.com/repos/{app['repository']}/commits/{app.get('branch', 'main')}",
              headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, timeout=20)
note = ""
header = r.headers.get("github-authentication-token-expiration", "")
if header:
    try:
        when = datetime.datetime.strptime(header, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=datetime.UTC)
        days = (when - datetime.datetime.now(datetime.UTC)).days
        note = f" (expires {when.date()}, {days} days left)" + ("  <-- make a new token soon" if days < 14 else "")
    except ValueError:
        note = f" (expires {header})"
print("GitHub token check:", ("ok" + note) if r.status_code == 200 else f"failed ({r.status_code}) for {app['repository']}")
sys.exit(0 if r.status_code == 200 else 1)
PY
}
until check_token; do
  rc=$?  # not "status": zsh reserves that name, and assigning it aborts the script
  if [[ ! -t 0 ]]; then
    echo "No working GitHub token in the Keychain and no terminal to ask in; run scripts/start.sh once by hand."
    exit 1
  fi
  if [[ $rc -eq 1 ]]; then
    security delete-generic-password -s lab-deploy-github-token >/dev/null 2>&1 || true
    echo "That token cannot read the application repository (expired or wrong permissions). Make a new one."
  fi
  echo "Paste the fine-grained GitHub token at the Keychain prompt below; it is typed hidden and stored only in your login"
  echo "Keychain. Permissions (docs/SETUP.md 7g): all repositories; Contents, Pull requests, Workflows, Actions,"
  echo "Environments, Variables and Administration read and write; Metadata read."
  security add-generic-password -s lab-deploy-github-token -a "$USER" -U -w
done
export GITHUB_TOKEN="$(security find-generic-password -s lab-deploy-github-token -w)" LAB_CONFIG=config.local.yaml
LOG="$HOME_DIR/ngrok.log"
ngrok http 127.0.0.1:8000 --url="$DOMAIN" --log=stdout > "$LOG" 2>&1 &
NGROK=$!
trap 'kill $NGROK 2>/dev/null' EXIT INT TERM
sleep 2
if ! kill -0 $NGROK 2>/dev/null; then
  echo "ngrok did not start; see $LOG"
  exit 1
fi
echo "Tunnel:    https://$DOMAIN  ->  http://127.0.0.1:8000   (log: $LOG)"
echo "Claude:    custom connector URL https://$DOMAIN/mcp"
echo "Sign-in:   paste your operator token on the page Claude opens. Ctrl+C here stops everything."
uvicorn labdeploy.server:create_app --factory --host 127.0.0.1 --port 8000
