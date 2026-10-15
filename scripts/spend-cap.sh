#!/bin/zsh
# Google's native budget spend cap for Cloud Run (docs/SETUP.md 7i): a monthly budget scoped to one project and the Cloud
# Run service that pauses every Cloud Run workload in the project once the month's gross estimated cost passes it. It needs
# nothing of this platform to work, and it does not stop Cloud SQL, buckets or topics (the platform's own cap does that).
# Needs gcloud signed in as someone who may manage budgets on the project's billing account (Billing Account Administrator,
# or Billing Account Costs Manager with Editor on the project), and the Budget API enabled in the project (the foundation does).
# Usage: scripts/spend-cap.sh check  PROJECT_ID
#        scripts/spend-cap.sh create PROJECT_ID AMOUNT_USD     (shows the request, then waits for a typed "yes")
#        scripts/spend-cap.sh amount PROJECT_ID AMOUNT_USD     (changes the amount and re-arms a lifted cap; typed "yes")
#        scripts/spend-cap.sh lift   PROJECT_ID                (lifts an enforced cap until the next month; typed "yes")
set -euo pipefail
ACTION=${1:?usage: spend-cap.sh check|create|amount|lift PROJECT_ID [AMOUNT_USD]}
PROJECT=${2:?usage: spend-cap.sh check|create|amount|lift PROJECT_ID [AMOUNT_USD]}
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
        value = int(amount.get("units", 0) or 0) + int(amount.get("nanos", 0) or 0) / 1e9
        print(budget["name"], f"{value:g}", amount.get("currencyCode", ""), cap.get("outputState", cap.get("inputState", "?")))
if data.get("nextPageToken"):
    print("(more than 100 budgets on this billing account: only the first page was read)", file=sys.stderr)
'
}

confirm() {  # $1: what is about to happen
  echo "$1"
  if [[ "${LAB_DEPLOY_APPLY_CONFIRMED:-}" == "yes" ]]; then
    echo "Confirmed through LAB_DEPLOY_APPLY_CONFIRMED."
  elif [[ -t 0 ]]; then
    read -r "answer?Type yes to continue: "
    [[ "$answer" == "yes" ]] || { echo "Nothing changed."; exit 1; }
  else
    echo "Not a terminal and LAB_DEPLOY_APPLY_CONFIRMED is not 'yes'; nothing changed."; exit 1
  fi
}

# The one cap on this project's Cloud Run, or a refusal when there is none or more than one.
the_cap() {
  found=$(caps)
  [[ -n "$found" ]] || { echo "No native Cloud Run spend cap on $PROJECT; create one first."; exit 1; }
  [[ $(echo "$found" | wc -l) -eq 1 ]] || { echo "More than one cap covers Cloud Run on $PROJECT:"; echo "$found"; exit 1; }
  echo "${found%% *}"
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
  amount)
    AMOUNT=${3:?usage: spend-cap.sh amount PROJECT_ID AMOUNT_USD}
    [[ "$AMOUNT" =~ ^[1-9][0-9]{0,5}$ ]] || { echo "AMOUNT_USD must be a whole number of dollars"; exit 1; }
    NAME=$(the_cap)
    # A spend cap budget accepts this update only with the documented mask and the whole budget in the body (a mask of
    # "amount" alone is refused as an invalid argument, seen 2026-10-15); CONFIGURED re-arms a cap lifted this month.
    BODY=$(PROJECT="$PROJECT" AMOUNT="$AMOUNT" CLOUD_RUN="$CLOUD_RUN" python3 -c '
import json, os
print(json.dumps({
    "displayName": "Lab Deploy: Cloud Run spend cap",
    "budgetFilter": {"projects": ["projects/" + os.environ["PROJECT"]], "services": [os.environ["CLOUD_RUN"]],
                     "calendarPeriod": "MONTH", "creditTypesTreatment": "EXCLUDE_ALL_CREDITS"},
    "amount": {"specifiedAmount": {"currencyCode": "USD", "units": os.environ["AMOUNT"], "nanos": 0}},
    "thresholdRules": [{"thresholdPercent": 0.5}, {"thresholdPercent": 0.8}, {"thresholdPercent": 1.0}],
    "notificationsRule": {"enableProjectLevelRecipients": True},
    "spendCap": {"inputState": "CONFIGURED"},
}))')
    confirm "About to set $NAME to $AMOUNT USD a month and arm it (an enforced cap must be lifted first)."
    call -X PATCH "https://billingbudgets.googleapis.com/v1/$NAME?updateMask=amount.specifiedAmount,spendCap.inputState" --data "$BODY" \
      | python3 -c 'import json, sys; b = json.load(sys.stdin); print("Now", b["amount"]["specifiedAmount"].get("units"), "USD, state", b.get("spendCap", {}).get("outputState"))'
    echo "Record the new amount in the controller configuration: cloud_run_spend_caps: {$PROJECT: $AMOUNT}"
    ;;
  lift)
    NAME=$(the_cap)
    confirm "About to lift $NAME: Cloud Run in $PROJECT resumes (up to an hour) and the cap stays lifted until the month ends or 'amount' re-arms it."
    call -X PATCH "https://billingbudgets.googleapis.com/v1/$NAME?updateMask=spendCap.inputState" --data '{"spendCap": {"inputState": "AWAITING_NEXT_PERIOD"}}' \
      | python3 -c 'import json, sys; b = json.load(sys.stdin); print("State", b.get("spendCap", {}).get("outputState"))'
    ;;
  *)
    echo "usage: spend-cap.sh check|create|amount|lift PROJECT_ID [AMOUNT_USD]"; exit 1 ;;
esac
