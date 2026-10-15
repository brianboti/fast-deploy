#!/bin/zsh
# The exit procedure (docs/RUNBOOK.md, "Leaving Google Cloud"): copies what one application needs to run somewhere else
# into one folder on this machine: the live image by digest (saved as an OCI archive), the service definition in Cloud
# Run's Knative-compatible export format, the root's Terraform state (read-only, a reference), every declared bucket, a
# Postgres dump or a Firestore export, and manifest.json (what was exported, digests, sizes, times).
# It deletes nothing and changes nothing in the cloud, with one exception for Postgres: Cloud SQL writes its export as its
# own identity, so the script lets that identity create objects in the application's recovery bucket for the export and
# removes the grant again afterwards. Database exports stay in the recovery bucket under exports/ (they bill as storage).
# --dry-run prints every command it would run and runs none; values read at run time are shown <like this>.
# Needs gcloud signed in as the project owner, Docker running and able to read Artifact Registry (once:
# gcloud auth configure-docker us-central1-docker.pkg.dev), and the repository's .venv (or python3 with PyYAML).
# Usage: scripts/export-app.sh APP [DEST_DIR] [--dry-run]     (APP is the application id that list_applications shows)
set -euo pipefail
USAGE="usage: export-app.sh APP [DEST_DIR] [--dry-run]"
DRY_RUN=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "$USAGE"; exit 1 ;;
    *) ARGS+=("$arg") ;;
  esac
