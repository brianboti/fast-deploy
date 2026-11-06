"""The machine session on Azure: the same host script, sent through the operator's Bastion with Entra sign-in as the
environment's managed identity (docs/research/RESEARCH-2026-11-03-azure.md, section 7.1). The team grants that identity
admin on its request, which cloudinfra makes Virtual Machine Administrator Login (sudo) on each machine and Reader on its
interface; the operator grants it Reader on the Bastion once.

What only a real run proves, and is written here as the documents describe it: that a managed identity signs in through
`az network bastion ssh --auth-type AAD` as a service principal does, that the CLI passes standard input through, and how
long a new login role takes to reach a machine (minutes, as IAP's grant on Google)."""
import os
import re
import time

from . import remote
from .azure import REGISTRY_USER
from .errors import DeliveryError, ErrorCode
from .infra import AZURE_VM
from .machine import SSH_FAILED, Machine, MachineSession, parse

# Refusals that pass by themselves: (what ssh or the CLI says, how long to keep trying, pause, what it means)
SIGN_IN_NOT_YET = ("Permission denied", 600, 30, "the machine does not let this environment's managed identity sign in yet: a login role takes minutes "
                   "to reach it, and the machine needs the Entra sign-in extension cloudinfra installs")
NOT_LISTENING = ("Connection refused", 120, 10, "nothing answers on port 22: the machine is starting or stopped")
NOT_ANSWERING = ("Connection timed out", 120, 10, "nothing answers on port 22: the machine is starting or stopped")
NOT_READY = ("Connection closed by", 120, 10, "the machine closed the session: it is starting")


class BastionSession(MachineSession):
    TRANSIENT = (SIGN_IN_NOT_YET, NOT_LISTENING, NOT_ANSWERING, NOT_READY)
    REGISTRY_USER = REGISTRY_USER

    def __init__(self, bastion: tuple[str, str, str], client_id: str, config_dir: str, runner=remote.run, sleep=time.sleep, clock=time.monotonic):
        self.bastion, self.client_id, self.config_dir = bastion, client_id, config_dir
        self.runner, self.sleep, self.clock = runner, sleep, clock
        self.signed_in = False

    def _sign_in(self) -> None:
        """The CLI signed in as the environment's managed identity, in a directory of the environment's own (once a session)."""
        if self.signed_in:
            return
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        done = remote.az_login_identity(self.client_id, self.config_dir, self.runner)
        if done.returncode != 0:
            raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"The Azure CLI could not sign in as the managed identity {self.client_id}: attach it to the host "
                                "the service runs on", {"client_id": self.client_id, "said": (done.stderr or done.stdout).strip()[-300:]})
        self.signed_in = True

    def _connect(self, machine: Machine, stdin: str, timeout: int) -> remote.Completed:
        if not re.fullmatch(AZURE_VM, machine.provider_id or ""):
            raise DeliveryError(ErrorCode.MACHINE_UNREACHABLE, f"The infrastructure layer reports no Azure resource id for {machine.name}, which the Bastion "
                                "reaches it by", {"machine": machine.name})
        self._sign_in()
        done = remote.az_bastion_ssh(self.bastion, machine.provider_id, stdin, self.config_dir, self.runner, timeout)
        # The CLI's own refusals (the Bastion, a missing extension, a token) end with its status 1 and an ERROR: line, which is
        # a session that did not happen, not a script that failed
        if done.returncode not in (0, SSH_FAILED) and parse(done.stdout) is None and any(line.startswith("ERROR:") for line in done.stderr.splitlines()):
            return remote.Completed(SSH_FAILED, done.stdout, done.stderr)
        return done
