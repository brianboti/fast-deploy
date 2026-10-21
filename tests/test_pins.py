"""The weekly pin refresh (labdeploy/pins.py, .github/workflows/pins.yml). No test touches the network: the official
metadata comes from FakeWeb, shaped like GitHub's release and git refs APIs, Docker Hub's tag API, gcr.io's image list,
PyPI's index and the npm registry as they answered on 2026-10-19. Its releases carry versions no real one will reach for
years (minor 900 and above), so that a refresh never turns a fake release here into the spelling of a real pin."""
import base64
import copy
import hashlib
import importlib.util
import json
import re
import shutil
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from labdeploy import infra, node, pins, scaffold
from labdeploy.workflows import central_deploy

NOW = datetime(2026, 10, 26, 5, 23, tzinfo=UTC)


def later(version):
    """A version after the pinned one that no real release will reach soon: what a test writes as "the new release"."""
    parts = version.split(".")
    return ".".join(parts[:-1] + [str(int(parts[-1]) + 900)])


def iso(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class FakeWeb:
    """Official metadata by URL: JSON documents, bytes for downloads, headers for HEAD requests, or the error a URL answers."""

    def __init__(self):
        self.routes, self.seen = {}, []

    def json(self, url, accept=None):
        self.seen.append(url)
        if url not in self.routes:
            raise urllib.error.URLError(f"no route for {url}")
        if isinstance(self.routes[url], Exception):
            raise self.routes[url]
        return copy.deepcopy(self.routes[url])

    def get(self, url, accept=None):
        self.seen.append(url)
        return self.routes[url]

    def head(self, url, accept=None, bearer=None):
        self.seen.append(("HEAD", url, bearer))
        return self.routes[("HEAD", url)]

    def hash(self, url, algorithm):
        self.seen.append(url)
        return hashlib.new(algorithm, self.routes[url])


def github_release(web, pin, version, days, *, listed=None, digest=True, asset_days=None, draft=False, prerelease=False, uploaded=True):
    """A release of `pin` as GitHub's API lists it, its file and the project's checksum list routed in `web`."""
    name, sums = pin.asset.format(version=version), pin.sums.format(version=version)
    base = f"https://github.com/{pin.source}/releases/download/v{version}"
    content = f"{pin.key} {version}".encode()
    listing = f"{listed or hashlib.new(pin.algorithm, content).hexdigest()}  {name}\n{'0' * 64} *other-file.tar.gz\n".encode()
    web.routes[f"{base}/{name}"], web.routes[f"{base}/{sums}"] = content, listing

    def asset(file, data, age):
        return {"name": file, "state": "uploaded" if uploaded or file == sums else "open", "updated_at": iso(age), "browser_download_url": f"{base}/{file}",
                "url": f"https://api.github.com/repos/{pin.source}/releases/assets/{abs(hash(file)) % 10_000}",
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}" if digest or file == sums else None}

    return {"tag_name": f"v{version}", "draft": draft, "prerelease": prerelease, "published_at": iso(days),
            "html_url": f"https://github.com/{pin.source}/releases/tag/v{version}",
            "assets": [asset(name, content, days if asset_days is None else asset_days), asset(sums, listing, days)]}


def route_releases(web, pin, releases):
    web.routes[f"https://api.github.com/repos/{pin.source}/releases?per_page=100"] = releases


def action_release(web, pin, version, days, commit, *, annotated=False, draft=False, prerelease=False, immutable=False):
    """A release of an action as GitHub's API lists it, its tag routed to `commit` in the git refs API (through a tag object
    when the tag is annotated)."""
    ref = f"https://api.github.com/repos/{pin.source}/git/ref/tags/v{version}"
    if annotated:
        tag = hashlib.sha256(f"{pin.source} v{version}".encode()).hexdigest()[:40]
        web.routes[ref] = {"ref": f"refs/tags/v{version}", "object": {"type": "tag", "sha": tag}}
        web.routes[f"https://api.github.com/repos/{pin.source}/git/tags/{tag}"] = {"tag": f"v{version}", "object": {"type": "commit", "sha": commit}}
    else:
        web.routes[ref] = {"ref": f"refs/tags/v{version}", "object": {"type": "commit", "sha": commit}}
    return {"tag_name": f"v{version}", "draft": draft, "prerelease": prerelease, "published_at": iso(days), "immutable": immutable,
            "html_url": f"https://github.com/{pin.source}/releases/tag/v{version}"}


def not_found(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


# ---- the catalogue and the files
def test_every_pin_is_found_once_in_every_place_and_its_places_agree():
    current = pins.read()
    actions = [pin for pin in pins.PINS if pin.kind == "action"]
    assert set(current) == set(pins.BY_KEY) and len(pins.PINS) == 39 and len(actions) == 10
    for pin in pins.PINS:
        fields = {"commit", "version"} if pin.kind == "action" else {name for _, pattern in pin.places for name in pins.fields_of(pattern)}
        assert set(current[pin.key]) == fields, pin.key
        for name, value in current[pin.key].items():
            assert re.fullmatch(pins.field_pattern(name), value), (pin.key, name, value)
    # An action is written one way wherever it appears: the commit, then the release it is.
    for pin in actions:
        assert re.fullmatch(r"\d+\.\d+\.\d+", current[pin.key]["version"]) and pins.in_line(current[pin.key]["version"], pin.line), pin.key
        for path in pins.action_files():
            text = Path(path).read_text()
            assert text.count(f"{pin.source}@") == text.count(pins.mentions(pin.key)[0]), (pin.key, path)
    # The same tool in two workflows is one pin with one value; the platform's and the demo app's Dockerfiles share 3.12.
    assert [path for path, _ in pins.BY_KEY["hadolint"].places].count(".github/workflows/ci.yml") == 2
    assert {path for path, _ in pins.BY_KEY["cosign"].places} == {"templates/deploy-gcp.yml", ".github/workflows/promote.yml"}
    assert scaffold.base_image("3.12").endswith(current["python-3.12"]["digest"])
    assert scaffold.UV_VERSION == current["uv"]["version"] and scaffold.UV_HASHES == tuple(current["uv"][tag] for tag in (
        "manylinux_2_17_x86_64", "manylinux_2_17_aarch64", "manylinux_2_28_aarch64"))
    assert scaffold.LOCK_NPM["22"] == (current["npm"]["version"], current["npm"]["integrity"])


def test_the_catalogue_follows_every_supported_python_and_node_line():
    assert [pin.key for pin in pins.PINS if pin.key.startswith("python-")] == [f"python-{version}" for version in scaffold.PYTHONS]
    assert [pin.key for pin in pins.PINS if pin.key.startswith("node-")] == [f"node-{line}" for line in node.NODES]
    assert all(set(pin.source.split(":")[1:]) <= {"latest", "google-22", "google-24"} for pin in pins.PINS if pin.kind == "gcr")


def test_a_reworded_or_disagreeing_place_stops_the_refresh_with_what_to_change(tmp_path):
    for path in pins.paths():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, tmp_path / path)
    shutil.copy("Dockerfile", tmp_path / "Dockerfile")
    (tmp_path / "examples/demo-app").mkdir(parents=True, exist_ok=True)
    shutil.copy("examples/demo-app/Dockerfile", tmp_path / "examples/demo-app/Dockerfile")
    assert pins.read(tmp_path) == pins.read()
    ci = tmp_path / ".github/workflows/ci.yml"
    ci.write_text(ci.read_text().replace(pins.read()["hadolint"]["sha256"], "f" * 64))
    with pytest.raises(pins.PinError, match=r"hadolint: \.github/workflows/ci\.yml pins sha256 f{64} where another place pins"):
        pins.read(tmp_path)
    shutil.copy(".github/workflows/ci.yml", ci)
    setup = tmp_path / "docs/SETUP.md"
    setup.write_text(setup.read_text().replace("checks each application's Dockerfile", "lints each Dockerfile"))
    with pytest.raises(pins.PinError, match="docs/SETUP.md should hold .* exactly once and holds it 0 times; .*change this place in PINS"):
        pins.read(tmp_path)
    shutil.copy("docs/SETUP.md", setup)
    # An action anywhere without its release comment, at another commit, or by tag stops the refresh too.
    download, checkout = pins.mentions("actions/download-artifact")[0], pins.mentions("actions/checkout")[0]
    promote = tmp_path / ".github/workflows/promote.yml"
    promote.write_text(promote.read_text().replace(download, download.split(" # ")[0], 1))
    with pytest.raises(pins.PinError, match=r"actions/download-artifact: \.github/workflows/promote\.yml pins it 1 time\(s\) otherwise than "
                                            r"`actions/download-artifact@<commit> # vX\.Y\.Z`"):
        pins.read(tmp_path)
    shutil.copy(".github/workflows/promote.yml", promote)
    recover = tmp_path / "templates/recover-gcp.yml"
    recover.write_text(recover.read_text().replace(download, download.replace(pins.read()["actions/download-artifact"]["commit"], "e" * 40)))
    with pytest.raises(pins.PinError, match=r"actions/download-artifact: templates/recover-gcp\.yml pins commit e{40} where another place pins [0-9a-f]{40}"):
        pins.read(tmp_path)
    shutil.copy("templates/recover-gcp.yml", recover)
    drafted = tmp_path / "templates/repo/ci.yml"
    drafted.write_text(drafted.read_text().replace(checkout, "actions/checkout@v7"))
    with pytest.raises(pins.PinError, match=r"actions/checkout: templates/repo/ci\.yml pins it 1 time\(s\) otherwise"):
        pins.read(tmp_path)
    shutil.copy("templates/repo/ci.yml", drafted)
    assert pins.read(tmp_path) == pins.read()


def pinned_files():
    """The files the weekly refresh reads, and every other workflow, template and Dockerfile of the platform."""
    files = set(pins.paths()) | {"Dockerfile", "examples/demo-app/Dockerfile", "labdeploy/infra.py"}
    files |= {str(path) for pattern in (".github/workflows/*.yml", "templates/*.yml", "templates/repo/*", "infra/gcp/**/*.tf") for path in Path().glob(pattern)}
    return sorted(path for path in files if Path(path).is_file() and "/.terraform/" not in path)


def test_the_catalogue_holds_every_download_by_checksum_and_every_digest_the_platform_pins():
    """A download held to a checksum or an image pinned by digest that PINS does not list would never be refreshed: add it to
    PINS (or, with the reason, to NOT_FOLLOWED)."""
    current = pins.read()
    values = {value for found in current.values() for value in found.values()}
    values |= {value.removeprefix("sha256:") for value in values}
    rendered = {place for key in pins.BY_KEY for place in pins.mentions(key, values=current[key])}
    for path in pinned_files():
        for number, line in enumerate(Path(path).read_text().splitlines(), 1):
            if any(marker in line for marker in pins.NOT_FOLLOWED):
                continue
            for value in re.findall(r"(?<![0-9a-f])(?:[0-9a-f]{128}|[0-9a-f]{64})(?![0-9a-f])", line) + re.findall(r"sha512-[A-Za-z0-9+/]{86}==", line):
                assert value in values, f"{path}:{number} pins {value[:16]}… which labdeploy/pins.py does not follow"
            if "releases/download/" in line and "$" not in line.split("releases/download/")[1].split("/")[0]:
                assert any(place in line for place in rendered), f"{path}:{number} downloads from a release labdeploy/pins.py does not follow"
    startup = Path(pins.STARTUP).read_text()
    for variable in re.findall(r"^([A-Z]+_(?:VERSION|SHA256|SHA512))=", startup, re.M):
        assert any(pattern.startswith(variable + "=") for path, pattern in pins.BY_KEY[variable.split("_")[0].lower()].places)


def test_dependabot_leaves_the_actions_the_templates_use_to_the_weekly_refresh():
    """Dependabot reads only .github/workflows: its bump of an action a template uses would leave the template behind. So
    those actions are in PINS, moved here in every file at once, and Dependabot ignores each of them at every version;
    every other action stays Dependabot's."""
    uses = re.compile(r"(?<![\w./-])([\w.-]+/[\w./-]+)@[0-9a-f]{40}")

    def used(*patterns):
        return {name for pattern in patterns for path in Path().glob(pattern) for name in uses.findall(path.read_text())}

    followed = {pin.source for pin in pins.PINS if pin.kind == "action"}
    assert followed == used("templates/*.yml", "templates/repo/*.yml", pins.WORKFLOWS)
    config = yaml.safe_load(Path(".github/dependabot.yml").read_text())
    actions = next(entry for entry in config["updates"] if entry["package-ecosystem"] == "github-actions")
    assert actions["directory"] == "/" and actions["cooldown"] == {"default-days": 7}
    assert sorted(rule["dependency-name"] for rule in actions["ignore"]) == sorted(followed)
    assert all(set(rule) == {"dependency-name"} for rule in actions["ignore"])  # every version: no bump of them reaches .github/workflows alone
    others = used(".github/workflows/*.yml") - followed
    assert others and not others & {rule["dependency-name"] for rule in actions["ignore"]}  # Dependabot's, like hashicorp/setup-terraform


def test_no_test_spells_out_a_value_the_weekly_refresh_moves():
    """Tests read the pins through labdeploy.pins (or the constants of scaffold and scan), so a refresh never edits a test
    and CI judges its pull request with tests it did not write."""
    current = pins.read()
    spelled = {value for found in current.values() for name, value in found.items() if name not in ("release", "debian", "released", "date")}
    spelled = {value for value in spelled if not re.fullmatch(r"\d+(\.\d+)+", value)}  # a bare version can be anything in a fixture
    for pin in pins.PINS:
        version = current[pin.key].get("version")
        if version:
            spelled |= {f"{pin.title} {version}", f"v{version}/", f"{pin.key}@{version}", f"{pin.key}=={version}", f"{pin.key}:v{version}"}
        spelled |= set(pins.mentions(pin.key, values=current[pin.key]))
    for path in sorted(Path("tests").glob("*.py")):
        text = path.read_text()
        found = sorted(value for value in spelled if value in text)
        assert not found, f"{path} spells out {found[0]!r}: read it through labdeploy.pins instead"


# ---- what the metadata says
def test_a_github_release_is_proposed_at_seven_days_with_githubs_digest_repeated_by_the_checksum_list():
    pin, web = pins.BY_KEY["trufflehog"], FakeWeb()
    route_releases(web, pin, [github_release(web, pin, "3.913.0", 1, draft=True), github_release(web, pin, "9.0.0", 9),
                              github_release(web, pin, "3.912.0", 2, prerelease=True), github_release(web, pin, "3.911.0", 6.9),
                              github_release(web, pin, "3.910.0", 7), github_release(web, pin, "3.905.0", 20),
                              github_release(web, pin, "3.900.0", 46)])
    found = pins.from_github(pin, {"version": "3.900.0", "sha256": hashlib.sha256(b"trufflehog 3.900.0").hexdigest()}, web, NOW)
    content = b"trufflehog 3.910.0"
    assert found.status == "update" and found.new == {"version": "3.910.0", "sha256": hashlib.sha256(content).hexdigest()} and not found.problems
    assert found.released == NOW - timedelta(days=7) and found.was == "3.900.0" and found.label == "3.910.0"
    assert [what for what, _, _ in found.waiting] == ["TruffleHog 3.911.0"]  # 6.9 days: not yet; drafts and pre-releases never
    assert [version for version, _, _ in found.newer] == ["9.0.0"]  # a newer line is reported, never proposed
    assert [words for words, _ in found.sources] == ["release notes", "GitHub's record of the asset", "the project's checksum list"]
    assert found.downloads == [{"url": pins.release_url(pin.source, pin.asset).format(version="3.910.0"), "algorithm": "sha256",
                                "expected": hashlib.sha256(content).hexdigest()}]
    # The checksum list was read from the release, never the tool itself: nothing is hashed to make the pin.
    assert not any(str(url).endswith("linux_amd64.tar.gz") for url in web.seen)


def test_the_highest_version_wins_over_the_newest_date_and_late_uploads_restart_the_clock():
    pin, web = pins.BY_KEY["opentofu"], FakeWeb()
    route_releases(web, pin, [github_release(web, pin, "1.912.8", 5), github_release(web, pin, "1.913.1", 18), github_release(web, pin, "1.912.7", 18)])
    found = pins.from_github(pin, {"version": "1.912.6", "sha256": "a" * 64}, web, NOW)
    assert found.new["version"] == "1.913.1" and found.waiting == []  # 1.912.8 is newer by date, older by version
    route_releases(web, pin, [github_release(web, pin, "1.913.1", 18, asset_days=3), github_release(web, pin, "1.912.7", 18, uploaded=False)])
    found = pins.from_github(pin, {"version": "1.912.6", "sha256": "a" * 64}, web, NOW)
    assert found.status == "current" and [what for what, _, _ in found.waiting] == ["OpenTofu 1.912.7", "OpenTofu 1.913.1"]
    assert found.waiting[1][1] == NOW - timedelta(days=3)  # a file uploaded after the release counts from its upload


def test_sources_that_disagree_hold_the_pin_and_a_replaced_file_is_reported():
    pin, web = pins.BY_KEY["trivy"], FakeWeb()
    route_releases(web, pin, [github_release(web, pin, "0.975.0", 18, listed="b" * 64), github_release(web, pin, "0.974.0", 66)])
    current = {"version": "0.974.0", "sha256": "c" * 64}
    found = pins.from_github(pin, current, web, NOW)
    assert found.status == "held" and "GitHub's digest of trivy_0.975.0_Linux-64bit.tar.gz of 0.975.0 and the project's trivy_0.975.0_checksums.txt disagree" in found.problems
    assert any("not the one GitHub records for it now" in problem for problem in found.problems)  # 0.974.0's digest is not the pin
    route_releases(web, pin, [github_release(web, pin, "0.975.0", 18, digest=False)])
    assert "GitHub records no digest" in pins.from_github(pin, current, web, NOW).problems[0]
    route_releases(web, pin, [github_release(web, pin, "0.975.0", 18)])
    sums = pins.release_url(pin.source, pin.sums).format(version="0.975.0")
    web.routes[sums] = web.routes[sums] + b"\n"  # not the file GitHub recorded at upload
    found = pins.survey({key: value for key, value in [("trivy", current)]} | {key: {} for key in pins.BY_KEY if key != "trivy"}, web, NOW)
    trivy = next(item for item in found if item.pin.key == "trivy")
    assert trivy.status == "unchecked" and "is not the file GitHub recorded" in trivy.problems[0]
    assert all(item.status == "unchecked" for item in found)  # the other sources answer nothing here: every pin stays


def test_caddy_takes_the_sha512_of_its_own_checksum_list():
    pin, web = pins.BY_KEY["caddy"], FakeWeb()
    route_releases(web, pin, [github_release(web, pin, "2.911.8", 9), github_release(web, pin, "9.0.0", 30), github_release(web, pin, "2.911.7", 16)])
    found = pins.from_github(pin, {"version": "2.911.7", "sha512": "d" * 128}, web, NOW)
    assert found.new == {"version": "2.911.8", "sha512": hashlib.sha512(b"caddy 2.911.8").hexdigest()}
    assert found.downloads[0]["algorithm"] == "sha512" and [version for version, _, _ in found.newer] == ["9.0.0"]


def hub(web, repository, tag, digest, days, archs=("amd64", "arm64"), names=()):
    web.routes[f"https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags/{tag}"] = {
        "digest": digest, "tag_last_pushed": iso(days), "images": [{"os": "linux", "architecture": arch} for arch in archs]}
    prefix = tag.split("-")[0] + "."
    web.routes[f"https://hub.docker.com/v2/namespaces/library/repositories/{repository}/tags?name={prefix}&page_size=100&ordering=last_updated"] = {
        "results": [{"name": name, "digest": digest} for name in names] + [{"name": f"{prefix}0-slim-bookworm", "digest": "sha256:" + "9" * 64}]}


def test_an_image_is_proposed_once_its_tag_has_pointed_to_it_for_seven_days():
    pin, web = pins.BY_KEY["python-3.12"], FakeWeb()
    current = {"digest": "sha256:" + "1" * 64, "release": "3.12.15", "debian": "trixie"}
    new = "sha256:" + "2" * 64
    hub(web, "python", "3.12-slim", new, 8, names=("3.12.16-slim", "3.12.16-slim-trixie"))
    found = pins.from_dockerhub(pin, current, web, NOW)
    assert found.status == "update" and found.new == {"digest": new, "release": "3.12.16", "debian": "trixie"}
    assert found.was == "3.12.15 on Debian trixie (sha256:111111111111…)" and found.label.startswith("3.12.16 on Debian trixie")
    assert found.downloads == [{"registry": "docker.io", "repository": "library/python", "digest": new}]
    hub(web, "python", "3.12-slim", new, 3, names=("3.12.16-slim-trixie",))
    found = pins.from_dockerhub(pin, current, web, NOW)
    assert found.status == "current" and found.waiting[0][0] == "python:3.12-slim, rebuilt"
    hub(web, "python", "3.12-slim", new, 8, archs=("amd64",), names=("3.12.16-slim-trixie",))
    assert pins.from_dockerhub(pin, current, web, NOW).problems == ["the new python:3.12-slim index has no linux/arm64 image"]
    hub(web, "python", "3.12-slim", new, 8)
    assert "names no python release" in pins.from_dockerhub(pin, current, web, NOW).problems[0]
    hub(web, "python", "3.12-slim", current["digest"], 1)
    assert pins.from_dockerhub(pin, current, web, NOW).status == "current"
    node24, web = pins.BY_KEY["node-24"], FakeWeb()
    hub(web, "node", "24-trixie-slim", new, 9, names=("24.23.0-trixie-slim",))
    found = pins.from_dockerhub(node24, {"digest": "sha256:" + "3" * 64, "release": "24.22.0", "debian": "trixie"}, web, NOW)
    assert node24.source == "node:24-trixie-slim" and found.new == {"digest": new, "release": "24.23.0", "debian": "trixie"}


def gcr(web, repository, images):
    """gcr.io's image list (digest: tags, days since upload, stack label), and each image's configuration."""
    web.routes[f"https://gcr.io/v2/{repository}/tags/list"] = {"manifest": {
        digest: {"tag": list(tags), "timeUploadedMs": str(int((NOW - timedelta(days=days)).timestamp() * 1000))} for digest, (tags, days, _) in images.items()}}
    for digest, (_, _, stack) in images.items():
        config = "sha256:c" + digest[8:]
        web.routes[f"https://gcr.io/v2/{repository}/manifests/{digest}"] = {"config": {"digest": config}}
        web.routes[f"https://gcr.io/v2/{repository}/blobs/{config}"] = {"config": {"Labels": {"io.buildpacks.stack.id": stack}}}


def test_buildpacks_take_the_newest_image_google_published_for_the_stack_seven_days_ago():
    pin, web = pins.BY_KEY["builder-22"], FakeWeb()
    digest = {name: "sha256:" + name * 64 for name in "abcdef"}
    images = {digest["a"]: (["google-22", "public-image-google-22"], 1.5, "google.22"), digest["b"]: (["google-24", "latest", "universal_builder_20261020_RC00"], 1.5, "google.24"),
              digest["c"]: (["deprecated-public-image-universal_builder_20261013_RC00", "universal_builder_20261013_RC00"], 8, "google.24"),
              digest["d"]: (["deprecated-public-image-" + "d" * 64], 8.01, "google.22"), digest["e"]: ([], 9, "google.22"),
              digest["f"]: (["deprecated-public-image-" + "f" * 64], 15, "google.22")}
    gcr(web, "buildpacks/builder", images)
    found = pins.from_gcr(pin, {"digest": digest["f"]}, web, NOW)
    # The tag's own image is a day and a half old; the 24.04 builder of the same week is skipped by its label; an image Google
    # never published (no public tag) is never taken.
    assert found.status == "update" and found.new == {"digest": digest["d"]} and [what for what, _, _ in found.waiting] == ["buildpacks builder (Ubuntu 22.04), a new upload"]
    assert found.label == "uploaded 2026-10-18 (sha256:dddddddddddd…)" and found.downloads == [{"registry": "gcr.io", "repository": "buildpacks/builder", "digest": digest["d"]}]
    assert pins.from_gcr(pin, {"digest": digest["d"]}, web, NOW).status == "current"
    gone = pins.from_gcr(pin, {"digest": "sha256:" + "0" * 64}, web, NOW)
    assert gone.status == "update" and "no longer listed in gcr.io/buildpacks/builder" in gone.problems[0]
    run = pins.from_gcr(pins.BY_KEY["builder-24"], {"digest": digest["f"]}, web, NOW)
    assert run.new == {"digest": digest["c"]} and run.label.startswith("universal_builder_20261013_RC00, uploaded 2026-10-18")


def pypi(web, versions):
    """PyPI's index for uv: version: (days since upload, platform tags, yanked)."""
    files = []
    for version, (days, platforms, yanked) in versions.items():
        for platform in platforms:
            name = f"uv-{version}-py3-none-{platform}.whl"
            files.append({"filename": name, "url": f"https://files.pythonhosted.org/{name}", "hashes": {"sha256": hashlib.sha256(name.encode()).hexdigest()},
                          "upload-time": iso(days), "yanked": yanked})
    files.append({"filename": "uv-0.12.930.tar.gz", "url": "https://files.pythonhosted.org/uv-0.12.930.tar.gz", "hashes": {"sha256": "e" * 64}, "upload-time": iso(1)})
    web.routes["https://pypi.org/simple/uv/"] = {"files": files}


WHEELS = ("manylinux_2_17_x86_64.manylinux2014_x86_64", "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64", "manylinux_2_28_aarch64",
          "macosx_11_0_arm64", "musllinux_1_1_x86_64", "win_amd64")


def test_uv_takes_the_newest_release_of_its_line_with_every_pinned_wheel():
    pin, web = pins.BY_KEY["uv"], FakeWeb()
    pypi(web, {"0.99.0": (10, WHEELS, False), "0.12.928": (2, WHEELS, False), "0.12.927": (9, WHEELS[1:], False), "0.12.926": (9, WHEELS, "broken"),
               "0.12.925": (12, WHEELS, False), "0.12.924": (18, WHEELS, False)})
    found = pins.from_pypi(pin, {"version": "0.12.924"}, web, NOW)
    expected = {tag: hashlib.sha256(f"uv-0.12.925-py3-none-{platform}.whl".encode()).hexdigest() for tag, platform in zip(
        ("manylinux_2_17_x86_64", "manylinux_2_17_aarch64", "manylinux_2_28_aarch64"), WHEELS, strict=False)}
    assert found.status == "update" and found.new == {"version": "0.12.925", "released": "2026-10-14", **expected}
    assert [what for what, _, _ in found.waiting] == ["uv 0.12.928"]  # 0.12.927 lacks a wheel and 0.12.926 is yanked: skipped
    assert found.newer[0][0] == "0.99.0" and "uv's minor versions are its breaking ones" in pin.line_note
    assert len(found.downloads) == 3 and all(item["algorithm"] == "sha256" for item in found.downloads)
    assert pins.from_pypi(pin, {"version": "0.12.925"}, web, NOW).status == "current"


def npm_registry(web, versions):
    """The npm registry's document for npm: version: (days since publication, changes to its metadata)."""
    document = {"versions": {}, "time": {"created": iso(4000)}}
    for version, (days, changes) in versions.items():
        meta = {"engines": {"node": "^20.17.0 || >=22.9.0"}, "scripts": {"test": "tap"},
                "dist": {"integrity": "sha512-" + base64.b64encode(hashlib.sha512(version.encode()).digest()).decode(), "tarball": f"https://registry.npmjs.org/npm/-/npm-{version}.tgz"}}
        document["versions"][version] = {**meta, **changes}
        document["time"][version] = iso(days)
    web.routes["https://registry.npmjs.org/npm"] = document


def test_npm_takes_the_newest_11_a_week_old_with_the_same_engines_and_no_install_script():
    pin, web = pins.BY_KEY["npm"], FakeWeb()
    npm_registry(web, {"99.0.0": (19, {}), "11.921.1": (4, {}), "11.921.0": (19, {}), "11.920.1": (25, {"deprecated": "use 11.921.0"}), "11.919.1": (54, {})})
    found = pins.from_npm(pin, {"version": "11.919.1"}, web, NOW)
    assert found.status == "update" and found.new["version"] == "11.921.0" and found.new["released"] == "2026-10-07"
    assert found.new["integrity"] == web.routes["https://registry.npmjs.org/npm"]["versions"]["11.921.0"]["dist"]["integrity"]
    assert [what for what, _, _ in found.waiting] == ["npm 11.921.1"] and found.newer[0][0] == "99.0.0"
    assert found.downloads == [{"url": "https://registry.npmjs.org/npm/-/npm-11.921.0.tgz", "algorithm": "sha512", "integrity": found.new["integrity"]}]
    npm_registry(web, {"11.921.0": (19, {"engines": {"node": ">=24"}}), "11.919.1": (54, {})})
    assert "check it still runs on Node.js 22" in pins.from_npm(pin, {"version": "11.919.1"}, web, NOW).problems[0]
    npm_registry(web, {"11.921.0": (19, {"scripts": {"postinstall": "node x.js"}}), "11.919.1": (54, {})})
    assert "has an install script" in pins.from_npm(pin, {"version": "11.919.1"}, web, NOW).problems[0]


def setup_node_releases():
    pin, web = pins.BY_KEY["actions/setup-node"], FakeWeb()
    route_releases(web, pin, [action_release(web, pin, "7.903.0", 1, "f" * 40, draft=True), action_release(web, pin, "9.0.0", 30, "9" * 40),
                              action_release(web, pin, "7.902.0", 3, "e" * 40, prerelease=True), action_release(web, pin, "7.901.1", 6.9, "d" * 40),
                              action_release(web, pin, "7.901.0", 8, "c" * 40, annotated=True, immutable=True), action_release(web, pin, "7.900.1", 20, "b" * 40),
                              action_release(web, pin, "7.900.0", 40, "a" * 40), {"tag_name": "v7", "draft": False, "prerelease": False, "published_at": iso(1)}])
    return pin, web


def test_an_action_takes_the_commit_its_release_tag_names_once_the_release_is_seven_days_old():
    pin, web = setup_node_releases()
    found = pins.from_action(pin, {"commit": "a" * 40, "version": "7.900.0"}, web, NOW)
    assert found.status == "update" and found.new == {"commit": "c" * 40, "version": "7.901.0"} and not found.problems
    assert found.was == "7.900.0 (aaaaaaaaaaaa)" and found.label == "7.901.0 (cccccccccccc)" and found.released == NOW - timedelta(days=8)
    # 6.9 days is not yet; a draft, a pre-release and a moving major tag (v7) never; the newer major is reported, never proposed.
    assert [what for what, _, _ in found.waiting] == ["actions/setup-node 7.901.1"] and [version for version, _, _ in found.newer] == ["9.0.0"]
    assert [words for words, _ in found.sources] == ["release notes", "its tag, locked by an immutable release", "the changes since the pinned commit"]
    assert found.sources[1][1] == "https://api.github.com/repos/actions/setup-node/git/ref/tags/v7.901.0"
    assert found.sources[2][1] == f"https://github.com/actions/setup-node/compare/{'a' * 40}...{'c' * 40}" and found.downloads == []
    # The annotated tag was followed to its commit through its tag object, and everything came from the action's own repository.
    assert any("/git/tags/" in str(url) for url in web.seen) and all(str(url).startswith("https://api.github.com/repos/actions/setup-node/") for url in web.seen)
    assert pins.from_action(pin, {"commit": "c" * 40, "version": "7.901.0"}, web, NOW).status == "current"


def test_a_moved_or_missing_tag_holds_the_action():
    pin, web = setup_node_releases()
    ref = "https://api.github.com/repos/actions/setup-node/git/ref/tags/v{}".format
    moved = pins.from_action(pin, {"commit": "0" * 40, "version": "7.900.0"}, web, NOW)
    assert moved.status == "held" and moved.new == {} and moved.waiting == [] and moved.problems == [
        "its tag v7.900.0 now names commit aaaaaaaaaaaa, not the pinned 000000000000: the tag was moved after it was pinned; nothing of this "
        "action is proposed until a person looks, and the pinned commit keeps working"]
    web.routes[ref("7.900.0")] = not_found(ref("7.900.0"))
    assert "its repository has no tag v7.900.0 any more" in pins.from_action(pin, {"commit": "a" * 40, "version": "7.900.0"}, web, NOW).problems[0]
    web.routes[ref("7.900.0")], web.routes[ref("7.901.0")] = {"object": {"type": "commit", "sha": "a" * 40}}, not_found(ref("7.901.0"))
    gone = pins.from_action(pin, {"commit": "a" * 40, "version": "7.900.0"}, web, NOW)
    assert gone.status == "held" and gone.problems == ["its release 7.901.0 names the tag v7.901.0, which its repository does not have"]
    web.routes[ref("7.901.0")] = {"object": {"type": "blob", "sha": "c" * 40}}
    with pytest.raises(pins.PinError, match="the tag v7.901.0 of actions/setup-node does not name a commit"):
        pins.from_action(pin, {"commit": "a" * 40, "version": "7.900.0"}, web, NOW)
    # Any other failure (a rate limit, say) leaves the pin as it is and says so.
    web.routes[ref("7.900.0")] = urllib.error.HTTPError(ref("7.900.0"), 403, "rate limit exceeded", {}, None)
    found = pins.survey({key: {"commit": "a" * 40, "version": "7.900.0"} if key == pin.key else {} for key in pins.BY_KEY}, web, NOW)
    node = next(item for item in found if item.pin.key == pin.key)
    assert node.status == "unchecked" and "rate limit exceeded" in node.problems[0]
    text = pins.report([moved, node], NOW)
    assert "- **actions/setup-node**: its tag v7.900.0 now names commit aaaaaaaaaaaa" in text and "(its pin stays as it is)" in text


def test_the_pull_request_shows_an_action_by_release_and_commit():
    pin, web = setup_node_releases()
    found = pins.from_action(pin, {"commit": "a" * 40, "version": "7.900.0"}, web, NOW)
    text = pins.report([found], NOW)
    assert ("| actions/setup-node | installs Node.js for a Node.js service's audit and tests, in the pipeline and in its drafted CI | 7.900.0 (aaaaaaaaaaaa) "
            "| 7.901.0 (cccccccccccc) | 2026-10-18 (8 days ago) | [release notes](https://github.com/actions/setup-node/releases/tag/v7.901.0) · ") in text
    assert "- [actions/setup-node 9.0.0](https://github.com/actions/setup-node/releases/tag/v9.0.0) (2026-09-26); the platform follows 7." in text
    assert "1. The release notes of actions/setup-node and the changes since each pinned commit, linked above" in text
    assert "2. CI runs the whole test suite" in text and "3. After you merge" in text and "4. To undo" in text
    assert "   - actions/setup-node: each of this repository's workflows at its next run (CI on the merge itself); an application's own workflows" in text
    assert pins.title([found], "2026-10-26") == "Weekly pin refresh, 2026-10-26: actions/setup-node 7.901.0"
    planned = pins.plan([found], "2026-10-26")
    assert planned["changes"] == [{"key": "actions/setup-node", "title": "actions/setup-node", "from": {"commit": "a" * 40, "version": "7.900.0"},
                                   "to": {"commit": "c" * 40, "version": "7.901.0"}, "released": (NOW - timedelta(days=8)).isoformat(), "downloads": []}]
    assert pins.verify(planned, FakeWeb()) == []  # nothing to download: the runner fetches the action by its commit
    assert "Infrastructure apply" not in text  # setup-node is not in the infrastructure pipeline


def test_the_pull_request_says_when_merging_it_applies_the_infrastructure(tmp_path):
    """An action .github/workflows/infra-apply.yml uses moves there too; that workflow runs when a push to main changes it,
    and a change to an infrastructure workflow counts for every root: the pull request must say so before anyone merges."""
    workflow = yaml.safe_load(Path(pins.INFRA_APPLY).read_text())
    assert workflow[True]["push"]["branches"] == ["main"] and pins.INFRA_APPLY in workflow[True]["push"]["paths"]
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    assert infra.roots([pins.INFRA_APPLY], tmp_path) == ["one", "two"]
    current = pins.read()
    checkout = update("actions/checkout", current, commit="1" * 40, version=later(current["actions/checkout"]["version"]))
    node = update("actions/setup-node", current, commit="2" * 40, version=later(current["actions/setup-node"]["version"]))
    text = pins.report([node, checkout], NOW)
    assert ("2. **Merging this starts Infrastructure apply for every application root**, staging and production: actions/checkout moves in "
            "`.github/workflows/infra-apply.yml`") in text and "look at its issue before you merge" in text
    assert "Infrastructure apply" not in pins.report([node], NOW)


# ---- writing, the pull request, and the check before it opens
def copy_pinned(tmp_path):
    for path in pins.paths() + ["Dockerfile", "examples/demo-app/Dockerfile"]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, tmp_path / path)


