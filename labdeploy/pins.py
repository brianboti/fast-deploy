"""The platform's own pins that Dependabot does not follow, kept current by one reviewed pull request a week.

Dependabot follows the platform's GitHub Actions and Terraform providers. Everything else the platform pins by version and
checksum, or by digest, is listed in PINS with its official source, the line of releases the platform follows and every
place it is written: the tools the pipeline downloads (templates/deploy-gcp.yml, .github/workflows/promote.yml), the
buildpacks builders and run images, the python:X.Y-slim and node:<line>-slim indexes, uv and the npm the lock containers
install (labdeploy/scaffold.py), CI's hadolint and OpenTofu, and the machine's Caddy and Litestream.

Once a week .github/workflows/pins.yml runs `refresh`: for each pin it reads the official release metadata, takes the
newest release of its line that is at least seven days old, and writes it in every place, with the checksum that metadata
states. A download from GitHub takes the SHA-256 GitHub recorded when the asset was uploaded, and the project's own
checksum list must say the same; an image takes its registry's index digest; uv and npm take the hashes PyPI and the npm
registry publish. Nothing here hashes a download to make a pin: `verify` downloads what changed only to check it against
the new pin before the pull request opens, and never runs it. The pull request names each change and its source, and a
person merges it.

    python -m labdeploy.pins list        the pins as the files hold them (no network)
    python -m labdeploy.pins check       what a refresh would propose today (reads the metadata, writes nothing)
    python -m labdeploy.pins refresh --report PULL_REQUEST.md --plan PLAN.json
    python -m labdeploy.pins verify PLAN.json
    python -m labdeploy.pins paths       the files a refresh may change
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .workflows import central_deploy

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = "templates/deploy-gcp.yml"
CENTRAL = ".github/workflows/deploy.yml"  # generated from the template, never edited by hand
SCAFFOLD = "labdeploy/scaffold.py"
CI = ".github/workflows/ci.yml"
PROMOTE = ".github/workflows/promote.yml"
STARTUP = "infra/gcp/modules/project/controller-startup.sh"
CONTRACT = "docs/DEVELOPER-CONTRACT.md"
RUNBOOK = "docs/RUNBOOK.md"
SETUP = "docs/SETUP.md"
HANDOFF = "PROJECT_HANDOFF.md"
# A release must be a week old before it is proposed, as Dependabot's cooldown here: a compromised or broken release is
# usually found and pulled by then (Trivy's malicious 0.69.4 of 2026-03-19 was up for about three hours, GHSA-69fq-xp46-6x23).
COOLDOWN = timedelta(days=7)
USER_AGENT = "fast-deploy-pins (+https://github.com/brianboti/fast-deploy)"
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")

SHA256 = r"[0-9a-f]{64}"
# What each {field} of a place may hold. A field named after a wheel's platform tag (manylinux_...) holds that wheel's SHA-256.
FIELDS = {"version": r"\d+(?:\.\d+){1,3}", "sha256": SHA256, "sha512": r"[0-9a-f]{128}", "digest": rf"sha256:{SHA256}",
          "integrity": r"sha512-[A-Za-z0-9+/]{86}==", "released": r"\d{4}-\d{2}-\d{2}", "date": r"\d{4}-\d{2}-\d{2}",
          "release": r"\d+\.\d+\.\d+", "debian": r"[a-z]+"}

# When a merged change reaches where it runs, in the pull request's words.
PIPELINE = ("the platform pipeline's next release of each service under apps/; an application with its own workflows once "
            "they are re-pinned (`update_app_workflows`, or by themselves with `platform_updates`)")
CONTROLLER = "the controller once it runs the merged commit (`scripts/machine.sh update <commit>`, or by itself with `platform_updates`)"
MACHINE = ("the controller's machine only after the foundation is applied (`scripts/apply-foundation.sh`) and its startup "
           "script runs again (`scripts/machine.sh startup`, or a restart)")
AT_ONCE = "this repository's CI as soon as it merges"


class PinError(Exception):
    """The files do not hold the pins as PINS describes them, or the metadata cannot be used as it is."""


@dataclass(frozen=True)
class Pin:
    key: str
    title: str
    kind: str          # github, dockerhub, gcr, pypi or npm: where its official metadata lives
    source: str        # owner/repository; image:tag; the package's name
    places: tuple      # (path, the text as written with its pinned values as {fields}), each found exactly once
    used: str          # what it does, in the pull request's words
    reaches: str       # when a merged change reaches where it runs
    line: str = ""     # the releases followed: "3" takes 3.x.y, "0.12" takes 0.12.y; an image follows its tag
    line_note: str = ""  # why a newer line waits for a person, when there is more to say than that it is a new line
    asset: str = ""    # github: the release asset, {version} standing for the version
    sums: str = ""     # github: the project's own checksum list among the release's assets
    algorithm: str = "sha256"  # github: the checksum the places hold (Caddy's own list is SHA-512)
    stack: str = ""    # gcr: the io.buildpacks.stack.id label the image must carry
    tags: str = ""     # dockerhub: the version tag that names the image's release and Debian, as a regular expression
    notes: str = ""    # pypi, npm: the release notes, {version} standing for the version


def release_url(repository, asset):
    return f"https://github.com/{repository}/releases/download/v{{version}}/{asset}"


def fetched(path, repository, asset, file):
    """The two lines of a workflow that download a tool by version and hold it to its SHA-256."""
    return ((path, release_url(repository, asset)), (path, f'echo "{{sha256}}  $RUNNER_TEMP/{file}"'))


def github_tool(key, title, repository, asset, sums, file, line, used, reaches, paths=(TEMPLATE,), extra=(), **more):
    places = tuple(place for path in paths for place in fetched(path, repository, asset, file)) + tuple(extra)
    return Pin(key, title, "github", repository, places, used, reaches, line=line, asset=asset, sums=sums, **more)


def python_image(version):
    places = ((SCAFFOLD, f'    "{version}": "{{digest}}",  # {{release}}, Debian {{debian}}'),)
    if version == "3.12":  # the platform's own image and the demo app build on the same index (tests/test_scaffold.py)
        places += (("Dockerfile", "FROM python:3.12-slim@{digest}"), ("examples/demo-app/Dockerfile", "FROM python:3.12-slim@{digest}"))
    used = "drafted Dockerfiles, the platform's own Python image (`build.recipe: platform`) and the lock containers"
    return Pin(f"python-{version}", f"python:{version}-slim", "dockerhub", f"python:{version}-slim", places,
               used + (", and the platform's and the demo app's Dockerfiles" if version == "3.12" else ""),
               f"drafts and lock containers on {CONTROLLER}; the platform's own image at {PIPELINE}",
               tags=r"(?P<release>\d+\.\d+\.\d+)-slim-(?P<debian>[a-z]+)")


def node_image(line):
    return Pin(f"node-{line}", f"node:{line}-slim", "dockerhub", f"node:{line}-slim",
               ((SCAFFOLD, f'    "{line}": "{{digest}}",  # {{release}}, Debian {{debian}}'),),
               "drafted Node.js Dockerfiles, the platform's Node.js image and the lock containers",
               f"drafts and lock containers on {CONTROLLER}; the platform's own image at {PIPELINE}",
               tags=r"(?P<release>\d+\.\d+\.\d+)-(?P<debian>[a-z]+)-slim")


def buildpacks(key, title, source, stack, pattern):
    return Pin(key, title, "gcr", source, ((TEMPLATE, pattern),),
               "builds and runs a service that has no Dockerfile (Google's buildpacks; the Ubuntu 22.04 pair for Python "
               "3.11 and 3.12, the 24.04 pair for the rest)", PIPELINE, stack=stack)


PINS = (
    github_tool("trufflehog", "TruffleHog", "trufflesecurity/trufflehog", "trufflehog_{version}_linux_amd64.tar.gz",
                "trufflehog_{version}_checksums.txt", "trufflehog.tar.gz", "3",
                "finds working keys in the checkout and the image of every release", PIPELINE),
    github_tool("trivy", "Trivy", "aquasecurity/trivy", "trivy_{version}_Linux-64bit.tar.gz", "trivy_{version}_checksums.txt",
                "trivy.tar.gz", "0", "reads Dockerfiles and makes each image's parts list (SBOM)", PIPELINE),
    github_tool("osv-scanner", "OSV-Scanner", "google/osv-scanner", "osv-scanner_linux_amd64", "osv-scanner_SHA256SUMS",
                "osv-scanner", "2", "audits a Node.js service's lockfile before anything is installed", PIPELINE,
                extra=(("labdeploy/scan.py", 'OSV_SCANNER = "OSV-Scanner {version}"'),
                       (CONTRACT, "OSV-Scanner {version} (pinned by version and checksum)"),
                       (HANDOFF, "The pipeline runs OSV-Scanner {version} on the lockfile"))),
    github_tool("hadolint", "hadolint", "hadolint/hadolint", "hadolint-linux-x86_64", "checksums.sha256", "hadolint", "2",
                "lints Dockerfiles, in every release and in this repository's CI", f"{PIPELINE}; {AT_ONCE}", paths=(TEMPLATE, CI),
                extra=((TEMPLATE, "Dockerfile lint (hadolint {version})"), (CONTRACT, "(version {version}, the same rules as"),
                       (CONTRACT, "hadolint/hadolint:v{version} < Dockerfile"), (SETUP, "hadolint {version} checks each application's Dockerfile"))),
    github_tool("pack", "pack", "buildpacks/pack", "pack-v{version}-linux.tgz", "pack-v{version}-linux.tgz.sha256",
                "pack.tgz", "0", "builds a service that has no Dockerfile with Google's buildpacks", PIPELINE),
    github_tool("cosign", "Cosign", "sigstore/cosign", "cosign-linux-amd64", "cosign_checksums.txt", "cosign", "3",
                "signs and checks build provenance on the Cloud KMS path, and every promotion to production",
                f"{PIPELINE}; the next promotion", paths=(TEMPLATE, PROMOTE)),
    github_tool("opentofu", "OpenTofu", "opentofu/opentofu", "tofu_{version}_linux_amd64.tar.gz", "tofu_{version}_SHA256SUMS",
                "tofu.tar.gz", "1", "validates every Terraform root in CI, so OpenTofu stays a drop-in", AT_ONCE, paths=(CI,),
                extra=((RUNBOOK, "with OpenTofu {version} as well as"), (HANDOFF, "then repeats it with OpenTofu {version})"))),
    Pin("caddy", "Caddy", "github", "caddyserver/caddy",
        ((STARTUP, "CADDY_VERSION={version}"), (STARTUP, "CADDY_SHA512={sha512}")),
        "serves HTTPS in front of the controller on its machine", MACHINE, line="2",
        asset="caddy_{version}_linux_amd64.tar.gz", sums="caddy_{version}_checksums.txt", algorithm="sha512"),
    Pin("litestream", "Litestream", "github", "benbjohnson/litestream",
        ((STARTUP, "LITESTREAM_VERSION={version}"), (STARTUP, "LITESTREAM_SHA256={sha256}")),
        "copies the controller's database to Cloud Storage, at most a minute behind", MACHINE, line="0.5",
        line_note="Litestream's configuration and replica format changed between its lines (0.3 to 0.5).",
        asset="litestream-{version}-linux-x86_64.tar.gz", sums="checksums.txt"),
    buildpacks("builder-22", "buildpacks builder (Ubuntu 22.04)", "buildpacks/builder:google-22", "google.22",
               "3.11|3.12)\n                BUILDER=gcr.io/buildpacks/builder@{digest}"),
    buildpacks("run-22", "buildpacks run image (Ubuntu 22.04)", "buildpacks/google-22/run:latest", "google.22",
               "RUN_IMAGE=gcr.io/buildpacks/google-22/run@{digest}"),
    buildpacks("builder-24", "buildpacks builder (Ubuntu 24.04)", "buildpacks/builder:google-24", "google.24",
               "*)\n                BUILDER=gcr.io/buildpacks/builder@{digest}"),
    buildpacks("run-24", "buildpacks run image (Ubuntu 24.04)", "buildpacks/google-24/run:latest", "google.24",
               "RUN_IMAGE=gcr.io/buildpacks/google-24/run@{digest}"),
    *(python_image(version) for version in ("3.11", "3.12", "3.13", "3.14", "3.15")),
    *(node_image(line) for line in ("22", "24", "26")),
    Pin("uv", "uv", "pypi", "uv",
        ((SCAFFOLD, 'UV_VERSION = "{version}"  # released {released}'),
         *((SCAFFOLD, f'    "{{{tag}}}",  # {tag}') for tag in ("manylinux_2_17_x86_64", "manylinux_2_17_aarch64", "manylinux_2_28_aarch64"))),
        "compiles a Python service's lock on the controller's machine (`compile_lock`, `export_uv_lock`)", CONTROLLER,
        line="0.12", line_note="uv's minor versions are its breaking ones (uv's versioning policy), and 0.13 changed archive handling.",
        notes="https://github.com/astral-sh/uv/releases/tag/{version}"),
    Pin("npm", "npm", "npm", "npm",
        ((SCAFFOLD, 'LOCK_NPM = {"22": ("{version}", "{integrity}")}  # published {released}'),
         (CONTRACT, "so npm {version} is installed there first"), (HANDOFF, "Node 22's gets npm {version} installed by version and integrity")),
        "makes a Node.js 22 service's lock on the controller's machine (Node 22's own npm cannot hold back new releases)", CONTROLLER,
        line="11", line_note="npm 12 blocks dependency scripts by default and no Node.js line bundles it yet.",
        notes="https://github.com/npm/cli/releases/tag/v{version}"),
)
BY_KEY = {pin.key: pin for pin in PINS}
# A date that says when a group of pins was resolved: written as the day of the refresh when any pin of the group changes.
STAMPS = ((SCAFFOLD, 'BASE_IMAGES_RESOLVED = "{date}"', "python-"), (SCAFFOLD, 'NODE_IMAGES_RESOLVED = "{date}"', "node-"))
# What else is pinned and stays out of the weekly refresh, and why (tests/test_pins.py keeps this list complete).
NOT_FOLLOWED = {
    "uses: ": "GitHub Actions, pinned by commit: Dependabot's",
    "us-docker.pkg.dev/cloudrun/container/hello@": "the placeholder a new service starts with; changing it plans every application root",
}


# ---- the places
def fields_of(pattern):
    return re.findall(r"\{(\w+)\}", pattern)


def field_pattern(name):
    return SHA256 if name.startswith("manylinux") else FIELDS[name]


def compiled(pattern):
    """A place's text as a regular expression: each {field} a group, the same field again the same value."""
    regex, seen = "", set()
    for index, part in enumerate(re.split(r"\{(\w+)\}", pattern)):
        if index % 2 == 0:
            regex += re.escape(part)
        elif part in seen:
            regex += f"(?P={part})"
        else:
            seen.add(part)
            regex += f"(?P<{part}>{field_pattern(part)})"
    return re.compile(regex)


