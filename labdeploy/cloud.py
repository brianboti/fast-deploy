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

from .telemetry import RESOURCE, CloudMonitoring, merge, percentile

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


def distribution_summary(dists):
    """Count, mean, 95th percentile and slowest bucket (seconds) of Cloud Monitoring distributions with the same buckets."""
    def bounds(options):
        if "exponentialBuckets" in options:
            spec = options["exponentialBuckets"]
            return [float(spec["scale"]) * float(spec["growthFactor"]) ** index for index in range(int(spec["numFiniteBuckets"]) + 1)]
        if "linearBuckets" in options:
            spec = options["linearBuckets"]
            return [float(spec["offset"]) + float(spec["width"]) * index for index in range(int(spec["numFiniteBuckets"]) + 1)]
        return [float(value) for value in options["explicitBuckets"]["bounds"]]
    edges = bounds(dists[0]["bucketOptions"])
    counts = [0] * (len(edges) + 1)
    total, weighted = 0, 0.0
    for dist in dists:
        n = int(dist.get("count", 0))
        total += n
        weighted += float(dist.get("mean", 0)) * n
        for index, value in enumerate(dist.get("bucketCounts", [])):
            if index < len(counts):
                counts[index] += int(value)

    def upper(index):
        return edges[index] if index < len(edges) else edges[-1] * 2
    target, seen, p95 = 0.95 * total, 0, None
    for index, value in enumerate(counts):
        if value and seen + value >= target:
            low = 0.0 if index == 0 else edges[index - 1]
            p95 = low + (upper(index) - low) * ((target - seen) / value)
            break
        seen += value
    slowest = max((upper(index) for index, value in enumerate(counts) if value), default=0.0)
    return {"starts": total, "mean_s": round(weighted / total / 1000, 1) if total else None,
            "p95_s": None if p95 is None else round(p95 / 1000, 1), "slowest_s": round(slowest / 1000, 1)}


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

    def error_budget(self, target, service_id, slo_id="availability"):
        """Production's objective (docs/ROADMAP.md D39) and what is left of its error budget now: the goal, the share of good
        requests since the start of its 30-day window, and the budget's remaining fraction, below zero once it is spent.
        None when the service has no objective (staging, or a production root not rendered with one yet)."""
        name = f"projects/{target.project_id}/services/{service_id}/serviceLevelObjectives/{slo_id}"
        response = self.client.get(f"https://monitoring.googleapis.com/v3/{name}", headers={"Authorization": "Bearer " + self.token()})
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            raise ValueError("The observer identity is not allowed to read this; apply the Terraform that grants it the viewer roles")
        response.raise_for_status()
        slo = response.json()
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(minutes=30)

        def newest(selector):
            data = self.get("https://monitoring.googleapis.com/v3/projects/" + target.project_id + "/timeSeries", filter=f'{selector}("{name}")',
                            **{"interval.startTime": start.isoformat().replace("+00:00", "Z"), "interval.endTime": end.isoformat().replace("+00:00", "Z")})
            points = [point for series in data.get("timeSeries", []) for point in series.get("points", [])]
            if not points:
                return None
            value = max(points, key=lambda point: (point.get("interval") or {}).get("endTime") or "").get("value") or {}
            return float(value["doubleValue"]) if "doubleValue" in value else None
        days = int(str(slo.get("rollingPeriod") or "2592000s").rstrip("s")) // 86400
        found = {"goal": slo.get("goal"), "days": days, "compliance": newest("select_slo_compliance"),
                 "budget_remaining": newest("select_slo_budget_fraction"), "name": name}
        # What the objective counts (docs/ROADMAP.md D58): its own filters, so the answer follows whatever it was rendered
        # with. The newest day with a server error says when a spent budget recovers at the latest, if no new one comes.
        # The API returns the rules under serviceLevelIndicator.requestBased (Terraform's request_based_sli is its own name for
        # them); until 1.29.2 this read the Terraform name, found nothing, and the counts below never appeared.
        ratio = ((slo.get("serviceLevelIndicator") or {}).get("requestBased") or {}).get("goodTotalRatio") or {}
        try:
            monitoring = CloudMonitoring(target.project_id, self.token, self.client)
            window_start = end - datetime.timedelta(days=days)
            if ratio.get("totalServiceFilter"):
                found["requests_counted"] = int(sum(value for _, value in monitoring.points(
                    ratio["totalServiceFilter"], "ALIGN_DELTA", "REDUCE_SUM", window_start, end, 86400)))
            if ratio.get("badServiceFilter"):
                bad = [(at, value) for at, value in monitoring.points(ratio["badServiceFilter"], "ALIGN_DELTA", "REDUCE_SUM", window_start, end, 86400) if value]
                found["errors_counted"] = int(sum(value for _, value in bad))
                if bad:
                    last = datetime.datetime.fromisoformat(bad[-1][0].replace("Z", "+00:00"))
                    found["last_error_day"] = last.date().isoformat()
                    found["clears_by"] = (last + datetime.timedelta(days=days)).date().isoformat()
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # the counts explain the budget; the budget itself was read
        return found

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

    # ---- the settings page's charts (labdeploy/monitor.py): one query per chart line, each line one time series
    def service_filter(self, metric, target):
        return f'metric.type="{metric}" AND {RESOURCE} AND resource.labels.service_name="{target.service}"'

    def traffic(self, target, start, end, step):
        """Requests per step by response class, and the 95th-percentile latency of each step and of the whole window, from one
        query: Cloud Run's request-latency distribution summed per step and kept apart by response class (one call; one time
        series per class). Each step is {"at": its end, "classes": {"2xx": requests, ...}, "p95_ms"}."""
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        groups = monitoring.distributions(self.service_filter("run.googleapis.com/request_latencies", target), start, end, step,
                                          "metric.label.response_code_class")
        by_class = {name or "other": dict(points) for name, points in groups.items()}
        steps = []
        for at in sorted({at for points in by_class.values() for at in points}):
            counted = {name: points[at] for name, points in by_class.items() if points.get(at)}
            steps.append({"at": at, "classes": {name: sum(found.values()) for name, found in counted.items()},
                          "p95_ms": percentile(merge(*counted.values()), 0.95)})
        whole = merge(*(found for points in by_class.values() for found in points.values()))
        return {"steps": steps, "p95_ms": percentile(whole, 0.95)}

    def saturation(self, target, start, end, step):
        """Per step, the most instances serving at once and the 95th-percentile memory use (a share of the limit): two calls."""
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        instances = monitoring.points(self.service_filter("run.googleapis.com/container/instance_count", target) + ' AND metric.labels.state="active"',
                                      "ALIGN_MAX", "REDUCE_SUM", start, end, step)
        memory = monitoring.points(self.service_filter("run.googleapis.com/container/memory/utilizations", target), "ALIGN_PERCENTILE_95", "REDUCE_MAX",
                                   start, end, step)
        return {"instances": instances, "memory": memory}

    def uptime_days(self, target, days=30, now=None):
        """The share of Google's uptime checks that passed on each of the last `days` UTC days, today so far last; None for a
        day without a measurement. One call of hourly points, averaged per day."""
        end = now or datetime.datetime.now(datetime.UTC)
        start = (end - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        hours = monitoring.points(self.service_filter("monitoring.googleapis.com/uptime_check/check_passed", target), "ALIGN_FRACTION_TRUE", "REDUCE_MEAN",
                                  start, end, 3600)
        found = {}
        for at, value in hours:
            try:
                moment = datetime.datetime.fromisoformat(at.replace("Z", "+00:00")) - datetime.timedelta(minutes=30)  # the hour the point covers
            except ValueError:
                continue
            found.setdefault(moment.date().isoformat(), []).append(float(value))
        result = []
        for offset in range(days):
            day = (start + datetime.timedelta(days=offset)).date().isoformat()
            values = found.get(day)
            result.append({"day": day, "passed": round(sum(values) / len(values), 4) if values else None})
        return result

    def uptime_pass_rate(self, target, start, end):
        """The share of Google's uptime checks that passed between two moments (every region together), or None without data."""
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        seconds = max(60, int((end - start).total_seconds()))
        values = monitoring.query("monitoring.googleapis.com/uptime_check/check_passed", "ALIGN_FRACTION_TRUE", "REDUCE_MEAN", start, end, seconds,
                                  f'resource.labels.service_name="{target.service}"')
        return round(min(values), 4) if values else None

    def memory_p95(self, target, revisions, start, end):
        """The busiest p95 memory utilization of these revisions in a window (a share of their limit), or None."""
        names = [name for name in revisions if re.fullmatch(r"[a-z][a-z0-9-]{1,62}", str(name))]
        if not names:
            return None
        monitoring = CloudMonitoring(target.project_id, self.token, self.client)
        seconds = max(60, int((end - start).total_seconds()))
        values = monitoring.query("run.googleapis.com/container/memory/utilizations", "ALIGN_PERCENTILE_95", "REDUCE_MAX", start, end, seconds,
                                  f'resource.labels.service_name="{target.service}" AND resource.labels.revision_name = one_of('
                                  + ", ".join(f'"{name}"' for name in names) + ")")
        return round(max(values), 4) if values else None

    REQUEST_CAP = 5000  # per window: enough to judge a share and a p95; a busier window is judged on its newest 5,000

    def revision_requests(self, target, revisions, start, end, health_path=None, cap=REQUEST_CAP):
        """Cloud Run's request log of some revisions between two moments, the health path left out: what users and programs
        sent, not the uptime check or the probes (Cloud Run's request metric cannot tell them apart, docs/ROADMAP.md D57).
        Returns {"requests": [{"status", "latency_ms"}], "truncated": bool}, newest first."""
        names = [name for name in revisions if re.fullmatch(r"[a-z][a-z0-9-]{1,62}", str(name))]
        if not names:
            raise ValueError("Invalid revision name")
        if health_path is not None and not re.fullmatch(r"/[A-Za-z0-9/_-]*", health_path):
            raise ValueError("Invalid health path")
        iso = lambda moment: moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
        clauses = ['resource.type="cloud_run_revision"', f'resource.labels.service_name="{target.service}"',
                   "(" + " OR ".join(f'resource.labels.revision_name="{name}"' for name in names) + ")",
                   f'logName="projects/{target.project_id}/logs/run.googleapis.com%2Frequests"', f'timestamp>="{iso(start)}"', f'timestamp<"{iso(end)}"']
        if health_path:
            clauses.append(f'NOT httpRequest.requestUrl=~"^https?://[^/]+{health_path}([?].*)?$"')
        body = {"resourceNames": [f"projects/{target.project_id}"], "orderBy": "timestamp desc", "pageSize": 1000, "filter": " AND ".join(clauses)}
        found, truncated = [], False
        while True:
            response = self.client.post("https://logging.googleapis.com/v2/entries:list", json=body, headers={"Authorization": "Bearer " + self.token()})
            if response.status_code == 403:
                raise ValueError("The observer identity is not allowed to read logs; apply the Terraform that grants it the viewer roles")
            response.raise_for_status()
            data = response.json()
            for entry in data.get("entries", []):
                request = entry.get("httpRequest") if isinstance(entry.get("httpRequest"), dict) else {}
                path = (re.sub(r"^[a-z]+://[^/]+", "", str(request.get("requestUrl") or "")).split("?", 1)[0]) or "/"
                if health_path and path == health_path:
                    continue
                latency = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", str(request.get("latency") or ""))
                status = request.get("status")
                found.append({"status": status if isinstance(status, int) else None, "latency_ms": float(latency.group(1)) * 1000 if latency else None})
            token = data.get("nextPageToken")
            if len(found) >= cap:
                return {"requests": found[:cap], "truncated": True}
            if not token:
                return {"requests": found, "truncated": truncated}
            body = {**body, "pageToken": token}

    # ---- for the daily digest (docs/ROADMAP.md 25.3 to 25.5): refused requests, starts, Google's incidents, quotas
    def refused_requests(self, target, minutes=1440, cap=200):
        """How many requests the service answered 429 in the window (Cloud Run's "no available instance" or the application's
        own refusal), from the request log: Cloud Run's request metric leaves out requests refused at the instance limit."""
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = {"resourceNames": [f"projects/{target.project_id}"], "orderBy": "timestamp desc", "pageSize": cap,
                "filter": (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{target.service}" AND '
                           f'logName="projects/{target.project_id}/logs/run.googleapis.com%2Frequests" AND httpRequest.status=429 AND timestamp>="{since}"')}
        response = self.client.post("https://logging.googleapis.com/v2/entries:list", json=body, headers={"Authorization": "Bearer " + self.token()})
        response.raise_for_status()
        return len(response.json().get("entries", []))

    def starts(self, target, start, end):
        """The service's container starts in a window from Cloud Run's start-up distribution: how many, and the 95th percentile
        and the slowest bucket in seconds; None when no instance started."""
        params = {"filter": f'metric.type="run.googleapis.com/container/startup_latencies" AND resource.type="cloud_run_revision" AND '
                            f'resource.labels.service_name="{target.service}"',
                  "interval.startTime": start.isoformat().replace("+00:00", "Z"), "interval.endTime": end.isoformat().replace("+00:00", "Z"),
                  "aggregation.alignmentPeriod": f"{max(60, int((end - start).total_seconds()))}s", "aggregation.perSeriesAligner": "ALIGN_DELTA",
                  "aggregation.crossSeriesReducer": "REDUCE_SUM"}
        series = self.get(f"https://monitoring.googleapis.com/v3/projects/{target.project_id}/timeSeries", **params).get("timeSeries", [])
        dists = [point["value"]["distributionValue"] for item in series for point in item.get("points", []) if "distributionValue" in point.get("value", {})]
        dists = [dist for dist in dists if int(dist.get("count", 0))]
        if not dists:
            return None
        return distribution_summary(dists)

    def google_incidents(self, project_id, minutes=1440):
        """Google's incidents that Personalized Service Health wrote to this project's log in the window, newest first."""
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = {"resourceNames": [f"projects/{project_id}"], "orderBy": "timestamp desc", "pageSize": 50,
                "filter": (f'logName="projects/{project_id}/logs/servicehealth.googleapis.com%2Factivity" AND '
                           f'jsonPayload.category="INCIDENT" AND timestamp>="{since}"')}
        response = self.client.post("https://logging.googleapis.com/v2/entries:list", json=body, headers={"Authorization": "Bearer " + self.token()})
        response.raise_for_status()
        seen, found = set(), []
        for entry in response.json().get("entries", []):
            payload = entry.get("jsonPayload") or {}
            key = payload.get("eventId") or payload.get("title")
            if key in seen:
                continue  # one line per incident: its updates are later entries of the same event
            seen.add(key)
            found.append({"title": str(payload.get("title") or "an incident")[:200], "state": payload.get("state"),
                          "detailed_state": payload.get("detailedState"), "relevance": payload.get("relevance"),
                          "locations": list(payload.get("impactedLocations") or [])[:5], "at": entry.get("timestamp")})
        return found

    QUOTA_SHARE = 0.8  # Google's recommended alerting rule for quotas: usage above 80 percent of the limit

    def quota_pressure(self, project_id):
        """The allocation quotas of this project above 80 percent of their limit, from the quota metrics Google exposes (only
        services that report consumer quotas appear; Cloud Run's instance quota does not)."""
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(hours=6)

        def latest(metric):
            found = {}
            params = {"filter": f'metric.type="serviceruntime.googleapis.com/quota/{metric}" AND resource.type="consumer_quota"',
                      "interval.startTime": start.isoformat().replace("+00:00", "Z"), "interval.endTime": end.isoformat().replace("+00:00", "Z")}
            for item in self.get(f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries", **params).get("timeSeries", []):
                key = ((item.get("resource") or {}).get("labels", {}).get("service"), (item.get("metric") or {}).get("labels", {}).get("quota_metric"))
                points = item.get("points") or []
                if not points:
                    continue
                value = points[0].get("value", {})
                number = float(value.get("int64Value", value.get("doubleValue", 0)))
                found.setdefault(key, []).append(number)
            return found
        usage, limits = latest("allocation/usage"), latest("limit")
        pressed = []
        for key, used in usage.items():
            bounds = [value for value in limits.get(key, []) if value > 0]
            if not bounds:
                continue
            share = max(used) / min(bounds)
            if share >= self.QUOTA_SHARE:
                pressed.append({"service": key[0], "quota": key[1], "usage": max(used), "limit": min(bounds), "share": round(share, 3)})
        return sorted(pressed, key=lambda item: -item["share"])

    # ---- serving images (docs/ROADMAP.md 26.3)
    MANIFESTS = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
                 "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")
    PULL_CAP = 4 * 1024 ** 3  # an image this large is listed, not pulled again

    def serving_images(self, target):
        """The images the service's serving revisions run, by digest, from its own project's registry."""
        base = f"https://run.googleapis.com/v2/projects/{target.project_id}/locations/{target.region}/services/{target.service}"
        data = self.get(base)
        revisions = sorted({item.get("revision") for item in data.get("trafficStatuses") or data.get("traffic") or []
                            if item.get("percent") and re.fullmatch(r"[a-z][a-z0-9-]{1,62}", item.get("revision") or "")})
        prefix = f"{target.region}-docker.pkg.dev/{target.project_id}/"
        images = []
        for revision in revisions:
            for container in self.get(f"{base}/revisions/{revision}").get("containers") or []:
                image = container.get("image") or ""
                if image.startswith(prefix) and re.search(r"@sha256:[a-f0-9]{64}$", image) and image not in images:
                    images.append(image)
        return images

    def image_findings(self, project_id, image):
        """What Artifact Analysis holds for one image now: findings by severity and with a fix, and whether it still
        analyses the image (only while it was pulled within 30 days)."""
        from .scan import fix_available, malicious
        url = f"https://containeranalysis.googleapis.com/v1/projects/{project_id}/occurrences"
        resource = f'resourceUrl="https://{image}"'
        found, page = [], None
        while True:
            params = {"filter": f'kind="VULNERABILITY" AND {resource}', "pageSize": 1000, **({"pageToken": page} if page else {})}
            data = self.get(url, **params)
            found += data.get("occurrences") or []
            page = data.get("nextPageToken")
            if not page or len(found) >= 10_000:
                break
        discovery = ((self.get(url, filter=f'kind="DISCOVERY" AND {resource}', pageSize=1).get("occurrences") or [{}])[0].get("discovery") or {})
        result = {"image": image, "findings": len(found), "critical": 0, "high": 0, "critical_fixable": 0, "high_fixable": 0,
                  "malicious": 0, "analysis": discovery.get("continuousAnalysis"), "status": discovery.get("analysisStatus"),
                  "last_scan": discovery.get("lastScanTime")}
        for occurrence in found:
            details = occurrence.get("vulnerability") or {}
            severity = (details.get("effectiveSeverity") or details.get("severity") or "").upper()
            if severity in ("CRITICAL", "HIGH"):
                key = severity.lower()
                result[key] += 1
                result[key + "_fixable"] += 1 if fix_available(occurrence) else 0
            result["malicious"] += 1 if malicious(occurrence) else 0
        return result

    def refresh_analysis(self, image):
        """Pull the image, its manifest and every layer read and discarded, so Artifact Analysis keeps analysing it: it does
        only for images pulled within 30 days, and Cloud Run never pulls a serving image again after its deploy. The machine
        and the registry are in the same region, so the pull costs nothing. Returns the bytes read."""
        found = re.fullmatch(r"(?P<host>[a-z0-9-]+-docker\.pkg\.dev)/(?P<path>[a-z0-9._/-]+)@(?P<digest>sha256:[a-f0-9]{64})", image)
        if not found:
            raise ValueError(f"{image} is not an Artifact Registry image by digest")
        base = f"https://{found['host']}/v2/{found['path']}"
        auth = {"Authorization": "Basic " + base64.b64encode(f"oauth2accesstoken:{self.token()}".encode()).decode()}

        def manifest(reference):
            response = self.client.get(f"{base}/manifests/{reference}", headers={**auth, "Accept": self.MANIFESTS})
            response.raise_for_status()
            return response.json()

        document = manifest(found["digest"])
        if document.get("manifests"):  # an index: the platform builds linux/amd64 only
            chosen = next((item for item in document["manifests"] if (item.get("platform") or {}).get("architecture") == "amd64"), document["manifests"][0])
            document = manifest(chosen["digest"])
        blobs = [document.get("config") or {}, *(document.get("layers") or [])]
        size = sum(int(blob.get("size") or 0) for blob in blobs)
        if size > self.PULL_CAP:
            raise ValueError(f"the image is {size // 1024 ** 2} MiB, over the {self.PULL_CAP // 1024 ** 3} GiB the weekly pull reads")
        total = 0
        for blob in blobs:
            if not blob.get("digest"):
                continue
            url, headers = f"{base}/blobs/{blob['digest']}", auth
            for _ in range(3):  # the registry may answer with a redirect to storage, which gets no credentials
                with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        url, headers = str(httpx.URL(url).join(response.headers["location"])), {}
                        continue
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                    break
        return total

    def month_spend(self, table, projects, now=None):
        """Each project's spend this month and its forecast, from the billing export (docs/ROADMAP.md 24.2)."""
        from .cost import month_to_date
        return month_to_date(table, projects, self.token(), self.client, now)

    def had_user_requests(self, target, days=30, health_path=None):
        """Whether anyone but the health checks called the service in the last `days`: one log line is enough."""
        end = datetime.datetime.now(datetime.UTC)
        clauses = ['resource.type="cloud_run_revision"', f'resource.labels.service_name="{target.service}"',
                   f'logName="projects/{target.project_id}/logs/run.googleapis.com%2Frequests"',
                   f'timestamp>="{(end - datetime.timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")}"']
        if health_path and re.fullmatch(r"/[A-Za-z0-9/_-]*", health_path):
            clauses.append(f'NOT httpRequest.requestUrl=~"^https?://[^/]+{health_path}([?].*)?$"')
        body = {"resourceNames": [f"projects/{target.project_id}"], "orderBy": "timestamp desc", "pageSize": 1, "filter": " AND ".join(clauses)}
        response = self.client.post("https://logging.googleapis.com/v2/entries:list", json=body, headers={"Authorization": "Bearer " + self.token()})
        response.raise_for_status()
        return bool(response.json().get("entries"))

    def policies(self, project_id):
        """How many alert policies and uptime checks a project holds, and the uptime checks' executions a month: what Google's
        free allotments are measured against (docs/ROADMAP.md 24.4)."""
        def listed(url, key):
            items, params = [], {"pageSize": 500}
            while True:
                data = self.get(url, **params)
                items += data.get(key, [])
                if not data.get("nextPageToken"):
                    return items
                params["pageToken"] = data["nextPageToken"]
        alerts = listed(f"https://monitoring.googleapis.com/v3/projects/{project_id}/alertPolicies", "alertPolicies")
        checks = listed(f"https://monitoring.googleapis.com/v3/projects/{project_id}/uptimeCheckConfigs", "uptimeCheckConfigs")
        executions = 0
        for check in checks:
            period = int(str(check.get("period") or "60s").rstrip("s") or 60)
            regions = len(check.get("selectedRegions") or []) or 6  # every region when none is chosen: six checker locations
            executions += round(30 * 86400 / max(period, 60)) * regions
        return {"alert_policies": len(alerts), "enabled_alert_policies": sum(1 for item in alerts if item.get("enabled", True)),
                "uptime_checks": len(checks), "uptime_executions_month": executions}

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