def update(key, current, age=9, **new):
    pin = pins.BY_KEY[key]
    return pins.Finding(pin, current[key], status="update", new=new, released=NOW - timedelta(days=age), was=current[key].get("version", "the old pin"),
                        label=new.get("version", "the new pin"), sources=[("release notes", f"https://example.org/{key}")])


def test_a_refresh_writes_every_place_dates_the_group_and_regenerates_the_deploy_workflow(tmp_path):
    copy_pinned(tmp_path)
    current = pins.read()
    hadolint = later(current["hadolint"]["version"])
    findings = [update("trufflehog", current, version=later(current["trufflehog"]["version"]), sha256="1" * 64), update("hadolint", current, version=hadolint, sha256="2" * 64),
                update("python-3.12", current, digest="sha256:" + "3" * 64, release=later(current["python-3.12"]["release"]), debian="trixie"),
                update("caddy", current, version=later(current["caddy"]["version"]), sha512="4" * 128),
                update("npm", current, version=later(current["npm"]["version"]), integrity="sha512-" + "A" * 86 + "==", released="2026-09-30"),
                update("uv", current, version=later(current["uv"]["version"]), released="2026-10-14", manylinux_2_17_x86_64="5" * 64, manylinux_2_17_aarch64="6" * 64,
                       manylinux_2_28_aarch64="7" * 64),
                pins.Finding(pins.BY_KEY["trivy"], current["trivy"])]
    changed = pins.apply(findings, tmp_path, "2026-10-26")
    assert changed == sorted({".github/workflows/ci.yml", ".github/workflows/deploy.yml", "Dockerfile", "docs/DEVELOPER-CONTRACT.md", "docs/SETUP.md", "PROJECT_HANDOFF.md",
                              "examples/demo-app/Dockerfile", "infra/gcp/modules/project/controller-startup.sh", "labdeploy/scaffold.py", "templates/deploy-gcp.yml",
                              "templates/repo/Dockerfile.static"})
    after = pins.read(tmp_path)
    for found in findings:
        assert after[found.pin.key] == {**current[found.pin.key], **found.new}, found.pin.key
    assert {key: value for key, value in after.items() if key not in {found.pin.key for found in findings}} == {
        key: value for key, value in current.items() if key not in {found.pin.key for found in findings}}
    scaffold_text = (tmp_path / "labdeploy/scaffold.py").read_text()
    assert 'BASE_IMAGES_RESOLVED = "2026-10-26"' in scaffold_text and f'NODE_IMAGES_RESOLVED = "{scaffold.NODE_IMAGES_RESOLVED}"' in scaffold_text
    assert '    "3.12": "sha256:' + "3" * 64 + f'",  # {later(current["python-3.12"]["release"])}, Debian trixie' in scaffold_text
    template = (tmp_path / "templates/deploy-gcp.yml").read_text()
    assert (tmp_path / ".github/workflows/deploy.yml").read_text() == central_deploy(template) and f"Dockerfile lint (hadolint {hadolint})" in template
    # Only the pinned lines moved: as many changed lines as the places written, and every other line as it was.
    moved = 0
    for path in changed:
        old, new = Path(path).read_text().splitlines(), (tmp_path / path).read_text().splitlines()
        assert len(old) == len(new), path
        moved += sum(1 for a, b in zip(old, new, strict=True) if a != b)
    # TruffleHog 2; hadolint 8 (template 3, ci.yml 2, the contract 2, setup 1); python 3.12 3 and its date 1; Caddy 2; npm 3; uv 4;
    # and deploy.yml, generated from the template, 5 (TruffleHog's 2 and hadolint's 3); Caddy's static-site template 2 (since 1.24.0).
    assert moved == 2 + 8 + 3 + 1 + 2 + 3 + 4 + 5 + 2
    assert pins.apply([pins.Finding(pin, current[pin.key]) for pin in pins.PINS], tmp_path, "2026-11-02") == []