def render(pattern, values):
    return "".join(values[part] if index % 2 else part for index, part in enumerate(re.split(r"\{(\w+)\}", pattern)))


def locate(text, pattern, pin, path):
    matches = list(compiled(pattern).finditer(text))
    if len(matches) != 1:
        raise PinError(f"{pin.title}: {path} should hold `{pattern}` exactly once and holds it {len(matches)} times; if the text was "
                       "reworded on purpose, change this place in PINS (labdeploy/pins.py) to match")
    return matches[0]


def read(root=ROOT):
    """The pins as the files hold them, {key: {field: value}}. Every place is found exactly once, and the places of one pin
    agree; otherwise PinError says which file and what to change."""
    texts, found = {}, {}
    for pin in PINS:
        values = {}
        for path, pattern in pin.places:
            if path not in texts:
                texts[path] = (Path(root) / path).read_text()
            for name, value in locate(texts[path], pattern, pin, path).groupdict().items():
                if values.setdefault(name, value) != value:
                    raise PinError(f"{pin.title}: {path} pins {name} {value} where another place pins {values[name]}; make them the same")
        found[pin.key] = values
    return found


def paths():
    """Every file a refresh may change: the places, the stamps and the generated deploy workflow."""
    return sorted({path for pin in PINS for path, _ in pin.places} | {path for path, _, _ in STAMPS} | {CENTRAL})


