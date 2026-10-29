"""The delivery service's image: its own Dockerfile, every input pinned and checked, nothing of the other packages in it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "Dockerfile.appdelivery").read_text()
INSTRUCTIONS = [line for line in TEXT.splitlines() if line and not line.startswith("#") and not line.startswith(" ")]


def arg(name):
    found = re.search(rf"^ARG {name}=(\S+)$", TEXT, re.MULTILINE)
    assert found, f"ARG {name} is missing"
    return found.group(1)


def test_the_base_image_is_the_repositorys_python_pinned_by_digest():
    base = next(line for line in INSTRUCTIONS if line.startswith("FROM "))
    assert re.fullmatch(r"FROM python:3\.12-slim@sha256:[a-f0-9]{64}", base)
    other = next(line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM "))
    assert base == other, "both images start from the same reviewed Python"


def test_gcloud_comes_from_the_versioned_archive_checked_for_each_architecture():
    assert re.fullmatch(r"\d+\.\d+\.\d+", arg("GCLOUD_VERSION"))
    for name in ("GCLOUD_SHA256_AMD64", "GCLOUD_SHA256_ARM64"):
        assert re.fullmatch(r"[a-f0-9]{64}", arg(name))
    assert "https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-${GCLOUD_VERSION}-linux-${gcloud_arch}.tar.gz" in TEXT
    assert '"${gcloud_sum}  /tmp/gcloud.tar.gz" | sha256sum -c -' in TEXT and "openssh-client" in TEXT
    assert re.search(r"^ARG TARGETARCH$", TEXT, re.MULTILINE), "no default architecture: the builder's must be used"


def test_cosign_is_a_3_1_release_checked_by_its_digest_for_each_architecture():
    assert re.fullmatch(r"v3\.1\.\d+", arg("COSIGN_VERSION"))
    for name in ("COSIGN_SHA256_AMD64", "COSIGN_SHA256_ARM64"):
        assert re.fullmatch(r"[a-f0-9]{64}", arg(name))
    assert '"${cosign_sum}  /usr/local/bin/cosign" | sha256sum -c -' in TEXT and "set -eux" in TEXT


def test_dependencies_are_installed_by_hash_and_only_this_package_is_copied():
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in TEXT
    copied = [line.split()[1] for line in INSTRUCTIONS if line.startswith("COPY ")]
    assert copied == ["requirements.lock", "appdelivery"], "nothing of the infrastructure layer or the old workflow rides in this image"


def test_it_runs_as_a_user_with_a_writable_home_and_serves_the_delivery_app():
    users = [line for line in INSTRUCTIONS if line.startswith("USER ")]
    assert users == ["USER 65532:65532"]
    assert "HOME=/data" in TEXT and "chown 65532:65532 /data" in TEXT
    assert "useradd --system --uid 65532" in TEXT, "ssh, which gcloud compute ssh runs, refuses a uid without a passwd entry"
    assert INSTRUCTIONS[-1] == 'CMD ["uvicorn", "appdelivery.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8200"]'


def test_gcloud_keeps_no_file_logs_that_would_grow_for_ever():
    """Found on Google Cloud on 2026-10-29: gcloud wrote a log file per SSH call into the service's data volume."""
    assert "CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1" in TEXT