def refreshed_generator(root):
    """labdeploy/workflows.py as a refresh left it under `root`: the generator CI runs on the pull request."""
    spec = importlib.util.spec_from_file_location("refreshed_workflows", root / pins.WORKFLOWS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_action_moves_in_every_template_and_workflow_at_once_and_deploy_yml_stays_generated(tmp_path):
    copy_pinned(tmp_path)
    current = pins.read()
    moving = {key: {"commit": digit * 40, "version": later(current[key]["version"])} for key, digit in (
        ("actions/checkout", "1"), ("actions/download-artifact", "2"), ("actions/create-github-app-token", "3"), ("google-github-actions/auth", "4"))}
    trufflehog = later(current["trufflehog"]["version"])
    findings = [update(key, current, **new) for key, new in moving.items()] + [update("trufflehog", current, version=trufflehog, sha256="5" * 64)]
    changed = pins.apply(findings, tmp_path, "2026-10-26")
    after = pins.read(tmp_path)
    assert {key: after[key] for key in moving} == moving and after["trufflehog"]["version"] == trufflehog
    assert {key: value for key, value in after.items() if key not in moving and key != "trufflehog"} == {
        key: value for key, value in current.items() if key not in moving and key != "trufflehog"}
    # Every file that used one of them changed, and only on the lines that use them (and TruffleHog's).
    old = {path: Path(path).read_text() for path in pins.action_files()}
    users = sorted(path for path, text in old.items() if any(pins.mentions(key)[0] in text for key in moving))
    assert changed == sorted(set(users) | {"templates/deploy-gcp.yml", ".github/workflows/deploy.yml"})
    for path in users:
        expected = old[path]
        for key, new in moving.items():
            expected = expected.replace(pins.mentions(key)[0], pins.action_text(pins.BY_KEY[key], new))
        for place in pins.mentions("trufflehog"):
            expected = expected.replace(place, place.replace(current["trufflehog"]["version"], trufflehog).replace(current["trufflehog"]["sha256"], "5" * 64))
        assert (tmp_path / path).read_text() == expected, path
    # deploy.yml is exactly what the refreshed labdeploy/workflows.py makes of the refreshed template, the App token step included.
    generator = refreshed_generator(tmp_path)
    assert generator.INSTALLATION_ACTION == pins.action_text(pins.BY_KEY["actions/create-github-app-token"], moving["actions/create-github-app-token"])
    assert generator.central_deploy((tmp_path / pins.TEMPLATE).read_text()) == (tmp_path / pins.CENTRAL).read_text()
    assert pins.apply([pins.Finding(pin, after[pin.key]) for pin in pins.PINS], tmp_path, "2026-11-02") == []


def finding_set():
    current = pins.read()
    trufflehog = update("trufflehog", current, version=later(current["trufflehog"]["version"]), sha256="1" * 64)
    trufflehog.sources = [("release notes", "https://github.com/trufflesecurity/trufflehog/releases/tag/vNEW"), ("GitHub's record of the asset", "https://api.github.com/a/1"),
                          ("the project's checksum list", "https://github.com/x/sums.txt")]
    trufflehog.downloads = [{"url": "https://github.com/x/trufflehog.tar.gz", "algorithm": "sha256", "expected": "1" * 64}]
    trivy = pins.Finding(pins.BY_KEY["trivy"], current["trivy"], waiting=[("Trivy 0.976.0", NOW - timedelta(days=12, hours=-1), "https://github.com/aquasecurity/trivy/releases/tag/v0.976.0")])
    uv = pins.Finding(pins.BY_KEY["uv"], current["uv"], newer=[("0.99.1", NOW - timedelta(days=12), "https://github.com/astral-sh/uv/releases/tag/0.99.1")])
    caddy = update("caddy", current, version=later(current["caddy"]["version"]), sha512="4" * 128)
    builder = pins.Finding(pins.BY_KEY["builder-22"], current["builder-22"], status="unchecked", problems=["its metadata could not be read: timed out"])
    return [trufflehog, trivy, uv, caddy, builder]


def test_the_pull_request_names_each_change_its_source_what_waits_and_what_to_review():
    text = pins.report(finding_set(), NOW, "https://github.com/o/r/actions/runs/7")
    pinned = pins.read()["trufflehog"]["version"]
    assert f"| TruffleHog | finds working keys in the checkout and the image of every release | {pinned} | {later(pinned)} | 2026-10-17 (9 days ago) |" in text
    assert "[release notes](https://github.com/trufflesecurity/trufflehog/releases/tag/vNEW) · [GitHub's record of the asset](https://api.github.com/a/1)" in text
    assert "- [Trivy 0.976.0](https://github.com/aquasecurity/trivy/releases/tag/v0.976.0) on 2026-10-14: can be proposed from 2026-10-21." in text
    assert "- [uv 0.99.1](https://github.com/astral-sh/uv/releases/tag/0.99.1) (2026-10-14); the platform follows 0.12. uv's minor versions" in text
    assert "- **buildpacks builder (Ubuntu 22.04)**: its metadata could not be read: timed out (its pin stays as it is)" in text
    assert "1. The release notes of TruffleHog, linked above" in text and "2. CI runs the whole test suite" in text and "4. To undo" in text
    assert "CI does not run the new tools, images or actions on a real release" in text and "and the changes since each pinned commit" not in text
    assert f"   - Caddy: {pins.MACHINE}; a static site at {pins.PIPELINE}." in text and "`scripts/machine.sh startup`" in pins.MACHINE
    assert "never hashed a download to make a pin" in text and "Nothing merges by itself" in text and "([this run](https://github.com/o/r/actions/runs/7))" in text
    assert "## What changes (2)" in text and text.index("## What changes") < text.index("## Waiting") < text.index("## Newer lines") < text.index("## Needs a look") < text.index("## What to review")
    planned, caddy = pins.plan(finding_set(), "2026-10-26"), pins.read()["caddy"]["version"]
    assert planned["title"] == f"Weekly pin refresh, 2026-10-26: TruffleHog {later(pinned)}, Caddy {later(caddy)}"
    assert f"- TruffleHog: {pinned} -> {later(pinned)}" in planned["message"]
    assert [change["key"] for change in planned["changes"]] == ["trufflehog", "caddy"] and planned["changes"][0]["downloads"][0]["expected"] == "1" * 64
    quiet = pins.report([pins.Finding(pin, {}) for pin in pins.PINS], NOW)
    assert "## Nothing to change this week" in quiet and "## What to review" not in quiet
    assert pins.title([], "2026-10-26") == "Weekly pin refresh, 2026-10-26: nothing to change"


def test_verify_checks_each_new_download_and_image_against_its_new_pin(tmp_path, capsys):
    web = FakeWeb()
    web.routes["https://github.com/x/tool.tar.gz"] = b"tool"
    web.routes["https://registry.npmjs.org/npm/-/npm-11.921.0.tgz"] = b"npm"
    web.routes["https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull"] = {"token": "anonymous"}
    web.routes[("HEAD", "https://registry-1.docker.io/v2/library/python/manifests/sha256:" + "2" * 64)] = {"docker-content-digest": "sha256:" + "2" * 64}
    web.routes[("HEAD", "https://gcr.io/v2/buildpacks/builder/manifests/sha256:" + "3" * 64)] = {"docker-content-digest": "sha256:" + "0" * 64}
    planned = {"changes": [
        {"title": "Tool", "downloads": [{"url": "https://github.com/x/tool.tar.gz", "algorithm": "sha256", "expected": hashlib.sha256(b"tool").hexdigest()}]},
        {"title": "npm", "downloads": [{"url": "https://registry.npmjs.org/npm/-/npm-11.921.0.tgz", "algorithm": "sha512",
                                        "integrity": "sha512-" + base64.b64encode(hashlib.sha512(b"npm").digest()).decode()}]},
        {"title": "python", "downloads": [{"registry": "docker.io", "repository": "library/python", "digest": "sha256:" + "2" * 64}]}]}
    assert all(passed for _, passed in pins.verify(planned, web))
    assert ("HEAD", "https://registry-1.docker.io/v2/library/python/manifests/sha256:" + "2" * 64, "anonymous") in web.seen  # HEAD: not counted as a pull
    planned["changes"][0]["downloads"][0]["expected"] = "f" * 64
    planned["changes"].append({"title": "builder", "downloads": [{"registry": "gcr.io", "repository": "buildpacks/builder", "digest": "sha256:" + "3" * 64}]})
    assert [passed for _, passed in pins.verify(planned, web)] == [False, True, True, False]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(planned))
    assert pins.main(["verify", str(path)], web=web) == 1
    output = capsys.readouterr()
    assert "DOES NOT MATCH its new pin: Tool: https://github.com/x/tool.tar.gz" in output.out and "nothing is proposed" in output.err


