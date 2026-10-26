"""Authenticated, durable admission before Cloud Run creates an application job execution."""
import datetime
import json
import time
import uuid

import httpx
import jwt
from pydantic import Field

from . import operations
from .models import StrictModel, spec_hash

RUN_API = "https://run.googleapis.com/v2/"
ACTIVE = ("reserved", "sending", "unknown", "running")


class Launch(StrictModel):
    job: str = Field(pattern=r"^[a-z][a-z0-9-]{0,19}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    arguments: list[str] | None = Field(default=None, max_length=64)


class Broker:
    def __init__(self, controller, client=None, clock=time.time, keys=None):
        self.controller, self.store, self.config = controller, controller.store, controller.config
        self.client = client or httpx.Client(timeout=10, follow_redirects=False)
        self.clock = clock
        self.keys = keys or jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, lifespan=300, timeout=10)
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS job_admissions (id TEXT PRIMARY KEY REFERENCES operations(id), subject TEXT NOT NULL, "
                       "application TEXT NOT NULL, environment TEXT NOT NULL, job TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, "
                       "day TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, provider TEXT, execution TEXT, created REAL NOT NULL, "
                       "UNIQUE(subject,application,environment,job,key))")
            if "checked" not in {row[1] for row in db.execute("PRAGMA table_info(job_admissions)")}:
                db.execute("ALTER TABLE job_admissions ADD COLUMN checked REAL NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS job_quota ON job_admissions(application,environment,job,day)")

    def authenticate(self, authorization):
        if self.config.job_broker is None:
            raise ValueError("Job admission is not enabled")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 16384:
            raise ValueError("A workload identity token is required")
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.config.job_broker.audience,
                                issuer=["https://accounts.google.com", "accounts.google.com"],
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=10)
        except (jwt.PyJWTError, OSError, ValueError):
            raise ValueError("Invalid workload identity token") from None
        subject = claims.get("sub")
        if subject not in self.config.job_broker.subjects:
            raise ValueError("Workload identity is not registered for job admission")
        return subject

    def scope(self, subject):
        if self.config.job_broker is None or subject not in self.config.job_broker.subjects:
            raise ValueError("Workload identity is not registered")
        return self.config.job_broker.subjects[subject]

    @staticmethod
    def view(row):
        return {"id": row[0], "application": row[2], "environment": row[3], "job": row[4],
                "state": row[8], "execution": row[11], "charged_day": row[7], "charged_units": 1}

    def submit(self, subject, request):
        scope = self.scope(subject)
        if not isinstance(request, Launch):
            request = Launch.model_validate(request)
        arguments = request.arguments
        if arguments is not None and (any(len(v) > 1024 or "\x00" in v for v in arguments) or len(json.dumps(arguments).encode()) > 8192):
            raise ValueError("Job arguments exceed their limits")
        fingerprint = operations.digest(request.model_dump())
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM job_admissions WHERE subject=? AND application=? AND environment=? AND job=? AND key=?",
                                  (subject, scope.application, scope.environment, request.job, request.idempotency_key)).fetchone()
        if existing:
            if existing[6] != fingerprint:
                raise ValueError("Idempotency key already names different arguments")
            return self.view(existing)
        if not self.config.live_dispatch or self.config.paused or self.config.operations_recovery_mode or self.store.recovery_mode():
            raise ValueError("New job admission is paused")
        if not self.config.durable_intents or not self.config.backup_bucket or self.controller.observer is None:
            raise ValueError("Job admission requires durable off-host intents and a cloud identity")
        app = self.config.applications.get(scope.application)
        if app is None or app.paused:
            raise ValueError("Application is missing or paused")
        selected = next((item for item in self.controller.jobs.targets() if item[0] == scope.application and item[1] == scope.environment), None)
        if selected is None or selected[3] is None or request.job not in selected[3].jobs:
            raise ValueError("The released contract does not declare this job")
        _, _, target, spec, _ = selected
        job = spec.jobs[request.job]
        name = f"projects/{target.project_id}/locations/{target.region}/jobs/{target.service}-{request.job}"
        headers = {"Authorization": "Bearer " + self.controller.observer.token()}
        service = self.client.get(RUN_API + f"projects/{target.project_id}/locations/{target.region}/services/{target.service}", headers=headers)
        service.raise_for_status()
        if service.json().get("ingress") == "INGRESS_TRAFFIC_INTERNAL_ONLY":
            raise ValueError("The spending cap has closed this application")
        described = self.client.get(RUN_API + name, headers=headers)
        described.raise_for_status()
        actual = described.json()
        template = (actual.get("template") or {}).get("template") or {}
        containers = template.get("containers") or []
        plan = self.store.latest_success(scope.application, kind="promotion" if scope.environment == "production" else None) or {}
        expected = (((plan.get("receipt") or {}).get("jobs") or {}).get(request.job) or {}).get("image")
        if len(containers) != 1 or not expected or containers[0].get("image") != expected:
            raise ValueError("Job image does not match the released contract evidence")
        limits = (containers[0].get("resources") or {}).get("limits") or {}
        if (template.get("maxRetries", 0) != 0 or template.get("serviceAccount") != f"{target.service}-{request.job}@{target.project_id}.iam.gserviceaccount.com"
                or str(limits.get("cpu")) != str(job.resources.cpu) or limits.get("memory") != f"{job.resources.memory_mib}Mi"):
            raise ValueError("Job identity or resource settings differ from the admitted contract")
        if not actual.get("etag"):
            raise ValueError("The job has no concurrency token; admission refused")
        identifier = uuid.uuid4().hex
        day = datetime.datetime.fromtimestamp(self.clock(), datetime.UTC).date().isoformat()
        payload = {"name": name, "image": expected, "spec_hash": spec_hash(spec), "arguments": arguments,
                   "timeout": job.timeout_minutes * 60, "etag": actual.get("etag")}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM job_admissions WHERE subject=? AND application=? AND environment=? AND job=? AND key=?",
                                  (subject, scope.application, scope.environment, request.job, request.idempotency_key)).fetchone()
            if existing:
                if existing[6] != fingerprint:
                    raise ValueError("Idempotency key already names different arguments")
                return self.view(existing)
            changing = db.execute("SELECT 1 FROM operations WHERE application=? AND environment=? AND kind!='job' "
                                  "AND state IN ('dispatching','dispatched','dispatch_unknown') LIMIT 1",
                                  (scope.application, scope.environment)).fetchone()
            if changing:
                raise ValueError("A release is changing this application's jobs; retry after it finishes")
            count = db.execute("SELECT COUNT(*) FROM job_admissions WHERE application=? AND environment=? AND job=? AND day=?",
                               (scope.application, scope.environment, request.job, day)).fetchone()[0]
            active = db.execute("SELECT COUNT(*) FROM job_admissions WHERE application=? AND environment=? AND job=? "
                                "AND state IN ('reserved','sending','unknown','running')", (scope.application, scope.environment, request.job)).fetchone()[0]
            if count >= job.runs_per_day or active >= self.config.job_broker.max_active_per_job:
                raise ValueError("Job admission limit reached; unknown launches remain charged")
            operations.record(db, "machine:" + subject, {"id": identifier, "kind": "job", "application": scope.application,
                              "environment": scope.environment, "target": {"job": name}, "spec_hash": spec_hash(spec)}, "dispatching")
            db.execute("INSERT INTO job_admissions(id,subject,application,environment,job,key,request_hash,day,state,payload,provider,execution,created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, NULL, NULL, ?)",
                       (identifier, subject, scope.application, scope.environment, request.job, request.idempotency_key,
                        fingerprint, day, operations.canonical(payload), self.clock()))
        # Failures here retain the reservation. No quota refund or retry guesses.
        witness = {"schema": 1, "kind": "job", "id": identifier, "subject": subject, "application": scope.application,
                   "environment": scope.environment, "job": request.job, "key": request.idempotency_key,
                   "request_hash": fingerprint, "day": day, "name": name, "image": expected, "spec_hash": spec_hash(spec)}
        sent = False
        try:
            operations.Intents(self.config.backup_bucket, self.controller.observer.token,
                               prefix=self.config.backup_prefix + "/intents/v1", client=self.client).write(identifier, witness)
            self.store.start_action(identifier)
            with self.store.connect() as db:
                db.execute("UPDATE job_admissions SET state='sending' WHERE id=?", (identifier,))
            override = {"env": [{"name": "FAST_DEPLOY_OPERATION_ID", "value": identifier}]}
            if arguments is not None:
                override.update(args=arguments, clearArgs=not arguments)
            sent = True
            response = self.client.post(RUN_API + name + ":run", headers=headers,
                                        json={"etag": actual.get("etag"), "overrides": {"taskCount": 1, "timeout": f"{payload['timeout']}s",
                                                                                     "containerOverrides": [override]}})
            response.raise_for_status()
            provider = response.json().get("name")
            if not isinstance(provider, str) or not provider.startswith(f"projects/{target.project_id}/locations/{target.region}/operations/"):
                raise ValueError("Provider returned no matching operation")
            with self.store.connect() as db:
                db.execute("UPDATE job_admissions SET state='running',provider=? WHERE id=?", (provider, identifier))
                operations.transition(db, identifier, "dispatched")
        except (httpx.HTTPError, ValueError, OSError) as error:
            # Nothing sent, or a definite refusal (403, an etag conflict): the job did not run, so its concurrency slot is
            # free again. The day's charge stays either way. Anything else may have started a job and stays unknown.
            refused = isinstance(error, httpx.HTTPStatusError) and 400 <= error.response.status_code < 500 \
                and error.response.status_code not in (408, 429)
            with self.store.connect() as db:
                if not sent or refused:
                    db.execute("DELETE FROM outbox WHERE operation_id=? AND state='prepared'", (identifier,))
                    db.execute("UPDATE job_admissions SET state='not_executed' WHERE id=?", (identifier,))
                    operations.transition(db, identifier, "failed")
                else:
                    db.execute("UPDATE job_admissions SET state='unknown' WHERE id=?", (identifier,))
                    operations.transition(db, identifier, "dispatch_unknown")
        return self.status(subject, identifier, refresh=False)

    def status(self, subject, identifier, refresh=True):
        scope = self.scope(subject)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM job_admissions WHERE id=? AND application=? AND environment=?",
                             (identifier, scope.application, scope.environment)).fetchone()
        if row is None:
            raise ValueError("Job operation not found")
        if refresh and row[8] in ACTIVE and self.controller.observer:
            headers = {"Authorization": "Bearer " + self.controller.observer.token()}
            result, execution = {}, row[11]
            if row[10] and not execution:
                # Once the execution is known its operation is not read again: operations expire, executions do not.
                response = self.client.get(RUN_API + row[10], headers=headers)
                if response.status_code != 404:
                    response.raise_for_status()
                    result = response.json()
                    execution = (result.get("response") or {}).get("name") or (result.get("metadata") or {}).get("name")
            if not execution and not result:
                execution = operations.scan_execution(self.store, self.client, headers, json.loads(row[9])["name"], identifier)
            expected = json.loads(row[9])["name"] + "/executions/"
            if execution and not execution.startswith(expected):
                raise ValueError("Provider execution belongs to another job")
            # The long-running operation finishing only establishes execution creation.
            # Concurrency is released after the execution itself has a completion time.
            completed, state = False, "running" if execution else row[8]
            if execution:
                found = self.client.get(RUN_API + execution, headers={"Authorization": "Bearer " + self.controller.observer.token()})
                found.raise_for_status()
                body = found.json()
                completed = bool(body.get("completionTime"))
                if completed:
                    state = "succeeded" if body.get("succeededCount") == 1 else "failed"
            if result.get("done") and result.get("error") and not execution:
                completed, state = True, "failed"
            with self.store.connect() as db:
                changed = db.execute("UPDATE job_admissions SET state=?,execution=COALESCE(?,execution) WHERE id=? "
                                     "AND state IN ('reserved','sending','unknown','running')", (state, execution, identifier)).rowcount
                if completed and changed:
                    operations.transition(db, identifier, state)
                row = db.execute("SELECT * FROM job_admissions WHERE id=?", (identifier,)).fetchone()
        return self.view(row)

    def tick(self):
        if self.config.job_broker is None:
            return {}
        with self.store.connect() as db:
            rows = db.execute("SELECT subject,id FROM job_admissions WHERE state IN ('reserved','sending','unknown','running') ORDER BY checked,created LIMIT 20").fetchall()
        results = {}
        deadline = time.monotonic() + 20
        for subject, identifier in rows:
            if time.monotonic() >= deadline:
                break
            with self.store.connect() as db:
                db.execute("UPDATE job_admissions SET checked=? WHERE id=?", (self.clock(), identifier))
            try:
                results[identifier] = self.status(subject, identifier)["state"]
            except (ValueError, httpx.HTTPError):
                results[identifier] = "unresolved"
        return results