def mentions(key, path=None, values=None):
    """Each place of a pin, in one file or all, as the files hold it today (or with `values`): what a test looks for
    instead of spelling the pinned value out, so the weekly refresh never has to edit a test."""
    pin, values = BY_KEY[key], values or read()[key]
    return [render(pattern, values) for where, pattern in pin.places if path in (None, where)]


# ---- versions and time
def numbers(version):
    return tuple(int(part) for part in version.split("."))


def in_line(version, line):
    return not line or numbers(version)[:len(numbers(line))] == numbers(line)


def beyond(version, line):
    """Whether a version starts a newer line than the one followed (cosign 4 for line 3, uv 0.13 for line 0.12)."""
    return bool(line) and numbers(version)[:len(numbers(line))] > numbers(line)


def moment(text):
    return datetime.fromisoformat(text) if text else None


def day(when):
    return when.astimezone(UTC).date().isoformat() if when else "?"


def ago(when, now):
    days = (now - when).days
    return "today" if days < 1 else "1 day ago" if days == 1 else f"{days} days ago"


# ---- the official metadata
class Web:
    """Reads official metadata over HTTPS, retrying a busy server. The workflow's read-only token goes to GitHub's API
    alone (more requests an hour) and never follows a redirect; no other host sees it."""

    def __init__(self, token=None, timeout=60):
        self.token = token if token is not None else os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.timeout = timeout

    def request(self, url, accept=None, method="GET", bearer=None):
        if not url.startswith("https://"):
            raise PinError(f"only HTTPS is read: {url}")
        request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **({"Accept": accept} if accept else {})})  # noqa: S310 - HTTPS only
        bearer = bearer or (self.token if urllib.parse.urlsplit(url).hostname == "api.github.com" else None)
        if bearer:
            request.add_unredirected_header("Authorization", f"Bearer {bearer}")
        for attempt in range(3):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310 - HTTPS only, checked above
            except urllib.error.HTTPError as exc:
                if (exc.code < 500 and exc.code != 429) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)

    def get(self, url, accept=None):
        with self.request(url, accept) as response:
            return response.read()

    def json(self, url, accept=None):
        return json.loads(self.get(url, accept))

    def head(self, url, accept=None, bearer=None):
        with self.request(url, accept, "HEAD", bearer) as response:
            return {name.lower(): value for name, value in response.headers.items()}

    def hash(self, url, algorithm):
        """A download's hash, read in pieces: only for checking a pin made from metadata, never for making one."""
        digest = hashlib.new(algorithm)
        with self.request(url) as response:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
        return digest


