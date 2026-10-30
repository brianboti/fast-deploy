# appdelivery's host script (docs/DELIVERY-SPEC.md section 4). It is sent on standard input to `sudo bash -s` on a
# Container-Optimized OS machine, after lines that set IN_* variables whose values are base64 text only. It runs as root.
# It never takes a command, a script or a mount from a caller: what it runs is the image the service verified.
#
# Actions: deploy (pull, demote the running container, start the new one, check health, restore on failure), rollback
# (start the previous container again), remove (delete both containers), status (what runs now).
#
# It reports on standard output in lines that start with "APPDELIVERY ", as key=value; values that may hold anything
# (logs) are base64. Exit 0 means the report is complete, whatever the outcome; any other exit is a fault of the script.
# Written for bash 3.2 too, so its tests run on a developer's machine.
set -euo pipefail
umask 077

decode() { printf '%s' "$1" | base64 -d; }
say() { printf 'APPDELIVERY %s\n' "$*"; }

ACTION=$(decode "${IN_ACTION:-}")
RELEASE=$(decode "${IN_RELEASE:-}")
IMAGE=$(decode "${IN_IMAGE:-}")
PORT=$(decode "${IN_PORT:-}")
HEALTH_PATH=$(decode "${IN_HEALTH_PATH:-}")
TIMEOUT=$(decode "${IN_TIMEOUT:-}")
REGISTRY=$(decode "${IN_REGISTRY:-}")
# the bounds every container runs under; the service sends them, and a missing one gets a conservative default
CPUS=$(decode "${IN_CPUS:-}"); CPUS=${CPUS:-1}
MEMORY_MB=$(decode "${IN_MEMORY_MB:-}"); MEMORY_MB=${MEMORY_MB:-1024}
PIDS=$(decode "${IN_PIDS:-}"); PIDS=${PIDS:-512}
RUN_DIR=${APPDELIVERY_RUN_DIR:-/run}  # memory on Container-Optimized OS; a test points it elsewhere

APP=app
PREVIOUS=app-previous

exists() { docker inspect --format '{{.Id}}' "$1" >/dev/null 2>&1; }
running() { [ "$(docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]; }
image_of() { docker inspect --format '{{.Config.Image}}' "$1" 2>/dev/null || true; }
release_of() { docker inspect --format '{{index .Config.Labels "appdelivery.release"}}' "$1" 2>/dev/null || true; }
logs_of() { docker logs --tail 50 "$1" 2>&1 | base64 | tr -d '\n' || true; }

# 2xx from the container's own port within TIMEOUT seconds; a container that stops meanwhile is unhealthy at once.
healthy() {
    local deadline code
    deadline=$(( $(date +%s) + TIMEOUT ))
    while [ "$(date +%s)" -le "$deadline" ]; do
        if ! running "$APP"; then
            return 1
        fi
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}${HEALTH_PATH}" || true)
        case "$code" in
            2??) return 0 ;;
        esac
        sleep 2
    done
    return 1
}

pull() {
    if [ -n "${IN_TOKEN:-}" ]; then
        local config
        config=$(mktemp -d "$RUN_DIR/appdelivery-XXXXXX")
        # the token goes on standard input, never on a command line; the login is gone when this function returns
        if ! decode "$IN_TOKEN" | DOCKER_CONFIG="$config" docker login -u oauth2accesstoken --password-stdin "https://$REGISTRY" >/dev/null 2>&1; then
            rm -rf "$config"
            say result=pull_failed reason=login
            exit 0
        fi
        if ! DOCKER_CONFIG="$config" docker pull --quiet "$IMAGE" >/dev/null 2>&1; then
            rm -rf "$config"
            say result=pull_failed reason=pull
            exit 0
        fi
        rm -rf "$config"
    elif ! docker pull --quiet "$IMAGE" >/dev/null 2>&1; then
        say result=pull_failed reason=pull
        exit 0
    fi
}

# Start the previous container again as the current one, with the restart policy that brings it back after a reboot.
restore() {
    if exists "$PREVIOUS"; then
        docker rename "$PREVIOUS" "$APP" >/dev/null
        docker update --restart=always "$APP" >/dev/null
        docker start "$APP" >/dev/null
        return 0
    fi
    return 1
}

deploy() {
    pull
    # the values exist on disk only in memory (/run) and only until this script ends; Docker keeps its own copy in the
    # container's record, readable by root, as with any environment variable
    ENV_FILE=$(mktemp "$RUN_DIR/appdelivery-env-XXXXXX")
    trap 'rm -f "$ENV_FILE"' EXIT
    decode "${IN_ENV:-}" > "$ENV_FILE"
    if exists "$PREVIOUS"; then
        docker rm -f "$PREVIOUS" >/dev/null
    fi
    if exists "$APP"; then
        # a stopped container with restart=always comes back when the daemon restarts, and would take the port at the
        # next boot: the demoted one gets no restart policy until it is current again
        docker update --restart=no "$APP" >/dev/null
        docker stop "$APP" >/dev/null
        docker rename "$APP" "$PREVIOUS" >/dev/null
    fi
    # No privilege escalation, the capabilities an entrypoint needs to start as root and drop to its user and nothing more
    # (no raw sockets, chroot, mknod), and bounds on CPU, memory, processes and log size: a compromised or runaway
    # application stays an application (the review of 2026-10-30)
    if docker run -d --name "$APP" --restart=always --label "appdelivery.release=$RELEASE" -p "${PORT}:${PORT}" --env-file "$ENV_FILE" \
        --security-opt no-new-privileges --cap-drop ALL \
        --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID --cap-add SETGID --cap-add SETUID --cap-add NET_BIND_SERVICE --cap-add KILL \
        --cpus "$CPUS" --memory "${MEMORY_MB}m" --pids-limit "$PIDS" --log-opt max-size=50m --log-opt max-file=3 "$IMAGE" >/dev/null 2>&1 && healthy; then
        say result=healthy image="$IMAGE" release="$RELEASE" previous="$(image_of "$PREVIOUS")"
        docker image prune -af >/dev/null 2>&1 || true
        return
    fi
    local logs
    logs=$(logs_of "$APP")
    docker rm -f "$APP" >/dev/null 2>&1 || true
    if restore; then
        say result=unhealthy restored=yes image="$(image_of "$APP")" log="$logs"
    else
        say result=unhealthy restored=none log="$logs"
    fi
}

rollback() {
    if ! exists "$PREVIOUS"; then
        say result=no_previous
        return
    fi
    if exists "$APP"; then
        docker rm -f "$APP" >/dev/null
    fi
    restore
    if healthy; then
        say result=rolled_back image="$(image_of "$APP")" release="$(release_of "$APP")"
    else
        say result=rollback_unhealthy image="$(image_of "$APP")" log="$(logs_of "$APP")"
    fi
}

remove() {
    docker rm -f "$APP" "$PREVIOUS" >/dev/null 2>&1 || true
    docker image prune -af >/dev/null 2>&1 || true
    say result=removed
}

status() {
    if exists "$APP"; then
        say result=status image="$(image_of "$APP")" release="$(release_of "$APP")" running="$(running "$APP" && echo yes || echo no)" previous="$(image_of "$PREVIOUS")"
    else
        say result=status image= release= running=no previous="$(image_of "$PREVIOUS")"
    fi
}

case "$ACTION" in
    deploy) deploy ;;
    rollback) rollback ;;
    remove) remove ;;
    status) status ;;
    *) say result=bad_action; exit 2 ;;
esac
