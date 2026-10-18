"""Read-only views of the cloud for Claude: what serves, how it behaves, what it logs. Never a mutation.

The controller still holds no cloud credential of its own. On a Google Cloud machine it uses the attached service
account through the metadata server; on a laptop it asks gcloud for a short-lived token impersonating the observer
identity, which has only viewer roles. Either way the token is cached and never written anywhere.
"""
import base64
import datetime
import json
import re
import subprocess
import time

import httpx

from .telemetry import CloudMonitoring

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def log_line(entry):
    """One readable line from a Cloud Logging entry: text, JSON message, HTTP summary or proto status."""
    payload = entry.get("textPayload")
    if payload is None and isinstance(entry.get("jsonPayload"), dict):
        payload = entry["jsonPayload"].get("message") or json.dumps(entry["jsonPayload"])
    if payload is None and isinstance(entry.get("httpRequest"), dict):
        request = entry["httpRequest"]
        payload = f"{request.get('status')} {request.get('requestMethod')} {request.get('requestUrl')}"
    if payload is None and isinstance(entry.get("protoPayload"), dict):
        payload = (entry["protoPayload"].get("status") or {}).get("message")
    labels = (entry.get("resource") or {}).get("labels") or {}
    return {"at": entry.get("timestamp"), "severity": entry.get("severity"), "revision": labels.get("revision_name"), "message": str(payload or "")[:500]}


class GoogleToken:
    """An access token for Google APIs as this host itself: the attached service account through the metadata server on a
    Google Cloud machine, else the operator's own gcloud sign-in on a laptop. Kept until a minute before it ends."""

    def __init__(self, client=None, runner=None, clock=time.time):
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.runner = runner or (lambda args: subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout.strip())
        self.clock = clock
        self.cached, self.until = None, 0.0

    def __call__(self):
        if self.cached and self.clock() < self.until:
            return self.cached
        try:
            response = self.client.get(METADATA, headers={"Metadata-Flavor": "Google"}, timeout=0.5)
            if response.status_code == 200:
                data = response.json()
                self.cached, self.until = data["access_token"], self.clock() + max(60, int(data.get("expires_in", 300)) - 60)
                return self.cached
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        try:
            token = self.runner(["gcloud", "auth", "print-access-token"])
        except (subprocess.CalledProcessError, OSError, subprocess.SubprocessError):
            raise ValueError("Could not get a Google token on this host: on a laptop, sign in with gcloud auth login; on the machine, "
                             "check that it runs with its service account") from None
        self.cached, self.until = token, self.clock() + 1800
        return token


def secret_reader(name, token, client=None, ttl=3600, clock=time.time):
    """A key source for the GitHub App: the latest version of a Secret Manager secret, kept in memory for an hour and read
    again when asked (refresh=True, after GitHub refused a signature). The value never reaches a log, a file or Claude."""
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    state = {"value": None, "until": 0.0}

    def read(refresh=False):
        if state["value"] is not None and not refresh and clock() < state["until"]:
            return state["value"]
        response = client.get(f"https://secretmanager.googleapis.com/v1/{name}/versions/latest:access", headers={"Authorization": "Bearer " + token()})
        if response.status_code in (403, 404):
            raise ValueError(f"The GitHub App's key could not be read from {name} (HTTP {response.status_code}): add the key as a version of that "
                             "secret and check that this host's identity may read it (docs/SETUP.md section 4)")
        response.raise_for_status()
        state["value"], state["until"] = base64.b64decode(response.json()["payload"]["data"]).decode(), clock() + ttl
        return state["value"]

    return read