done
(( ${#ARGS} >= 1 && ${#ARGS} <= 2 )) || { echo "$USAGE"; exit 1; }
APP=${ARGS[1]}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${LAB_CONFIG:-$REPO/config.local.yaml}"
PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
STAMP=$(date -u +%Y%m%d-%H%M%S)
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEST=${ARGS[2]:-${LAB_DEPLOY_HOME:-$HOME/.local/share/lab-deploy}/exports/$APP-$STAMP}
TERRAFORM_IMAGE="hashicorp/terraform:1.13.5"
[[ -f "$CONFIG" ]] || { echo "No controller configuration at $CONFIG; set LAB_CONFIG to its path."; exit 1; }

# The application's target: its entry in the configuration file, or the one onboarding registered in the controller's
# database (opened read-only).
TARGET=$("$PY" - "$APP" "$CONFIG" "$REPO" <<'PY'
import json, os, sqlite3, sys
from pathlib import Path
import yaml
app, path, repo = sys.argv[1], sys.argv[2], sys.argv[3]
config = yaml.safe_load(Path(path).read_text()) or {}
entry, source = (config.get("applications") or {}).get(app), "the configuration file"
if entry is None and config.get("database"):
    database = Path(os.path.expanduser(str(config["database"])))
    database = database if database.is_absolute() else Path(repo) / database
    if database.is_file():
        try:
            row = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True).execute("SELECT payload FROM applications WHERE id=?", (app,)).fetchone()
        except sqlite3.Error:
            row = None
        entry, source = (json.loads(row[0]) if row else None), "the controller's database"
if entry is None:
    sys.exit(f"Unknown application {app!r}: not in {path} and not registered in the controller's database (list_applications shows the ids)")
t = entry["target"]
print("|".join([t["project_id"], t.get("region", "us-central1"), t["service"], t["registry"], t["recovery_bucket"], source]))
PY
)
IFS='|' read -r PROJECT REGION SERVICE REGISTRY RECOVERY SOURCE <<< "$TARGET"
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ && "$REGION" =~ ^[a-z]+-[a-z]+[0-9]$ && "$SERVICE" =~ ^[a-z][a-z0-9-]{2,20}$ \
  && "$REGISTRY" =~ ^[a-z][a-z0-9-]{2,62}$ && "$RECOVERY" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo "Unexpected target values for $APP"; exit 1; }

# What the application declared, from its rendered root on this checkout (what the pipeline applied from main).
ROOT="$REPO/infra/gcp/apps/$SERVICE"
BUCKET_NEEDS=""; DATABASE="none"; STATE_BUCKET=""; STATE_PREFIX=""
if [[ -f "$ROOT/main.tf" && -f "$ROOT/backend.tf" ]]; then
  NEEDS=$("$PY" - "$ROOT/main.tf" <<'PY'
import json, re, sys
text = open(sys.argv[1]).read()
match = re.search(r'^\s*needs\s*=\s*\{\s*buckets\s*=\s*(\[[^\]]*\]),\s*topics\s*=\s*(\[[^\]]*\]),\s*database\s*=\s*"([a-z]+)"', text, re.M)
if not match:
    sys.exit("The rendered root has no needs line; render it again with the current platform (propose_infrastructure)")
print(" ".join(json.loads(match.group(1))) + "|" + match.group(3))
PY
)
  IFS='|' read -r BUCKET_NEEDS DATABASE <<< "$NEEDS"
  STATE_BUCKET=$(sed -n 's/^ *bucket *= *"\(.*\)"/\1/p' "$ROOT/backend.tf" | head -1)
  STATE_PREFIX=$(sed -n 's/^ *prefix *= *"\(.*\)"/\1/p' "$ROOT/backend.tf" | head -1)
fi

# Each helper prints the command, then runs it unless this is a dry run. A failure exits explicitly: zsh skips the EXIT trap
# (which removes the temporary grant) when `set -e` stops the script inside a function, but runs it for `exit`.
show() { print -r -- "+ ${(j: :)${(q-)@}}"; }
try() { show "$@"; (( DRY_RUN )) && return 0; "$@"; }  # returns the status, for a caller that explains the failure
run() { show "$@"; (( DRY_RUN )) && return 0; "$@" || exit $?; }
run_quiet() { show "$@"; (( DRY_RUN )) && return 0; "$@" > /dev/null || exit $?; }
run_to() {  # run_to FILE COMMAND...: the command's output goes to FILE
  local target=$1; shift
  print -r -- "+ ${(j: :)${(q-)@}} > ${(q-)target}"
  (( DRY_RUN )) && return 0
  "$@" > "$target" || exit $?
}
capture() {  # capture NAME PLACEHOLDER COMMAND...: the command's output goes to the variable NAME (the placeholder on a dry run)
  local name=$1 placeholder=$2 out
  shift 2
  show "$@"
  if (( DRY_RUN )); then out=$placeholder; else out=$("$@") || exit $?; fi
  typeset -g "$name=$out"
}
note() {  # note STEP STATUS DETAIL: one line of the manifest's step list
  (( DRY_RUN )) || print -r -- "$1|$2|$(date -u +%Y-%m-%dT%H:%M:%SZ)|$3" >> "$WORK/steps"
}

echo "Export of $APP: service $SERVICE in $PROJECT ($REGION), from $SOURCE, into $DEST"
if (( DRY_RUN )); then
  echo "Dry run: nothing below runs and nothing is written."
else
  if [[ -e "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null)" ]]; then
    echo "$DEST exists and is not empty; nothing exported. Give a new folder."
    exit 1
  fi
  umask 077  # the export holds the application's data: readable by this account only
  mkdir -p "$DEST"/{image,service,terraform,buckets,database}
  WORK=$(mktemp -d)
  GRANT=""
  cleanup() {
    if [[ -n "$GRANT" ]]; then
      echo "Removing the temporary grant for Cloud SQL's identity on gs://$RECOVERY."
      gcloud storage buckets remove-iam-policy-binding "gs://$RECOVERY" --member "$GRANT" --role roles/storage.objectCreator > /dev/null \
        || echo "Could not remove it: gcloud storage buckets remove-iam-policy-binding gs://$RECOVERY --member $GRANT --role roles/storage.objectCreator"
    fi
    rm -rf "$WORK"  # this script's own scratch folder, nothing else
  }
  trap cleanup EXIT
  : > "$WORK/steps"
  : > "$WORK/images"
fi

echo "== The service definition (Knative-compatible YAML)"
run_to "$DEST/service/service.yaml" gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format export
note service done "service/service.yaml from gcloud run services describe --format export"

echo "== The live image, by digest"
capture SERVICE_JSON "<service status>" gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format json
if (( DRY_RUN )); then
  REVISIONS=("<serving revision>")
else
  REVISIONS=("${(@f)$(print -r -- "$SERVICE_JSON" | "$PY" -c '
import json, sys
status = json.load(sys.stdin).get("status", {})
names = []
for entry in status.get("traffic", []):
    name = status.get("latestReadyRevisionName") if entry.get("latestRevision") else entry.get("revisionName")
    if entry.get("percent") and name and name not in names:
        names.append(name)
print("\n".join(names))')}")
  [[ -n "${REVISIONS[1]:-}" ]] || { echo "$SERVICE serves no revision; there is no live image to export."; exit 1; }
fi
for REVISION in $REVISIONS; do
  capture IMAGE "<image>@sha256:<digest>" gcloud run revisions describe "$REVISION" --region "$REGION" --project "$PROJECT" --format 'value(status.imageDigest)'
  (( DRY_RUN )) || [[ "$IMAGE" =~ ^[a-z0-9.-]+/[^@[:space:]]+@sha256:[a-f0-9]{64}$ ]] || { echo "Unexpected image reference for $REVISION: $IMAGE"; exit 1; }
  try docker pull "$IMAGE" || { echo "Docker could not pull $IMAGE: run gcloud auth configure-docker $REGION-docker.pkg.dev once, then export again."; exit 1; }
  run docker tag "$IMAGE" "$SERVICE:$REVISION"
  run docker save "$SERVICE:$REVISION" -o "$DEST/image/$SERVICE-$REVISION.tar"
  (( DRY_RUN )) || print -r -- "$REVISION|$IMAGE|image/$SERVICE-$REVISION.tar" >> "$WORK/images"
  note image done "$REVISION $IMAGE as image/$SERVICE-$REVISION.tar (docker load -i; tagged $SERVICE:$REVISION)"
done

echo "== The Terraform state of infra/gcp/apps/$SERVICE (read-only, a reference)"
if [[ -z "$STATE_BUCKET" || -z "$STATE_PREFIX" ]]; then
  echo "No rendered root at infra/gcp/apps/$SERVICE on this checkout: no state to pull."
  note terraform skipped "no rendered root"
else
  # The gcs backend creates an empty state (and a lock) when none exists, so a root never applied is skipped, not initialised.
  STATE_OBJECT="gs://$STATE_BUCKET/$STATE_PREFIX/default.tfstate"
  HAS_STATE=1
  show gcloud storage objects describe "$STATE_OBJECT" --format 'value(name)'
  if (( ! DRY_RUN )) && ! ANSWER=$(gcloud storage objects describe "$STATE_OBJECT" --format 'value(name)' 2>&1); then
    if grep -qiE "not found|404|No URLs matched" <<< "$ANSWER"; then
      HAS_STATE=0
      echo "No state at $STATE_OBJECT: the pipeline has not applied this root."
      note terraform skipped "no state at $STATE_OBJECT"
    else
      echo "$ANSWER"
      exit 1
    fi
  fi
  if (( HAS_STATE )); then
    capture GOOGLE_OAUTH_ACCESS_TOKEN "<access token>" gcloud auth print-access-token
    export GOOGLE_OAUTH_ACCESS_TOKEN  # passed to the container by name, never on a command line
    STATE_DIR="${WORK:-<scratch folder>}/state"
    (( DRY_RUN )) || { mkdir -p "$STATE_DIR"; printf 'terraform {\n  backend "gcs" {}\n}\n' > "$STATE_DIR/main.tf"; }
    run_quiet docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN -v "$STATE_DIR":/work -w /work "$TERRAFORM_IMAGE" init -input=false -no-color \
      -backend-config="bucket=$STATE_BUCKET" -backend-config="prefix=$STATE_PREFIX"
    run_to "$DEST/terraform/terraform.tfstate" docker run --rm -e GOOGLE_OAUTH_ACCESS_TOKEN -v "$STATE_DIR":/work -w /work "$TERRAFORM_IMAGE" state pull
    note terraform done "terraform/terraform.tfstate pulled from $STATE_OBJECT with $TERRAFORM_IMAGE"
  fi
fi

echo "== Buckets the application declared"
[[ -n "$BUCKET_NEEDS" ]] || echo "None declared."
for NAME in ${=BUCKET_NEEDS}; do
  BUCKET="$PROJECT-$SERVICE-$NAME"
  capture LISTING "<objects>" gcloud storage ls "gs://$BUCKET" --project "$PROJECT"
  if [[ -z "$LISTING" ]]; then
    echo "gs://$BUCKET is empty."
    note "bucket $NAME" empty "gs://$BUCKET holds no objects"
    continue
  fi
  run gcloud storage cp -r "gs://$BUCKET" "$DEST/buckets/"
  note "bucket $NAME" done "gs://$BUCKET copied to buckets/$BUCKET"
done

case "$DATABASE" in
  firestore)
    echo "== The Firestore database $SERVICE"
    echo "Billable: Firestore bills an export as one document read per document, plus the export's storage and the download."
    EXPORT="gs://$RECOVERY/exports/$STAMP/firestore"
    run gcloud firestore export "$EXPORT" --database "$SERVICE" --project "$PROJECT"
    run gcloud storage cp -r "$EXPORT" "$DEST/database/"
    note firestore done "managed export at $EXPORT, copied to database/firestore (restore: gcloud firestore import)"
    ;;
  postgres)
    echo "== The Cloud SQL for PostgreSQL database app on instance $SERVICE"
    echo "Billable and slow: the export runs on the instance, which must be running (a stopped one bills by the hour once started),"
    echo "takes minutes even for a small database, and the dump bills as storage in gs://$RECOVERY and as egress when copied here."
    capture SQL_STATE "RUNNABLE|ALWAYS" gcloud sql instances describe "$SERVICE" --project "$PROJECT" --format 'value(state,settings.activationPolicy)'
    if [[ "${SQL_STATE//$'\t'/|}" != "RUNNABLE|ALWAYS" ]]; then
      echo "The instance is not running ($SQL_STATE); the spending cap stops databases at the budget line. Start it deliberately with"
      echo "gcloud sql instances patch $SERVICE --activation-policy=ALWAYS --project $PROJECT (it bills by the hour), export again, then stop it."
      exit 1
    fi
    capture SQL_IDENTITY "<the instance's service account>" gcloud sql instances describe "$SERVICE" --project "$PROJECT" --format 'value(serviceAccountEmailAddress)'
    MEMBER="serviceAccount:$SQL_IDENTITY"
    capture POLICY "{}" gcloud storage buckets get-iam-policy "gs://$RECOVERY" --format json
    HELD=$(print -r -- "$POLICY" | "$PY" -c '
import json, sys
policy = json.loads(sys.stdin.read() or "{}")
print(any(sys.argv[1] in b.get("members", []) and b.get("role") in ("roles/storage.objectCreator", "roles/storage.objectAdmin", "roles/storage.admin")
          for b in policy.get("bindings", [])))' "$MEMBER")
    if [[ "$HELD" != True ]]; then
      echo "Granting Cloud SQL's identity object creation on gs://$RECOVERY for the export; removed again at the end."
      run_quiet gcloud storage buckets add-iam-policy-binding "gs://$RECOVERY" --member "$MEMBER" --role roles/storage.objectCreator
      (( DRY_RUN )) || GRANT="$MEMBER"
    fi
    DUMP="gs://$RECOVERY/exports/$STAMP/postgres-app.sql.gz"
    run gcloud sql export sql "$SERVICE" "$DUMP" --database app --project "$PROJECT"
    if [[ "$HELD" != True ]]; then
      run_quiet gcloud storage buckets remove-iam-policy-binding "gs://$RECOVERY" --member "$MEMBER" --role roles/storage.objectCreator
      (( DRY_RUN )) || GRANT=""
    fi
    run gcloud storage cp "$DUMP" "$DEST/database/postgres-app.sql.gz"
    note postgres done "plain SQL dump of database app at $DUMP, copied to database/postgres-app.sql.gz (restore: gunzip -c | psql)"
    ;;
  *)
    echo "== No database declared." ;;
