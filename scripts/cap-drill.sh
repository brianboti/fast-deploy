#!/bin/zsh
# The cap-fire drill (docs/RUNBOOK.md, "The cap-fire drill"): prove that a budget notification at the spending cap closes a
# project's services, along the path a real one takes. It publishes a made-up notification to the budget topic (a cost at
# the cap's line of a budget of 100: the amounts are in the message and no money moves), so Pub/Sub, Eventarc and the
# workflow all run; checks that every managed service turned internal-only and answers 404 from outside; then reopens each
# one exactly as it was, with the same call the cap makes (no new revision), and starts any database it stopped. It refuses
# a project whose cap would detach billing: a drill must never reach that line. The services are closed for a minute or
# two, so run it when that is acceptable. Run by the operator with gcloud signed in as the project's owner.
# Usage: scripts/cap-drill.sh PROJECT
set -euo pipefail
PROJECT=${1:?usage: scripts/cap-drill.sh PROJECT}
REGION=us-central1
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "PROJECT is a project id"; exit 1; }
DIR=$(mktemp -d)
RUN="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/services"
token() { gcloud auth print-access-token; }
services() { curl -sSf -H "Authorization: Bearer $(token)" "$RUN" > "$DIR/services.json"; }
SOURCE=$(gcloud workflows describe lab-deploy-spending-cap --location "$REGION" --project "$PROJECT" --format='value(sourceContents)' 2>/dev/null) || {
  echo "$PROJECT has no spending cap (no workflow lab-deploy-spending-cap): nothing to drill"; exit 1; }
# The detach step as the foundation rendered it: "false" == "true" means detach is off. Anything else is refused.
[[ "$SOURCE" == *'${"false" == "true" and exceeded >='* ]] || {
  echo "REFUSED: the cap in $PROJECT may detach billing (or its definition is not the platform's); the drill never runs there"; exit 1; }
THRESHOLD=$(print -r -- "$SOURCE" | python3 -c 'import re, sys; print(re.search(r"exceeded < ([0-9.]+)\}", sys.stdin.read()).group(1))')

# What the cap will close: managed services that are open and not lifted, and managed databases that run and are not lifted.
services
gcloud sql instances list --project "$PROJECT" --format=json --quiet > "$DIR/instances.json" 2>/dev/null || echo "[]" > "$DIR/instances.json"
python3 - "$DIR" <<'PY' || { rm -rf "$DIR"; exit 1; }
import json, pathlib, sys
folder = pathlib.Path(sys.argv[1])
services = json.loads((folder / "services.json").read_text()).get("services", [])
managed = [s for s in services if (s.get("labels") or {}).get("lab-deploy-managed") == "true" and (s.get("labels") or {}).get("lab-deploy-cap-lifted") != "true"]
closed = [s["name"].rsplit("/", 1)[1] for s in managed if s.get("ingress") == "INGRESS_TRAFFIC_INTERNAL_ONLY"]
if closed:
    print(f"REFUSED: {', '.join(closed)} is internal-only already without lab-deploy-cap-lifted: the cap may be in force now. "
          "Follow docs/RUNBOOK.md, 'The spending cap fired', first.")
    sys.exit(1)
(folder / "open.txt").write_text("".join(f"{s['name'].rsplit('/', 1)[1]} {s.get('ingress') or 'INGRESS_TRAFFIC_ALL'} {s.get('uri') or ''}\n" for s in managed))
instances = json.loads((folder / "instances.json").read_text())
running = [i["name"] for i in instances if ((i.get("settings") or {}).get("userLabels") or {}).get("lab-deploy-managed") == "true"
           and ((i.get("settings") or {}).get("userLabels") or {}).get("lab-deploy-cap-lifted") != "true"
           and (i.get("settings") or {}).get("activationPolicy") == "ALWAYS"]
