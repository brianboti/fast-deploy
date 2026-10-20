#!/bin/zsh
# Retires one application cleanly (docs/RUNBOOK.md, "Retire an application"): the order `lab-deploy decommission` prints,
# run for real. Without --yes it only shows what it would delete. The pipeline never destroys anything; this is the
# operator's deliberate deletion, run on the laptop as the project owner.
#   1. the Cloud Run service (and its migration job, when the contract declares one)
#   2. the recovery bucket, emptied first, and the image registry (both guarded against Terraform's destroy on purpose)
#   3. `terraform destroy` of the application's root, after those three are taken out of its state; Terraform drops the
#      identities, trust, alerts and dashboard it still owns, and schedules the provenance key's versions for destruction
#   4. the GitHub environment of a contract the platform keeps (staging-<service>, on the platform repository)
# It refuses an application whose root declares data (a database, buckets, topics): retire the data deliberately first
# (docs/RUNBOOK.md, "Retire a database deliberately"), then run this. Afterwards remove apps/<service>/ and
# infra/gcp/apps/<service>/ in a pull request and run `scripts/machine.sh unregister <service>`; it prints both.
# Needs Docker running, gcloud signed in as the project owner, and gh signed in (for the environment).
# Usage: scripts/decommission.sh SERVICE [--yes]
set -euo pipefail
YES=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    -*) echo "usage: decommission.sh SERVICE [--yes]"; exit 1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
(( ${#ARGS} == 1 )) || { echo "usage: decommission.sh SERVICE [--yes]"; exit 1; }
SERVICE=${ARGS[1]}
[[ "$SERVICE" =~ ^[a-z][a-z0-9-]{0,40}$ ]] || { echo "SERVICE is the Cloud Run service's name, as infra/gcp/apps/ spells it."; exit 1; }
cd "$(dirname "$0")/.."
ROOT="infra/gcp/apps/$SERVICE"
[[ -f "$ROOT/main.tf" ]] || { echo "No $ROOT/main.tf: nothing to retire here (production roots are under infra/gcp/apps-production)."; exit 1; }
value() { awk -F'"' -v key="$1" '$0 ~ "^[[:space:]]*" key "[[:space:]]*=" {print $2; exit}' "$ROOT/main.tf"; }
PROJECT=$(value project_id)
REGION=$(value region)
[[ -n "$PROJECT" && -n "$REGION" ]] || { echo "$ROOT/main.tf does not name project_id and region."; exit 1; }
NEEDS=$(grep -E "^[[:space:]]*needs[[:space:]]*=" "$ROOT/main.tf" | head -1 || true)
if echo "$NEEDS" | grep -Eq 'database = "(postgres|firestore)"|buckets = \[[^]]|topics = \[[^]]'; then
  echo "$SERVICE declares data in $ROOT/main.tf: $NEEDS"
  echo "Retire the data deliberately first (docs/RUNBOOK.md, \"Retire a database deliberately\"; a bucket or topic the same way: export, delete,"
  echo "terraform state rm), merge the root without it, then run this again. Nothing was deleted."
  exit 1
fi
MIGRATE=0
echo "$NEEDS" | grep -Eq 'migrate = \[[^]]' && MIGRATE=1
PLATFORM_REPO=$(value github_repository)
ENVIRONMENT=""
[[ -f "apps/$SERVICE/platform.yaml" ]] && ENVIRONMENT="staging-$SERVICE"
BUCKET="$PROJECT-$SERVICE-recovery"
cat <<PLAN
Retiring $SERVICE in $PROJECT ($REGION); the root is $ROOT.
  1. Cloud Run service $SERVICE$([[ $MIGRATE == 1 ]] && echo " and its job $SERVICE-migrate")
  2. recovery bucket gs://$BUCKET (emptied first) and the image registry $SERVICE
  3. terraform destroy of $ROOT (state apps/$SERVICE/staging in $PROJECT-tfstate), after the three above are removed from its state
$([[ -n "$ENVIRONMENT" ]] && echo "  4. GitHub environment $ENVIRONMENT on $PLATFORM_REPO (the contract is kept by the platform)")
PLAN
if [[ $YES != 1 ]]; then
  echo "Dry run: nothing was deleted. Run again with --yes to do it."
  exit 0
fi
echo "--- 1. the service"
gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet 2>&1 | tail -1 || true
if [[ $MIGRATE == 1 ]]; then
  gcloud run jobs delete "$SERVICE-migrate" --region "$REGION" --project "$PROJECT" --quiet 2>&1 | tail -1 || true
fi
echo "--- 2. the recovery bucket and the registry"
gcloud storage rm -r "gs://$BUCKET/**" --project "$PROJECT" --quiet 2>/dev/null || true
gcloud storage buckets delete "gs://$BUCKET" --project "$PROJECT" --quiet 2>&1 | tail -1 || true
gcloud artifacts repositories delete "$SERVICE" --location "$REGION" --project "$PROJECT" --quiet 2>&1 | tail -1 || true
echo "--- 3. terraform destroy"
TOKEN=$(gcloud auth print-access-token)
tf() { docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN="$TOKEN" -e USER_PROJECT_OVERRIDE=true -e GOOGLE_BILLING_PROJECT="$PROJECT" -v "$PWD/infra/gcp":/infra -w "/infra/apps/$SERVICE" hashicorp/terraform:1.13.5 "$@"; }
tf init -input=false -no-color > /dev/null
# What was deleted by hand above leaves the state here: the service (deletion_protection), the recovery bucket
# (prevent_destroy), the registry, and the grants on the two; Terraform would refuse to destroy the first two.
tf state list -no-color 2>/dev/null | grep -E 'google_cloud_run_v2_service\.|google_cloud_run_v2_job\.|google_storage_bucket\.recovery|google_storage_bucket_iam_member\.recovery|google_artifact_registry_repository(_iam_member)?\.' | while read -r address; do
  tf state rm -no-color "$address" > /dev/null && echo "forgot $address"
done
tf destroy -input=false -no-color -auto-approve 2>&1 | grep -E "^module\.|^Destroy complete|^Error|^│ Error|Plan:|destroyed" | tail -60
git checkout -- "$ROOT/.terraform.lock.hcl" 2>/dev/null || true
rm -rf "$ROOT/.terraform"
if [[ -n "$ENVIRONMENT" ]]; then
  echo "--- 4. the GitHub environment"
  gh api -X DELETE "repos/$PLATFORM_REPO/environments/$ENVIRONMENT" > /dev/null 2>&1 && echo "deleted $ENVIRONMENT on $PLATFORM_REPO" || echo "the environment $ENVIRONMENT was not deleted (gone already, or gh is not signed in): check Settings > Environments"
fi
cat <<REST
Done in the cloud. What remains is in the repository and the controller:
  - remove $ROOT/$([[ -n "$ENVIRONMENT" ]] && echo " and apps/$SERVICE/") in a pull request (the drift check and the pipeline then forget the root)
  - scripts/machine.sh unregister $SERVICE   (the controller forgets the application; its plans stay as history)
Cloud KMS keeps the key $SERVICE-provenance by name with its versions scheduled for destruction, and the Workload Identity
pool $SERVICE-gh stays soft-deleted for thirty days: an application of the same name within that time imports them.
REST
