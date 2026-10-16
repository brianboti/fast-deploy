"""Conservative static inspection; never imports or executes repository code."""
import re
from pathlib import Path
from typing import get_args

import yaml

from .models import AppSpec

FILES = ("requirements.txt", "pyproject.toml", "Dockerfile", "package.json", "platform.yaml")
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
PYTHONS = get_args(AppSpec.model_fields["python"].annotation)
# Package managers whose files the platform does not read; each exports the pip files it does (DEVELOPER-CONTRACT 1a).
OTHER_MANAGERS = (("environment.yml", "conda"), ("environment.yaml", "conda"), ("poetry.lock", "Poetry"), ("uv.lock", "uv"))
TEST_PATH = re.compile(r"(^|/)tests?(/|$)|(^|/)test_[^/]+\.py$|_test\.py$")
# Decision D16: needs are suggested in the review questions from the dependencies, never added to the draft silently.
NEEDS_HINTS = (
    ("database: postgres", r"psycopg|asyncpg|pg8000|cloud-sql-python-connector"),
    ("database: firestore", r"google-cloud-firestore"),
    ("buckets: [NAME] (arrives as BUCKET_NAME)", r"google-cloud-storage"),
    ("topics: [NAME] (arrives as TOPIC_NAME and SUBSCRIPTION_NAME)", r"google-cloud-pubsub"),
    ("migrate: [alembic, upgrade, head]", r"\balembic\b"),
)
UNPROVIDED = (("redis", r"\bredis\b"), ("mysql", r"pymysql|mysqlclient"))
# The component rule (docs/DEVELOPER-CONTRACT.md, "One repository, several services"): where each part of an app goes.
CACHE = ("No cache is provided by default. Keep cached data in process memory (per instance, lost on restart), or in a Firestore "
         "collection when it must be shared; Memorystore only with the operator's approval: about 36 to 47 dollars a month, and it "
         "never scales to zero.")
WORKER = ("Background work runs as a Cloud Run job (needs.migrate for a one-off step before release) or arrives through Pub/Sub "
          "push to a request-billed service (a topic need); a worker that polls forever needs an always-on instance, which the "
          "platform does not run.")
FRONT_END = ("A front end: build it and serve the built files from the API's own container (for example FastAPI's StaticFiles) "
             "behind the same sign-in, with exposure: lab-members for people; a separate public site is not offered.")
WORKERS = r"\bcelery\b|\brq\b|\bdramatiq\b|\bhuey\b|\barq\b|\bapscheduler\b"
URL_DRIVERS = r"psycopg|asyncpg|sqlalchemy|databases|tortoise-orm|peewee"
CONNECTOR = r"cloud-sql-python-connector"