(folder / "databases.txt").write_text("".join(f"{name}\n" for name in running))
PY
OPEN=("${(@f)$(< "$DIR/open.txt")}"); OPEN=(${OPEN:#})
DATABASES=("${(@f)$(< "$DIR/databases.txt")}"); DATABASES=(${DATABASES:#})
echo "The cap in $PROJECT closes services at $THRESHOLD of the budget, and never detaches billing."
if (( ${#OPEN} )); then
  echo "The drill closes for a minute or two: ${(j:, :)${OPEN%% *}}"
else
  echo "No managed service is open: the drill proves the path only (the workflow closes nothing)."
fi
(( ${#DATABASES} )) && echo "It also stops these databases and starts them again (a few minutes): ${(j:, :)DATABASES}"
read -r "ANSWER?Type yes to run the drill: "
[[ "$ANSWER" == "yes" ]] || { echo "Nothing done."; rm -rf "$DIR"; exit 0; }

reopen() {
  # Only what the drill closed: each service back to the ingress it had, then each database it stopped.
  local line name ingress
  for line in "${OPEN[@]}"; do
    name=${line%% *}; ingress=${${line#* }%% *}
    curl -sSf -X PATCH -H "Authorization: Bearer $(token)" -H "Content-Type: application/json" -d "{\"ingress\": \"$ingress\"}" \
      "$RUN/$name?updateMask=ingress" > /dev/null && echo "Reopened $name ($ingress)" || echo "COULD NOT REOPEN $name: run gcloud run services update $name --region $REGION --project $PROJECT --ingress=all"
  done
  for name in "${DATABASES[@]}"; do
    gcloud sql instances patch "$name" --project "$PROJECT" --activation-policy=ALWAYS --quiet > /dev/null && echo "Started $name again" \
      || echo "COULD NOT START $name: run gcloud sql instances patch $name --project $PROJECT --activation-policy=ALWAYS"
  done
}
state_of() { python3 -c 'import json, sys; print({s["name"].rsplit("/", 1)[1]: s.get("ingress") for s in json.load(open(sys.argv[1])).get("services", [])}.get(sys.argv[2], ""))' "$DIR/services.json" "$1"; }
answer() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" || true; }
finish() { reopen; rm -rf "$DIR"; }
trap finish EXIT

MESSAGE=$(THRESHOLD=$THRESHOLD python3 -c 'import json, os; t = float(os.environ["THRESHOLD"]); print(json.dumps({"budgetDisplayName": "Fast Deploy cap drill", "alertThresholdExceeded": t, "costAmount": round(t * 100 + 0.01, 2), "budgetAmount": 100, "currencyCode": "USD"}))')
ENCODED=$(print -rn -- "$MESSAGE" | base64 | tr -d '\n')
STARTED=$(date +%s)
gcloud pubsub topics publish lab-deploy-budget-alerts --project "$PROJECT" --message "$MESSAGE" > /dev/null
echo "$(date -u +%H:%M:%S) published a notification of $MESSAGE"
FAILED=0
if (( ${#OPEN} )); then
  for attempt in $(seq 60); do
    sleep 5
    services
    left=0
    for line in "${OPEN[@]}"; do [[ "$(state_of "${line%% *}")" == "INGRESS_TRAFFIC_INTERNAL_ONLY" ]] || left=$(( left + 1 )); done
    (( left == 0 )) && break
  done
  if (( left )); then
    echo "FAILED: $left service(s) still open five minutes after the notification"; FAILED=1
  else
    echo "$(date -u +%H:%M:%S) every managed service is internal-only, $(( $(date +%s) - STARTED )) seconds after the notification"
    for line in "${OPEN[@]}"; do
      url=${line##* }
      [[ -n "$url" && "$url" != *INGRESS* ]] || continue
      for attempt in $(seq 24); do code=$(answer "$url"); [[ "$code" == "404" ]] && break; sleep 5; done
      [[ "$code" == "404" ]] && echo "${line%% *} answers 404 from outside" || { echo "FAILED: ${line%% *} answers $code from outside, not 404"; FAILED=1; }
    done
  fi
fi
# The drill's own execution: the one whose argument carries this message.
for attempt in $(seq 24); do
  curl -sSf -H "Authorization: Bearer $(token)" \
    "https://workflowexecutions.googleapis.com/v1/projects/$PROJECT/locations/$REGION/workflows/lab-deploy-spending-cap/executions?view=FULL&pageSize=20" > "$DIR/executions.json"
  RESULT=$(ENCODED=$ENCODED python3 -c '
import json, os, sys
for e in json.load(open(sys.argv[1])).get("executions", []):
    event = json.loads(e.get("argument") or "{}")
    if ((event.get("data") or {}).get("message") or {}).get("data") == os.environ["ENCODED"]:
        result = json.loads(e["result"]) if e.get("result") else (e.get("error") or {}).get("payload") or ""
        print(e.get("state"), "|", result)
        break' "$DIR/executions.json")
  [[ "$RESULT" == SUCCEEDED* || "$RESULT" == FAILED* || "$RESULT" == CANCELLED* ]] && break
  sleep 5
done
echo "The drill's execution: ${RESULT:-not found}"
[[ "$RESULT" == "SUCCEEDED | cap enforced at"* ]] || FAILED=1
trap - EXIT
reopen
# Pub/Sub delivers at least once: a second delivery of the drill's message would close everything again. Look a minute later.
if (( ${#OPEN} )); then
  sleep 60
  services
  again=()
  for line in "${OPEN[@]}"; do [[ "$(state_of "${line%% *}")" == "${${line#* }%% *}" ]] || again+=("${line%% *}"); done
  if (( ${#again} )); then echo "Closed again by a second delivery of the message: ${(j:, :)again}; reopening"; reopen; fi
fi
for line in "${OPEN[@]}"; do
  url=${line##* }
  [[ -n "$url" && "$url" != *INGRESS* ]] || continue
  for attempt in $(seq 24); do code=$(answer "$url"); [[ "$code" != "404" ]] && break; sleep 5; done
  [[ "$code" != "404" ]] && echo "${line%% *} is open again (it answers $code without a token)" \
    || { echo "FAILED: ${line%% *} still answers 404 after reopening"; FAILED=1; }
done
rm -rf "$DIR"
(( FAILED )) && { echo "FAILED: see the lines above; every service the drill closed was reopened"; exit 2; }
echo "PASSED: the cap fired on a notification at its line and closed ${#OPEN} service(s); all are open again. Record it in docs/VALIDATION.md"