@dataclass
class Finding:
    """What the metadata says about one pin this week."""
    pin: Pin
    current: dict
    status: str = "current"   # current, update, held (something needs a person) or unchecked (the metadata could not be read)
    new: dict = field(default_factory=dict)
    released: datetime | None = None
    was: str = ""             # the current pin, in words
    label: str = ""           # the new pin, in words
    sources: list = field(default_factory=list)    # (words, URL): the metadata the new pin was read from
    waiting: list = field(default_factory=list)    # (what, when it was released, URL): newer, but not seven days old yet
    newer: list = field(default_factory=list)      # (version, when, URL): the newest release of a newer line
    problems: list = field(default_factory=list)   # sentences
    downloads: list = field(default_factory=list)  # what `verify` checks before the pull request opens


def checksum_list(web, asset):
    """The project's checksum list, {file: hash}, after checking that it is the file GitHub recorded at upload."""
    data = web.get(asset["browser_download_url"])
    recorded = asset.get("digest") or ""
    if recorded.startswith("sha256:") and hashlib.sha256(data).hexdigest() != recorded.removeprefix("sha256:"):
        raise PinError(f"{asset['name']} is not the file GitHub recorded when it was uploaded")
    listed = {}
    for line in data.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}|[0-9a-f]{128}", parts[0]):
            listed[parts[1].lstrip("*").removeprefix("./")] = parts[0]
    return listed


def from_github(pin, current, web, now):
    """The newest release of the line, at least seven days old with its asset and the project's checksum list uploaded.
    Its SHA-256 is GitHub's record of the asset, which the project's own list must repeat (Caddy's list, SHA-512, is the pin)."""
    found = Finding(pin, current, was=current["version"])
    api = f"https://api.github.com/repos/{pin.source}/releases?per_page=100"
    eligible, pending = [], []
    for release in web.json(api, "application/vnd.github+json"):
        match = re.fullmatch(r"v(\d+\.\d+\.\d+)", release.get("tag_name") or "")
        if release.get("draft") or release.get("prerelease") or not match:
            continue
        version, published = match.group(1), moment(release.get("published_at"))
        if not in_line(version, pin.line):
            if beyond(version, pin.line) and (not found.newer or numbers(version) > numbers(found.newer[0][0])):
                found.newer = [(version, published, release.get("html_url"))]
            continue
        if numbers(version) < numbers(current["version"]):
            continue
        assets = {asset.get("name"): asset for asset in release.get("assets") or []}
        asset, sums = assets.get(pin.asset.format(version=version)), assets.get(pin.sums.format(version=version))
        if version == current["version"]:
            recorded = (asset or {}).get("digest") or ""
            if pin.algorithm == "sha256" and recorded.startswith("sha256:") and recorded.removeprefix("sha256:") != current["sha256"]:
                found.problems.append(f"the pinned SHA-256 of {asset['name']} is not the one GitHub records for it now: the asset may have "
                                      "been replaced after it was pinned; downloads fail closed until a person looks")
            continue
        ready = all(item and item.get("state") == "uploaded" for item in (asset, sums))
        ready_at = max([published] + [moment(item.get("updated_at")) for item in (asset, sums) if item and item.get("updated_at")])
        if not ready or now - ready_at < COOLDOWN:
            pending.append((numbers(version), (f"{pin.title} {version}", ready_at, release.get("html_url"))))
            continue
        eligible.append((numbers(version), version, release, asset, sums, ready_at))
    newest = max(eligible, key=lambda item: item[0]) if eligible else None
    found.waiting = [item for number, item in sorted(pending) if not newest or number > newest[0]]
    if not newest:
        return found
    _, version, release, asset, sums, ready_at = newest
    recorded = asset.get("digest") or ""
    listed = checksum_list(web, sums).get(asset["name"], "")
    if not recorded.startswith("sha256:"):
        return held(found, f"GitHub records no digest for {asset['name']} of {version}")
    if pin.algorithm == "sha256" and listed != recorded.removeprefix("sha256:"):
        return held(found, f"GitHub's digest of {asset['name']} of {version} and the project's {sums['name']} disagree")
    value = recorded.removeprefix("sha256:") if pin.algorithm == "sha256" else listed
    if not re.fullmatch(FIELDS[pin.algorithm], value):
        return held(found, f"{sums['name']} of {version} has no {pin.algorithm.upper()} for {asset['name']}")
    found.status, found.new, found.released, found.label = "update", {"version": version, pin.algorithm: value}, ready_at, version
    found.sources = [("release notes", release.get("html_url")), ("GitHub's record of the asset", asset.get("url")),
                     ("the project's checksum list", sums.get("browser_download_url"))]
    found.downloads = [{"url": asset["browser_download_url"], "algorithm": pin.algorithm, "expected": value}]
    return found


