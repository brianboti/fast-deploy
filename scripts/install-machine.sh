#!/bin/zsh
# The move (docs/ROADMAP.md 15.5, docs/RUNBOOK.md "The controller's machine"): makes the machine the controller. Run it
# on the laptop, from this repository, after the foundation apply created the machine (controller in terraform.tfvars),
# the GitHub App's key is in Secret Manager (docs/SETUP.md section 4) and the lab's DNS name points at the machine.
#   scripts/install-machine.sh DOMAIN GITHUB_APP_ID [--replace]
# It stops at the first thing that is not right:
#   1. the laptop's controller must be stopped: two controllers would release the same applications twice
#   2. DOMAIN must resolve to the machine's reserved address
#   3. a copy of the database and the configuration rewritten for the machine (python -m labdeploy.machine export)
#   4. both go to the machine through Identity-Aware Proxy (no open SSH port) and are imported there; a machine that
#      already has a database keeps it unless --replace is given (then the old one stays beside the new)
#   5. https://DOMAIN/healthz must answer through Caddy and Let's Encrypt
#   6. machine_url goes into config.local.yaml, so scripts/start.sh keeps the laptop off while the machine answers, and
#      the platform repository's CONTROLLER_HEALTH_URL points the GitHub ping at the machine
set -euo pipefail
DOMAIN=${1:?usage: scripts/install-machine.sh DOMAIN GITHUB_APP_ID [--replace]}
APP_ID=${2:?usage: scripts/install-machine.sh DOMAIN GITHUB_APP_ID [--replace]}
REPLACE=${3:-}
[[ -z "$REPLACE" || "$REPLACE" == "--replace" ]] || { echo "The third argument can only be --replace."; exit 1; }
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || { echo "Give the DNS name only, like deploy.lab.example.edu"; exit 1; }
cd "$(dirname "$0")/.."
PROJECT=$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.local.yaml"))["onboarding"]["project_id"])')
REPO=$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.local.yaml"))["platform"]["repository"])')
ZONE=${FAST_DEPLOY_ZONE:-us-central1-a}
NAME=fast-deploy
iap() { gcloud compute ssh "$NAME" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap --command "$1"; }

echo "1/6 The laptop's controller"
if curl -fsS --max-time 3 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
  echo "    It is running. Stop it first: Ctrl+C in the terminal that runs scripts/start.sh."
  [[ -f "$HOME/Library/LaunchAgents/edu.utsa.lab-deploy.plist" ]] && echo "    The launch agent starts it at login; remove it: launchctl bootout gui/$(id -u)/edu.utsa.lab-deploy"
  exit 1
fi
echo "    stopped"

echo "2/6 $DOMAIN"
ADDRESS=$(gcloud compute addresses describe fast-deploy-controller --region us-central1 --project "$PROJECT" --format='value(address)')
RESOLVED=$(dig +short "$DOMAIN" A | tail -n 1)
if [[ "$RESOLVED" != "$ADDRESS" ]]; then
  echo "    $DOMAIN resolves to '${RESOLVED:-nothing}', not the machine's $ADDRESS. Set an A record $DOMAIN -> $ADDRESS at the"
  echo "    registrar (or controller.dns_zone in terraform.tfvars), wait for it to resolve, and run this again."
  exit 1
fi
echo "    resolves to the machine ($ADDRESS)"

echo "3/6 The copy"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
.venv/bin/python -m labdeploy.machine export --config config.local.yaml --domain "$DOMAIN" --github-app-id "$APP_ID" --out "$WORK/import"

echo "4/6 To the machine (through Identity-Aware Proxy)"
iap "rm -rf /tmp/fast-deploy-import && mkdir -m 700 /tmp/fast-deploy-import"
gcloud compute scp --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap "$WORK/import/config.yaml" "$WORK/import/deployments.db" "$NAME:/tmp/fast-deploy-import/"
iap "sudo /usr/local/sbin/fast-deploy-import ${REPLACE} /tmp/fast-deploy-import"

echo "5/6 https://$DOMAIN/healthz"
for attempt in {1..30}; do
  if curl -fsS --max-time 5 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
    echo "    answers"
    break
  fi
  if (( attempt == 30 )); then
    echo "    no answer after two minutes: scripts/machine.sh logs caddy (the certificate) and scripts/machine.sh logs (the controller)"
    exit 1
  fi
  sleep 4
done

echo "6/6 The laptop steps back"
grep -q '^machine_url:' config.local.yaml || printf '\n# The always-on machine (Phase 15): scripts/start.sh stays off while it answers.\nmachine_url: https://%s\n' "$DOMAIN" >> config.local.yaml
gh variable set CONTROLLER_HEALTH_URL --repo "$REPO" --body "https://$DOMAIN/healthz"
cat <<EOF

The controller runs on the machine. Two things are yours:
  - In Claude: Customize > Connectors, remove the old Fast Deploy connector, add https://$DOMAIN/mcp and sign in with
    your operator token (the old address's sessions do not carry over).
  - The ngrok domain is no longer used; keep it only if this laptop should stay ready as the fallback.
Day to day: scripts/machine.sh status | logs | update COMMIT | config-get FILE | config-put FILE
EOF
