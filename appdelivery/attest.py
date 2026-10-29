"""Verifying an image before it runs: an attestation of the environment's type, signed by one of its trusted signers, on
exactly this digest. Cosign does the cryptography; this module decides what counts and keeps the registry login it needs
in a directory of its own for one call."""
import base64
import json
import os
import tempfile

from . import remote
from .errors import DeliveryError, ErrorCode
from .models import ReleaseRequest
from .planner import ARTIFACT_REGISTRY_SUFFIX


def docker_config(directory: str, registry: str, token: str | None) -> str:
    """A Docker config.json in `directory` that logs in to an Artifact Registry host with the token; empty for any other host."""
    auths = {registry: {"auth": base64.b64encode(f"oauth2accesstoken:{token}".encode()).decode()}} if token and registry.endswith(ARTIFACT_REGISTRY_SUFFIX) else {}
    path = os.path.join(directory, "config.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"auths": auths}, handle)
    return directory


def verify(release: ReleaseRequest, environment, token: str | None, runner=remote.run) -> dict:
    """{'signer': …} for the first trusted signer whose attestation verifies, or ATTESTATION_FAILED naming what each said."""
    if not environment.trusted_signers:
        raise DeliveryError(ErrorCode.ATTESTATION_FAILED, f"Environment {release.environment} trusts no signer")
    said = []
    with tempfile.TemporaryDirectory(prefix="appdelivery-cosign-") as directory:
        config = docker_config(directory, release.registry(), token)
        for signer in environment.trusted_signers:
            # Cosign reads a gcpkms:// key with this host's own Google credentials, so it keeps the host's environment
            done = remote.cosign_verify_attestation(release.image, signer, environment.attestation_type, config, runner, dict(os.environ))
            name = signer if isinstance(signer, str) else f"{signer.identity} ({signer.issuer})"
            if done.returncode == 0:
                return {"signer": name, "type": environment.attestation_type}
            said.append({"signer": name, "said": (done.stderr or done.stdout).strip()[-300:]})
    raise DeliveryError(ErrorCode.ATTESTATION_FAILED, f"No trusted signer of {release.environment} has a {environment.attestation_type} attestation for this image",
                        {"image": release.image, "tried": said})