def from_dockerhub(pin, current, web, now):
    """The index the tag points to on Docker Hub, once the tag has pointed there for seven days, with linux/amd64 and
    linux/arm64 in it; its Python or Node.js release and its Debian come from the version tag that names the same index."""
    repository, tag = pin.source.split(":")
    page = f"https://hub.docker.com/_/{repository}/tags?name={tag}"
    found = Finding(pin, current, was=f"{current['release']} on Debian {current['debian']} ({current['digest'][:19]}…)")
    info = web.json(f"https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags/{tag}")
    digest, pushed = info.get("digest") or "", moment(info.get("tag_last_pushed") or info.get("last_updated"))
    if not re.fullmatch(FIELDS["digest"], digest) or pushed is None:
        return held(found, f"Docker Hub gave no index digest or date for {pin.source}")
    if digest == current["digest"]:
        return found
    if now - pushed < COOLDOWN:
        found.waiting.append((f"{pin.source}, rebuilt", pushed, page))
        return found
    platforms = {(image.get("os"), image.get("architecture")) for image in info.get("images") or []}
    missing = sorted(f"{os_}/{arch}" for os_, arch in {("linux", "amd64"), ("linux", "arm64")} - platforms)
    if missing:
        return held(found, f"the new {pin.source} index has no {' or '.join(missing)} image")
    prefix = tag.split("-")[0] + "."
    listing = web.json(f"https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags?name={prefix}&page_size=100&ordering=last_updated")
    named = next((match for result in listing.get("results") or [] if result.get("digest") == digest
                  for match in [re.fullmatch(pin.tags, result.get("name") or "")] if match and match["release"].startswith(prefix)), None)
    if not named:
        return held(found, f"Docker Hub names no {repository} release for the new {pin.source} index")
    found.status, found.released = "update", pushed
    found.new = {"digest": digest, "release": named["release"], "debian": named["debian"]}
    found.label = f"{named['release']} on Debian {named['debian']} ({digest[:19]}…)"
    found.sources = [("Docker Hub's tag", page), ("its index digest and date", f"https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags/{tag}")]
    found.downloads = [{"registry": "docker.io", "repository": f"library/{repository}", "digest": digest}]
    return found


def gcr_stack(web, repository, digest):
    """The io.buildpacks.stack.id label of an image on gcr.io (google.22 or google.24), read from its configuration."""
    manifest = web.json(f"https://gcr.io/v2/{repository}/manifests/{digest}", ", ".join(MANIFEST_TYPES + INDEX_TYPES))
    if manifest.get("manifests"):
        amd64 = next(entry for entry in manifest["manifests"] if (entry.get("platform") or {}).get("architecture") == "amd64")
        manifest = web.json(f"https://gcr.io/v2/{repository}/manifests/{amd64['digest']}", ", ".join(MANIFEST_TYPES))
    config = web.json(f"https://gcr.io/v2/{repository}/blobs/{manifest['config']['digest']}")
    return ((config.get("config") or {}).get("Labels") or {}).get("io.buildpacks.stack.id", "")


