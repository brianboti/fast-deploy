#!/bin/zsh
# The monthly alert proof (docs/RUNBOOK.md, "Prove the alerts work"; docs/ROADMAP.md 23.6): show that an alert reaches you.
# 1. A notification below the cap's line goes through the spending cap's workflow, which must answer "below cap": the
#    trigger path and the cap's identity work, and nothing is closed.
# 2. A message that is not JSON makes the workflow fail on purpose; within about five minutes the alert "[ENV] the spending
#    cap could not act" must reach the alert email (and the webhook when there is one), and close itself half an hour later.
# 3. The project's notification channels must be enabled.
# You answer whether the email arrived; the script records the drill on the controller's machine (scripts/machine.sh
# record-drill), so the watchdog stops reminding for a month. Nothing is closed and no money moves.
# Usage: scripts/alert-drill.sh PROJECT
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=${1:?usage: scripts/alert-drill.sh PROJECT}
REGION=us-central1
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "PROJECT is a project id"; exit 1; }
FAILED=0
NOTES=()
gcloud workflows describe lab-deploy-spending-cap --location "$REGION" --project "$PROJECT" --format='value(name)' >/dev/null 2>&1 || {
  echo "$PROJECT has no spending cap workflow (lab-deploy-spending-cap): apply the foundation with spending_cap enabled first"; exit 1; }

echo "1. A notification below the cap (cost 0 of a budget of 1)"
BELOW=$(print -n '{"costAmount": 0, "budgetAmount": 1}' | base64)
RESULT=$(gcloud workflows run lab-deploy-spending-cap --location "$REGION" --project "$PROJECT" \
  --data="{\"data\":{\"message\":{\"data\":\"$BELOW\"}}}" --format='value(result)' 2>&1) || true
if [[ "$RESULT" == *"below cap"* ]]; then
  echo "   passed: $RESULT"; NOTES+=("below-cap path ok")
else
  echo "   FAILED: the workflow answered: $RESULT"; FAILED=1; NOTES+=("below-cap path failed")
fi

echo "2. A message that is not JSON: the workflow fails on purpose, and its alert should email you within about five minutes"
gcloud workflows run lab-deploy-spending-cap --location "$REGION" --project "$PROJECT" \
  --data="{\"data\":{\"message\":{\"data\":\"$(print -n 'not json' | base64)\"}}}" --format='value(state)' >/dev/null 2>&1 || true
echo "   started at $(date -u +%H:%M) UTC"

echo "3. The notification channels"
TOKEN=$(gcloud auth print-access-token)
CHANNELS=$(curl -sSf -H "Authorization: Bearer $TOKEN" "https://monitoring.googleapis.com/v3/projects/$PROJECT/notificationChannels") || CHANNELS='{}'
if ! CHANNELS="$CHANNELS" python3 - <<'PY'
import json, os, sys
channels = json.loads(os.environ["CHANNELS"]).get("notificationChannels", [])
for item in channels:
    state = "enabled" if item.get("enabled", True) else "DISABLED"
    print(f"   {item.get('displayName')}: {item.get('type')}, {state}")
if not channels:
    print("   none: alerts reach nobody (set alert_email in the foundation and apply it)")
sys.exit(0 if channels and all(item.get("enabled", True) for item in channels) else 1)
PY
then
  FAILED=1; NOTES+=("a channel is missing or disabled")
fi

echo
echo "Wait for the email \"[...] the spending cap could not act\" (up to five minutes; check spam the first time)."
read -r "ARRIVED?Did it arrive? [y/n] "
if [[ "$ARRIVED" == [yY]* ]]; then
  NOTES+=("alert email arrived")
else
  FAILED=1; NOTES+=("alert email did not arrive")
fi

OUTCOME=ok
(( FAILED )) && OUTCOME=failed
DETAIL="$PROJECT: ${(j:; :)NOTES}"
echo
if [[ -f config.local.yaml ]] && grep -q "project_id" config.local.yaml; then
  scripts/machine.sh record-drill alerts "$OUTCOME" "$DETAIL" || echo "Could not record it on the machine; record it later: scripts/machine.sh record-drill alerts $OUTCOME"
else
  echo "No config.local.yaml here: record it on the controller with: lab-deploy record-drill alerts $OUTCOME --detail \"$DETAIL\""
fi
(( FAILED )) && { echo "FAILED: $DETAIL. docs/RUNBOOK.md, \"Prove the alerts work\", says what to check"; exit 2; }
echo "PASSED: $DETAIL"
