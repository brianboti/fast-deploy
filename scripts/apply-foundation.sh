#!/bin/zsh
# Applies the FOUNDATION root (infra/gcp/project: APIs, identities, cost controls, alert channels, spending cap, the
# pipeline's identities, the backup bucket) to one project. Read-only plan first; the apply waits for a typed "yes".
# Application roots (infra/gcp/apps/*) are applied by the GitHub pipeline after this root exists.
# Needs Docker running, gcloud signed in as the project owner, and the environment's settings in infra/gcp/project:
# staging.tfvars (or, until it is renamed, terraform.tfvars) for staging, production.tfvars for production (Phase 16).
# A project of an application's own (docs/ROADMAP.md 14.9) takes `own` and its settings in own-PROJECT_ID.tfvars: a
# staging project for that one application, with its own pipeline identities, budget and spending cap.
# Usage: scripts/apply-foundation.sh PROJECT_ID [ENVIRONMENT]      (ENVIRONMENT: staging, the default, production or own)
set -euo pipefail
PROJECT=${1:?usage: apply-foundation.sh PROJECT_ID [ENVIRONMENT]}
ENVIRONMENT=${2:-staging}
[[ "$ENVIRONMENT" == "staging" || "$ENVIRONMENT" == "production" || "$ENVIRONMENT" == "own" ]] || { echo "ENVIRONMENT is staging, production or own."; exit 1; }
MODE=$ENVIRONMENT
# A project of its own holds an application's staging: Terraform names it so, in that project's own state bucket.
[[ "$MODE" == "own" ]] && ENVIRONMENT=staging
cd "$(dirname "$0")/../infra/gcp/project"
# Terraform loads terraform.tfvars by itself, so for production it must not exist: staging's values (its controller
# machine, its pipeline) would otherwise reach the production project. Each environment names its own file.
if [[ "$MODE" == "staging" ]]; then
  VARS=staging.tfvars
  [[ -f "$VARS" ]] || VARS=terraform.tfvars
elif [[ "$MODE" == "own" ]]; then
  VARS=own-$PROJECT.tfvars
  if [[ -f terraform.tfvars ]]; then
    echo "Rename infra/gcp/project/terraform.tfvars to staging.tfvars first: Terraform would load it into $PROJECT too."
    exit 1
  fi
else
  VARS=$ENVIRONMENT.tfvars
  if [[ -f terraform.tfvars ]]; then
    echo "Rename infra/gcp/project/terraform.tfvars to staging.tfvars first: Terraform would load it into $ENVIRONMENT too."
    exit 1
  fi
fi
[[ -f "$VARS" ]] || { echo "infra/gcp/project/$VARS is missing: write the $MODE settings first (docs/SETUP.md)."; exit 1; }
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
# Binary Authorization reads the release attestor's note as its own service agent (binauthz.tf); the API is enabled
# first, since an agent is made only for an enabled service (idempotent).
gcloud services enable binaryauthorization.googleapis.com --project "$PROJECT" --quiet
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://serviceusage.googleapis.com/v1beta1/projects/$PROJECT/services/binaryauthorization.googleapis.com:generateServiceIdentity" > /dev/null
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
# A new project's first apply switches on APIs and uses them seconds later, before Google has them ready ("has not been
# used in project ... or it is disabled"); found with the production project on 2026-10-18, where 6 of 104 resources
# stopped. Only that error is waited out, once: a minute later the rest of the same configuration is planned and applied.
LOG=$(mktemp)
if ! run apply -input=false -no-color project.tfplan 2>&1 | tee "$LOG"; then
  if ! grep -q "has not been used in project" "$LOG"; then rm -f "$LOG" project.tfplan; exit 1; fi
  echo "Google is still switching on the APIs this apply enabled (a new project's first apply); the rest follows in a minute."
  sleep 60
  run plan -input=false -no-color -var-file="$VARS" -var="environment=$ENVIRONMENT" -out=project.tfplan | grep -E "^  # |^Plan:|^No changes" || true
  run apply -input=false -no-color project.tfplan
fi
rm -f "$LOG"
run output -json > outputs.json
MODE="$MODE" python3 - <<'PY'
import json, os
d = json.load(open("outputs.json"))
p = d["pipeline"]["value"]
# Production's pipeline identities are its own, in variables of their own (INFRA_PRODUCTION_*), used by its own jobs.
prefix = "INFRA_PRODUCTION_" if os.environ["MODE"] == "production" else "INFRA_"
if p and os.environ["MODE"] == "own":
    # A project of its own: one entry, under the application's service name, in the repository variable INFRA_OWN_PROJECTS.
    entry = {key: p[key] for key in ("plan_provider", "apply_provider", "plan_service_account", "apply_service_account")}
    print("INFRA_OWN_PROJECTS entry (add it under the service's name):", json.dumps(entry))
elif p:
    for name, key in (("PLAN_WIF_PROVIDER", "plan_provider"), ("APPLY_WIF_PROVIDER", "apply_provider"),
                      ("PLAN_SERVICE_ACCOUNT", "plan_service_account"), ("APPLY_SERVICE_ACCOUNT", "apply_service_account")):
        print(f"{prefix}{name}".ljust(40), "=", p[key])
print("Backup bucket:", d["backup_bucket"]["value"])
print("Cost controls:", d["cost_controls"]["value"])
PY
rm -f project.tfplan outputs.json
git checkout -- .terraform.lock.hcl 2>/dev/null || true
if [[ "$MODE" == "own" ]]; then
  echo "Done. Foundation applied (a project of its own); add the entry above to INFRA_OWN_PROJECTS and the project under onboarding.own_projects (docs/SETUP.md 7s)."
else
  echo "Done. Foundation applied ($ENVIRONMENT); set the four INFRA_* (production: INFRA_PRODUCTION_*) repository variables on the platform repository if they changed (docs/SETUP.md 7f, 7n)."
fi
