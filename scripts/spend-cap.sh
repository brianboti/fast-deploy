#!/bin/zsh
# Google's native budget spend cap for Cloud Run (docs/SETUP.md 7i): a monthly budget scoped to one project and the Cloud
# Run service that pauses every Cloud Run workload in the project once the month's gross estimated cost passes it. It needs
# nothing of this platform to work, and it does not stop Cloud SQL, buckets or topics (the platform's own cap does that).
# Needs gcloud signed in as someone who may manage budgets on the project's billing account (Billing Account Administrator,
# or Billing Account Costs Manager with Editor on the project), and the Budget API enabled in the project (the foundation does).
# Usage: scripts/spend-cap.sh check  PROJECT_ID
#        scripts/spend-cap.sh create PROJECT_ID AMOUNT_USD     (shows the request, then waits for a typed "yes")
set -euo pipefail
ACTION=${1:?usage: spend-cap.sh check|create PROJECT_ID [AMOUNT_USD]}
PROJECT=${2:?usage: spend-cap.sh check|create PROJECT_ID [AMOUNT_USD]}
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || { echo "Not a project id: $PROJECT"; exit 1; }
CLOUD_RUN="services/152E-C115-5142"  # Cloud Run's service id in Cloud Billing
ACCOUNT=$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')
[[ "$ACCOUNT" =~ ^billingAccounts/[0-9A-F-]+$ ]] || { echo "$PROJECT has no billing account linked"; exit 1; }
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
TOKEN=$(gcloud auth print-access-token)
API="https://billingbudgets.googleapis.com/v1/$ACCOUNT/budgets"
call() { curl -fsS -H "Authorization: Bearer $TOKEN" -H "x-goog-user-project: $PROJECT" -H "Content-Type: application/json" "$@"; }

# The spend caps on this project's Cloud Run, one per line: name, amount, state. The API names projects by number.
caps() {
  call "$API?pageSize=100" | PROJECT="$PROJECT" NUMBER="$NUMBER" CLOUD_RUN="$CLOUD_RUN" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
projects = {"projects/" + os.environ["PROJECT"], "projects/" + os.environ["NUMBER"]}
for budget in data.get("budgets", []):
    scope = budget.get("budgetFilter", {})
    cap = budget.get("spendCap")
    if cap and projects & set(scope.get("projects", [])) and os.environ["CLOUD_RUN"] in scope.get("services", []):
        amount = budget.get("amount", {}).get("specifiedAmount", {})
        print(budget["name"], amount.get("units", "?"), amount.get("currencyCode", ""), cap.get("outputState", cap.get("inputState", "?")))
if data.get("nextPageToken"):
    print("(more than 100 budgets on this billing account: only the first page was read)", file=sys.stderr)
'
}

case "$ACTION" in
  check)
    found=$(caps)
    if [[ -z "$found" ]]; then
      echo "No native Cloud Run spend cap on $PROJECT. Create one: scripts/spend-cap.sh create $PROJECT AMOUNT_USD"
      exit 1
    fi
    echo "$found"
    echo "Record it in the controller configuration: cloud_run_spend_caps: {$PROJECT: AMOUNT}"
    ;;
  create)
    AMOUNT=${3:?usage: spend-cap.sh create PROJECT_ID AMOUNT_USD}
    [[ "$AMOUNT" =~ ^[1-9][0-9]{0,5}$ ]] || { echo "AMOUNT_USD must be a whole number of dollars"; exit 1; }
    found=$(caps)
    if [[ -n "$found" ]]; then
      echo "A spend cap already covers Cloud Run on $PROJECT; nothing created:"
      echo "$found"
      exit 0
    fi
    BODY=$(PROJECT="$PROJECT" AMOUNT="$AMOUNT" CLOUD_RUN="$CLOUD_RUN" python3 -c '
import json, os
print(json.dumps({
    "displayName": "Lab Deploy: Cloud Run spend cap",
    "budgetFilter": {"projects": ["projects/" + os.environ["PROJECT"]], "services": [os.environ["CLOUD_RUN"]],
                     "calendarPeriod": "MONTH", "creditTypesTreatment": "EXCLUDE_ALL_CREDITS"},
    "amount": {"specifiedAmount": {"currencyCode": "USD", "units": os.environ["AMOUNT"]}},
    "thresholdRules": [{"thresholdPercent": 0.5}, {"thresholdPercent": 0.8}, {"thresholdPercent": 1.0}],
    "notificationsRule": {"enableProjectLevelRecipients": True},
    "spendCap": {"inputState": "CONFIGURED"},
}, indent=2))')
    echo "About to create on $ACCOUNT:"
    echo "$BODY"
    echo "When the month's gross estimated Cloud Run cost in $PROJECT passes $AMOUNT USD, every Cloud Run service there answers 5xx"
    echo "until the cap is lifted in Cloud Billing or the month ends."
    if [[ "${LAB_DEPLOY_APPLY_CONFIRMED:-}" == "yes" ]]; then
      echo "Creating: confirmed through LAB_DEPLOY_APPLY_CONFIRMED."
    elif [[ -t 0 ]]; then
      read -r "answer?Create this spend cap? Type yes to continue: "
      [[ "$answer" == "yes" ]] || { echo "Nothing created."; exit 1; }
    else
      echo "Not a terminal and LAB_DEPLOY_APPLY_CONFIRMED is not 'yes'; nothing created."; exit 1
    fi
    call -X POST "$API" --data "$BODY" | python3 -c 'import json, sys; b = json.load(sys.stdin); print("Created", b["name"], "state", b.get("spendCap", {}).get("outputState"))'
    echo "Now record it in the controller configuration: cloud_run_spend_caps: {$PROJECT: $AMOUNT}"
    ;;
  *)
    echo "usage: spend-cap.sh check|create PROJECT_ID [AMOUNT_USD]"; exit 1 ;;
esac
