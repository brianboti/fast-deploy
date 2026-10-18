#!/bin/zsh
# Applies the FOUNDATION root (infra/gcp/project: APIs, identities, cost controls, alert channels, spending cap, the
# pipeline's identities, the backup bucket) to one project. Read-only plan first; the apply waits for a typed "yes".
# Application roots (infra/gcp/apps/*) are applied by the GitHub pipeline after this root exists.
# Needs Docker running, gcloud signed in as the project owner, and the environment's settings in infra/gcp/project:
# staging.tfvars (or, until it is renamed, terraform.tfvars) for staging, production.tfvars for production (Phase 16).
# Usage: scripts/apply-foundation.sh PROJECT_ID [ENVIRONMENT]      (ENVIRONMENT: staging, the default, or production)
set -euo pipefail
PROJECT=${1:?usage: apply-foundation.sh PROJECT_ID [ENVIRONMENT]}
ENVIRONMENT=${2:-staging}
[[ "$ENVIRONMENT" == "staging" || "$ENVIRONMENT" == "production" ]] || { echo "ENVIRONMENT is staging or production."; exit 1; }
cd "$(dirname "$0")/../infra/gcp/project"
# Terraform loads terraform.tfvars by itself, so for production it must not exist: staging's values (its controller
# machine, its pipeline) would otherwise reach the production project. Each environment names its own file.
if [[ "$ENVIRONMENT" == "staging" ]]; then
  VARS=staging.tfvars
  [[ -f "$VARS" ]] || VARS=terraform.tfvars
else
  VARS=$ENVIRONMENT.tfvars
  if [[ -f terraform.tfvars ]]; then
    echo "Rename infra/gcp/project/terraform.tfvars to staging.tfvars first: Terraform would load it into $ENVIRONMENT too."
    exit 1
  fi
fi
[[ -f "$VARS" ]] || { echo "infra/gcp/project/$VARS is missing: write the $ENVIRONMENT settings first (docs/SETUP.md)."; exit 1; }
grep -q "^project_id *= *\"$PROJECT\"" "$VARS" || { echo "$VARS names another project_id than $PROJECT."; exit 1; }
# A new project has only Google's default APIs: the plan reads the project through Cloud Resource Manager, and Service
# Usage enables the rest. Both are switched on first; idempotent (found with the production project on 2026-10-18).
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project "$PROJECT" --quiet
TOKEN=$(gcloud auth print-access-token)
# The uptime check signs in as the Monitoring service agent; make sure the agent exists (idempotent).
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://serviceusage.googleapis.com/v1beta1/projects/$PROJECT/services/monitoring.googleapis.com:generateServiceIdentity" > /dev/null
# Identity-Aware Proxy calls people's pages as its own service agent; make sure it exists too (idempotent).
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://serviceusage.googleapis.com/v1beta1/projects/$PROJECT/services/iap.googleapis.com:generateServiceIdentity" > /dev/null
run() { docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN="$TOKEN" -e USER_PROJECT_OVERRIDE=true -e GOOGLE_BILLING_PROJECT="$PROJECT" -v "$PWD/..":/infra -w /infra/project hashicorp/terraform:1.13.5 "$@"; }
run init -reconfigure -input=false -backend-config="bucket=$PROJECT-tfstate" -backend-config="prefix=project/$ENVIRONMENT" > /dev/null
run plan -input=false -no-color -var-file="$VARS" -var="environment=$ENVIRONMENT" -out=project.tfplan | grep -E "^  # |^Plan:|^No changes" || true
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
ENVIRONMENT="$ENVIRONMENT" python3 - <<'PY'
import json, os
d = json.load(open("outputs.json"))
p = d["pipeline"]["value"]
# Production's pipeline identities are its own, in variables of their own (INFRA_PRODUCTION_*), used by its own jobs.
prefix = "INFRA_PRODUCTION_" if os.environ["ENVIRONMENT"] == "production" else "INFRA_"
if p:
    for name, key in (("PLAN_WIF_PROVIDER", "plan_provider"), ("APPLY_WIF_PROVIDER", "apply_provider"),
                      ("PLAN_SERVICE_ACCOUNT", "plan_service_account"), ("APPLY_SERVICE_ACCOUNT", "apply_service_account")):
        print(f"{prefix}{name}".ljust(40), "=", p[key])
print("Backup bucket:", d["backup_bucket"]["value"])
print("Cost controls:", d["cost_controls"]["value"])
PY
rm -f project.tfplan outputs.json
git checkout -- .terraform.lock.hcl 2>/dev/null || true
echo "Done. Foundation applied ($ENVIRONMENT); set the four INFRA_* (production: INFRA_PRODUCTION_*) repository variables on the platform repository if they changed (docs/SETUP.md 7f, 7n)."
