# What a repository must contain to be deployed by Lab Deploy

Give this page to the developer. A repository that follows it can be deployed to the lab's Google Cloud staging project
with one request in Claude chat; everything else (identities, registry, buckets, topics, databases, monitoring, cost
control, rollback) is provided by the platform. Written for version 1.5.0; `examples/demo-app` in the platform
repository is a complete reference that follows every rule below.

## The files

```
your-app/
├── Dockerfile            # builds the service; listens on the port below
├── platform.yaml         # what the app is and what it needs (reference below)
├── requirements.txt      # runtime dependencies, pinned
├── requirements-dev.txt  # test dependencies; must include pytest
├── requirements.lock     # optional but recommended: pip-compile --generate-hashes
├── app.py …              # your code, any layout
└── test_*.py or tests/   # pytest tests (or declare tests: none in platform.yaml)
```

Rules the pipeline checks, in the order it checks them:

1. **Tests.** `python -m pytest` must pass after `pip install -r requirements-dev.txt` (and the lock file with
   `--require-hashes` when it exists, otherwise `requirements.txt`). An app without tests must say
   `tests: none` in `platform.yaml`; every release of it then carries a warning.
2. **Dependency audit.** `pip-audit -r requirements.txt` must report no known vulnerability. A vulnerable pin fails the
   release until it is bumped.
3. **Build.** `docker build --platform linux/amd64` from the repository root must succeed without network credentials.
4. **Health.** `GET <health_path>` (default `/health`) must answer 200 within a few seconds of start. The startup probe,
   the release probes and the uptime check all use it. Cloud Run's IAM layer authenticates callers; the app itself
   does not check tokens.
5. **Port.** The container listens on `0.0.0.0` at the port in `platform.yaml` (default 8080) and `EXPOSE`s it.

## The Dockerfile

```dockerfile
FROM python:3.12-slim@sha256:<digest>      # pin the base image by digest; refresh it deliberately
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
USER 65532:65532                           # never run as root
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

No secrets in the image, no build steps that need credentials, nothing written to the container's disk that must
survive a restart (instances come and go).

## platform.yaml

```yaml
api_version: lab/v1
name: my-service          # 3 to 21 characters: a letter, then lowercase letters, digits, dashes; this is the Cloud Run service name
runtime: python-http      # the only runtime today
port: 8080
health_path: /health
resources: { cpu: 1, memory_mib: 512 }   # cpu 1 to 4, memory 512 to 4096; the lab's policy refuses more than 2 CPUs, 2048 MiB today
scaling: { minimum: 0, maximum: 2 }      # minimum 0 or 1 (1 keeps an instance warm and costs money all month); maximum up to 3 under the lab's policy
concurrency: 80           # requests one instance serves at once; higher is cheaper when the app can take it
timeout_seconds: 60       # per request, up to 300
exposure: authenticated   # the only option: callers present a Google identity token
state: stateless
env:                      # plain configuration values
  LOG_LEVEL: info
secrets: [API_KEY]        # names only; the value lives in Secret Manager as my-service-API_KEY and arrives as the env var API_KEY
tests: required           # or none, knowingly
needs:
  buckets: [uploads]      # env BUCKET_UPLOADS = the bucket's name
  topics: [events]        # env TOPIC_EVENTS and SUBSCRIPTION_EVENTS
  database: none          # none | firestore | postgres
  migrate: []             # e.g. [alembic, upgrade, head]: runs as a job from the same image before traffic moves
reviewed: true            # set by the person who reviewed the file
```

What the app receives at run time: `PORT`, the `env` values, each secret as an environment variable of the same name,
`BUCKET_<NAME>` per bucket, `TOPIC_<NAME>` and `SUBSCRIPTION_<NAME>` per topic, `FIRESTORE_DATABASE` for Firestore,
and `DATABASE_INSTANCE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_IP_TYPE` for Postgres. Use Google's client
libraries with default credentials: the service runs as its own least-privilege identity that may only reach its
declared resources. Postgres has IAM sign-in only (the identity is the user; there is no password) and a private
address; connect with the Cloud SQL Python connector or psycopg over the private IP.

Secrets: the developer names them; the operator creates the values once with
`gcloud secrets create my-service-API_KEY --data-file=-`. Values never pass through Claude or Git.

Migrations: idempotent, compatible with the previous revision (expand first, contract later, so a rollback stays safe),
finished within nine minutes. A failing migration stops the release with traffic untouched.

## Costs the developer controls

- `scaling.minimum: 0` costs nothing while idle; `1` costs about 9 dollars a month per warm instance.
- Cloud SQL costs about 9 dollars a month from the moment it exists; Firestore, buckets and topics cost nothing at
  rest within the free tiers. Every plan shows the floor and the worst case before anything is deployed.
- Memory and CPU set the per-request price; start small, read the weekly right-sizing report, then raise.

## What the platform does for you

Builds on every release, audits and tests, signs the image and verifies the signature before deploying, scans it for
vulnerabilities, deploys a candidate without traffic, probes it, moves traffic only when it answers, restores the
previous revision when it does not, keeps a durable checkpoint so a lost runner is recovered, records every step,
watches errors, latency, saturation and memory, and reports measured cost every week.

## What is not supported

Public endpoints, GPUs, runtimes other than Python HTTP services, stateful containers, a branch other than `main`,
production environments, and editing the three workflow files the platform adds under `.github/workflows/`.