def test_the_command_line_lists_checks_and_refreshes(tmp_path, capsys, monkeypatch):
    assert pins.main(["list"]) == 0 and "cosign: version " in capsys.readouterr().out
    assert pins.main(["paths"]) == 0 and capsys.readouterr().out.split() == pins.paths()
    copy_pinned(tmp_path)
    before = {path: (tmp_path / path).read_text() for path in pins.paths()}
    monkeypatch.setattr(pins, "survey", lambda current, web, now: finding_set())
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert pins.main(["check", "--root", str(tmp_path)], web=FakeWeb(), now=NOW) == 0
    assert "## What changes (2)" in capsys.readouterr().out and before == {path: (tmp_path / path).read_text() for path in pins.paths()}
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report, planned = tmp_path / "pr.md", tmp_path / "plan.json"
    assert pins.main(["refresh", "--root", str(tmp_path), "--report", str(report), "--plan", str(planned)], web=FakeWeb(), now=NOW) == 0
    assert "2 pin(s) to change in 4 file(s)" in capsys.readouterr().out and summary.read_text() == report.read_text()
    assert json.loads(planned.read_text())["date"] == "2026-10-26" and pins.read(tmp_path)["trufflehog"]["version"] == later(pins.read()["trufflehog"]["version"])
    (tmp_path / "templates/deploy-gcp.yml").write_text("reworded")
    assert pins.main(["refresh", "--root", str(tmp_path)], web=FakeWeb(), now=NOW) == 2
    assert "TruffleHog: templates/deploy-gcp.yml should hold" in capsys.readouterr().err


