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
| Which Python? | the Dockerfile's `FROM python:X.Y`; `.python-version`; `runtime.txt`; `Pipfile.lock`; `requires-python` (3.12 when it allows it). A version file naming a line at its end of life (3.10 and older) gets 3.11, the lowest supported line, with a note saying the date (since 1.22.0, D48); a Dockerfile's own `FROM` is yours and stays refused | 3.12 |
| Which packages? | `requirements.lock`; a hashed `requirements.txt`; `uv.lock`; `poetry.lock`; `Pipfile.lock`; `pdm.lock`; `pylock.toml`; the pip part of `environment.yml`; `requirements.txt` (its `-r` includes followed inside the folder; since 1.22.0) or the `requirements/` folder's production, base or only file; `pyproject.toml` dependencies; then, locked by the platform: `[tool.poetry.dependencies]`, a `Pipfile`, `setup.cfg`'s or `setup.py`'s `install_requires` (read as a literal list, never run) | refused: there is no list of dependencies |
| What starts it? | the Dockerfile's `CMD`; the Procfile's `web:`; Compose's `command`; `[tool.fastapi] entrypoint`; Django's `WSGI_APPLICATION` or `ASGI_APPLICATION`, else its `<package>/wsgi.py` or `asgi.py` beside `manage.py`; the application object or factory the frameworks' own discovery would pick first (`main.py` before `app.py`, `app` before `api`; FastAPI, Starlette, Quart, Litestar, Falcon, Flask, Bottle); then the dependency that names the framework (D50, since 1.22.0): Streamlit, Gradio, Chainlit, Dash, Panel, Shiny, NiceGUI and Reflex with their own commands and variables; FastAPI, Flask and the others with `uvicorn` or `gunicorn` on `main:app` by name at any depth; aiohttp (a factory under `python -m aiohttp.web`, an object under gunicorn's worker, else `python main.py`), Sanic and Tornado; last, `python main.py` (or `app.py`, `server.py`, `run.py`) with `HOST=0.0.0.0` | refused: say the start command |
| Which port? | Compose `ports` (the container side); one `EXPOSE`; `--port` in the start command; a literal in the entry file's `run(port=N)`, `launch(server_port=N)` or `--port` default (since 1.22.0) | 8080, passed in as `PORT` |
| System packages? | `apt.txt`, `Aptfile` or `packages.txt`, installed in the platform's image, with a compiler at hand in a build stage (since 1.22.0) | none |
| Health check? | a Compose `healthcheck`; a `/health` route in the code | none: Cloud Run's TCP check, and the release says the HTTP check is off |
| Settings and needs? | names read from the code, Compose and `.env.example`, a Postgres service in Compose | names are listed for a person to fill in; values are never read |
| Tests? | pytest's configuration, a `tests/` folder, `test_*.py` files; a suite that names a browser, a database URL or Docker (Playwright, Selenium, `DATABASE_URL`, testcontainers) is drafted `tests: report`: run and counted, never a stop (since 1.24.0, D54) | none: said plainly, never invented |
| Runs as? | your Dockerfile's `USER`; for code nobody has reviewed whose image runs as root, `build.user: 65532`: the platform builds your Dockerfile, then a stage on top sets that user (since 1.24.0, D52) | the platform's images run as 65532 |

The packages are locked by the platform when the repository has no lock: with uv, in a container of your Python, every
package pinned with its hashes (`apps/<service>/requirements.lock`); the tests get a lock of their own with pytest. A
command the dependency decided (a Streamlit app, `python main.py`) is a guess the gates make safe: the first revision's
log says whether it serves, and `build.entrypoint` in the review overrides it. A Django project gets `collectstatic` at
build, and the review offers `needs.migrate`.

It **refuses rather than guesses** when two candidate apps rank the same (two `app` objects in files of the same name
and depth), a Django project has neither `WSGI_APPLICATION` nor one `wsgi.py` beside `manage.py`, there is no dependency
list (or a `requirements/` folder holds one file per hardware and names none as the base), it needs conda-only packages
(unless your own Dockerfile installs them), its own Dockerfile builds on a Python outside 3.11 to 3.15, a secret is
committed (`.env`, a private key, a credentials file; `.env.local.example` and the other documented examples are not
secrets), or a file is over 100 MiB. A notebook beside the code is a note, never the refusal. A private repository is
refused while the platform repository is public, because the platform's build logs would be public too. The repository
is read from its own default branch, whatever its name (`master` included, since 1.22.1): the contract pins a commit of
it, and a release moves that commit to the branch's head; only a repository that takes the platform's own workflows
needs `main`. A root whose
services live in folders names them in every refusal; *"deploy everything in REPO"* (`onboard_application` with `all`)
opens one contract per folder (D56), and a root with one service in a folder takes that folder without asking.

