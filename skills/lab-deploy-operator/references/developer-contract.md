# What a repository must contain (short version)

A condensed copy of the lab's standard (`docs/DEVELOPER-CONTRACT.md` in the platform repository,
https://github.com/brianboti/lab-deploy/blob/main/docs/DEVELOPER-CONTRACT.md). Use it to check a repository by hand,
to answer "what do developers need to give me?", and to write the "why" lines of a message for a developer. The full
page is the authority; if the two ever disagree, trust the full page and say so.

## Contents

1. What `onboard_application` checks automatically
2. The files and why each exists
3. `platform.yaml`, field by field
4. Rules for the app itself
5. Pages for people
6. Not supported
7. The short list for developers

## 1. What `onboard_application` checks automatically

It stops before creating anything when any of these fails:

- The default branch is `main` (the platform releases only from main).
- `Dockerfile` exists at the root (the build recipe).
- `platform.yaml` exists at the root (the app's contract with the platform) and is valid.
- `requirements.txt` exists (pinned runtime dependencies).
- `platform.yaml` says `reviewed: true` (a person has read it).
- Unless `platform.yaml` says `tests: none`: test files (`tests/`, `test_*.py` or `*_test.py`) and
  `requirements-dev.txt` (installs pytest for the test step).
- The `Dockerfile` has `EXPOSE <port>` matching the port in `platform.yaml` (the port Cloud Run sends requests to).

Everything else below is checked by review or recommended; the release can still fail later if the app breaks the
rules in section 4 (for example, the health check never answers).

## 2. The files and why each exists

| File | Level | Why |
|---|---|---|
| `Dockerfile` | Required | How the app is built: a Linux x86_64 image listening on `$PORT` |
| `platform.yaml` | Required | Name, port, health path, size, scaling, timeout, settings, secret names, needs, tests policy, access |
| `requirements.txt` | Required | Runtime dependencies at exact versions; the release audits them for known security holes |
| `requirements-dev.txt` | Required unless `tests: none` | Test tools (pytest), kept out of the image |
| `tests/` or `test_*.py` | Required unless `tests: none` | Prove the app works before anything is built |
| `README.md` | Required by review | What the app is, how to run it, its health path, the settings it expects |
| `LICENSE` | Required by review | The lab's license (MIT unless told otherwise) |
| `requirements.lock` | Required by review beyond a demo | Hash-locked install, so the same code builds the same way every time |
| `.dockerignore` | Recommended | Keeps `.git`, caches, tests and local secrets out of the image |
| `SECURITY.md` | Recommended | How to report a security problem privately |
| `.github/CODEOWNERS`, `.github/dependabot.yml`, `.github/workflows/ci.yml` | Recommended | Reviewers, automatic dependency updates, tests on every pull request |
| `pyproject.toml`, `.pre-commit-config.yaml`, `CHANGELOG.md` | Recommended or optional | Project settings, checks before a push, release notes |
| `deploy-gcp.yml`, `recover-gcp.yml`, `rightsize-gcp.yml` | Added by the platform | Never written or edited by the developer |

## 3. `platform.yaml`, field by field

```yaml
api_version: lab/v1
name: my-service          # 3 to 21 characters: a letter, then lowercase letters, digits, dashes
runtime: python-http      # the only runtime today
port: 8080
health_path: /health
resources: { cpu: 1, memory_mib: 512 }   # the lab allows up to 2 CPUs and 2048 MiB
scaling: { minimum: 0, maximum: 2 }      # minimum 1 keeps an instance warm all month (about 9 dollars); maximum up to 3
concurrency: 80           # requests one instance serves at once; higher is cheaper when the app can take it
timeout_seconds: 60       # per request, up to 300
exposure: authenticated   # or lab-members: people, after a Google sign-in, listed accounts only
state: stateless
env:                      # plain settings
  LOG_LEVEL: info
secrets: [API_KEY]        # names only; the operator stores the value as my-service-API_KEY
tests: required           # or none, knowingly: every release then carries a warning
needs:
  buckets: [uploads]      # arrives as BUCKET_UPLOADS
  topics: [events]        # arrives as TOPIC_EVENTS and SUBSCRIPTION_EVENTS
  database: none          # none | firestore | postgres (postgres: about 9 dollars a month from the moment it exists)
  migrate: []             # e.g. [alembic, upgrade, head]: runs before visitors move to a new version
reviewed: true            # set by the person who reviewed the file
```

## 4. Rules for the app itself

- Listens on `0.0.0.0` at the port in `platform.yaml` (Cloud Run also passes it as `PORT`); plain HTTP, no TLS.
- Answers the health path with 200 within seconds of starting, cheaply, without calling anything outside.
- Runs as a non-root user; the server is started directly (`CMD ["uvicorn", ...]`) so it shuts down cleanly.
- Keeps nothing on its own disk and no sessions in memory: the next request may reach another copy. Files go to a
  declared bucket, data to a declared database.
- Does no work after the response is sent (CPU is only given during requests); batch work goes in `needs.migrate` or a
  job.
- Reads settings only from environment variables; no keys or passwords in the code. Google services are reached with
  Google's client libraries and the app's own identity; Postgres signs in without a password.
- Writes logs as one JSON object per line with `severity` and `message`; never logs secrets or personal data.
- Database migrations are safe to run twice and still work with the previous version, so a rollback stays safe.

## 5. Pages for people

`exposure: lab-members` puts a Google sign-in in front of the app; only accounts the operator lists get through. The app
does not build its own sign-in for these people: Google has already checked who they are, and the app reads it from the
request header `X-Goog-Authenticated-User-Email` (verified, when needed, from `X-Goog-IAP-JWT-Assertion`). Programs that
call the app need a token for the sign-in's client id, which the operator provides.

## 6. Not supported

Apps anyone can open without signing in, GPUs, runtimes other than Python web services, apps that keep state in the
container, branches other than `main`, production, and editing the three workflow files the platform adds.

## 7. The short list for developers

When the operator asks "what do I tell developers?", give this, in plain words:

1. A `Dockerfile` at the root that builds the app and listens on port 8080.
2. A `platform.yaml` filled in and marked `reviewed: true` (the operator can draft it).
3. `requirements.txt` with exact versions; `requirements-dev.txt` with pytest.
4. Tests that pass with `python -m pytest`.
5. A health page (for example `/health`) that answers quickly.
6. `README.md` and `LICENSE`.
7. Work happens on `main`; no passwords or keys in the repository (send secret values to the operator separately, never
   in chat or in Git).
