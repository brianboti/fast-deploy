"""Conservative static inspection; never imports or executes repository code."""
import re
from pathlib import Path

from .models import AppSpec

FILES = ("requirements.txt", "pyproject.toml", "Dockerfile", "package.json", "platform.yaml")


def analyze(files: dict[str, str], name: str) -> dict:
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
    dependencies = []
    for label, pattern in (("postgres", r"psycopg|asyncpg"), ("redis", r"\bredis\b"), ("mysql", r"pymysql|mysqlclient")):
        if re.search(pattern, deps, re.I):
            dependencies.append(label)
    requirements = files.get("requirements.txt", "")
    if requirements.strip() and "--hash=" not in requirements:
        evidence.append("requirements.txt pins without hashes: builds are not reproducible. Add a requirements.lock from "
                        "'pip-compile --generate-hashes' and install it with --require-hashes in the Dockerfile.")
    sync_framework = any(re.search(rf"\b{framework}\b", deps, re.I) for framework in ("flask", "django"))
    if sync_framework:
        evidence.append("Synchronous framework hint: draft concurrency lowered to 8 per instance")
    draft = AppSpec(name=name, port=port, dependencies=dependencies, concurrency=8 if sync_framework else 80)
    return {
        "supported_candidate": python,
        "evidence": evidence,
        "draft_spec": draft.model_dump(),
        "review_required": [
            "Confirm stateless HTTP service; source files cannot prove this.",
            "Confirm port and health endpoint; EXPOSE is only a hint.",
            "CPU/RAM/scaling are conservative defaults, not measured requirements.",
            "Confirm all external services, secrets, data classification, and access needs.",
            "Dependency matches are hints; unknown dependencies require review.",
            "Confirm concurrency: it must not exceed what the server's workers or threads can serve in parallel; higher values need fewer instances and cost less.",
            "Add pytest tests, or declare tests: none knowingly; every plan for an app without tests carries a warning.",
            "List configuration as env (plain values) and secrets (names of Secret Manager secrets called <service>-NAME); nothing else reaches the container.",
        ],
    }


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
    return analyze(files, name)

