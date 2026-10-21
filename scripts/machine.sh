#!/bin/zsh
# The controller's machine, day to day (docs/RUNBOOK.md, "The controller's machine"), from the laptop through
# Identity-Aware Proxy: there is no open SSH port, and gcloud signs you in with OS Login.
#   scripts/machine.sh status            the services, the commit that runs, disk, memory, /healthz and the last
#                                        automatic update
#   scripts/machine.sh logs [UNIT]       the last 200 lines of fast-deploy (default), litestream, caddy, startup or update
#   scripts/machine.sh restart           restart the controller
#   scripts/machine.sh drill             the quarterly restore drill, on the machine: the nightly copy and the live copy
#   scripts/machine.sh record-drill NAME ok|failed [DETAIL]
#                                        record another drill (alerts, rollback, cap_fire) so the watchdog stops reminding
#   scripts/machine.sh backup            the nightly backup, now
#   scripts/machine.sh update COMMIT     install a commit from main; the previous one comes back if it does not answer
#                                        (with platform_updates on, the machine does this by itself after a release)
#   scripts/machine.sh startup           run the machine's startup script again, after a foundation apply changed it
#   scripts/machine.sh config-get FILE   copy the machine's configuration into FILE (mode 600) to edit
#   scripts/machine.sh config-put FILE   check FILE loads, then make it the machine's; principals, applications and
#                                        settings apply at once, oauth and github_app changes need `restart`
#   scripts/machine.sh stop              hold the controller: stopped, and not started again by a reboot (before a laptop
#                                        takes over)
#   scripts/machine.sh start             release the hold and start it
#   scripts/machine.sh unregister APP    the controller forgets an onboarded application (after scripts/decommission.sh);
#                                        its plans and audit events stay as history
#   scripts/machine.sh ssh               a shell on the machine
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=$(.venv/bin/python -c 'import yaml; print(yaml.safe_load(open("config.local.yaml"))["onboarding"]["project_id"])')
ZONE=${FAST_DEPLOY_ZONE:-us-central1-a}
NAME=fast-deploy
iap() { gcloud compute ssh "$NAME" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap --command "$1"; }
# A controller command run on the machine as the controller's own user, with its configuration and its identity.
as_controller() {
  iap "cd /opt/fast-deploy/current && sudo -u fastdeploy env LAB_CONFIG=/var/lib/fast-deploy/config.yaml HOME=/var/lib/fast-deploy CLOUDSDK_CONFIG=/var/lib/fast-deploy/.gcloud PATH=/opt/fast-deploy/venv/bin:/usr/local/bin:/usr/bin:/bin /opt/fast-deploy/venv/bin/python -m labdeploy.cli $1"
}
COMMAND=${1:-status}
case "$COMMAND" in
  status)
    iap 'systemctl is-active fast-deploy litestream caddy | paste -sd" " - | sed "s/^/services (fast-deploy litestream caddy): /";
         echo "commit: $(basename "$(readlink /opt/fast-deploy/current)")";
         df -h / | awk "NR==2 {print \"disk: \" \$3 \" used of \" \$2}";
         free -m | awk "NR==2 {print \"memory: \" \$3 \" MiB used of \" \$2}";
         echo "automatic updates (fast-deploy-update.path): $(systemctl is-active fast-deploy-update.path)";
         [ -f /var/lib/fast-deploy-update/result.json ] && echo "last automatic update: $(cat /var/lib/fast-deploy-update/result.json)";
         curl -fsS --max-time 5 http://127.0.0.1:8000/healthz && echo' ;;
  logs)
    UNIT=${2:-fast-deploy}
    [[ "$UNIT" == "startup" ]] && UNIT=google-startup-scripts
    [[ "$UNIT" == "update" ]] && UNIT=fast-deploy-update
    [[ "$UNIT" =~ ^(fast-deploy|litestream|caddy|google-startup-scripts|fast-deploy-update)$ ]] || { echo "UNIT is fast-deploy, litestream, caddy, startup or update"; exit 1; }
    iap "sudo journalctl -u $UNIT -n 200 --no-pager" ;;
  restart)
    iap 'sudo systemctl restart fast-deploy && sleep 3 && systemctl is-active fast-deploy' ;;
  drill)
    as_controller restore-drill ;;
  record-drill)
    DRILL=${2:?usage: scripts/machine.sh record-drill alerts|rollback|cap_fire ok|failed [DETAIL]}
    OUTCOME=${3:?usage: scripts/machine.sh record-drill alerts|rollback|cap_fire ok|failed [DETAIL]}
    [[ "$DRILL" =~ ^(alerts|rollback|cap_fire)$ && "$OUTCOME" =~ ^(ok|failed)$ ]] || { echo "NAME is alerts, rollback or cap_fire; the outcome ok or failed"; exit 1; }
    DETAIL=${4:-}
    ALLOWED='^[A-Za-z0-9 .,:;_/%()-]{0,200}$'
    [[ "$DETAIL" =~ $ALLOWED ]] || { echo "Keep DETAIL to letters, digits, spaces and . , : ; _ / % ( ) -"; exit 1; }
    if [[ -n "$DETAIL" ]]; then as_controller "record-drill $DRILL $OUTCOME --detail '$DETAIL'"; else as_controller "record-drill $DRILL $OUTCOME"; fi ;;
  backup)
    as_controller backup ;;
  update)
    COMMIT=${2:?usage: scripts/machine.sh update COMMIT (a full commit on main)}
    [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Give the full 40-character commit, for example: git rev-parse origin/main"; exit 1; }
    iap "sudo /usr/local/sbin/fast-deploy-install $COMMIT" ;;
  startup)
    iap 'sudo google_metadata_script_runner startup' ;;
  config-get)
    FILE=${2:?usage: scripts/machine.sh config-get FILE}
    (umask 077 && iap 'sudo cat /var/lib/fast-deploy/config.yaml' > "$FILE")
    echo "The machine's configuration is in $FILE; edit it, then scripts/machine.sh config-put $FILE" ;;
  config-put)
    FILE=${2:?usage: scripts/machine.sh config-put FILE}
    .venv/bin/python -c 'import sys; from labdeploy.config import load_config; load_config(sys.argv[1])' "$FILE"
    iap 'rm -f /tmp/fast-deploy-config.yaml'
    gcloud compute scp --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap "$FILE" "$NAME:/tmp/fast-deploy-config.yaml"
    iap 'set -e; source /etc/fast-deploy/machine.env;
         sudo install -o fastdeploy -g fastdeploy -m 0600 /tmp/fast-deploy-config.yaml /var/lib/fast-deploy/config.yaml;
         rm -f /tmp/fast-deploy-config.yaml;
         sudo gcloud storage cp /var/lib/fast-deploy/config.yaml "gs://$BUCKET/machine/config.yaml" --quiet;
         echo "The machine has the new configuration (and its copy in the backup bucket)."'
    echo "Principals, applications and settings apply at the next request; oauth or github_app changes need: scripts/machine.sh restart" ;;
  stop)
    iap 'sudo touch /var/lib/fast-deploy/held && sudo systemctl stop fast-deploy && echo "The controller is held: stopped, and no reboot starts it."' ;;
  start)
    iap 'sudo rm -f /var/lib/fast-deploy/held && sudo systemctl start fast-deploy && sleep 3 && systemctl is-active fast-deploy' ;;
  unregister)
    APP=${2:?usage: scripts/machine.sh unregister APP}
    [[ "$APP" =~ ^[a-z][a-z0-9-]{0,40}$ ]] || { echo "APP is the application's name as list_applications shows it"; exit 1; }
    as_controller "unregister $APP" ;;
  ssh)
    gcloud compute ssh "$NAME" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap ;;
  *)
    awk 'NR > 1 && /^#/ {print; next} NR > 1 {exit}' "$0"
    exit 1 ;;
esac
