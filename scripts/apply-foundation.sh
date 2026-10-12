#!/bin/zsh
# Applies the FOUNDATION root (infra/gcp/project: APIs, identities, cost controls, alert channels, spending cap, the
# pipeline's identities, the backup bucket) to one project. Read-only plan first; the apply waits for a typed "yes".
# Application roots (infra/gcp/apps/*) are applied by the GitHub pipeline after this root exists.
# Needs Docker running, gcloud signed in as the project owner, and infra/gcp/project/terraform.tfvars filled in.
# Usage: scripts/apply-foundation.sh PROJECT_ID [ENVIRONMENT]      (ENVIRONMENT defaults to staging)
set -euo pipefail
PROJECT=${1:?usage: apply-foundation.sh PROJECT_ID [ENVIRONMENT]}
ENVIRONMENT=${2:-staging}
cd "$(dirname "$0")/../infra/gcp/project"
TOKEN=$(gcloud auth print-access-token)
# The uptime check signs in as the Monitoring service agent; make sure the agent exists (idempotent).
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://serviceusage.googleapis.com/v1beta1/projects/$PROJECT/services/monitoring.googleapis.com:generateServiceIdentity" > /dev/null
run() { docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN="$TOKEN" -e USER_PROJECT_OVERRIDE=true -e GOOGLE_BILLING_PROJECT="$PROJECT" -v "$PWD/..":/infra -w /infra/project hashicorp/terraform:1.13.5 "$@"; }
run init -reconfigure -input=false -backend-config="bucket=$PROJECT-tfstate" -backend-config="prefix=project/$ENVIRONMENT" > /dev/null
run plan -input=false -no-color -out=project.tfplan | grep -E "^  # |^Plan:|^No changes" || true
# The apply needs a typed "yes" at the prompt, or LAB_DEPLOY_APPLY_CONFIRMED=yes in the environment for an unattended run.
if [[ "${LAB_DEPLOY_APPLY_CONFIRMED:-}" == "yes" ]]; then
  echo "Applying: confirmed through LAB_DEPLOY_APPLY_CONFIRMED."
elif [[ -t 0 ]]; then
  read -r "answer?Apply this plan to $PROJECT? Type yes to continue: "
  [[ "$answer" == "yes" ]] || { echo "Nothing applied."; rm -f project.tfplan; exit 1; }
else
  echo "Not a terminal and LAB_DEPLOY_APPLY_CONFIRMED is not 'yes'; nothing applied."; rm -f project.tfplan; exit 1
fi
run apply -input=false -no-color project.tfplan
run output -json > outputs.json
python3 - <<'PY'
import json
d = json.load(open("outputs.json"))
p = d["pipeline"]["value"]
if p:
    print("INFRA_PLAN_WIF_PROVIDER      =", p["plan_provider"])
    print("INFRA_APPLY_WIF_PROVIDER     =", p["apply_provider"])
    print("INFRA_PLAN_SERVICE_ACCOUNT   =", p["plan_service_account"])
    print("INFRA_APPLY_SERVICE_ACCOUNT  =", p["apply_service_account"])
print("Backup bucket:", d["backup_bucket"]["value"])
print("Cost controls:", d["cost_controls"]["value"])
PY
rm -f project.tfplan outputs.json
git checkout -- .terraform.lock.hcl 2>/dev/null || true
echo "Done. Foundation applied; set the four INFRA_* repository variables on the platform repository if they changed (docs/SETUP.md 7f)."
