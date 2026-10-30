"""The platform's own images are built the way a team's are (cloudbuild.yaml; docs/DELIVERY-SPEC.md section 3): in the
trusted project, by Cloud Build, pushed by Cloud Build itself so that it records signed provenance, with nothing that
could push from inside a step, and a local submit uploads only what the Dockerfiles copy."""
import re
from pathlib import Path

import yaml

from appdelivery.config import BuildSettings

ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text())
PINNED = re.compile(r"^gcr\.io/cloud-builders/docker@sha256:[a-f0-9]{64}$")


def test_it_builds_both_images_and_cloud_build_pushes_them():
    tags = {f"${{_REGISTRY}}/{name}:${{BUILD_ID}}" for name in ("cloudinfra", "appdelivery")}
    assert set(CONFIG["images"]) == tags, "pushed through images, the only push Cloud Build records provenance for"
    built = {step["args"][step["args"].index("-t") + 1]: step["args"][step["args"].index("-f") + 1] for step in CONFIG["steps"]}
    assert built == {"${_REGISTRY}/cloudinfra:${BUILD_ID}": "Dockerfile.cloudinfra", "${_REGISTRY}/appdelivery:${BUILD_ID}": "Dockerfile.appdelivery"}
    for step in CONFIG["steps"]:
        assert step["args"][0] == "build" and "push" not in step["args"] and "entrypoint" not in step and "script" not in step, step["id"]


def test_provenance_is_required_and_logs_stay_in_cloud_logging():
    assert CONFIG["options"]["requestedVerifyOption"] == "VERIFIED"
    assert CONFIG["options"]["logging"] == "CLOUD_LOGGING_ONLY", "a user-specified build account cannot write to Google's log bucket"
    assert CONFIG["options"]["substitutionOption"] == "MUST_MATCH", "a missing _REGISTRY fails the build instead of pushing somewhere else"


def test_the_builder_is_pinned_by_digest_and_is_the_delivery_services_default():
    names = {step["name"] for step in CONFIG["steps"]}
    assert len(names) == 1 and PINNED.fullmatch(names.pop()) and BuildSettings.model_fields["builder"].default in {step["name"] for step in CONFIG["steps"]}


def test_only_the_delivery_image_reads_the_architecture():
    """Cloud Build's docker step has no BuildKit (found 2026-10-29), so the Dockerfile that downloads per-architecture binaries is told amd64."""
    steps = {step["id"]: step["args"] for step in CONFIG["steps"]}
    assert "TARGETARCH=amd64" in steps["appdelivery"] and "TARGETARCH=amd64" not in steps["cloudinfra"]
    assert "ARG TARGETARCH" in (ROOT / "Dockerfile.appdelivery").read_text() and "TARGETARCH" not in (ROOT / "Dockerfile.cloudinfra").read_text()


def test_a_local_submit_uploads_only_what_the_dockerfiles_copy():
    lines = [line for line in (ROOT / ".gcloudignore").read_text().splitlines() if line and not line.startswith("#")]
    assert lines[0] == "*", "everything is ignored first"
    kept = {line[1:].rstrip("*").rstrip("/") for line in lines if line.startswith("!")}
    assert kept == {"Dockerfile.cloudinfra", "Dockerfile.appdelivery", "requirements.lock", "cloudinfra", "appdelivery"}
    copied = set()
    for dockerfile in ("Dockerfile.cloudinfra", "Dockerfile.appdelivery"):
        copied |= {line.split()[1] for line in (ROOT / dockerfile).read_text().splitlines() if line.startswith("COPY ")}
    assert copied | {"Dockerfile.cloudinfra", "Dockerfile.appdelivery"} == kept
