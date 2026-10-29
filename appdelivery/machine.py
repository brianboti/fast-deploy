"""The machine session: one action of the host script on one machine, through IAP as the environment's service account.

The script is a file of this package, checked against the hash recorded here before it is sent, so a changed script is
a reviewed change of this file too. Its inputs go before it as IN_* assignments whose values are base64 text only, so
nothing a caller sends can become shell syntax. Its answer is the lines it prints that start with "APPDELIVERY ".

IAP refuses for a while after a grant and after a start (the spike of 2026-10-29, docs/DELIVERY-SPIKE.md): those two
refusals are retried within their windows; any other failure is reported at once.
"""
import base64
import hashlib
import re
import time
from dataclasses import dataclass, field
from importlib import resources

from . import remote
from .errors import DeliveryError, ErrorCode
from .models import ReleaseRequest

SCRIPT = "host/release.sh"
SCRIPT_SHA256 = "ef2dc2ea92f24590b023ffe64fb1df61e2fe9ef40238b3704e6c4b96ef43c285"
ACTIONS = ("deploy", "rollback", "remove", "status")
INPUTS = ("RELEASE", "IMAGE", "PORT", "HEALTH_PATH", "TIMEOUT", "REGISTRY", "TOKEN", "ENV")
RELEASE_ID = re.compile(r"^rel-[a-f0-9]{12}$")
REPORT = re.compile(r"^APPDELIVERY (.*)$")
# IAP's refusals that pass by themselves: (code in gcloud's output, how long to keep trying, pause between tries)
NOT_AUTHORIZED = ("4033", 600, 30)  # the tunnel grant has not reached IAP yet: 4 to 6 minutes after a grant
NO_BACKEND = ("4003", 120, 10)  # nothing answers on port 22 yet: about 30 seconds after a start
TRANSIENT = (NOT_AUTHORIZED, NO_BACKEND)
SSH_FAILED = 255  # ssh's own exit status; any other non-zero status is the host script's
BASE_TIMEOUT = 300  # pull, stop and start, on top of the health check's own time


@dataclass(frozen=True)
class Machine:
    name: str
    zone: str


@dataclass
class HostReport:
    result: str
    fields: dict[str, str] = field(default_factory=dict)
    attempts: int = 1

    def log(self) -> str | None:
        """The container's last log lines the script sent, when it sent any."""
        encoded = self.fields.get("log")
        if not encoded:
            return None
        try:
            return base64.b64decode(encoded).decode(errors="replace")
        except ValueError:
            return None


def script() -> str:
    text = resources.files("appdelivery").joinpath(SCRIPT).read_text()
    if hashlib.sha256(text.encode()).hexdigest() != SCRIPT_SHA256:
        raise DeliveryError(ErrorCode.INTERNAL_ERROR, f"{SCRIPT} does not match its recorded hash: it was changed without updating machine.SCRIPT_SHA256")
    return text


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def compose(action: str, inputs: dict[str, str]) -> str:
    """The text sent to `sudo bash -s`: the inputs as base64 assignments, then the script."""
    if action not in ACTIONS:
        raise ValueError(f"unknown host action {action!r}")
    unknown = sorted(set(inputs) - set(INPUTS))
    if unknown:
        raise ValueError(f"unknown host inputs {unknown}")
    lines = [f"IN_ACTION={_b64(action)}", *(f"IN_{name}={_b64(value)}" for name, value in inputs.items())]
    return "\n".join(lines) + "\n" + script()


def parse(stdout: str) -> HostReport | None:
    """The script's last report line, as result and fields; None when it printed none."""
    report = None
    for line in stdout.splitlines():
        found = REPORT.match(line.strip())
        if not found:
            continue
        fields = dict(part.split("=", 1) if "=" in part else (part, "") for part in found.group(1).split())
        report = HostReport(result=fields.pop("result", ""), fields=fields)
    return report


def _tail(text: str, limit: int = 600) -> str:
    kept = [line for line in text.splitlines() if line.strip() and not line.startswith("WARNING:") and "NumPy" not in line and "increase the performance" not in line]
    return "\n".join(kept)[-limit:]


class MachineSession:
    def __init__(self, project: str, service_account: str, runner=remote.run, sleep=time.sleep, clock=time.monotonic):
        self.project, self.service_account = project, service_account
        self.runner, self.sleep, self.clock = runner, sleep, clock

    def run(self, machine: Machine, action: str, inputs: dict[str, str], timeout: int = BASE_TIMEOUT) -> HostReport:
        stdin = compose(action, inputs)
        started, attempts = self.clock(), 0
        while True:
            attempts += 1
            done = remote.gcloud_ssh(machine.name, machine.zone, self.project, self.service_account, stdin, self.runner, timeout)
            report = parse(done.stdout)
            if done.returncode == 0 and report is not None:
                report.attempts = attempts
                return report
            if done.returncode not in (0, SSH_FAILED):
                raise DeliveryError(ErrorCode.MACHINE_ERROR, f"The host script stopped on {machine.name} with status {done.returncode}",
                                    {"machine": machine.name, "action": action, "said": _tail(done.stderr + done.stdout)})
            if done.returncode == 0:
                raise DeliveryError(ErrorCode.MACHINE_ERROR, f"The host script on {machine.name} ended without a report", {"machine": machine.name, "action": action})
            said = done.stderr + done.stdout
            transient = next((item for item in TRANSIENT if f"[{item[0]}:" in said or f"{item[0]}:" in said), None)
            if transient is not None and self.clock() - started + transient[2] <= transient[1]:
                self.sleep(transient[2])
                continue
            reason = {"4033": "IAP does not let this environment's service account through yet: a grant takes minutes to reach it",
                      "4003": "nothing answers on port 22: the machine is starting or stopped"}.get(transient[0] if transient else "", "the SSH session failed")
            raise DeliveryError(ErrorCode.MACHINE_UNREACHABLE, f"Cannot reach {machine.name}: {reason}",
                                {"machine": machine.name, "action": action, "attempts": attempts, "said": _tail(said)})

    def deploy(self, machine: Machine, release_id: str, release: ReleaseRequest, env_text: str, token: str | None) -> HostReport:
        """Pull release.image (logging in with the token when there is one), start it with env_text, check its health,
        restore the previous container on failure."""
        inputs = {"RELEASE": self._release_id(release_id), "IMAGE": release.image, "PORT": str(release.port), "HEALTH_PATH": release.health.path,
                  "TIMEOUT": str(release.health.timeout_seconds), "REGISTRY": release.registry(), "ENV": env_text}
        if token:
            inputs["TOKEN"] = token
        return self.run(machine, "deploy", inputs, BASE_TIMEOUT + release.health.timeout_seconds)

    def rollback(self, machine: Machine, release: ReleaseRequest) -> HostReport:
        """Start the previous container again and check its health on the release's port and path."""
        inputs = {"PORT": str(release.port), "HEALTH_PATH": release.health.path, "TIMEOUT": str(release.health.timeout_seconds)}
        return self.run(machine, "rollback", inputs, BASE_TIMEOUT + release.health.timeout_seconds)

    def remove(self, machine: Machine) -> HostReport:
        return self.run(machine, "remove", {})

    def status(self, machine: Machine) -> HostReport:
        return self.run(machine, "status", {})

    @staticmethod
    def _release_id(value: str) -> str:
        if not RELEASE_ID.match(value):
            raise ValueError(f"release id {value!r} is not rel-<12 hex>")
        return value
