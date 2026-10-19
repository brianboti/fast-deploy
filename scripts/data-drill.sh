#!/bin/zsh
# The data restore drill (docs/RUNBOOK.md, Production): restore a production Postgres database as it was some minutes ago
# into a throwaway clone, check that the clone runs and holds the application's database, then delete the clone. It proves
# that point-in-time recovery works before it is needed. The source is only read; the clone costs its minutes (about a
# cent). Run by the operator with gcloud signed in as the project's owner.
# Usage: scripts/data-drill.sh PROD_PROJECT SERVICE [MINUTES_AGO]      (MINUTES_AGO: 15 by default, at most 7 days)
set -euo pipefail
PROJECT=${1:?usage: scripts/data-drill.sh PROD_PROJECT SERVICE [MINUTES_AGO]}
SERVICE=${2:?usage: scripts/data-drill.sh PROD_PROJECT SERVICE [MINUTES_AGO]}
AGO=${3:-15}
[[ "$SERVICE" =~ ^[a-z][a-z0-9-]{1,19}[a-z0-9]$ ]] || { echo "SERVICE is the application's service name"; exit 1; }
[[ "$AGO" =~ ^[0-9]{1,5}$ ]] && (( AGO >= 1 && AGO <= 10080 )) || { echo "MINUTES_AGO is from 1 to 10080 (seven days)"; exit 1; }
CLONE="$SERVICE-drill-$(date -u +%m%d%H%M)"
POINT=$(python3 -c "import datetime; print((datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=$AGO)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
STARTED=$(date +%s)
cleanup() {
  # Only the drill's own clone is ever deleted: its name holds -drill- and is never the source's.
  if [[ "$CLONE" == "$SERVICE"-drill-* && "$CLONE" != "$SERVICE" ]] && gcloud sql instances describe "$CLONE" --project "$PROJECT" --format='value(name)' > /dev/null 2>&1; then
    echo "Deleting the drill's clone $CLONE"
    gcloud sql instances patch "$CLONE" --no-deletion-protection --project "$PROJECT" --quiet > /dev/null 2>&1 || true
    gcloud sql instances delete "$CLONE" --project "$PROJECT" --quiet
  fi
}
trap cleanup EXIT
gcloud sql instances describe "$SERVICE" --project "$PROJECT" --format='value(settings.backupConfiguration.pointInTimeRecoveryEnabled)' | grep -qi true || {
  echo "$SERVICE has no point-in-time recovery in $PROJECT: production's root turns it on (docs/RUNBOOK.md, Production)"; exit 1; }
echo "Restoring $SERVICE as it was at $POINT into $CLONE (a few minutes)"
gcloud sql instances clone "$SERVICE" "$CLONE" --point-in-time "$POINT" --project "$PROJECT"
STATE=$(gcloud sql instances describe "$CLONE" --project "$PROJECT" --format='value(state)')
DATABASES=$(gcloud sql databases list --instance "$CLONE" --project "$PROJECT" --format='value(name)' | tr '\n' ' ')
echo "Clone state: $STATE; databases: $DATABASES"
[[ "$STATE" == "RUNNABLE" ]] || { echo "FAILED: the clone is $STATE, not RUNNABLE"; exit 2; }
[[ " $DATABASES " == *" app "* ]] || { echo "FAILED: the clone holds no database named app"; exit 2; }
echo "PASSED: $SERVICE restored to $POINT in $(( $(date +%s) - STARTED )) seconds; record it in docs/VALIDATION.md"
