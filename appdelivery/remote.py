"""The only module that runs a command, and only these: the gcloud CLI and Cosign, each with a fixed argument list built
here from validated values, never a shell string. Everything else in the package calls these functions, so tests replace
one runner."""
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

COMMANDS = ("gcloud", "cosign")


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str | None, dict[str, str] | None, int], Completed]


def run(argv: Sequence[str], stdin: str | None = None, env: dict[str, str] | None = None, timeout: int = 120) -> Completed:
    """Run one allowed command and return what it said. A command outside COMMANDS is a programming error."""
    if not argv or argv[0] not in COMMANDS:
        raise ValueError(f"appdelivery runs only {', '.join(COMMANDS)}")
    try:
        done = subprocess.run(list(argv), input=stdin, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"{argv[0]} did not finish in {timeout} seconds")
    except OSError as exc:
        return Completed(127, "", f"{argv[0]} could not start: {exc.strerror or type(exc).__name__}")
    return Completed(done.returncode, done.stdout, done.stderr)


def gcloud_access_token(runner: Runner = run) -> Completed:
    """This host's own access token from the gcloud CLI, for a laptop or a host without a metadata server."""
    return runner(["gcloud", "auth", "print-access-token", "--quiet"], None, None, 60)


def cosign_verify_attestation(image: str, signer, attestation_type: str, docker_config: str, runner: Runner = run, base_env: dict[str, str] | None = None) -> Completed:
    """cosign verify-attestation of image@digest against one signer: a gcpkms:// key or a public key file (--key), or a
    keyless identity and issuer. DOCKER_CONFIG points Cosign at a registry login written for this one call."""
    argv = ["cosign", "verify-attestation", "--type", attestation_type]
    if isinstance(signer, str):
        argv += ["--key", signer]
    else:
        argv += ["--certificate-identity", signer.identity, "--certificate-oidc-issuer", signer.issuer]
    argv += ["--output", "json", image]
    return runner(argv, None, {**(base_env or {}), "DOCKER_CONFIG": docker_config}, 120)


def gcloud_ssh(name: str, zone: str, project: str, service_account: str, stdin: str, runner: Runner = run, timeout: int = 600) -> Completed:
    """Run `sudo bash -s` on a machine through IAP's tunnel as the environment's service account (OS Login), with the host
    script and its inputs on standard input. IAP authenticates the path to the instance, and gcloud keeps each machine's
    host key under its numeric id, so a new machine's key is accepted on first contact and a replaced machine never
    inherits an old one."""
    argv = ["gcloud", "compute", "ssh", name, "--zone", zone, "--project", project, "--tunnel-through-iap", "--impersonate-service-account", service_account,
            "--quiet", "--strict-host-key-checking=no", "--command", "sudo bash -s"]
    return runner(argv, stdin, None, timeout)