**A Node.js service** (since 1.20.0; `docs/ROADMAP.md` D41 to D46) is a folder with a `package.json` and no Python
manifest. Its answers come from the same kind of ladder:

| Question | Where the answer comes from, first match wins | When nothing says |
|---|---|---|
| How is the image built? | your `Dockerfile`; a `Procfile` (Google's buildpacks, for code the lab has reviewed only: they run install scripts) | the platform's own image on `node:<line>-trixie-slim` (Debian 13), made at build time and never committed; with a build stage when the service builds first (since 1.23.0, section 1b): the development dependencies, the `build` script or the framework's command, then the production dependencies alone |
| Which Node? | the Dockerfile's `FROM node:X`; `.nvmrc`; `.node-version`; the `nodejs` line of `.tool-versions`; `engines.node` (a range takes the highest of 22, 24 and 26 it allows); `devEngines.runtime`. A version file or range naming only a line at its end of life (16, 18, 20, the odd lines) gets 22, the lowest supported line, with a note saying the date (since 1.22.0, D48); a Dockerfile's own `FROM` is yours and stays refused | the newest line in Active LTS: 24 until 2026-10-28, 26 from then |
| Which packages? | `npm-shrinkwrap.json`; `package-lock.json` (version 2 or 3, up to 5 MB), installed with `npm ci`; `pnpm-lock.yaml`, `yarn.lock` (Yarn 1, or Yarn 2 and later with `nodeLinker: node-modules`) or `bun.lock`, each installed by its own manager, frozen and with every install script off (since 1.23.0, section 1b); a workspace member without a lockfile of its own installs from the root's; an npm lockfile that disagrees with `package.json` is set aside and `package.json` locked again by the platform (since 1.22.0) | the platform locks `package.json` once with npm (below) |
| What starts it? | the Dockerfile's `CMD`; the Procfile's `web:`; Compose's `command`; `scripts.start` when it is `node [flags] <file> [args]`, or `nodemon`, `node-dev`, `pm2` or `forever` on a file (rewritten to `node <file>`, since 1.22.0; a pm2 ecosystem file in JSON names the file), or a start the build replaces (since 1.23.0: `next start`, `nest start`, `remix-serve`, `react-router-serve`, `tsx` and `ts-node` become node on the built file; `npm run build && node FILE` builds first); `main`; then `server.js`, `index.js`, `app.js`, `main.js`, `src/index.js`, `server/index.mjs` and the rest of the entry list; last, a framework's documented start after its build (Next.js, NestJS, React Router, Remix, SvelteKit with adapter-node, Nuxt, Astro with its Node adapter, AdonisJS) | refused: say the start command |
| Which port? | Compose `ports` (the container side); one `EXPOSE`; `--port` in the start command; the code's `listen(process.env.PORT \|\| 8787)`, or its literal `listen(3000)` even when `PORT` is never read (since 1.22.0: Cloud Run's `--port` sets the container port and `PORT` alike) | 8080, passed in as `PORT` |
| Which host? | the entry file's listen call and the modules it imports, never a helper script: a host read from a variable whose default is `127.0.0.1` gets `0.0.0.0` in the contract's `env`; `HOST`, `HOSTNAME` or `NITRO_HOST` are set to `0.0.0.0` for the frameworks that read them (Next.js, Astro, Nuxt, the serve commands of React Router and Remix) | every interface, Node's own default |
| Health check? | a Compose `healthcheck`; a `/health` or `/healthcheck` route (Express, Fastify, Koa, Hono, Hapi, or `req.url === '/health'` in a plain server), prefixed paths like `/api/health` included | none: Cloud Run's TCP check, and the release says the HTTP check is off |
| Install scripts? | the platform's own table of packages whose script is needed on Cloud Run (`sharp` below 0.33, `bcrypt` below 5.1, `canvas`, `sqlite3`, `better-sqlite3`, `argon2`, `re2`, `node-pty`, `@parcel/watcher`, `@tensorflow/tfjs-node`), drafted at the locked version for the merge to approve (since 1.23.0, D53); then what `package.json` approves for its manager (`allowScripts` by version; pnpm's `onlyBuiltDependencies`, Yarn's `dependenciesMeta` `built`, Bun's `trustedDependencies` by name, joined with the locked version), less those that ship their binary anyway | none run |
| Tests? | `scripts.test` unless it is npm's placeholder; files `node --test` finds by its own patterns; a suite that names Playwright, Cypress, `db:seed`, Docker or a database URL is drafted `tests: report` (since 1.24.0) | none: said plainly, never invented |
| Bound to the loopback? | a listen call on `127.0.0.1` or `localhost` with no variable for it, or Fastify without `host`, gets the platform's proxy sidecar (`proxy: true`, since 1.24.0, D51): Nginx, pinned by digest, takes the requests on the ingress port and forwards them on the loopback the two containers share, billed as the app's while serving | no proxy |
| A site with no server? | a Vite, Create React App, Vue CLI, Parcel, Astro, Gatsby, Docusaurus or Eleventy site, or SvelteKit with adapter-static, with a `build` script: `runtime: static`, built by the platform and served by Caddy behind IAP (`exposure: lab-members`; since 1.24.0, D55) | refused |