def analyze(files: dict[str, str], name: str, paths: list[str] | None = None) -> dict:
    """`paths`, the repository's file list when known, lets the checks see tests and a lock file without reading them."""
    deps = files.get("requirements.txt", "") + "\n" + files.get("pyproject.toml", "")
    python = bool(deps.strip())
    evidence = []
    if python:
        evidence.append("Python dependency manifest found")
    for framework in ("fastapi", "flask", "django"):
        if re.search(rf"\b{framework}\b", deps, re.I):
            evidence.append(f"{framework} appears in dependencies")
    ports = re.findall(r"^\s*EXPOSE\s+(\d+)(?:/tcp)?\s*$", files.get("Dockerfile", ""), re.M)
    port = int(ports[0]) if len(ports) == 1 and 1024 <= int(ports[0]) <= 65535 else 8080
    dependencies = [label for label, pattern in UNPROVIDED if re.search(pattern, deps, re.I)]
    for label in dependencies:
        evidence.append(f"{label} appears in dependencies: the platform does not provide it")
    suggested = [need for need, pattern in NEEDS_HINTS if re.search(pattern, deps, re.I)]
    requirements, dockerfile = files.get("requirements.txt", ""), files.get("Dockerfile", "")
    locked = paths is not None and "requirements.lock" in paths
    if requirements.strip() and "--hash=" not in requirements:
        if locked and "--require-hashes" in dockerfile and "requirements.lock" in dockerfile:
            evidence.append("requirements.lock is installed with --require-hashes: builds are reproducible")
        elif locked:
            evidence.append("requirements.lock exists but the Dockerfile does not install it with --require-hashes; "
                            "install it that way so the image matches the audited versions.")
        else:
            evidence.append("requirements.txt pins without hashes: builds are not reproducible. Add a requirements.lock from "
                            "'pip-compile --generate-hashes' and install it with --require-hashes in the Dockerfile.")
    base = re.search(r"^\s*FROM\s+(?:docker\.io/)?(?:library/)?python:(3\.\d+)", dockerfile, re.M | re.I)
    version = base.group(1) if base else None
    if version in PYTHONS and version != "3.12":
        evidence.append(f"The Dockerfile builds on Python {version}; the draft sets python: \"{version}\" so the tests and the "
                        "dependency audit run on the same version")
    unsupported = version is not None and version not in PYTHONS
    managers = sorted({manager for filename, manager in OTHER_MANAGERS if filename in (paths or ())})
    tests = [path for path in paths or () if TEST_PATH.search(path) and path.endswith(".py")]
    if tests:
        evidence.append(f"{len(tests)} test file{'' if len(tests) == 1 else 's'} found; they run before every build")
    driver = re.search(URL_DRIVERS, deps, re.I)
    if driver and not re.search(CONNECTOR, deps, re.I):
        evidence.append(f"{driver.group(0)} without the Cloud SQL connector: the code likely connects with a DATABASE_URL; with "
                        "database: postgres, add database_auth: password and the URL arrives as a secret")
    workers = re.search(WORKERS, deps, re.I)
    if workers:
        evidence.append(f"{workers.group(0)} appears in dependencies: a background worker")
    front_end = paths is not None and any(path == "package.json" or path.endswith("/package.json") for path in paths) or bool(files.get("package.json"))
    sync_framework = any(re.search(rf"\b{framework}\b", deps, re.I) for framework in ("flask", "django"))
    if sync_framework:
        evidence.append("Synchronous framework hint: draft concurrency lowered to 8 per instance")
    draft = AppSpec(name=name, port=port, dependencies=dependencies, concurrency=8 if sync_framework else 80,
                    python=version if version in PYTHONS else "3.12")
    review = [
        "Confirm stateless HTTP service; source files cannot prove this.",
        "Confirm port and health endpoint; EXPOSE is only a hint.",
        "CPU/RAM/scaling are conservative defaults, not measured requirements.",
        "Confirm all external services, secrets, data classification, and access needs.",
        "Dependency matches are hints; unknown dependencies require review.",
        "Confirm concurrency: it must not exceed what the server's workers or threads can serve in parallel; higher values need fewer instances and cost less.",
    ]
    if suggested:
        review.append("The dependencies suggest these needs; add the ones the app really uses under `needs` (nothing was added): "
                      + "; ".join(suggested) + ".")
    if not tests:
        review.append("Add pytest tests, or declare tests: none knowingly; every plan for an app without tests carries a warning.")
    if unsupported:
        review.append(f"The Dockerfile builds on Python {version}, which the platform does not test or audit on (supported: "
                      f"{', '.join(PYTHONS)}); move the image to a supported version.")
    if "redis" in dependencies:
        review.append("Redis: " + CACHE)
    if workers:
        review.append(WORKER)
    if front_end:
        review.append(FRONT_END)
    if managers and not requirements.strip():
        review.append(f"{' and '.join(managers)} files found but no requirements.txt: the platform installs with pip. Export "
                      "requirements.txt, requirements.lock and requirements-dev.txt as docs/DEVELOPER-CONTRACT.md section 1a shows.")
    review.append("List configuration as env (plain values) and secrets (names of Secret Manager secrets called <service>-NAME); "
                  "nothing else reaches the container.")
    result = {"supported_candidate": python, "evidence": evidence, "draft_spec": draft.model_dump(),
              "suggested_needs": suggested, "review_required": review}
    if files.get("compose"):
        result["compose"] = compose_drafts(files["compose"])
    return result


IMAGE_KINDS = (
    ("database", r"postgres|postgis|timescale", "Postgres is provided as needs.database: postgres (IAM sign-in, or database_auth: password "
                                                 "when the code uses a DATABASE_URL); the platform creates and backs it up."),
    ("database", r"mysql|mariadb", "MySQL is not provided; the platform's SQL database is Postgres (needs.database: postgres)."),
    ("database", r"mongo", "MongoDB is not provided; the platform's document database is Firestore (needs.database: firestore)."),
    ("cache", r"redis|valkey|keydb|memcached|dragonfly", CACHE),
    ("queue", r"rabbitmq|kafka|nats|redpanda|activemq", "Messages go through Pub/Sub: declare needs.topics; each topic arrives with a "
                                                        "subscription, and a push subscription can call a service."),
    ("proxy", r"nginx|traefik|caddy|envoy|haproxy", "No proxy is needed: Cloud Run terminates TLS and routes to the service, and "
                                                    "Identity-Aware Proxy gives people a sign-in (exposure: lab-members)."),
    ("storage", r"minio|localstack|azurite|fake-gcs", "Files go to declared buckets (needs.buckets); each arrives as BUCKET_<NAME>."),
)


def folder(text):
    """A Compose path as the platform writes a service's folder: relative, no ./ prefix or trailing slash, '' for the root."""
    value = Path(str(text).strip()).as_posix().removeprefix("./").rstrip("/")
    return "" if value in ("", ".") else value


