"""Disposable worker entry point. No controller modules, repository checkout or credentials are installed."""
import base64
import hashlib
import io
import json
import os
import re
import resource
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

MAX_INPUT, MAX_OUTPUT = 1024 * 1024, 4 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Redirect refused")


def registry_requirement(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~][A-Za-z0-9.*,<>=!~+ -]+)?(?:\s*;[^\r\n]+)?", value)) and "://" not in value


def validate_input(mode, text):
    if len(text.encode()) > MAX_INPUT or "\x00" in text:
        raise ValueError("Input limit")
    if mode.endswith("-compile"):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not registry_requirement(line):
                raise ValueError("Only registry requirements are accepted")
    elif mode.startswith("node-"):
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("workspaces"):
            raise ValueError("Workspace dependencies need reviewed locks")
        for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for name, version in data.get(field, {}).items():
                if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name) or not isinstance(version, str):
                    raise ValueError("Invalid registry dependency")
                if not re.fullmatch(r"[A-Za-z0-9.*^~<>=| +_-]+", version):
                    raise ValueError("Only public registry dependency versions are accepted")
        if data.get("overrides"):
            raise ValueError("Dependency overrides need a reviewed lockfile")
    else:
        content = base64.b64decode(text, validate=True)
        if len(content) > MAX_INPUT:
            raise ValueError("Export input limit")
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            members = archive.getmembers()
            if len(members) != 2 or {m.name for m in members} != {"pyproject.toml", "uv.lock"}:
                raise ValueError("Export accepts exactly two input files")
            files = {}
            for member in members:
                if not member.isfile() or member.size > MAX_INPUT:
                    raise ValueError("Invalid archive member")
                files[member.name] = tomllib.loads(archive.extractfile(member).read().decode())
        tool = files["pyproject.toml"].get("tool", {}).get("uv", {})
        if any(key in tool for key in ("index", "sources", "workspace", "find-links", "index-url", "extra-index-url")):
            raise ValueError("Custom package sources require separate review")
        for package in files["uv.lock"].get("package", []):
            source = package.get("source", {})
            if source not in ({"registry": "https://pypi.org/simple"}, {"virtual": "."}):
                raise ValueError("Unapproved lock source")
            for artifact in package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else []):
                url = urllib.parse.urlsplit(artifact.get("url", ""))
                if url.scheme != "https" or url.netloc != "files.pythonhosted.org":
                    raise ValueError("Unapproved artifact source")


def main():
    mode = Path("/worker/mode").read_text().strip()
    input_url, output_url = os.environ.pop("INPUT_URL"), os.environ.pop("OUTPUT_URL")
    for url in (input_url, output_url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "storage.googleapis.com":
            raise ValueError("Invalid transfer endpoint")
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(input_url, timeout=30) as response:  # noqa: S310 - validated HTTPS endpoint, redirects refused
        data = response.read(MAX_INPUT * 2 + 1)
    if len(data) > MAX_INPUT * 2:
        raise ValueError("Manifest limit")
    manifest = json.loads(data)
    text = manifest["input"]
    if (manifest.get("schema") != 1 or manifest.get("mode") != mode or not re.fullmatch(r"[a-f0-9]{32}", manifest.get("id", ""))
            or manifest["deadline"] < time.time() or hashlib.sha256(text.encode()).hexdigest() != manifest["input_hash"]):
        raise ValueError("Manifest binding")
    validate_input(mode, text)
    # No inherited workload credentials or transfer URLs are passed to dependency hooks. The worker SA has no resource roles.
    env = {key: value for key, value in os.environ.items() if key.lower() in {"https_proxy", "http_proxy"}}
    env.update(PATH="/usr/local/bin:/usr/bin:/bin", HOME="/tmp", UV_NO_CACHE="1", UV_INDEX_URL="https://pypi.org/simple",  # noqa: S108 - disposable task
               PIP_INDEX_URL="https://pypi.org/simple", npm_config_registry="https://registry.npmjs.org", npm_config_update_notifier="false")
    # Bound individual files, process CPU and the task's wall time. Logs never include package output or transfer URLs.
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (840, 840))
    with open("/tmp/resolver.stdout", "wb") as output, open("/tmp/resolver.stderr", "wb") as error:  # noqa: S108 - private disposable task filesystem
        subprocess.run(["sh", "/worker/command.sh"], input=text.encode(), stdout=output, stderr=error, env=env, cwd="/tmp", check=True, timeout=840)  # noqa: S108
    with open("/tmp/resolver.stdout", "rb") as stream:  # noqa: S108
        output = stream.read(MAX_OUTPUT + 1)
    if len(output) > MAX_OUTPUT:
        raise ValueError("Output limit")
    payload = json.dumps({"schema": 1, "id": manifest["id"], "mode": mode, "input_hash": manifest["input_hash"], "output": output.decode()}).encode()
    if len(payload) > MAX_OUTPUT:
        raise ValueError("Result envelope limit")
    request = urllib.request.Request(output_url, data=payload, method="PUT", headers={"x-goog-if-generation-match": "0"})  # noqa: S310 - validated HTTPS endpoint
    with opener.open(request, timeout=30) as response:  # noqa: S310 - validated HTTPS endpoint, redirects refused
        if response.status not in (200, 201):
            raise ValueError("Result transfer failed")


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        print("Dependency worker failed: " + type(failure).__name__, file=sys.stderr)
        sys.exit(1)
