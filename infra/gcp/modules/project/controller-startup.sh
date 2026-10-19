#!/bin/bash
# The Fast Deploy controller's machine (docs/ROADMAP.md D36, Phase 15). Compute Engine runs this as root at every boot;
# running it again changes nothing that is already right. It installs Caddy and Litestream (pinned by checksum), writes
# their configuration and three systemd services with Restart=always, and installs the controller from a commit on the
# platform repository's main branch; the machine's own update service waits for the controller's request after a release. On a fresh disk it also restores what the machine had: its configuration and the
# commit it ran from gs://BUCKET/machine/, and its database from the Litestream replica (or, failing that, the newest
# nightly copy). It never starts the controller without a database: an empty one would forget every plan and release.
# Its log: `journalctl -u google-startup-scripts` on the machine, or `scripts/machine.sh logs startup` from the laptop.
# A foundation apply changes it in the machine's metadata without a reboot; `scripts/machine.sh startup` runs it again then.
set -euo pipefail
umask 022

# Pinned tools: the release archives' own published checksums (Caddy's list is SHA-512, Litestream's SHA-256).
CADDY_VERSION=2.11.7
CADDY_SHA512=a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7
LITESTREAM_VERSION=0.5.17
LITESTREAM_SHA256=cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d

DATA=/var/lib/fast-deploy
OPT=/opt/fast-deploy
STAMPS=/usr/local/lib/fast-deploy-tools

log() { echo "fast-deploy-startup: $*"; }
meta() { curl -fsS --retry 5 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }

HOST=$(meta fast-deploy-host)  # where Claude reaches the machine: its own address, or a DNS name
REPOSITORY=$(meta fast-deploy-repository)
COMMIT=$(meta fast-deploy-commit)
BUCKET=$(meta fast-deploy-bucket)
ACME_EMAIL=$(meta fast-deploy-acme-email 2>/dev/null || true)
ADDRESS_ONLY=""
if [[ "$HOST" =~ ^(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}$ ]]; then
  ADDRESS_ONLY=yes
