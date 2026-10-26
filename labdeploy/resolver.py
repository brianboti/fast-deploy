"""Asynchronous dependency resolution; the controller never executes package code."""
import base64
import datetime
import hashlib
import json
import time
import uuid
from urllib.parse import quote

import httpx
from pydantic import Field

from . import operations
from .models import StrictModel
from .resolution_protocol import MAX_INPUT, MAX_OUTPUT, ResolutionPending


class Worker(StrictModel):
    job: str = Field(pattern=r"^projects/[a-z0-9-]+/locations/us-central1/jobs/[a-z0-9-]+$")
    image: str = Field(pattern=r"^us-central1-docker\.pkg\.dev/[a-z0-9/-]+@sha256:[a-f0-9]{64}$")
    network: str
    subnet: str
    proxy: str = Field(pattern=r"^http://10\.[0-9.]+:3128$")
    service_account: str = Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")


class Settings(StrictModel):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    signer: str = Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com$")
    workers: dict[str, Worker]
    max_active: int = Field(default=2, ge=1, le=2)


def command_kind(args):
    from . import scaffold
    for version in scaffold.PYTHONS:
        for mode, command in (("compile", scaffold.lock_container(version)), ("export", scaffold.export_container(version)),
                              ("export-dev", scaffold.export_container(version, True))):
            if args == command:
                return f"python-{version}-{mode}"
    for version in scaffold.NODE_IMAGES:
        if args == scaffold.node_lock_container(version):
            return f"node-{version}-lock"
    raise ValueError("Resolver command is outside the fixed allowlist")


