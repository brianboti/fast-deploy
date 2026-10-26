"""Independent recovery observer. Cloud writes belong exclusively to the GitHub recovery queue."""
import datetime
import json
import os
import re
import time

from fastapi import FastAPI, HTTPException
from pydantic import Field

from .cloud import GoogleToken, secret_reader
from .github import AppInstallations, GitHub, github_client
from .journal import GCSJournal
from .models import StrictModel
from .release import RECOVERABLE, TERMINAL
from .target import Target

RECOVERY_DEADLINE = 900  # seconds from the recovery request until an unfinished checkpoint is escalated


class Settings(StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    app_id: str
    key_secret: str = Field(pattern=r"^projects/[a-z0-9-]+/secrets/[A-Z][A-Z0-9_]+$")
    targets: dict[str, Target]
    observe_only: bool = True
    stale_seconds: int = Field(default=180, ge=120, le=900)


class Supervisor:
    def __init__(self, settings, github, token, clock=time.time, journal_factory=GCSJournal):
        self.settings, self.github, self.token = settings, github, token
        self.clock, self.journal_factory = clock, journal_factory

    def inspect(self, application, target):
        journal = self.journal_factory(target.recovery_bucket, target.service, self.token)
        record = journal.read()
        if not record or record.get("state") in TERMINAL:
            return "healthy"
        if record.get("target") != target.model_dump() or record.get("schema_version") != 2:
            return "manual_review: checkpoint binding or version"
        if record.get("pending_mutation") or record.get("state") not in RECOVERABLE:
            return "manual_review: unconfirmed mutation"
        try:
            heartbeat = datetime.datetime.fromisoformat(record["heartbeat_at"])
            if heartbeat.tzinfo is None:
                return "manual_review: invalid heartbeat"
            age = self.clock() - heartbeat.timestamp()
        except (KeyError, ValueError, TypeError):
            return "manual_review: missing heartbeat"
        if age < 0:
            return "manual_review: future heartbeat"
        if age < self.settings.stale_seconds:
            return "active"
        plan = record.get("plan_id", "")
        origin = (record.get("origin") or {}).get("run_url", "")
        match = re.fullmatch(r"https://github\.com/" + re.escape(self.settings.repository) + r"/actions/runs/([0-9]+)", origin)
        if not match or not re.fullmatch(r"[a-f0-9]{32}", plan):
            return "manual_review: missing release identity"
        root = f"/repos/{self.settings.repository}/actions"
        response = self.github.request("GET", f"{root}/runs/{match[1]}")
        response.raise_for_status()
        run = response.json()
        if (run.get("path") != ".github/workflows/promote.yml" or run.get("head_branch") != "main"
                or run.get("display_title") != "promote-" + plan):
            return "manual_review: GitHub run identity"
        if self.settings.observe_only:
            return "would_recover" if run.get("status") == "completed" else "would_cancel"
        if run.get("status") != "completed":
            # Cancellation is not a fence. Wait for a later observation of completion before dispatching recovery.
            response = self.github.request("POST", f"{root}/runs/{match[1]}/cancel")
            if response.status_code != 409:
                response.raise_for_status()
            return "waiting_for_runner_completion"
        # This separate prefix needs create/read permissions only. An uncertain POST is not repeated.
        claim = self.journal_factory(target.recovery_bucket, target.service, self.token)
        claim.key = f"recovery-requests/{target.service}.json"
        previous = claim.read()
        if previous and previous.get("plan_id") == plan:
            # The checkpoint is still unfinished, so the recovery has not done its work yet. A recovery run that ended, or
            # one that has not finished it in RECOVERY_DEADLINE, needs a person: say so loudly (the 503 raises the alert).
            if previous.get("run_id"):
                recovery = self.github.request("GET", f"{root}/runs/{int(previous['run_id'])}")
                recovery.raise_for_status()
                if recovery.json().get("status") == "completed":
                    return f"manual_review: recovery run ended ({recovery.json().get('conclusion')}) without finishing the checkpoint"
            try:
                requested = datetime.datetime.fromisoformat(previous["heartbeat_at"]).timestamp()
            except (KeyError, ValueError, TypeError):
                return "manual_review: recovery request has no time"
            if self.clock() - requested > RECOVERY_DEADLINE:
                return "manual_review: recovery did not complete in time"
            return "recovery_requested_or_unknown"
        claim.write({"plan_id": plan, "state": "dispatching"})
        response = self.github.request("POST", f"{root}/workflows/recover-production.yml/dispatches",
                                       json={"ref": "main", "inputs": {"application": application, "plan_id": plan}})
        response.raise_for_status()
        claim.write({"plan_id": plan, "state": "dispatched", "run_id": response.json().get("workflow_run_id")})
        return "recovery_requested"

    def tick(self):
        results = {}
        for application, target in self.settings.targets.items():
            try:
                results[application] = self.inspect(application, target)
            except Exception as error:  # one provider failure must not conceal the remaining applications
                results[application] = "unavailable:" + type(error).__name__
        return results


def create_app():
    settings = Settings.model_validate_json(os.environ["RECOVERY_SETTINGS"])
    token, client = GoogleToken(), github_client()
    credentials = AppInstallations(settings.app_id, secret_reader(settings.key_secret, token), client, home_repository=settings.repository)
    supervisor = Supervisor(settings, GitHub(client, credentials), token)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/tick")
    def tick():
        # Cloud Run IAM authenticates Cloud Scheduler; this service must never have allUsers invoker.
        results = supervisor.tick()
        print(json.dumps({"event": "recovery_supervision", "results": results}), flush=True)
        if any(value.startswith(("manual_review", "unavailable")) for value in results.values()):
            raise HTTPException(503, detail=results)
        return results

    return app
