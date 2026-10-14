"""Conservative static inspection; never imports or executes repository code."""
import re
from pathlib import Path

from .models import AppSpec

FILES = ("requirements.txt", "pyproject.toml", "Dockerfile", "package.json", "platform.yaml")
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
    if base and base.group(1) != "3.12":
        evidence.append(f"The Dockerfile builds on Python {base.group(1)}; set python: \"{base.group(1)}\" in platform.yaml so the "
                        "tests run on the same version (supported: 3.11, 3.12, 3.13)")
    tests = [path for path in paths or () if TEST_PATH.search(path) and path.endswith(".py")]
    if tests:
        evidence.append(f"{len(tests)} test file{'' if len(tests) == 1 else 's'} found; they run before every build")
    sync_framework = any(re.search(rf"\b{framework}\b", deps, re.I) for framework in ("flask", "django"))
    if sync_framework:
        evidence.append("Synchronous framework hint: draft concurrency lowered to 8 per instance")
    draft = AppSpec(name=name, port=port, dependencies=dependencies, concurrency=8 if sync_framework else 80)
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
    review.append("List configuration as env (plain values) and secrets (names of Secret Manager secrets called <service>-NAME); "
                  "nothing else reaches the container.")
    return {"supported_candidate": python, "evidence": evidence, "draft_spec": draft.model_dump(),
            "suggested_needs": suggested, "review_required": review}


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
    for filename in FILES:
        target = root / filename
        if target.is_symlink():
            raise ValueError("Symlinks are not accepted in analyzer inputs")
        if target.is_file():
            if target.stat().st_size > 65_536:
                raise ValueError(f"{filename} exceeds 64 KiB")
            files[filename] = target.read_text()
    return analyze(files, name, local_paths(root))

