# 1.22.5: no package manager in the platform's Python image

Found on 2026-10-20 in the live exercise of Phase 20. `django-static`, the trial's Django project, was built on
`python:3.11-slim` (its `.python-version` asked for 3.10, a line at its end of life, so the lowest supported line ran it,
D48) and stopped at the image scan: "2 critical or high findings with a fix available, in code nobody has reviewed".
Neither was the service's: `wheel 0.45.1` (CVE-2026-24049, fixed in 0.46.2) and `jaraco-context 5.3` (CVE-2026-23949,
fixed in 6.1, inside setuptools) come with the base image, which ships pip, setuptools and wheel for Python 3.11; the
3.12 image ships pip alone, and `aiohttp-main` on it passed the same scan (five high findings, none fixable). The
service never runs any of the three. The same thing stopped the platform's Node.js image in Phase 18 (1.20.9), and the
answer is the same.

1. **The platform's Python image uninstalls pip, setuptools and wheel** after the dependencies are installed and Django's
   static files collected, before the user line, in both variants (the plain image, and the build stage's virtual
   environment plus the base's interpreter). The tests, the audit and the migration job are untouched: the tests install
   on the runner, the audit reads the lock, and the job runs the image's own command.

Nothing on the machine changes: the pipeline renders the Dockerfile from the platform's main at build time, so the next
release of `django-static` takes it (one build, about $0.26).

Tests: 913 pass (1 new); ruff clean.

# 1.22.3: certifi 2026.10.19, for compromised root certificates

Found on 2026-10-20 in the live exercise of Phase 20: the platform's own dependency audit began failing every pull request
at 21:42 UTC, two hours after GitHub published GHSA-6q48-6c7w-9gw2 (critical, 2026-10-20 19:35 UTC): certifi before
2026.10.19 bundles emSign root certificates whose intermediate keys are compromised, so a client trusting that bundle can
be shown a forged certificate. The controller uses certifi's bundle for every HTTPS call it makes, to GitHub and to
Google. The trial's infrastructure pull requests and main's own checks stopped on it too.

1. **certifi 2026.10.19** in `requirements.lock`, `requirements-dev.lock` and `audit.lock`, made with the commands in their
   headers and `--upgrade-package certifi`, so nothing else moves; the hashes are PyPI's for the wheel and the source
   distribution, published 2026-10-20 02:24 UTC. A security fix does not wait out the seven days the weekly pin refresh
   gives a new release.

The application locks the platform keeps that still name certifi 2026.7.22 (`apps/llm-debate-backend/`, and the test lock
of `apps/lab-demo-two/`) are not changed here: their next release's audit names it, and locking them again is a change of
its own.

Tests: unchanged; `pip-audit` finds no known vulnerability in the three locks.


# 1.22.2: "deploy everything" answers at once

Found on 2026-10-20 in the live exercise of Phase 20. `onboard_application` with `all=true` on a repository holding four
services (a Gradio app, an aiohttp app, a Node.js service and a Django project) read all four, compiled three locks on the
machine and opened the four contract pull requests in about a minute and a half, and the call that asked had timed out
long before: the work was done, and the answer was lost. In Phase 18 the same call answered in seconds because the
Node.js services carried their lockfiles; a Python service's lock is compiled at this step, Gradio's alone runs to a
thousand lines.

1. **The call returns once each service is read.** Each onboarding stops at the `contract` step, and the onboarding
   tick, which runs every minute, compiles the lock and opens the contract pull request, one service after another. The
   answer says so ("read as it is; its contract pull request opens within a minute"), and `get_onboarding_status` or a
   second "deploy everything" shows the pull requests once they are open. A single `onboard_application` call still
   opens its pull request before answering, as before.

Built on 1.22.1 (#128).

Tests: 912 pass; ruff clean.

# 1.22.1: a repository's own default branch, and HOST for the Python rungs

Found on 2026-10-20 at the start of the live exercise of Phase 20 (docs/ROADMAP.md), before anything was built.

1. **A repository taken as it is is read from its own default branch.** `check_repository` on both public candidates of
   the exercise, `streamlit/streamlit-example` and `HackSoftware/Django-Styleguide-Example`, stopped before the reader ran:
   "The default branch is master; the platform releases only from main". The rule came from the platform's own
   workflows, which trigger on `main`; a contract the platform keeps (13.1) pins a commit and a release moves that commit
   to the branch's head, so the branch's name never mattered there. The reader, `import_contract` and `onboard_application`
   with `as_is` or `all` now read the repository's default branch, whatever it is, and the registered application follows
   it (`deploy_latest`, the comparison with the live release; the application model's rule that binds the branch to `main`
   now holds only for an application with its own workflows). A repository that takes the platform's own workflows
   still needs `main`, and the checklist's message now says to deploy it as it is instead; `check_repository` answers for
   "as it is" even when the checklist is blocked by the branch.
2. **A `python FILE` rung sets `HOST` when the entry file reads it.** 1.22.0 promised `HOST=0.0.0.0` for the last rung
   and set it there, but the aiohttp, Gradio, Tornado and NiceGUI rungs set only their frameworks' own variables; a
   `main.py` that reads `HOST` with the usual development default (127.0.0.1) would bind an address Cloud Run cannot
   reach, and its first revision would fail. The contract now carries `HOST: 0.0.0.0` for such a file, with a "Host"
   choice naming the file, as it has for a Node.js service since 1.20.0; a value the contract already gives stays.

Tests: 912 pass (2 new); ruff clean.

# 1.22.0: read more, refuse less (Phase 20)

Roadmap Phase 20 (docs/ROADMAP.md 20.1 to 20.7, decisions D48, D50 and D56 at their defaults), from the research review
of 2026-10-20 (`docs/RESEARCH-2026-10-20.md`, sections 4, 6.4, 7.1 and the appendix). That review ran the reader over 33
public Python and Node.js repositories: 3 were taken as they are and 30 refused, two thirds of the refusals being the
platform's own choices or bugs rather than Cloud Run's needs. This version fixes the bugs and gives the reader the
breadth the source builders (Google's buildpacks, Railpack) have. Only the reader, the lock converters, the platform's
own Python Dockerfile and `onboard_application` change: the pipeline, the gates and the contract's shape are the same,
and nothing is ever written into an application's repository. Built and checked locally; the live exercise listed under
Phase 20 has not run yet.

1. **The survey as a test** (20.6). The 33 repositories' file trees and the texts the reader reads are recorded under
   `tests/fixtures/survey/` (2.8 MB, gzipped), with the expected contract or refusal for each in `tests/test_survey.py`.
   The same sample now reads as **8 accepted and 42 refusals**, from 3 and 60: a Streamlit demo, a Gradio web UI, a
   Django project that includes `requirements/base.txt`, ComfyUI (`python main.py`) and Open WebUI's backend join the
   three of 1.21.0. Of the 25 still refused, 9 carry a Dockerfile that runs as root (Phase 22's wrapper user), 8 need
   the builds, package managers or workspaces of Phase 21, and 8 need one answer from a person (a real `.env`, a
   requirements file per hardware, a folder above several services, a command the files do not say).
2. **The reader's bugs** (20.1). `.env.local.example` and every other documented example (`.env.<name>.example`,
   `.sample`, `.template`, `.dist`) are examples, not secrets. A Django project is one with `django` among its
   dependencies or a `manage.py` that imports Django, never a settings file that mentions the word (Litestar's
   fullstack template was read as Django); without `WSGI_APPLICATION`, the one `<package>/wsgi.py` or `asgi.py` beside
   `manage.py` says the application. `-r` includes in `requirements.txt` are followed inside the service's folder, and a
   `requirements/` folder with no `requirements.txt` is read from its production, base or only file (one file per
   hardware is refused until a person names one). `main.py`, `app.py` and the frameworks' other discovery names are
   fetched by name at any depth before shallower helpers, so a large application's entry is read (Open WebUI's
   `backend/open_webui/main.py` never was). Only the entry file and its imports decide a Node.js service's host, never a
   helper script. A notebook is a note, never the refusal. A root whose services live in folders names them in every
   refusal, not only the manifest one.
3. **Python entries** (20.2, D50). When no application object is found, the dependency that names the framework decides
   the command and the environment, as Google's buildpacks and Railpack do: Streamlit (`streamlit run FILE
   --server.address 0.0.0.0 --server.port $PORT --server.headless true`), Gradio (`python FILE` with
   `GRADIO_SERVER_NAME` and `GRADIO_SERVER_PORT`), Chainlit, Dash, Panel, Shiny, NiceGUI, Reflex, each from its own
   entry-file list; FastAPI, Flask, Litestar and the other ASGI and WSGI frameworks on `module:app` by name at any depth
   (uvicorn or gunicorn), when the object was not read; aiohttp as a factory under `python -m aiohttp.web`, an object
   under gunicorn's aiohttp worker, else `python main.py`; Sanic and Tornado; Django's `ASGI_APPLICATION`. The last rung
   is `python main.py` (or `app.py`, `server.py`, `run.py`) with `HOST=0.0.0.0` and `PORT` set. A literal port in the
   entry file (`launch(server_port=7860)`, an argparse `--port` default) goes into the contract. Every such rung notes
   that the first revision's log decides and that `build.entrypoint` in the review overrides it. Several candidate
   objects are ranked as the frameworks rank them (`main.py` before `app.py`, `app` before `api`, examples and tools
   last) and refused only on a tie.
4. **Python manifests** (20.3). `[tool.poetry.dependencies]` without a `poetry.lock`, a `Pipfile` without its lock, and
   the `install_requires` of `setup.cfg` or `setup.py` (read as a literal list with `ast`, never run; a computed list is
   refused) are locked by the platform in its lock container, as `requirements.txt` is. `pdm.lock` and a PEP 751
   `pylock.toml` are read at their hashes. `apt.txt`, `Aptfile` and `packages.txt` are installed in the platform's own
   Python image (`templates/repo/Dockerfile.platform`, rendered at build time), in a build stage that also carries a
   compiler whenever system packages are listed or the lock holds a package with a single distribution file (usually a
   source distribution); only the finished virtual environment enters the final image. A Django project gets
   `collectstatic` at build, as Heroku runs it, and the review offers `needs.migrate: [python, manage.py, migrate]`.
   Package names are checked before they reach the Dockerfile; `bindep.txt` stays refused.
5. **Node.js** (20.4). A literal port that never reads `PORT` becomes `port: N` instead of a refusal (gcloud's `--port`
   sets the container port and `PORT` alike). `nodemon`, `node-dev`, `pm2`, `pm2-runtime` and `forever` on a file are
   rewritten to `node <file>` with a note (with `--exec` or an ecosystem file they stay refused, as `concurrently` and a
   chain do). `main` falls through the whole entry list (`app.js`, `main.js`, `src/index.js`, `server/index.mjs`, ...).
   A lockfile behind its `package.json` is set aside and `package.json` locked again by the platform. Lockfiles are read
   up to 5 MB (past GitHub's 1 MB contents limit the blob is fetched by its sha). `HOST`, `HOSTNAME` and `NITRO_HOST`
   are set to `0.0.0.0` for the frameworks that read them.
6. **Dead lines and everything in one go** (20.5, D48 and D56). A `.nvmrc`, `.node-version`, `.tool-versions`,
   `engines.node`, `.python-version`, `runtime.txt`, `Pipfile.lock` or `requires-python` that names a line at its end of
   life (Node 16, 18, 20 and the odd lines; Python 3.10 and older) takes the lowest supported line (22, or 3.11) with a
   note naming the date (`NODE_ENDED` and `PYTHON_ENDED` in `labdeploy/models.py`); a Dockerfile's own `FROM` stays the
   developer's and is refused as before. `onboard_application(url, all=true)` ("deploy everything in REPO") opens one
   contract pull request per service a root holds, named after its folder, through `check_repository`'s list of
   services, and the chat answer lists them; a root with one service in a folder takes that folder without asking. The
   tool's description changed, so `schema/tools.sha256.json` is re-pinned.
7. **Documents** (20.7). The contract page's "what the platform assumes" tables and refusal paragraphs, its sections 1a
   and 1b; CLAUDE-CHAT's "Deploy it as it is"; the operator skill and its reference (the skill zip on claude.ai needs
   re-uploading); the roadmap's Phase 20 status; the handoff.

Tests: 910 pass (62 new, on top of 848); ruff clean. The survey test reads fixtures only: nothing from the sampled
repositories is run.


# 1.21.0: what a data-bearing application needs (Phase 19)

Roadmap Phase 19 (docs/ROADMAP.md 19.1 to 19.5, decision D47), from the research review of 2026-10-19
(`docs/RESEARCH-2026-10-19.md` section 5), built on top of 1.20.0. An application can now read files from a bucket at a
fixed path and call Claude through Google Cloud with no internet and no API key. Applications that declare neither see no
difference: their contracts hash and their roots render as before, and their releases only gain two flags that clear
volumes nobody added. Built and checked locally; the live exercise listed under Phase 19 has not run yet.

1. **Bucket mounts.** `needs.mounts` maps a declared bucket to the absolute path where the code reads it
   (`mounts: { corpus: /app/data/index }`). Every release clears the mounts and volumes it finds and mounts what the
   contract names, read only, through Cloud Storage FUSE (gcloud clears before it adds; read in its source). A mount puts
   the service in the second-generation environment, which FUSE needs. A path that would hide what the container needs
   (`/app` itself, `/tmp`, `/var`, the system folders) is refused, as are overlapping mounts. The runtime identity already
   held `roles/storage.objectUser` on its buckets, so nothing changes in Terraform.
2. **Claude through Vertex AI.** `needs.models: [claude]` sets `ANTHROPIC_VERTEX_PROJECT_ID` and `CLOUD_ML_REGION=global`,
   which the Anthropic SDK's Vertex client reads, and the app's root grants its runtime identity the foundation's new role
   `labDeployModelUser`: one permission, `aiplatform.endpoints.predict`, rather than Vertex AI User and its 459. The
   foundation turns on `aiplatform.googleapis.com` and publishes the role as `model_user_role`; a root that declares a model
   reads it, and its plan stops with the step to take when the foundation has not been applied. The global endpoint is the
   one Private Google Access reaches, so a contained app needs no internet. The cost view lists the model with its
   per-token prices from the Billing Catalog of 2026-10-19 (Claude Opus 5.5 $4 and $20 per million input and output
   tokens, Sonnet 5.5 $2 and $10). The plan policy accepts the role as a foundation custom role and still refuses
   `roles/aiplatform.user`.
3. **Long requests.** `timeout_seconds` goes up to 3600, Cloud Run's own ceiling, for a service that streams long answers.
4. **Dropped before it was built: `state: sqlite`.** The plan had a Litestream sidecar replicate an app's SQLite file to a
   bucket. Every release runs the old and the new revision at once (the candidate's probes, the bake, production's split),
   each with its own copy and its own Litestream writing the same replica, so one side's writes would be lost on every
   release. A small database goes in Firestore or Postgres; Sophia's moves to Firestore. The research review carries a dated
   update, which also corrects its claim that Firestore is free here: the platform's named databases bill from the first
   operation.
5. **Documents.** The contract page says where files, a small database and a model API go, and describes both new needs;
   the runbook has the three steps for an app that calls Claude (the foundation, Model Garden, the app's root); the roadmap
   records what was built and what was dropped.

Tests: 848 pass (10 new, on top of 1.20.11); ruff clean; `terraform validate` passes on an app root and the foundation (Terraform 1.13.5).


# 1.20.11: an unchanged contract's image is reused again

Found on 2026-10-19 in the live exercise of Phase 18, while making a second live revision of `node-docker` for the
rollback drill. A plan for the same commit reused the image of the release before it, as designed, and the pipeline
stopped: "No signed provenance shows ... was built from brianboti/fast-deploy-node-trial at e027aa13d964 by
brianboti/fast-deploy/.github/workflows/deploy.yml on main (made from a different contract)". The contract had not
changed. But a contract the platform keeps (docs/ROADMAP.md 13.1) is read at the platform repository's main, which moves
with every merge, and the reused image's signed provenance names the commit main was at when it was built. So a release
of an unchanged commit, which costs nothing when it reuses, failed for every kept contract, Python's too, as soon as
anything else had merged.

1. **The plan keeps the commit the image was built with**, when nothing under `apps/<service>/` changed since (neither
   the contract nor the locks the platform made); that commit is an ancestor of main, which the pipeline already
   accepts, and its contract reads the same. When something there changed, or GitHub's comparison is incomplete, the
   image is built again rather than reused.

Promotions verify a staging image against the contract commit of the staging plan that released it, so they read the
same. Not exercised live yet: it needs this on the machine.

Built while 1.20.9 and 1.20.10 were open (#117, #120); merged on top of them.

Tests: 838 pass; ruff clean.


# 1.20.10: no false secret in every Node.js release

Found on 2026-10-19 in the live exercise of Phase 18: `node-docker`'s release said "no working keys found (1 possible
secret to check)", and the service's own files held none (the checkout's scan found nothing). TruffleHog reads an image
layer by layer, and the official node images carry Yarn in `/opt/yarn-v1.22.22/`, whose README matches a pattern; the
platform's build deletes Yarn in a later layer, but the base layer still holds the file. The secret scan counted only
Python's installed packages and the system's folders as someone else's code, so every Node.js release would have asked
the operator to check Yarn's README, and a pattern in any installed npm package would have read as the service's own.

1. **`node_modules/` and the node image's `/opt/yarn-…` count as installed code**, like `site-packages/`: an unconfirmed
   pattern there is counted, not listed. A working key is still reported wherever it is, and stops a build of code nobody
   has reviewed.

Checked with TruffleHog 3.97.4 on `node:24-trixie-slim` and on node-docker's image: the only patterns are in Debian's
package database (`/var/lib/dpkg/`) and Yarn's README, none verified.

Built while 1.20.8 and 1.20.9 were open (#110, #117); merged on top of them.

Tests: 837 pass; ruff clean.


# 1.20.9: the platform's Node.js image passes its own gate

Found on 2026-10-19 in the live exercise of Phase 18: the first releases of `node-bare`, `node-docker`, `node-onnx` and
`node-nolock` (code nobody has reviewed, from `brianboti/fast-deploy-node-trial`) passed their tests and the dependency
audit, then stopped at the image's scan: "13 critical or high findings with a fix available, in code nobody has
reviewed". None of them was in the services' code. Artifact Analysis and Trivy agree on the image the platform pins:

- `node:24-slim` and `node:22-slim` are Debian 12, whose `perl` (5.36.0-7+deb12u3) had five critical and five high
  findings fixed in deb12u4, which the official image had not taken; the pinned digest was that day's newest.
- npm's own bundled libraries (`brace-expansion` 5.0.9, `undici` 6.28.0) carried three high findings with fixes.

So no Node.js service nobody had reviewed could be released on the platform's image. The Python images are Debian 13 and
had no such finding.

1. **Debian 13 for every Node.js line.** The platform's image, its lock containers and the drafted Dockerfile build on
   `node:<line>-trixie-slim` by index digest (22.23.3 and 24.22.0, the same releases as before; `node:26-slim` was already
   Debian 13, its digest unchanged). The weekly pin refresh follows the same tags.
2. **No package manager in the image.** After `npm ci` (and `npm rebuild` for `build.scripts`), npm, npx, Corepack and
   Yarn are removed: node itself starts the service. A service that runs `npm` while it serves fails there; the contract
   page says so and how to start a child process with `process.execPath` instead.

Checked on Docker: `node-bare` built this way has no critical or high finding with a fix (Trivy 0.74.0), answers
`/health`, stops cleanly on SIGTERM and is 84 MB; both Node.js Dockerfiles pass hadolint 2.15.1. The live releases are in
`docs/VALIDATION.md` once this is on the machine. A Dockerfile of a repository's own on `node:<line>-slim` is still stopped
the same way: the contract page names the fix.

1.20.7 and 1.20.8 are the docker command on the machine (#109) and a repository whose services live in folders (#110).

Tests: 837 pass; ruff clean.


# 1.20.8: a repository whose services live in folders

Found on 2026-10-19 in the live exercise of Phase 18, by `check_repository` on the root of
`brianboti/fast-deploy-node-trial`, which keeps eight Node.js services in folders and nothing at its root. The answer said
the root "has Node.js code but no package.json", told the developer to add one there, listed a Python `requirements.txt`
among the missing files, and offered to draft a Dockerfile and a contract for the root. The reader had found the eight
folders, but nothing said so.

1. **The reader names the folders.** At a root with no `package.json` whose folders hold services, the fix reads "the
   service lives in node-bare, node-docker, node-localhost and 5 more: take that folder (path node-bare), or add a
   package.json at the root", as it already did for Python.
2. **The check says which folder to take.** When the root's only refusal is that it holds no service of its own,
   `check_repository` names the folders and asks which one to check or deploy, instead of listing files for the root.
   A root refused for something of its own (a committed secret) still shows that refusal.

1.20.5 to 1.20.7 are the weekly pin refresh (#103, #108) and the docker command on the machine (#109).

Tests: 837 pass; ruff clean.


# 1.20.7: the docker command on the controller's machine

Found on 2026-10-19 in the live exercise of Phase 18: importing `node-nolock` (a Node.js 22 service without a lockfile)
stopped with "The dependencies could not be locked: Docker is not installed on this machine". The machine has Docker's
daemon (Debian 13's `docker.io` 26.1.5, its socket active, the controller's user in the `docker` group) but no `docker`
command: Debian 13 ships the command in `docker-cli`, which `docker.io` only recommends, and the startup script installs
with `--no-install-recommends`. So every lock the platform makes on the machine failed, a Python service's (D27) as well
as a Node.js one's (D42), since the controller moved there on 2026-10-18; no import had needed one until now.

1. **The startup script installs `docker-cli`** beside `docker.io`. The script is the machine's metadata, which changes in
   place: the next foundation apply updates it without replacing the machine, and the script installs the package at the
   next boot. A running machine gets it at once with `sudo apt-get install -y --no-install-recommends docker-cli`.

1.20.5 and 1.20.6 are the weekly pin refresh (#103, #108).

Tests: 836 pass; ruff clean.


# 1.20.6: the actions the templates use, in the weekly pin refresh

Asked for on 2026-10-19: Dependabot's pull requests #47 (`actions/download-artifact` 4.3.0 to 8.0.2) and #48
(`actions/upload-artifact` 4.6.2 to 7.0.2) fail CI. Dependabot reads only `.github/workflows` and a root `action.yml` (its
options reference), so it changes `.github/workflows/deploy.yml`, which is made from `templates/deploy-gcp.yml`, and never
the template; the test that keeps them equal fails, and the templates fall behind the platform's own workflows. Two ways
were weighed: the weekly job copying Dependabot's bumps into the templates, or following these actions itself. Copying
cannot work well: a Dependabot pull request cannot pass CI until the templates move, so main never holds a bump to copy
and the job would have to push to Dependabot's branches; it would copy whatever Dependabot proposes, a new major included,
one pull request per action; and `labdeploy/workflows.py`, whose anchors name the template's actions, would still need a
hand. So the weekly job follows them itself. This builds on the weekly pin refresh (1.20.5, #103).

1. **Ten actions join `labdeploy/pins.py`** (`PINS`, kind `action`): checkout, setup-python, setup-node, upload-artifact,
   download-artifact, attest-build-provenance, attest-sbom and create-github-app-token, and Google's auth and
   setup-gcloud: every action a template or `labdeploy/workflows.py` uses. Each is written `owner/name@<commit> # vX.Y.Z`
   wherever it appears (`templates/*.yml`, `templates/repo/*.yml`, `labdeploy/workflows.py` and every workflow), with one
   commit everywhere. Four carried `# v4`, `# v3` or no comment; they now name their release (upload-artifact 4.6.2,
   download-artifact 4.3.0, auth 3.0.0, setup-gcloud 3.0.1), with the same commits as before.
2. **The rules**, as for the other pins: the newest release of the action's major version at least seven days after its
   publication, never a draft or a pre-release; a newer major is listed under "Newer lines" and never proposed
   (upload-artifact 7 and download-artifact 8 today). The commit is the one the release's tag names in the action's own
   repository, read from GitHub's git refs API (an annotated tag is followed to its commit): a commit from the action's
   repository, never a fork's, as GitHub's secure-use reference asks. Each week the pinned release's tag must still name
   the pinned commit; a release's tag never needs to move, so a moved or deleted one holds that action and says so under
   "Needs a look".
3. **Written everywhere at once.** `deploy.yml` is regenerated first, then the new commit is written in the template,
   `labdeploy/workflows.py` and every workflow alike, so `deploy.yml` stays what the new generator makes of the new
   template (a test loads the rewritten generator to check it). The pull request names each action with its release
   notes, its tag and the changes since the pinned commit, and says what to look for: an input or output renamed or
   removed.
4. **Dependabot leaves them alone**: `.github/dependabot.yml` ignores those ten, at every version. Dependabot raises no
   alerts for actions pinned by commit (About Dependabot alerts), so this loses no security update. It still follows
   `hashicorp/setup-terraform`, which no template uses, and the Terraform providers. A test keeps the ignore list equal
   to the actions in `PINS`, and those equal to the actions the templates use.
5. **What merging starts.** When an action `.github/workflows/infra-apply.yml` uses moves (checkout, Google's auth), the
   pull request says that merging it starts Infrastructure apply for every application root: that workflow runs when a
   push to main changes it, and `labdeploy.infra.roots` counts a change to an infrastructure workflow as one to every
   root. Dependabot's bumps of those actions did the same, without a word. This change is one itself: it writes the
   release of Google's auth in `infra-apply.yml`.
6. The tests that spelled out the checkout and setup-node commits read them through `labdeploy.pins` now.

Checked live, reading only, on 2026-10-19: every pinned action commit is the one its release's tag names, and zizmor's
online audits (`ref-version-mismatch`, `impostor-commit`) find nothing. No action has a newer release on its line, so the
first refresh proposes none of them; it lists upload-artifact 7.0.2 and download-artifact 8.0.2 as newer lines. Moving
to them is a decision for a person, and worth making soon: the pinned 4.6.2 and 4.3.0 declare Node.js 20, which GitHub
removed from its runners on 2026-09-23, so every run that uses them warns that they are "being forced to run on Node.js
24" (the promotion of 2026-10-19, for one) and GitHub asks for versions that support it; their release notes list
breaking changes (download-artifact 8 fails on a digest mismatch by default and no longer unzips a file that is not a
zip). Cost: about twenty more reads of GitHub's API a week, well within the workflow token's allowance, seconds of runner
time, and no cloud resource.

Tests: 836 pass (6 new); ruff and zizmor clean.


# 1.20.5: the weekly pin refresh

Asked for on 2026-10-19: the pins Dependabot does not follow were refreshed by hand, and some were behind (TruffleHog
3.97.4 against 3.99.2, Trivy 0.74.0 against 0.76.0; the research review of 2026-10-19). Dependabot follows only the
platform's GitHub Actions and Terraform providers: it cannot follow a download held to a checksum, a digest in Python
code or a machine's startup script. Renovate could, with regular expressions, but its source for release files finds a
new checksum by downloading small files as checksum lists or by hashing the file itself, never by reading the digest
GitHub records, and its hosted app does not run the command that regenerates `.github/workflows/deploy.yml`. So the
platform has a small job of its own. It was built as 1.20.3 and takes 1.20.5, because 1.20.1 (#100), 1.20.2 (#101)
and 1.20.4 (#102) merged before it; 1.20.3 is not used.

1. **`labdeploy/pins.py`** lists the 23 pins with their official source, the line the platform follows and every place
   each one is written: TruffleHog, Trivy, OSV-Scanner, hadolint, pack and Cosign in the pipeline, the four buildpacks
   images, `python:X.Y-slim` and `node:<line>-slim`, uv with its wheel hashes and the npm for Node 22 locks with its
   integrity (`labdeploy/scaffold.py`), CI's hadolint and OpenTofu, and the machine's Caddy and Litestream. `list` and
   `check` show the pins and what a refresh would do; `refresh` writes; `verify` checks the downloads.
2. **The rules.** Only a release at least seven days old, counted from the later of its publication and its files'
   upload, and only from the line followed: a newer line is listed, never proposed (uv 0.13 and npm 12 today). Checksums
   come from official metadata, never from hashing a download: GitHub's SHA-256 of a release file, which the project's
   own checksum list must repeat (and the list must be the file GitHub recorded); Docker Hub's index digest once its tag
   has pointed to it for seven days, with linux/amd64 and linux/arm64 in it; for the buildpacks images, the newest one
   Google published for the stack that is seven days old (Google uploads them about weekly, so the tag's own image rarely
   is); PyPI's and the npm registry's hashes. A newer npm must ask for the same Node.js versions and run no install
   script. Every week the current pins are checked too: a file GitHub now records differently, or a buildpacks image
   gone from its registry, is reported.
3. **`.github/workflows/pins.yml`**, Mondays at 05:23 UTC. The first job reads the metadata with the read-only token,
   writes the new pins, regenerates `deploy.yml` from the template, and downloads each new file to check it against its
   new pin, without running it. The second, in the environment `pins`, holds the key of a small GitHub App (Contents,
   Pull requests and Workflows) and reads nothing from the internet: it opens one pull request, replaces last week's if it
   is still open, or closes it when nothing is left, and the change may touch only the files that hold pins. It never
   merges. The pull request names each change with its release notes, GitHub's record and the checksum list, what waits,
   the newer lines, what needs a look, and what to review. Without the App (`docs/SETUP.md` 7o), the proposal stays in the
   run's summary.
4. **Tests the job never edits.** The tests that spelled out TruffleHog, Trivy, OSV-Scanner, hadolint, pack, Cosign,
   OpenTofu and npm read them through `labdeploy.pins` now, and check a little more than before (for TruffleHog, Trivy,
   OSV-Scanner and OpenTofu, that the checksum is checked before the tool is unpacked or run). New tests fail when a place is reworded, when two places disagree, when a
   download held to a checksum or an image digest is missing from the list, and when a test spells out a pinned value.
5. The comments of `BASE_IMAGES`, `NODE_IMAGES`, `UV_VERSION` and `LOCK_NPM` moved onto the pinned lines, so each line
   carries its own release and date. The runbook has a section for the refresh, and its monthly checklist names the pull
   request; the setup guide has section 7o.

Checked live, reading only, on 2026-10-19: every current pin equals its official metadata. A refresh on a copy of the
repository would propose TruffleHog 3.99.2, Trivy 0.75.0 (0.76.0 waits until 2026-10-21), OpenTofu 1.13.1 and npm
11.21.0, and its four downloads matched their new pins. With them, three of the old tests failed, which is why the tests
read the pins now. Cost: nothing on this public repository (about 11 runner minutes a week with the CI it starts), and no
cloud resource.

Tests: 797 pass (19 new); ruff and zizmor clean.


# 1.20.4: after a release, the machine and the re-pins by themselves

Two chores followed every merge into the platform repository's `main`, by hand: updating the controller's machine
(`scripts/machine.sh update`) and re-pinning each application's three workflow files (`update_app_workflows`, one pull
request per application). With `platform_updates` in the machine's configuration both now happen by themselves, in that
order, with an hour in which a release can still be stopped; without the block nothing changes. Built and checked
locally; the live trial waits for the operator's go (docs/RUNBOOK.md, "After a release", has the one-time steps). 1.20.1
to 1.20.3 are other pull requests of the same day (the Node.js 22 lock, GitHub's malware advisories, the weekly pin
refresh), so the number may move with the order they merge in.

1. **The machine updates itself.** The controller follows `main`: once the **Platform tests** run that the merge started
   has passed, and only when the merge changed something the controller runs (`labdeploy/`, `templates/`, `schema/`,
   `requirements.lock`), it waits `delay_minutes` (60; a newer change to those files starts it again, a contract release
   or a document does not), then for a quiet moment (no tool call for `quiet_minutes`, 10, and no release on its way to
   GitHub). Then it writes the commit into a request file next to its database. The controller has no privileges and
   cannot restart itself, so a small service of the machine's own takes the request as root (`fast-deploy-update`, which a
   systemd path unit starts). It installs the commit with `fast-deploy-install`: only a commit on `main`, now never one
   older than the commit that runs, with the existing minute of health checks and the way back. Then it watches two more
   minutes, and the previous commit comes back if the controller stops answering or restarts. Its result goes to a folder
   only root writes, and the controller records it once. A commit that went back is not tried again.
2. **The re-pins open themselves.** An hour after the controller started on a new commit, each application whose
   workflows pin an older runner gets the usual re-pin pull request to that commit, and the one it opened for an earlier
   commit is closed (never one a person opened). "An older runner" is now exact: the templates, the three lock files the
   workflows install, or one of the sixteen modules they import (`labdeploy/updates.py`; a test works the list out again
   from the templates). A release that changes only the controller re-pins nothing, so it starts no application release.
   Applications that are paused, still onboarding or whose contract the platform keeps are left alone.
3. **They merge themselves exactly where they may.** The controller merges a re-pin only where it merges an application's
   own pull requests: the application's `auto_merge` allows it, its checks have passed, it merges cleanly, and it changes
   exactly the three workflow files with the content the platform rendered (their hashes are kept when it opens). Anything
   else waits for a person, and one a person closes is not opened again for that commit.
4. **One change at a time.** The controller moves first; applications follow only after it has run the new commit for the
   delay, so a runner is never rolled out ahead of a controller that could not read what it reports.
   `platform.minimum_commit` never moves by itself.
5. **The background jobs read a changed configuration on their own.** Before, a configuration put on the machine with
   `config-put` reached auto-deploy and contract releases only with the next request; now every pass of the background
   thread reads it, so a pause or a switch applies within fifteen seconds.
6. **The machine's side.** The startup script writes `fast-deploy-update`, its oneshot service and the path unit that
   starts it, and `/var/lib/fast-deploy-update`. `fast-deploy-install` takes one install at a time (a lock in a folder
   only root can open, which both paths share, so a hand update never lands in the middle of an automatic one's watch),
   tells "did not answer, switched back" (exit status 75) apart from "stopped before the switch", never fails over what
   follows a healthy switch, always keeps the release it replaced (the way back), and removes the Python environments no
   kept release uses. Each script is written beside the old one and moved into place, so one that runs while the startup
   script runs again keeps its own text. `scripts/machine.sh startup` runs the startup script again after a foundation
   apply, `scripts/machine.sh logs update` reads the update service's log, and `status` shows the service and its last
   result.
7. **What an independent review of this change found, and what changed for it.** One request at a time, and nothing new
   until the machine has reported on it (or two hours have passed without a report); a commit that went back blocks the
   later commits that leave the controller's code as it was; an install that changed nothing (`not_installed`: GitHub
   away, held, another install) is asked for again after half an hour instead of never; the commit that runs is read
   under the lock, so an install by hand that finishes meanwhile is never undone; the request is moved into root's own
   folder before it is read (a link, a folder or a pipe from the controller's side is never followed or read); a
   dispatch left behind by a killed process no longer holds updates forever; a refused sign-in from anywhere no longer
   postpones them; a failing part (the machine or the re-pins) is recorded once and never stops the other; re-pins wait
   at least ten minutes and never run while the machine installs; a re-pin is merged only at the head the gate read, and
   what GitHub blocks for an hour, or refuses, is named as waiting for a person.
8. **What the chat says.** `get_controller_status` adds `platform_updates`: the commit the controller runs, what happens
   next and when, the last automatic update, and each application's re-pin. Its answer names a re-pin that waits for a
   person, an update that went back and a step that fails. `list_events` shows every step ("platform update", "workflow
   update").
9. **Documents.** The runbook's new section "After a release" (what happens, how to stop each part, what to do when
   something goes wrong, the one-time setup), the machine table, the monthly checklist, the setup guide (7m), the
   configuration example, the roadmap's re-pin rule and the handoff.

Sources read on 2026-10-19 (docs/SOURCES.md): systemd's path units (the service starts again while the request file is
there, so it removes the file first) and service and execution environment pages (a restart passes through inactive and
gets a new invocation ID). Also Google's startup scripts (root, each boot, `google_metadata_script_runner startup` to run
one again), Identity-Aware Proxy's TCP forwarding, GitHub's workflow runs API (filters by commit and event), the GitHub
token's rule that installation tokens start workflows, Google's SRE workbook on canarying (a small part first, time to
judge it, automatic rollback, one change at a time) and Dependabot's cooldown (a delay on purpose).

Requires regeneration of app workflows: no (no template or runner module changed). Terraform apply: the foundation once
(the startup script in the machine's metadata, in place), then `scripts/machine.sh startup` on the machine.

# 1.20.2: GitHub's malware advisories stop a Node.js release

Found on 2026-10-19 while preparing the live exercise of Phase 18, by running the pinned OSV-Scanner 2.7.0 on a lockfile
with `flatmap-stream@0.1.1` planted in it (the 2018 event-stream attack; npm removed it that year).

1. **A malware advisory without a MAL- record stops the release.** The dependency audit counted a package as malicious
   only through an OpenSSF `MAL-` record. GitHub files its own malware advisories as embedded malicious code (CWE-506,
   "Malicious Package in ..."), often with no fixed version; alone, such a finding was listed as "critical, no fix yet"
   and the release went on. `flatmap-stream` was stopped only because OpenSSF also filed MAL-2025-20690 for it. An
   advisory with CWE-506 is now a known malicious package, like a `MAL-` record. Checked on the scanner's real output with
   the `MAL-` record taken out: "1 known malicious package in the lockfile (GHSA-9x64-5r7x-2q53 (flatmap-stream@0.1.1))".
2. **One package counts once.** A package with several advisories is named once, by its `MAL-` record when it has one.

The image's scan (Artifact Analysis) is unchanged: its notes carry no CWE list, and it already blocks `MAL-` records.

Tests: 778 pass; ruff clean.

# 1.20.1: the lock for a Node.js 22 service

Found on 2026-10-19 while preparing the live exercise of Phase 18, by running the platform's own lock container on Docker
for a Node.js 22 service without a lockfile. The unit tests had replaced Docker with a stand-in, so they never ran it.

1. **The lock for Node.js 22 works.** Node 22's npm 10.9.9 cannot hold back new releases, so its container first installs
   npm 11.19.1 into a folder of its own. That folder had no `package.json`, so npm walked up to the service's
   `package.json` one folder above, installed itself there (adding npm to the service's dependencies) and left no
   lockfile in its own folder; the integrity check then crashed, and no Node.js 22 service without a lockfile could be
   imported. The folder now gets a `package.json` first. Run on Docker: npm 11.19.1 installed and checked, 65 packages
   locked, the service's own dependencies untouched. Node.js 24 and 26 use their own npm and were not affected.
2. **A failed lock says what failed.** The error quoted the last line the container printed, which after a crash is
   Node's bare version ("npm could not lock the dependencies: Node.js v22.23.3"). It now quotes npm's own error, else the
   error a crash names, else the last line.

Tests: 778 pass; ruff clean.

# 1.20.0: Node.js web services (Phase 18)

Roadmap Phase 18 (docs/ROADMAP.md 18.1 to 18.7, decisions D41 to D46), from the research review of 2026-10-19
(`docs/RESEARCH-2026-10-19.md` section 4). The platform took Python services only; it now takes a Node.js web service the
same two ways, from a repository that follows the contract or as it is, with npm's 2026 defaults built in. Python
services see no difference, and their contracts hash as before. Built and checked locally; the live exercise listed under
Phase 18 has not run yet. 1.19.0 stays Phase 17's number.

1. **The contract.** `runtime: node-http` with `node: "22"`, `"24"` or `"26"`; a contract names exactly one of `python`
   and `node`. Each line is refused from its end of life (22 on 2027-04-30, 24 on 2028-04-30; the runbook lists them),
   when a release or promotion is planned; a rollback is never refused. `build.scripts` lists the packages whose install
   scripts may run, each as `name@version`; a name alone, a range or `--all` is refused. A Node.js service is started by
   `node` itself, never through npm.
2. **The reader.** A folder with `package.json` and no Python manifest is read with the Node.js ladders of the review's
   section 4.2: the line (the Dockerfile, `.nvmrc`, `.node-version`, `.tool-versions`, `engines.node` read as an npm range,
   `devEngines.runtime`, else the newest Active LTS line: 24 until 2026-10-28, 26 from then), the npm lockfile checked
   entry by entry, what starts it, the port, the host, the health route, the settings, the install scripts and the tests.
   It refuses with the file and the fix what D46 lists. The first refusal now reads "no Python or Node.js code or
   manifest". The entry file and the modules it imports are read in up to three more passes, so a listen call in a
   configuration module (Sophia's) is found. A host read from a variable whose default is `127.0.0.1` gets `0.0.0.0` in
   the draft's `env`.
3. **The lock the platform makes.** For a repository without a lockfile, `npm install --package-lock-only
   --ignore-scripts --min-release-age=3` runs in a container of the service's own line on the controller's machine, by
   npm 11.10 or later. Node 22's npm 10.9.9 only warns about `min-release-age` and would lock without the hold, so its
   container first installs npm 11.19.1 by version and checks it against the integrity hash the registry published.
   Every container refuses an npm older than 11.10, and the lock stays resolved on the service's own line, since npm
   prefers the versions whose `engines` it meets. Every entry must carry its integrity hash and come from the npm
   registry before the lock is proposed as `apps/<service>/package-lock.json`.
4. **The pipeline.** `actions/setup-node` 7.1.0 is pinned by commit, with `package-manager-cache: false`. OSV-Scanner
   2.7.0, downloaded by version and checked against its SHA-256, reads the lockfile **before anything is installed**. For
   every trust level, the release stops on a known malicious package, a critical or high finding with a fixed version,
   an audit that did not finish, or an entry from outside the registry. Its summary goes into the release bundle
   (`dependency-audit.json`) and the receipt, and the repository's own `osv-scanner.toml` is never read. Then come
   `npm ci --ignore-scripts` (from the public registry whatever an `.npmrc` says), `npm audit signatures` as a note,
   `npm rebuild` for `build.scripts`, and `npm test`. When there is no test script, `node --test` runs and must find a
   test. The Python steps run for Python services only, and the policy step outputs the runtime, the line and the scripts.
5. **The image.** `templates/repo/Dockerfile.node` is `node:<line>-slim` by index digest, resolved 2026-10-19:
   `NODE_ENV=production`, `HOME=/tmp`, dependencies before the code, `npm ci --omit=dev --ignore-scripts` then the
   contract's `npm rebuild`, `npm cache clean --force`, `USER 65532:65532`, and an exec-form `CMD` with `node`. Google's
   buildpacks (`GOOGLE_NODEJS_VERSION`) build Node.js for reviewed code only. `node_modules`, `.npmrc` and `.yarnrc.yml`
   are in the deny-list and the buildpacks' descriptor. The Dockerfile policy adds two notes, which never refuse: an image
   started through npm, and an Alpine image for dependencies with native code.
6. **The drafter and the checks.** A Node.js repository that follows the contract gets its own files: the Dockerfile,
   `.dockerignore`, `test/health.test.mjs` on `node --test` (it starts the service on a free port), `platform.yaml`, CI on
   setup-node with the same install, Dependabot for npm with a seven-day cooldown, and `package-lock.json` made as above.
   `check_repository` asks a Node.js service for `package.json` and its lockfile instead of `requirements.txt`, and
   `analyze_repository` recognizes one.
7. **What the chat says.** A planned release names its runtime ("A Node.js 24 web service"). The release answer counts
   the audited packages, and the reader's choices list the Node.js line, the host and the install scripts. The steps of a
   Node.js run have their own words and advice, and a release proves its tests and its audit in the Node.js steps.
8. **Documents.** The contract page has the Node.js table and refusals, a new section 1b (lines, the lockfile, install
   scripts, the audit, cooldowns, converting pnpm, Yarn and Bun lockfiles), and the container notes (signals, memory,
   keep-alive). The runbook has the two end-of-life dates and the pins to refresh. The chat guide, the operator skill and
   its reference, and the handoff are updated too.

What differs from the plan, and why:

- A `build` script is refused only when the start command runs a file that is not committed. Sophia has one, and it
  builds a browser extension, not the server.
- Readiness routes (`/ready`, `/ping`, `/status`) and `/healthz` are offered to the reviewer, never chosen, as for Python:
  such a route may call other services, and Cloud Run keeps `/healthz` for itself. Prefixed routes such as `/api/health`
  are chosen.
- The audit runs before the install and the tests, not after them as Python's does, so no package's code has run when a
  malicious one is found. It also checks the lockfile's entries at every release, not only at import.
- The drafted Dependabot file writes a seven-day cooldown, as it does for pip, where the review recommended Dependabot's
  three-day default.

Tests: 778 pass (101 new); ruff and zizmor clean.


# 1.18.11: what the first tagged promotion showed

Found on 2026-10-19 when the demo's re-pinned staging release was promoted to check the release tag (`docs/VALIDATION.md`).
Production took the new version through 10, 50 and 100 percent, and every step of the job succeeded by 15:34 UTC. GitHub
marked the run completed only at 15:52, while its job still read in progress. The controller read it at that moment,
found the release step unfinished, and recorded the promotion as completed_unverified. So no release tag was written, and
the controller still took the earlier promotion for production's live release.

1. **A run that succeeded with a check not reported yet is read again.** For six hours, the background follow-up and
   `get_deployment_status` ask GitHub again about such a plan. Once every check has come in, it is recorded once more:
   the receipt, the outcome, and a promotion's release tag. Until then nothing is fetched or written. A run that did not
   succeed is never read again, and the audit trail gets a `recheck` event.
2. **What chat says** for an unverified release: GitHub can be late, so it may become a release by itself within six
   hours; until then, open the run.

Tests: 677 pass (2 new); ruff clean.


# 1.18.10: what the first report with production showed

Found on 2026-10-19 in the follow-ups of 1.18.8 (`docs/VALIDATION.md`). The demo's workflows were re-pinned to 1.18.8,
and its weekly cost report now shows staging and production together.

1. **No more "$-0.00".** Net of credits, a total can sit a fraction of a cent below zero (credits just above the cost),
   which printed as "$-0.00" in the issue, the report and chat. It now reads $0.00 ($0 in chat), and a real credit reads
   -$1.23.
2. **Parts below a cent say so.** The issue printed the demo's share as "$0.01 (staging $0.00, production $0.00)": each
   part rounded down on its own. Like chat, the issue now says "under $0.01" for a fraction of a cent.

Tests: 675 pass; ruff clean.


# 1.18.9: a name of the lab's own (Phase 16, part six)

Roadmap item 16.6 (docs/ROADMAP.md D39). An application in production can answer at a domain of the lab's own, as a
per-application choice with its cost shown. Staging keeps its `run.app` address.

1. **`domain: api.example.org`** in the contract (production only). Production's root then builds:
   - a global external Application Load Balancer with a Google-managed certificate;
   - an SSL policy that refuses TLS before 1.2, and a redirect from HTTP to HTTPS;
   - a Cloud Armor policy that answers 429 to any one address sending more than 600 requests a minute;
   - request logs at one in ten.

   The root prints `domain_address` for the domain's A record, and the certificate follows once the name resolves there.
2. **Who may call is unchanged.** Programs send the same Google identity token: the service now also accepts one minted for
   `https://DOMAIN` (a custom audience). `lab-members` apps keep Identity-Aware Proxy on the service, which Google documents
   as enforced on every way in, load balancers included; IAP is never set on the load balancer as well.
3. **What it costs, everywhere it matters.** About $25.25 a month whether or not anyone visits (Billing Catalog read on
   2026-10-19): the forwarding rules $18.25 (a project's first five share one charge, so the HTTP redirect and a second
   domain add nothing), the Cloud Armor policy $5 and its two rules $2. On top come $0.75 per million requests and $0.008 per
   GiB each way. The floor counts in every promotion's approval block and against the application's `cost_ceiling_usd`. A
   cost finding warns that it exceeds a $20 budget by itself, and the infrastructure plan's floor prices it the same way.
4. **The policy** refuses a load balancer's backend without a Cloud Armor policy, and an HTTPS proxy or SSL policy that
   would allow TLS before 1.2. The plan comment lists the name a load balancer answers at as an exposure change.
5. **Production's pipeline** may build it. In production only, the apply identity gains `roles/compute.loadBalancerAdmin`
   (it reads instances and networks but changes neither) and a custom role with only the Cloud Armor policy permissions,
   not `securityAdmin`'s firewall rules. The plan identity gains a read-only custom role for the two kinds of resource
   `networkViewer` leaves out. Staging's foundation is unchanged.
6. **A promotion says where the domain stands.** It notes when the contract names a domain that production's root does
   not build yet (ask for `add_production`), and otherwise where production also answers.

Not done: ingress restricted to the load balancer. Every release probes the candidate at its `run.app` tag address, which
restricted ingress would refuse, so the `run.app` address keeps answering behind the same sign-in. No application is
public, so the domain adds a name and a rate limit, not a new way in. Not tried live: the lab has no name to point at it
yet.

Upgrade, before the first domain: apply production's foundation (`scripts/apply-foundation.sh PROD_PROJECT production`) and
raise its `monthly_budget_usd` (docs/RUNBOOK.md, "A custom domain"). An application with its own workflows needs them at
1.18.9 before its `platform.yaml` names a domain. Roots without a domain plan no change.

Also recorded here: the cap-fire drill's pass on production after 1.18.8's fix (docs/VALIDATION.md).

Tests: 674 pass (9 new in `tests/test_domain.py`); ruff clean; `terraform fmt` and `terraform validate` clean for a
production root with a domain and for the foundation.


# 1.18.8: operations (Phase 16, part seven)

Roadmap item 16.7. Production gets what one operator needs to run it: every promotion leaves a tag, the weekly cost report
shows production's spend, a drill proves the spending cap closes production, and the runbook has an incident procedure.

1. **A release tag per promotion.** Once a promotion succeeds, the controller tags the promoted commit in the
   application's repository: `production/SERVICE/DAY-PLAN`, an annotated tag that names the plan, the staging release, the
   image's digest, who approved it and the run. It is written once and never moved; `git tag -l 'production/*'` lists
   what reached production and when. A tag that cannot be written (a repository the App is not installed on, a rule
   there) is named in the promotion's status and in `list_events` (`release_tag`); the release is not affected.
2. **Production's spend in the weekly cost report.** The billing account's one export holds both projects, and the
   application's staging identity already reads it, so no access changes. `add_production` now names the production
   project in the application's staging environment (`PRODUCTION_PROJECT_ID`), and the report reads both projects: the
   total, each project's share, and the application's own share in each. The issue and `get_cost_report` show them side
   by side. Without a production the report reads as before.
3. **`scripts/cap-drill.sh PROJECT`**, the cap-fire drill. It publishes a made-up budget notification at the cap's line
   to the budget topic, the path a real one takes (Pub/Sub, Eventarc, the workflow), checks that every managed service
   turned internal-only and answers 404 from outside, then reopens each one exactly as it was with the call the cap
   itself makes (no new revision, no `lab-deploy-cap-lifted` label, so the cap stays armed). It starts any database it
   stopped, and it looks again a minute later in case Pub/Sub delivered the message twice. It refuses a project whose cap
   could detach billing or whose cap is in force already, asks for **yes** first, and reopens what it closed even after a
   failure. No money moves.
4. **The incident procedure** (docs/RUNBOOK.md, "An incident") for one operator, in the shape of NIST SP 800-61 Rev. 3:
   detect and decide, mitigate first (with the command for each kind of harm), communicate, review. The runbook also
   has the cap-fire drill and the release tags, and the monthly checklist has the twice-yearly production drills.
5. **The drift check** has covered production's roots since 1.18.0. Its first run with a production, on 2026-10-19,
   planned `production-lab-demo` with production's own read-only identity and found no drift.
6. **What the first drill found, fixed here.** The drill's notification reached the cap's workflow in a second, and the
   workflow could not close the demo. Cloud Run answered "Permission 'iam.serviceaccounts.actAs' denied" on the service's
   runtime identity: Cloud Run asks whoever changes a service, even only its ingress, to be allowed to act as that
   identity. So the platform's own cap had never closed anything, in either project. Earlier checks ran only the
   below-cap and failure paths; Google's native spend cap, which was tripped live, stayed the working backstop in staging.
   - The fix: each application root lets the cap act as that application's runtime identity, and nothing else. The
     policy allows exactly that grant to the cap's identity.
   - The script: zsh skips the EXIT trap when an error ends the script inside a function. After publishing, the drill now
     checks each step itself, retries a passing API error (a 503 ended the first run), tries five times to reopen, and an
     interrupt still reopens.

Terraform: every application root, staging and production, gains that one grant when this merges. Production's apply
waits for approval in `infra-production`.

Upgrade, after the machine runs 1.18.8:
1. For each application with a production, ask "add production for APP" once more. It names production for the weekly
   report and changes nothing else.
2. An application with its own pipeline (not `contract: platform`) needs its workflows updated (`update_app_workflows`)
   for the report to pass the new variable. The platform's own `rightsize.yml` needs nothing.

Tests: 665 pass (11 new in `tests/test_operations.py`, including the drill run end to end against fake `gcloud` and
`curl`, with the Cloud Run API failing midway); ruff and zizmor clean; `terraform validate` clean.


# 1.18.7: production's data (Phase 16, part five)

Roadmap item 16.5 (docs/ROADMAP.md D39). Production's databases can be recovered to any moment of the last week. Postgres
gets a per-application shape with its floor shown, migrations stop instead of waiting on locks, and a drill proves a
restore. Staging keeps the smallest and cheapest shape.

1. **Recoverable to any moment of the week, in production.** Postgres keeps seven days of write-ahead log beside its week
   of daily backups, and Firestore seven days of versions. Both are billed as storage, cents at a lab's size. Staging keeps
   daily backups only. Deletion protection stays on everywhere.
2. **`needs.database_tier`** in the contract (Postgres only, production only) chooses the shape:
   - `standard` (default): `db-f1-micro` in one zone, about $8.57 a month, no SLA.
   - `ha`: the same with a standby in a second zone, about $17.13, still no SLA (shared core).
   - `sla`: one dedicated vCPU and 3.75 GB with a standby, about $100.43, the smallest shape Google's 99.95 percent SLA
     covers.

   Prices come from the Billing Catalog reading of 2026-10-16. Every promotion's approval block and the infrastructure
   plan's floor price the chosen shape, standby included. The developer contract says plainly that production has no
   SLA unless `sla` is chosen.
3. **A migration never waits long for a lock.** The migration job runs with `PGOPTIONS=-c lock_timeout=10s`, so a
   statement waiting more than ten seconds fails and the release stops before anything new serves, instead of freezing
   the table behind it. libpq drivers (psycopg) honor it. A `PGOPTIONS` of the contract's own wins, and the revision
   itself never gets it.
4. **`scripts/data-drill.sh PROD_PROJECT SERVICE`** restores a production Postgres database as it was fifteen minutes ago
   into a throwaway clone, checks that it runs and holds the `app` database, then deletes the clone (about a cent). It
   refuses a database without point-in-time recovery, and only ever deletes its own clone.
5. **The readiness checklist** names the recovery window and the drill.

Not done: squawk, and with it a check that a release never contracts a schema. The platform has no SQL-file convention to
lint, since applications run their own migration command; the contract asks for add-only migrations instead. The live
drill waits for an application with a database in production.

Terraform: the production roots of applications with a database change when they are next proposed (`add_production`);
staging's roots plan no change.

Tests: 654 pass (6 new in `tests/test_data.py`); ruff clean; `terraform validate` and `fmt` clean.


# 1.18.6: what the objective showed

Found on 2026-10-19 in the live exercise of 1.18.5 (`docs/VALIDATION.md`). Demo's production objective, its two alerts and
the minute uptime check are live, and the health view showed the error budget spent: the rollback drill's planted errors
are still in the 30-day window.

1. **A spent budget says so.** The note read "Only -28% of the 30-day error budget is left". It now says "The 30-day error
   budget is spent, 28% beyond it", or "has only 20% left" while some remains.
2. **"Updates its production root."** `add_production` for an application whose production exists, asked after a newer
   platform changed the root, said it "adds" the root. It now says "updates".
3. **The runbook** names the alerts as Cloud Monitoring shows them ("burning fast", "burning slow"). It also says the
   rollback drill spends a quiet application's error budget for 30 days.

Tests: 648 pass; ruff clean.


# 1.18.5: production's objective and its error budget (Phase 16, part four)

Roadmap item 16.4 (docs/ROADMAP.md D39). Every production application gets one objective with two burn-rate alerts and a
faster uptime check. The health view shows the budget left, and an application's first promotion carries a readiness
checklist. Staging is unchanged: its roots plan no change.

1. **One objective per production application.** 99.5 percent of requests are answered without a server error over a
   rolling 30 days. It is counted from Cloud Run's own request metric as server errors out of all requests, so the uptime
   check's calls to the health path count too and a quiet application still has a measure. It is a request-based
   objective on a custom service, so nothing depends on Service Monitoring detecting the service.
2. **Two alerts on its error budget**, with Google's starting points: burning fast (ten times the sustainable rate over an
   hour, an outage) and burning slowly (twice over a day, a leak). Both say what to do.
3. **The uptime check runs every minute from three US regions in production**, the regions the controller's own check
   uses: about 130,000 checks a month per application, inside the free allotment. Staging keeps every five minutes.
4. **`get_service_health` takes `environment: production`.** It reads production's project, its live promotion and what is
   left of the error budget, and says so when less than a quarter remains. A production root rendered before this
   version has no objective, and the view says to propose it again with `add_production`.
5. **The readiness checklist at an application's first promotion** is built by code from what is true now. It covers the
   health probe, the alerts and objective in the production root, the runbook, backups, capacity, and the rollback path.
   It appears in the approval block, and each shortfall becomes a warning with what to do.

Cost: none. Objectives are free, and alerting policies are free until at least 2027-09-01.

Applying it: after the machine runs this version, ask for `add_production` for each application with a production. It
proposes the production root again with its objective; a person merges it and approves the apply in `infra-production`.

Tests: 648 pass (7 new in `tests/test_slo.py`); ruff clean; `terraform validate` and `fmt` clean.


# 1.18.4: what the rollback drill showed

Found on 2026-10-18 in the rollback drill of 1.18.3 (`docs/VALIDATION.md`). A fault the probes cannot see failed its bake
at 10 percent (16 of 31 requests answered 500), and the earlier split came back for everyone. The fixed version then
passed every step of the split on real traffic, and `rollback_production` moved production back in 16 seconds once
approved.

1. **A bake that fails at a step names the step.** It said "Bake failed 69 seconds after promotion"; at a step of the
   split it now says "after the switch to 10 percent of the traffic". A staging release, which moves everyone at once,
   still says "after promotion".
2. **`scripts/promotion-traffic.sh`** sends real requests with your own sign-in token each time a promotion's new revision
   takes a new share (10, 50, 100 percent), so the bake judges real traffic at every step. It stops at 100 percent, or
   reports when the earlier split comes back. The runbook's drill uses it in place of a loop typed by hand.

Tests: 641 pass; ruff clean.


# 1.18.3: the split release and production's rollback (Phase 16, part three)

Roadmap item 16.3 (docs/ROADMAP.md D38). A promotion no longer moves all of production at once, and production can be
rolled back by tool. Staging releases are unchanged, so no application's workflows need regenerating, and no Terraform
root changes: the rollback runs in the one workflow production's deployer already trusts.

1. **The split release.** After its candidate passes the probes, a promotion takes 10, 50, then 100 percent of
   production's traffic (`production.rollout`), and the bake watches each step for `production.bake_seconds` with its
   rules (D22): a crash, more than 5 percent of answers being server errors, or latency above three times the earlier
   version's. A broken rule at any step brings the earlier split back for everyone. Too little traffic to judge passes
   with a note, as in staging.
   - Each step keeps the earlier split's proportions, in whole percents. While traffic is split, the probe asks the
     candidate's own address.
   - The receipt records every step (its share, the bake's verdict, requests and server errors), and each phase says at
     which share.
   - The promote job's time limit rises to 75 minutes for three watched steps.
2. **Secrets at their version.** In production each secret runs at the version that was newest when the promotion
   released it, the same for the migration and the revision. The receipt names each one (name and version number, never a
   value). A new version reaches production only by promoting again (`docs/RUNBOOK.md`, "A new secret version"), through
   the same split. Staging keeps `latest`.
3. **`rollback_production`**, a new tool (27 in all). It returns production's traffic to the split before its live
   promotion, in seconds and without a build. It runs as `promote.yml`'s new `rollback` operation, so it waits for a
   production reviewer's approval like every job in `production-<service>`. The runner refuses if the checkpoint belongs
   to another promotion or traffic moved since. The rolled-back promotion stops being production's live release.
4. **`promote.yml` has two operations**, `promote` (the default) and `rollback`, with the run named after each. The
   promotion inputs are optional, and `labdeploy.promotion check` holds each operation to its own inputs. A rollback's
   approval block names the promotion it undoes and the split coming back.
5. **Claude's answers** say which share a promotion is at or failed at ("failed its watch with 10% of the traffic"), list
   the steps a successful promotion took, and offer "roll APP back in production". The runbook gives the rollback drill:
   a fault the probes cannot see (`FAULT_EVERY` in the demo), caught at 10 percent.

Not yet: the drill itself, live (it needs a pull request on the demo application and real traffic during the 10 percent
step); SLOs and production data (16.4, 16.5).

Tests: 640 pass (10 new in `tests/test_split.py`); ruff clean; zizmor clean.


# 1.18.2: what the production setup showed

Found on 2026-10-18 in the live exercise of 1.18.0 and 1.18.1 (`docs/VALIDATION.md`): the production project's foundation,
`add_production` for the demo through its four steps, and the demo's first promotion, which served staging's exact image
in production with the operator's approval.

1. **A new project's first foundation apply finishes by itself.** It enables APIs and uses them seconds later, before
   Google has them ready: 6 of the production project's 104 resources stopped with "has not been used in project ... or it
   is disabled", and a second run finished them. `scripts/apply-foundation.sh` now waits that one error out once: a minute
   later it plans and applies the rest of the same configuration. Any other failure still stops it.
2. **No administrator can skip a production approval.** GitHub lets administrators bypass an environment's required
   reviewers unless told otherwise, and `production-lab-demo` was created that way. The controller now creates every
   `production-<service>` environment with the bypass off, and `promote_to_production` refuses while it is on;
   `add_production` turns it off again. The live environment was closed by hand at once.
3. **A promotion checks the commit GitHub's attestation names.** An application's own deploy workflow can build an older
   commit of main on a run that started at main's tip, and GitHub's attestation names the tip. Such a release would have
   failed the promotion's check (closed, but wrongly). The promotion now checks the commit the image's build run started
   on, following a reused image back to the run that built it, and records it in its statement; `promote.yml` takes it as
   the optional input `attested_commit`. Found reading the template; the live promotion was of a release built at the tip.
4. **Approving from a terminal** (`docs/RUNBOOK.md`, Production): the operator's approvals in the browser did not reach
   GitHub twice, while `gh` could approve; the runbook gives the command for a promotion or an apply that waits.

Tests: 630 pass; ruff clean; zizmor clean.


# 1.18.1: promotion by digest (Phase 16, part two)

Roadmap item 16.2 (docs/ROADMAP.md D37): a staging release reaches production only by promotion. The promotion takes the
exact image staging deployed, never a rebuild, and nothing happens until one of production's reviewers approves the run on
GitHub. Nothing changes for staging, and no application's workflows need regenerating.

1. **`promote_to_production`**, a new tool (26 in all), promotes the newest staging release that succeeded with signed
   provenance, or the one named. It refuses a release made under the operator's provenance exception, one rolled back since,
   and one older than 30 days, whose image is no longer scanned. It also refuses while production is not ready: the
   protected environment, the production root on main, or the staging read access is missing. The plan's approval block
   names the commit, the image by digest, the staging release it came from, the cost bounds in both environments and who
   may approve. Promotions stay out of staging's views: the live release, its rollback and its image reuse are unchanged,
   and `list_applications` shows each production's own live release.
2. **The platform repository's `promote.yml`**, started by the controller as the GitHub App, has two jobs.
   - **describe** has no cloud identity. It checks the inputs, reads the exact specification staging released (the
     application's `platform.yaml` at the commit, or the contract at the platform commit), holds it to production's
     policy, and writes the approval block to the run's summary for the reviewer.
   - **promote** waits in `production-<service>` until a reviewer approves, and first refuses to go on unless GitHub
     recorded that approval for this very run. It then checks the staging image's provenance (GitHub's attestation bound
     to the commit and `main`, or the statement signed with the application's Cloud KMS key in staging). It copies the image
     by digest with a carbon copy into production's own registry and checks that the digest is unchanged. It signs a SLSA
     verification summary with production's key, naming the source and who approved. Finally it releases with the same
     runner as staging: a candidate with no traffic, the probes, the switch, the bake, and the previous version restored
     on any failure. The receipt records the staging release, the image and the approvers.
3. **`add_production` has four steps now.** After the production root is merged, it waits until that root's apply has
   succeeded (which waits for a person's approval in `infra-production`). Then it creates the environment and opens a second
   pull request on the application's staging root: `promotion_readers` lets the production deployer read that one
   registry and see the public half of that one provenance key, nothing else. A person merges it too.
4. **Plans stay staging-only**: a plan for production is refused with the way forward, and production takes the same limits
   as staging (2 CPUs, 2048 MiB, 3 instances).
5. **Claude's answers**: the instructions, the skill and the chat guide say production is reached only by promotion; the
   status of a promotion reads "waiting for approval" with who may approve and the run's link, then its own steps in plain
   words.

6. **The foundation script switches on Cloud Resource Manager and Service Usage first.** A new project has neither, and the
   root reads the project through the first, so the production project's first plan failed (found on 2026-10-18).

Not yet: a production rollback by tool (16.3, with the split release); until then `docs/RUNBOOK.md`, Production, gives the
command.

Setup: `docs/SETUP.md` 7n, steps 7 and 8. Terraform: each application with a production gets the staging root's read
access through `add_production`; other roots plan no change. The skill gained the new tool; upload it again.

Tests: 629 pass (13 new in `tests/test_promotion.py`); ruff clean; zizmor clean; `terraform validate` and `fmt` clean.


# 1.18.0: production as a project of its own (Phase 16, part one)

The first part of Phase 16 (docs/ROADMAP.md D37): production gets its own Google Cloud project, created under the
organization from the start because the tier for protected health information (Phase 17) needs it there, with its own
foundation, state and identities. Each application gets its own production root and a protected GitHub environment.
Nothing is promoted yet: promotion by digest (16.2) and the split release (16.3) come next. Staging is unchanged.

1. **The production foundation** is the same Terraform as staging's, applied by the operator with
   `scripts/apply-foundation.sh PROJECT production`. It has its own state (`project/production` in its own bucket), its
   own budget and alerts named for production, and its own pipeline identities: the plan identity answers only the
   platform repository's plan workflow on a pull request, and the apply identity only its apply workflow on `main` in
   the environment `infra-production`. Each environment now names its own settings file (`staging.tfvars`,
   `production.tfvars`). The script refuses production while `terraform.tfvars` exists, because Terraform loads that
   file by itself and staging's values (its controller machine, its pipeline) would reach production. Git now ignores
   every `*.tfvars`, so neither file can be committed.
   `viewer_members` gives the controller's machine in staging the observer's viewer roles in production, so health and
   logs will work for production apps.
2. **The configuration's `production` block** names the project, its number and the GitHub accounts that approve a
   promotion (`reviewers`, one to six). It also holds the split release's steps (`rollout`, default 10, 50, 100) and how
   long each step is watched (`bake_seconds`), which 16.3 uses. The controller refuses a production in the staging project.
3. **`add_production`**, a new tool (25 in all), works one step at a time and is safe to repeat.
   - The first call registers the application's production in the controller's database, by convention:
     `<service>-deploy` and `<service>-probe` in the production project, the `<service>-gh` identity pool, the bucket
     `<project>-<service>-recovery` and the environment `production-<service>`. It works the same for an application of
     the configuration file and for one onboarding registered.
   - It then opens the production root, `infra/gcp/apps-production/<service>`, as a pull request. **A person merges it:
     the controller never merges production infrastructure.** The pipeline plans it with production's read-only
     identity and applies it with production's apply identity.
   - Once the root is on `main`, the next call creates the platform repository's environment `production-<service>`. It
     deploys only from `main`, every job in it waits for one of the reviewers, and self-review is prevented. The
     controller starts runs as the GitHub App, so the operator's approval counts. The environment also gets the eight
     target variables.
4. **The production root trusts only the platform repository's `promote.yml` in `production-<service>`.** The deploy
   identity's Workload Identity condition names that workflow and that environment, so neither the application's
   repository nor staging's workflows can reach production. The service module accepts exactly that shape and nothing
   looser, and its dashboard says which environment it shows.
5. **The pipeline covers both projects.** The plan, apply and drift workflows find production roots under
   `infra/gcp/apps-production/` and run them in jobs of their own with the `INFRA_PRODUCTION_*` variables. A change to
   the shared service module plans and applies every root in both projects. Drift names production roots
   `production-<service>`. CI validates and lint-checks those roots too.

Setup: `docs/SETUP.md` section 7n, once. Terraform: the production foundation once, by the operator; staging's foundation
plans no change. The skill (`skills/fast-deploy-operator`) gained the new tool; upload it again.

Tests: 616 pass (12 new in `tests/test_phase16.py`); ruff clean.


# 1.17.3: the controller merges its own pull request only after its checks pass

Found on 2026-10-18 in the live onboarding through the GitHub App (`docs/VALIDATION.md`): lab-bare has no required checks,
so GitHub called the onboarding's workflows pull request (lab-bare#6) mergeable while its tests were still running, and
the controller merged it twelve seconds before they ended. They passed, but the promise in the configuration ("merge
the application's own pull requests once the policy and the checks have passed") was not kept. The gap is older than
the App; the App's test only showed it.

1. **Before it merges its own pull request**, the controller now reads every GitHub Actions run on the pull request's
   head (with the Actions permission the App already has) and waits until all have finished; a failed or cancelled run
   stops the onboarding with its name and link, and asking again after the fix continues from the merge. With no run at
   all it gives GitHub two minutes to start them, then treats the repository as one without checks.
2. **The same rule guards release pull requests** on the platform repository, whose required checks already made
   GitHub wait.

Tests: 604 pass (3 new in `tests/test_merge_gate.py`); ruff clean.


# 1.17.2: what the move to the machine showed

Found on 2026-10-18 in the live exercise of Phase 15 (`docs/VALIDATION.md`).

1. **The machine may list its own folder the way gcloud asks**: `gcloud storage ls gs://BUCKET/machine/` lists the
   prefix `machine`, without the slash, and the bucket condition only allowed `machine/`, so the restore drill on the
   machine failed. The condition now matches the prefix without the slash; reading and writing objects stays limited to
   `machine/`.
2. **The machine writes its own log lines to Cloud Logging** (`roles/logging.logWriter`): Google's guest agent and the
   startup script were refused and retried into the serial console. Inside Logging's free allotment; the controller's
   own log stays on the machine.
3. **`scripts/tool.sh` and `scripts/ctl.sh` call the machine** when `config.local.yaml` names `machine_url` (the move
   writes it), else the laptop; `FAST_DEPLOY_URL` chooses another.
4. **The GitHub App's setup follows GitHub's current pages** (docs/SETUP.md section 4): a link that fills in the form,
   the key under Credentials, Key pairs, New key, and no Variables permission, which the controller never used.

Terraform: the foundation once (two IAM changes). The machine: `scripts/machine.sh update` to this commit.

Tests: 601 pass (2 new); ruff clean.


# 1.17.1: clearer answers in Claude chat

Found on 2026-10-17 in a read-only review of seventeen tools against the running controller (`docs/CLAUDE-CHAT.md`, "What
answers look like"). The shape held; these are the gaps. Two facts shaped the fix: claude.ai does not pass the connector's
own instructions to the model (anthropics/claude-ai-mcp issue #93, open since March 2026), so only the tool descriptions,
each result's `summary` and the uploaded skill reach Claude; and the tool descriptions are pinned by checksum
(`schema/tools.sha256.json`), so changing them is a reviewed diff. Nothing here changes what any tool does in the cloud.

1. **Five tools answered with raw JSON** (`list_plans` at 30 KB with epoch timestamps and 32-character ids, `list_events`,
   `get_cost_report`, `analyze_repository`, `propose_rightsizing`). Each now starts with a `summary` in the one shape:
   the plan list as a table of the newest eight (app, outcome in plain words, 7-character commit, time in UTC) with the
   history's counts; the audit trail as a short table with readable times and an error count; the measured cost report
   with actual spend, measured use, bounds, proposals and drift; the repository analysis with the detected facts and
   the questions a person confirms; the right-sizing proposal with its pull request, or "nothing to propose".
2. **Failed and cancelled releases in the operator's words.** The Next line repeated the pipeline's sentence ("see
   vulnerability-summary.json", "or no receipt was uploaded. The recovery run that followed concluded 'success':
   no_recovery_needed") and showed a raw `KeyboardInterrupt:` as the error. The chat now says what happened and who does
   what ("the image scan found serious problems with fixes available; the developer updates the libraries or the base
   image, then release again"; "it was cancelled before anything changed, so the previous version kept serving. The check
   that followed confirmed nothing needed restoring"), keeps the GitHub run link for the detail, and leaves the technical
   sentence in `next_action` (`labdeploy/status.py`) for whoever asks.
3. **A rollback names commits, not revisions** (`lab-notebook-r9111fd55b0d94066`): the controller records the previous
   version's commit with the rollback when it released it, and the answer shows both 7-character commits, else "the
   previous version".
4. **Log lines**: a rollback is suggested only for WARNING and above; INFO and DEFAULT lines say they are ordinary output
   and how to ask for the errors.
5. **Cost estimates** with a traffic scenario say its best and worst monthly cost for the given requests, and a finding is
   never cut mid-sentence.
6. **Onboarding, days later**: the complete state no longer promises to follow the first release once it is more than an
   hour old; it says when the app was onboarded, which plan its first release was, and to ask about the app's health or
   releases. An app registered by hand (`demo`) was told to call `onboard_application`; it now says it was registered by
   hand, which release is live, and points to the application list and the health view.
7. **Repository checks** list the refusals of a repository taken as it is one per line (file, problem, fix) instead of
   one 400-character sentence.
8. **Every tool parameter has a description** (minutes, severity, limit, wait_seconds, path, as_is, auto_deploy,
   commit_sha, platform_commit, requests_per_month, average_request_seconds and the rest), and every tool's description
   ends with the one answering rule claude.ai lets through: "Answer from `summary` as written (bold outcome, bullets,
   Next line); the rest is detail for when the operator asks." Each description stays under 500 characters, so
   `onboard_application`'s was shortened. `schema/tools.sha256.json` was regenerated for the deliberate change.
9. **The controller status** no longer says "nothing to do" beside "weekly cost report not run yet"; it says the report
   runs on its own every week and to tell the operator only if it is still missing after a week.
10. **The skill and the page** (`skills/fast-deploy-operator/SKILL.md`, `docs/CLAUDE-CHAT.md`) say that claude.ai does not
    read the connector's instructions, so the skill and the tool descriptions are the only rules, and that the skill must be
    re-uploaded after every release that changes `skills/`.

Nothing to apply or regenerate; restart the controller and re-upload the skill zip in Claude (Customize → Skills).

Tests: 599 pass (15 new in `tests/test_chat.py`); ruff clean.


# 1.17.0: the controller off the laptop (Phase 15)

Asked for on 2026-10-16 with the defaults of D36: the controller moves from the operator's laptop and its ngrok tunnel to
a machine of its own. Built and tested; the live move waits for the GitHub App and the foundation apply. No DNS name is
needed (the operator's choice on 2026-10-17, in place of D6's): Claude reaches the machine at its own reserved address.

1. **The machine** (15.2, `infra/gcp/modules/project/controller.tf`, set `controller` in the foundation's tfvars): one
   Always Free `e2-micro` in us-central1-a with a 30 GB standard disk, Debian 13, Shielded VM, in a network of its own:
   HTTPS and HTTP (for Let's Encrypt) open, SSH only through Identity-Aware Proxy with OS Login and no keys in metadata.
   A reserved address stays the same through a replaced machine. It costs that address, about $3.65 a month, plus a few
   cents; the machine and its disk are inside the free tier.
   **Claude reaches it at that address**, with no name to buy or set: since January 2026 Let's Encrypt certifies an IP
   address itself, in its short-lived profile (six days, free like every certificate, renewed by Caddy every few days;
   the uptime check checks the certificate). Caddy 2.11.7's certificate library allows Let's Encrypt IP certificates, and
   is told which one to show a client that connects by address (no server name). Google's automatic name for the
   address would not do: only Google's own CA may certify `googleusercontent.com` names. A DNS name stays an option
   (`domain`, and `--domain` for the move).
2. **Its own identity, a refinement of D36**: `lab-deploy-controller` holds the observer's viewer roles, read and write
   on its own folder `machine/` of the backup bucket (an IAM condition), and access to the GitHub App's key; the observer
   stays viewer-only, so impersonating it from a laptop reaches neither.
3. **The startup script** (`controller-startup.sh`): Caddy 2.11.7 and Litestream 0.5.17 pinned by their published
   checksums; three systemd services with `Restart=always` (Caddy with its admin endpoint off, the controller and
   Litestream as an unprivileged user with a read-only system); a swap file; security updates with a reboot at 09:00 UTC
   when one needs it; the controller installed only from a commit on main, with `--require-hashes`. On a fresh disk it
   restores the machine's configuration, commit and database (the Litestream replica, else the newest nightly copy),
   and it never starts the controller with an empty database. `fast-deploy-install` updates with a rollback when the new
   commit does not answer within a minute.
4. **Docker on the machine, contained** (found while building: the plan had missed it). Onboarding compiles lock files in
   a container of the application's Python (D27), and resolving dependencies can build a package and run its code. So
   Docker starts only when used; its containers use public DNS and can reach neither the metadata server (the machine's
   identity, and through it the GitHub App's key) nor any private address; and every lock container, on the machine and
   on a laptop, now runs without Linux capabilities or privilege gain, with limits on processes and memory.
5. **The GitHub App** (15.1, D14, `labdeploy/github.py`): an installation token per repository, kept until five
   minutes before it ends, minted with the App's key, which the controller reads from Secret Manager (the machine's own
   identity there, the operator's gcloud sign-in on a laptop) and keeps in memory; a refused signature reads the key
   again, so a rotation needs no restart. A repository without the App is read with the platform repository's token
   (public data only) and refused for anything else, with what to install. The key's secret is
   `FAST_DEPLOY_GITHUB_APP_KEY`: application runtimes read secrets named `<service>-NAME`, and no service name can start
   with an uppercase letter. A fine-grained `GITHUB_TOKEN` still wins, so a laptop keeps working; `scripts/start.sh`
   uses the App when `github_app` is set.
6. **Durability** (15.3): the nightly copy is made with `VACUUM INTO` and goes to the host's own folder of the bucket
   (`backup_prefix`: `controller` for a laptop, `machine` for the machine). Litestream keeps the machine's database in
   `machine/litestream/`, at most a minute behind, with intervals set for cost; retired versions there are pruned after a
   day. The restore drill also restores the live copy and says how far it is behind; the watchdog reports a stopped
   Litestream.
7. **Reachability** (15.4): an uptime check on `/healthz` every minute from three US locations, with a certificate check
   and an alert when more than one fails for five minutes, beside the GitHub ping.
8. **The move** (15.5): `scripts/install-machine.sh APP_ID [--domain NAME]` checks the laptop's controller is stopped
   (and that a given name points at the machine), exports the database and the configuration rewritten for the machine
   (`python -m labdeploy.machine export`), imports them through Identity-Aware Proxy, waits for `/healthz`, records
   `machine_url` so `scripts/start.sh` stays off while the machine answers, and points `CONTROLLER_HEALTH_URL` at it.
   `scripts/machine.sh` does the rest: status, logs, restart, update, configuration, backup, drill, and a hold that
   survives reboots while a laptop takes over. The runbook's "The controller's machine" covers a machine that is gone, the
   laptop as the fallback, and key rotation.

New settings: `github_app`, `backup_prefix`, `litestream_replica`, `machine_url`. Requires regeneration of app workflows:
no. Terraform: the foundation once, with `controller` set. The skill changed: rebuild the zip and upload it to claude.ai.

Tests: 584 pass (20 new); ruff clean; `terraform fmt` and `validate` clean on the foundation root; both Caddyfiles adapt with Caddy 2.11.7.


# 1.16.1: what the live trial of 1.16.0 showed

Found on 2026-10-16 in the live exercise of Phase 14 (`docs/VALIDATION.md`); nothing here changes what is deployed.

1. **An image is reused when its inputs match**, comparing what the build section and the Python set rather than raw
   dumps: 1.16.0's new `build.network` made every application's first release after the upgrade build again.
2. **The scan's line says whether the gate stopped the release**: since every unknown app's gate is on, 1.16.0's answer
   said "it blocked the release" for one the gate had let through (none of its five high findings could be fixed yet).
   The scan now records `blocked`.
3. **Unconfirmed patterns in installed packages are counted, not listed**: the image of a real application held fourteen,
   all in third-party files (URL patterns in httpx, pip and pandas tests, two in the system's package lists). A working key
   is still reported, and still stops unknown code, wherever it is.
4. **Refusals that share a fix are told once**, with every file they name (two committed environment files repeated the
   same long fix).

Tests: 564 pass (4 new); ruff clean.


# 1.16.0: safety for code nobody has reviewed (Phase 14)

Asked for on 2026-10-16 with the default of D35: code nobody has reviewed (a repository taken as it is) is built and run
under stricter rules; the lab's own code keeps its behaviour, with reports where unknown code gets refusals.

1. **Trust levels** (D35): `trust` (reviewed | unknown) and `egress` (internet | private) in the contract. Unset, trust
   follows where the contract lives (reviewed in a repository's own `platform.yaml`, unknown in one the platform keeps)
   and egress follows trust, so no existing contract changes and nothing releases again on its own. Plans, their approval
   block and the release receipt say how the code is contained.
2. **The Dockerfile policy gate** (14.1, `labdeploy/dockerpolicy.py`), before anything of the repository runs: a download
   piped into a shell, `sudo`, `ADD` of a URL without `--checksum`, disabled TLS or signature checks (curl -k, pip
   --trusted-host, apt --allow-unauthenticated and their kin), `RUN --security` or `--network=host`, a copied `.git`,
   environment file or key, and, for unknown code, a final image running as root; each refused with its line and the
   fix. The build never passes `--allow`. Trivy 0.74.0's configuration findings (high and critical) are reported beside.
   The reader applies the same policy before a contract is proposed.
3. **Secret scans** (14.2, `labdeploy/leaks.py`): TruffleHog 3.97.4 on the checkout before the tests and on the built image,
   checking each finding with its provider; a key that works right now stops the build of unknown code and is reported for
   the lab's own; the values never leave the scanner (the annotations name the detector, the file and the line). A
   repository's own Dockerfile without a `.dockerignore` gets the platform's deny-list for its build, never committed; a
   build context over 1 GiB is refused. One scanner where the roadmap named two: TruffleHog covers the checkout as gitleaks
   would, and only it can tell a working key from a pattern.
4. **No cache** (14.3): every `docker build` says `--no-cache`, no workflow restores a cache (a test asserts both);
   `build.network: none` builds with no network at all for vendored dependencies.
5. **The vulnerability gate** (14.4): for unknown code a fixable critical or high finding, or a scan that did not finish
   in five minutes, stops the release; a known malicious package (OSV's MAL- advisories) stops every release; the lab's
   own code keeps the operator's switch for critical findings. An image over 30 days old is never reused: the release
   builds it again, so it is scanned again.
6. **Containment at run time** (14.5, 14.6): unknown code sends all its traffic into the lab's network, which has Private
   Google Access and no Cloud NAT, so it reaches Google's APIs and the lab's private network and never the internet unless
   its reviewed contract says `egress: internet`; its revisions and migration jobs carry the network tag
   `lab-deploy-contained` and run in the second-generation environment. The infrastructure policy refuses a NAT, a router
   or an internet route in an application root. Threat detection (Security Command Center Premium) is the operator's
   switch, documented in the runbook.
7. **The parts list** (14.7, `labdeploy/sbom.py`): Trivy makes a CycloneDX SBOM of every new image; it is attested next to
   the provenance (with the application's Cloud KMS key, or GitHub's attestation for a public repository's own pipeline)
   and summarised in the receipt.
8. **Answers**: a finished release says what the checks before the build found and whether the code ran contained.

Open from Phase 14, each for a reason: Binary Authorization (14.8) reads only its own attestations under one project-wide
rule for Cloud Run, so the per-application keys need a shared attestor or another signing step (the research's open point,
to settle in a dry run of its own); a project of its own (14.9) shares the second-project machinery Phase 16 builds;
inspecting data files with Sensitive Data Protection (14.10) needs credentials in a step that never runs the
application's code, before files reach the registry, which is a design of its own.

Re-pinning the applications' workflows brings the gates to the lab's own repositories; their behaviour changes only where
they now report. The platform repository's pipeline has them at once.

Tests: 560 pass (34 new in `tests/test_phase14.py`); ruff and zizmor clean.


# 1.15.1: what the first live trial of a repository as it is showed

Found on 2026-10-16 while taking `llm-debate-agent` (its `backend/` folder) and `lab-demo-two` as they are, through the
running controller; nothing here changes what is deployed or how.

1. Settings are called secrets only when their last part says so (`OPENAI_API_KEY`, `SECRET_KEY`, `GITHUB_TOKEN`,
   `DATABASE_URL`); flags and limits such as `CORS_ALLOW_CREDENTIALS` and `MAX_OUTPUT_TOKENS` no longer are.
2. An application name given to `import_contract` or `onboard_application` wins over the name in the repository's own
   `platform.yaml`.
3. Onboarding as it is, without a name, of a folder with a generic name (`backend`, `api`, `src`) names the service after
   the repository and the folder (`llm-debate-agent-backend` would be cut at a dash), never `backend`.
4. The contract pull request's first line names the repository and the folder with intact formatting.
5. `check_repository`'s answer gives the fix of an as-is refusal ("the service lives in backend: take that folder"),
   and keeps "I can" capitalised.
6. A buildpacks image leaves out the repository's `Dockerfile` and `.dockerignore` too (found in lab-demo-two's image).

The live exercise of 1.15.0 is recorded in `docs/VALIDATION.md`: two repositories released as they are by the platform's
own pipeline, a contract change released by its merge, refusals on four real repositories.

Tests: 526 pass (4 new); ruff clean.


# 1.15.0: a repository as it is (Phase 13)

Asked for on 2026-10-16 with the defaults of D31 to D34: deploy a repository without adding anything to it. The contract,
the build inputs and the pipeline move into the platform; repositories that follow `docs/DEVELOPER-CONTRACT.md` keep
everything as before.

1. **The reader** (`labdeploy/reader.py`, 13.5, D34): one service of a repository read by the precedence ladders of the
   2026-10-16 review (how it is built, the Python version, the lock, the start command, the port, the health route, the
   settings it reads, its needs, its tests, its Compose service), each choice with the file it came from and the files it
   set aside. It refuses rather than guesses: several candidate apps or frameworks, Django without `WSGI_APPLICATION`, no
   dependency list, conda-only or system packages for a build the platform makes, a Python outside 3.11 to 3.15, a
   committed secret (recognized by its name, never opened), a notebook or a file over 100 MiB (or in Git LFS) as the
   app; each refusal names the file and the fix. `check_repository` reports what it would do as `as_it_is`, with the
   services a repository holds (one per Compose build, else one per folder with a manifest and an entry file).
2. **The contract the platform keeps** (13.1, D31, D32): `apps/<service>/platform.yaml` in the platform repository, the
   same model plus `source` (`repository`, `path`, `commit`), recorded in the registry as `contract: platform`.
   `import_contract` (a new tool) opens the pull request with it and the locks the platform made: `uv export` of a
   `uv.lock`, `poetry.lock` and `Pipfile.lock` read at their locked versions and hashes, anything else compiled once with
   uv in a container of the app's Python, the server that starts it added when the repository does not list it, and a
   lock of the service with its test tools. A `platform.yaml` in the repository is imported once as the draft. Merging is
   the review: the file never carries `reviewed`, and the controller never merges it. `onboard_application` with
   `as_is: true` runs the whole path, waiting for that merge. Plans bind the contract commit and the source commit, and
   the approval block shows both.
3. **Releases are pull requests** (D32): `deploy_latest` opens one that moves `source.commit` and nothing else; the
   controller merges it only under the application's `auto_merge`, when the requester may merge their own requests, the
   change is exactly that line and the checks passed. A newer request closes the older one. A new scheduler job
   (`contracts`, every minute) releases what any merge to a contract put on main, once. `auto_deploy` opens the pull
   request when main moves.
4. **The platform's own pipeline** (13.2, D33): `.github/workflows/deploy.yml` is generated from `templates/deploy-gcp.yml`
   (a test keeps them equal), with the application, its repository, folder and the contract commit as inputs; the
   application is checked out read-only at its commit, with a GitHub App installation token narrowed to that repository
   when `APPS_APP_ID` is set, else the run's own token (public repositories). `recover.yml` and `rightsize.yml` run once
   per service under `apps/`. Each runs in the environment `staging-<service>`, and the service module's federation
   trusts exactly those three workflows of the platform repository on `main` in that environment. The provenance is signed
   with the application's Cloud KMS key and names the source repository and commit, the contract commit, the recipe and
   the Dockerfile's and the lock's SHA-256; it is verified before the release. The receipt carries the platform run, the
   application commit and the contract commit. Private repositories are refused while the platform repository is public.
5. **Builds from outside** (13.3): `build.recipe` is `repository` (the default: the service's Dockerfile, else its
   Procfile), `platform` (the platform's own Dockerfile, rendered at build time beside a copy of the service made through
   a deny-list of secrets, tests, notebooks and caches) or `buildpacks`; `build.entrypoint` is the start command the
   platform builds in (`GOOGLE_ENTRYPOINT` for the buildpacks). Every buildpacks build now uses the platform's project
   descriptor with the same deny-list. Without test tools of its own, a service's tests get the platform's pinned pytest
   (`tests.lock`).
6. **Run-time defaults** (13.4): `command` and `args` replace the image's ENTRYPOINT and CMD (never on a buildpacks
   image, whose launcher they would bypass); `health_path: null` starts the service under Cloud Run's TCP startup probe,
   the release probes accept any answer below 500, the uptime check any answer, and the receipt and the plan say the HTTP
   probe is off.
7. **Documents**: the contract page opens with what the platform assumes when given nothing; `docs/CLAUDE-CHAT.md`
   "Deploy it as it is"; the runbook names the platform repository as the single control point whose protection never
   loosens; the playbook skill knows `import_contract` and `as_is`. CI answers in seconds for a pull request that only
   changes `apps/`.

A fix found on the way: the plan of this change wanted to start `mono-admin`'s database again, stopped on purpose two
days before. Terraform owned its activation policy, so any apply undid a stop by the spending cap or the operator. The
module now leaves the activation policy to them (`ignore_changes`); creating an instance still starts it.

Drafts for repositories that follow the contract read exactly as before (`draft_dump`), and Terraform roots of existing
applications render unchanged. Re-pinning the existing applications' workflows is optional: the template's new steps
behave as before for them, except that a buildpacks image now leaves out secrets, tests and caches, and tests without
a `requirements-dev.txt` get the platform's pytest instead of failing.

Read-only before the merge, the reader took four of the operator's own public repositories, and they improved it: names
are cut at a dash and never a generic folder (`backend`, `api`, `src`); a root without a manifest names the folder
that has one; environment files by other names (`env.prod`, `env.yaml`, `*.env`, `secrets.json`) are refused by name,
never opened; a readiness route (`/ready`, `/ping`) is offered to the reviewer, never chosen; a `src/` folder that is a
package keeps its name (`uvicorn src.app:app`: its modules import each other relatively). One repository is ready
from its `backend/` folder; one is refused for its environment files; two are refused for having no web app.

Tests: 522 pass (44 new in `tests/test_phase13.py`); ruff and zizmor clean.


# 1.14.1: the repository is brianboti/fast-deploy

Asked for on 2026-10-16: the platform repository follows the new name. GitHub keeps a renamed repository's numeric id
and redirects the old name, but two things carried the name itself. The pipeline's sign-in providers check the workflow
paths (`OWNER/NAME/.github/workflows/infra-plan.yml@...`) besides the ids, and the controller reads an app's platform pin
by the line `repository: OWNER/NAME` in its workflow. Both now accept former names during a move, and the runbook gains
"Rename the platform repository" with the order that keeps everything working.

1. **`pipeline.former_repositories`** (Terraform, default empty): the plan, drift and apply providers accept the workflow
   paths under the repository's name and each former one; the repository and owner ids stay the anchor.
2. **`platform.former_repositories`** (controller configuration, default empty): an app pinned under a former name keeps
   its platform pin; `update_app_workflows` rewrites an app pinned to the current commit under a former name, so the re-pin
   moves it to the new name. Pins are compared without case, as GitHub compares names.
3. The current documents and the playbook link to `brianboti/fast-deploy`; the history keeps the old links, which GitHub
   redirects.

Tests: 478 pass (4 new in `tests/test_rename.py`). The foundation applies twice: once with the old name listed, once
after the move without it.


# 1.14.0: Lab Deploy is now Fast Deploy

Asked for on 2026-10-16. Everything people see carries the new name: what Claude chat reads when it connects (the
server's name and instructions), the sign-in page, the files and pull requests the platform drafts ("Drafted by Fast
Deploy", "Add the Fast Deploy workflows", "Update Fast Deploy workflows to platform ..."), the watchdog's message, the
release notifications, the weekly cost report issue ("Fast Deploy cost report: SERVICE"), the drift issue ("Fast Deploy:
infrastructure drift"), a spend cap created from now on, the documents people read and the chat playbook skill, now
`skills/fast-deploy-operator`.

What keeps the old name, on purpose, so nothing running had to move: the Python package `labdeploy` and the command
`lab-deploy`, the repository `brianboti/lab-deploy` (renaming it on GitHub is a separate, optional step), and the Google
Cloud resources and their display names (the project, service accounts, roles, buckets, the key ring, the alert
"Lab Deploy: spending-cap workflow failed" and the existing spend cap "Lab Deploy: Cloud Run spend cap"), which the
runbook still names as the console shows them. The history in this file, `docs/VALIDATION.md` and the reviews stays as
written.

The cost report and drift workflows look for their open issue under the new title, then the old one, and rename an old
one on its next update, so no second issue opens. The app repositories' workflow files take the new names at their next
re-pin; nothing changes before then.

For the operator: in Claude, remove the skill `lab-deploy-operator` and upload `fast-deploy-operator`
(docs/CLAUDE-CHAT.md, step 4); optionally rename the connector to Fast Deploy. Restart the controller.


# 1.13.1: every release is followed to its end

Found live on 2026-10-16 while checking the new chat answers: `demo`'s auto-deploy of 15 October had succeeded, but the
application list still showed the release before it, with "1 new commit" waiting, and the auto-deploy pass said the
commit was "already handled ... (dispatched)". The controller records a release's outcome when someone asks for its
status; nobody had asked. A new background job, `follow`, asks every minute about each release still on its way (newest
first, ten at a time): an auto-deploy, an onboarding's first release, or a release started from chat and not followed.
So the application list, the watchdog and the chat's summaries are right without anyone asking. A plan of an
application unregistered since is left as history. The health summary also rounds latency to a tenth of a millisecond.
Nothing to apply or regenerate; restart the controller.


# 1.13.0: clear, consistent answers in Claude chat

Asked for on 2026-10-16: chatting with Lab Deploy should be easy, understandable and professional, with formatted
answers. Until now Claude read each tool's full result (a release status is about 21 KB of JSON, the application list
19 KB) and worded every answer its own way. Nothing here changes what any tool does or costs; no apply or regeneration.

## Added

1. **A summary written by code** (`labdeploy/chat.py`). The main tools' results now start with `summary`: a few Markdown
   lines built from that result alone, never from Claude's words, in one shape: a bold outcome, the facts that matter
   (version, address, checks, time, cost), then one **Next:** line. Releases (planned, started, running, live, stopped
   with the previous version serving, failed at a named step, rolled back, needing a person), onboarding (step N of 9,
   what it waits for, who acts), repository checks and drafts, the application table, health, logs, cost, the
   comparison with the live release, pull requests and the controller status. Plain words throughout: "running the
   tests", "watching the new version with real traffic", "the previous version is still serving". A renderer that fails
   leaves the tool's answer as it was, and a result that already has a `summary` keeps it.
2. **How to answer, in the server's instructions.** What Claude chat reads when it connects now starts with the answer
   rules: build on `summary` and keep its lines as written, one shape for every answer, plain words, short ids, Markdown
   links, a table for three or more items, no raw JSON or secrets, one question at most with the recommendation first, and
   one combined answer when several tools ran.
3. **The playbook skill** (`skills/lab-deploy-operator`): "How to answer" with the same rules and two worked examples;
   its facts brought up to date (a service in a folder, a Procfile instead of a Dockerfile, Postgres by password,
   `check_repository` before onboarding when unsure, 4 to 15 minutes). Re-upload it in Claude (docs/CLAUDE-CHAT.md).
4. **docs/CLAUDE-CHAT.md, "What answers look like"**, with three real answers.

Platform tests: 473 pass (12 new in `tests/test_chat.py`); the tool descriptions are unchanged (`schema/tools.sha256.json`).


# 1.12.3: a repository onboarded again keeps its environment

Found live on 2026-10-16, re-onboarding Sophia's copy from `backend/` under its old name: the step that prepares the GitHub
environment stopped with "GitHub answered HTTP 303". The environment's `main` branch policy already existed from the first
onboarding, and GitHub now answers 303 See Other for an existing policy where it once answered 422. Both mean the policy
is in place; any other answer still stops the step. Nothing to apply or regenerate; restart the controller.


# 1.12.2: the checker drafts for a real project with large files

Found live on 2026-10-16: `propose_configuration` for Sophia's `backend/` stopped with "Repository input exceeds 64 KiB".
Among the up to 40 files it reads for hints (the configuration, the Python files most likely to hold the application, a
few tests) were a 241 KB module and a 190 KB test. Such a file is now skipped and named in the draft's review list ("Not
read, over 64 KiB each"); any other refusal from GitHub still stops the draft. The 64 KiB limit itself is unchanged.
Nothing to apply or regenerate; restart the controller.


# 1.12.1: what the 1.12.0 live exercise found

The live exercise of 1.10.0 to 1.12.0 (`docs/VALIDATION.md`, 2026-10-15) onboarded two services of one repository, a
Postgres database by password, and the checker's draft on a bare repository. It found one real failure and four small
gaps, fixed here. Nothing here costs anything. **Apply the foundation once** (`scripts/apply-foundation.sh PROJECT`: 1 to
change, the custom role); no workflow regeneration is needed.

## Fixed

1. **Postgres by password could not be applied.** The first live apply of a `database_auth: password` root created the
   instance, the database and the user, then failed: Google's provider enables a secret version right after adding it,
   and the custom role `labDeploySecretWriter` lacked `secretmanager.versions.enable`; the workflow's retry then tried to
   replace the failed version and lacked `secretmanager.versions.destroy`. The role now also has `versions.enable`,
   `versions.disable` and `versions.destroy` (state changes only; still no `versions.access`, so no value is ever read
   back). Had the retry succeeded, it would have written a URL with a new password while the user kept the first one: the
   user's `password_wo_version` now follows the URL's secret version number, so every new version (the first, a rotation
   through `password_version`, a retry) sets the user to the same run's password in place. A user is never dropped for
   this, because it may own the application's tables; a new instance writes the URL again.
2. **A Compose file's database reaches the draft.** A web service that depends on a Postgres service in the Compose
   file is drafted with `needs.database: postgres`, and with `database_auth: password` when its `environment` sets
   `DATABASE_URL` (both the list and the mapping forms of `depends_on` and `environment`); the part says why in
   `needs_from`. Before, the live test repository's admin service was drafted with `database: none`.
3. **The infrastructure pull request says who asked.** Its description, written by the controller, ends with
   `Requested by <identity>`, and with `through the onboarding of <application>` when onboarding opened it, so the chain
   reads request, pull request, plan run, apply run (roadmap 12.1). The plan comment's code-rendered block is unchanged.
4. **An open draft is named as possibly stale.** When `propose_configuration` (or any platform pull request) finds a pull
   request already open on its branch, it still leaves it as it is, and now says so: it may hold a person's edits or
   predate the request, and closing it lets the platform open a fresh one. The live run had returned `changed: []` and
   the open 1.10.0 draft that still said `my-service`.
5. A progress message no longer ends with two full stops ("in progress..").

Platform tests: 459 pass; `terraform validate` passes for the foundation and the live `mono-admin` root.


# 1.12.0: legible approvals and the operator's succession (roadmap Phase 12)

Planned in `docs/ROADMAP.md` Phase 12 from the research review (`docs/RESEARCH-2026-10-14.md`, section 4.4, the trust
gaps). Nothing here costs anything at idle.

## Added

1. **Approval blocks rendered by code** (12.1). The infrastructure plan comment opens with a fixed block that
   `labdeploy/infra.py` renders from the Terraform plan, never from Claude's words: resources to add, change, destroy and
   replace, also by type; a reversibility tier per type and for the whole plan (`reversible`; `identity` for grants,
   service accounts and federation; `data-bearing` for the types the policy never lets the pipeline destroy; the highest
   wins); the root's idle floor per month before and after, at list prices (a size it cannot price is named); every
   exposure change; and the pull request, head commit and plan run. `create_deployment_plan` returns an `approval` block
   built the same way from the plan: the commit and its first line, the spec hash, the target and the service's folder,
   the cost bounds, exposure, needs, the number of warnings, a reversibility tier (`data-bearing` when the release runs a
   migration, because a rollback restores traffic only), and the chain of identifiers (plan, commit, who asked, the
   onboarding, and the workflow run once dispatched).
2. **Requester and approver** (12.2, D29). A principal's `may_merge_own_requests` (default true) and an application's
   `auto_merge` (under `applications.<id>` or `application_settings.<id>`; by default it follows `onboarding.auto_merge`).
   Onboarding merges its own pull requests only when both allow it; otherwise it waits for another operator and says so,
   names who asked (`requested_by`), and records who merged (`merged_by`) when someone does. An identity no longer in
   the configuration approves nothing. `.github/CODEOWNERS` names `/infra/gcp/apps/**`, and `docs/SETUP.md` 7l says how
   to require Code Owner review once a second operator exists.
3. **Tool descriptions pinned** (12.3). `schema/tools.sha256.json` holds a SHA-256 of every tool's name, title,
   description, schemas and annotations; a test compares it with what Claude is served, so changing what Claude reads
   about a tool is a deliberate, reviewed commit (`python -m labdeploy.cli tools-checksum --write`). A second test refuses
   any tool that is not wrapped by the audited decorator.
4. **The exit procedure** (12.4). `scripts/export-app.sh APP [DEST_DIR] [--dry-run]` copies what one application needs to
   run elsewhere: the image of every serving revision by digest, the service in Cloud Run's Knative-compatible format,
   the root's Terraform state (read-only), every declared bucket, a Postgres dump or a Firestore export, and a manifest
   with sizes and SHA-256s. The runbook's "Leaving Google Cloud" says how each part runs or restores elsewhere. CI
   validates the foundation and every application root with OpenTofu 1.12.6 too (downloaded by version and SHA-256).
5. **The quarterly restore drill** (12.5). `python -m labdeploy.cli restore-drill` restores the newest controller backup
   from the backup bucket into a temporary folder, read-only, checks its integrity, counts what it holds, and records a
   `restore_drill` event; the daily watchdog says when no drill has passed for 92 days. The runbook gains "Succession"
   and "Quarterly restore drill".

## Changed

1. An unexpected failure in a tool or REST call returns a plain message with what to check before asking again (REST
   answers 500 with the same text), and the commonest refusals name their next action.
2. `get_onboarding_status` puts what the current step waits for into its next action.
3. The policy treats a Cloud SQL database (`google_sql_database`) as data: the pipeline never deletes one.
4. CI installs zsh so the export script's tests run there.

## Upgrade steps

1. Restart the controller. No workflow regeneration and no Terraform apply are needed for this release.
2. Run the first restore drill (`python -m labdeploy.cli restore-drill`); until then the daily watchdog reports it due.
3. Once a second operator exists: `docs/SETUP.md` 7l and the runbook's "Succession".

# 1.11.0: the platform writes the boilerplate (roadmap Phase 11)

## Added

1. **`check_repository`** (11.1), a read-only tool: the contract's checklist for a repository or one service folder of it,
   before anything is registered. Each required and recommended item says whether it holds, why it matters, and whether
   the platform can draft it. Onboarding runs the same check first and stops with the same list, naming what the
   platform can draft. Any CI workflow of the repository counts for CI, and Renovate counts for Dependabot.
2. **The checker writes** (11.2, decisions D26 and D27). `propose_configuration` now opens one draft pull request with
   every file the contract asks for that the service lacks: a Dockerfile (the app's Python slim image by digest,
   dependencies before code, a non-root user, uvicorn or gunicorn on `$PORT`), an allow-list `.dockerignore`,
   `requirements-dev.txt`, a health test, `platform.yaml` with `reviewed: false`, a CI workflow, `SECURITY.md` and
   Dependabot. It detects FastAPI, Starlette, Flask, Django and others, the Python version and the health route from the
   code, read as text and never run. The lock is compiled on the controller's machine with uv in a container of the
   app's own Python; without Docker the pull request names the exact command instead. It works for a repository that is
   not registered yet (an identity with `applications: ['*']`), never changes a file that exists (also when one appears
   on the branch meanwhile), and is never merged by the platform. The pull request explains each file in one line.
3. **Building without a Dockerfile** (11.3, D28). A service with a `Procfile` and no Dockerfile is built with Google's
   buildpacks: `pack` 0.40.9 downloaded by version and SHA-256, the builder and run image pinned by digest. Python 3.11
   and 3.12 use the Ubuntu 22.04 builder because the Ubuntu 24.04 one offers 3.13 and later only (found in the trial).
   pip installs the lock with its hashes when there is one. Measured locally on the demo app, the buildpack image is
   about 394 MB against 52 MB from its Dockerfile, so the Dockerfile stays the default and the checker drafts one.
4. **The contract opens with the three things a developer does** (11.4): a service on `$PORT` that answers its health
   path, pinned requirements, tests. Everything else is under "What the platform writes for you".
5. **Onboarding measures** (11.5): each onboarding records how long it took, how many times a step blocked, which steps,
   and how many times the operator stepped in. `list_applications` shows it, and the weekly cost issue repeats the line
   (the controller writes it to the environment as `ONBOARDING_SUMMARY` when onboarding finishes).

## Changed

1. `propose_configuration` takes a registered `application`, or a `repository` link and `path`; it no longer refuses a
   repository that has a `platform.yaml`, it drafts the other missing files instead.
2. The controller's container image includes `templates/`; `lab-deploy sync-pins` also updates the drafted CI workflow;
   CI lints the drafted workflows and the platform's Dependabot file, which now waits seven days for a new release.

## Upgrade steps

1. Restart the controller. Docker must be running on its machine for the lock to be compiled (otherwise the pull request
   carries the command).
2. Re-pin every application: the deploy workflow gained the buildpack build and the right-sizing workflow the onboarding
   summary.

# 1.10.0: one repository, several services (roadmap Phase 10)

## Added

1. **A service's folder.** An application can live in a folder of its repository (`services/api`, `backend`): the registry
   records it as `path`, `onboard_application` takes it, and the analyzer, the contract check, the plan, the drafted
   configuration, the right-sizing pull request and the weekly cost report all read `platform.yaml` there. The deploy
   workflow runs the tests and the audit in that folder and builds from it. A plan records the folder and workflow names
   it was made for and is refused if they changed (decision D23).
2. **Several services in one repository.** The first service of a repository keeps the plain names; each further one gets
   its own workflows (`deploy-gcp-<service>.yml`, `recover-gcp-<service>.yml`, `rightsize-gcp-<service>.yml`), GitHub
   environment (`staging-<service>`), queue and Terraform root. Its deploy identity trusts only its own workflows in its
   own environment, so one service's workflow cannot release another, and the pipeline publishes its variables to its own
   environment. Re-pinning, rollback, recovery records, cost reports and `lab-deploy decommission` use the service's own
   names; retiring one service keeps the repository's access for the others (D24).
3. **`build: {context: repository}`** in `platform.yaml` for a service that copies shared code from elsewhere in the
   repository: the whole repository is the build context and the Dockerfile stays in the service's folder. The contract
   says how to keep that context small with a `<Dockerfile>.dockerignore`.
4. **Where each part of an app goes** (the contract's new section 4b): every web component its own service, data under
   `needs`, no cache by default, workers through a topic's push delivery or a job, a lab-members front end served by the
   API's own container. The analyzer says so when it sees Redis, Celery or RQ, a `package.json`, or a database driver
   without the Cloud SQL connector.
5. **Compose files are read, never deployed.** `analyze_repository` reads a `compose.yaml` (the service's folder first,
   then the root) and returns one draft `platform.yaml` per web service it builds, with its folder and build context, and
   the rule for every database, cache, queue and worker it names.
6. **Postgres by password** (`needs.database_auth: password`, roadmap item 10.6): for code that only
   speaks a connection URL. The platform creates the database user `app` and the secret `<service>-DATABASE_URL`, which the
   service receives as `DATABASE_URL`. The root generates the password in each run with an ephemeral `random_password` and
   sends it once through write-only arguments, so neither Terraform's state, nor a plan, nor the repository, nor the chat
   ever holds it (checked: a plan of such a root contains no password and passes the policy). The release checks the
   secret has an enabled version before deploying. IAM sign-in stays the default.
7. **Auto-deploy in a shared repository** skips a service whose folder and workflows did not change since its live release
   (unless its build reads the whole repository), so a commit to one service does not rebuild the others.

## Changed

1. **The image is built from exactly the reviewed commit.** The test step kept its virtualenv inside the app's checkout,
   so a Dockerfile that copies everything (`COPY . .`) without a `.dockerignore` shipped the test environment in the
   image. The virtualenv now lives in the runner's temporary folder and the checkout is cleaned back to the commit
   before the build.
2. **Dockerfile lint errors stop the release by default**, as 1.9.0 announced. Warnings never do; the repository variable
   `HADOLINT_BLOCKING=false` keeps error-level findings as annotations for one repository. The four Dockerfiles in use
   have no findings.
3. The onboarding block's `environment` setting accepts `staging` only: the platform names GitHub environments itself.
4. The foundation's lock file pins the `random` provider (3.9.1) for the roots that copy it.

## Upgrade steps

1. Apply the foundation (`scripts/apply-foundation.sh PROJECT`): the plan identity gets `roles/secretmanager.viewer` and
   the apply identity the custom role `labDeploySecretWriter`, which create and describe secrets without reading a value.
2. Restart the controller.
3. Re-pin every application ("re-pin APP" or `lab-deploy update-workflows`): the workflow templates changed. Existing
   roots render unchanged, so no infrastructure pull request is needed.

# 1.9.2: the spend-cap check says whether the cap is recorded

## Fixed

1. `scripts/spend-cap.sh check` ended with a generic "record it" line showing the word AMOUNT even when the cap was
   already recorded. It now compares the cap with `cloud_run_spend_caps` in `config.local.yaml` and says "Nothing to
   do", names the differing amount, or gives the exact line to add.

# 1.9.1: a missing drift report is asked for again

## Fixed

1. Found in the 1.9.0 live exercise: the controller remembered "the drift check has produced no report yet" for an hour,
   so `get_controller_status` kept saying `not_run` after the first manual drift run had already reported. With no report
   it now asks GitHub again on the next call; a report it found is still remembered for an hour.

2. **The health view now sees an outage from outside.** When the native spend cap paused staging in the live exercise,
   every service answered 503 while `get_service_health` still showed open ingress and no server errors: requests the cap
   (or Google's front end) refuses never reach the container, so the service's own metrics do not count them. The view
   now reports `uptime_checks_passed`, the share of Google's uptime-check probes that reached the health path in the
   window, and notes it plainly below 100%, pointing to the spend cap when every app fails at once. Measured during the
   trial: 75% for the demo and Sophia over ten minutes.
3. `scripts/spend-cap.sh` gains `amount` (change the cap and re-arm a lifted one) and `lift`. Changing a spend cap's amount
   through the Budget API needs the mask `amount.specifiedAmount,spendCap.inputState` with the whole budget in the body;
   a mask of `amount` alone is refused as an invalid argument (found while lowering the cap for the live exercise). The
   check prints fractional amounts.

## Upgrade steps

1. Restart the controller.

# 1.9.0: cost and safety layers that cost nothing (roadmap Phase 9)

Planned in `docs/ROADMAP.md` Phase 9 from the research review `docs/RESEARCH-2026-10-14.md`. Every item has zero idle cost.

## Added

1. **A bake after promotion** (decision D22). Once traffic has moved to the new revision and its final probe passed, the
   release step watches that revision's own Cloud Run logs for `bake_seconds` (default 300, at most 900, 0 turns it off)
   before the release counts as done. It restores the previous traffic split, exactly as a failed probe does, on a crash
   (Cloud Run's system log shows the container exiting, stopped for memory or a signal, or a new instance failing its
   startup probe), on more than 5% 5xx answers among at least 20 requests, or, at the end of the window, on a p95 latency
   above three times the previous revisions' p95 over the hour before the switch (when both saw at least 20 requests). The
   health path is left out: the uptime check and the probes call it every minute. Too little traffic is
   `inconclusive` and passes with a note; logs that cannot be read leave the bake `unobserved`, which also passes, loudly.
   The receipt carries the window's numbers (`bake`), the status shows them, a failure reads "failed its bake after
   promotion" with the new revision's last log lines, and a runner lost while baking is recovered like one lost while
   promoting. Observed in staging while building it: Cloud Run logs `Container called exit(1).` at WARNING, so the bake
   matches crash messages rather than severity alone, and ignores harmless sandbox warnings.
   The release phases are now `preparing, (migrating,) deploying, promoting, baking, succeeded`. The setting is per
   application (`bake_seconds` under `applications.<id>` or `application_settings.<id>`); the plan records it and the
   dispatch sends it only when it differs from 300, so workflows pinned before 1.9.0 keep working. The deploy job may now
   run 35 minutes instead of 20.
2. **Google's native spend cap for Cloud Run** (decision D21), generally available since 2026-10-12: a monthly budget on one
   project and the Cloud Run service that pauses every Cloud Run workload in the project when the month's gross estimated
   cost passes it. It does not stop Cloud SQL, buckets or topics, so the platform's own cap workflow keeps that job; the
   native cap is the second layer that needs nothing of this platform to work. `scripts/spend-cap.sh check|create` reads
   and creates it through the Budget API (Terraform's Google provider has no spend-cap argument as of 8.7.0); the
   operator records it under `cloud_run_spend_caps` in the controller configuration. Every plan, `estimate_cost`,
   `get_cost_report` and `get_controller_status` now name both guards, and a project without a recorded cap gets the
   warning finding `no_native_spend_cap`. Docs: `docs/SETUP.md` 7i and the runbook's "The native spend cap tripped".
3. **Dockerfile lint**: hadolint 2.15.1 (downloaded by version and SHA-256, like Cosign) runs in the build job before the
   image is built. Findings become annotations on the run and travel in the release receipt (`dockerfile_lint`); they stop
   a release only when the repository variable `HADOLINT_BLOCKING` is `true`, and then only for error-level findings
   (the default from 1.10.0). The platform's own CI lints its Dockerfile and the example app's with the same pin. The
   four Dockerfiles in use today (the platform, the demo, the Sophia copy, lab-notebook) have no findings.
4. **Data-protection rules in the infrastructure policy**, each with a planted-violation test: Cloud SQL keeps automated
   backups (`database_backups`), accepts encrypted connections only (`encrypted_connections`) and keeps deletion
   protection in Cloud SQL itself as well as in Terraform; a Firestore database is abandoned, never deleted, when Terraform
   lets go of it; no bucket is force-destroyed (`no_force_destroy`); the recovery and backup buckets keep versioning
   (`versioned_checkpoints`); and no resource may store secret material in Terraform's state or plan
   (`no_secret_in_state`: secret versions with `secret_data`, SQL users with `password`, generated passwords and keys;
   the write-only forms are allowed). The modules already met every rule, so no live resource changes; a test keeps a
   trimmed real plan of the demo's root (`tests/fixtures/lab-demo-plan-2026-10-14.json`) passing with every resource
   treated as changing, and another scans every module and rendered root for secret-bearing resources.
5. **The weekly drift check** (decision D30): `.github/workflows/infra-drift.yml` plans every application root against the
   live infrastructure on Mondays (or on demand from `main`) with the read-only plan identity and no state lock, and
   reports what differs from the code: an edit made outside the pipeline, or an apply that did not finish. The result is
   a living issue on the platform repository, "Lab Deploy: infrastructure drift" (closed when everything matches), and a
   `drift-report` artifact of addresses and categories only, never attribute values. The controller reads it:
   `get_controller_status` shows it, and the daily watchdog notifies on drift in IAM, networking or exposure, on a root
   whose plan failed, and on a report older than eight days. A separate workflow keeps the pull-request plan workflow
   unchanged; the plan identity's federation now admits `schedule` and `workflow_dispatch` events only for that file on
   `main`.
6. **Per-event charges in the weekly cost report**: the month's freshly built images (each one is an Artifact Analysis
   scan at about USD 0.26), the repository's billable GitHub Actions minutes when it is private (each job rounded up to
   the minute, USD 0.006 a minute on the standard Linux runner after the account's included minutes), and the bytes the
   billing queries read. Measured on 2026-10-14: the demo built 15 images this month (about USD 3.90 in scans, more than
   its compute), and the private Sophia copy used 873 billable minutes in two days, 709 of them its own CI. The right-sizing
   job gains `actions: read` for this.

## Changed

1. The cost documents and the report say plainly that Google's Cloud Run recommender advises only the billing mode
   (and two security checks); it never sizes CPU or memory, so the weekly measured proposals are the only sizing signal.
2. The living cost-report issue no longer lists each proposal twice.

## Upgrade steps

1. Merge, then apply the foundation (`scripts/apply-foundation.sh PROJECT`): it lets the plan identity run the drift check.
2. Restart the controller and re-pin every application's workflows (the deploy and right-sizing templates changed); after
   that, raise `platform.minimum_commit` to the 1.9.0 commit, so a non-default `bake_seconds` never reaches an old pin.
3. Create the native spend cap (`scripts/spend-cap.sh create PROJECT AMOUNT`, or the console, `docs/SETUP.md` 7i) and
   record it under `cloud_run_spend_caps`.
4. Optional: dispatch the drift workflow once from the Actions tab to see the first report.

# 1.8.8: applications on Python 3.11 to 3.15, audited on their own Python; conda, Poetry and uv

Asked after the Sophia test: people bring different Pythons and package managers. Three gaps, all in the build job; no
cloud change and no cost.

## Added

1. **Python 3.14 and 3.15** for `platform.yaml` `python` (now 3.11 to 3.15; default still 3.12, so existing specs keep
   their hash). Both are on GitHub's Ubuntu 24.04 runners (3.14.8, 3.15.0). 3.10 and older stay unsupported: 3.10's
   security support ends this month.
2. **The analyzer drafts `python` from the Dockerfile's base image** instead of only suggesting it, and says plainly when
   that image is on a Python the platform does not test (3.10 and older).
3. **conda, Poetry and uv**: `docs/DEVELOPER-CONTRACT.md` section 1a gives the exact export commands for the three pip
   files the platform reads, and the analyzer points there when it finds `environment.yml`, `poetry.lock` or `uv.lock`
   without a `requirements.txt`. The uv flow was run end to end on Python 3.14 (hash-locked install and a clean audit).

## Fixed

1. **The dependency audit ran on the platform's Python 3.12 whatever the application used.** Checked on 2026-10-14:
   `numpy==1.24.4`, an ordinary pin for a Python 3.11 project, has no 3.12 build, so the audit failed to install it and
   would have stopped the release; and pip-audit judges version markers for the Python it runs on, so on 3.12 it skipped
   packages only a 3.14 application installs. The audit now runs on the application's own Python (the same
   `setup-python` step as the tests, now also when `tests: none`). With a `requirements.lock` it reads every locked
   package at its exact version without installing or building anything (`--disable-pip`); without one it resolves
   `requirements.txt` as before. The audit tool installs from wheels only, checked on Python 3.11, 3.13, 3.14 and 3.15.

## Upgrade steps

1. Restart the controller and re-pin application workflows (the template changed).

# 1.8.7: an image released under the provenance exception is never reused

## Fixed

1. Found while preparing the exception's live test with Sophia: once the operator removed `provenance: unavailable`, the
   next plan for the same commit reused the image released under the exception. That image has no signed provenance,
   so the reuse path's verification would have stopped the release (safely, but for no reason the operator could see).
   A plan now reuses only an image whose release receipt does not say `provenance: unavailable`: an earlier signed image
   of the same commit if there is one, otherwise the commit is built, tested and signed fresh.

## Upgrade steps

1. Restart the controller.

# 1.8.6: signed provenance for private repositories

Found by deploying the private copy of the lab's Sophia backend: GitHub's artifact attestations cover public repositories
(private ones only on GitHub Enterprise Cloud), so `actions/attest-build-provenance` refused the image and nothing was
deployed. Provenance stays required; the platform now signs with its own trusted path when GitHub cannot.

## Added

1. **Cosign with Cloud KMS for private repositories.** The deploy job picks the path from the repository's visibility:
   public uses GitHub's attestation as before; otherwise Cosign v3.1.3 (downloaded by version and SHA-256) signs a SLSA v1
   statement for the pushed digest with the application's own key, `<service>-provenance` in the foundation's
   `lab-deploy` key ring (EC P-256, software, about USD 0.06 a month per application). Only the application's deployer
   may use the key (`roles/cloudkms.signerVerifier` and `roles/cloudkms.viewer` on that key alone), and the deployer is
   reachable only from its deploy workflows on `main` in `staging`. The signature is stored as the image's
   `sha256-<digest>.att` tag and never sent to a public transparency log, which would publish a private repository's
   name and commits. Before release, Cosign checks the signature with the key and `labdeploy/provenance.py` checks that
   the statement names this image digest, repository, commit and workflow; a reused image is checked the same way.
2. **The operator's exception, `provenance: unavailable`**, under `applications.<id>` or `application_settings.<id>` in the
   controller configuration, for staging or an emergency only: the plan warns, the run shows a warning, the image is
   always built fresh (the workflow refuses the exception with a reused image), and the receipt records it. A plan made
   before the setting changed is refused at dispatch. `platform.yaml` has no such field.
3. Every release receipt records its provenance path (`github`, `cosign-kms` or `unavailable`), and the release step
   refuses to run without one.

## Changed

1. Foundation: Cloud KMS API and the `lab-deploy` key ring (free; Cloud KMS never deletes a ring), administered within
   that ring only by the apply identity and readable by the plan identity; neither can sign. Output `kms_key_ring`.
2. Service module: the key and its two grants, created once the foundation has the ring (application roots read it with
   `try()`, so they plan cleanly before); a registry KEEP rule for `sha256-` tags. The policy check treats a
   `google_kms_crypto_key` as data (never destroyed or replaced by the pipeline), holds its key ring to the allowed
   region and requires its labels.

## Upgrade steps

1. Merge, then apply the foundation (`scripts/apply-foundation.sh PROJECT`): it enables Cloud KMS and creates the ring.
2. Apply each application root again (dispatch the infrastructure apply, or merge its next infrastructure change): this
   creates the application's key. The roots in this release already read the ring.
3. Restart the controller and re-pin application workflows (the template changed).

# 1.8.5: a real lab project's Python and test suite

Found by deploying a private copy of the lab's Sophia backend (a FastAPI app on Python 3.13 with 913 tests).

## Added

1. **`python:` in `platform.yaml`** (3.11, 3.12 or 3.13; default 3.12, so existing specs keep their hash): the release's
   tests run on that version. The platform's own steps keep Python 3.12 (`update-environment: false`). Live, a lock
   compiled for 3.13 omitted greenlet, which SQLAlchemy needs below 3.13 on Linux x86_64, so `pip install
   --require-hashes` failed on the platform's 3.12 (reproduced in python:3.12-slim and 3.13-slim containers).
2. The analyzer notices a Dockerfile on another Python and suggests the `python:` line.

## Changed

1. The build job may run 45 minutes instead of 20: a real suite takes minutes before the image is built.

## Upgrade steps

1. Restart the controller, re-pin application workflows (the template changed), then add `python:` where it differs.

# 1.8.4: a second infrastructure change for the same application

## Fixed

1. **The second infrastructure pull request of an application conflicted.** The controller reused the branch
   `lab-deploy/infra-<app>` when no pull request was open on it; after a squash merge that branch's history no longer
   matches main, so the new pull request conflicted and GitHub ran no plan workflow for it (live: brianboti/lab-deploy#29,
   retiring lab-notebook's database). An existing branch is now reused only when it is exactly the base commit; otherwise
   the pull request uses a new branch named after the base commit. Nothing is deleted or force-pushed.

## Upgrade steps

1. Restart the controller. Optionally turn on "Automatically delete head branches" in the platform repository's settings.

# 1.8.3: a cancel reads as a cancel

## Fixed

1. Re-tested live on 1.8.2, the cancel worked (the deploy job was cancelled, the release restored traffic, the previous
   revision kept serving), but the status read "The database migration failed ... (KeyboardInterrupt: )". The receipt
   now marks a cancelled release, and the status says it was cancelled and in which phase; during a migration it adds
   that the started job runs on in Google Cloud and may still finish.
2. `docs/VALIDATION.md` records the comprehensive test with `brianboti/lab-notebook`.

## Upgrade steps

1. Restart the controller and re-pin application workflows (the runner's receipt changed).

# 1.8.2: a cancel really cancels the release

## Fixed

1. **A cancelled run kept releasing.** Re-tested on 1.8.1, a cancel during the migration again let the release promote.
   The cause was the deploy job's condition, `if: always() && ...`: GitHub does not cancel a job whose condition uses
   `always()`. It is now `!cancelled() && ...`, which still runs the job when the build was skipped (a reused image).
   Correction to 1.8.0: its note blamed the shell for not passing the signal on; that was not the cause. Running the
   release with `exec`, as 1.8.0 does, stays: the signal now reaches the release directly when GitHub sends it.
2. A running controller re-reads the registered applications every minute, so `lab-deploy unregister` reaches it
   without a restart.
3. **A failed migration's cause was still missing on 1.8.1** (re-tested live): the job's traceback became searchable
   seconds after the first read, and as one 76-line entry its cause is the last line, beyond the 500 characters kept. The
   runner now waits up to half a minute for an error line, keeps the end of each entry, and names its last line.

## Upgrade steps

1. Restart the controller, and re-pin every application's workflows to this commit (the deploy job changed).

# 1.8.1: after the late cancel, the billing export and an abandoned onboarding

## Fixed

1. **A release that completed before a late cancel stayed unrecorded.** The plan was stored as `cancelled` from the run's
   conclusion, the controller kept the earlier release as live, and the health view reported traffic changed outside the
   pipeline. The receipt now decides: a run that ended cancelled, failed or timed out after the release succeeded is the
   live release, and its status says how the run ended.
2. **Actual spend read $0 when the billing export had no recent data** (its newest row was 18 days old). A freshness
   query runs first; with nothing in the last two days the report says the spend is unknown, not zero, and how to check.
3. **An onboarded application could not be retired.** `decommission` read only the configuration file ("Unknown
   application"), had no Cloud SQL or migration-job step, and nothing removed an abandoned onboarding, which the
   watchdog reported daily. `decommission` now reads the registry, the checklist covers Postgres, `lab-deploy unregister
   APP` forgets an onboarded application, and the watchdog names an unfinished onboarding as such.

## Upgrade steps

1. Restart the controller. Re-pin application workflows for the billing-export change in the weekly report.

# 1.8.0: what the first complicated application found after its onboarding

`lab-notebook` was onboarded with one sentence, released (Postgres migrations through the app's own IAM identity), used
end to end (24 live checks), updated, rolled back, redeployed from its image, and taken through a failing health check,
a failing migration, a failing test, a cancel, policy refusals and a missing secret. Each finding has a regression test in
`tests/test_comprehensive_findings.py`.

## Added

1. **`application_settings`** in the configuration: the operator's switches (`cost_ceiling_usd`, `auto_deploy`, `paused`,
   `viewers`, `infra`) for an application onboarding registered, laid over its database entry without copying it.
2. **The health view shows the Postgres database**: state, tier, version and whether it runs (the spending cap stops it);
   the observer gets `roles/cloudsql.viewer`. A refusal for one need is a note instead of hiding the others.

## Fixed

1. **A cancel did not stop a running release.** The release ran behind a waiting bash, which never passed GitHub's SIGINT
   and SIGTERM on: a cancel during the migration let the release promote two minutes later. The step now execs the
   release, which restores traffic and ignores further signals while it does.
2. **A failed migration was reported as a failed candidate** with gcloud's cut-off tail. The receipt now keeps the
   execution's own last log lines and the real error (`relation "..." does not exist`), names the failed phase, and the
   status says the migration failed and nothing new was deployed. A pre-check stop (a missing secret) says the same.
3. **Onboarding stayed blocked after the recovery it recommends**: a successful apply on a later commit of main now counts.
4. **A refused GitHub request blamed the wrong permission**; it now names the one the request needs (Administration for
   environments, Workflows for workflow files, ...).
5. **A recovery check still running read "concluded 'None'"**; it now says it is still running, with its link.
6. **An invalid `platform.yaml` came back as pydantic's raw error**; it now names the field and what it must be.

## Upgrade steps

1. Re-apply the foundation (`roles/cloudsql.viewer` for the observer; 1 to add).
2. Restart the controller.
3. Re-pin every application's workflows to this commit (`update_app_workflows`, merge): the release step changed.

# 1.7.3: the first apply of a new application

Found by the first apply of `lab-notebook`'s root (run 38440870710), each with a regression test.

## Fixed

1. **The apply identity could not create the log-error alert.** A log-based alert policy is stored as a Cloud Logging
   notification rule, which `roles/monitoring.admin` does not cover. The foundation adds the custom role
   `labDeployLogAlertWriter` (the five `logging.notificationRules.*` permissions, nothing wider such as sinks) for the
   apply identity. The demo's alert had been created by the operator before the pipeline existed.
2. **The first apply of a new application failed on the uptime check** ("404 Requested entity was not found"): it was
   created four seconds after the brand-new Cloud Run service, which Monitoring did not see yet; the same apply a
   minute later succeeded. The apply step now plans again after a minute, re-checks the policy and applies once more
   when an apply fails; a real problem still fails twice.

## Upgrade steps

1. Re-apply the foundation (`scripts/apply-foundation.sh PROJECT`): the plan adds the role, its grant, and the IAP API
   from 1.7.0 (3 to add, 0 to change, 0 to destroy on staging).

# 1.7.2: fixes from the comprehensive test with a complicated application

A new reference application, `brianboti/lab-notebook` (pages for people, a JSON API, Postgres with Alembic migrations,
an uploads bucket, an events topic read back on a page, an API key in Secret Manager), was taken through the platform
from a repository link. It found these, each with a regression test (`tests/test_comprehensive_findings.py`):

## Fixed

1. **New applications could not be planned** (blocker). The gcs backend creates a missing state, and its lock, even with
   `-lock=false`; the read-only plan identity may not, so the first plan of every new application root failed with a
   403. `infra-plan.yml` now plans a root with no state yet against an empty local state; any other answer stops.
2. **The policy refused the platform's own module** (blocker). The service module grants the deployer
   `roles/containeranalysis.occurrences.viewer` for the scan summary; the role was missing from the allow-list, so
   every new application's plan was `Policy: blocked`. Existing roots never noticed: only changes are judged.
3. **A real migration could not create a table.** Since PostgreSQL 15 an ordinary user may not create objects in
   `public`, and Cloud SQL gives a new IAM user no privileges beyond connecting. The runtime identity now gets
   `database_roles = ["cloudsqlsuperuser"]` on its own instance. The 1.4 trial ran a migration that only printed.
4. **Every new application failed to plan before the foundation was re-applied for 1.7.0**: the renderer read
   `outputs.iap_client_id`, which an older foundation state does not have. It now falls back to `""` (no IAP client).
5. `scripts/start.sh` aborted under zsh (`status` is reserved) exactly when the Keychain had no working token, so the
   prompt for a new one never appeared; the prompt now lists the 7g permissions.
6. The analyzer read only `requirements.txt` and printed false advice for a reproducible repository (lock file ignored)
   and asked for tests that existed. It now reads the repository's file list, and suggests needs (bucket, topic,
   database, migration) in the review questions instead of putting database libraries in the legacy `dependencies`
   field, whose policy message still said databases are not implemented (decision D16).
7. Demo mode registered every directory as `lab-demo`, so any other application's plan was refused; it now uses the
   name in the application's `platform.yaml`, and the walkthrough stops with a plain message on a refusal.

## Upgrade steps

1. Restart the controller. No runner or template change: application workflows keep their pin.

# 1.7.1: a playbook for Claude chat

## Added

1. **`skills/lab-deploy-operator`**, a skill for Claude chat: the operator's playbook as instructions Claude reads whenever a
   request is about deploying, releasing, rolling back, health, logs, cost or access. It sorts the 22 tools into look,
   record and act; says when to ask before acting (and that `onboard_application` goes all the way when a repository
   passes); walks through the seven daily jobs; explains every release state in plain words; lists the hard limits; and
   turns a failed repository check into a message the operator can paste to the developer. `references/developer-contract.md`
   is a short copy of the standard for checking a repository by hand. Upload once under Customize → Skills
   (`docs/CLAUDE-CHAT.md`, step 4); its description stays within claude.ai's 200-character limit.

## Fixed

1. The instructions the controller sends to Claude at connection said databases are refused by policy; they have been
   supported since 1.4.0. They now name what is refused (other clouds, production, public access, GPUs, sizes above the
   lab's limits), how databases, buckets, topics and migrations are added, and the health, logs and rollback path.
2. `docs/CLAUDE-CHAT.md` sent "give demo a bucket" to `propose_configuration`, which refuses an app that already has a
   `platform.yaml`; the row now describes the reviewed `needs` change followed by `propose_infrastructure`.
3. A test keeps the playbook's tool table, the server's instructions and the registered tools in step.

## Upgrade steps

1. Restart the controller for the new instructions (no runner or template change; app workflows keep their pin).
2. Build the zip and upload the skill in Claude (`docs/CLAUDE-CHAT.md`, "Before the first conversation", step 4).

# 1.7.0: pages for people (Phase 8, items 8.2 to 8.4 and 8.6)

## Added

1. **`exposure: lab-members`** in `platform.yaml`: the service gets Google's Identity-Aware Proxy in front of it, directly on Cloud Run (no load balancer, no charge). People open the pages after a Google sign-in; only the listed accounts get through. `authenticated` (programs with identity tokens) stays the default; `public` is still refused by the model and the policy.
2. **Viewers**: `onboarding.default_viewers` (the lab's accounts, applied to every `lab-members` app) and `viewers` per application in the controller's configuration or registry (`user:`, `group:` or `domain:`). The renderer writes them into the app's Terraform root as `roles/iap.httpsResourceAccessor` grants; the operator edits the lists, developers cannot grant themselves. A `lab-members` root without a viewer is refused at render time.
3. **Terraform** (service module): `iap_enabled` on the service, the IAP service agent as invoker, one grant per viewer, the release probe identity as a viewer, and an uptime check that, behind the proxy, accepts the sign-in redirect as "the front door is up" (the release probes prove the page itself). The foundation enables `iap.googleapis.com` and publishes `iap_client_id` (a variable the operator sets after the one-time console step; the id is not a secret). The apply script creates the IAP service agent.
4. **The runner**: a pre-check that refuses a release whose `platform.yaml` disagrees with the service's proxy state (Terraform owns the switch), and probes that mint their identity token for the proxy's OAuth client (`IAP_CLIENT_ID` in the app's GitHub environment, written by onboarding when configured) instead of the service URL.
5. **The health view** shows `access`: the exposure, whether the proxy is on, and who may open the pages; a mismatch between the two is a note.

## Upgrade steps

1. Once, in the console (`docs/SETUP.md`, "Pages for people"): create the project's IAP OAuth client with "Auto generate credentials" on any service, then put its client id in the foundation's `terraform.tfvars` (`iap_client_id`) and in `onboarding.iap_client_id`; add the lab's accounts to `onboarding.default_viewers`.
2. Re-apply the foundation (the IAP API, the service agent, the new output); re-render every application root (two new inputs). Runner and template changed: regenerate the app workflows against this commit and raise `platform.minimum_commit`; set `IAP_CLIENT_ID` in existing apps' `staging` environments.

# 1.6.0: one sentence (Phase 7, items 7.1, 7.2, 7.4 and 7.6)

## Added

1. **`onboard_application`**: from a repository link to a running service, for a repository that follows `docs/DEVELOPER-CONTRACT.md`. The tool checks the contract (the files, a valid and reviewed `platform.yaml`, tests, the exposed port) and stops with a checklist when something is missing; registers the application; renders its Terraform root and opens the pull request on the platform repository; merges it once the plan workflow has run and the policy says passed (and only when every file is under the application's own root); waits for the apply; creates the app's `staging` environment restricted to `main` and writes its eight variables (plus `BILLING_EXPORT_TABLE` when configured); opens and merges the pull request that adds the three workflows; then plans and dispatches the first release. Steps that wait on GitHub or the pipeline end the call; the scheduler continues every minute, and `get_onboarding_status` says where it stands. Calling again resumes, also after a fix.
2. **Applications registered in the database** (decision D13): onboarding writes the application entry to the controller's database and the live configuration shows it next to the file's entries; the file keeps principals and settings. An identity with `applications: ["*"]` sees every application, including the ones registered later.
3. **`onboarding` block** in the configuration: `project_id`, `project_number`, `region`, optional `state_bucket`, `bootstrap_image`, `environment`, `billing_export_table`, and `auto_merge` (default true; false leaves every merge to a person, the tool then waits).

## Changed

- The scheduler runs the onboarding tick every minute; `get_controller_status` shows it with the other jobs.
- The GitHub credential needs more than reading: see `docs/SETUP.md`, "One sentence", for the fine-grained token's permissions (all repositories; contents, pull requests, workflows, actions, environments, variables and administration). A GitHub App (decision D14) stays planned.

## Upgrade steps

1. Restart the controller, add the `onboarding` block and `applications: ["*"]` for the operator, and replace the GitHub token with one that covers every repository and the permissions above (`scripts/start.sh` stores it). No runner change: application workflows keep their pin.

# 1.5.0: the review's fixes

Every finding of the strict review of 2026-10-12 (`docs/REVIEW-2026-10-12.md`), in its order, with a regression test where code changed.

## Fixed

1. **Merging is now the gate** (finding 1): the apply identity's provider accepts only `infra-apply.yml` on `refs/heads/main` in the `infra` environment, the plan identity's provider only `infra-plan.yml` on a `pull_request` event, and each binds through an attribute the other provider never maps. `main` is protected (pull request, the `test` and `terraform` checks, administrators included, no force push), the environment forbids administrator bypass, and `.github/CODEOWNERS` names the owner.
2. **The plan policy fails closed** (findings 2, 15): public members refused everywhere, an allow-list of roles an application may grant (plus the platform's `labDeploy*` custom roles), the platform's identities and the state bucket matched in every string field, authoritative policies refused, the invariants (private database, delete protection, private bucket, labels, region including `location_id`) checked on update too, unknown values refused. The checker runs from a checkout of `main`, not from the pull request, and the comment carries the full plan. The renderer escapes `${` and `%{`, and `migrate` parts reject `,`, `$`, `^` and `"`.
3. **Tools no longer block the controller** (finding 3): every tool runs in a thread pool with the caller's identity carried along; a status wait no longer stalls sign-ins or other callers.
4. **A runner lost during a migration recovers** (finding 4): `migrating` is a recoverable checkpoint state (traffic untouched), the advice names it.
5. **One-word migration commands release** (finding 5): `--args=` resets the job's arguments; `--clear-args` never existed.
6. **The cap is robust** (finding 6): every call retries, each service and instance is handled in its own try block, failures are logged, counted and end the execution, an alert policy watches failed executions, the ratio comes from `costAmount` and `budgetAmount` (so any threshold works), a resource labelled `lab-deploy-cap-lifted=true` is left alone, and `ingress` joined the service's `ignore_changes` so a later apply does not reopen a capped service. The cap's per-service grant is a custom role (`labDeployCapOperator`: read and update a service) instead of `roles/run.developer`.
7. **Lost dispatches end** (finding 7): a plan still `dispatching` or `dispatch_unknown` two minutes later with no visible run becomes `dispatch_lost`, a final state with advice.
8. **Auto-deploy retries** (finding 8): a `planned`, errored or lost attempt is retried after thirty minutes instead of sticking forever.
9. **Firestore cost note corrected** (finding 9): no free quota for named databases, regional storage at 0.15 per GiB-month.
10. **One migration timeout** (finding 10): the job's task timeout is derived from the runner's command timeout; a timeout records the execution as still running and the release fails without a second migration.
11. **The ceiling is named** (finding 11): the policy text says it compares the compute bound plus the needs' floor; the report prices live settings as they are and warns when live `maxScale` exceeds the reviewed bound.
12. **SQLite in WAL mode with guarded writes** (finding 12): `finish` only completes a record in its claimed state; OAuth tables are pruned hourly.
13. **No client secrets** (finding 13): registered clients are public (`token_endpoint_auth_method: none`, PKCE); the access log no longer records query strings. Access tokens and authorization requests are bound to the resource (finding 26).
14. **Narrower pipeline identities** (finding 14): plan has specific viewer roles and security reviewer, no `roles/viewer`; apply has the admin roles a root needs and a custom bucket-manager role, no `roles/editor`, no `roles/storage.admin`; state access is conditioned to the `apps/` prefix (plus read of the foundation's outputs).
15. **Documents match the code** (finding 16): README, ARCHITECTURE, HANDOFF, SETUP and ROADMAP corrected; `LICENSE` (MIT); Dependabot watches the roots that exist; CI runs weekly and audits both lock files.
16. **Disaster recovery** (finding 17): `backup_bucket` sends the nightly copy and the configuration to the foundation's new private versioned bucket (`PROJECT-lab-deploy-backups`); the operator scripts live in `scripts/`; `docs/RUNBOOK.md` covers the laptop-off case, a rehearsed restore, the cap, stuck releases, credentials and a monthly alert exercise.
17. **Container behind a proxy** (finding 18): the Dockerfile documents `FORWARDED_ALLOW_IPS`.
18. Low findings 19 to 25: a re-run gets a fresh revision suffix (`a2`, `a3`); candidate log lines are marked untrusted; names must be 3 to 21 characters without a trailing dash; the scheduler is joined on shutdown; `var.subnet` is used by the migration job; the Pub/Sub API is managed once; the billing dataset is labelled; `install-launch-agent.sh` defines its variable; the foundation apply script shows the plan and asks; `start.sh` never puts the token on a command line; the reload scope is documented.

## Upgrade steps

1. Re-apply the foundation (`scripts/apply-foundation.sh PROJECT_ID`): two providers replace the old one, roles change, the cap's custom role, alert policy and the backup bucket appear.
2. On the platform repository replace `INFRA_WIF_PROVIDER` with `INFRA_PLAN_WIF_PROVIDER` and `INFRA_APPLY_WIF_PROVIDER` from the output; protect `main` and forbid bypass on the `infra` environment.
3. Re-render every application root (the cap's role input) and merge; runner code changed, so regenerate the app workflows against this commit and raise `platform.minimum_commit`.
4. Set `backup_bucket` in the controller configuration to the foundation's `backup_bucket` output and restart; the scripts in `~/.local/share/lab-deploy/` now call the ones in `scripts/`.

# 1.4.2: fix from the live plan

## Fixed

1. **The label policy refused every Cloud SQL instance**: it looked for a top-level `labels` map, and Cloud SQL keeps its labels under `settings.user_labels`. Found by the first plan of a database root, which the policy refused as designed.

# 1.4.1: fix from the live apply

## Fixed

1. **Cloud Workflows limits an expression to 400 characters**; the cap's log line had grown to 445 with the database count. The message is now built in parts, the test checks the limit, and the corrected workflow was deployed and deleted as a throwaway before this release. The foundation re-apply updates the workflow.

# 1.4.0: Cloud SQL, migrations and a cap that stops databases (Phase 6, items 6.5 and 6.6)

## Added

1. **`database: postgres`** under `needs`: the smallest Cloud SQL for PostgreSQL instance (shared core, zonal, 10 GB of HDD that grows to 20, daily backups kept a week), with a private address only and IAM sign-in only: no password exists anywhere. The runtime identity is the database user; the revision gets `DATABASE_INSTANCE` (the connection name), `DATABASE_NAME`, `DATABASE_USER` and `DATABASE_IP_TYPE`, and the service joins the foundation's private network for its egress. It bills every hour it exists, about 9 dollars a month: every plan and estimate says so, the warning names it, and the operator's cost ceiling now counts it.
2. **`migrate`** under `needs`: a command run as the `<service>-migrate` Cloud Run job from the release's own image before the candidate exists, so a schema change lands before any new code serves. A failed migration stops the release with traffic untouched; the receipt gains a `migrating` phase and the job's execution name. Keep migrations compatible with the previous revision (expand first, contract later), so a traffic rollback stays safe.
3. **The foundation's private network**: one VPC with a subnet for Cloud Run egress and Private Service Access for Cloud SQL, free while idle; the Compute, Service Networking and Cloud SQL Admin APIs.
4. **The spending cap stops databases**: at the budget line the cap workflow also sets every managed Cloud SQL instance's activation policy to never, after closing ingress. Restart one with `gcloud sql instances patch NAME --activation-policy=ALWAYS`.
5. Policies: a database instance is created with deletion protection; the plan refuses a public address.

## Upgrade steps

1. Re-apply the foundation (network, subnet, peering, three APIs, the cap's Cloud SQL role) and re-render each application root (the `migrate` field and the network inputs). Runner code changed: regenerate the app workflows against this commit and raise `platform.minimum_commit`.

# 1.3.0: spend by application (Phase 6, item 6.6, part one)

## Added

1. **Actual spend by application.** The billing export query now groups by Google service and by the `lab-deploy-app` label that every managed resource carries, so the weekly report, the living issue and `get_cost_report` show the project's spend by application beside the per-service view, and the app's own labelled spend with its breakdown. Firestore operations and Cloud Run request fees carry no label and appear as unlabelled.

The other half of item 6.6, the spending cap stopping databases, arrives with Cloud SQL (item 6.5).

## Upgrade steps

None beyond the billing export being enabled; the next right-sizing run shows the new breakdown.

# 1.2.0: needs in the health view and the retirement checklist (Phase 6, item 6.7)

## Added

1. **`get_service_health` checks the declared needs**: each bucket, topic, subscription and database with whether it exists and the facts that matter (location, acknowledgement deadline, delete protection), through the observer identity, which gains the bucket, Pub/Sub and Datastore viewer roles in the foundation. A missing need becomes a note that names the fix.
2. **`lab-deploy decommission` lists the needs** with their data-loss warnings and the exact commands, reads them from the app's `platform.yaml` when a GitHub credential is present, and tells the operator to remove them from the state before `terraform destroy`.

## Upgrade steps

1. Re-apply the foundation (three viewer roles for the observer) and restart the controller.

# 1.1.1: fix from the live run

## Fixed

1. **The pipeline's apply identity could not finish the first `needs` apply**: the editor role sets no Pub/Sub IAM policies and creates no Firestore databases (two 403s after the bucket, topic and subscription were created). The foundation now grants it `roles/pubsub.admin` and `roles/datastore.owner`.

## Upgrade steps

1. Re-apply the foundation, then re-run the apply for the affected application root (`workflow_dispatch` on the apply workflow).

# 1.1.0: what an app needs, part one (Phase 6, items 6.1 and 6.2)

## Added

1. **`needs` in `platform.yaml`**: `buckets`, `topics` and `database: firestore`. The platform provisions them through the app's Terraform root and the pipeline, and the release sets the derived variables on every revision: `BUCKET_<NAME>` (`<project>-<service>-<name>`), `TOPIC_<NAME>` and `SUBSCRIPTION_<NAME>` (one pull subscription per topic) and `FIRESTORE_DATABASE` (`<service>`). Names follow fixed conventions, so no output travels anywhere and the variables cannot collide with `env` or `secrets`.
2. **Least privilege per need**: the runtime identity gets object user on its buckets, publisher on its topics, subscriber on its subscriptions and datastore user on its own named database only; the deployer may only look, which the new pre-check uses: a release whose needs do not exist yet stops before deploying and says what to merge first.
3. **Policies**: buckets use uniform access with enforced public access prevention; databases are created with delete protection; topics and subscriptions carry the managed labels. Buckets and databases cannot be destroyed by a plan.
4. **Cost**: every plan and estimate lists the declared needs with their list prices and a monthly floor of zero (they cost by use), and the findings explain the free allowances.

## Changed

- The spec hash leaves default values out, so adding an optional field to the schema no longer changes the hash of a spec that does not use it. This change itself changes every hash once.
- The foundation enables the Pub/Sub and Firestore APIs.
- Per-app infrastructure settings (alert thresholds, image retention) live under `applications.<app>.infra` in the controller configuration; the renderer writes them into the root, which is never the source of truth.

## Upgrade steps

1. The spec hash changed: regenerate the app workflows against this commit **before** deploying and set `platform.minimum_commit` to it; plans created before the upgrade must be created again.
2. Re-apply the foundation (two APIs) and re-render each application root (one `needs` line); the pipeline applies it.

# 1.0.2: fixes from the live run, part two

## Fixed

1. **A failure in the publish step was hidden** by the pipe that also writes the job summary, so the first pipeline apply reported success although the outputs were never published. Both workflows now run their steps with `pipefail`, and `publish` explains a root whose outputs predate the current renderer instead of crashing.
2. The demo app's committed root was rendered before the `github_repository` output existed; it is re-rendered.

# 1.0.1: fixes from the live run

## Fixed

1. **The region policy refused every recovery bucket**: Cloud Storage reports locations in capitals (`US-CENTRAL1`), and the rule compared literally. Found by the first pipeline apply, which refused the demo app's plan exactly as designed; the comparison now ignores case.
2. **Cloud Workflows rejected the spending-cap workflow's discovery step**: its expressions cannot contain map or list literals (`{}`, `[]`). Missing fields are now tested with `in` and ordered switch conditions; the test asserts no such literal appears, and the rendered workflow was deployed and deleted as a throwaway before this release.

## Upgrade steps

1. Re-apply the foundation root (one change: the cap workflow's source).

# 1.0.0: infrastructure as pull requests (Phase 6, item 6.3)

## Added

1. **One Terraform root per application.** `infra/gcp/project` is the foundation root (APIs, observer and pipeline identities, cost controls, alert channels, spending cap), applied by the operator and rarely changed. Each application gets `infra/gcp/apps/<service>`, three generated files with every input written out, its own state (`apps/<service>/<environment>`), and the project-wide values read from the foundation's state. `lab-deploy infra render APP` writes a root from the controller configuration, the app's reviewed `platform.yaml` and GitHub's immutable ids; the new tool `propose_infrastructure` renders it and opens the pull request on the platform repository.
2. **The pipeline.** `infra-plan.yml` plans every root a pull request touches with a read-only identity, checks the plan against the platform's policies and posts one comment per application, updated in place. `infra-apply.yml` applies on merge with a write identity from the protected `infra` environment, checks the policy again right before applying, and publishes the outputs to the application's GitHub environment when the `APP_REPOS_TOKEN` secret exists (otherwise it prints them to copy). Both stay dormant until the three `INFRA_*` repository variables are set from the foundation root's `pipeline` output. `workflow_dispatch` applies named roots by hand.
3. **Policies** (`labdeploy/infra.py`, tested, standard library only): no service account keys, no owner or editor grants, no destruction of resources that hold data, nothing outside the allowed region, no public database addresses, every managed resource labelled `lab-deploy-app`, and no touching of the platform's own identities from an application root.
4. **Managed labels.** Services, registries and recovery buckets carry `lab-deploy-managed` and `lab-deploy-app`; releases merge their revision labels in with `--update-labels` instead of replacing them. The spending-cap workflow now discovers the services to close by label, so adding an application no longer needs a foundation re-apply.
5. **Pipeline identities** in the foundation root: a Workload Identity pool for the platform repository, a plan identity (viewer, security reviewer, state reader) and an apply identity whose roles are listed in `pipeline.tf`; the apply identity is only reachable from the `infra` GitHub environment.

## Changed

- The single root (`infra/gcp/main.tf`, `variables.tf`, `outputs.tf`, `moved.tf`) is gone; `services` is no longer a variable. `~/.local/share/lab-deploy/migrate-state.sh` splits an existing state into the two new ones once, without touching the cloud, and the operator's apply script now applies the foundation root.
- `lab-deploy decommission` tells the operator to run `terraform destroy` in the app's root; the pipeline never destroys.

## Upgrade steps

1. Run the state migration once, then apply the foundation root (it adds the pipeline identities and the cap's list permission).
2. Set `INFRA_WIF_PROVIDER`, `INFRA_PLAN_SERVICE_ACCOUNT` and `INFRA_APPLY_SERVICE_ACCOUNT` on the platform repository and create the `infra` environment restricted to `main`; optionally add `APP_REPOS_TOKEN`.
3. Merge the rendered root of each existing application; the pipeline applies the label changes. Runner code changed (`--update-labels`), so regenerate the app workflows and raise `platform.minimum_commit`.

# 0.9.5: clean plans

## Fixed

1. **Every plan wanted to update the dashboard.** The Monitoring API stores dashboards with an explicit axis on each data set and without zero positions or empty group lists, so Terraform saw a difference on every run. The dashboard is now written in that stored form; a read-only plan against the fully applied staging project reports no changes.

## Upgrade steps

None: the next plan is simply clean.

# 0.9.4: fix from the live apply, part three

## Fixed

1. **The uptime check was rejected again** ("validate_ssl feature is currently only allowed on uptime_url checks"): certificate validation is only offered for plain-URL checks. The option is dropped for the Cloud Run target; the check still uses HTTPS. A throwaway check with the exact final shape was created through the API and deleted again before this release, so the next apply is expected to pass.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check and the health alert.

# 0.9.3: fixes from the live apply, part two

## Fixed

1. **The uptime check was rejected** ("Service agent authentication is not supported for arbitrary URLs"): Google offers the Monitoring service agent's OIDC token only for checks that name a Cloud Run service as a `cloud_run_revision` resource, not for a plain URL. The check now names the service by project, location and name, as Google's own example does, and the health alert filters on that resource type.
2. **The saturation alert was rejected**: the Monitoring API only offers greater-than and less-than comparisons. "n or more instances" is now expressed as "more than n-1", which is the same test for whole numbers.

## Upgrade steps

1. Re-run the Terraform apply: it creates the uptime check, the health alert and the saturation alert that the previous apply could not.

# 0.9.2: fix from the live apply

## Fixed

1. **Cloud Workflows rejected the spending-cap workflow** ("Unterminated expression"): its log messages contain a colon followed by a space inside expressions that were not quoted, which YAML reads as a nested key. The six string-building expressions are now single-quoted, as Google's workflow syntax guide asks, and a test renders the file with Terraform's placeholders, parses it and checks that every expression is a complete scalar.
2. **`get_service_health` reported every service as not ready.** Cloud Run v2 returns the Ready condition as `terminalCondition`; the observer looked for it in `conditions`, which only holds RoutesReady and ConfigurationsReady. Found on the first live call after the observer identity existed.

## Upgrade steps

1. Re-run the Terraform apply: it creates the workflow and the Eventarc trigger that the first apply could not.
2. Restart the controller.

# 0.9.1: fix from the live run

## Fixed

1. **The observer error quoted gcloud's impersonation warning** instead of the reason the token could not be minted; it now quotes gcloud's error line.

# 0.9.0: always on, on a laptop

## Added

1. **Live views.** `get_service_health` shows what Cloud Run serves right now (readiness, revisions with the plan and commit they carry, ingress), the last hour's requests, server errors, p95 latency, instances and memory, and whether it agrees with the release this controller recorded. `get_recent_logs` returns the newest log lines at or above a severity. Both read through a viewer-only **observer identity** created by Terraform: on a machine the identity is attached; on a laptop the operator impersonates it through gcloud (`observer_impersonators`), so the controller never holds a key and never gains write access. Configure `observer.service_account`.
2. **Auto-deploy.** `auto_deploy: true` on an application makes the controller release the tip of main whenever it is neither the live release nor already attempted, as the identity `auto-deploy`, with every guardrail (pause, policy, cost ceiling, platform pin) still applied. Interval `auto_deploy_interval_seconds` (default 300).
3. **Watchdog.** Once a day the controller checks its configuration file, the GitHub credential's expiry, each app's schedules, platform pin and newest release attempt, records the result, and posts problems to `notify_webhook_url`. `lab-deploy watchdog` runs it on demand.
4. **Backups.** A consistent copy of the database every night into `backup_dir` (default next to the database), kept `backup_keep_days` (default 14). `lab-deploy backup` runs it on demand.
5. **Controller watch** from GitHub: the platform repository's `controller-watch.yml` pings the public health address every thirty minutes when the `CONTROLLER_HEALTH_URL` repository variable is set; a failed run means the laptop, the tunnel or the controller is down, and GitHub's failed-workflow email says so.
6. `get_controller_status` reports the observer, the auto-deploy apps and the background jobs' last and next runs.

The always-on machine of the roadmap's Phase 5 is deferred by the operator's choice; everything above also works there with the observer identity attached.

## Upgrade steps

1. Apply the Terraform change (observer identity, its viewer roles and your impersonation grant) and set `observer.service_account` in the configuration.
2. Optionally set `notify_webhook_url`, `auto_deploy: true` per app, and the `CONTROLLER_HEALTH_URL` variable on the platform repository.

# 0.8.0: more kinds of apps

## Added

1. **Configuration and secrets in `platform.yaml`.** `env` holds plain values set on every revision; `secrets` lists names of Secret Manager secrets called `<service>-NAME` that Cloud Run mounts as variables. The reviewed file is the whole truth: values not in it are removed from the revision. Before deploying, the runner checks that every referenced secret exists with an enabled version and names the missing ones. Terraform grants the runtime identity read access, and the deployer existence checks, only to secrets named after the service, through IAM conditions. Values are created by the operator with gcloud and never pass through Claude.
2. **Apps without tests.** `tests: none` in `platform.yaml` skips the unit-test step knowingly: the plan's checks say `skipped_by_config`, every plan carries a warning, and the run annotates itself. An unexpected skip still shows as `not_run`.
3. **Request timeout** `timeout_seconds` (1 to 300) replaces the fixed sixty seconds.
4. **Reproducible builds.** The analyzer flags a `requirements.txt` without hashes and recommends a `requirements.lock`; the test step installs such a lock with `--require-hashes` when the repository has one.
5. **`lab-deploy decommission APP`** prints the ordered checklist that retires an application without leaving a bill or a dangling trust.

## Upgrade steps

1. The specification gained fields, so every plan hash changes: regenerate the app workflows with this platform commit **before** deploying, and set `platform.minimum_commit` to it so the controller refuses older pins.
2. Apply the Terraform change (Secret Manager API and the two conditional grants).

# 0.7.1: fix from the live run

## Fixed

1. **The cost-report issue step failed** because the right-sizing job never checks out the application repository and the GitHub CLI looked for one; every call now names the repository explicitly.

# 0.7.0: real cost control

## Added

1. **Spending cap** (`spending_cap` in Terraform, on by default with a budget): every budget notification reaches a Pub/Sub topic; an Eventarc trigger runs a Cloud Workflow that closes ingress on every platform service once the budget line is crossed, and optionally detaches billing at a higher line. No code to build. Reopen a service with `gcloud run services update SERVICE --ingress=all`; the next release reopens it as well.
2. **Terraform in two modules**: `modules/project` (APIs, scanning, budget, cap, alert channels, tag role, billing dataset) and `modules/service` (one per application, keyed by service name in the `services` map). `moved.tf` migrates the pilot's single-module state in place; a plan against the staging state showed every resource moving and nothing destroyed.
3. **Living cost-report issue**: the weekly right-sizing workflow creates or updates one issue per repository with the summary, proposals, drift and actual spend (`issues: write`).
4. **`propose_rightsizing(application)`** turns the latest report's concrete proposals into a pull request against `platform.yaml`, editing values in place so comments survive, with the savings and the cost bounds before and after. The staging policy and the cost ceiling still apply.
5. **Actual spend**: with `billing_export_dataset` in Terraform, the billing export enabled once in the Billing console, and the `BILLING_EXPORT_TABLE` repository variable, the weekly report and the issue show the project's net spend by Google service next to the list-price figures.
6. **Schedule watchdog**: `list_applications` reports, per app, whether GitHub still runs the recovery and right-sizing schedules, when they last ran and how they concluded. GitHub pauses schedules after sixty idle days.
7. Image scanning is documented as a per-project cost choice (`image_vulnerability_scanning`).

## Upgrade steps

1. Rewrite `terraform.tfvars` into the `services` map (see docs/SETUP.md) and apply; the state migrates automatically.
2. Regenerate the app workflows (the right-sizing workflow changed).
3. Optional: enable the billing export to the new dataset and set `BILLING_EXPORT_TABLE`.

# 0.6.2: two more findings from the live exercise

## Fixed

1. **A reused image's release was reported as `completed_unverified`** because the skipped build job has no test and audit steps. The plan's `passed_in_earlier_release` checks are now kept and count as passed.
2. **The audit trail recorded a final outcome twice** when the record was re-saved with the recovery run's result. An event is now recorded only when the state actually changes.

# 0.6.1: fixes from the live steering exercise

## Fixed

1. **Status of a plan pinned to an older commit** was rejected as a workflow identity mismatch: GitHub reports the run's head as the branch tip, not the plan's commit. The unique plan id in the run title is the identity; the head check now applies only to plans of the tip itself.
2. **Image reuse ignored a rolled-back release.** A rolled-back release's image is still the attested image of that commit, so it now counts for reuse.
3. **Provenance verification failed when the build was skipped**, because the registry login lived in the push step. A dedicated "Configure registry access" step now runs in both modes.
4. **Stale serving tags could not be deleted**: the deployer's writer role creates tags but cannot delete them. Terraform adds a one-permission custom role on the repository, and the reconcile reports a failed deletion instead of abandoning the update.

## Upgrade steps

1. Apply the Terraform change (custom role and binding) and regenerate the app workflows.

# 0.6.0: steer after a release

## Added

1. **`rollback_staging(application)`**: traffic returns to the split that served before the live release, through the recovery workflow's new `rollback_plan_id` input. No build; seconds. The runner refuses when the checkpoint belongs to another release, the release did not complete, or traffic has moved since. The rolled-back release stops counting as live.
2. **`cancel_deployment(plan_id)`** cancels the GitHub run; the recovery workflow restores traffic if the release had started changing it.
3. **Deploy a specific commit**: `create_deployment_plan(application, commit_sha=...)` accepts any commit already on main. The controller checks the history through GitHub's compare, and the workflow checks it again before building.
4. **Image reuse**: a commit released before is redeployed from its already-attested image. The build job is skipped, the configuration comes from the commit itself, provenance is verified again, and the plan's checks say `passed_in_earlier_release`.
5. **`compare_with_live(application)`**: commits and files on main since the live release, what kinds of change they are (code, dependencies, Dockerfile, configuration, workflows), and the cost bounds before and after when `platform.yaml` changed.
6. **`deploy_latest(application)`** plans and dispatches in one call.
7. **Pause switches** honoured without a restart: `paused: true` on the whole configuration or on one application refuses new plans and dispatches; reads keep working. The controller re-reads its configuration file whenever it changes; an invalid file is reported by `get_controller_status` and ignored.
8. **`lab-deploy sessions`** lists Claude's sign-in sessions and ends one or all of them.
9. **Platform pin check**: `list_applications` reports the platform commit each app's workflows pin; with `platform.minimum_commit` set, dispatch refuses older pins and names the fix.
10. **Drift in the weekly report**: live Cloud Run settings compared with the reviewed `platform.yaml`, differences named in the report and its summary.

## Changed

- The explicit Deployments API steps added in 0.5.0 duplicated the record GitHub keeps for the deploy job's environment; the job's environment now carries the service URL instead, and the `deployments` permission is gone.
- CI passes the template files to the workflow linter explicitly; given the directory, it audited only the platform's own workflow.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (`update-workflows --all`); the deploy workflow has a new `image_digest` input and the recovery workflow a `rollback_plan_id` input.
2. Optionally set `platform.minimum_commit` to this platform commit so apps on older pins are refused until regenerated.

# 0.5.0: see every step

## Added

1. **Full timeline in status.** `get_deployment_status` reports every step of both jobs with timing, names the first failed step, fetches the release receipt and vulnerability summary once the run ends and stores them with the plan, shows the release step's own phases with timestamps (preparing, deploying, promoting, succeeded or restored), the gcloud error line, whether traffic was restored and which revision serves, the recovery run that followed a cancelled or lost release, and a plain `next_action` for every state. `wait_seconds` (up to 45) waits for a change before answering.
2. **Release history.** A finished run's outcome, receipt, revision, digest and commit are stored, so history survives GitHub's artifact retention. `list_plans` shows outcome, commit summary and revision; `list_applications` shows the live release this controller recorded and the commits main holds beyond it. Plans carry the commit message, author and date.
3. **Candidate logs.** When a candidate fails, the runner puts its last fifty log lines into the receipt (Terraform grants the deployer `roles/logging.viewer`).
4. **Revision labels, release tags, serving tags.** Every revision is labelled with its plan id and commit; every pushed image also gets an immutable `release-<plan>` tag; the release and right-sizing jobs tag each serving revision's digest `serving-<revision>` and the registry cleanup policy keeps those, so an image that still serves is never deleted.
5. **Notifications.** A final job in the deploy, recovery and right-sizing workflows posts the outcome with the receipt summary and a link to the `NOTIFY_WEBHOOK_URL` repository secret (Slack or Discord incoming webhook). Nothing is sent when the secret is unset; routine recovery runs stay quiet.
6. **GitHub Deployments.** Each release is recorded through the Deployments API (in progress, then success, failure or error, with the service URL), visible on the commit and in the repository's Environments view. The deploy job gains `deployments: write`.
7. **Audit everything.** Every tool and REST call, sign-in, failed-login burst, client registration, token reuse and revocation is recorded with identity, client address, outcome and duration. New `list_events` tool and `GET /events`; deployers see every identity, read-only identities see their own. Optional `log_file` setting writes a rotating controller log.
8. **Runtime alerts in Terraform** (`runtime_alerts`, default on): an authenticated uptime check on the health path, alert policies for the health check, 5xx responses, p95 latency, instances at maximum, memory above 80% and error lines in the log, email and webhook notification channels (`alert_email`, `alert_webhook_url`), and a console dashboard. Outputs `console_url` and `dashboard_url`.
9. **`update_app_workflows` tool and `lab-deploy update-workflows --all`.** Regenerate an app's three workflows against the platform's current (or a given) commit and open one pull request per repository. Needs `platform.repository` in the configuration and a credential with Contents, Pull requests and Workflows write on the app repositories.

## Upgrade steps

1. Add `platform: {repository: OWNER/lab-deploy}` to the configuration; optionally `log_file`.
2. Apply the Terraform change (logging viewer, cleanup policy, alerts, uptime check, dashboard). Set `alert_email` and, if wanted, `alert_webhook_url` and `health_path` in `terraform.tfvars`. In a new project create the Monitoring service agent first: `gcloud beta services identity create --service=monitoring.googleapis.com`.
3. Add the `NOTIFY_WEBHOOK_URL` secret to each app repository if notifications are wanted.
4. Regenerate the three app workflows with the new platform commit, now with `update-workflows --all`.

# 0.4.1: housekeeping and the two cost leaks

## Changed

1. **Recovery schedule** runs every six hours instead of every ten minutes. The completion trigger after each release handles the normal case, and a lost runner leaves traffic on the previous revision, so recovery is not urgent. On a private repository the old schedule consumed roughly 6,000 to 9,000 Actions minutes a month.
2. **Cost estimates list per-release overheads** outside the Cloud Run figures (the image scan fee of about USD 0.26 per release, Actions minutes, registry storage) and warn when the recorded list prices are more than 90 days old.
3. **GitHub failures are explained in plain words** to Claude and to REST callers: expired token, missing permission, rate limit, missing repository or file, refused dispatch, outage. An app without `platform.yaml` is told to run `propose_configuration`.
4. **New `get_controller_status` tool**: version, dispatch and sign-in switches, registered apps, GitHub credential expiry with a two-week warning, pricing freshness and uptime. The MCP handshake reports Lab Deploy's version, and the controller logs a warning at startup when the credential is about to expire.
5. **`lab-deploy sync-pins`** copies action pins from the platform CI workflow into the templates, so a Dependabot update becomes one command. `actions/checkout` and `actions/setup-python` move to v7.0.1 and v7.0.0.

## Upgrade steps

1. Regenerate the three app workflows with the new platform commit (schedule and action pins changed).
2. Dependabot's Terraform provider update (google 7.x to 8.x) is deferred to the Terraform restructuring planned in `docs/ROADMAP.md`.

# 0.4.0: browser sign-in for Claude chat

## Added

1. **OAuth 2.1 authorization server** (`labdeploy/oauth.py`), enabled by `oauth.public_url`: protected-resource and authorization-server metadata, dynamic client registration restricted to Claude's callback and loopback redirects, PKCE S256, five-minute single-use codes, one-hour access tokens, thirty-day rotating refresh tokens with reuse detection, revocation, and a sign-in page at `/login` that accepts only a configured operator token. Tokens and codes are stored as SHA-256 hashes in the existing SQLite file. Unauthenticated requests now answer `WWW-Authenticate: Bearer resource_metadata="..."` so Claude discovers the sign-in. Rotating an operator's token hash ends its sessions.
2. **Request gate** accepts either a static operator token or a live access token; the OAuth endpoints are rate limited per client address and failed sign-ins count against the existing failed-login budget.

## Upgrade steps

1. Optional: set `oauth.public_url` to the controller's public https origin and list that host in `allowed_hosts`. Without it nothing changes.
2. The database gains four `oauth_*` tables automatically on start.

# 0.3.1: fixes from the live staging trial

## Fixed

1. **Identity federation condition:** GitHub's OIDC `sub` claim now embeds numeric IDs, so the Workload Identity Federation condition matches the `environment` claim plus the immutable repository and owner IDs, the branch and the workflow paths. Re-apply Terraform; the provider updates in place.
2. **Status right after dispatch:** a run that GitHub has not finished creating is reported as not visible yet instead of failing the identity check.
3. **Traffic and tag commands verify the routing state** rather than trusting gcloud's exit code, which is non-zero whenever the service's newest revision is not Ready.
4. **Failed candidates stay in place:** Cloud Run refuses to delete a service's newest revision. The failure path restores traffic, drops the tag, and records `service_ready` with a note; the previous revision keeps serving until the next successful release.
5. **Receipts record the underlying error text** (the command's ERROR line) for the failed step and for a failed restoration.

## Upgrade steps

1. Apply the Terraform change (one in-place update of the identity provider).
2. Regenerate the three app workflows with the new platform commit.

# 0.3.0: hardening against 2026 practice

## Added

1. **Rate limiting** in the authentication layer: per-identity request budgets and per-client failed-login budgets (`rate_limit_per_minute`, `auth_failures_per_minute`), answering 429 with `Retry-After`.
2. **Tool safety hints and structured output**: every MCP tool declares a title, `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, publishes an output schema and returns `structuredContent`. The demo walkthrough shows the hints.
3. **Cloud Run startup probe** from the reviewed `health_path` and port (`--startup-probe`, 60 seconds of grace), so Cloud Run itself gates traffic.
4. **Build provenance**: `actions/attest-build-provenance` signs the pushed image digest and `gh attestation verify` checks it before release; the deployment job gains `attestations: write`.
5. **Image vulnerability summary** (`labdeploy/scan.py`): waits for the Artifact Analysis scan, writes `vulnerability-summary.json` into the release receipt, and blocks the release on critical findings when `BLOCK_ON_CRITICAL_VULNERABILITIES=true`. Terraform enables the Container Scanning API (`image_vulnerability_scanning`) and grants the deployer read access to results.
6. **Repository hygiene**: ruff configuration and a clean lint, zizmor workflow linting (with one documented suppression for the recovery workflow's `workflow_run` trigger), pip-audit of the platform lock and a Terraform validation job in CI, Dependabot for actions and providers, `.github/CODEOWNERS` (placeholder owners), `SECURITY.md`, `.pre-commit-config.yaml`, and tests that the templates' action pins match the platform's own.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit; the deployment workflow has new steps and permissions.
2. Apply the Terraform change (Container Scanning API and the deployer's occurrence-viewer role; set `image_vulnerability_scanning = false` to opt out).
3. Optionally set the repository variable `BLOCK_ON_CRITICAL_VULNERABILITIES=true` and the controller's rate-limit fields.
4. Replace the CODEOWNERS placeholder with real maintainers and choose a license.

# 0.2.0: cost optimizer

## Added

1. **Cost bounds in every plan** (`labdeploy/cost.py`): idle floor, worst-case compute ceiling and an optional traffic-scenario range computed from Google's published Tier 1 Cloud Run list prices, with assumptions and exclusions listed. New `estimate_cost` tool, `GET /applications/{id}/cost` route and `lab-deploy cost` CLI command.
2. **Operator cost ceiling**: `cost_ceiling_usd` per application in the controller configuration. Plans whose worst-case monthly compute exceeds it are refused at planning and again at dispatch.
3. **Cost findings** from Google's cost-optimization guidance: warm instances with their monthly price, unmeasured memory or CPU, low concurrency, the effect of `scaling.maximum`, and authenticated-only exposure.
4. **Measured right-sizing**: `templates/rightsize-gcp.yml` (weekly, read-only, pinned platform code, no application checkout) reads Cloud Monitoring usage through `labdeploy/telemetry.py`, prices it, proposes `platform.yaml` changes with estimated savings, lists `google.run.service.CostRecommender` recommendations, and uploads `cost-report.json`. New `get_cost_report` tool and `GET /applications/{id}/cost-report` route fetch the newest artifact.
5. **`concurrency`** is now a reviewed `platform.yaml` field (default 80, the Cloud Run default). The analyzer drafts 8 for synchronous frameworks and adds a review question. Existing spec hashes change; create fresh plans after upgrading.
6. **Terraform cost controls**: Artifact Registry cleanup policies (dry run by default), recovery-bucket lifecycle rules, project-level `roles/monitoring.viewer` and `roles/run.viewer` for the deployer identity, the right-sizing workflow in the WIF condition, and an optional monthly billing budget with current and forecast thresholds.

## Upgrade steps

1. Regenerate all three app workflows with the new platform commit and commit `rightsize-gcp.yml`.
2. Apply the Terraform change (new APIs, IAM grants, cleanup policies, WIF condition; budget only if configured).
3. Optionally add `cost_ceiling_usd` to each application in the controller configuration and restart the controller.
4. Create fresh plans; the spec hash now includes `concurrency`.

# 0.1.2: local demo mode and hardening

## Added

1. **Local demo mode:** `python -m labdeploy.demo serve` runs the real controller and MCP server against an application directory on disk, with dispatch permanently disabled and a repository adapter that refuses every write. `python -m labdeploy.demo walkthrough` exercises every tool and two policy refusals. `labdeploy.server:create_app` never uses this module, so the running service still substitutes no local or fake GitHub data.
2. **Plan listing and audit trail:** the `list_plans` tool and `GET /plans` return the caller's own plans, newest first. Status responses include the persisted `events` for the plan (`planned`, `dispatching`, `dispatched`, `dispatch_unknown`).
3. **Model instructions:** the MCP server sends usage instructions during the handshake: tool order, plan expiry, what `dispatched` means, and that policy refusals must be relayed rather than worked around.

## Changed

4. **Pinned base images:** both Dockerfiles pin `python:3.12-slim` to its multi-arch index digest, with the refresh command in a comment.
5. **CLI errors:** validation, policy, missing-file and overwrite failures print one plain line and exit non-zero instead of a traceback.
6. **Database connections** are closed after every operation; commit and rollback behaviour is unchanged.
7. **Authentication scheme:** `bearer` is matched case-insensitively per RFC 9110; tokens are still compared by hash.
8. **Configuration** rejects wildcard `allowed_hosts` entries, which the MCP transport would silently never match.

## Upgrade steps

No configuration or workflow changes are required. Rebuild the controller image to pick up the pinned base image.

# 0.1.1: deployment review fixes

## Fixed

1. **Stale status:** the controller now reads jobs from the exact GitHub run attempt. The returned lifecycle state and individual check results reflect the observed steps. Overall workflow success without expected step evidence is `completed_unverified`, never an invented pass. Dispatch bookkeeping stays separate so a completed plan cannot be redispatched by mistake.
2. **Unbound destination:** application registration now requires a typed cloud target. Plans carry it; the controller rejects registry drift; the workflow rejects environment drift before authentication. Project, region, service, artifact repository, deploy/probe service accounts, WIF provider and recovery bucket are included. Older plans without a target cannot deploy.
3. **Pending deployments replaced:** release and recovery workflows use the same concurrency group with `queue: max` and `cancel-in-progress: false`. GitHub currently allows up to 100 pending runs with this setting. It is not an unlimited queue, and dispatch order is not guaranteed. The installed actionlint release does not yet recognize this documented field; all other lint errors remain fatal.
4. **Interrupted releases:** before traffic changes, the runner saves its exact rollback map to a versioned GCS object. Writes use generation preconditions. A graceful interruption attempts immediate traffic restoration. A separate pinned recovery workflow runs after release completion and on a ten-minute schedule, under the same concurrency group. It recovers unfinished checkpoints, refuses target mismatches or unrelated current revisions, and never rolls back a checkpoint marked successful.

The recovery workflow does not check out or execute the triggering app's code. It operates only on the operator-configured service and saved checkpoint. Scheduled GitHub runs may be delayed or disabled; recovery requires working GitHub/GCP APIs and configured credentials. This improves crash recovery without claiming instantaneous or guaranteed availability.

## Upgrade steps

1. Publish this updated platform source and record its full commit SHA.
2. Review and apply the Terraform change: a private versioned recovery bucket, its scoped IAM grant, and the additional recovery workflow in the WIF condition.
3. Set the new `RECOVERY_BUCKET` GitHub staging environment variable from Terraform output.
4. Add each app's `target` block to the controller configuration; values must match the GitHub staging environment exactly.
5. Regenerate and review **both** `deploy-gcp.yml` and `recover-gcp.yml` using the new platform commit. The render command refuses overwrite: write temporary output files and review their diff before replacing existing workflow files.
6. Restart the controller and create fresh plans. Older workflow templates do not accept the new `target_json` dispatch input.
7. Verify a normal release, failed candidate, post-promotion failure, and killed-runner recovery in disposable staging.

## Still outside this patch

Telemetry-driven resource optimization, dollar-cost estimates, AWS support and production readiness are features/integration milestones, not silently completed by these bug fixes. The analyzer still reports conservative defaults and review assumptions. Full live cloud testing remains required.