class Observer:
    def __init__(self, config, client=None, runner=None):
        self.account = config.observer.service_account
        self.cache_seconds = config.observer.cache_seconds
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.runner = runner or (lambda args: subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout.strip())
        self.cached, self.until = None, 0.0

    def token(self):
        if self.cached and time.time() < self.until:
            return self.cached
        token, lifetime = None, self.cache_seconds
        try:
            response = self.client.get(METADATA, headers={"Metadata-Flavor": "Google"}, timeout=0.5)
            if response.status_code == 200:
                data = response.json()
                token, lifetime = data["access_token"], min(lifetime, int(data.get("expires_in", lifetime)) - 60)
        except (httpx.HTTPError, ValueError, KeyError):
            token = None
        if token is None:
            try:
                token = self.runner(["gcloud", "auth", "print-access-token", "--impersonate-service-account", self.account])
            except (subprocess.CalledProcessError, OSError, subprocess.SubprocessError) as exc:
                lines = [line.strip() for line in str(getattr(exc, "stderr", "") or exc).splitlines() if line.strip()]
                detail = next((line for line in lines if line.startswith("ERROR")), lines[-1] if lines else str(exc))  # gcloud's warning line is not the reason
                raise ValueError("Could not obtain a token for the observer identity: sign in with gcloud on this machine and make sure the Terraform "
                                 f"observer identity exists with you as an impersonator ({detail[:200]})") from None
        self.cached, self.until = token, time.time() + lifetime
        return token

    def get(self, url, **params):
        response = self.client.get(url, params=params, headers={"Authorization": "Bearer " + self.token()})
        if response.status_code == 403:
            raise ValueError("The observer identity is not allowed to read this; apply the Terraform that grants it the viewer roles")
        response.raise_for_status()
        return response.json()

    def service(self, target):
        """The service as Cloud Run sees it now: readiness, traffic by revision with the plan and commit each carries, URL."""
        base = f"https://run.googleapis.com/v2/projects/{target.project_id}/locations/{target.region}/services/{target.service}"
        data = self.get(base)
        # Cloud Run v2 reports readiness as terminalCondition; "conditions" holds RoutesReady and ConfigurationsReady.
        ready = data.get("terminalCondition") or {}
        if ready.get("type") != "Ready":
            ready = next((c for c in data.get("conditions", []) if c.get("type") == "Ready"), {})
        traffic = []
        for item in data.get("trafficStatuses") or data.get("traffic") or []:
            revision = item.get("revision")
            entry = {"revision": revision, "percent": item.get("percent", 0), "tag": item.get("tag"), "url": item.get("uri")}
            if revision and re.fullmatch(r"[a-z][a-z0-9-]{1,62}", revision):
                labels = (self.get(f"{base}/revisions/{revision}").get("labels") or {})
                entry["plan_id"] = labels.get("lab-deploy-plan")
                entry["commit"] = labels.get("lab-deploy-commit")
            traffic.append(entry)
        return {"url": data.get("uri"), "ready": ready.get("state") == "CONDITION_SUCCEEDED", "ready_message": ready.get("message"),
                "ingress": data.get("ingress"), "iap_enabled": bool(data.get("iapEnabled")),
                "latest_ready_revision": (data.get("latestReadyRevision") or "").rsplit("/", 1)[-1] or None,
                "updated_at": data.get("updateTime"), "traffic": traffic}

    def needs(self, target, needs):
        """Whether each declared need exists, with the facts that matter; a missing one is reported, not raised."""
        items = []

        def probe(kind, name, url, wanted, **params):
            try:
                data = self.get(url, **params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    items.append({"need": kind, "name": name, "exists": False})
                    return
                raise
            items.append({"need": kind, "name": name, "exists": True, **{key: data.get(field) for key, field in wanted.items()}})

        project, service = target.project_id, target.service
        for name in needs.get("buckets") or []:
            bucket = f"{project}-{service}-{name}"
            probe("bucket", bucket, f"https://storage.googleapis.com/storage/v1/b/{bucket}", {"location": "location", "updated": "updated"}, fields="name,location,updated")
        for name in needs.get("topics") or []:
            probe("topic", f"{service}-{name}", f"https://pubsub.googleapis.com/v1/projects/{project}/topics/{service}-{name}", {})
            probe("subscription", f"{service}-{name}", f"https://pubsub.googleapis.com/v1/projects/{project}/subscriptions/{service}-{name}", {"ack_deadline_seconds": "ackDeadlineSeconds"})
        if needs.get("database") == "firestore":
            probe("firestore", service, f"https://firestore.googleapis.com/v1/projects/{project}/databases/{service}",
                  {"location": "locationId", "delete_protection": "deleteProtectionState"})
        if needs.get("database") == "postgres":
            # Whether it runs matters most: the spending cap stops instances by setting the activation policy to NEVER.
            try:
                data = self.get(f"https://sqladmin.googleapis.com/v1/projects/{project}/instances/{service}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                items.append({"need": "postgres", "name": service, "exists": False})
            except ValueError as exc:  # the observer may not read Cloud SQL yet; the other needs are still shown
                items.append({"need": "postgres", "name": service, "exists": None, "unreadable": str(exc)[:160]})
            else:
                settings = data.get("settings") or {}
                items.append({"need": "postgres", "name": service, "exists": True, "state": data.get("state"),
                              "running": data.get("state") == "RUNNABLE" and settings.get("activationPolicy") == "ALWAYS",
                              "tier": settings.get("tier"), "version": data.get("databaseVersion")})
        return items

    def metrics(self, target, minutes=60):
        """The last hour at a glance: requests, server errors, p95 latency, instances, memory. Missing data stays None."""
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(minutes=minutes)
        whole = minutes * 60
        service = f'resource.labels.service_name="{target.service}"'

        def total(metric, extra=None):
            values = monitoring.query(metric, "ALIGN_DELTA", "REDUCE_SUM", start, end, whole, " AND ".join(filter(None, (service, extra))))
            return sum(values) if values else 0

        def peak(metric, aligner, extra=None):
            values = monitoring.query(metric, aligner, "REDUCE_MAX", start, end, whole, " AND ".join(filter(None, (service, extra))))
            return max(values) if values else None

        def passed():
            # Google's uptime check calls the health path from outside every minute: it sees an outage the service's own
            # metrics cannot, such as requests a spend cap or the front end refuses before they reach the container.
            values = monitoring.query("monitoring.googleapis.com/uptime_check/check_passed", "ALIGN_FRACTION_TRUE", "REDUCE_MEAN", start, end, whole, service)
            return round(min(values), 3) if values else None
        return {"window_minutes": minutes, "requests": total("run.googleapis.com/request_count"),
                "uptime_checks_passed": passed(),
                "server_errors": total("run.googleapis.com/request_count", 'metric.labels.response_code_class="5xx"'),
                "request_latency_ms_p95": peak("run.googleapis.com/request_latencies", "ALIGN_PERCENTILE_95"),
                "max_active_instances": peak("run.googleapis.com/container/instance_count", "ALIGN_MAX", 'metric.labels.state="active"'),
                "memory_utilization_p95": peak("run.googleapis.com/container/memory/utilizations", "ALIGN_PERCENTILE_95")}

    def logs(self, target, minutes=30, severity="ERROR", limit=50):
        """The newest log lines of the service at or above a severity, newest first."""
        if severity not in {"DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("severity must be one of DEFAULT, DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL")
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=max(1, min(int(minutes), 1440)))).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = {"resourceNames": [f"projects/{target.project_id}"], "orderBy": "timestamp desc", "pageSize": max(1, min(int(limit), 200)),
                "filter": f'resource.type="cloud_run_revision" AND resource.labels.service_name="{target.service}" AND severity>={severity} AND timestamp>="{since}"'}
        response = self.client.post("https://logging.googleapis.com/v2/entries:list", json=body, headers={"Authorization": "Bearer " + self.token()})
        if response.status_code == 403:
            raise ValueError("The observer identity is not allowed to read logs; apply the Terraform that grants it the viewer roles")
        response.raise_for_status()
        return [log_line(entry) for entry in response.json().get("entries", [])]
