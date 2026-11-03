"""The repository itself, held by tests (the security assessment of 2026-10-30, section 7, S3): no credential in a tracked
file, and the dependency set exactly the one docs/REQUIREMENTS.md lists, so a package arrives only on purpose."""
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Credentials by their published shapes. A PEM key needs a real body: the tests' fixtures carry four characters.
CREDENTIALS = {
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\s|\\n)*[A-Za-z0-9+/]{60,}"),
    "a service account key file": re.compile(r'"private_key"\s*:\s*"-----BEGIN'),
    "a Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "a Google access token": re.compile(r"ya29\.[0-9A-Za-z_-]{30,}"),
    "a Google OAuth client secret": re.compile(r"GOCSPX-[A-Za-z0-9_-]{24,}"),
    "a GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "an AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "a Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "an Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
}
PINNED = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^\s\\]+)", re.M)


def tracked():
    names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")  # noqa: S603, S607
    return [ROOT / name for name in names if name and (ROOT / name).is_file()]


def locked(name):
    return {(package.lower(), version) for package, version in PINNED.findall((ROOT / name).read_text())}


def documented():
    """The packages section 2 of docs/REQUIREMENTS.md lists: those the services run with, and those development adds."""
    text = (ROOT / "docs/REQUIREMENTS.md").read_text()
    section = text[text.index("## 2. Python packages"):text.index("## 3.")]
    run_part, dev_part = section.split("Development extras", 1)
    dev_part = dev_part.split("Dependency audit", 1)[0]
    row = re.compile(r"^\| `([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?` \| ([0-9][^ |]*) \|", re.M)
    listed = re.compile(r"\b([a-z][a-z0-9_.-]+) ([0-9]+(?:\.[0-9a-z]+)+)\b")
    run = {(name.lower(), version) for name, version in row.findall(run_part)}
    run |= set(listed.findall(run_part.split("Transitive packages", 1)[1]))
    dev = {(name.lower(), version) for name, version in row.findall(dev_part)}
    dev |= set(listed.findall(dev_part.split("it adds", 1)[1].split(")", 1)[0]))
    return run, dev


def test_no_tracked_file_holds_a_credential():
    found = []
    for path in tracked():
        if path.stat().st_size > 2_000_000:
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        found += [f"{path.relative_to(ROOT)}: {what}" for what, shape in CREDENTIALS.items() if shape.search(text)]
    assert not found, "credentials in tracked files: " + "; ".join(found)


def test_the_sweep_catches_each_shape_it_names():
    samples = {"a private key": "-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 2,
               "a service account key file": '{"private_key": "-----BEGIN PRIVATE KEY-----', "a Google API key": "AIza" + "Sy" + "A1b2C3d4" * 4 + "e",
               "a Google access token": "ya29." + "a0AfB_byC1" * 4, "a Google OAuth client secret": "GOCSPX-" + "Ab1_" * 7,
               "a GitHub token": "ghp_" + "A1b2C3d4E5" * 4, "an AWS access key": "AKIA" + "ABCDEFGH23456789", "a Slack token": "xoxb-" + "1234567890-abc",
               "an Anthropic API key": "sk-ant-" + "api03-" + "Ab1" * 10}
    assert samples.keys() == CREDENTIALS.keys()
    for what, sample in samples.items():
        assert CREDENTIALS[what].search(sample), what


def test_every_lock_pins_every_package_by_hash():
    for name in ("requirements.lock", "requirements-dev.lock", "audit.lock", "pip.lock"):
        entries = re.split(r"\n(?=[A-Za-z0-9_.-]+(?:\[[^\]]*\])?==)", (ROOT / name).read_text())
        packages = [entry for entry in entries if PINNED.match(entry)]
        assert packages, name
        assert all("--hash=sha256:" in entry for entry in packages), f"{name} has a package without a hash"


def test_the_direct_dependencies_are_pinned_exactly_and_locked_at_that_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for group, lock in ((project["dependencies"], "requirements.lock"), (project["optional-dependencies"]["dev"], "requirements-dev.lock")):
        for requirement in group:
            name, version = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([0-9][0-9A-Za-z.]*)", requirement).groups()
            assert (name.lower(), version) in locked(lock), f"{requirement} is not what {lock} holds"


def test_the_dependency_set_is_exactly_the_one_the_requirements_list():
    run, dev = documented()
    assert locked("requirements.lock") == run, "requirements.lock and docs/REQUIREMENTS.md section 2 differ: a package added or moved on purpose is listed there"
    assert locked("requirements-dev.lock") - locked("requirements.lock") == dev, "requirements-dev.lock adds other packages than docs/REQUIREMENTS.md section 2 lists"
