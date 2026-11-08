"""The only module that runs a command, and only these: the gcloud CLI, the Azure CLI and Cosign, each with a fixed
argument list built here from validated values, never a shell string. Everything else in the package calls these
functions, so tests replace one runner."""
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

COMMANDS = ("gcloud", "cosign", "az")
# What the Azure CLI keeps of this service's environment: the system's basics and the host's managed identity endpoint,
# never the service's own settings (its cloudinfra tokens); its sign-in lives in a directory of the environment's own, and
# it installs no extension on its own (the image carries the pinned ssh and bastion extensions)
AZ_KEEPS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "MSI_ENDPOINT", "MSI_SECRET")
# gcloud registers its SSH key in the service account's OS Login profile with this expiry, and registers it again when it
# has expired: a copy of the key (the data volume, a backup) is worth nothing minutes after the last session.
KEY_LIFETIME = "15m"


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


def az_environment(config_dir: str, base: dict[str, str] | None = None) -> dict[str, str]:
    base = os.environ if base is None else base
    found = {name: base[name] for name in AZ_KEEPS if name in base}
    found.update({"AZURE_CONFIG_DIR": config_dir, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no", "AZURE_CORE_COLLECT_TELEMETRY": "false",
                  "AZURE_CORE_NO_COLOR": "true", "AZURE_CORE_ONLY_SHOW_ERRORS": "true"})
    return found


def az_login_identity(client_id: str, config_dir: str, runner: Runner = run, base_env: dict[str, str] | None = None) -> Completed:
    """Sign the Azure CLI in, in the environment's own directory, as the managed identity attached to this host."""
    return runner(["az", "login", "--identity", "--client-id", client_id, "--allow-no-subscriptions", "--output", "none"], None,
                  az_environment(config_dir, base_env), 120)


def az_bastion_ssh(bastion: tuple[str, str, str], machine_id: str, stdin: str, config_dir: str, runner: Runner = run, timeout: int = 600,
                   base_env: dict[str, str] | None = None) -> Completed:
    """Run `sudo bash -s` on an Azure machine through the operator's Bastion, signed in with Entra as the environment's managed
    identity (Virtual Machine Administrator Login gives sudo), with the host script and its inputs on standard input. The
    Bastion reaches the machine by its resource id, so the path is the platform's; the host key is accepted as gcloud's
    is through IAP."""
    subscription, group, name = bastion
    argv = ["az", "network", "bastion", "ssh", "--subscription", subscription, "--resource-group", group, "--name", name, "--target-resource-id", machine_id,
            "--auth-type", "AAD", "--", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "sudo bash -s"]
    return runner(argv, stdin, az_environment(config_dir, base_env), timeout)


def gcloud_ssh(name: str, zone: str, project: str, service_account: str, stdin: str, runner: Runner = run, timeout: int = 600) -> Completed:
    """Run `sudo bash -s` on a machine through IAP's tunnel as the environment's service account (OS Login), with the host
    script and its inputs on standard input. IAP authenticates the path to the instance, and gcloud keeps each machine's
    host key under its numeric id, so a new machine's key is accepted on first contact and a replaced machine never
    inherits an old one."""
    argv = ["gcloud", "compute", "ssh", name, "--zone", zone, "--project", project, "--tunnel-through-iap", "--impersonate-service-account", service_account,
            "--quiet", "--strict-host-key-checking=no", f"--ssh-key-expire-after={KEY_LIFETIME}", "--command", "sudo bash -s"]
    return runner(argv, stdin, None, timeout)
