#!/bin/zsh
# Usage: scripts/set-domain.sh YOUR-DOMAIN.ngrok-free.app
# Writes oauth.public_url and the allowed_hosts entry into config.local.yaml (repository root), then validates the config.
set -euo pipefail
DOMAIN="${1:-}"
if [[ ! "$DOMAIN" =~ ^[a-z0-9.-]+\.ngrok(-free)?\.(app|dev|io)$ ]]; then
  echo "Give the ngrok domain only, like abc-def-ghi.ngrok-free.app"; exit 1
fi
cd "$(dirname "$0")/.."
DOMAIN="$DOMAIN" .venv/bin/python - <<'PY'
import os, re, pathlib
domain = os.environ["DOMAIN"]
path = pathlib.Path("config.local.yaml")
lines = path.read_text().splitlines()
# drop any previous oauth block and previous ngrok hosts
out, skipping = [], False
for line in lines:
    if re.match(r"^oauth:\s*$", line):
        skipping = True; continue
    if skipping and re.match(r"^\s+\S", line):
        continue
    skipping = False
    if re.match(r"^\s*-\s*\"?[a-z0-9.-]+\.ngrok(-free)?\.(app|dev|io)\"?\s*$", line):
        continue
    out.append(line)
# add the host after the last allowed_hosts entry
i = next(i for i, line in enumerate(out) if line.startswith("allowed_hosts:"))
j = i + 1
while j < len(out) and re.match(r"^\s*-\s", out[j]):
    j += 1
out.insert(j, f"  - {domain}")
while out and not out[-1].strip():
    out.pop()
out += ["oauth:", f"  public_url: https://{domain}", ""]
path.write_text("\n".join(out))
from labdeploy.config import load_config
config = load_config("config.local.yaml")
print("config.local.yaml updated and valid:", config.oauth.public_url, "| allowed_hosts:", config.allowed_hosts)
PY
