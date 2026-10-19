# The repository standard: what a service must contain to be deployed by Fast Deploy

Give this page to the developer. A repository that follows it is deployed to the lab's Google Cloud staging project
with one request in Claude chat; the platform provides everything else (identities, registry, buckets, topics,
databases, monitoring, cost control, signed images, rollback). `examples/demo-app` in the platform repository is a
complete reference.

## What the platform assumes when you give it nothing

A repository can also be deployed **as it is**, with nothing added to it (since 1.15.0; `docs/ROADMAP.md` D31 to D34).
The platform reads it, and each answer below comes from the first place in its row that says it. Where reading would
turn into guessing, it refuses and names the file to fix. The contract it writes is kept in the platform repository
(`apps/<service>/platform.yaml`), and a person's merge there is the review.

| Question | Where the answer comes from, first match wins | When nothing says |
|---|---|---|
| How is the image built? | your `Dockerfile`; a `Procfile` (Google's buildpacks) | the platform's own Dockerfile, made at build time and never committed |
| Which Python? | the Dockerfile's `FROM python:X.Y`; `.python-version`; `runtime.txt`; `Pipfile.lock`; `requires-python` (3.12 when it allows it) | 3.12 |
| Which packages? | `requirements.lock`; a hashed `requirements.txt`; `uv.lock`; `poetry.lock`; `Pipfile.lock`; the pip part of `environment.yml`; `requirements.txt`; `pyproject.toml` dependencies | refused: there is no list of dependencies |
| What starts it? | the Dockerfile's `CMD`; the Procfile's `web:`; Compose's `command`; `[tool.fastapi] entrypoint`; Django's `WSGI_APPLICATION`; one `app = FastAPI()` or `Flask(...)` object, or one `create_app()` | refused: say the start command |
| Which port? | Compose `ports` (the container side); one `EXPOSE`; `--port` in the start command | 8080, passed in as `PORT` |
| Health check? | a Compose `healthcheck`; a `/health` route in the code | none: Cloud Run's TCP check, and the release says the HTTP check is off |
| Settings and needs? | names read from the code, Compose and `.env.example`, a Postgres service in Compose | names are listed for a person to fill in; values are never read |
| Tests? | pytest's configuration, a `tests/` folder, `test_*.py` files | none: said plainly, never invented |

The packages are locked by the platform when the repository has no lock: with uv, in a container of your Python, every
package pinned with its hashes (`apps/<service>/requirements.lock`); the tests get a lock of their own with pytest.

It **refuses rather than guesses** when a service has several candidate apps or frameworks, a Django project lacks
`WSGI_APPLICATION`, there is no dependency list, it needs conda-only or system packages (unless your own Dockerfile
installs them), it asks for a Python outside 3.11 to 3.15, a secret is committed (`.env`, a private key, a credentials
file), or the app is a notebook or a file over 100 MiB. A private repository is refused while the platform repository
is public, because the platform's build logs would be public too.

**A Node.js service** (since 1.20.0; `docs/ROADMAP.md` D41 to D46) is a folder with a `package.json` and no Python
manifest. Its answers come from the same kind of ladder:

| Question | Where the answer comes from, first match wins | When nothing says |
|---|---|---|
| How is the image built? | your `Dockerfile`; a `Procfile` (Google's buildpacks, for code the lab has reviewed only: they run install scripts) | the platform's own image on `node:<line>-trixie-slim` (Debian 13), made at build time and never committed |
| Which Node? | the Dockerfile's `FROM node:X`; `.nvmrc`; `.node-version`; the `nodejs` line of `.tool-versions`; `engines.node` (a range takes the highest of 22, 24 and 26 it allows); `devEngines.runtime` | the newest line in Active LTS: 24 until 2026-10-28, 26 from then |
| Which packages? | `npm-shrinkwrap.json`; `package-lock.json` (version 2 or 3), installed with `npm ci` | the platform locks `package.json` once (below) |
| What starts it? | the Dockerfile's `CMD`; the Procfile's `web:`; Compose's `command`; `scripts.start` when it is `node [flags] <file> [args]`; `main`; `server.js` or `index.js` | refused: say the start command |
| Which port? | Compose `ports` (the container side); one `EXPOSE`; `--port` in the start command; the default in the code's `listen(process.env.PORT \|\| 8787)` | 8080, passed in as `PORT` |
| Which host? | the listen call: a host read from a variable whose default is `127.0.0.1` gets `0.0.0.0` in the contract's `env` | every interface, Node's own default |
| Health check? | a Compose `healthcheck`; a `/health` or `/healthcheck` route (Express, Fastify, Koa, Hono, Hapi, or `req.url === '/health'` in a plain server), prefixed paths like `/api/health` included | none: Cloud Run's TCP check, and the release says the HTTP check is off |
| Install scripts? | the lockfile's packages that have one, kept only where `package.json`'s `allowScripts` pins them by version and the package needs it on Cloud Run | none run |
| Tests? | `scripts.test` unless it is npm's placeholder; files `node --test` finds by its own patterns | none: said plainly, never invented |

The packages are locked by the platform when the repository has no lockfile: `npm install --package-lock-only
--ignore-scripts --min-release-age=3` with npm 11.10 or later, in a container of your Node.js line on the controller's
machine (Node 22 bundles npm 10.9.9, which has no `min-release-age`, so npm 11.19.1 is installed there first, by version
and checked against its integrity hash), into `apps/<service>/package-lock.json`. Every entry must carry its integrity hash and come from the npm registry.

For Node.js it also **refuses**: a start script that watches, transpiles or chains (`nodemon`, `ts-node`, `tsx`,
`node --watch`, a development server, `concurrently`, `&&` or `sh -c`); a service that needs a build first (its start
file is not in the repository, TypeScript on Node 22, Next.js, NestJS, Remix, SvelteKit, Astro or Nuxt) unless your own
Dockerfile builds it; a listen call on a literal port that never reads `PORT`, on `127.0.0.1` or `localhost`, or Fastify
without `host: '0.0.0.0'`; a front-end site with no server; two lockfiles, or `packageManager` and `engines` naming
different managers; a `yarn.lock`, `pnpm-lock.yaml` or `bun.lock` (convert it, section 1b); a lockfile entry without an
integrity hash or from git, a tarball elsewhere, a `file:` or `link:` path, or `bundleDependencies`; a workspace at the
root (give one package's folder) or a package of one; a committed `.npmrc` or `.yarnrc.yml` with a literal token; and
`engines.iojs`. A committed `node_modules/`, `dist/` or `.next/` is set aside with a note, and a `build` script that the
start does not need (Sophia's builds a browser extension) is noted, not refused.

Following the rest of this page is still the better path: your repository then carries its own tests, lock and
workflows, and every change is reviewed in one place.

## The three things you do

1. **A web service that listens on `$PORT` and answers its health path.** Start an HTTP server on `0.0.0.0` at the port
   Cloud Run passes in `PORT` (8080 unless `platform.yaml` says otherwise), and answer `GET /health` with 200 within
   seconds of starting, without calling anything else.
2. **Pinned requirements.** `requirements.txt` lists every runtime dependency at an exact version (`fastapi==0.143.1`).
   For Node.js: `package.json` and the `package-lock.json` npm writes beside it, both committed.
3. **Tests.** At least one pytest test (a `tests/` folder or `test_*.py` files) that passes with `python -m pytest`. For
   Node.js: a `test` script that `npm test` runs, or files that `node --test` finds (`test/*.test.mjs`, say).

That is all you must write. Ask Claude *"check github.com/ORG/REPO"* to see what else is missing, then *"draft the files
for it"*: the platform opens one draft pull request with everything below, and never changes a file you already have.
Read it, set `reviewed: true` in `platform.yaml` once its values are right, merge it, and say *"deploy
github.com/ORG/REPO"*.

### What the platform writes for you

| File | What the draft holds |
|---|---|
| `Dockerfile` | Your Python's slim image pinned by digest, dependencies installed before the code, a non-root user, your server (uvicorn or gunicorn) started on `$PORT` |
| `.dockerignore` | An allow-list: only what the service needs reaches the image; tests, environments, notebooks and keys stay out |
| `requirements.lock` | Every dependency with its hashes for your Python, compiled with uv in a container of that Python |
| `requirements-dev.txt` | pytest (and httpx for an ASGI app), pinned, kept out of the image |
| `tests/test_health.py` | A test that the health path answers 200 |
| `platform.yaml` | The deployment contract, with `reviewed: false`: the one file you must read before merging |
| `.github/workflows/ci.yml` | Your tests on every pull request, with a read-only token |
| `SECURITY.md`, `.github/dependabot.yml` | How to report a vulnerability; weekly, reviewable updates of the pins |

For a Node.js service the draft holds a `Dockerfile` on your line's `node:<line>-trixie-slim` pinned by digest (`npm ci` with
every install script off, a non-root user, `node` itself as the command), a `.dockerignore` that keeps `node_modules`
out, `package-lock.json` when you have none (made as above), `test/health.test.mjs` on `node --test` (it starts the
service on a free port and asks for the health path), `platform.yaml` with `runtime: node-http`, a CI workflow on
`actions/setup-node` that installs the same way, and Dependabot for npm with a seven-day cooldown written in.

**No Dockerfile at all?** A `Procfile` with one line (`web: uvicorn app:app --host 0.0.0.0 --port $PORT`) works too:
Google's buildpacks then build the image. The images are several times larger (the demo app: about 394 MB against
52 MB from its Dockerfile, measured locally on 2026-10-15), which costs registry storage and cold-start time, so the
platform drafts a Dockerfile by default. A service whose build reads the whole repository needs a Dockerfile.

The rest of this page is the full standard, for reviewers and for anyone who writes these files by hand. It has three
levels:

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
| `requirements.txt` | Required | Runtime dependencies pinned to exact versions. | The Dockerfile's `pip install`; the audit step when there is no lock (`pip-audit -r requirements.txt` on the app's Python, a known vulnerability fails the release); the analyzer |
| `requirements-dev.txt` | Required unless `tests: none` | Test and lint tools (pytest, ruff), kept out of the image. | The test step (`pip install -r requirements-dev.txt`, then `python -m pytest`) |
| `requirements.lock` | Recommended; required by review for anything beyond a demo | A hash-locked, reproducible install (`uv pip compile --universal --python-version X.Y --generate-hashes`, or `pip-compile --generate-hashes` run on the app's Python). A version pin alone still trusts whatever the index serves. | The test step installs it with `--require-hashes` when present; the audit step reads every package in it at its exact version, on the app's Python, without installing anything; the Dockerfile should install it too |
| `tests/` or `test_*.py` | Required unless `tests: none` | Prove the app before anything is built; the same tests run in the app's own CI on pull requests. | `python -m pytest` in the deploy workflow and in the app's CI |
| `package.json`, `package-lock.json` | Required for a Node.js service, instead of the requirement files | The dependencies and every package locked with its integrity hash (section 1b). | OSV-Scanner before anything is installed; `npm ci --ignore-scripts` for the tests and `npm ci --omit=dev --ignore-scripts` in the image; then `npm test` |
| `.dockerignore` | Recommended | Keeps `.git`, virtual environments, tests, caches and local secrets out of the build context: smaller, faster, safer images. | `docker build` |
| `pyproject.toml` | Recommended | One place for project metadata and the ruff and pytest configuration; `requirements*.txt` can be generated from it. | Developers and tools |
| `README.md` | Required by review | What the service is, how to run it locally, its health path, the environment it expects. | People; the analyzer's review questions |
| `LICENSE` | Required by review | The lab's license (MIT unless told otherwise); a public repository without one cannot be reused. | People; the OpenSSF License check |
| `SECURITY.md` | Recommended | How to report a vulnerability privately. | People; the OpenSSF Security-Policy check |
| `.github/CODEOWNERS` | Recommended | Who reviews what; branch protection can require their review. | GitHub |
| `.github/dependabot.yml` | Recommended | Keeps pins current through reviewable pull requests for `pip`, `docker` and `github-actions`. | GitHub; the OpenSSF Dependency-Update-Tool check |
| `.github/workflows/ci.yml` | Recommended | Runs the tests and lint on every pull request so `main` stays releasable; make it a required status check. Set `permissions: contents: read` at the top. | GitHub; the OpenSSF CI-Tests and Token-Permissions checks |
| `.github/workflows/deploy-gcp.yml`, `recover-gcp.yml`, `rightsize-gcp.yml` | Added by the platform | The release, recovery and right-sizing jobs, pinned to a platform commit. Never edited by hand; "re-pin APP" regenerates them. A second service in the same repository gets its own three, named after it (`deploy-gcp-admin.yml`, section 4a). | GitHub Actions and the controller |
| `.pre-commit-config.yaml` | Recommended | Lint, format and secret-scan before a push (ruff, gitleaks). | Developers |
| `CHANGELOG.md` | Optional | What changed per release, for people; the platform records every release by commit and digest regardless. | People |

## 1a. Python versions and package managers

**Python.** The running service uses whatever Python its Dockerfile's base image has. The release's tests and its
dependency audit run on the version `platform.yaml` names under `python` (3.11 to 3.15, default 3.12); keep the two
the same, and the analyzer drafts it from the Dockerfile. 3.15 shipped in October 2026: before moving to it, check that
every compiled dependency publishes 3.15 wheels, or the install falls back to building from source. Python 3.10 and
older are not supported (3.10's security support ends in October 2026).

**Lock files.** A lock compiled on one Python can leave out packages another one needs (live: SQLAlchemy needs greenlet
below 3.13, so a lock compiled on 3.13 failed to install on 3.12). Compile it for every version from the one in
`platform.yaml` up, with markers (`--python-version` is that version; without it uv starts from the Python on your
machine):

```bash
uv pip compile requirements.txt --universal --python-version 3.12 --generate-hashes -o requirements.lock
```

**conda, Poetry and uv.** The platform installs with pip from three files: `requirements.txt` (exact pins),
`requirements.lock` (the same with hashes) and `requirements-dev.txt` (test tools). Keep your manager for development
and export these files whenever the dependencies change; commit them with the change.

- **uv** (a `uv.lock`):

  ```bash
  uv export --format requirements.txt --no-dev --no-hashes --no-emit-project -o requirements.txt
  uv export --format requirements.txt --no-dev --no-emit-project -o requirements.lock
  uv export --format requirements.txt --only-dev --no-hashes --no-emit-project -o requirements-dev.txt
  ```

- **Poetry** (2.x, with the export plugin: `poetry self add poetry-plugin-export`, or declare it under
  `[tool.poetry.requires-plugins]`):

  ```bash
  poetry export -f requirements.txt --without-hashes --output requirements.txt
  poetry export -f requirements.txt --output requirements.lock
  poetry export -f requirements.txt --only dev --without-hashes --output requirements-dev.txt
  ```

- **conda** (an `environment.yml`): the service must install from PyPI, so a package that exists only on a conda channel
  needs a PyPI equivalent first. In the activated environment, list the Python packages with plain `name==version`
  lines (`pip freeze` would print local `file://` paths for conda-installed packages), remove what only notebooks or
  development need, then lock:

  ```bash
  python -m pip list --format=freeze --exclude-editable > requirements.txt
  uv pip compile requirements.txt --universal --python-version 3.12 --generate-hashes -o requirements.lock
  ```

  and list pytest and the other test tools in `requirements-dev.txt`. The Dockerfile then uses a `python:*-slim` base
  and installs `requirements.lock`, like any other service.

## 1b. Node.js lines, packages and install scripts

**The line.** `platform.yaml` says `runtime: node-http` and `node: "22"`, `"24"` or `"26"`, the lines in long-term support
(20 and the odd-numbered lines are refused). The tests, the dependency audit and the platform's image use it; keep your
Dockerfile's `FROM node:X` the same. A line is refused from its end of life, and the dates are in the runbook: 22 on
2027-04-30, 24 on 2028-04-30, 26 on 2029-04-30. The newest line in Active LTS is the default for a new contract (24 until
2026-10-28, 26 from then).

**The lockfile.** npm only in this version: `package-lock.json` (version 2 or 3) or `npm-shrinkwrap.json`, committed with
`package.json` and installed with `npm ci`, which installs exactly the lockfile and stops when the two disagree. Every
entry must carry its `integrity` hash and come from `https://registry.npmjs.org/`; a git dependency, a tarball from
elsewhere, a `file:` or `link:` path and `bundleDependencies` are refused, because `npm ci` installs them without checking
them against the registry. The platform reads no `.npmrc`: it installs from the public registry with npm's own settings.

**Install scripts never run at install.** Every npm attack of 2025 and 2026 ran from a package's install script, so the
platform installs with `npm ci --ignore-scripts` on every npm, then runs the scripts of the packages `platform.yaml` lists
under `build.scripts`, each by name and exact version, and nothing else:

```yaml
build:
  scripts: [sharp@0.34.4]   # npm rebuild sharp@0.34.4 after the install; a name alone, a range or --all is refused
```

The reader drafts that list from the lockfile's packages that have a script, keeping only those your `package.json`
approves by version in its `allowScripts` field (`npm install-scripts approve` writes them, from npm 11.16), and only when
the package needs its script on Cloud Run. It is often empty: a package that ships its native binary inside its tarball
needs no script (onnxruntime-node's script only downloads GPU libraries; protobufjs's only prints a warning). Your own
`package.json`'s `preinstall`, `postinstall` and `prepare` never run either; `npm test` runs your `pretest` and `test`.

**The dependency audit.** Before anything is installed, OSV-Scanner 2.7.0 (pinned by version and checksum) reads the
lockfile against the OSV database, which carries the malicious-package records (`MAL-`) of the OpenSSF and GitHub's
malware advisories (embedded malicious code, CWE-506): either is a known malicious package. For every service,
reviewed or not, a known malicious package, a critical or high finding with a fixed version, an audit that did not finish,
or a lockfile entry from outside the registry stops the release; everything else is listed in the run's summary and in
`dependency-audit.json` in the release bundle. `npm audit signatures` then checks the registry's signatures of what was
installed, as a note. `npm audit` is not the gate: it needs the registry, and whether it returns malware advisories is
not documented.

**Cooldowns.** Most malicious versions were withdrawn within one to four hours, a worm's within days. Dependabot waits
three days by default before it proposes a version; the Dependabot file the platform drafts writes seven, as it does for
pip and Docker. npm 11.10 and later have `min-release-age` (in days) for your own installs, and the lock the platform makes
uses three.

**Other package managers** come later (a roadmap item). Until then, convert a pnpm, Yarn or Bun lockfile with npm 11.10 or
later (Node 24 or 26 bundle it), from the service's folder, then review the lockfile's diff and run the tests:

```bash
rm -rf node_modules && npm install --package-lock-only --ignore-scripts --min-release-age=3
```

and remove the old file in the same commit (`git rm yarn.lock`, `pnpm-lock.yaml` or `bun.lock`) and any `packageManager`
field that names another manager. npm follows a Yarn 1 `yarn.lock` where it can; for the others it resolves the versions
again from `package.json`'s ranges.

**What does not need a build.** The platform's image runs your code as committed: plain JavaScript, or TypeScript under
Node's type stripping on 24 and 26 (`node src/index.ts`; enums and namespaces need `--experimental-transform-types`).
A service that must be compiled or bundled first (`tsc` into `dist/`, Next.js, NestJS) commits the built output, or
builds it with a Dockerfile of its own; a build stage in the platform's image is a later roadmap item.

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

For a Node.js service (the platform's own image does the first three):

- Start `node` itself: `CMD ["node", "server.js"]`, never `npm start`. npm adds latency and does not pass `SIGTERM` on,
  and in Cloud Run's second-generation environment the command is the container's first process, with no `--init`.
- Handle `SIGTERM` yourself, or requests in flight fail when the instance stops:
  `process.on('SIGTERM', () => server.close(() => process.exit(0)))`. The reader notes a server without one.
- Listen on every interface: `server.listen(process.env.PORT || 8080)` does; Fastify needs `host: '0.0.0.0'`, and a
  host read from a variable must not default to `127.0.0.1` in the container (the reader sets it to `0.0.0.0`).
- Memory: Node sizes its heap from the instance's memory limit, half of it between 256 MiB and 4 GiB, so the default
  512 MiB gives a 256 MiB heap. Raise `resources.memory_mib` when the service needs more (Google's buildpacks give the
  heap 80 percent; `NODE_OPTIONS=--max-old-space-size=400` under `env` does the same). Buffers live outside the heap, and
  an instance over its limit is stopped with "Memory limit exceeded".
- Use a Debian image (`node:<line>-trixie-slim`), not Alpine, when a dependency carries native code: Alpine's musl does not load
  binaries built for glibc. The Dockerfile policy notes both an `npm start` and an Alpine image with native code.
- The platform's image keeps no package manager: after `npm ci`, npm, npx, Corepack and Yarn are removed, so a service
  that runs `npm` while it serves fails there (start child processes with `process.execPath`, which is `node`). The
  official `node:<line>-slim` of Node 22 and 24 is Debian 12; on 2026-10-19 its perl carried five critical findings with
  a fix the image had not taken, and npm's own bundled libraries three high ones, so the vulnerability gate stopped every
  service nobody had reviewed. A Dockerfile of your own on that image is stopped the same way: build on
  `node:<line>-trixie-slim` and remove the package managers after the install.
- Node keeps idle connections open for five seconds (`keepAliveTimeout`); Google documents no idle timeout for Cloud Run's
  front end, so the platform prescribes no other number.

Recommended:

- Pin the base image by digest (`python:3.12-slim@sha256:...`) and let Dependabot propose refreshes; rebuild regularly
  so security fixes land. Prefer `python:*-slim`; use a multi-stage build when build tools are needed; `--no-cache-dir`;
  no extra system packages.
- `ENV PYTHONUNBUFFERED=1` so log lines appear immediately.
- Size the server to `platform.yaml`: uvicorn for an ASGI app, or gunicorn with roughly one worker per CPU and a few
  threads (`--workers 1 --threads 8 --timeout 0`); too many workers lengthen cold starts and use memory.
- Keep the image small and the dependency list short; import heavy modules lazily.
- Order the Dockerfile so what changes least comes first: the base image, then system packages, then
  `COPY requirements.lock` and the install, and only then `COPY` the code. A code change then rebuilds one layer instead of
  reinstalling every dependency.
- Keep it clean for hadolint, which the build job runs on every release (version 2.15.1, the same rules as
  `hadolint Dockerfile` on your machine or `docker run --rm -i hadolint/hadolint:v2.15.1 < Dockerfile`). Findings are
  annotations on the run, and error-level findings stop the release (warnings do not).

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
- **5xx means the server failed.** After a release takes over, the platform watches its real requests for five minutes
  (the bake) and brings the previous version back if it crashes, if more than 5% of at least twenty requests answer 5xx,
  or if it is more than three times slower than the version before. Answer a client's mistake with a 4xx, never a 5xx,
  or a release can be taken back for a bad request. Requests to the health path are not counted.

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

## 4a. Several services in one repository

A repository may hold more than one service, each in its own folder with its own `Dockerfile`, `platform.yaml`,
requirement files and tests (the files of section 1, inside the folder):

```text
services/api/      Dockerfile  platform.yaml  requirements.txt  requirements-dev.txt  tests/
services/admin/    Dockerfile  platform.yaml  requirements.txt  requirements-dev.txt  tests/
lib/               shared code, when a service needs it
compose.yaml       optional, for local development only
```

- **One sentence per service.** "Deploy https://github.com/lab/mono, the service in services/api" onboards one; the
  folder is the `path` of `onboard_application`. The service name comes from `platform.yaml`, the application id from
  the folder's last part unless you give one.
- **Names.** The first service of a repository keeps the plain names (`deploy-gcp.yml`, the `staging` environment);
  every further one gets its own, suffixed with its service: `deploy-gcp-admin.yml`, `recover-gcp-admin.yml`,
  `rightsize-gcp-admin.yml` and the environment `staging-admin`. Each service has its own identities, trusted only from
  its own workflows in its own environment, so one service's workflow can never release another.
- **The build context.** By default the service's folder is the Docker build context: the Dockerfile copies paths
  relative to the folder and nothing outside it is visible. When a service copies shared code from elsewhere in the
  repository (`COPY lib/ ./lib/`), say so in its `platform.yaml`:

  ```yaml
  build:
    context: repository   # the whole repository is the build context; the Dockerfile stays in the service's folder
  ```

  Paths in that Dockerfile are then relative to the repository's root (`COPY services/admin/app.py .`), and the
  service's tests run with the repository's root on Python's import path, so `import lib.shared` works in both. Docker reads
  the `.dockerignore` at the root of the context; give the service its own `services/admin/Dockerfile.dockerignore`
  (BuildKit prefers a `<Dockerfile>.dockerignore` beside the Dockerfile), listing `.git`, virtual environments, the
  other services' folders and anything secret, so the build sends only what the image needs.
- **Releases.** Each service is released on its own. With `auto_deploy`, a commit that changes nothing in a service's
  folder (or its workflows) does not release it again, unless its build reads the whole repository.
- **Compose.** A `compose.yaml` at the root is read as a description of the parts, never deployed (section 4b).

## 4b. Where each part of an application goes

Cloud Run runs one container per service and bills the default way only while a request is being served, so a database,
cache or worker inside the app's container (or beside it as a sidecar) loses its data with the instance and gets no CPU
between requests. Each part has one place:

| Part of the application | Where it goes | Why |
|---|---|---|
| Each component that answers HTTP (API, admin pages, a second service) | Its own service with its own `platform.yaml`, in its own folder (section 4a) | Each scales to zero and is billed on its own |
| Postgres, Firestore, files, topics | `needs` in `platform.yaml` | The platform creates, backs up and connects them |
| Cache or session store (Redis, memcached) | None by default: keep it in the process's memory or in Firestore. A managed cache never scales to zero (tens of dollars a month), so it is the operator's decision, not the contract's | Postgres with Redis is the most common pairing in real apps; the answer must be explicit |
| Background worker or queue consumer (Celery, RQ, Dramatiq) | A topic need with a push subscription to a request-billed service, or a Cloud Run job for batches | Pull workers need an always-on instance, which the platform does not run |
| Front end for lab members | Build it and serve the built files from the API's own container (for example FastAPI's `StaticFiles`) with `exposure: lab-members` | No extra service and the same sign-in; a separate public site is not offered |
| Proxy, telemetry agent | Not needed: Cloud Run terminates TLS and collects logs and metrics | Sidecars may come later for these only |

**Postgres sign-in.** The default (`database_auth: iam`) has the service sign in as its own Google identity through the
Cloud SQL Python Connector: no password exists anywhere, which is what Google recommends. Code that only speaks a
connection URL (SQLAlchemy's `create_engine(os.environ["DATABASE_URL"])`, Django's `dj-database-url`, many ORMs) says
`database_auth: password`: the platform creates a database user `app` with a generated password and hands the service
`DATABASE_URL` (`postgresql://app:...@PRIVATE-IP:5432/app?sslmode=require`) as a secret. Terraform generates the
password in each run and writes it once through write-only arguments, so it is never in Terraform's state, a plan, the
repository or the chat.

**Postgres in production: no SLA unless you ask for it.** Staging always runs the smallest instance. In production the
database is recoverable to any moment of the last seven days (point-in-time recovery, beside a week of daily backups), and
`database_tier` chooses its shape:

| `database_tier` | In production | Floor a month | Covered by Google's SLA |
|---|---|---|---|
| `standard` (default) | `db-f1-micro`, one zone | about $8.57 | no (shared core) |
| `ha` | `db-f1-micro` with a standby in a second zone | about $17.13 | no (shared core) |
| `sla` | 1 dedicated vCPU and 3.75 GB, with a standby | about $100.43 | yes, 99.95 percent |

Every promotion shows the chosen tier's floor in its approval block. **Migrations** run with
`PGOPTIONS=-c lock_timeout=10s`: a statement that waits more than ten seconds for a lock fails, and the release stops
before anything new serves, instead of freezing the table behind it. Drivers built on libpq (psycopg) honor it; set your
own `PGOPTIONS` under `env` to change it. Write migrations that only add (columns, tables, indexes) and remove things in
a later release, so the previous version keeps working while traffic moves.

## 5. platform.yaml reference

```yaml
api_version: lab/v1
name: my-service          # 3 to 21 characters: a letter, then lowercase letters, digits, dashes; the Cloud Run service name
runtime: python-http      # or node-http, with node instead of python (section 1b)
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
python: "3.12"            # the Python the tests and the audit run on: 3.11 to 3.15; match the Dockerfile's base image
# node: "24"              # node-http only, instead of python: "22", "24" or "26", each refused from its end of life
needs:
  buckets: [uploads]      # env BUCKET_UPLOADS
  topics: [events]        # env TOPIC_EVENTS and SUBSCRIPTION_EVENTS
  database: none          # none | firestore | postgres (about 9 dollars a month from the moment it exists)
  database_auth: iam      # postgres only: iam (no password anywhere) or password (DATABASE_URL arrives as a secret; section 4b)
  database_tier: standard # postgres only, production only: standard, ha (a standby) or sla (a standby and a dedicated CPU)
  migrate: []             # e.g. [alembic, upgrade, head]: a job from the same image, before traffic moves
build:
  context: service        # service (the folder holding platform.yaml) or repository (shared code in a monorepo; section 4a)
  network: default        # or none: the image builds with no network at all, for dependencies vendored in the repository
  scripts: []             # node-http only: packages whose install scripts run after npm ci --ignore-scripts, as name@version
trust: reviewed           # who has read the code: reviewed (the lab) or unknown (nobody); unset, reviewed here (section 5a)
egress: internet          # where it may connect: internet, or private (Google's APIs and the lab's network only); unset, by trust
domain: api.example.org   # production only: a name of the lab's own, through a load balancer with Cloud Armor, about $25 a month (section 6b)
reviewed: true            # set by the person who reviewed the file
```

### 5a. Code nobody has reviewed

A repository taken as it is (its contract kept in the platform repository) is code nobody has reviewed: `trust:
unknown` unless its contract says `trust: reviewed`, which only a person can merge (docs/ROADMAP.md D35). Since 1.16.0
the platform treats it as follows; the lab's own code keeps everything but the first two rules as reports:

| | Code nobody has reviewed | The lab's own code |
|---|---|---|
| Dockerfile rules: no `curl \| sh`, no `sudo`, no `ADD` of a URL without `--checksum`, no disabled TLS or signature checks, no `RUN --security` or `--network=host`, no copied `.env`, keys or `.git` | refused, with the line and the fix | refused |
| A final image running as root (no `USER`, or `USER root`) | refused | reported |
| A key that works right now, found in the files or in the built image (TruffleHog checks each one with its provider) | stops the build | reported |
| Fixable critical or high vulnerabilities, or a scan that did not finish | stop the release | reported (the operator's switch blocks critical ones) |
| A known malicious package | stops the release | stops the release |
| Where it may connect | Google's APIs and the lab's private network, never the internet, unless `egress: internet` | anywhere |
| Where it runs | Cloud Run's second-generation environment (a microVM) | Cloud Run's default |

Every image also gets a parts list (a CycloneDX SBOM), attested next to its provenance, and no release ever uses a build
cache. A repository without a `.dockerignore` gets the platform's deny-list for its build (never committed), and a build
context over 1 GiB is refused: data belongs in a bucket.

Costs the developer controls: `scaling.minimum: 0` costs nothing idle, `1` about 9 dollars a month per warm
instance; Cloud SQL about 9 a month; Firestore, buckets and topics nothing at rest within the free tiers. Every plan
shows the floor and the worst case before anything is deployed; start small and raise with the weekly report.

## 6. Checklist for the pull request that onboards a service

- [ ] `Dockerfile` builds locally with `docker build --platform linux/amd64 .`, runs as non-root, listens on 8080
- [ ] `python -m pytest` passes after installing `requirements-dev.txt`; `pip-audit --disable-pip -r requirements.lock` (or `pip-audit -r requirements.txt`) is clean on the app's Python
- [ ] For Node.js: `npm ci --ignore-scripts && npm test` passes on the line in `platform.yaml`; `osv-scanner scan source --lockfile package-lock.json` is clean; the server handles `SIGTERM`
- [ ] `GET /health` answers 200 within seconds of start
- [ ] `platform.yaml` reviewed: size, scaling, timeout, env, secret names, needs; `reviewed: true`
- [ ] Logs are JSON lines with `severity` and `message`; nothing sensitive is logged
- [ ] `hadolint Dockerfile` reports no errors; client mistakes answer 4xx, never 5xx
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

## 6b. A name of the lab's own

Production answers at its `run.app` address. `domain: api.example.org` (production only; staging ignores it) adds a name
of the lab's own: a global load balancer with a Google-managed certificate, behind Cloud Armor, which answers 429 to any
one address that sends more than 600 requests a minute. Plain HTTP redirects to HTTPS, and TLS before 1.2 is refused.
Who may call is unchanged: programs send the same Google identity token, minted for `https://api.example.org` or for the
`run.app` address, and `lab-members` apps keep Identity-Aware Proxy, which Cloud Run enforces on every way in. The
`run.app` address keeps answering behind the same sign-in, because the release probes it there.

It costs about $25.25 a month whether or not anyone visits ($18.25 for the load balancer, $7 for Cloud Armor), plus $0.75
per million requests and $0.008 per GiB each way. That is more than a $20 monthly budget by itself, so the operator raises
production's budget first (docs/RUNBOOK.md, "A custom domain"), and every promotion shows it in its approval block. The
operator points the domain's A record at the address production's root prints; the certificate follows within about an
hour. An application with its own workflows needs them at 1.18.9 or later before its `platform.yaml` names a domain: an
older runner refuses a field it does not know. To stop paying, remove the line; the next apply of production's root
removes the load balancer.

## 7. What is not supported

Public endpoints (anyone without a sign-in), GPUs, runtimes other than Python and Node.js HTTP services, stateful
containers, a branch other than `main`, reaching production other than by promotion, editing the workflow files the
platform adds, several containers in one service, and deploying a Compose file as such. For Node.js, not yet (later
roadmap items): pnpm, Yarn and Bun lockfiles, a build step in the platform's image, workspaces, and Node's permission model.

## Sources

- [Cloud Run container runtime contract](https://docs.cloud.google.com/run/docs/container-contract): port and `PORT`, `0.0.0.0`, the four-minute startup limit, `SIGTERM` then `SIGKILL` after ten seconds, the in-memory filesystem, x86_64, root when the user is missing, no TLS in the container
- [Cloud Run general development tips](https://docs.cloud.google.com/run/docs/tips/general): no background work with request-based billing, delete temporary files, handle every exception, global and lazy initialization, concurrency defaults, non-root images, pinned deterministic builds
- [Optimize Python applications for Cloud Run](https://docs.cloud.google.com/run/docs/tips/python): server and worker settings, `--timeout 0`, `PYTHONUNBUFFERED`, slim images, lazy loading
- [Cloud Run health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks): startup and liveness probes, HTTP/1 endpoints, make the startup probe a readiness check
- [Cloud Run logging](https://docs.cloud.google.com/run/docs/logging): JSON lines on stdout, `severity`, `message`, `logging.googleapis.com/trace` from `X-Cloud-Trace-Context`
- [The Twelve-Factor App](https://12factor.net/): config, backing services, processes, port binding, disposability, logs, admin processes
- [Docker: building best practices](https://docs.docker.com/build/building/best-practices/): multi-stage builds, `.dockerignore`, pinned base images by digest, exec-form `CMD`, non-root `USER`, rebuild often
- [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md): Branch-Protection, CI-Tests, Code-Review, Dependency-Update-Tool, License, Pinned-Dependencies, Security-Policy, Token-Permissions, Vulnerabilities, Binary-Artifacts
- [Cloud Run sidecars](https://docs.cloud.google.com/run/docs/deploying#sidecars) and [billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings): with request-based billing, sidecars get CPU only while a request is served or the instance starts
- [Cloud SQL IAM database authentication](https://docs.cloud.google.com/sql/docs/postgres/iam-authentication) and [built-in users](https://docs.cloud.google.com/sql/docs/postgres/users): the IAM path Google recommends, and the password path for URL-only code
- [Docker: .dockerignore files](https://docs.docker.com/build/concepts/context/#dockerignore-files): a `<Dockerfile>.dockerignore` beside the Dockerfile takes precedence over the context root's
- [GitHub community health files](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file): `SECURITY.md`, `CONTRIBUTING.md`, `SUPPORT.md`, and the per-repository `LICENSE`
- [Optimize Node.js applications for Cloud Run](https://docs.cloud.google.com/run/docs/tips/nodejs): start with `node`, not `npm start`; startup and memory
- [Node.js release schedule](https://github.com/nodejs/Release) and [Docker and Node.js best practices](https://github.com/nodejs/docker-node/blob/main/docs/BestPractices.md): the LTS lines and their end-of-life dates; `NODE_ENV=production`, a non-root user, exec-form `CMD`, signals and PID 1, slim over Alpine for native code
- npm's documents for [`npm ci`](https://docs.npmjs.com/cli/v11/commands/npm-ci), [`npm rebuild`](https://docs.npmjs.com/cli/v11/commands/npm-rebuild) and [its configuration](https://docs.npmjs.com/cli/v11/using-npm/config) (`ignore-scripts`, `min-release-age`), and npm's changelog for `allowScripts`
- [OSV-Scanner](https://google.github.io/osv-scanner/): lockfile scanning, JSON output and exit codes; the OpenSSF malicious-packages records it carries
- `docs/RESEARCH-2026-10-19.md` section 4, the review this part of the contract comes from