def compose_port(ports):
    """The container port of the first published port, from a Compose `ports` list in any of its forms."""
    for item in ports or []:
        if isinstance(item, dict) and str(item.get("target", "")).isdigit():
            return int(item["target"])
        text = str(item).split("/")[0]
        container = text.rsplit(":", 1)[-1]
        if container.isdigit():
            return int(container)
    return None


def compose_names(value):
    """The names in a Compose `depends_on` or `environment`, from either form: a list, or a mapping."""
    if isinstance(value, dict):
        return [str(key) for key in value]
    if isinstance(value, list):
        return [str(item).split("=", 1)[0] for item in value]
    return []


def compose_needs(body, kinds):
    """What a built service's Compose entry says it needs from the platform: Postgres when it depends on a Postgres service,
    by password when it reads a DATABASE_URL (the platform's password sign-in delivers exactly that variable)."""
    if not any(kinds.get(name) == "postgres" for name in compose_names(body.get("depends_on"))):
        return {}
    needs = {"database": "postgres"}
    if "DATABASE_URL" in compose_names(body.get("environment")):
        needs["database_auth"] = "password"
    return needs


def compose_drafts(text, limit=20):
    """A Compose file read as a description of the app's parts: one platform.yaml draft per HTTP service it builds, and the
    component rule for everything else. Compose is never the deployment unit; nothing here is deployed."""
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return {"error": f"The Compose file is not valid YAML: {str(exc)[:200]}"}
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return {"error": "The Compose file has no services"}
    kinds = {str(name): "postgres" for name, body in services.items()
             if isinstance(body, dict) and re.search(IMAGE_KINDS[0][1], str(body.get("image") or ""), re.I)}
    parts = []
    for service_name, body in list(services.items())[:limit]:
        body = body if isinstance(body, dict) else {}
        build = body.get("build")
        context = build if isinstance(build, str) else (build or {}).get("context") if isinstance(build, dict) else None
        image = str(body.get("image") or "")
        port = compose_port(body.get("ports")) or compose_port(body.get("expose"))
        name = re.sub(r"[^a-z0-9-]+", "-", str(service_name).lower()).strip("-")[:21] or "service"
        if not name[0].isalpha():
            name = f"svc-{name}"[:21]
        part = {"service": service_name}
        if context is not None:
            path = folder(context)
            dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
            build_context = "service"
            if dockerfile and "/" in str(dockerfile).strip("./"):
                own = folder(f"{context}/{Path(str(dockerfile)).parent.as_posix()}")
                if own != path:
                    if path:
                        part["note"] = ("The Dockerfile is outside its build context's folder; the platform builds from the service's folder "
                                        "or from the repository's root (build.context: repository)")
                    path, build_context = own, "repository"
            part["path"] = path
            if port:
                needs = compose_needs(body, kinds)
                draft = AppSpec(name=name if len(name) >= 3 else f"{name}-svc", port=port if 1024 <= port <= 65535 else 8080,
                                build={"context": build_context}, needs=needs)
                part.update(kind="http", port=port, draft_spec=draft.model_dump(),
                            advice=f"Its own Cloud Run service: onboard it with path {path or '(the root)'}; this draft needs review like any other.")
                if needs:
                    part["needs_from"] = ("It depends on the Compose file's Postgres service" + (" and reads DATABASE_URL" if "database_auth" in needs else "")
                                          + ": the draft asks for the platform's Postgres instead; add needs.migrate if the schema is created by a command.")
            else:
                part.update(kind="worker", advice=WORKER)
        else:
            kind, advice = "other", "Not provided by the platform; say what it does and the operator decides."
            for candidate, pattern, text_ in IMAGE_KINDS:
                if re.search(pattern, image, re.I):
                    kind, advice = candidate, text_
                    break
            part.update(kind=kind, image=image, advice=advice)
        parts.append(part)
    return {"parts": parts, "note": "Read to draft platform.yaml files and to say where each part goes; a Compose file is never deployed as such "
                                     "(each web service becomes its own Cloud Run service, data goes to the platform's managed services)."}


def local_paths(root: Path, limit: int = 5000) -> list[str]:
    """The files under a local application directory, as a repository tree would list them (no virtualenvs or caches)."""
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
    found = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if skip.intersection(relative.parts) or path.is_symlink() or not path.is_file():
            continue
        found.append(relative.as_posix())
        if len(found) >= limit:
            break
    return found


def analyze_local(path: Path, name: str) -> dict:
    root = path.resolve(strict=True)
    files = {}
    for filename in FILES + COMPOSE_FILES:
        target = root / filename
        if target.is_symlink():
            raise ValueError("Symlinks are not accepted in analyzer inputs")
        if target.is_file():
            if target.stat().st_size > 65_536:
                raise ValueError(f"{filename} exceeds 64 KiB")
            if filename in COMPOSE_FILES:
                files.setdefault("compose", target.read_text())
            else:
                files[filename] = target.read_text()
    return analyze(files, name, local_paths(root))