The packages are locked by the platform when the repository has no lockfile: `npm install --package-lock-only
--ignore-scripts --min-release-age=3` with npm 11.10 or later, in a container of your Node.js line on the controller's
machine (Node 22 bundles npm 10.9.9, which has no `min-release-age`, so npm 11.19.1 is installed there first, by version
and checked against its integrity hash), into `apps/<service>/package-lock.json`. Every entry must carry its integrity hash and come from the npm registry.

For Node.js it also **refuses**: a start script that chains or watches (`node --watch`, a development server,
`concurrently`, a chain through npm or a shell; since 1.24.0 a chain of `node` commands, `node migrate.js && node server.js`,
runs as written under the image's init, tini, which passes SIGTERM to the last one; `nodemon`, `node-dev`, `pm2` and `forever` on a file are
rewritten to `node <file>` since 1.22.0, and refused only with `--exec` or an ecosystem file in JavaScript); a start file
that is not in the repository when nothing builds it, and TypeScript without `typescript` among the dependencies (since
1.23.0 a `build` script, the frameworks' builds and `tsc` run in the platform's image, section 1b); a build that needs a
secret (Strapi, Payload); a loopback listen call only when the contract says `proxy: false` (a literal port is taken as
`port` since 1.22.0); a front-end site the platform cannot build as one (SvelteKit with another adapter than
adapter-static or adapter-node, Angular, no `build` script); two lockfiles, `bun.lockb` (Bun's old binary format), or `packageManager` and `engines` naming
different managers; Yarn 2+ with Plug'n'Play (no `nodeLinker: node-modules`); a lockfile entry without an integrity hash
or checksum, or from git, a tarball elsewhere, a `file:`, `link:` or `patch:` source, or `bundleDependencies`; a lockfile
over 5 MB; a workspace root that starts nothing itself (give one package's folder: it installs from the root's lockfile,
since 1.23.0), a root lockfile that does not list the package, or a repository managed by nx, Lerna or Rush with no
`workspaces` field; a committed `.npmrc` or `.yarnrc.yml` with a literal token; a line newer than 26; and `engines.iojs`.
A committed `node_modules/`, `dist/` or `.next/` is set aside with a note, as is an npm lockfile behind its
`package.json`, and a `build` script that the start does not need (Sophia's builds a browser extension) is noted, not
refused.

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

**What the platform reads as it is, when there is no lock** (since 1.22.0, roadmap 20.3): a repository taken as it is
need not export anything. `[tool.poetry.dependencies]` without a `poetry.lock`, a `Pipfile` without its lock, and the
`install_requires` of `setup.cfg` or `setup.py` are read as text (Poetry's `^1.2` becomes `>=1.2,<2.0`; a `setup.py`
whose list is computed rather than written out is refused, because reading it would mean running it) and locked by the
platform with uv, like a `requirements.txt`. `pdm.lock` and a PEP 751 `pylock.toml` are read at their recorded hashes,
like `poetry.lock`. A `requirements.txt` that includes other files (`-r requirements/base.txt`) is followed inside the
service's folder, and a `requirements/` folder with no `requirements.txt` beside it is read from its `production.txt`,
`prod.txt`, `base.txt` or `main.txt`, else its only file; a folder with one file per hardware is refused until a person
names one. System packages listed in `apt.txt`, `Aptfile` or `packages.txt` (Debian names, one per line) are installed
in the platform's image, in a build stage that also carries a compiler, so a dependency that ships only as a source
distribution builds there and only the finished virtual environment enters the image. A Django project (`manage.py`
beside `django`) gets `python manage.py collectstatic --noinput` at build, as Heroku runs it; a settings module that
needs a value at import stops the build, and the contract's `env` is where to give it.

## 1b. Node.js lines, packages and install scripts

**The line.** `platform.yaml` says `runtime: node-http` and `node: "22"`, `"24"` or `"26"`, the lines in long-term support
(20 and the odd-numbered lines are refused). The tests, the dependency audit and the platform's image use it; keep your
Dockerfile's `FROM node:X` the same. A line is refused from its end of life, and the dates are in the runbook: 22 on
2027-04-30, 24 on 2028-04-30, 26 on 2029-04-30. The newest line in Active LTS is the default for a new contract (24 until
2026-10-28, 26 from then).

**The lockfile.** `package-lock.json` (version 2 or 3) or `npm-shrinkwrap.json`, committed with `package.json` and
installed with `npm ci`, which installs exactly the lockfile and stops when the two disagree; or, since 1.23.0 (roadmap
21.1), `pnpm-lock.yaml` (lockfile version 6 or 9), `yarn.lock` (Yarn 1's format, or Yarn 2 and later's with
`nodeLinker: node-modules` in `.yarnrc.yml`, since Plug'n'Play needs a loader at every start) or `bun.lock` (Bun 1.2's
text format; `bun.lockb` is refused with the command that rewrites it). Each is installed by its own manager, frozen and
with every install script off: `pnpm install --frozen-lockfile --ignore-scripts`, `yarn install --immutable` with
`YARN_ENABLE_SCRIPTS=false`, `yarn install --frozen-lockfile --ignore-scripts` (Yarn 1), `bun install --frozen-lockfile
--ignore-scripts`. The manager itself is installed by version and integrity from its npm package (pnpm, `@yarnpkg/cli-dist`,
`yarn`, `@oven/bun-linux-x64`), never through Corepack, which Node.js 25 and later no longer ship. The release is the one
`package.json`'s `packageManager` names (`"pnpm@9.15.0+sha512.…"`, `"yarn@4.5.3"`), as Corepack and Railpack honour it,
held to the sha512 the field carries or else to the integrity the npm registry publishes; without the field it is the
platform's pin (pnpm 12, Yarn 4, Yarn 1, Bun 1), which the weekly pin refresh follows. A lockfile only another release can
install frozen (checked on 2026-10-20: pnpm 12 reads lockfile 9.0 only, and Yarn 4.18 rewrites every other Yarn 2+
format, Yarn 4.0 to 4.12's included) is refused without `packageManager`, with the line to add. Whatever the manager, every entry must carry its integrity hash or checksum and come from
`https://registry.npmjs.org/` (Yarn 1 lockfiles may name `registry.yarnpkg.com`, the same registry); a git dependency, a
tarball from elsewhere, a `file:`, `link:` or `patch:` source and `bundleDependencies` are refused, because the manager
installs them without checking them against the registry. The platform reads no `.npmrc`: it installs from the public
registry with the manager's own settings. A lockfile is read up to 5 MB (a monorepo's is often 2 to 5 MB; since 1.22.0).
For a repository taken as it is, an npm lockfile that is behind its `package.json` is set aside and `package.json` is
locked again by the platform, as below; the other managers' frozen installs stop on a stale lockfile, so commit the two
together. A package of a workspace (`workspaces` in the root's `package.json`, or `pnpm-workspace.yaml`) without a lockfile
of its own installs from the root's lockfile, which must list it (roadmap 21.4): the whole repository is the build
context, the install runs at the root (`pnpm install --filter`, `npm ci`, `yarn workspaces focus`, `bun install`), the
build in the package's folder, and only that package with its production dependencies enters the image (`pnpm deploy`
for pnpm; the pruned tree for the others).

**Install scripts never run at install.** Every npm attack of 2025 and 2026 ran from a package's install script, so the
platform installs with `npm ci --ignore-scripts` on every npm, then runs the scripts of the packages `platform.yaml` lists
under `build.scripts`, each by name and exact version, and nothing else:

```yaml
build:
  scripts: [sharp@0.34.4]   # npm rebuild sharp@0.34.4 after the install; a name alone, a range or --all is refused
```

The reader drafts that list from the platform's own table of packages whose script is needed on Cloud Run (since 1.23.0,
D53: `sharp` below 0.33, `bcrypt` below 5.1, `canvas`, `sqlite3`, `better-sqlite3`, `argon2`, `re2`, `cpu-features`,
`node-pty`, `@parcel/watcher`, `@tensorflow/tfjs-node`; `puppeteer` is named so the review can replace it with
`puppeteer-core`), each at the version the lockfile pins, for the merge to approve; and from what your `package.json`
approves for its manager, `allowScripts` by version (`npm install-scripts approve` writes them, from npm 11.16), pnpm's
`onlyBuiltDependencies`, Yarn's `dependenciesMeta` with `built: true`, Bun's `trustedDependencies` (names, joined with
the locked version), only when the package needs its script on Cloud Run. It is often empty: a package that ships its
native binary inside its tarball needs no script (onnxruntime-node's script only downloads GPU libraries; protobufjs's
only prints a warning). With npm and pnpm 8 the lockfile says which packages have a script; Yarn's, Bun's and pnpm 9's
do not, so there the table alone decides. The scripts run through the manager that installed the packages (`npm
rebuild`, `pnpm rebuild`, `yarn rebuild`, `bun pm trust`). Your own `package.json`'s `preinstall`, `postinstall` and
`prepare` never run either; the test command runs your `pretest` and `test`.

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

**Converting a lockfile** is no longer needed (since 1.23.0 the platform installs pnpm, Yarn and Bun lockfiles with their
own manager, above). A repository that follows this page with its own workflows still drafts npm's CI and Dockerfile; to
move to npm by hand, convert with npm 11.10 or later from the service's folder, review the lockfile's diff and run the
tests (`rm -rf node_modules && npm install --package-lock-only --ignore-scripts --min-release-age=3`), and remove the
old file and any `packageManager` field that names another manager in the same commit.

**Builds** (since 1.23.0, roadmap 21.2 and 21.3). The platform's image runs plain JavaScript as committed, and TypeScript
under Node's type stripping on 24 and 26 when `tsconfig.json` says `erasableSyntaxOnly` (`node src/index.ts`). Anything
else is built in a build stage of the platform's image: the development dependencies are installed (scripts off), the
`build` script runs (or the framework's own command when there is none), the development dependencies are pruned, and
only the result enters the image, with `NODE_ENV=production`. The frameworks' outputs and starts are built in, and the
start the contract names is the built one, run by node itself: Next.js (`output: 'standalone'` gives `node server.js`
with `HOSTNAME=0.0.0.0`; otherwise `next start` under node with the production dependencies), NestJS (`node
dist/main.js`), React Router and Remix (their serve commands on `build/server/index.js`, `HOST=0.0.0.0`), SvelteKit with
adapter-node (`node build`), Nuxt (`node .output/server/index.mjs`, `NITRO_HOST`), Astro with its Node adapter (`node
dist/server/entry.mjs`), AdonisJS (`node build/bin/server.js`); a `.ts` entry that cannot be stripped is built with `tsc`
into the `outDir` of `tsconfig.json`, and `tsx`, `ts-node`, `next start`, `nest start` and `build && node FILE` starts
are rewritten accordingly. A build that needs a secret (Strapi, Payload) is refused with the name: the platform's build
has none. The image's build is decided again at build time from the same files, so what the contract's pull request says
is what the image does.

## 2. The container: Cloud Run's contract and Docker's rules

Required:

- Listen on `0.0.0.0` at the port in `platform.yaml` (default 8080), which Cloud Run also passes as `PORT`. Nothing
  else may be assumed about the network; Cloud Run terminates TLS, the container speaks plain HTTP/1.
- Start fast and answer the health path as soon as the app can serve. The platform's startup probe hits that path every
  second and waits one second for an answer, for up to a minute (since 1.29.0; startup CPU boost gives the instance twice
  its CPU while it starts); Cloud Run's hard limit is four minutes, the lab's expectation is a few seconds. Do heavy
  initialization once in global scope or lazily, never per request.
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
  `FIRESTORE_DATABASE` or `DATABASE_INSTANCE`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_IP_TYPE`, and
  `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION` for `models: [claude]`; a `lab-members` app also gets
  `IAP_AUDIENCE` (section 6a). Nothing is read from committed files or baked into the image. Firestore's clients read
  no variable for the database, so pass it yourself: `new Firestore({ databaseId: process.env.FIRESTORE_DATABASE })`,
  `firestore.Client(database=os.environ["FIRESTORE_DATABASE"])`.
- **Backing services as attached resources.** Use Google's client libraries with default credentials: the service
  runs as its own least-privilege identity that may reach only its declared resources. No keys, no passwords;
  Postgres signs in with IAM (the identity is the user) over a private address, through the Cloud SQL Python connector
  or psycopg, or for Node.js `@google-cloud/cloud-sql-connector` with `authType: 'IAM'` and `ipType: 'PRIVATE'`.
- **Write only under `/tmp`.** The image's files belong to root and the service runs as a user that is not, so a library
  that writes a cache beside its own code (transformers.js keeps its models inside `node_modules`) stops with `EACCES`.
  Point such caches at `/tmp`, which is memory on Cloud Run and counts toward `resources.memory_mib`, or better, put the
  files in a mounted bucket (section 4b) so nothing is fetched or written at a cold start.
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
| Files the code reads at a fixed path (a corpus, a model, a search index) | A bucket need mounted read only (`needs.mounts`), filled by a person or a script | They stay out of the repository and the image; what an instance writes to its own disk is memory, and it is wiped |
| A small database kept in a file (SQLite) | Firestore or Postgres under `needs`, never a file on the instance's disk | The disk is wiped when the instance stops, and during a release the old and new versions run side by side, each with its own copy, so one side's writes would be lost |
| Calls to Claude | `needs.models: [claude]`: Claude through Vertex AI | No internet and no API key: the global endpoint is reached through Private Google Access, billed to the project inside its cap |
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

**Files from a bucket.** `needs.mounts` maps a declared bucket to the folder where the code reads it
(`mounts: { corpus: /app/data/index }`): every release mounts it read only through Cloud Storage FUSE and drops any mount
the contract no longer names. A mount runs the service in the second-generation environment, can add a little to a cold
start, and fails the start if it takes over 30 seconds. FUSE has no file locking and is not a full POSIX filesystem, so
it carries files the app only reads; a database never goes there. The path is absolute and may not hide what the container
needs (`/app` itself, `/tmp`, `/var`, the system folders); mount below the app's folder or in one of its own (`/models`).
Fill the bucket once with `gcloud storage cp -r ./index gs://PROJECT-SERVICE-corpus/`; the code reads the files at the
same paths as before.

Work that takes minutes belongs before the release, not at every start: Cloud Run gives a new instance 60 seconds to
answer its health path, and every visitor after an idle spell waits for that start. Compute embeddings, indexes and
other derived files once, put them in the bucket beside their inputs, and have the code load them. A model's files go
there too: transformers.js loads them from the mount with `env.localModelPath` set to its folder and
`env.allowRemoteModels = false` (the layout is `<folder>/<model id>/config.json`, `tokenizer.json`,
`tokenizer_config.json` and `onnx/` with the file the chosen `dtype` names), and Python's Hugging Face libraries load a
folder path with `HF_HUB_OFFLINE=1`. Measured on 2026-10-20 in the platform's Node.js image on a laptop: a search
service that embedded about 5,000 passages at start took 2 minutes 41 seconds with six CPUs, and the same service
reading its precomputed vectors and its model from read-only folders, with one CPU and 512 MiB, answered in under a
second and used about 190 MiB after its first search.

**Answers that stream.** A response sent as server-sent events, as most chat front ends do, is one request to Cloud
Run, and Cloud Run closes it when `timeout_seconds` runs out (60 seconds by default, 3600 at most), whatever is still
streaming. Set it to the longest answer the service must carry.

**Claude through Google Cloud.** `needs.models: [claude]` lets the service call Claude on Vertex AI with no internet and
no API key. Its identity gets a role with one permission (calling a model), and the service gets
`ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION=global`, which the Anthropic SDK's Vertex client reads
(`AnthropicVertex` from `@anthropic-ai/vertex-sdk`, or `AnthropicVertex()` from `anthropic[vertex]` in Python). Use the
global region: Private Google Access does not reach Vertex AI's regional and multi-region endpoints. The server-side
`fallbacks` parameter, the Files and Batches APIs and the server-side tools are not offered there; streaming, tool use,
prompt caching, adaptive thinking and `output_config.effort` are. On 2026-10-19 the Billing Catalog priced Claude Opus 5.5
($4 input, $20 output per million tokens) and Sonnet 5.5 ($2 and $10) on the global endpoint, the same as Anthropic's list;
calls bill to the project, inside its budget and spending cap, and nothing while idle. The operator enables each model once
per project in Model Garden (`docs/RUNBOOK.md`).

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
timeout_seconds: 60       # per request, up to 3600 (a service that streams long answers holds its request open)
exposure: authenticated   # programs with a Google identity token; or lab-members: people, after a Google sign-in, listed accounts only
state: stateless
env:                      # plain configuration values
  LOG_LEVEL: info
secrets: [API_KEY]        # names only; the value lives in Secret Manager as my-service-API_KEY and arrives as the env var API_KEY
tests: required           # or none, knowingly: every release then carries a warning; or report: run and counted, never a stop
# proxy: true              # an Nginx sidecar in front of a service that binds 127.0.0.1 (D51); billed as the app's while serving
python: "3.12"            # the Python the tests and the audit run on: 3.11 to 3.15; match the Dockerfile's base image
# node: "24"              # node-http only, instead of python: "22", "24" or "26", each refused from its end of life
needs:
  buckets: [uploads]      # env BUCKET_UPLOADS
  topics: [events]        # env TOPIC_EVENTS and SUBSCRIPTION_EVENTS
  database: none          # none | firestore | postgres (about 9 dollars a month from the moment it exists)
  database_auth: iam      # postgres only: iam (no password anywhere) or password (DATABASE_URL arrives as a secret; section 4b)
  database_tier: standard # postgres only, production only: standard, ha (a standby) or sla (a standby and a dedicated CPU)
  migrate: []             # e.g. [alembic, upgrade, head]: a job from the same image, before traffic moves
  mounts: {}              # e.g. { uploads: /app/data/uploads }: a declared bucket mounted read only at that path (section 4b)
  models: []              # e.g. [claude]: Claude through Vertex AI, no internet or API key (section 4b)
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
| A final image running as root (no `USER`, or `USER root`) | wrapped: the contract's `build.user: 65532` adds a stage that sets that user (since 1.24.0, D52); refused only when `build.user` is removed and trust stays unknown | reported |
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
  verify it, from the signed `X-Goog-IAP-JWT-Assertion` header: an ES256 token from `https://cloud.google.com/iap`, signed
  with the keys at `https://www.gstatic.com/iap/verify/public_key-jwk`, whose `aud` must equal the variable
  `IAP_AUDIENCE` the platform sets (`/projects/NUMBER/locations/REGION/services/SERVICE`) and whose `email` claim names
  the person. Google's auth libraries do it in a few lines (Node.js `google-auth-library`: `OAuth2Client`'s
  `getIapPublicKeys`, then `verifySignedJwtWithCertsAsync(token, pubkeys, IAP_AUDIENCE, ['https://cloud.google.com/iap'])`;
  Python `google-auth`: `id_token.verify_token(token, Request(), audience=IAP_AUDIENCE,
  certs_url="https://www.gstatic.com/iap/verify/public_key")`). The proxy removes such headers when a visitor sends them,
  so they come only from it. Leave the health path open: Cloud Run's own startup probe reaches the container without
  passing the proxy.
- Who can be listed: Google accounts and Google groups. A person whose work address is not Google's can create a Google
  account on that same address and be listed by it. Signing in with another provider (Microsoft Entra ID, say) is not
  offered (`docs/ROADMAP.md`, D47): Google does it for this proxy through Workforce Identity Federation, which needs the
  project, the identity pool and the OAuth client in one organization (staging is in none) and a registration in the
  other provider by its administrators.
- Pages served by the app itself work as they are. A page or browser extension on another site that calls the app's API
  sends a preflight request without the sign-in cookie, which the proxy refuses; such a client is not supported yet.
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
