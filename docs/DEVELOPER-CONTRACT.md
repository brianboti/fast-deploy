# The repository standard: what a service must contain to be deployed by Lab Deploy

Give this page to the developer. A repository that follows it is deployed to the lab's Google Cloud staging project
with one request in Claude chat; the platform provides everything else (identities, registry, buckets, topics,
databases, monitoring, cost control, signed images, rollback). `examples/demo-app` in the platform repository is a
complete reference.

The standard has three levels:

- **Required**: the pipeline refuses the release without it.
- **Required by review**: the lab's rule; `analyze_repository` and the reviewer of `platform.yaml` check it.
- **Recommended**: what makes a service behave well on Cloud Run and keeps the repository healthy; taken from
  Google's Cloud Run container contract and development tips, the Twelve-Factor App, Docker's Dockerfile best
  practices, the OpenSSF Scorecard checks and GitHub's community health files (sources at the end).

## 1. The files, what each is for, and what reads it

| File | Level | What it is for | What reads it |
|---|---|---|---|
| `Dockerfile` | Required | How the service is built. The only build file the platform understands; it must produce a Linux x86_64 image that listens on `$PORT`. | The deploy workflow (`docker build --platform linux/amd64`, no network credentials), then Cloud Run |
| `platform.yaml` | Required | The contract between the app and the platform: name, port, health path, size, scaling, timeout, env, secret names, needs, tests policy (section 5). | The controller (plans, cost bounds, policy), the runner (deploy flags, environment variables, migration job), the renderer (the app's Terraform root) |
| `requirements.txt` | Required | Runtime dependencies pinned to exact versions. | The Dockerfile's `pip install`; the audit step (`pip-audit -r requirements.txt`, a known vulnerability fails the release); the analyzer |
| `requirements-dev.txt` | Required unless `tests: none` | Test and lint tools (pytest, ruff), kept out of the image. | The test step (`pip install -r requirements-dev.txt`, then `python -m pytest`) |
| `requirements.lock` | Recommended; required by review for anything beyond a demo | A hash-locked, reproducible install (`pip-compile --generate-hashes` or `uv pip compile --generate-hashes`). A version pin alone still trusts whatever the index serves. | The test step installs it with `--require-hashes` when present; the Dockerfile should too |
| `tests/` or `test_*.py` | Required unless `tests: none` | Prove the app before anything is built; the same tests run in the app's own CI on pull requests. | `python -m pytest` in the deploy workflow and in the app's CI |
| `.dockerignore` | Recommended | Keeps `.git`, virtual environments, tests, caches and local secrets out of the build context: smaller, faster, safer images. | `docker build` |
| `pyproject.toml` | Recommended | One place for project metadata and the ruff and pytest configuration; `requirements*.txt` can be generated from it. | Developers and tools |
| `README.md` | Required by review | What the service is, how to run it locally, its health path, the environment it expects. | People; the analyzer's review questions |
| `LICENSE` | Required by review | The lab's license (MIT unless told otherwise); a public repository without one cannot be reused. | People; the OpenSSF License check |
| `SECURITY.md` | Recommended | How to report a vulnerability privately. | People; the OpenSSF Security-Policy check |
| `.github/CODEOWNERS` | Recommended | Who reviews what; branch protection can require their review. | GitHub |
| `.github/dependabot.yml` | Recommended | Keeps pins current through reviewable pull requests for `pip`, `docker` and `github-actions`. | GitHub; the OpenSSF Dependency-Update-Tool check |
| `.github/workflows/ci.yml` | Recommended | Runs the tests and lint on every pull request so `main` stays releasable; make it a required status check. Set `permissions: contents: read` at the top. | GitHub; the OpenSSF CI-Tests and Token-Permissions checks |
| `.github/workflows/deploy-gcp.yml`, `recover-gcp.yml`, `rightsize-gcp.yml` | Added by the platform | The release, recovery and right-sizing jobs, pinned to a platform commit. Never edited by hand; "re-pin APP" regenerates them. | GitHub Actions and the controller |
| `.pre-commit-config.yaml` | Recommended | Lint, format and secret-scan before a push (ruff, gitleaks). | Developers |
| `CHANGELOG.md` | Optional | What changed per release, for people; the platform records every release by commit and digest regardless. | People |

## 2. The container: Cloud Run's contract and Docker's rules

Required:

- Listen on `0.0.0.0` at the port in `platform.yaml` (default 8080), which Cloud Run also passes as `PORT`. Nothing
  else may be assumed about the network; Cloud Run terminates TLS, the container speaks plain HTTP/1.
- Start fast and answer the health path as soon as the app can serve. The platform's startup probe hits that path;
  Cloud Run's hard limit is four minutes, the lab's expectation is a few seconds. Do heavy initialization once in
  global scope or lazily, never per request.
- Run as a non-root user with an explicit UID (`USER 65532:65532`); Cloud Run runs the container as root when the
  user does not exist. No `sudo`, no setuid binaries.
- Exec-form `CMD ["uvicorn", ...]` so the server is PID 1 and receives signals. On shutdown Cloud Run sends `SIGTERM`
  and, ten seconds later, `SIGKILL`: finish in-flight requests, flush logs and close connections within about eight
  seconds. uvicorn and gunicorn do this on their own when they are PID 1; a shell wrapper must `exec` them.
- Treat the filesystem as memory: it is in-memory, counts against the memory limit and disappears with the instance.
  Persist through the declared buckets and databases; delete temporary files.
- Stateless: no in-process sessions, no expectation that the next request reaches the same instance.
- No work after the response. The platform deploys with CPU allocated only during requests, so background threads
  are throttled and may resume unexpectedly during a later request. Batch work belongs in `needs.migrate` or a job.
- Handle every exception; a crash restarts the instance and slows startup for everyone queued behind it.

Recommended:

- Pin the base image by digest (`python:3.12-slim@sha256:...`) and let Dependabot propose refreshes; rebuild regularly
  so security fixes land. Prefer `python:*-slim`; use a multi-stage build when build tools are needed; `--no-cache-dir`;
  no extra system packages.
- `ENV PYTHONUNBUFFERED=1` so log lines appear immediately.
- Size the server to `platform.yaml`: uvicorn for an ASGI app, or gunicorn with roughly one worker per CPU and a few
  threads (`--workers 1 --threads 8 --timeout 0`); too many workers lengthen cold starts and use memory.
- Keep the image small and the dependency list short; import heavy modules lazily.

## 3. Behaviour: the twelve factors on Cloud Run

- **Config in the environment, only.** Values come from `platform.yaml` `env`, secrets arrive as environment variables
  of the same name, and every declared need arrives as `BUCKET_<NAME>`, `TOPIC_<NAME>`, `SUBSCRIPTION_<NAME>`,
  `FIRESTORE_DATABASE` or `DATABASE_INSTANCE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_IP_TYPE`. Nothing is read
  from committed files or baked into the image.
- **Backing services as attached resources.** Use Google's client libraries with default credentials: the service
  runs as its own least-privilege identity that may reach only its declared resources. No keys, no passwords;
  Postgres signs in with IAM (the identity is the user) over a private address, through the Cloud SQL Python connector
  or psycopg.
- **The health endpoint is a readiness check.** `GET health_path` answers 200 only when the app can serve, cheaply, with
  no external call that could fail or cost; it is HTTP/1 and reachable from outside. The startup probe, the release
  probes and the uptime check (every minute) all use it.
- **Logs are event streams.** Write one JSON object per line to stdout or stderr with `severity` and `message`; add
  `logging.googleapis.com/trace` built from the `X-Cloud-Trace-Context` header to nest lines under their request log.
  Never log secrets, tokens or personal data. The platform's error alert counts `ERROR` lines, and a failed candidate's
  last lines appear in the release status.
- **Concurrency is the app's promise.** `concurrency` requests (default 80) are served by one instance at once: no
  mutable global state without a lock, and memory per request times concurrency within `memory_mib`.
- **Admin tasks as one-off processes.** `needs.migrate` runs as a Cloud Run job from the same image before traffic
  moves; migrations are idempotent, compatible with the previous revision (expand first, contract later, so a
  rollback stays safe) and finish within nine minutes.
- **Disposability.** Fast startup, graceful shutdown, scale to zero.

## 4. The repository and its process

- `main` is protected: changes arrive by pull request, the CI check is required, force pushes are refused. Releases
  only ever come from `main`.
- Dependencies are pinned everywhere: exact versions in `requirements.txt`, hashes in `requirements.lock`, the base
  image by digest, GitHub Actions by commit SHA. Dependabot keeps them moving.
- No binaries and no secrets in Git. Secret values are created once by the operator with
  `gcloud secrets create <service>-NAME --data-file=-`; the repository only names them.
- `README.md`, `LICENSE` and `SECURITY.md` exist and are current.
- What the platform supplies so the repository does not have to: signed build provenance verified before release (GitHub's
  for a public repository, Cosign with the application's own Cloud KMS key for a private one),
  vulnerability scanning of every image, least-privilege workflow tokens in the three generated workflows, deploy by
  digest, recovery after runner loss, measured cost reports.

## 5. platform.yaml reference

```yaml
api_version: lab/v1
name: my-service          # 3 to 21 characters: a letter, then lowercase letters, digits, dashes; the Cloud Run service name
runtime: python-http      # the only runtime today
port: 8080
health_path: /health
resources: { cpu: 1, memory_mib: 512 }   # cpu 1 to 4, memory 512 to 4096; the lab's policy refuses more than 2 CPUs, 2048 MiB today
scaling: { minimum: 0, maximum: 2 }      # minimum 0 or 1 (1 keeps an instance warm all month); maximum up to 3 under the lab's policy
concurrency: 80           # requests one instance serves at once; higher is cheaper when the app can take it
timeout_seconds: 60       # per request, up to 300
exposure: authenticated   # programs with a Google identity token; or lab-members: people, after a Google sign-in, listed accounts only
state: stateless
env:                      # plain configuration values
  LOG_LEVEL: info
secrets: [API_KEY]        # names only; the value lives in Secret Manager as my-service-API_KEY and arrives as the env var API_KEY
tests: required           # or none, knowingly: every release then carries a warning
python: "3.12"            # the Python the tests run on: 3.11, 3.12 or 3.13; match the Dockerfile's base image
needs:
  buckets: [uploads]      # env BUCKET_UPLOADS
  topics: [events]        # env TOPIC_EVENTS and SUBSCRIPTION_EVENTS
  database: none          # none | firestore | postgres (about 9 dollars a month from the moment it exists)
  migrate: []             # e.g. [alembic, upgrade, head]: a job from the same image, before traffic moves
reviewed: true            # set by the person who reviewed the file
```

Costs the developer controls: `scaling.minimum: 0` costs nothing idle, `1` about 9 dollars a month per warm
instance; Cloud SQL about 9 a month; Firestore, buckets and topics nothing at rest within the free tiers. Every plan
shows the floor and the worst case before anything is deployed; start small and raise with the weekly report.

## 6. Checklist for the pull request that onboards a service

- [ ] `Dockerfile` builds locally with `docker build --platform linux/amd64 .`, runs as non-root, listens on 8080
- [ ] `python -m pytest` passes after installing `requirements-dev.txt`; `pip-audit -r requirements.txt` is clean
- [ ] `GET /health` answers 200 within seconds of start
- [ ] `platform.yaml` reviewed: size, scaling, timeout, env, secret names, needs; `reviewed: true`
- [ ] Logs are JSON lines with `severity` and `message`; nothing sensitive is logged
- [ ] No work after the response; nothing persisted on the container's disk
- [ ] `requirements.lock` with hashes, base image pinned by digest, Dependabot enabled
- [ ] `README.md`, `LICENSE`, `SECURITY.md`; `main` protected with the CI check required
- [ ] Secret values created by the operator with `gcloud`; none in Git

## 6a. Pages for people

An app with pages for people says `exposure: lab-members`. The platform then puts Google's Identity-Aware Proxy in front
of the service: a person opening the URL signs in with a Google account, and only the accounts the operator lists get
through (the lab's list plus the app's own list; developers ask the operator to add someone). Rules for the app:

- Never build your own login. The proxy has already authenticated the person; read who it is from the request header
  `X-Goog-Authenticated-User-Email` (the value looks like `accounts.google.com:name@example.edu`) and, when you need to
  verify it, from the signed `X-Goog-IAP-JWT-Assertion` header.
- Programs calling the app's API need a Google identity token minted for the proxy's OAuth client, not for the service
  URL; the platform's own release probes do this. Ask the operator for the client id when a program must call the app.
- Everything else (port, health path, logs, statelessness) is unchanged. The health path is reachable to the proxy's
  probe identity; a browser reaching it sees the sign-in page first.

## 7. What is not supported

Public endpoints (anyone without a sign-in), GPUs, runtimes other than Python HTTP services, stateful containers, a branch
other than `main`, production environments, and editing the three workflow files the platform adds.

## Sources

- [Cloud Run container runtime contract](https://docs.cloud.google.com/run/docs/container-contract): port and `PORT`, `0.0.0.0`, the four-minute startup limit, `SIGTERM` then `SIGKILL` after ten seconds, the in-memory filesystem, x86_64, root when the user is missing, no TLS in the container
- [Cloud Run general development tips](https://docs.cloud.google.com/run/docs/tips/general): no background work with request-based billing, delete temporary files, handle every exception, global and lazy initialization, concurrency defaults, non-root images, pinned deterministic builds
- [Optimize Python applications for Cloud Run](https://docs.cloud.google.com/run/docs/tips/python): server and worker settings, `--timeout 0`, `PYTHONUNBUFFERED`, slim images, lazy loading
- [Cloud Run health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks): startup and liveness probes, HTTP/1 endpoints, make the startup probe a readiness check
- [Cloud Run logging](https://docs.cloud.google.com/run/docs/logging): JSON lines on stdout, `severity`, `message`, `logging.googleapis.com/trace` from `X-Cloud-Trace-Context`
- [The Twelve-Factor App](https://12factor.net/): config, backing services, processes, port binding, disposability, logs, admin processes
- [Docker: building best practices](https://docs.docker.com/build/building/best-practices/): multi-stage builds, `.dockerignore`, pinned base images by digest, exec-form `CMD`, non-root `USER`, rebuild often
- [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md): Branch-Protection, CI-Tests, Code-Review, Dependency-Update-Tool, License, Pinned-Dependencies, Security-Policy, Token-Permissions, Vulnerabilities, Binary-Artifacts
- [GitHub community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file): `SECURITY.md`, `CONTRIBUTING.md`, `SUPPORT.md`, and the per-repository `LICENSE`