esac

if (( DRY_RUN )); then
  echo "+ (write $DEST/manifest.json: every file with its size and SHA-256, the image digests, the steps and their times)"
  echo "Dry run complete: nothing was run or written."
  exit 0
fi
APP="$APP" SERVICE="$SERVICE" PROJECT="$PROJECT" REGION="$REGION" SOURCE="$SOURCE" STARTED="$STARTED" REPO="$REPO" \
  "$PY" - "$DEST" "$WORK/steps" "$WORK/images" <<'PY'
import datetime, hashlib, json, os, subprocess, sys
from pathlib import Path
dest, steps, images = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
files = []
for path in sorted(p for p in dest.rglob("*") if p.is_file()):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    files.append({"path": str(path.relative_to(dest)), "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
try:
    commit = subprocess.run(["git", "-C", os.environ["REPO"], "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
except (OSError, subprocess.CalledProcessError):
    commit = None
manifest = {
    "application": os.environ["APP"], "service": os.environ["SERVICE"], "project_id": os.environ["PROJECT"], "region": os.environ["REGION"],
    "target_from": os.environ["SOURCE"], "started_at": os.environ["STARTED"],
    "finished_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "exported_by": "scripts/export-app.sh", "platform_commit": commit,
    "images": [{"revision": revision, "reference": reference, "digest": reference.rsplit("@", 1)[-1], "file": file}
               for revision, reference, file in (line.split("|", 2) for line in images.read_text().splitlines() if line)],
    "steps": [dict(zip(("step", "status", "at", "detail"), line.split("|", 3))) for line in steps.read_text().splitlines() if line],
    "files": files,
    "not_exported": ["secret values (Secret Manager; the operator keeps them, decision D5)",
                     "Pub/Sub topics and subscriptions (messages in flight only; recreate them from the names in the Terraform state)",
                     "logs, metrics and alerting history (Cloud Logging and Monitoring)"],
}
(dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Wrote {dest / 'manifest.json'}: {len(files)} files, {sum(f['bytes'] for f in files) / 1_000_000:.1f} MB")
PY
[[ "$DATABASE" == none ]] || echo "The database export also stays in gs://$RECOVERY/exports/$STAMP/ (it bills as storage until you remove it)."
echo "Done. $DEST holds the export; docs/RUNBOOK.md, \"Leaving Google Cloud\", says how each part runs or restores elsewhere."
