#!/bin/zsh
# Real traffic for a promotion's steps (docs/RUNBOOK.md, "The rollback drill"): each time the new revision's share of a
# production service changes (10, 50, 100 percent), send a batch of requests with your own identity token, so the bake
# judges what the revision does with real requests at every step. Stops at 100 percent, or says so when the earlier split
# comes back (the drill's planted fault caught). Reads the service; sends nothing but those requests.
# Usage: scripts/promotion-traffic.sh PROJECT SERVICE REVISION [REQUESTS_PER_STEP] [PATH]
#   REVISION is SERVICE-r followed by the first 16 characters of the promotion's plan id (get_deployment_status shows it).
set -euo pipefail
PROJECT=${1:?usage: scripts/promotion-traffic.sh PROJECT SERVICE REVISION [REQUESTS_PER_STEP] [PATH]}
SERVICE=${2:?usage: scripts/promotion-traffic.sh PROJECT SERVICE REVISION [REQUESTS_PER_STEP] [PATH]}
REVISION=${3:?usage: scripts/promotion-traffic.sh PROJECT SERVICE REVISION [REQUESTS_PER_STEP] [PATH]}
COUNT=${4:-300}
ROUTE=${5:-/}
[[ "$COUNT" =~ ^[0-9]{1,4}$ ]] || { echo "REQUESTS_PER_STEP is a whole number"; exit 1; }
describe() { gcloud run services describe "$SERVICE" --project "$PROJECT" --region us-central1 --format=json; }
URL=$(describe | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"]["url"])')
LAST=0
for attempt in $(seq 360); do
  SHARE=$(describe | REVISION="$REVISION" python3 -c '
import json, os, sys
traffic = json.load(sys.stdin)["status"].get("traffic", [])
print(sum(t.get("percent", 0) for t in traffic if t.get("revisionName") == os.environ["REVISION"]))')
  if [ "$SHARE" != "$LAST" ] && [ "$SHARE" != "0" ]; then
    TOKEN=$(gcloud auth print-identity-token)
    echo "$(date -u +%H:%M:%S) $REVISION holds $SHARE percent; sending $COUNT requests to $URL$ROUTE"
    for i in $(seq "$COUNT"); do
      curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" "$URL$ROUTE"
    done | sort | uniq -c
    LAST=$SHARE
    [ "$SHARE" = "100" ] && { echo "$(date -u +%H:%M:%S) done: $REVISION serves everyone"; exit 0; }
  fi
  if [ "$SHARE" = "0" ] && [ "$LAST" != "0" ]; then
    echo "$(date -u +%H:%M:%S) $REVISION lost its share at $LAST percent: the earlier split came back"
    exit 3
  fi
  sleep 10
done
echo "$(date -u +%H:%M:%S) gave up after an hour at $LAST percent"
exit 1