elif [[ ! "$HOST" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
  log "fast-deploy-host is neither an IPv4 address nor a DNS name"
  exit 1
fi
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { log "fast-deploy-repository is not owner/name"; exit 1; }
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { log "fast-deploy-commit is not a full commit"; exit 1; }
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$ ]] || { log "fast-deploy-bucket is not a bucket name"; exit 1; }
[[ -z "$ACME_EMAIL" || "$ACME_EMAIL" =~ ^[^[:space:]@\"{}]+@[^[:space:]@\"{}]+$ ]] || { log "fast-deploy-acme-email is not an address"; exit 1; }

# Swap: the free machine has 1 GB of memory, and gcloud (the nightly upload) is a large Python program.
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Packages, and security updates that install themselves; one that needs a reboot gets it at 09:00 UTC (about four in
# the morning in Texas), and the controller is back within a minute.
export DEBIAN_FRONTEND=noninteractive
missing=()
for package in git python3 python3-venv sqlite3 ca-certificates curl unattended-upgrades docker.io nftables; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || missing+=("$package")
done
if (( ${#missing[@]} )); then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends "${missing[@]}"
fi
if ! command -v gcloud >/dev/null; then  # Google's Debian images carry it; this is only the fallback
  apt-get install -y -qq --no-install-recommends gnupg
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends google-cloud-cli
fi
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52fast-deploy-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "09:00";
EOF

# Caddy and Litestream from their release archives, checked against the pinned sums before anything runs.
install_tool() {  # NAME URL SUM ALGORITHM: install /usr/local/bin/NAME from the archive unless this sum is installed
  local name=$1 url=$2 sum=$3 algorithm=$4 work
  [[ -x "/usr/local/bin/$name" && "$(cat "$STAMPS/$name" 2>/dev/null)" == "$sum" ]] && return 0
  work=$(mktemp -d)
  curl -fsSL --retry 3 -o "$work/archive.tar.gz" "$url"
  echo "$sum  $work/archive.tar.gz" | "${algorithm}sum" --check --strict --quiet
  tar -xzf "$work/archive.tar.gz" -C "$work" "$name"
  install -m 0755 "$work/$name" "/usr/local/bin/$name"
  install -d -m 0755 "$STAMPS"
  echo "$sum" > "$STAMPS/$name"
  rm -rf "$work"
  log "installed $name from $url"
}
install_tool caddy "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" "$CADDY_SHA512" sha512
install_tool litestream "https://github.com/benbjohnson/litestream/releases/download/v$LITESTREAM_VERSION/litestream-$LITESTREAM_VERSION-linux-x86_64.tar.gz" "$LITESTREAM_SHA256" sha256

# Docker, only for the lock files onboarding compiles in a container of the application's own Python (decision D27).
# Resolving dependencies can build a package and so run its code, so those containers never reach the metadata server
# (the machine's identity, and through it the GitHub App's key) or any private address, and they use public DNS
# (on Google Cloud the metadata server is the DNS server too). Docker starts on first use, not at boot.
install -d -m 0755 /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{"dns": ["8.8.8.8", "8.8.4.4"], "log-driver": "local"}
EOF
cat > /etc/fast-deploy-containers.nft <<'EOF'
table inet fast_deploy_containers {
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "docker0" ip daddr { 169.254.0.0/16, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 } drop
  }
}
EOF
nft delete table inet fast_deploy_containers >/dev/null 2>&1 || true
nft -f /etc/fast-deploy-containers.nft
systemctl disable --quiet --now docker.service
systemctl enable --quiet --now docker.socket

# Users and folders: the controller and Litestream run as fastdeploy, Caddy as caddy; the code is root's and read-only.
id -u fastdeploy >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin fastdeploy
usermod -aG docker fastdeploy
id -u caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --create-home --shell /usr/sbin/nologin caddy
install -d -o fastdeploy -g fastdeploy -m 0750 "$DATA"
install -d -m 0755 "$OPT" "$OPT/releases" "$OPT/venvs" /etc/caddy /etc/fast-deploy
cat > /etc/fast-deploy/machine.env <<EOF
HOST=$HOST
REPOSITORY=$REPOSITORY
BUCKET=$BUCKET
EOF

# fast-deploy-install COMMIT: the update path (scripts/machine.sh update, and fast-deploy-update below). Only a commit on
# main runs here; the old release comes back if the new one does not answer within a minute. One install at a time.
cat > /usr/local/sbin/fast-deploy-install <<'EOF'
#!/bin/bash
set -euo pipefail
source /etc/fast-deploy/machine.env
COMMIT=${1:?usage: fast-deploy-install COMMIT}
OPT=/opt/fast-deploy
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Give a full 40-character commit."; exit 1; }
if [[ -z "${FAST_DEPLOY_INSTALL_LOCKED:-}" ]]; then  # fast-deploy-update holds the lock already, for its watch afterwards too
  exec 9>/run/fast-deploy-install.lock
  flock -w 900 9 || { echo "Another install has held the machine for fifteen minutes; try again later."; exit 1; }
fi
[[ -d "$OPT/repo/.git" ]] || git clone --quiet --no-checkout "https://github.com/$REPOSITORY" "$OPT/repo"
git -C "$OPT/repo" fetch --quiet --prune origin main
git -C "$OPT/repo" merge-base --is-ancestor "$COMMIT" origin/main || { echo "$COMMIT is not on $REPOSITORY's main branch: only merged code runs here."; exit 1; }
release="$OPT/releases/$COMMIT"
[[ -d "$release" ]] || git -C "$OPT/repo" worktree add --quiet --detach "$release" "$COMMIT"
venv="$OPT/venvs/$(sha256sum "$release/requirements.lock" | cut -c1-16)"
if [[ ! -x "$venv/bin/uvicorn" ]]; then
  rm -rf "$venv"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check --require-hashes -r "$release/requirements.lock"
fi
previous=$(readlink "$OPT/current" 2>/dev/null || true)
previous_venv=$(readlink "$OPT/venv" 2>/dev/null || true)
switch() { ln -sfn "$1" "$OPT/current.next" && mv -Tf "$OPT/current.next" "$OPT/current"; ln -sfn "$2" "$OPT/venv.next" && mv -Tf "$OPT/venv.next" "$OPT/venv"; }
answers() { for _ in $(seq 1 30); do curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && return 0; sleep 2; done; return 1; }
switch "$release" "$venv"
if [[ -f /var/lib/fast-deploy/held ]]; then
  echo "Installed; the controller is held (scripts/machine.sh start runs it)."
elif [[ -f /var/lib/fast-deploy/config.yaml && -f /var/lib/fast-deploy/deployments.db ]]; then
  systemctl restart fast-deploy
  if ! answers; then
    echo "The controller did not answer on $COMMIT."
    if [[ -n "$previous" ]]; then
      switch "$previous" "$previous_venv"
      systemctl restart fast-deploy
      echo "Back on $(basename "$previous"); see journalctl -u fast-deploy for why the new one failed."
    fi
    exit 1
  fi
fi
echo "$COMMIT" | gcloud storage cp - "gs://$BUCKET/machine/installed-commit" --quiet
for old in $(ls -1t "$OPT/releases" | tail -n +4); do
  if [[ "$OPT/releases/$old" != "$(readlink "$OPT/current")" ]]; then git -C "$OPT/repo" worktree remove --force "$OPT/releases/$old"; fi
done
# An environment that no kept release installs from goes too: each holds a few hundred megabytes of the 30 GB disk, and
# updates that install themselves would otherwise pile them up.
kept=$(for folder in "$OPT"/releases/*/; do sha256sum "$folder/requirements.lock" 2>/dev/null | cut -c1-16 || true; done)
for environment in "$OPT"/venvs/*/; do
  environment=${environment%/}
  [[ -d "$environment" ]] || continue
  if ! grep -qxF "$(basename "$environment")" <<<"$kept" && [[ "$environment" != "$(readlink "$OPT/venv")" ]]; then rm -rf "$environment"; fi
done
echo "The controller runs $COMMIT."
EOF
chmod 0755 /usr/local/sbin/fast-deploy-install

# fast-deploy-update: the machine's own update after a platform release (docs/RUNBOOK.md, "After a release"). The
# controller cannot restart itself (its service has no privileges), so once main's checks have passed and the delay is
# over it writes the commit into /var/lib/fast-deploy/update-request, and fast-deploy-update.path starts this as root.
# It installs only a commit on main that contains the one running (never an older one), through fast-deploy-install and
# its minute of health checks, then watches the controller for two more minutes and puts the previous commit back if it
# stops answering or restarts. The result goes to /var/lib/fast-deploy-update/result.json, in a folder only root writes,
# and the controller records it in its audit trail. Its log: `scripts/machine.sh logs update`.
cat > /usr/local/sbin/fast-deploy-update <<'EOF'
#!/bin/bash
set -uo pipefail
DATA=${FAST_DEPLOY_DATA:-/var/lib/fast-deploy}
OPT=${FAST_DEPLOY_OPT:-/opt/fast-deploy}
OUT=${FAST_DEPLOY_UPDATE_OUT:-/var/lib/fast-deploy-update}
INSTALL=${FAST_DEPLOY_INSTALL:-/usr/local/sbin/fast-deploy-install}
LOCK=${FAST_DEPLOY_LOCK:-/run/fast-deploy-install.lock}
SOAK=${FAST_DEPLOY_SOAK_SECONDS:-120}
HEALTH=http://127.0.0.1:8000/healthz
REQUEST="$DATA/update-request"
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# The request leaves the folder first, whatever it holds: the path unit starts this again for as long as it is there.
requested=""
if [[ -f "$REQUEST" && ! -L "$REQUEST" ]]; then requested=$(head -c 100 "$REQUEST" | tr -d '[:space:]'); fi
rm -f "$REQUEST"
previous=$(readlink "$OPT/current" 2>/dev/null || true)
previous=${previous##*/}
result() {  # OUTCOME DETAIL: the one line the controller reads, written whole
  install -d -m 0755 "$OUT"
  python3 -c 'import datetime, json, sys; print(json.dumps(dict(zip(("commit", "previous", "outcome", "detail", "started_at"), sys.argv[1:]),
              finished_at=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))))' \
    "$requested" "$previous" "$1" "$2" "$started" > "$OUT/result.json.next"
  chmod 0644 "$OUT/result.json.next"
  mv -f "$OUT/result.json.next" "$OUT/result.json"
  echo "fast-deploy-update: $1 ${requested:0:12}: $2"
}
if [[ ! "$requested" =~ ^[0-9a-f]{40}$ ]]; then
  requested=""
  result refused "the request did not hold a full 40-character commit"
  exit 0
fi
exec 9>"$LOCK"
flock -w 900 9 || { result failed "another install held the machine for fifteen minutes"; exit 1; }
export FAST_DEPLOY_INSTALL_LOCKED=1
if [[ -f "$DATA/held" ]]; then result refused "the controller is held (scripts/machine.sh start releases it)"; exit 0; fi
if [[ "$requested" == "$previous" ]]; then result current "the controller runs it already"; exit 0; fi
git -C "$OPT/repo" fetch --quiet --prune origin main || { result failed "main could not be fetched from GitHub; nothing changed"; exit 1; }
if ! git -C "$OPT/repo" merge-base --is-ancestor "$requested" origin/main 2>/dev/null; then
  result refused "it is not on main: only merged code runs here"
  exit 0
fi
if [[ -n "$previous" ]] && ! git -C "$OPT/repo" merge-base --is-ancestor "$previous" "$requested" 2>/dev/null; then
  result refused "it does not contain the commit that runs (${previous:0:12}): the machine never goes back by itself"
  exit 0
fi
if ! output=$("$INSTALL" "$requested" 2>&1); then
  echo "$output"
  why=$(tail -n 2 <<<"$output" | tr '\n' ' ')
  now=$(readlink "$OPT/current" 2>/dev/null || true)
  if [[ "${now##*/}" == "$previous" ]]; then result rolled_back "${previous:0:12} still runs: $why"; else result failed "$why"; fi
  exit 1
fi
echo "$output"
# Two more minutes: the controller must keep answering and must not have restarted. With RestartMode at its default, a
# restart by Restart=always passes through inactive, and systemd gives every new run of a unit a new invocation ID.
invocation=$(systemctl show -p InvocationID --value fast-deploy)
for _ in $(seq 1 $((SOAK / 10))); do
  sleep 10
  if ! systemctl is-active --quiet fast-deploy || ! curl -fsS --max-time 5 "$HEALTH" >/dev/null 2>&1 \
     || [[ "$(systemctl show -p InvocationID --value fast-deploy)" != "$invocation" ]]; then
    if [[ -n "$previous" ]] && "$INSTALL" "$previous"; then
      result rolled_back "it stopped answering or restarted within $SOAK seconds; ${previous:0:12} runs again (scripts/machine.sh logs says why)"
    else
      result failed "it stopped answering within $SOAK seconds, and putting ${previous:0:12} back failed too: scripts/machine.sh status"
    fi
    exit 1
  fi
done
result installed "the controller answered for $SOAK seconds after its first minute, without a restart"
EOF
chmod 0755 /usr/local/sbin/fast-deploy-update
install -d -m 0755 /var/lib/fast-deploy-update

# fast-deploy-import [--replace] DIR: the move (scripts/install-machine.sh copies a database and a configuration into
# DIR). A machine that already has a database keeps it unless --replace is given; then the old one stays beside it.
cat > /usr/local/sbin/fast-deploy-import <<'EOF'
#!/bin/bash
set -euo pipefail
source /etc/fast-deploy/machine.env
DATA=/var/lib/fast-deploy
replace=""
if [[ "${1:-}" == "--replace" ]]; then replace=yes; shift; fi
DIR=${1:?usage: fast-deploy-import [--replace] DIR}
[[ -f "$DIR/config.yaml" && -f "$DIR/deployments.db" ]] || { echo "$DIR needs config.yaml and deployments.db."; exit 1; }
if [[ -f "$DATA/deployments.db" && -z "$replace" ]]; then
  echo "This machine already has a database. Run again with --replace to make the copied one current (the old one is kept)."
  exit 1
fi
# Both are checked before anything here changes: the configuration with the code that will read it, the database with
# SQLite's own check (read-only: a VACUUM INTO copy needs no journal files to open).
(cd /opt/fast-deploy/current && /opt/fast-deploy/venv/bin/python -c 'import sys; from labdeploy.config import load_config; load_config(sys.argv[1])' "$DIR/config.yaml")
[[ "$(sqlite3 -readonly "$DIR/deployments.db" 'PRAGMA quick_check')" == "ok" ]] || { echo "The copied database fails its integrity check; nothing was changed."; exit 1; }
systemctl stop fast-deploy litestream 2>/dev/null || true
if [[ -f "$DATA/deployments.db" ]]; then
  stamp=$(date -u +%Y%m%d-%H%M%S)
  for part in "" -wal -shm; do
    if [[ -f "$DATA/deployments.db$part" ]]; then mv "$DATA/deployments.db$part" "$DATA/deployments.db.replaced-$stamp$part"; fi
  done
fi
install -o fastdeploy -g fastdeploy -m 0600 "$DIR/config.yaml" "$DATA/config.yaml"
install -o fastdeploy -g fastdeploy -m 0600 "$DIR/deployments.db" "$DATA/deployments.db"
rm -f "$DATA/held"  # this machine is the controller now
# A new database starts a new replica: the old one belongs to a database that is no longer this machine's.
gcloud storage rm --recursive --quiet "gs://$BUCKET/machine/litestream/" >/dev/null 2>&1 || true
gcloud storage cp "$DATA/config.yaml" "gs://$BUCKET/machine/config.yaml" --quiet
rm -f "$DIR/config.yaml" "$DIR/deployments.db"
rmdir "$DIR" 2>/dev/null || true
systemctl start litestream fast-deploy
systemctl restart caddy
for _ in $(seq 1 30); do curl -fsS --max-time 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && { echo "The controller runs here now."; exit 0; }; sleep 2; done
echo "The controller did not answer; see journalctl -u fast-deploy."
exit 1
EOF
chmod 0755 /usr/local/sbin/fast-deploy-import

# Caddy: HTTPS with Let's Encrypt, in front of the controller on 127.0.0.1:8000. Its admin endpoint is off, so nothing on
# the machine can reconfigure it; HTTP/3 is off because the firewall opens TCP only. For the machine's own address,
# Let's Encrypt issues a certificate for the address itself only in its short-lived profile (six days; free like every
# other, and Caddy renews it every few days), and a client that connects by address sends no server name, so Caddy is
# told which certificate to show it.
{
  echo "# Written at every boot by the machine's startup script (infra/gcp/modules/project/controller-startup.sh)."
  echo "{"
  echo "	admin off"
  if [[ -n "$ACME_EMAIL" ]]; then echo "	email $ACME_EMAIL"; fi
  if [[ -n "$ADDRESS_ONLY" ]]; then echo "	default_sni $HOST"; fi
  echo "	servers {"
  echo "		protocols h1 h2"
  echo "	}"
  echo "}"
  echo ""
  echo "$HOST {"
  if [[ -n "$ADDRESS_ONLY" ]]; then
    echo "	tls {"
    echo "		issuer acme {"
    echo "			profile shortlived"
    echo "		}"
    echo "	}"
  fi
  echo "	reverse_proxy 127.0.0.1:8000"
  echo "}"
} > /etc/caddy/Caddyfile

# Litestream: the database's live copy, at most a minute behind. The intervals are chosen for cost: an upload a minute
# while the database changes, compaction every five minutes and every hour, a daily snapshot kept three days, and the
# old small files swept every five minutes; a few cents of Cloud Storage operations a month.
cat > /etc/litestream.yml <<EOF
# Written at every boot by the machine's startup script (infra/gcp/modules/project/controller-startup.sh).
dbs:
  - path: $DATA/deployments.db
    replica:
      url: gs://$BUCKET/machine/litestream/deployments.db
      sync-interval: 1m
levels:
  - interval: 5m
  - interval: 1h
snapshot:
  interval: 24h
  retention: 72h
l0-retention: 10m
l0-retention-check-interval: 5m
EOF

cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy: HTTPS in front of the Fast Deploy controller
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
RestartSec=5
TimeoutStopSec=5s
LimitNOFILE=1048576
PrivateTmp=true
ProtectSystem=full
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/litestream.service <<'EOF'
[Unit]
Description=Litestream: the controller database's live copy in Cloud Storage
After=network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/fast-deploy/deployments.db

[Service]
User=fastdeploy
Group=fastdeploy
ExecStart=/usr/local/bin/litestream replicate -config /etc/litestream.yml
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/fast-deploy

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/fast-deploy.service <<'EOF'
[Unit]
Description=Fast Deploy controller
After=network-online.target litestream.service
Wants=network-online.target
ConditionPathExists=/var/lib/fast-deploy/config.yaml
ConditionPathExists=/var/lib/fast-deploy/deployments.db
# scripts/machine.sh stop holds the controller (before a laptop takes over); not even a reboot starts it then.
ConditionPathExists=!/var/lib/fast-deploy/held

[Service]
User=fastdeploy
Group=fastdeploy
SupplementaryGroups=docker
WorkingDirectory=/opt/fast-deploy/current
Environment=LAB_CONFIG=/var/lib/fast-deploy/config.yaml
Environment=HOME=/var/lib/fast-deploy
Environment=CLOUDSDK_CONFIG=/var/lib/fast-deploy/.gcloud
Environment=PATH=/opt/fast-deploy/venv/bin:/usr/local/bin:/usr/bin:/bin
# Caddy is the only proxy, on this machine: uvicorn takes the caller's address from its X-Forwarded-For, so the
# per-address limits on failed sign-ins see real callers.
ExecStart=/opt/fast-deploy/venv/bin/uvicorn labdeploy.server:create_app --factory --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=5
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/fast-deploy
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryMax=600M

[Install]
WantedBy=multi-user.target
EOF

# The machine's own update (fast-deploy-update above): the controller's request file starts it, as root, once at a time.
cat > /etc/systemd/system/fast-deploy-update.service <<'EOF'
[Unit]
Description=Fast Deploy: install the commit the controller asked for, with the health check and the way back
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fast-deploy-update
TimeoutStartSec=30min
EOF
cat > /etc/systemd/system/fast-deploy-update.path <<'EOF'
[Unit]
Description=Fast Deploy: wait for the controller's update request

[Path]
PathExists=/var/lib/fast-deploy/update-request

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --quiet caddy litestream fast-deploy
systemctl enable --quiet --now fast-deploy-update.path

# A fresh disk restores the machine's own state from its folder in the backup bucket; the laptop's copies under
# controller/ are never read here, so a machine can not wake up as a second controller with a laptop's configuration.
if [[ ! -f "$DATA/config.yaml" ]] && gcloud storage cp "gs://$BUCKET/machine/config.yaml" "$DATA/config.yaml.restoring" --quiet >/dev/null 2>&1; then
  chown fastdeploy:fastdeploy "$DATA/config.yaml.restoring"
  chmod 0600 "$DATA/config.yaml.restoring"
  mv "$DATA/config.yaml.restoring" "$DATA/config.yaml"
  log "restored the configuration from gs://$BUCKET/machine/config.yaml"
fi
if [[ -f "$DATA/config.yaml" && ! -f "$DATA/deployments.db" ]]; then
  if runuser -u fastdeploy -- /usr/local/bin/litestream restore -if-replica-exists -integrity-check quick -o "$DATA/deployments.db" "gs://$BUCKET/machine/litestream/deployments.db" && [[ -f "$DATA/deployments.db" ]]; then
    log "restored the database from the Litestream replica"
  else
    newest=$(gcloud storage ls "gs://$BUCKET/machine/deployments-*.db" 2>/dev/null | sort | tail -n 1 || true)
    if [[ -n "$newest" ]] && gcloud storage cp "$newest" "$DATA/deployments.db.restoring" --quiet >/dev/null 2>&1; then
      chown fastdeploy:fastdeploy "$DATA/deployments.db.restoring"
      chmod 0600 "$DATA/deployments.db.restoring"
      mv "$DATA/deployments.db.restoring" "$DATA/deployments.db"
      log "no Litestream replica: restored the nightly copy $newest (up to a day old)"
    else
      log "no database to restore: the controller stays off until scripts/install-machine.sh brings one"
    fi
  fi
fi

if [[ ! -L "$OPT/current" ]]; then
  wanted=$(gcloud storage cat "gs://$BUCKET/machine/installed-commit" 2>/dev/null | tr -d '[:space:]' || true)
  [[ "$wanted" =~ ^[0-9a-f]{40}$ ]] || wanted=$COMMIT
  /usr/local/sbin/fast-deploy-install "$wanted"
fi

systemctl restart caddy
if [[ -f "$DATA/held" ]]; then
  if [[ -f "$DATA/deployments.db" ]]; then systemctl start litestream; fi
  log "the controller is held (scripts/machine.sh stop); scripts/machine.sh start runs it again"
elif [[ -f "$DATA/config.yaml" && -f "$DATA/deployments.db" ]]; then
  systemctl start litestream fast-deploy
  log "the controller is running for https://$HOST"
else
  log "Caddy answers for https://$HOST; the controller waits for its configuration and database (scripts/install-machine.sh)"
fi
