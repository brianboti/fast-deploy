#!/bin/zsh
# The controller's machine, day to day (docs/RUNBOOK.md, "The controller's machine"), from the laptop through
# Identity-Aware Proxy: there is no open SSH port, and gcloud signs you in with OS Login.
#   scripts/machine.sh status            the three services, the commit that runs, disk, memory and /healthz
#   scripts/machine.sh logs [UNIT]       the last 200 lines of fast-deploy (default), litestream, caddy or startup
#   scripts/machine.sh restart           restart the controller
#   scripts/machine.sh drill             the quarterly restore drill, on the machine: the nightly copy and the live copy
#   scripts/machine.sh backup            the nightly backup, now
#   scripts/machine.sh update COMMIT     install a commit from main; the previous one comes back if it does not answer
#   scripts/machine.sh config-get FILE   copy the machine's configuration into FILE (mode 600) to edit
#   scripts/machine.sh config-put FILE   check FILE loads, then make it the machine's; principals, applications and
#                                        settings apply at once, oauth and github_app changes need `restart`
#   scripts/machine.sh stop              hold the controller: stopped, and not started again by a reboot (before a laptop
#                                        takes over)
#   scripts/machine.sh start             release the hold and start it
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
         curl -fsS --max-time 5 http://127.0.0.1:8000/healthz && echo' ;;
  logs)
    UNIT=${2:-fast-deploy}
    [[ "$UNIT" == "startup" ]] && UNIT=google-startup-scripts
    [[ "$UNIT" =~ ^(fast-deploy|litestream|caddy|google-startup-scripts)$ ]] || { echo "UNIT is fast-deploy, litestream, caddy or startup"; exit 1; }
    iap "sudo journalctl -u $UNIT -n 200 --no-pager" ;;
  restart)
    iap 'sudo systemctl restart fast-deploy && sleep 3 && systemctl is-active fast-deploy' ;;
  drill)
    as_controller restore-drill ;;
  backup)
    as_controller backup ;;
  update)
    COMMIT=${2:?usage: scripts/machine.sh update COMMIT (a full commit on main)}
    [[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Give the full 40-character commit, for example: git rev-parse origin/main"; exit 1; }
    iap "sudo /usr/local/sbin/fast-deploy-install $COMMIT" ;;
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
  ssh)
    gcloud compute ssh "$NAME" --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap ;;
  *)
    sed -n '2,19p' "$0"
    exit 1 ;;
esac
