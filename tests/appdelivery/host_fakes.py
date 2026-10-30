"""A fake machine for the host script: `docker` and `curl` as small bash scripts that keep containers as directories, so
release.sh runs for real on a developer's bash (3.2 or later) with the exact input text machine.compose sends."""
import os
import subprocess
from pathlib import Path

from appdelivery import machine

FAKE_DOCKER = r'''#!/bin/bash
# containers live in $FAKE_STATE/c/<name>/{image,running,restart,release,env}
S="$FAKE_STATE"
echo "docker $*" >> "$S/calls"
c="$S/c"
mkdir -p "$c"
cmd=$1; shift
case "$cmd" in
  inspect)
    format=$2; name=$3
    [ -d "$c/$name" ] || exit 1
    case "$format" in
      *'.Id'*) echo "$name" ;;
      *'.State.Running'*) cat "$c/$name/running" ;;
      *'.Config.Image'*) cat "$c/$name/image" ;;
      *'appdelivery.release'*) cat "$c/$name/release" ;;
    esac ;;
  logs) echo "log line from $(cat "$c/$3/image" 2>/dev/null)"; echo "second line" ;;
  login)
    [ -n "$DOCKER_CONFIG" ] && [ -d "$DOCKER_CONFIG" ] || exit 3
    echo "$DOCKER_CONFIG" > "$S/login_config"
    cat > "$S/login_password"
    [ -e "$S/fail_login" ] && exit 1
    touch "$DOCKER_CONFIG/config.json"; exit 0 ;;
  pull) [ -e "$S/fail_pull" ] && exit 1; echo "${DOCKER_CONFIG:-none}" > "$S/pull_config"; exit 0 ;;
  rm) for name in "$@"; do [ "$name" = "-f" ] || rm -rf "${c:?}/$name"; done ;;
  update) policy=${1#--restart=}; echo "$policy" > "$c/$2/restart" ;;
  stop) echo false > "$c/$1/running" ;;
  start) echo true > "$c/$1/running" ;;
  rename) mv "$c/$1" "$c/$2" ;;
  run)
    name=""; restart=""; release=""; envfile=""; image=""; flags=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -d) ;;
        --name) name=$2; shift ;;
        --restart=*) restart=${1#--restart=} ;;
        --label) release=${2#appdelivery.release=}; shift ;;
        -p) shift ;;
        --env-file) envfile=$2; shift ;;
        --security-opt|--cap-drop|--cap-add|--cpus|--memory|--pids-limit|--log-opt) flags="$flags $1=$2"; shift ;;
        *) image=$1 ;;
      esac
      shift
    done
    [ -e "$S/fail_run" ] && exit 125
    mkdir -p "$c/$name"
    echo "$image" > "$c/$name/image"; echo "$restart" > "$c/$name/restart"; printf '%s' "$release" > "$c/$name/release"
    cp "$envfile" "$c/$name/env"; printf '%s' "${flags# }" > "$c/$name/flags"
    if [ -e "$S/crash_image" ] && [ "$(cat "$S/crash_image")" = "$image" ]; then echo false > "$c/$name/running"; else echo true > "$c/$name/running"; fi
    echo "container-$name" ;;
  image) echo pruned >> "$S/prunes" ;;
  *) echo "fake docker: unknown $cmd" >&2; exit 64 ;;
esac
'''

FAKE_CURL = r'''#!/bin/bash
S="$FAKE_STATE"
url=""
for arg in "$@"; do url=$arg; done
echo "$url" >> "$S/health_urls"
image=$(cat "$S/c/app/image" 2>/dev/null)
if [ -e "$S/unhealthy_image" ] && [ "$(cat "$S/unhealthy_image")" = "$image" ]; then printf 500; else printf 200; fi
'''


class FakeHost:
    def __init__(self, root: Path):
        self.root = root
        self.bin, self.state, self.run_dir = root / "bin", root / "state", root / "run"
        for directory in (self.bin, self.state, self.run_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for name, text in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            path = self.bin / name
            path.write_text(text)
            path.chmod(0o755)

    def run(self, action: str, inputs: dict[str, str], raw: str | None = None) -> subprocess.CompletedProcess:
        text = raw if raw is not None else machine.compose(action, inputs)
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_STATE": str(self.state), "APPDELIVERY_RUN_DIR": str(self.run_dir), "HOME": str(self.root)}
        return subprocess.run(["bash", "-s"], input=text, env=env, capture_output=True, text=True, timeout=60, check=False)

    def report(self, action, inputs):
        done = self.run(action, inputs)
        assert done.returncode == 0, done.stderr
        return machine.parse(done.stdout)

    def container(self, name: str) -> dict | None:
        directory = self.state / "c" / name
        if not directory.is_dir():
            return None
        return {item.name: item.read_text().strip() for item in directory.iterdir()}

    def mark(self, name: str, value: str = "") -> None:
        (self.state / name).write_text(value)

    def calls(self) -> str:
        path = self.state / "calls"
        return path.read_text() if path.exists() else ""
