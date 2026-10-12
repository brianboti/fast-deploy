#!/bin/zsh
# One-time migration of a single-root state (prefix SERVICE/ENVIRONMENT, versions up to 0.9.x) into the per-root layout
# of 1.0.0: project/ENVIRONMENT (foundation) and apps/SERVICE/ENVIRONMENT (the application). Nothing in the cloud
# changes; only state objects are written, and the old state stays in place as a backup. Safe to re-run: it stops if a
# new state already exists. Needs Docker running and gcloud signed in as the project owner.
# Usage: scripts/migrate-state.sh PROJECT_ID SERVICE [ENVIRONMENT]
set -euo pipefail
PROJECT=${1:?usage: migrate-state.sh PROJECT_ID SERVICE [ENVIRONMENT]}
SERVICE=${2:?usage: migrate-state.sh PROJECT_ID SERVICE [ENVIRONMENT]}
ENVIRONMENT=${3:-staging}
BUCKET="$PROJECT-tfstate"
WORK="${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}/migrate-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$WORK/old" "$WORK/empty"
TOKEN=$(gcloud auth print-access-token)
tf() { docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN="$TOKEN" -v "$WORK":/work -w "/work/$1" hashicorp/terraform:1.13.5 "${@:2}"; }
for prefix in "project/$ENVIRONMENT" "apps/$SERVICE/$ENVIRONMENT"; do
  if gcloud storage objects describe "gs://$BUCKET/$prefix/default.tfstate" >/dev/null 2>&1; then
    echo "gs://$BUCKET/$prefix/default.tfstate already exists; nothing done. Remove it first if you really want to redo the migration."
    exit 1
  fi
done
printf 'terraform {\n  backend "gcs" {}\n}\n' > "$WORK/old/main.tf"
tf old init -input=false -no-color -backend-config="bucket=$BUCKET" -backend-config="prefix=$SERVICE/$ENVIRONMENT" > /dev/null
tf old state pull > "$WORK/old.tfstate"
cp "$WORK/old.tfstate" "$WORK/old.tfstate.backup"
cp "$WORK/old.tfstate" "$WORK/project.tfstate"
cp "$WORK/old.tfstate" "$WORK/app.tfstate"
echo "Pulled the current state ($(python3 -c "import json;print(len(json.load(open('$WORK/old.tfstate'))['resources']))") resources); backup at $WORK/old.tfstate.backup"
tf empty state rm -state=/work/project.tfstate "module.service[\"$SERVICE\"]" > /dev/null
tf empty state rm -state=/work/app.tfstate 'module.project' > /dev/null
tf empty state mv -state=/work/app.tfstate "module.service[\"$SERVICE\"]" 'module.service' > /dev/null  # no -state-out: with the same path Terraform writes the source last and loses the move
echo "Split: $(tf empty state list -state=/work/project.tfstate | wc -l | tr -d ' ') resources for the foundation, $(tf empty state list -state=/work/app.tfstate | wc -l | tr -d ' ') for $SERVICE"
mkdir -p "$WORK/project" "$WORK/app"
printf 'terraform {\n  backend "gcs" {}\n}\n' > "$WORK/project/main.tf"
printf 'terraform {\n  backend "gcs" {}\n}\n' > "$WORK/app/main.tf"
tf project init -input=false -no-color -backend-config="bucket=$BUCKET" -backend-config="prefix=project/$ENVIRONMENT" > /dev/null
tf project state push /work/project.tfstate
tf app init -input=false -no-color -backend-config="bucket=$BUCKET" -backend-config="prefix=apps/$SERVICE/$ENVIRONMENT" > /dev/null
tf app state push /work/app.tfstate
echo "Done. New states: gs://$BUCKET/project/$ENVIRONMENT and gs://$BUCKET/apps/$SERVICE/$ENVIRONMENT. The old state at $SERVICE/$ENVIRONMENT is untouched (backup)."
echo "Next: scripts/apply-foundation.sh $PROJECT $ENVIRONMENT, then merge the first infrastructure pull request so the pipeline applies apps/$SERVICE."
