# Infrastructure layer

Two small services that let a company's developers, and the AI agents working for them, get cloud infrastructure and
run containers on it without ever holding a cloud credential or writing infrastructure code:

- **`cloudinfra`, the Cloud Infrastructure MCP.** A provider-independent service that provisions, grants access to,
  stops, starts, prices, expires and destroys infrastructure from a typed request: machines (in groups, with or without
  GPUs), disks and buckets, networks and load balancers, managed databases, caches, queues, secrets, image registries,
  Kubernetes clusters with a front door, and managed container services and jobs. Twelve kinds, 54 MCP tools, a REST
  API, one Google Cloud adapter today.
- **`appdelivery`, the application delivery service.** A separate service above it that runs an image that already
  exists, named by digest, on what a `cloudinfra` request created: a group of machines, a cluster, a managed container
  service or a job. Health checks, rollback, approval and attestation per environment. Eleven tools.

```
Application teams, Claude, CI        code · build · image in a registry · the request for infrastructure
        │  image@sha256 + request_id + port + env bindings           │  InfrastructureRequest
        ▼                                                            │
appdelivery   verify · bind · run · health · roll back · record      │
        │  reads the request over cloudinfra's REST API              │
        ▼                                                            ▼
cloudinfra    validate → plan → price → provision → grant access → stop/start → expire → destroy
        │  CloudProvider
        ▼
Google Cloud  (AWS, Azure: adapters to come; nothing above changes)
```

The infrastructure service takes no repository, image, script, command or key, holds no credential in any file, and
grants access only on what it created, never on the project. The delivery service takes no source, Dockerfile or
command, creates no infrastructure, reaches machines only through Identity-Aware Proxy as an environment's own service
account, and is itself only a read-only caller of `cloudinfra`. Tests hold both lines.

## Documentation

| Read | For |
|---|---|
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Every requirement with its version: Python and packages, the images' pinned inputs, CI tools, what a host and the Google Cloud project need, product versions, limits, clients |
| [docs/SETUP.md](docs/SETUP.md) | Setting both services up on a Google Cloud project, the first request and the first release, connecting Claude |
| [docs/INFRASTRUCTURE-ARCHITECTURE.md](docs/INFRASTRUCTURE-ARCHITECTURE.md) | The infrastructure service: ownership, the contract, the lifecycle, access, time, state, security, observability |
| [docs/MCP-CATALOG.md](docs/MCP-CATALOG.md) | The infrastructure service's 54 tools, their fields, outputs, provider mappings and error codes (generated) |
| [docs/PROVIDER-INTERFACE.md](docs/PROVIDER-INTERFACE.md) | The `CloudProvider` interface, the Google adapter's mapping, and how to add a provider |
| [docs/DELIVERY-SPEC.md](docs/DELIVERY-SPEC.md) | The delivery service: who trusts whom, the release contract, the four targets, a release's life, boundaries |
| [docs/DELIVERY-CATALOG.md](docs/DELIVERY-CATALOG.md) | The delivery service's 11 tools and error codes (generated) |
| [docs/DELIVERY-SPIKE.md](docs/DELIVERY-SPIKE.md) | What each real run on Google Cloud proved and found, machines to Cloud Run |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Conventions: layout, tests, boundary tests, the generated catalogs, lock files, versions, releases |
| [docs/CHANGES.md](docs/CHANGES.md) | What each version changed |
| [docs/research/](docs/research/) | The reviews behind the design: the split into layers, whether the layer is built right, where it stands, the delivery service's shape |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability, and the security design in short |

## Try it without a cloud

Python 3.12 or newer.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
```

```bash
python -m pip install --require-hashes -r requirements-dev.lock
```

```bash
python -m pytest -q
```

```bash
python -m cloudinfra.cli validate examples/infrastructure-request.yaml
```

```bash
python -m cloudinfra.cli plan examples/infrastructure-request.yaml
```

The plan lists the resources the request would create, in order, with the provider operation and shape of each, the
monthly estimate from list prices, and the warnings; nothing is called. `examples/release.yaml` is a release for the
same request's web tier.

## Run the infrastructure service

```bash
python -m cloudinfra.cli token
```

```bash
cp cloudinfra.example.yaml cloudinfra.local.yaml
```

Put the token's hash under `principals`, the project under `providers.gcp`, and the company's domains under
`access.allowed_domains`. No credential goes in the file: on Google Cloud the service uses the machine's service
account; on a laptop it uses your gcloud sign-in (see [docs/SETUP.md](docs/SETUP.md) for the roles and the quota
project).

```bash
CLOUDINFRA_CONFIG=cloudinfra.local.yaml uvicorn cloudinfra.mcp.server:create_app --factory --host 127.0.0.1 --port 8100
```

The MCP endpoint is `/mcp`; `GET /healthz` is public; `GET /catalog` lists the tools. A first request:

```bash
curl -s -X POST "http://127.0.0.1:8100/infrastructure?wait_seconds=45" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data @request.json
```

Its image: `docker build -f Dockerfile.cloudinfra -t cloudinfra .`

## Run the delivery service

```bash
app-delivery token
```

```bash
cp appdelivery.example.yaml appdelivery.local.yaml
```

Each environment names its service account, the variable holding its `cloudinfra` token, the registries images may come
from, and whether releases need an attestation or an approval. Teams name the environment's service account at `admin`
in their request's `access` list; that grant is their consent.

```bash
app-delivery plan examples/release.yaml --config appdelivery.local.yaml --as shop-team
```

```bash
APPDELIVERY_CONFIG=appdelivery.local.yaml uvicorn appdelivery.server:create_app --factory --host 127.0.0.1 --port 8200
```

Its image, with the gcloud CLI and Cosign pinned and checked: `docker build -f Dockerfile.appdelivery -t appdelivery .`

## Connect Claude

Claude chat (claude.ai) signs in to a connector with OAuth only, so each service can sign a person in itself: serve it
on a public HTTPS address, add that host to `allowed_hosts`, set `sign_in: {url: https://HOST}`, and add a custom
connector on `https://HOST/mcp`. The person pastes their principal's token once on the service's own page, never in the
chat, and Claude then acts as that principal. Claude Code can do the same, or keep a bearer header:

```bash
claude mcp add --transport http cloud-infra https://HOST/mcp --header "Authorization: Bearer $TOKEN"
```

## Status

Built between 2026-10-28 and 2026-10-30. Every feature ran against the unit tests' fake Google Cloud, and the following
ran on a real project (recorded in [docs/DELIVERY-SPIKE.md](docs/DELIVERY-SPIKE.md)): machines with access grants, stop
and start; the delivery service from its own image on Container-Optimized OS, releasing to a group of machines with
approval, a failed release restored, rollback and remove; a release on a GKE cluster; two clusters with their own node
accounts, a private registry and a front door reached from the internet over plain HTTP; Cloud Run services and a
scheduled job. Not yet run for real: a front door with HTTPS on a real domain, removing a job's release, release A's
expiry and idle stop against a live project, and a second cloud provider.

## Layout

```
cloudinfra/        the infrastructure service: models, orchestration, providers/gcp, mcp, security, state
appdelivery/       the delivery service: models, planner, releases, machine, kube, run, server, host/release.sh
tests/             cloudinfra/ and appdelivery/ (fakes, no cloud); integration/ (a real project, opt in)
docs/              the documents above; research/ holds the reviews
examples/          one infrastructure request and one release, validated by CI
Dockerfile.*       one image per service
*.example.yaml     one configuration template per service
```

## License

MIT (`LICENSE`).