def signed_url(client, token, signer, bucket, key, method, now=None, generation=None):
    """GCS V4 RSA signing through IAM; no private signing key is present on the controller."""
    stamp = now or datetime.datetime.now(datetime.UTC)
    date, timestamp = stamp.strftime("%Y%m%d"), stamp.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date}/auto/storage/goog4_request"
    signed_headers = "host;x-goog-if-generation-match" if method == "PUT" else "host"
    params = {"X-Goog-Algorithm": "GOOG4-RSA-SHA256", "X-Goog-Credential": f"{signer}/{scope}",
              "X-Goog-Date": timestamp, "X-Goog-Expires": "1200", "X-Goog-SignedHeaders": signed_headers}
    if generation is not None:
        params["generation"] = str(generation)
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items()))
    path = f"/{bucket}/{quote(key, safe='/')}"
    headers = "host:storage.googleapis.com\n" + ("x-goog-if-generation-match:0\n" if method == "PUT" else "")
    canonical = "\n".join((method, path, query, headers, signed_headers, "UNSIGNED-PAYLOAD"))
    signing = "\n".join(("GOOG4-RSA-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    response = client.post(f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{signer}:signBlob",
                           headers={"Authorization": "Bearer " + token()}, json={"payload": base64.b64encode(signing.encode()).decode()})
    response.raise_for_status()
    signature = base64.b64decode(response.json()["signedBlob"], validate=True).hex()
    return f"https://storage.googleapis.com{path}?{query}&X-Goog-Signature={signature}"


class Resolver:
    def __init__(self, controller, client=None):
        self.c, self.store = controller, controller.store
        self.client = client or httpx.Client(timeout=10, follow_redirects=False)
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS resolutions (id TEXT PRIMARY KEY REFERENCES operations(id), owner TEXT NOT NULL, "
                       "binding TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, provider TEXT, execution TEXT, result TEXT, created REAL NOT NULL, "
                       "UNIQUE(owner,binding))")

    def runner(self, owner, source):
        if self.c.config.resolver is None and self.c.config.resolver_local_fallback:
            from .scaffold import local_docker_runner
            return local_docker_runner  # the recorded transitional exception: no workers yet
        return lambda args, stdin: self.resolve(owner, source, args, stdin)

    def resolve(self, owner, source, args, stdin):
        settings = self.c.config.resolver
        if not settings or not self.c.observer:
            raise ValueError("Remote dependency workers are not configured; supply a reviewed lockfile. Local execution is disabled.")
        if not self.c.config.live_dispatch or self.c.config.paused or self.store.recovery_mode() or self.c.config.operations_recovery_mode:
            raise ValueError("New dependency resolution is paused")
        if len(stdin.encode()) > MAX_INPUT:
            raise ValueError("Dependency input exceeds its limit")
        if not self.c.config.durable_intents or not self.c.config.backup_bucket:
            raise ValueError("Remote resolution requires off-host mutation intents (durable_intents and backup_bucket)")
        mode = command_kind(args)
        worker = settings.workers.get(mode)
        if worker is None:
            raise ValueError("No reviewed worker image is configured for this runtime and operation")
        binding = {"source": source, "mode": mode, "input_hash": hashlib.sha256(stdin.encode()).hexdigest(), "worker": worker.model_dump()}
        fingerprint = operations.digest(binding)
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM resolutions WHERE owner=? AND binding=?", (owner, fingerprint)).fetchone()
        if row:
            result = self.status(owner, row[0])
            if result["state"] == "succeeded":
                with self.store.connect() as db:
                    return db.execute("SELECT result FROM resolutions WHERE id=?", (row[0],)).fetchone()[0]
            if result["state"] == "failed":
                raise ValueError("Remote resolution failed; inspect worker status or supply a reviewed lockfile")
            raise ResolutionPending(row[0])
        identifier = uuid.uuid4().hex
        request = {"id": identifier, "kind": "resolver", "source": binding, "target": {"job": worker.job}}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM resolutions WHERE state NOT IN ('succeeded','failed')").fetchone()[0] >= settings.max_active:
                raise ValueError("Remote resolver capacity is full; unknown launches require reconciliation")
            existing = db.execute("SELECT id FROM resolutions WHERE owner=? AND binding=?", (owner, fingerprint)).fetchone()
            if existing:
                raise ResolutionPending(existing[0])
            operations.record(db, owner, request, "dispatching")
            db.execute("INSERT INTO resolutions VALUES (?,?,?,'prepared',?,NULL,NULL,NULL,?)",
                       (identifier, owner, fingerprint, operations.canonical(binding), time.time()))
        token = self.c.observer.token
        sent = False
        try:
            # Only fixed pre-provisioned jobs; never pass a caller's shell, command or destination in overrides.
            response = self.client.get("https://run.googleapis.com/v2/" + worker.job, headers={"Authorization": "Bearer " + token()})
            response.raise_for_status()
            job = response.json()
            template = job["template"]["template"]
            if (template.get("containers", [{}])[0].get("image") != worker.image or len(template.get("containers", [])) != 1
                    or template.get("maxRetries", 0) != 0 or template.get("serviceAccount") != worker.service_account
                    or (template.get("vpcAccess") or {}).get("egress") != "ALL_TRAFFIC" or not job.get("etag")
                    or job["template"].get("taskCount", 1) != 1 or job["template"].get("parallelism", 1) != 1
                    or template.get("containers", [{}])[0].get("command") or template.get("containers", [{}])[0].get("args")):
                raise ValueError("Worker configuration differs from its reviewed boundary")
            interfaces = (template.get("vpcAccess") or {}).get("networkInterfaces", [])
            container = template["containers"][0]
            env = {item.get("name"): item.get("value") for item in container.get("env", [])}
            if (interfaces != [{"network": worker.network, "subnetwork": worker.subnet, "tags": ["resolver-worker"]}]
                    or env != {"HTTPS_PROXY": worker.proxy, "HTTP_PROXY": worker.proxy}
                    or (container.get("resources") or {}).get("limits") != {"cpu": "1", "memory": "1Gi"}):
                raise ValueError("Worker configuration differs from its reviewed boundary")
            manifest = {"schema": 1, "id": identifier, "mode": mode, "input_hash": binding["input_hash"], "input": stdin, "deadline": time.time() + 900}
            key = f"inputs/{identifier}.json"
            response = self.client.post(f"https://storage.googleapis.com/upload/storage/v1/b/{settings.bucket}/o",
                                        headers={"Authorization": "Bearer " + token()},
                                        params={"uploadType": "media", "name": key, "ifGenerationMatch": "0"}, content=operations.canonical(manifest).encode())
            response.raise_for_status()
            input_url = signed_url(self.client, token, settings.signer, settings.bucket, key, "GET", generation=response.json()["generation"])
            output_url = signed_url(self.client, token, settings.signer, settings.bucket, f"outputs/{identifier}.json", "PUT")
            operations.Intents(self.c.config.backup_bucket, token, prefix=self.c.config.backup_prefix + "/intents/v1", client=self.client).write(
                identifier, {"schema": 1, "kind": "resolver", "owner": owner, "request": request, "request_hash": operations.digest(request)})
            self.store.start_action(identifier)
            sent = True
            response = self.client.post("https://run.googleapis.com/v2/" + worker.job + ":run", headers={"Authorization": "Bearer " + token()},
                                        json={"etag": job["etag"], "overrides": {"taskCount": 1, "timeout": "900s", "containerOverrides": [{"env": [
                                            {"name": "INPUT_URL", "value": input_url}, {"name": "OUTPUT_URL", "value": output_url},
                                            {"name": "FAST_DEPLOY_OPERATION_ID", "value": identifier}]}]}})
            response.raise_for_status()
            provider = response.json()["name"]
            if not provider.startswith(worker.job.rsplit("/jobs/", 1)[0] + "/operations/"):
                raise ValueError("Worker launch returned an unrelated operation")
            with self.store.connect() as db:
                db.execute("UPDATE resolutions SET state='running',provider=? WHERE id=?", (provider, identifier))
                operations.transition(db, identifier, "dispatched")
        except Exception as error:
            refused = isinstance(error, httpx.HTTPStatusError) and 400 <= error.response.status_code < 500 \
                and error.response.status_code not in (408, 429)
            with self.store.connect() as db:
                if not sent or refused:
                    # No worker started: free the slot and let the same proposal try again later.
                    db.execute("DELETE FROM outbox WHERE operation_id=? AND state='prepared'", (identifier,))
                    db.execute("DELETE FROM resolutions WHERE id=?", (identifier,))
                    operations.transition(db, identifier, "failed")
                else:  # Never replay after a provider timeout: the worker may be running.
                    db.execute("UPDATE resolutions SET state='unknown' WHERE id=?", (identifier,))
                    operations.transition(db, identifier, "dispatch_unknown")
            if not sent or refused:
                raise ValueError(f"The dependency worker could not be started: {type(error).__name__}; ask again later or supply a reviewed lockfile") from None
        raise ResolutionPending(identifier)

    def status(self, owner, identifier):
        with self.store.connect() as db:
            row = db.execute("SELECT state,payload,provider,execution FROM resolutions WHERE id=? AND owner=?", (identifier, owner)).fetchone()
        if row is None:
            raise ValueError("Resolution operation not found")
        state, payload, provider, execution = row
        binding = json.loads(payload)
        if state in {"succeeded", "failed"}:
            return {"id": identifier, "state": state, "kind": "resolver"}
        headers = {"Authorization": "Bearer " + self.c.observer.token()}
        name = binding["worker"]["job"]
        if provider and not execution:
            # Once the execution is known its operation is not read again: operations expire, executions do not.
            response = self.client.get("https://run.googleapis.com/v2/" + provider, headers=headers)
            response.raise_for_status()
            result = response.json()
            execution = (result.get("response") or {}).get("name") or (result.get("metadata") or {}).get("name")
            if result.get("done") and result.get("error") and not execution:
                self.finish(identifier, "failed")
                return {"id": identifier, "kind": "resolver", "state": "failed"}
        elif not execution:
            execution = operations.scan_execution(self.store, self.client, headers, name, identifier)
        if execution:
            if not execution.startswith(name + "/executions/"):
                raise ValueError("Worker execution identity mismatch")
            with self.store.connect() as db:
                db.execute("UPDATE resolutions SET execution=? WHERE id=?", (execution, identifier))
            response = self.client.get("https://run.googleapis.com/v2/" + execution, headers=headers)
            response.raise_for_status()
            execution_body = response.json()
            if execution_body.get("completionTime"):
                if execution_body.get("succeededCount") != 1:
                    self.finish(identifier, "failed")
                    state = "failed"
                else:
                    try:
                        output = self.collect(identifier, binding, headers)
                    except (ValueError, httpx.HTTPStatusError) as error:
                        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500:
                            raise  # the bucket is unavailable: observe again later
                        # The worker finished, and what it left is missing or not a valid lock: a final failure, not a slot
                        # held forever. Its output is untrusted, so only the kind of error is kept.
                        self.finish(identifier, "failed", f"rejected worker output: {type(error).__name__}")
                        return {"id": identifier, "kind": "resolver", "state": "failed"}
                    self.finish(identifier, "succeeded", output)
                    state = "succeeded"
        return {"id": identifier, "kind": "resolver", "state": state,
                "next_action": "Repeat the proposal after completion; an unknown launch is observed, never automatically retried."}

    def collect(self, identifier, binding, headers):
        """The worker's output, checked against this operation's binding and the lock rules; raises when it is not acceptable."""
        key = quote(f"outputs/{identifier}.json", safe="")
        with self.client.stream("GET", f"https://storage.googleapis.com/storage/v1/b/{self.c.config.resolver.bucket}/o/{key}",
                                headers=headers, params={"alt": "media"}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_OUTPUT:
                    raise ValueError("Oversized worker result")
        result = json.loads(data)
        if (not isinstance(result, dict) or result.get("schema") != 1 or result.get("id") != identifier or result.get("input_hash") != binding["input_hash"]
                or result.get("mode") != binding["mode"] or not isinstance(result.get("output"), str)):
            raise ValueError("Worker result binding mismatch")
        validate_output(binding["mode"], result["output"])
        return result["output"]

    def tick(self):
        if not self.c.config.resolver or not self.c.observer:
            return
        with self.store.connect() as db:
            rows = db.execute("SELECT id,owner FROM resolutions WHERE state NOT IN ('succeeded','failed') ORDER BY created LIMIT 2").fetchall()
        for identifier, owner in rows:
            try:
                self.status(owner, identifier)
            except (ValueError, httpx.HTTPError, KeyError):
                continue  # remains unresolved and consumes capacity; next observation can reconcile it

    def finish(self, identifier, state, result=None):
        with self.store.connect() as db:
            changed = db.execute("UPDATE resolutions SET state=?,result=? WHERE id=? AND state NOT IN ('succeeded','failed')", (state, result, identifier)).rowcount
            if changed:
                operations.transition(db, identifier, state)


def validate_output(mode, output):
    if len(output.encode()) > MAX_OUTPUT or "\x00" in output:
        raise ValueError("Worker output exceeds its contract")
    if mode.startswith("python-"):
        from .contractcheck import python_lock
        python_lock(output)
    else:
        from .node import lock_problems, read_lockfile
        data, _ = read_lockfile(output)
        if data is None or lock_problems(data):
            raise ValueError("Worker returned an unsafe dependency lock")