def from_gcr(pin, current, web, now):
    """Google uploads new buildpacks images about once a week, so the tag's own image is rarely seven days old. The pin is the
    newest image Google published on this line (it carries the tag, or the deprecated-public-image tag Google leaves on a
    public image it replaced) that was uploaded at least seven days ago and carries the line's stack label."""
    repository, tag = pin.source.rsplit(":", 1)
    api = f"https://gcr.io/v2/{repository}/tags/list"
    manifests = web.json(api).get("manifest") or {}
    uploaded = {digest: datetime.fromtimestamp(int(info.get("timeUploadedMs") or 0) / 1000, UTC) for digest, info in manifests.items()}

    def names(digest):
        tags = [name.removeprefix("deprecated-public-image-") for name in manifests[digest].get("tag") or []]
        return sorted({name for name in tags if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|latest|public-image-.*|google-\d+", name)})

    def words(digest):
        return ", ".join(names(digest) + [f"uploaded {day(uploaded[digest])}"]) + f" ({digest[:19]}…)"

    pinned_at = uploaded.get(current["digest"])
    found = Finding(pin, current, was=words(current["digest"]) if pinned_at else f"{current['digest'][:19]}…")
    if pinned_at is None:
        found.problems.append(f"the pinned {pin.title} is no longer listed in gcr.io/{repository}: a build that needs it fails until it moves")
    public = [digest for digest, info in manifests.items()
              if tag in (info.get("tag") or []) or any(name.startswith("deprecated-public-image-") for name in info.get("tag") or [])]
    for digest in sorted(public, key=uploaded.get, reverse=True):
        if pinned_at and uploaded[digest] <= pinned_at:
            break
        if now - uploaded[digest] < COOLDOWN:
            if tag in (manifests[digest].get("tag") or []):
                found.waiting.append((f"{pin.title}, {', '.join(names(digest)) or 'a new upload'}", uploaded[digest], f"https://gcr.io/v2/{repository}/tags/list"))
            continue
        if gcr_stack(web, repository, digest) != pin.stack:
            continue
        found.status, found.new, found.released, found.label = "update", {"digest": digest}, uploaded[digest], words(digest)
        found.sources = [("the registry's list of images, with upload times", api),
                         ("Google's builders", "https://cloud.google.com/docs/buildpacks/builders")]
        found.downloads = [{"registry": "gcr.io", "repository": repository, "digest": digest}]
        break
    return found


def from_pypi(pin, current, web, now):
    """The newest release of the line whose wheels for each pinned platform are on PyPI, not yanked, uploaded at least seven
    days ago; the hashes are the ones PyPI's index publishes (PEP 691)."""
    api = f"https://pypi.org/simple/{pin.source}/"
    found = Finding(pin, current, was=current["version"])
    wheels = [name for _, pattern in pin.places for name in fields_of(pattern) if name.startswith("manylinux")]
    files = {}
    for item in web.json(api, "application/vnd.pypi.simple.v1+json").get("files") or []:
        match = re.fullmatch(rf"{re.escape(pin.source)}-(\d+\.\d+\.\d+)-py3-none-([\w.]+)\.whl", item.get("filename") or "")
        if match:
            files.setdefault(match.group(1), []).append((set(match.group(2).split(".")), item))
    for version in sorted(files, key=numbers, reverse=True):
        uploaded = max(moment(item.get("upload-time")) for _, item in files[version])
        if not in_line(version, pin.line):
            if beyond(version, pin.line) and not found.newer:
                found.newer = [(version, uploaded, pin.notes.format(version=version))]
            continue
        if numbers(version) <= numbers(current["version"]):
            break
        chosen = {wheel: [item for platforms, item in files[version] if wheel in platforms] for wheel in wheels}
        if any(len(items) != 1 or items[0].get("yanked") or not (items[0].get("hashes") or {}).get("sha256") for items in chosen.values()):
            continue
        released = max(moment(items[0].get("upload-time")) for items in chosen.values())
        if now - released < COOLDOWN:
            found.waiting.append((f"{pin.title} {version}", released, pin.notes.format(version=version)))
            continue
        found.status, found.released, found.label = "update", released, version
        found.new = {"version": version, "released": day(released), **{wheel: items[0]["hashes"]["sha256"] for wheel, items in chosen.items()}}
        found.sources = [("release notes", pin.notes.format(version=version)), ("PyPI's files and hashes", f"https://pypi.org/project/{pin.source}/{version}/#files")]
        found.downloads = [{"url": items[0]["url"], "algorithm": "sha256", "expected": items[0]["hashes"]["sha256"]} for items in chosen.values()]
        break
    return found


def from_npm(pin, current, web, now):
    """The newest release of the line on the npm registry at least seven days old and not deprecated, asking for the same
    Node.js versions as the pinned one and running no install script; its integrity is the one the registry publishes."""
    api = f"https://registry.npmjs.org/{pin.source}"
    found = Finding(pin, current, was=current["version"])
    document = web.json(api)
    versions, times = document.get("versions") or {}, document.get("time") or {}
    engines = (versions.get(current["version"]) or {}).get("engines")
    for version in sorted((name for name in versions if re.fullmatch(r"\d+\.\d+\.\d+", name)), key=numbers, reverse=True):
        meta, published = versions[version], moment(times.get(version))
        if not in_line(version, pin.line):
            if beyond(version, pin.line) and not found.newer:
                found.newer = [(version, published, pin.notes.format(version=version))]
            continue
        if numbers(version) <= numbers(current["version"]):
            break
        if meta.get("deprecated") or published is None:
            continue
        if now - published < COOLDOWN:
            found.waiting.append((f"npm {version}", published, pin.notes.format(version=version)))
            continue
        scripts = meta.get("scripts") or {}
        if meta.get("engines") != engines:
            return held(found, f"npm {version} asks for {meta.get('engines')} where {current['version']} asked for {engines}: check it still runs on Node.js 22")
        if meta.get("hasInstallScript") or any(scripts.get(name) for name in ("preinstall", "install", "postinstall")):
            return held(found, f"npm {version} has an install script, and the lock container installs npm with scripts off")
        integrity = (meta.get("dist") or {}).get("integrity") or ""
        if not re.fullmatch(FIELDS["integrity"], integrity):
            return held(found, f"the registry publishes no SHA-512 integrity for npm {version}")
        found.status, found.released, found.label = "update", published, version
        found.new = {"version": version, "integrity": integrity, "released": day(published)}
        found.sources = [("release notes", pin.notes.format(version=version)), ("the registry's metadata", f"{api}/{version}")]
        found.downloads = [{"url": meta["dist"]["tarball"], "algorithm": "sha512", "integrity": integrity}]
        break
    return found


def held(found, why):
    found.status = "held"
    found.problems.append(why)
    return found


SOURCES = {"github": from_github, "dockerhub": from_dockerhub, "gcr": from_gcr, "pypi": from_pypi, "npm": from_npm}


def survey(current, web, now):
    """What the official metadata says about every pin this week. A source that cannot be read leaves its pin as it is."""
    findings = []
    for pin in PINS:
        try:
            findings.append(SOURCES[pin.kind](pin, current[pin.key], web, now))
        except (OSError, ValueError, KeyError, TypeError, StopIteration, PinError) as exc:  # urllib's errors are OSErrors
            findings.append(Finding(pin, current[pin.key], status="unchecked", problems=[f"its metadata could not be read: {str(exc)[:200]}"]))
    return findings


# ---- writing the new pins
def rewrite(text, pattern, values, pin, path):
    match = locate(text, pattern, pin, path)
    return text[:match.start()] + render(pattern, {**match.groupdict(), **values}) + text[match.end():]


def apply(findings, root=ROOT, today=None):
    """Write each proposed pin in every place it appears, date the groups that changed, and regenerate the central deploy
    workflow from the template. Returns the paths it changed."""
    texts, original = {}, {}

    def text(path):
        if path not in texts:
            texts[path] = original[path] = (Path(root) / path).read_text()
        return texts[path]

    updates = [found for found in findings if found.status == "update"]
    for found in updates:
        for path, pattern in found.pin.places:
            texts[path] = rewrite(text(path), pattern, found.new, found.pin, path)
    for path, pattern, prefix in STAMPS:
        if any(found.pin.key.startswith(prefix) for found in updates):
            texts[path] = rewrite(text(path), pattern, {"date": today or day(datetime.now(UTC))}, Pin("stamp", "the resolved date", "", "", (), "", ""), path)
    if TEMPLATE in texts and texts[TEMPLATE] != original[TEMPLATE]:
        text(CENTRAL)
        texts[CENTRAL] = central_deploy(texts[TEMPLATE])
    changed = sorted(path for path in texts if texts[path] != original[path])
    for path in changed:
        (Path(root) / path).write_text(texts[path])
    return changed


# ---- the pull request
def link(words, url):
    return f"[{words}]({url})" if url else words


def title(findings, today):
    updates = [found for found in findings if found.status == "update"]
    named = [f"{found.pin.title} {found.label.split(' (')[0]}" if found.pin.kind in ("github", "pypi", "npm") else found.pin.title for found in updates]
    shown = ", ".join(named[:4]) + (f" and {len(named) - 4} more" if len(named) > 4 else "")
    return f"Weekly pin refresh, {today}: {shown}" if updates else f"Weekly pin refresh, {today}: nothing to change"


def report(findings, now, run_url=""):
    """The pull request's text: each change with its source, what waits, what needs a person, and what to review."""
    updates = [found for found in findings if found.status == "update"]
    lines = ["Once a week this job checks the pins Dependabot does not follow (`labdeploy/pins.py` lists each one, with its official "
             "source and every place it is written) and proposes those with a newer release at least seven days old.",
             "",
             "Every checksum here comes from official release metadata: for a download from GitHub, the SHA-256 GitHub recorded when "
             "the file was uploaded, which the project's own checksum list had to repeat; for an image, its registry's index digest; "
             "for uv and npm, the hashes PyPI and the npm registry publish. The job never hashed a download to make a pin. Before this "
             "pull request opened it downloaded each new file and checked it against its new pin, without running it. Nothing merges "
             "by itself: let CI finish, review, and merge it yourself.",
             ""]
    if updates:
        lines += [f"## What changes ({len(updates)})", "", "| Pin | What it does | From | To | Released | Sources |", "|---|---|---|---|---|---|"]
        for found in updates:
            sources = " · ".join(link(words, url) for words, url in found.sources)
            lines.append(f"| {found.pin.title} | {found.pin.used} | {found.was} | {found.label} | {day(found.released)} ({ago(found.released, now)}) | {sources} |")
        lines.append("")
    else:
        lines += ["## Nothing to change this week", "", "Every pin is current, or its newer release is younger than seven days.", ""]
    waiting = [(what, when, url) for found in findings for what, when, url in found.waiting]
    if waiting:
        lines += ["## Waiting until they are seven days old", ""]
        lines += [f"- {link(what, url)} on {day(when)}: can be proposed from {day(when + COOLDOWN)}." for what, when, url in sorted(waiting, key=lambda item: item[1])]
        lines.append("")
    newer = [(found, version, when, url) for found in findings for version, when, url in found.newer]
    if newer:
        lines += ["## Newer lines, not proposed", "",
                  "Each of these starts a line newer than the one the platform follows. A new line can change how the tool behaves, so "
                  "moving to it is a deliberate change: set its `line` in `PINS` (`labdeploy/pins.py`) in a reviewed pull request, and "
                  "the next run proposes its newest release.", ""]
        lines += [f"- {link(f'{found.pin.title} {version}', url)} ({day(when)}); the platform follows {found.pin.line}."
                  + (f" {found.pin.line_note}" if found.pin.line_note else "") for found, version, when, url in newer]
        lines.append("")
    problems = [(found, problem) for found in findings for problem in found.problems]
    if problems:
        lines += ["## Needs a look", ""]
        lines += [f"- **{found.pin.title}**: {problem}" + ("" if found.status == "update" else " (its pin stays as it is)") for found, problem in problems]
        lines.append("")
    if updates:
        tools = [found.pin.title for found in updates if found.pin.key in ("trufflehog", "trivy", "osv-scanner", "hadolint", "pack", "cosign")]
        reaches = sorted({(found.pin.title, found.pin.reaches) for found in updates}, key=lambda item: item[1])
        lines += ["## What to review", ""]
        if tools:
            lines.append(f"1. The release notes of {', '.join(tools)}, linked above: look for changed command-line options and changed "
                         "output. The pipeline reads TruffleHog's, Trivy's, OSV-Scanner's and hadolint's JSON output; a changed field "
                         "stops a release or hides a finding.")
        lines += [f"{2 if tools else 1}. CI runs the whole test suite on this pull request: every place a pin is written must agree with "
                  "the others, and `.github/workflows/deploy.yml` must equal its template. CI does not run the new tools or images on a "
                  "real release; the first release after the merge does, so look at its run.",
                  f"{3 if tools else 2}. After you merge, each change arrives at:"]
        grouped = {}
        for name, where in reaches:
            grouped.setdefault(where, []).append(name)
        lines += [f"   - {', '.join(names)}: {where}." for where, names in grouped.items()]
        lines += [f"{4 if tools else 3}. To undo, revert this pull request: the previous pins come back.", ""]
    lines.append("Made by `.github/workflows/pins.yml`" + (f" ([this run]({run_url}))" if run_url else "")
                 + ". If this pull request is still open next week, the next run replaces it; push nothing to its branch.")
    return "\n".join(lines) + "\n"


def plan(findings, today):
    """What `verify` checks and what the workflow commits: the title, the commit message and every change with its downloads."""
    updates = [found for found in findings if found.status == "update"]
    message = title(findings, today) + "\n\n" + "\n".join(f"- {found.pin.title}: {found.was} -> {found.label}" for found in updates) + "\n"
    return {"date": today, "title": title(findings, today), "message": message,
            "changes": [{"key": found.pin.key, "title": found.pin.title, "from": found.current, "to": found.new,
                         "released": found.released.isoformat() if found.released else None, "downloads": found.downloads} for found in updates]}


# ---- checking the downloads against the new pins
def registry_has(web, item):
    """Whether the registry serves this digest in this repository: a HEAD request, which Docker Hub does not count as a pull."""
    host = "registry-1.docker.io" if item["registry"] == "docker.io" else item["registry"]
    bearer = None
    if item["registry"] == "docker.io":
        bearer = web.json(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{item['repository']}:pull")["token"]
    headers = web.head(f"https://{host}/v2/{item['repository']}/manifests/{item['digest']}", ", ".join(INDEX_TYPES + MANIFEST_TYPES), bearer)
    return headers.get("docker-content-digest") == item["digest"]


def verify(planned, web):
    """Each new download checked against its new pin, and each new image digest found in its registry: [(what, passed)]."""
    results = []
    for change in planned["changes"]:
        for item in change["downloads"]:
            try:
                if "registry" in item:
                    results.append((f"{change['title']}: {item['registry']}/{item['repository']}@{item['digest'][:19]}…", registry_has(web, item)))
                elif "integrity" in item:
                    digest = web.hash(item["url"], "sha512")
                    results.append((f"{change['title']}: {item['url']}", "sha512-" + base64.b64encode(digest.digest()).decode() == item["integrity"]))
                else:
                    results.append((f"{change['title']}: {item['url']}", web.hash(item["url"], item["algorithm"]).hexdigest() == item["expected"]))
            except (OSError, ValueError, KeyError) as exc:
                results.append((f"{change['title']}: {item.get('url') or item.get('digest')} ({str(exc)[:120]})", False))
    return results


# ---- the command line
def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as file:
            file.write(text if text.endswith("\n") else text + "\n")


def main(argv=None, web=None, now=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.pins", description="The weekly pin refresh (docs/RUNBOOK.md).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the pins as the files hold them")
    commands.add_parser("paths", help="the files a refresh may change")
    commands.add_parser("check", help="what a refresh would propose today; writes nothing")
    refresh = commands.add_parser("refresh", help="write the new pins, regenerate deploy.yml, and write the pull request's text")
    refresh.add_argument("--report", help="the pull request's text (Markdown)")
    refresh.add_argument("--plan", help="the changes, for verify and the commit (JSON)")
    check = commands.add_parser("verify", help="download each new file and check it against its new pin")
    check.add_argument("plan")
    for command in commands.choices.values():
        command.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "paths":
        print("\n".join(paths()))
        return 0
    web, now = web or Web(), now or datetime.now(UTC)
    if args.command == "verify":
        results = verify(json.loads(Path(args.plan).read_text()), web)
        for what, passed in results:
            print(("matches its new pin: " if passed else "DOES NOT MATCH its new pin: ") + what)
        failed = [what for what, passed in results if not passed]
        summary(f"Downloads checked against their new pins: {len(results) - len(failed)} of {len(results)} match")
        if failed:
            print(f"::error title=Weekly pin refresh::{len(failed)} download(s) do not match the pin read from their metadata; nothing is proposed", file=sys.stderr)
            return 1
        return 0
    try:
        current = read(args.root)
    except (PinError, OSError) as exc:
        print(f"::error title=Weekly pin refresh::{exc}", file=sys.stderr)
        return 2
    if args.command == "list":
        for pin in PINS:
            print(f"{pin.key}: " + ", ".join(f"{name} {value}" for name, value in current[pin.key].items()))
        return 0
    findings = survey(current, web, now)
    today = day(now)
    run = os.environ.get("GITHUB_RUN_ID")
    text = report(findings, now, f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run}" if run else "")
    if args.command == "check":
        print(text)
        return 0
    changed = apply(findings, args.root, today)
    if args.report:
        Path(args.report).write_text(text)
    if args.plan:
        Path(args.plan).write_text(json.dumps(plan(findings, today), indent=2) + "\n")
    summary(text)
    print(f"{sum(found.status == 'update' for found in findings)} pin(s) to change in {len(changed)} file(s): {', '.join(changed) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
