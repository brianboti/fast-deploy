# Development

How the repository is laid out, what the tests hold, and the steps every change goes through.

## Layout

```
cloudinfra/                 the infrastructure service (version in __init__.py, tied to pyproject.toml by a test)
  models/                   the contract: request, resources, response, errors (pydantic, strict)
  orchestration/            validator → planner → provisioner; access, lifecycle, sweeper
  providers/base.py         the CloudProvider interface;  providers/gcp/  the Google adapter, one module per kind
  mcp/                      the FastAPI app, the MCP server, the catalog (one record per tool), tools/
  security/                 principals and token hashes, request value validation, the Claude chat sign-in (OAuth)
  state/                    SQLite
  observability/            JSON events, redaction
appdelivery/                the delivery service (its own version in __init__.py)
  models.py planner.py      the release contract and the checks against cloudinfra's answer
  releases.py state.py      the orchestrator and its SQLite state
  machine.py host/release.sh  a release on a machine: the host script, sent over gcloud ssh through IAP
  kube.py run.py            a release on a cluster, and on a Cloud Run service or job
  infra.py google.py        the cloudinfra HTTP client; Google tokens and Secret Manager reads
  remote.py                 the only module that runs a command (gcloud, cosign), with fixed argument lists
  server.py auth.py catalog.py tools.py oauth.py
tests/cloudinfra/           test_infra_*.py with gcp_fakes.py (a fake Google Cloud and Kubernetes); no conftest
tests/appdelivery/          test_delivery_*.py with its own fakes (delivery_fakes, fleet_fakes, host_fakes, kube_fakes, run_fakes); no conftest
tests/integration/          a real project, marked integration, skipped unless CLOUDINFRA_INTEGRATION=1
examples/                   one request and one release, validated by CI and a test
docs/                       MCP-CATALOG.md and DELIVERY-CATALOG.md are generated; research/ holds the reviews
```

## Rules the tests hold

- **The infrastructure service never imports the delivery service, GitHub, Docker or any application code**; a
  command runs only in `providers/gcp/auth.py`; Google Cloud is named only under `providers/gcp/` and in the catalog's
  mapping column; the catalog has no tool named after deploying, building, repositories or images; the contract has
  no field that could carry a script, command, key or image (`tests/cloudinfra/test_infra_catalog.py`,
  `test_infra_models.py`).
- **The delivery service imports neither `cloudinfra` nor anything of the removed workflow**; it speaks to
  `cloudinfra` only over HTTP; a command runs only in `remote.py`; the release contract has no field that could run,
  build or mount anything; its tests use no shared helper and no conftest (`tests/appdelivery/test_delivery_boundaries.py`).
- **Secret values leave no trace**: a test provisions a database and reads the state file and the log; another
  releases with a secret binding and a failed target and reads the delivery service's state, log and every answer.
- **The documents match the code**: the two catalogs are compared byte for byte with what the code renders; the
  `Dockerfile.appdelivery` pins are checked for shape and the Python digest for equality with `Dockerfile.cloudinfra`;
  the example files must fit the contracts.

## Before finishing a change

```bash
python -m ruff check .
```

```bash
python -m pytest -q
```

```bash
python -m cloudinfra.cli catalog --write docs/MCP-CATALOG.md
```

```bash
app-delivery catalog --write docs/DELIVERY-CATALOG.md
```

Then a `docs/CHANGES.md` entry (what changed for a caller or an operator, and what to do when upgrading), and the
documents that describe the changed part: `docs/INFRASTRUCTURE-ARCHITECTURE.md` and `docs/PROVIDER-INTERFACE.md` for the
infrastructure service, `docs/DELIVERY-SPEC.md` for the delivery service, `docs/REQUIREMENTS.md` for any new version,
role, API, limit or tool, `docs/SETUP.md` for a new operator step.

## Conventions

- Python 3.12, ruff with line length 200 and the bandit rules; every subprocess call is a fixed argument list.
- New infrastructure tests go in `tests/cloudinfra/` as `test_infra_<topic>.py`, using and extending `gcp_fakes.py`;
  new delivery tests in `tests/appdelivery/` as `test_delivery_<topic>.py` with the fakes there. Neither directory
  takes a `conftest.py`.
- A real-cloud test goes in `tests/integration/`, marked `integration`, creates the smallest thing that proves the
  point and destroys it. Every run on a real project is written up in `docs/DELIVERY-SPIKE.md` with what it found.
- Adding a kind to the infrastructure service: the request and resource models, the validator and planner rules, the
  provider method on `CloudProvider` and in the Google adapter (one module), the id prefix, the grants it takes in
  `providers/gcp/iam.py`, pricing, the connect block, three tools and their catalog records, the fake, tests, the
  catalog document, and the architecture document's contract section.
- Adding a provider: [PROVIDER-INTERFACE.md](PROVIDER-INTERFACE.md), "Adding a provider".
- Adding a delivery target: a `Target` field and its checks in `planner.py`, a deployer module like `kube.py` or
  `run.py`, its fake, the rollout, rollback and remove paths in `releases.py`, tests, the spec's section 4.

## Dependencies and pins

- Direct dependencies are pinned exactly in `pyproject.toml`; the hash locks are regenerated with
  `uv pip compile --generate-hashes pyproject.toml -o requirements.lock` and
  `uv pip compile --generate-hashes pyproject.toml --extra dev -o requirements-dev.lock`;
  `audit.lock` from `audit.in` and `pip.lock` from `pip.in` the same way (with `--upgrade --refresh` to move past a
  cached index). Run the tests and `python -m pip_audit --require-hashes -r <lock>` on every lock after.
- The Python image digest is the same in both Dockerfiles; the gcloud and Cosign pins in `Dockerfile.appdelivery` are
  moved as its header describes. CI's hadolint and the actions are pinned by commit or checksum in
  `.github/workflows/ci.yml`; Dependabot proposes action updates weekly.
- [REQUIREMENTS.md](REQUIREMENTS.md) lists every pin with its source; update it in the same change.

## Versions and releases

- `pyproject.toml` and `cloudinfra/__init__.py` carry the repository's version (a test ties them);
  `appdelivery/__init__.py` carries the delivery service's own.
- A release is a version bump, the `CHANGES.md` heading for it, green CI, and a pull request on `main` (branch
  protection requires one). The repository's settings are expected to hold what the security assessment of 2026-10-30
  asked for: Dependabot alerts on, and branch protection on `main` with one approving review, code-owner review,
  status checks on the merged base (`strict`) and conversation resolution; checked with
  `gh api repos/{owner}/{repo}/branches/main/protection` and `gh api repos/{owner}/{repo}/vulnerability-alerts`. Stacked pull requests are retargeted to `main` before each is merged, or a squash merge
  lands in the stack's base instead of `main` (seen twice on 2026-10-29 and 2026-10-30).