def test_only_githubs_api_sees_the_token_and_only_https_is_read(monkeypatch):
    seen = []

    class Response:
        headers = {"Docker-Content-Digest": "sha256:x"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return b"{}" if size == -1 else b""

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_method(), request.unredirected_hdrs.get("Authorization"), request.headers.get("Authorization")))
        return Response()

    monkeypatch.setattr(pins.urllib.request, "urlopen", urlopen)
    web = pins.Web(token="workflow-token")
    assert web.json("https://api.github.com/repos/o/r/releases") == {} and web.json("https://pypi.org/simple/uv/") == {}
    assert web.head("https://registry-1.docker.io/v2/x/manifests/y", bearer="registry-token") == {"docker-content-digest": "sha256:x"}
    assert seen == [("https://api.github.com/repos/o/r/releases", "GET", "Bearer workflow-token", None), ("https://pypi.org/simple/uv/", "GET", None, None),
                    ("https://registry-1.docker.io/v2/x/manifests/y", "HEAD", "Bearer registry-token", None)]
    with pytest.raises(pins.PinError, match="only HTTPS"):
        web.get("http://example.org/")


# ---- the workflow
def test_the_weekly_workflow_proposes_one_pull_request_and_never_merges():
    text = Path(".github/workflows/pins.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow[True]["schedule"] == [{"cron": "23 5 * * 1"}] and "workflow_dispatch" in workflow[True]
    assert workflow["permissions"] == {} and workflow["concurrency"] == {"group": "pins", "cancel-in-progress": False}
    propose, pull_request = workflow["jobs"]["propose"], workflow["jobs"]["pull-request"]
    # The job that reads the internet holds no secret and no write token; the one that writes reads nothing from the internet.
    assert propose["if"] == "github.ref == 'refs/heads/main'" and propose["permissions"] == {"contents": "read"} and "environment" not in propose
    assert "secrets." not in yaml.safe_dump(propose)
    names = [step.get("name") or step["uses"].split("@")[0] for step in propose["steps"]]
    assert names == ["actions/checkout", "actions/setup-python", "Read the official release metadata and write the new pins",
                     "Check each new download against its new pin", "Keep the proposal for the pull request", "actions/upload-artifact"]
    assert "python -m labdeploy.pins refresh" in propose["steps"][2]["run"] and propose["steps"][2]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert propose["steps"][3]["run"].startswith("python -m labdeploy.pins verify")
    assert pull_request["needs"] == "propose" and pull_request["environment"] == "pins" and pull_request["if"] == "vars.PINS_APP_CLIENT_ID != ''"
    token = next(step for step in pull_request["steps"] if step.get("id") == "token")
    assert {name: value for name, value in token["with"].items() if name.startswith("permission-")} == {
        "permission-contents": "write", "permission-pull-requests": "write", "permission-workflows": "write"}
    script = pull_request["steps"][-1]["run"]
    assert "labdeploy.pins refresh" not in script and "curl" not in script and "python3 -m labdeploy.pins paths" in script
    assert "gh pr create" in script and "gh pr edit" in script and "--force" in script and "BRANCH=pins/weekly-refresh" in script
    for forbidden in ("gh pr merge", "--auto", "auto-merge", "automerge", "--admin"):
        assert forbidden not in text, forbidden
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")  # values reach scripts through the environment only
            if "uses" in step:
                assert re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
