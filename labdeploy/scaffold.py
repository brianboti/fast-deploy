"""The platform writes the boilerplate (Phase 11, decisions D26 and D27).

From a repository's file list and the text of a few of its files, `draft_files` drafts what docs/DEVELOPER-CONTRACT.md
asks for and the service lacks: a Dockerfile, .dockerignore, requirements-dev.txt, a health test, a platform.yaml draft
with reviewed: false, CI, SECURITY.md and Dependabot, each with a one-line purpose for the pull request, plus review notes
for what it could not decide. Conservative like the analyzer: repository code is read as text, never imported or run,
and a file that already exists is never returned. The one side effect is `compile_lock`, which runs uv in a container of
the application's own Python through an injected runner (D27).

The file templates live in templates/repo/ with __NAME__ placeholders, filled by plain string replacement.
"""
import json
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import get_args

import yaml

from .analyzer import TEST_PATH, analyze
from .models import AppSpec, draft_dump

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "repo"
PYTHONS = get_args(AppSpec.model_fields["python"].annotation)
DEFAULT_PYTHON = AppSpec.model_fields["python"].default
DEFAULT_PORT = AppSpec.model_fields["port"].default
DEFAULT_HEALTH = AppSpec.model_fields["health_path"].default

# python:X.Y-slim multi-architecture index digests, each with the Python release and the Debian it carries, resolved on the
# date below: the index every architecture shares (`docker buildx imagetools inspect python:X.Y-slim`, the top "Digest:"
# line), never a per-platform manifest. The weekly pin refresh (labdeploy/pins.py, .github/workflows/pins.yml) proposes a
# newer index once Docker Hub's tag has pointed to it for seven days, and dates the change. A repository keeps its own pin
# once drafted; there, Dependabot's docker updates refresh it.
BASE_IMAGES_RESOLVED = "2026-10-14"
BASE_IMAGES = {
    "3.11": "sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce",  # 3.11.17, Debian trixie
    "3.12": "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",  # 3.12.15, Debian trixie
    "3.13": "sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f",  # 3.13.16, Debian trixie
    "3.14": "sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170",  # 3.14.8, Debian trixie
    "3.15": "sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619",  # 3.15.0, Debian trixie
}
# node:<line>-trixie-slim multi-architecture index digests, each with the Node.js release and the Debian it carries, resolved
# and refreshed like BASE_IMAGES; a drafted repository's own pin is Dependabot's to refresh. Debian 13 for every line: the
# default node:22-slim and node:24-slim are Debian 12, whose perl carried five critical findings with a fix on 2026-10-19
# that the image had not taken, so code nobody has reviewed could not be released on them.
NODE_IMAGES_RESOLVED = "2026-10-19"
NODE_IMAGES = {
    "22": "sha256:154ba2f4d6fec323d28e4f4bb86bba4677f1223391a1979cf521304e03a98dfa",  # 22.23.3, Debian trixie
    "24": "sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315",  # 24.22.0, Debian trixie
    "26": "sha256:0d1e33586362bb9e608c4e751e7a9e7edb55c9ac11461ed5666af696cc461dd4",  # 26.12.0, Debian trixie
}
# The npm that makes a Node.js service's lock (docs/ROADMAP.md D42, 18.3): min-release-age arrived in npm 11.10.0
# (2026-02-11), and Node 22 bundles npm 10.9.9, which only warns about a flag it does not know and would lock without the
# hold. In the node:22-trixie-slim container a newer npm 11 is therefore installed by version and checked against the integrity
# hash the registry published for it. The weekly pin refresh keeps it on the newest npm 11 at least seven days old that
# asks for the same Node.js versions (22.9 and later) and runs no install script. The lock is still resolved on the
# service's own line, as npm prefers versions whose engines it meets. Every container refuses an npm older than 11.10
# rather than lock without the hold.
LOCK_NPM = {"22": ("11.19.1", "sha512-ztsxKxt/kkIaAs+2i0GU6I+DRmUdrNasxTZKJe9TCdSjKxlhah/4r/hl5ygMD6XAg1qZ9c2TNomR4qgOydp10g==")}  # published 2026-08-26
NODE_LOCK_OPTIONS = "--package-lock-only --ignore-scripts --min-release-age=3"
# Exact versions checked on PyPI on 2026-10-14 (the demo app and the platform pin the same test tools).
DEV_PINS = {"pytest": "9.1.1", "httpx": "0.28.1"}
SERVER_PINS = {"uvicorn": "0.54.0", "gunicorn": "26.2.0"}
# uv for compile_lock and export_uv_lock, installed into the container only by these hashes, as PyPI publishes them: its
# manylinux wheels for x86_64 and aarch64 (Docker on an Apple silicon Mac runs aarch64 Linux). The weekly pin refresh keeps
# it on the newest 0.12 release at least seven days old: uv's minor versions are its breaking ones, and 0.13 changed archive
# handling, so a newer line is a deliberate move (its `line` in labdeploy/pins.py).
UV_VERSION = "0.12.24"  # released 2026-10-08
UV_HASHES = (
    "e47005957dca320272ad68960b591b854b6bf0c3288368efb096d9ccd06b8404",  # manylinux_2_17_x86_64
    "94c9ed22c1b5d6861a72639e23c5707bb07a354b67ec0462e9a0169a9665b5fd",  # manylinux_2_17_aarch64
    "0fc14fabb78c55d140a01125d5cd038afb4fed317eb0c3868f3fba87b8cefe16",  # manylinux_2_28_aarch64
)
# The throwaway lock container's only writable directory for its non-root user.
CONTAINER_HOME = "/tmp"  # noqa: S108 - inside a disposable container, never on the controller's disk

SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]{1,19}[a-z0-9]")  # AppSpec.name
SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")
WORKFLOW = re.compile(r"\.github/workflows/[^/]+\.ya?ml")
PLATFORM_WORKFLOW = re.compile(r"\.github/workflows/(?:deploy|recover|rightsize)-gcp(?:-[a-z0-9-]+)?\.yml")

# A module-level `NAME = Constructor(...)` is the application object: (framework, interface, constructor).
CONSTRUCTORS = (
    ("fastapi", "asgi", r"(?:fastapi\.)?FastAPI"),
    ("starlette", "asgi", r"(?:starlette\.applications\.)?Starlette"),
    ("quart", "asgi", r"(?:quart\.)?Quart"),
    ("litestar", "asgi", r"(?:litestar\.)?Litestar"),
    ("falcon", "asgi", r"(?:falcon\.)?asgi\.App"),
    ("flask", "wsgi", r"(?:flask\.)?Flask"),
    ("bottle", "wsgi", r"(?:bottle\.)?Bottle"),
    ("falcon", "wsgi", r"falcon\.(?:App|API)"),
)
CONSTRUCTOR_CALL = re.compile(r"(?<![\w.])(" + "|".join(pattern for _, _, pattern in CONSTRUCTORS) + r")\s*\(")
ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]*)?=\s*([A-Za-z_][\w.]*)\s*\(", re.M)
FACTORY_NAMES = ("create_app", "make_app", "get_app", "build_app", "app_factory")
FACTORY_DEF = re.compile(r"^def\s+(" + "|".join(FACTORY_NAMES) + r")\s*\(", re.M)
# The dependency that names the framework when no application object was found, in order of precedence.
FRAMEWORK_DEPENDENCIES = (("django", "wsgi"), ("fastapi", "asgi"), ("starlette", "asgi"), ("quart", "asgi"), ("litestar", "asgi"),
                          ("flask", "wsgi"), ("bottle", "wsgi"), ("falcon", "wsgi"))
ENTRY_FILES = ("main.py", "app.py", "wsgi.py", "asgi.py", "server.py", "api.py", "application.py", "run.py", "app/main.py",
               "app/__init__.py", "app/app.py", "api/main.py", "src/main.py", "src/app.py")
ROUTE_FILES = re.compile(r"(^|/)(urls|routes|views|health|api|endpoints)\.py$|(^|/)(routers|routes|views|api)/")
NOT_APPLICATION = re.compile(r"(^|/)(\.?venv|env|site-packages|node_modules|migrations|alembic|docs|build|dist)/|(^|/)(setup|conftest|noxfile)\.py$")

# Health routes the platform recognizes. Cloud Run reserves some paths ending in z (/healthz among them): a request from
# outside never reaches the container, so the release probes and the uptime check would fail on it.
HEALTH_LITERAL = re.compile(r"""["'](/(?:health|healthcheck|healthz)/?)["']""")
DJANGO_ROUTE = re.compile(r"""\b(?:re_)?path\(\s*r?["']\^?(health|healthcheck|healthz)(/)?(\?)?\$?["']""")
HEALTH_ORDER = ("/health", "/health/", "/healthcheck", "/healthcheck/")
RESERVED_HEALTH = ("/healthz", "/healthz/")

# What the allow-list .dockerignore leaves out of the image: dot entries, tests, environments and build output, project and
# platform files, documents, notebooks, keys and infrastructure. Everything else at the service's top level is let in.
QUIET_DIRECTORIES = {"tests", "test", "node_modules", "venv", "env", "build", "dist", "htmlcov", "__pycache__"}
# Left out too, but named in a review note: a service sometimes serves or reads these.
NAMED_DIRECTORIES = {"docs", "doc", "site", "notebooks", "infra", "terraform"}
LEFT_OUT_FILES = re.compile(r"(?i)^(dockerfile|containerfile|platform\.ya?ml|requirements-dev\.(txt|lock)|(dev|test)-requirements\.txt|"
                            r"requirements-test\.txt|test_.*\.py|.*_test\.py|conftest\.py|noxfile\.py|tox\.ini|pytest\.ini|setup\.cfg|setup\.py|"
                            r"pyproject\.toml|poetry\.lock|uv\.lock|pipfile(\.lock)?|environment\.ya?ml|makefile|procfile|(docker-)?compose.*\.ya?ml|"
                            r".*\.md|.*\.rst|license.*|licence.*|.*\.ipynb|.*\.pem|.*\.key|.*\.env|.*\.tf|.*\.tfvars)$")
# Left out of a Node.js service's image as well: its linters' and test runners' settings, version files and lockfiles of
# other managers (package.json and the npm lockfile are let in first, by name).
NODE_LEFT_OUT = re.compile(r"(?i)^(\.?eslint.*|eslint\.config\..*|\.prettier.*|prettier\.config\..*|jest\.config\..*|vitest\.config\..*|"
                           r"tsconfig.*\.json|nodemon\.json|.*\.(test|spec)\.(c|m)?[jt]s|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?)$")
CI_FILES = (".github/workflows/ci.yml", ".github/workflows/ci.yaml")
DEPENDABOT = (".github/dependabot.yml", ".github/dependabot.yaml")
RENOVATE = ("renovate.json", "renovate.json5", ".renovaterc", ".renovaterc.json", ".renovaterc.json5", ".github/renovate.json",
            ".github/renovate.json5", ".gitlab/renovate.json")
SECURITY_POLICY = ("security.md", ".github/security.md", "docs/security.md")
OTHER_DEV_REQUIREMENTS = ("dev-requirements.txt", "requirements-test.txt", "test-requirements.txt", "requirements/dev.txt", "requirements/test.txt")


# ---- small helpers
def clean_path(path):
    """The service's folder inside the repository ("" for the root), refused when it could escape or confuse a command."""
    text = (path or "").strip().strip("/")
    if text in ("", "."):
        return ""
    parts = text.split("/")
    if any(part in (".", "..") or not SEGMENT.fullmatch(part) for part in parts):
        raise ValueError(f"'{path}' is not a folder inside the repository (letters, digits, '.', '_' and '-' per segment)")
    return "/".join(parts)


# What a lock container may not do, wherever it runs: no Linux capabilities, no privilege gain, bounded processes and
# memory (the controller's machine has 1 GB). Resolving dependencies can build a package and so run its code.
CONTAINER_LIMITS = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "640m")


def base_image(python_version):
    """The pinned base image for a supported Python: python:X.Y-slim@<multi-architecture index digest>."""
    if python_version not in BASE_IMAGES:
        raise ValueError(f"Python {python_version} is not supported; choose one of {', '.join(PYTHONS)}")
    return f"python:{python_version}-slim@{BASE_IMAGES[python_version]}"


def node_base_image(line):
    """The pinned base image for a supported Node.js line: node:<line>-trixie-slim@<multi-architecture index digest>."""
    if line not in NODE_IMAGES:
        raise ValueError(f"Node.js {line} is not supported; choose one of {', '.join(NODE_IMAGES)}")
    return f"node:{line}-trixie-slim@{NODE_IMAGES[line]}"


def template(name):
    return (TEMPLATES / name).read_text()


def fill(text, **values):
    """Replace each __KEY__ with its value; a placeholder left over is a bug in the template or the caller."""
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    leftover = sorted(set(PLACEHOLDER.findall(text)))
    if leftover:
        raise ValueError(f"Template placeholders not filled: {', '.join(leftover)}")
    return text


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(text):
    """Distribution names in a requirements file (options, local paths and URLs are not names)."""
    names = set()
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(?:[<>=!~;@]|$)", line) if line and not line.startswith(("-", "#", ".", "/")) else None
        if match:
            names.add(normalize(match.group(1)))
            if match.group(2) and normalize(match.group(1)) == "fastapi" and re.search(r"standard|all", match.group(2)):
                names.add("uvicorn")  # fastapi[standard] installs uvicorn
    return names


def pyproject_data(text):
    try:
        return tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:
        return {}


def pyproject_names(data):
    names = set()
    for spec in (data.get("project") or {}).get("dependencies") or []:
        if isinstance(spec, str):
            names |= requirement_names(spec)
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    names |= {normalize(name) for name in poetry if isinstance(name, str) and name.lower() != "python"}
    return names


# ---- the Python version
def satisfies(version, specifier):
    """Whether Python X.Y, at its newest patch release (what python:X.Y-slim carries), meets a requires-python specifier.

    PEP 440 clauses and Poetry's ^ and ~; "||" separates alternatives. Lenient where people are: "<=3.12" and "==3.12"
    accept every 3.12 release. A single excluded patch release ("!=3.12.1") cannot remove a whole minor version.
    """
    candidate = (3, int(version.split(".")[1]), 99)
    groups = [group for group in specifier.split("||") if group.strip()] or ["*"]
    clause = r"(~=|===|==|!=|<=|>=|<|>|\^|~|=)?\s*v?(\d+(?:\.(?:\d+|\*)){0,2})(?:\.?(?:a|b|rc|post|dev)\d*)*"  # pre/post/dev suffixes ignored
    return any(all(_clause(candidate, op or "==", text) for op, text in re.findall(clause, group)) for group in groups)


def _clause(candidate, op, text):
    parts = text.split(".")
    numbers = [int(part) for part in parts if part != "*"]
    major = numbers[0]
    minor = numbers[1] if len(numbers) > 1 else None
    patch = numbers[2] if len(numbers) > 2 else None
    exact = (major, minor or 0, patch or 0)
    if op in ("==", "===", "="):
        return candidate[0] == major if minor is None else candidate[:2] == (major, minor)
    if op == "!=":
        if minor is None:
            return candidate[0] != major
        return True if patch is not None else candidate[:2] != (major, minor)
    if op == ">=":
        return candidate >= exact
    if op == ">":
        return candidate > exact
    if op == "<=":
        if patch is None:
            return candidate[0] <= major if minor is None else candidate[:2] <= (major, minor)
        return candidate <= exact
    if op == "<":
        if patch is None:
            return candidate[0] < major if minor is None else candidate[:2] < (major, minor)
        return candidate < exact
    if op in ("~=", "~") and minor is not None and (patch is not None or op == "~"):
        return candidate >= exact and candidate[:2] == (major, minor)
    return candidate >= exact and candidate[0] == major  # ~=X.Y, ~X and ^X.Y: same major, at least that release


def requires_python(data):
    project = (data.get("project") or {}).get("requires-python")
    poetry = (((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}).get("python")
    value = project or poetry
    return value if isinstance(value, str) else None


def detect_python(repo, spec):
    """(version, source, notes): the Dockerfile's base image, then python in platform.yaml, .python-version, runtime.txt,
    requires-python in pyproject.toml (the service's file before the repository's), else 3.12. Only 3.11 to 3.15 count."""
    notes = []
    found = re.findall(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?python:(3\.\d+)", repo.text("Dockerfile"), re.M | re.I)
    declared = spec.get("python") if isinstance(spec.get("python"), str) and spec.get("python") in PYTHONS else None
    if found:
        version = found[-1]
        if version in PYTHONS:
            if declared and declared != version:
                notes.append(f"The Dockerfile builds on Python {version} but platform.yaml says python: \"{declared}\"; make them the same, "
                             "or the tests and the audit run on a different Python than the service.")
            return version, "Dockerfile", notes
        notes.append(f"The Dockerfile builds on Python {version}, which the platform does not support ({', '.join(PYTHONS)}); "
                     "move the image to a supported version.")
    if declared:
        return declared, "platform.yaml", notes
    for source in (".python-version", "runtime.txt"):
        for where, text in repo.nearest(source):
            match = re.search(r"^\s*(?:cpython-|python-?)?(3\.\d+)", text, re.M | re.I)
            if match and match.group(1) in PYTHONS:
                return match.group(1), where, notes
            if match:
                notes.append(f"{where} asks for Python {match.group(1)}, which the platform does not support ({', '.join(PYTHONS)}).")
    for where, text in repo.nearest("pyproject.toml"):
        specifier = requires_python(pyproject_data(text))
        if not specifier:
            continue
        fits = [version for version in PYTHONS if satisfies(version, specifier)]
        if fits:
            return (DEFAULT_PYTHON if DEFAULT_PYTHON in fits else fits[0]), f"{where} (requires-python {specifier})", notes
        notes.append(f"{where} requires Python {specifier}, which no supported version ({', '.join(PYTHONS)}) meets; "
                     f"the drafts use {DEFAULT_PYTHON}.")
    return DEFAULT_PYTHON, "the platform's default", notes


# ---- the application
def module_of(relative, src_package=False):
    """("", "app.main") for app/main.py; ("src", "pkg.main") for src/pkg/main.py; None when it is not importable by name.
    When src/ is itself a package (it has an __init__.py), its modules are src.<module>: they may import each other
    relatively, which only works under that name (found live: llm-debate-agent's backend/src)."""
    parts = relative[:-3].split("/")
    app_dir = ""
    if parts[0] == "src" and len(parts) > 1 and not src_package:
        app_dir, parts = "src", parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(IDENTIFIER.fullmatch(part) for part in parts):
        return None
    return app_dir, ".".join(parts)


def rank(relative):
    """Entry points first (main.py, app.py, ...), then route modules, then the rest by depth."""
    if relative in ENTRY_FILES:
        return (ENTRY_FILES.index(relative), 0, relative)
    return (len(ENTRY_FILES) + (0 if ROUTE_FILES.search(relative) else 1), relative.count("/"), relative)


def application_python(relative):
    return relative.endswith(".py") and not TEST_PATH.search(relative) and not NOT_APPLICATION.search(relative)


def classify(call):
    for framework, interface, pattern in CONSTRUCTORS:
        if re.fullmatch(pattern, call):
            return framework, interface
    return None


def find_applications(sources, src_package=None):
    """Module-level application objects (constructed, or made by a factory call) and, separately, factory functions.
    `src_package`: whether src/__init__.py exists (read from the tree; when unknown, from the files read)."""
    objects, factories = [], []
    if src_package is None:
        src_package = "src/__init__.py" in sources
    for relative in sorted(sources, key=rank):
        located = module_of(relative, src_package)
        if not located:
            continue
        text = sources[relative]
        constructed = CONSTRUCTOR_CALL.search(text)
        for attribute, call in ASSIGNMENT.findall(text):
            kind = classify(call)
            via = call.split(".")[-1]
            if kind or via in FACTORY_NAMES:
                objects.append({"file": relative, "app_dir": located[0], "module": located[1], "attribute": attribute,
                                "framework": kind[0] if kind else None, "interface": kind[1] if kind else None, "via": None if kind else via})
        if constructed:
            framework, interface = classify(constructed.group(1))
            for attribute in FACTORY_DEF.findall(text):
                factories.append({"file": relative, "app_dir": located[0], "module": located[1], "attribute": attribute,
                                  "framework": framework, "interface": interface})
    return objects, factories


def find_django(repo, dependencies):
    """(module, settings module, notes) for a Django project, or None."""
    manage = repo.text("manage.py")
    settings = re.search(r"""DJANGO_SETTINGS_MODULE["']\s*,\s*["']([\w.]+)["']""", manage)
    wsgi = sorted(path for path in repo.local_paths if re.fullmatch(r"[A-Za-z_]\w*/wsgi\.py", path))
    uses = "get_wsgi_application" in "".join(repo.text(path) for path in wsgi)
    if not (settings or (repo.has("manage.py") and "django" in dependencies) or uses):
        return None
    notes = []
    settings_module = settings.group(1) if settings else None
    package = settings_module.split(".")[0] if settings_module else None
    if package and f"{package}/wsgi.py" in repo.local_paths:
        module = f"{package}.wsgi"
    elif len(wsgi) == 1:
        module = wsgi[0][:-3].replace("/", ".")
    else:
        module = f"{package or 'config'}.wsgi"
        notes.append(f"Could not tell which wsgi.py serves this Django project; the drafts use {module}:application. Correct the "
                     "Dockerfile's command if that is wrong.")
    if not settings_module:
        settings_module = module.rsplit(".", 1)[0] + ".settings"
        notes.append(f"manage.py does not name DJANGO_SETTINGS_MODULE; the drafted test uses {settings_module}.")
    return module, settings_module, notes


def find_health(sources):
    """{route: the file that routes it} for literal /health, /healthcheck and /healthz routes (Django's path("health/")
    included), read from the application's own modules, never from its tests."""
    seen = {}
    for relative in sorted(sources, key=rank):
        text = sources[relative]
        for literal in HEALTH_LITERAL.findall(text):
            seen.setdefault(literal, relative)
        for name, slash, optional in DJANGO_ROUTE.findall(text):
            seen.setdefault("/" + name + (slash if slash and not optional else ""), relative)
    return seen


def server_command(interface, application, app_dir="", factory=False, port=DEFAULT_PORT):
    """The exec-form command: uvicorn for ASGI; gunicorn for WSGI with one worker, eight threads and no timeout (Cloud Run's
    guidance for one CPU: the request timeout is Cloud Run's, and more workers lengthen cold starts)."""
    if interface == "wsgi":
        command = ["gunicorn", "--bind", f"0.0.0.0:{port}", "--workers", "1", "--threads", "8", "--timeout", "0"]
        return command + (["--chdir", app_dir] if app_dir else []) + [application + ("()" if factory else "")]
    command = ["uvicorn", application, "--host", "0.0.0.0", "--port", str(port)]  # noqa: S104 - Cloud Run's contract: listen on every interface
    return command + (["--factory"] if factory else []) + (["--app-dir", app_dir] if app_dir else [])


class Repository:
    """The repository as draft_files sees it: every path and text, and the service's own view at its folder."""

    def __init__(self, paths, texts, path=""):
        self.path = clean_path(path)
        self.prefix = self.path + "/" if self.path else ""
        self.texts = {key.strip("/"): value.replace("\r\n", "\n") for key, value in (texts or {}).items() if isinstance(value, str)}
        self.paths = {str(item).strip("/") for item in (paths or ())} | set(self.texts)
        self.local_paths = {item[len(self.prefix):] for item in self.paths if item.startswith(self.prefix)}

    def at(self, relative):
        return self.prefix + relative

    def has(self, relative):
        return self.at(relative) in self.paths

    def text(self, relative):
        return self.texts.get(self.at(relative), "")

    def nearest(self, name):
        """[(where, text)] for a file at the service's folder and then at the repository root (version files apply below)."""
        found = [(self.at(name), self.text(name))] if self.text(name) else []
        if self.path and self.texts.get(name):
            found.append((name, self.texts[name]))
        return found

    def sources(self):
        return {item[len(self.prefix):]: text for item, text in self.texts.items()
                if item.startswith(self.prefix) and application_python(item[len(self.prefix):])}


def existing_spec(repo):
    try:
        data = yaml.safe_load(repo.text("platform.yaml")) if repo.text("platform.yaml") else None
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def detect(paths, texts, path=""):
    """What the drafts are built from, read statically: the framework and its interface (ASGI or WSGI), the server and its
    exec-form command, the application's import path, the Python version and where it came from, the health path and the
    file that routes it, the port. `notes` are the things it could not decide."""
    repo = Repository(paths, texts, path)
    return _detect(repo)


def find_application(repo, sources, dependencies):
    """(what serves the app, notes): Django's wsgi module, else the module-level application object, else a factory, else
    a guess from the dependencies and the usual file names."""
    found = {"framework": None, "interface": None, "module": None, "attribute": "app", "app_dir": "", "factory": False,
             "settings_module": None, "application_file": None}
    django = find_django(repo, dependencies)
    if django:
        module, settings_module, notes = django
        found.update(framework="django", interface="wsgi", module=module, attribute="application", settings_module=settings_module,
                     application_file=module.replace(".", "/") + ".py")
        return found, notes
    notes = []
    objects, factories = find_applications(sources, "src/__init__.py" in repo.local_paths)
    chosen = None
    if objects:
        chosen = dict(objects[0])
        if chosen["via"]:  # made by a factory call: the factory's own module says which framework
            maker = next((item for item in factories if item["attribute"] == chosen["via"]), None)
            chosen.update(framework=maker["framework"] if maker else None, interface=maker["interface"] if maker else None)
        others = sorted({f"{item['module']}:{item['attribute']}" for item in objects if item["module"] != chosen["module"]})
        if others:
            notes.append(f"Found more than one application object ({', '.join([chosen['module'] + ':' + chosen['attribute']] + others)}); the "
                         f"drafts serve {chosen['module']}:{chosen['attribute']}. If another one is the service, change the Dockerfile's command "
                         "and the test's import.")
    elif factories:
        chosen = dict(factories[0], factory=True)
    if chosen:
        found.update(framework=chosen["framework"], interface=chosen["interface"], module=chosen["module"], attribute=chosen["attribute"],
                     app_dir=chosen["app_dir"], factory=bool(chosen.get("factory")), application_file=chosen["file"])
    if not found["interface"]:
        hinted = next(((name, interface) for name, interface in FRAMEWORK_DEPENDENCIES if name in dependencies and name != "django"), None)
        if hinted:
            found.update(framework=found["framework"] or hinted[0], interface=hinted[1])
    if not found["module"]:
        default = next((candidate for candidate in ("main.py", "app.py", "app/__init__.py", "app/main.py") if candidate in repo.local_paths), "main.py")
        found.update(module=module_of(default)[1], application_file=default if default in repo.local_paths else None)
        notes.append(f"Could not find the application object (a module-level app = FastAPI(), app = Flask(__name__) or similar) in the code "
                     f"read; the drafts assume {found['module']}:app. Correct the Dockerfile's command and the test's import if that is wrong.")
    if not found["interface"]:
        found["interface"] = "asgi"
        notes.append("Could not tell the framework from the code or the dependencies; the drafts assume an ASGI application served by uvicorn.")
    return found, notes


def choose_health(repo, sources, found, declared):
    """(health path, the file that routes it or None, notes). A path platform.yaml declares wins; else the first of /health,
    /health/, /healthcheck and /healthcheck/ the code routes; else /health, which the app must add."""
    seen = find_health(sources)
    notes = []
    path = declared or next((candidate for candidate in HEALTH_ORDER if candidate in seen), None)
    reserved = next((candidate for candidate in RESERVED_HEALTH if candidate in seen), None)
    if path and path in seen:
        where = seen[path]
        if found["framework"] != "django" and found["application_file"] and where != found["application_file"]:
            notes.append(f"{path} is routed in {repo.at(where)}, not beside the application object: confirm that no router or blueprint prefix "
                         "moves it.")
        return path, repo.at(where), notes
    path = path or DEFAULT_HEALTH
    if reserved:
        notes.append(f"{repo.at(seen[reserved])} routes {reserved}, but Cloud Run reserves paths ending in z: a request from outside never reaches "
                     f"the container, so the release probes and the uptime check would fail. Add a {path} route that answers the same way.")
    elif declared:
        notes.append(f"platform.yaml's health path {declared} was not found in the code read; the drafted test checks it, so it fails until "
                     "the route exists.")
    else:
        hint = (" In Django, route it as path(\"health\", ...) without a trailing slash, or the platform's probe gets a redirect."
                if found["framework"] == "django" else "")
        notes.append(f"No {path} route was found in the code read: the app must add one that answers 200 quickly, without calling other "
                     f"services; the drafted test and platform.yaml use {path}.{hint}")
    return path, None, notes


def choose_port(repo, spec):
    """platform.yaml's port, else the existing Dockerfile's single EXPOSE (as the analyzer reads it), else 8080."""
    declared = spec.get("port")
    if isinstance(declared, int) and not isinstance(declared, bool) and 1024 <= declared <= 65535:
        return declared
    exposed = re.findall(r"^\s*EXPOSE\s+(\d+)(?:/tcp)?\s*$", repo.text("Dockerfile"), re.M)
    return int(exposed[0]) if len(exposed) == 1 and 1024 <= int(exposed[0]) <= 65535 else DEFAULT_PORT


def _detect(repo):
    spec = existing_spec(repo)
    pyproject = pyproject_data(repo.text("pyproject.toml"))
    dependencies = requirement_names(repo.text("requirements.txt")) | pyproject_names(pyproject)
    sources = repo.sources()
    python, python_source, notes = detect_python(repo, spec)
    found, application_notes = find_application(repo, sources, dependencies)
    notes += application_notes
    if found["app_dir"]:
        options = ((pyproject.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
        pythonpath = options.get("pythonpath") or []
        if "src" not in ([pythonpath] if isinstance(pythonpath, str) else pythonpath):
            notes.append("The code is under src/: add pythonpath = [\"src\"] under [tool.pytest.ini_options] in pyproject.toml (or install the "
                         "package in the test step) so the tests can import it; the Dockerfile's command already looks there.")
    declared = spec.get("health_path") if isinstance(spec.get("health_path"), str) and re.fullmatch(r"/[A-Za-z0-9/_-]*", spec["health_path"]) else None
    health_path, health_file, health_notes = choose_health(repo, sources, found, declared)
    notes += health_notes
    if found["framework"] == "django":
        notes.append("Django answers 400 to any host ALLOWED_HOSTS does not list: allow the service's run.app host (an env value in "
                     "platform.yaml works) before the first release, or every probe fails.")
    port = choose_port(repo, spec)
    application = f"{found['module']}:{found['attribute']}"
    server = "gunicorn" if found["interface"] == "wsgi" else "uvicorn"
    installed = dependencies | requirement_names(repo.text("requirements.lock"))
    return {"framework": found["framework"], "interface": found["interface"], "server": server, "server_listed": server in installed,
            "application": application, "module": found["module"], "attribute": found["attribute"], "factory": found["factory"],
            "app_dir": found["app_dir"], "settings_module": found["settings_module"], "application_file": found["application_file"],
            "command": server_command(found["interface"], application, found["app_dir"], found["factory"], port),
            "python": python, "python_source": python_source, "health_path": health_path, "health_route_found": health_file is not None,
            "health_route_file": health_file, "port": port, "dependencies": sorted(dependencies), "notes": notes}


# ---- the lock file (D27)
def lock_command(python_version=DEFAULT_PYTHON, path=""):
    """The command docs/DEVELOPER-CONTRACT.md (section 1a) documents, run from the repository root for a service in a folder."""
    if python_version not in PYTHONS:
        raise ValueError(f"Python {python_version} is not supported; choose one of {', '.join(PYTHONS)}")
    folder = clean_path(path)
    command = f"uv pip compile requirements.txt --universal --python-version {python_version} --generate-hashes -o requirements.lock"
    return f"cd {folder} && {command}" if folder else command


def lock_container(python_version):
    """The docker command compile_lock runs: the application's own python:X.Y-slim by digest, a non-root user, uv installed by
    hash, the requirements on stdin and the lock on stdout. Nothing from the repository reaches a command line."""
    pin = f"uv=={UV_VERSION} " + " ".join(f"--hash=sha256:{digest}" for digest in UV_HASHES)
    script = "; ".join((
        "set -eu",
        "cat > requirements.txt",
        f"printf '%s\\n' {shlex.quote(pin)} > uv-requirements.txt",
        "python -m venv uv-venv",
        "uv-venv/bin/pip install --quiet --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r uv-requirements.txt",
        "uv-venv/bin/uv pip compile requirements.txt --universal --python-version " + python_version
        + " --generate-hashes -o requirements.lock --quiet --no-progress",
        "cat requirements.lock",
    ))
    return ["docker", "run", "--rm", "-i", *CONTAINER_LIMITS, "--user", "65532:65532", "-e", f"HOME={CONTAINER_HOME}", "-e", "UV_NO_CACHE=1", "-w", CONTAINER_HOME,
            base_image(python_version), "sh", "-c", script]


def docker_runner(args, stdin, timeout=900):
    """Run a command with text on stdin and return its stdout; raises when it fails or when Docker is not installed."""
    return subprocess.run(args, input=stdin, capture_output=True, text=True, check=True, timeout=timeout).stdout


def compile_lock(requirements_text, python_version=DEFAULT_PYTHON, runner=docker_runner, path=""):
    """D27: compile requirements.lock in a container of the application's own Python on the controller's machine.

    Returns {"text": the lock, "command": ..., "error": None}, or {"text": None, "command": the exact command to run by
    hand, "error": why} when Docker is unavailable or the compile fails (a private index, a local path, a git URL)."""
    command = lock_command(python_version, path)
    if not requirements_text.strip():
        return {"text": None, "command": command, "error": "requirements.txt is empty"}
    try:
        output = runner(lock_container(python_version), requirements_text)
    except FileNotFoundError:
        return {"text": None, "command": command, "error": "Docker is not installed on this machine"}
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        return {"text": None, "command": command, "error": f"the compile failed: {detail[-1][:300] if detail else exc}"}
    except Exception as exc:  # noqa: BLE001 - a timeout or a daemon that is not running is reported the same way
        return {"text": None, "command": command, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if "uv pip compile" not in output or ("==" in output and "--hash=sha256:" not in output):
        return {"text": None, "command": command, "error": "the container did not return a hash-locked requirements file"}
    return {"text": output, "command": command, "error": None}


def node_lock_command(path=""):
    """The command a person runs by hand, with npm 11.10 or later, from the repository root for a service in a folder."""
    folder = clean_path(path)
    command = f"npm install {NODE_LOCK_OPTIONS}"
    return f"cd {folder} && {command}" if folder else command


def pinned_npm_check(version, folder):
    """JavaScript that exits 4 unless the npm installed in `folder` is `version` with the integrity hash given as its first
    argument, read from the lockfile npm wrote there."""
    return (f"const e = (require('{folder}/package-lock.json').packages || {{}})['node_modules/npm'] || {{}}; "
            f"if (e.version !== '{version}' || e.integrity !== process.argv[1]) {{ console.error('npm ' + e.version + "
            "' does not match its pinned version and integrity'); process.exit(4); }")


def node_lock_container(line):
    """The docker command node_lock runs: the service's own node:<line>-trixie-slim by digest, a non-root user, package.json on
    stdin and package-lock.json on stdout. npm resolves with every script off and nothing published in the last three days;
    for a line whose npm is older than 11.10 (LOCK_NPM), a pinned npm is installed first and checked against its integrity
    hash. Nothing from the repository reaches a command line."""
    steps = ["set -eu", "cat > package.json"]
    if line in LOCK_NPM:
        version, integrity = LOCK_NPM[line]
        pinned = f"{CONTAINER_HOME}/npm-pinned"
        # The folder gets a package.json of its own first: without one, npm walks up to the service's package.json in the
        # folder above, installs itself there (adding npm to the service's dependencies) and leaves no lockfile here to check.
        steps += [f"mkdir {pinned}",
                  f"echo '{{\"private\": true}}' > {pinned}/package.json",
                  f"(cd {pinned} && npm install --no-audit --no-fund --ignore-scripts --loglevel=error npm@{version} >&2)",
                  f"node -e {shlex.quote(pinned_npm_check(version, pinned))} {shlex.quote(integrity)}",
                  f'npm() {{ node {pinned}/node_modules/npm/bin/npm-cli.js "$@"; }}']
    script = "; ".join((
        *steps,
        "version=$(npm --version)",
        'major=${version%%.*}; rest=${version#*.}; minor=${rest%%.*}',
        'if [ "$major" -lt 11 ] || { [ "$major" -eq 11 ] && [ "$minor" -lt 10 ]; }; then '
        'echo "npm $version cannot hold back new releases: min-release-age needs npm 11.10 or later" >&2; exit 3; fi',
        f"npm install {NODE_LOCK_OPTIONS} --no-audit --no-fund --loglevel=error >&2",
        "cat package-lock.json",
    ))
    return ["docker", "run", "--rm", "-i", *CONTAINER_LIMITS, "--user", "65532:65532", "-e", f"HOME={CONTAINER_HOME}",
            "-e", "npm_config_update_notifier=false", "-w", CONTAINER_HOME, node_base_image(line), "sh", "-c", script]


def failure_line(output):
    """The line of a failed container's output that says what went wrong: npm's own error, else the error a crash names
    (Node.js ends a crash with its bare version, "Node.js v22.23.3", which says nothing), else the last line."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip() and not re.fullmatch(r"Node\.js v[\d.]+", line.strip())]
    found = [line for line in lines if re.match(r"(npm (error|ERR!) |\w*Error\b)", line)
             and not re.match(r"npm (error|ERR!) (code|errno|syscall|A complete log|Log files)\b", line)]
    return (found[0] if found else lines[-1] if lines else "")[:300]


def node_lock(package_text, line, runner=docker_runner, path=""):
    """D42 in the pattern of D27: package-lock.json made in a container on the controller's machine, by npm 11.10 or later,
    with every install script off and no version younger than three days. Every entry must carry its integrity hash and a
    registry address before the lock is proposed. Returns {"text", "command", "error"} like compile_lock."""
    from .node import lock_problems, package_data, read_lockfile
    command = node_lock_command(path)
    if not package_data(package_text):
        return {"text": None, "command": command, "error": "package.json is missing or is not valid JSON"}
    try:
        output = runner(node_lock_container(line), package_text)
    except FileNotFoundError:
        return {"text": None, "command": command, "error": "Docker is not installed on this machine"}
    except subprocess.CalledProcessError as exc:
        return {"text": None, "command": command, "error": f"npm could not lock the dependencies: {failure_line(exc.stderr or exc.stdout) or exc}"}
    except Exception as exc:  # noqa: BLE001 - a timeout or a daemon that is not running is reported the same way
        return {"text": None, "command": command, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    data, problem = read_lockfile(output or "")
    if data is None:
        return {"text": None, "command": command, "error": f"the container did not return a usable package-lock.json (it {problem})"}
    problems = lock_problems(data)
    if problems:
        return {"text": None, "command": command, "error": "the lock holds packages npm ci would not check against the registry: "
                + "; ".join(f"{name} {why}" for name, why in problems[:4])}
    return {"text": output if output.endswith("\n") else output + "\n", "command": command, "error": None}


# ---- the drafts
def dockerignore(repo, existing_dockerfile=""):
    """An allow-list built from the service's own top level: (text, left-out entries a service sometimes needs)."""
    entries = sorted({item.split("/", 1)[0] for item in repo.local_paths if item})
    allowed, named = [], []
    for entry in entries:
        directory = any(item.startswith(entry + "/") for item in repo.local_paths)
        if directory and entry in NAMED_DIRECTORIES:
            named.append(entry + "/")
        elif not (entry.startswith(".") or entry.endswith(".egg-info") or (directory and entry in QUIET_DIRECTORIES)
                  or (not directory and LEFT_OUT_FILES.match(entry))):
            allowed.append(entry)
    allowed = ["requirements.txt", "requirements.lock"] + [entry for entry in allowed if entry not in ("requirements.txt", "requirements.lock")]
    for source in copy_sources(existing_dockerfile):  # whatever an existing Dockerfile copies must stay in the context
        if source not in allowed:
            allowed.append(source)
            named = [entry for entry in named if entry.rstrip("/") != source and not source.startswith(entry)]
    return fill(template("dockerignore"), ALLOW="\n".join("!" + entry for entry in allowed)), named


def copy_sources(dockerfile):
    """What an existing Dockerfile copies from the build context (globs stay globs); stages and URLs are not the context."""
    sources = []
    for line in re.sub(r"\\\n", " ", dockerfile or "").splitlines():
        match = re.match(r"^\s*(COPY|ADD)\s+(.+)$", line, re.I)
        if not match or "<<" in match.group(2):
            continue
        rest = match.group(2).strip()
        try:
            args = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        except ValueError:  # json.JSONDecodeError is a ValueError
            continue
        if any(str(arg).startswith("--from") for arg in args):
            continue
        args = [str(arg) for arg in args if not str(arg).startswith("--")]
        for source in args[:-1]:
            source = source.removeprefix("./").rstrip("/")
            if source and source != "." and "://" not in source and "$" not in source and ".." not in source.split("/"):
                sources.append(source)
    return sources


def dockerfile(found, locked):
    requirements = "requirements.lock" if locked else "requirements.txt"
    return fill(template("Dockerfile"), PYTHON=found["python"], RESOLVED=BASE_IMAGES_RESOLVED, DIGEST=BASE_IMAGES[found["python"]],
                REQUIREMENTS=requirements, INSTALL=("--require-hashes -r " if locked else "-r ") + requirements, PORT=found["port"],
                CMD=json.dumps(found["command"]))


def node_dockerfile(found):
    """The Node.js Dockerfile (docs/ROADMAP.md D45): node:<line>-trixie-slim by digest, NODE_ENV=production, the lockfile installed
    with npm ci and every script off, then npm rebuild for the contract's build.scripts only, the code, a numeric non-root
    user and an exec-form CMD that starts node itself. `found`: node, port, command (words), lockfile, scripts."""
    scripts = found.get("scripts") or []
    return fill(template("Dockerfile.node"), NODE=found["node"], RESOLVED=NODE_IMAGES_RESOLVED, DIGEST=NODE_IMAGES[found["node"]],
                LOCKFILE=found.get("lockfile") or "package-lock.json", REBUILD=(" && npm rebuild " + " ".join(scripts)) if scripts else "",
                PORT=found["port"], CMD=json.dumps(found["command"]))


def platform_draft(repo, name, found, dockerfile_text, planned):
    """The analyzer's draft, so it stays what analyze_repository proposes, with the detected Python and health path; reviewed: false."""
    files = {key: text for key, text in (("requirements.txt", repo.text("requirements.txt")), ("pyproject.toml", repo.text("pyproject.toml")),
                                         ("Dockerfile", dockerfile_text)) if text}
    analysis = analyze(files, name, sorted(planned))
    draft = dict(analysis["draft_spec"], python=found["python"], health_path=found["health_path"], port=found["port"], reviewed=False)
    if found["interface"] == "wsgi":
        draft["concurrency"] = min(draft["concurrency"], 8)  # one gunicorn worker with eight threads serves eight requests at once
    spec = AppSpec.model_validate(draft)
    return fill(template("platform.yaml.tmpl"), SPEC=yaml.safe_dump(draft_dump(spec), sort_keys=False).rstrip("\n")), analysis


def health_test(found):
    """The framework's own way to call the health path: Starlette's TestClient (FastAPI, Starlette), httpx's ASGI transport
    (other ASGI frameworks), Flask's test client, Django's test client, or a plain WSGI call."""
    target = found["attribute"] + ("()" if found["factory"] else "")
    common = {"IMPORT": f"from {found['module']} import {found['attribute']}", "APP": target, "HEALTH_PATH": found["health_path"]}
    if found["framework"] == "django":
        return fill(template("test_health_django.py.tmpl"), SETTINGS=found["settings_module"], HEALTH_PATH=found["health_path"])
    if found["framework"] in ("fastapi", "starlette"):
        return fill(template("test_health_testclient.py.tmpl"), TESTCLIENT=f"{found['framework']}.testclient", **common)
    if found["interface"] == "asgi":
        return fill(template("test_health_asgi.py.tmpl"), **common)
    if found["framework"] == "flask":
        return fill(template("test_health_flask.py.tmpl"), **common)
    return fill(template("test_health_wsgi.py.tmpl"), **common)


def service_name(name, repo, fallback=None):
    """(name, note): the explicit name, refused when Cloud Run would refuse it, else one made from the folder, pyproject.toml
    or `fallback` (the repository's own name, for a service at its root)."""
    if name is not None:
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            raise ValueError(f"'{name}' is not a valid service name: 3 to 21 characters, a lowercase letter first, then lowercase letters, "
                             "digits or dashes, not ending in a dash")
        return name, None
    project = (pyproject_data(repo.text("pyproject.toml")).get("project") or {}).get("name")
    for candidate in (repo.path.rsplit("/", 1)[-1] if repo.path else None, project if isinstance(project, str) else None, fallback):
        slug = re.sub(r"[^a-z0-9]+", "-", (candidate or "").lower())
        slug = re.sub(r"^[^a-z]+", "", slug)[:21].strip("-")
        if SERVICE_NAME.fullmatch(slug):
            return slug, None
    return "my-service", "No service name was given or found, so platform.yaml says my-service: choose the real name (3 to 21 characters)."


def ci_file(repo, name, runners=("pytest",)):
    """(file to draft or None, note, workflows that already run these tests). A service at the root gets ci.yml; one in a
    folder gets ci.yml while the repository has none, else ci-<name>.yml, unless a workflow already runs its tests there
    (`runners`: pytest, or npm test and node --test for a Node.js service)."""
    workflows = sorted(item for item in repo.paths if WORKFLOW.fullmatch(item) and not PLATFORM_WORKFLOW.fullmatch(item))
    covering = [item for item in workflows if any(runner in repo.texts.get(item, "") for runner in runners)
                and (not repo.path or repo.path in repo.texts[item])]
    if covering:
        return None, None, covering
    if not any(item in repo.paths for item in CI_FILES):
        target = CI_FILES[0]
    elif repo.path and f".github/workflows/ci-{name}.yml" not in repo.paths:
        target = f".github/workflows/ci-{name}.yml"
    else:
        return None, None, [item for item in (*CI_FILES, f".github/workflows/ci-{name}.yml") if item in repo.paths]
    unread = [item for item in workflows if item not in repo.texts]
    where = f" in {repo.path}" if repo.path else ""
    note = (f"The repository already has {', '.join(unread)}; if one of them runs the tests{where} on pull requests, drop the drafted "
            f"{target}.") if unread else None
    return target, note, []


def draft_files(paths, texts, path="", name=None, lock_text=None, fallback_name=None):
    """D26: the files docs/DEVELOPER-CONTRACT.md asks for that the service lacks, drafted for one pull request.

    `paths` is the repository's file list as its Git tree gives it (relative to the root); `texts` maps some of those paths
    to their text (files_to_read says which); `path` is the service's folder ("" for the root); `name` the service name
    (derived when absent); `lock_text` a requirements.lock that compile_lock produced, drafted when the service has none.

    Returns {"files": {repository path: {"content", "purpose"}}, "existing": [the contract's files already there, left
    untouched], "review": [what a person must decide], "detected": detect()'s answer, "lock_command": the command that
    compiles the lock, "analysis": the analyzer's result when platform.yaml was drafted}. The service's files go under
    `path`; .github/ files and SECURITY.md go at the repository root. A file that exists is never in "files"."""
    repo = Repository(paths, texts, path)
    from .reader import runtime_of  # the reader imports this module
    if runtime_of(repo, {item for item in repo.local_paths if item}) == "node-http":
        return draft_node_files(repo, name, lock_text, fallback_name)
    if not any(item.endswith(".py") for item in repo.local_paths) and not repo.has("requirements.txt") and not repo.has("pyproject.toml"):
        where = f"{repo.path}/" if repo.path else "the repository root"
        return {"files": {}, "existing": [], "review": [f"No Python or Node.js code was found at {where}: the platform deploys Python and Node.js "
                                                       "web services, so nothing was drafted."], "detected": None, "lock_command": None, "analysis": None}
    found = _detect(repo)
    name, name_note = service_name(name, repo, fallback_name)
    command = lock_command(found["python"], repo.path)
    files, existing, review = {}, [], found["notes"] + ([name_note] if name_note else [])

    def offer(target, content, purpose):
        files[target] = {"content": content, "purpose": purpose}

    def present(*candidates, root=False):
        """The first candidate that exists (service-relative unless root), recorded as existing; None when all are missing."""
        for candidate in candidates:
            target = candidate if root else repo.at(candidate)
            if target in repo.paths:
                existing.append(target)
                return target
        return None

    # requirements.lock: compiled by the caller (compile_lock) or explained.
    locked = bool(present("requirements.lock")) or lock_text is not None
    if lock_text is not None and repo.at("requirements.lock") not in existing:
        offer(repo.at("requirements.lock"), lock_text if lock_text.endswith("\n") else lock_text + "\n",
              f"Every dependency pinned with its hashes, for Python {found['python']} and later, compiled with uv; the image and the tests install "
              "it with --require-hashes.")
    elif not locked and repo.has("requirements.txt"):
        review.append(f"There is no requirements.lock yet: run `{command}` (it needs uv) and commit the file, then make the Dockerfile copy and "
                      "install it with pip install --require-hashes -r requirements.lock.")
    requirements = repo.text("requirements.txt")
    if not repo.has("requirements.txt") and not locked:
        review.append("requirements.txt is missing: the contract requires one with exact pins (docs/DEVELOPER-CONTRACT.md, section 1a, shows "
                      "how to export it from Poetry, uv or conda); the drafted Dockerfile installs it.")
    if re.search(r"(?m)^\s*(-r|-c|--requirement|--constraint)\s", requirements):
        review.append("requirements.txt includes other files (-r or -c): the drafted Dockerfile copies only requirements.txt before installing, "
                      "so copy those files first as well.")
    if re.search(r"(?m)^\s*(-e\s|\.|/|file:)", requirements):
        review.append("requirements.txt installs a local path: the dependency layer is built before the code is copied, so publish that "
                      "package or move it into the service's own code.")

    dockerfile_text = repo.text("Dockerfile") or repo.text("dockerfile")
    if not present("Dockerfile", "dockerfile"):
        dockerfile_text = dockerfile(found, locked)
        offer(repo.at("Dockerfile"), dockerfile_text, f"How the image is built: python:{found['python']}-slim pinned by digest, dependencies "
              f"before the code, a non-root user, {found['server']} serving {found['application']} on 0.0.0.0:{found['port']}.")
        if not found["server_listed"]:
            review.append(f"The requirements do not list {found['server']}, which the drafted Dockerfile starts: add "
                          f"{found['server']}=={SERVER_PINS[found['server']]} to requirements.txt (and recompile the lock).")

    # A Dockerfile-specific ignore file beside the Dockerfile counts: BuildKit prefers it, and it is the one a service built
    # from the whole repository (build.context: repository) uses.
    if not present(".dockerignore", "Dockerfile.dockerignore"):
        text, named = dockerignore(repo, "" if repo.at("Dockerfile") in files else dockerfile_text)
        offer(repo.at(".dockerignore"), text, "An allow-list that keeps tests, secrets, environments and caches out of the image: only the "
              "entries it names are copied.")
        if named:
            review.append(f"The drafted .dockerignore leaves {', '.join(named)} out of the image; add a !name line for any the service reads "
                          "at runtime.")

    if not present("requirements-dev.txt"):
        tools = ["pytest"] + (["httpx"] if found["interface"] == "asgi" else [])
        offer(repo.at("requirements-dev.txt"), fill(template("requirements-dev.txt"), PINS="\n".join(f"{tool}=={DEV_PINS[tool]}" for tool in tools)),
              f"The test tools ({', '.join(tools)}) for the platform's test step and CI, pinned exactly and kept out of the image.")
        others = [repo.at(item) for item in OTHER_DEV_REQUIREMENTS if repo.has(item)]
        if others:
            review.append(f"{', '.join(others)} also lists development tools; the platform reads only requirements-dev.txt, so merge them into it.")

    # pytest refuses two test modules with the same name, so any test_health.py under the service counts as the health test;
    # so does a test file that already requests the health path (files_to_read fetches a few test files for this).
    def tests_health(item):
        text = repo.text(item)
        return bool(TEST_PATH.search(item)) and item.endswith(".py") and any(f"{quote}{found['health_path']}{quote}" in text for quote in "\"'")

    health_tests = sorted(item for item in repo.local_paths if item.rsplit("/", 1)[-1] == "test_health.py" or tests_health(item))
    if health_tests:
        existing.extend(repo.at(item) for item in health_tests)
    else:
        offer(repo.at("tests/test_health.py"), health_test(found),
              f"Checks that GET {found['health_path']} answers 200: the path the platform probes before and after every release.")
        if existing_spec(repo).get("tests") == "none":
            review.append("platform.yaml says tests: none; with the drafted health test it can say tests: required.")

    analysis = None
    if not present("platform.yaml"):
        planned = repo.local_paths | {target[len(repo.prefix):] for target in files if target.startswith(repo.prefix)}
        text, analysis = platform_draft(repo, name, found, dockerfile_text, planned)
        offer(repo.at("platform.yaml"), text, "The deployment contract, drafted with reviewed: false: check every value, then set reviewed: true.")

    ci, ci_note, covering = ci_file(repo, name)
    existing.extend(covering)
    if ci:
        job = f"tests-{name}" if repo.path else "tests"
        offer(ci, fill(template("ci.yml"), WORKFLOW_NAME="CI" if ci == CI_FILES[0] else f"CI {name}", JOB=job, WORKING_DIRECTORY=repo.path or ".",
                       PYTHON=found["python"], LOCK_COMMAND=command),
              f"Runs the tests on every pull request, on Python {found['python']} with a read-only token; make \"{job}\" a required check on main.")
        review += [ci_note] if ci_note else []

    policy = sorted(item for item in repo.paths if item.lower() in SECURITY_POLICY)
    if policy:
        existing.extend(policy)
    else:
        offer("SECURITY.md", template("SECURITY.md"), "How to report a vulnerability privately; it works once private vulnerability reporting "
              "is turned on in the repository's Settings, under Code security.")

    dependabot = present(*DEPENDABOT, root=True)
    if dependabot:
        text = repo.texts.get(dependabot)
        if repo.path and text is not None and repo.path not in text:
            review.append(f"{dependabot} does not mention {repo.path}: add /{repo.path} to its pip and docker entries so this service's pins are "
                          "kept current.")
    elif not present(*RENOVATE, root=True):
        offer(".github/dependabot.yml", fill(template("dependabot.yml"), DIRECTORY="/" + repo.path, LOCK_COMMAND=command),
              "Weekly pull requests for the pip, Docker and GitHub Actions pins, each update at least seven days old.")
    return {"files": files, "existing": existing, "review": review, "detected": found, "lock_command": command, "analysis": analysis}


def node_dockerignore(repo, lockfile, start=None):
    """The allow-list .dockerignore for a Node.js service: package.json and its lockfile first, then the service's own top
    level as for Python, with the start file's folder let in even when it is build output (dist/): (text, named entries)."""
    entries = sorted({item.split("/", 1)[0] for item in repo.local_paths if item})
    allowed, named = [], []
    for entry in entries:
        directory = any(item.startswith(entry + "/") for item in repo.local_paths)
        if directory and entry in NAMED_DIRECTORIES:
            named.append(entry + "/")
        elif not (entry.startswith(".") or (directory and entry in QUIET_DIRECTORIES)
                  or (not directory and (LEFT_OUT_FILES.match(entry) or NODE_LEFT_OUT.match(entry)))):
            allowed.append(entry)
    first = (start or "").split("/")[0]
    if first and first not in allowed and first not in ("node_modules", "test", "tests") and not first.startswith("."):
        allowed.append(first)
        named = [entry for entry in named if entry.rstrip("/") != first]
    allowed = ["package.json", lockfile] + [entry for entry in allowed if entry not in ("package.json", "package-lock.json", "npm-shrinkwrap.json")]
    return fill(template("dockerignore.node"), ALLOW="\n".join("!" + entry for entry in allowed)), named


def node_health_test(found):
    """test/health.test.mjs on node --test: it starts the service as its image does on a free port and asks for the health path."""
    words = found["command"][1:] if found["command"][:1] == ["node"] else found["command"]
    return fill(template("health.test.mjs.tmpl"), HEALTH_PATH=found["health_path"], START=" ".join(words), ROOT="../", ARGS=json.dumps(words))


def draft_node_files(repo, name=None, lock_text=None, fallback_name=None):
    """D26 for a Node.js service (docs/ROADMAP.md 18.6): the files docs/DEVELOPER-CONTRACT.md asks for that the service lacks,
    drafted from what the reader reads in it: the Dockerfile on node:<line>-trixie-slim, .dockerignore, test/health.test.mjs on
    node --test, platform.yaml, ci.yml with setup-node, Dependabot for npm with its cooldown written in, SECURITY.md, and
    package-lock.json when the caller made one (node_lock). Nothing is drafted while the reader refuses the service."""
    from . import node as nodejs
    from .reader import read
    found_read = read(sorted(repo.paths), repo.texts, repo.path, name=name, fallback_name=fallback_name)
    command = node_lock_command(repo.path)
    if found_read["spec"] is None:
        refusals = [f"{item['file']} {item['problem']}; fix: {item['fix']}" for item in found_read["refusals"]]
        return {"files": {}, "existing": [], "review": ["Nothing was drafted: the service must change first."] + refusals, "detected": None,
                "lock_command": command, "analysis": None}
    spec = found_read["spec"]
    name = spec.name
    choices = {item["what"]: item for item in found_read["choices"]}
    lockfile = next((item for item in nodejs.LOCKFILES if repo.has(item)), "package-lock.json")
    words = start_command(spec.build.entrypoint) if spec.build.entrypoint else None
    if words is None and str(choices.get("Start command", {}).get("value", "")).startswith("node "):
        words = shlex.split(choices["Start command"]["value"])  # the service's own Dockerfile starts node itself
    found = {"runtime": "node-http", "node": spec.node, "node_source": choices.get("Node.js", {}).get("from"), "port": spec.port,
             "health_path": spec.health_path or DEFAULT_HEALTH, "health_route_found": spec.health_path is not None,
             "health_route_file": choices.get("Health", {}).get("from") if spec.health_path else None, "command": words, "lockfile": lockfile,
             "scripts": spec.build.scripts, "application": " ".join(words or []) or "the image's own start command", "framework": None,
             "server": "node", "notes": list(found_read["notes"])}
    files, existing, review = {}, [], list(found_read["notes"])

    def offer(target, content, purpose):
        files[target] = {"content": content, "purpose": purpose}

    def present(*candidates, root=False):
        for candidate in candidates:
            target = candidate if root else repo.at(candidate)
            if target in repo.paths:
                existing.append(target)
                return target
        return None

    if present(*nodejs.LOCKFILES) is None:
        if lock_text is not None:
            offer(repo.at("package-lock.json"), lock_text if lock_text.endswith("\n") else lock_text + "\n",
                  "Every dependency pinned with its integrity hash, made by npm 11.10 or later with every install script off and no version "
                  "younger than three days; the image and the tests install it with npm ci.")
        else:
            review.append(f"There is no package-lock.json yet: run `{command}` with npm 11.10 or later and commit it; npm ci installs only from a lockfile.")
    if not present("Dockerfile", "dockerfile"):
        offer(repo.at("Dockerfile"), node_dockerfile(found),
              f"How the image is built: node:{spec.node}-slim pinned by digest, npm ci with every install script off"
              + (f", then npm rebuild for {', '.join(spec.build.scripts)}" if spec.build.scripts else "")
              + f", a non-root user, `{' '.join(words)}` on port {spec.port}.")
    if not present(".dockerignore", "Dockerfile.dockerignore"):
        text, named = node_dockerignore(repo, lockfile, words[1] if words and len(words) > 1 else None)
        offer(repo.at(".dockerignore"), text, "An allow-list that keeps node_modules, tests, secrets and caches out of the image: only the "
              "entries it names are copied.")
        if named:
            review.append(f"The drafted .dockerignore leaves {', '.join(named)} out of the image; add a !name line for any the service reads at runtime.")
    package = nodejs.package_data(repo.text("package.json")) or {}
    test_script = ((package.get("scripts") or {}) if isinstance(package.get("scripts"), dict) else {}).get("test")
    tests = sorted(item for item in repo.local_paths if nodejs.NODE_TEST.search(item) and not nodejs.NOT_CODE.search(item))
    if any(f"{quote}{found['health_path']}" in repo.text(item) for item in tests for quote in "\"'`"):
        existing.extend(repo.at(item) for item in tests if any(f"{q}{found['health_path']}" in repo.text(item) for q in "\"'`"))
    elif words and words[0] == "node":
        offer(repo.at("test/health.test.mjs"), node_health_test(found),
              f"Checks that GET {found['health_path']} answers 200: the path the platform probes before and after every release.")
        if not found["health_route_found"]:
            review.append(f"No {found['health_path']} route was found in the code read: the service must add one that answers 200 quickly, "
                          "without calling other services; the drafted test and platform.yaml use it.")
        if isinstance(test_script, str) and test_script.strip() and not nodejs.PLACEHOLDER_TEST.fullmatch(test_script) and "node --test" not in test_script:
            review.append(f"package.json's test script (`{test_script[:60]}`) does not run node --test files: add test/health.test.mjs to it, "
                          "or the platform's npm test will not run the drafted check.")
    if not present("platform.yaml"):
        data = draft_dump(spec)
        data["reviewed"] = False
        if repo.at("Dockerfile") in files:
            for key in ("recipe", "entrypoint"):
                data["build"].pop(key, None)
        data["tests"] = "required" if (tests or repo.at("test/health.test.mjs") in files or found_read["tests"]) else "none"
        offer(repo.at("platform.yaml"), fill(template("platform.yaml.tmpl"), SPEC=yaml.safe_dump(data, sort_keys=False).rstrip("\n")),
              "The deployment contract, drafted with reviewed: false: check every value, then set reviewed: true.")
    ci, ci_note, covering = ci_file(repo, name, ("npm test", "node --test", "npm run test"))
    existing.extend(covering)
    if ci:
        job = f"tests-{name}" if repo.path else "tests"
        rebuild = f"npm rebuild --ignore-scripts=false {' '.join(spec.build.scripts)}" if spec.build.scripts else "# build.scripts is empty: no package runs an install script"
        offer(ci, fill(template("ci-node.yml"), WORKFLOW_NAME="CI" if ci == CI_FILES[0] else f"CI {name}", JOB=job, WORKING_DIRECTORY=repo.path or ".",
                       NODE=spec.node, REBUILD=rebuild),
              f"Runs the tests on every pull request, on Node.js {spec.node} with every install script off and a read-only token; make \"{job}\" "
              "a required check on main.")
        review += [ci_note] if ci_note else []
    policy = sorted(item for item in repo.paths if item.lower() in SECURITY_POLICY)
    if policy:
        existing.extend(policy)
    else:
        offer("SECURITY.md", template("SECURITY.md"), "How to report a vulnerability privately; it works once private vulnerability reporting "
              "is turned on in the repository's Settings, under Code security.")
    dependabot = present(*DEPENDABOT, root=True)
    if dependabot:
        text = repo.texts.get(dependabot)
        if text is not None and "package-ecosystem: npm" not in text:
            review.append(f"{dependabot} has no npm entry: add one for /{repo.path} with a cooldown, so the lockfile's pins are kept current.")
    elif not present(*RENOVATE, root=True):
        offer(".github/dependabot.yml", fill(template("dependabot-node.yml"), DIRECTORY="/" + repo.path),
              "Weekly pull requests for the npm, Docker and GitHub Actions pins, each update at least seven days old.")
    return {"files": files, "existing": existing, "review": review, "detected": found, "lock_command": command, "analysis": None}


def files_to_read(paths, path="", limit=40):
    """The repository paths whose text draft_files uses, so a caller fetches only those: the configuration first, then the
    Python files most likely to hold the application object and its health route. For a Node.js service: the reader's
    files, the repository's workflows and Dependabot file, and a few test files (an existing health test is not drafted again)."""
    repo = Repository(paths, {}, path)
    from .reader import PYTHON_MANIFESTS
    from .reader import files_to_read as reader_files
    if repo.has("package.json") and not any(repo.has(name) for name in PYTHON_MANIFESTS):
        from .node import NODE_TEST, NOT_CODE
        wanted = reader_files(paths, path, limit) + [item for item in DEPENDABOT if item in repo.paths]
        wanted += sorted(item for item in repo.paths if WORKFLOW.fullmatch(item) and not PLATFORM_WORKFLOW.fullmatch(item))[:5]
        wanted += sorted(repo.at(item) for item in repo.local_paths if NODE_TEST.search(item) and not NOT_CODE.search(item))[:5]
        return list(dict.fromkeys(wanted))
    names = ("requirements.txt", "requirements.lock", "pyproject.toml", "Dockerfile", "dockerfile", ".python-version", "runtime.txt",
             "platform.yaml", "manage.py")
    wanted = [repo.at(name) for name in names if repo.has(name)]
    if repo.path:
        wanted += [name for name in (".python-version", "pyproject.toml") if name in repo.paths]
    wanted += [item for item in DEPENDABOT if item in repo.paths]
    wanted += sorted(item for item in repo.paths if WORKFLOW.fullmatch(item) and not PLATFORM_WORKFLOW.fullmatch(item))[:5]
    code = [repo.at(item) for item in sorted(repo.local_paths, key=rank) if application_python(item) and repo.at(item) not in wanted]
    # A few test files, so an existing health test is recognized and not drafted again.
    tests = sorted(repo.at(item) for item in repo.local_paths if TEST_PATH.search(item) and item.endswith(".py") and repo.at(item) not in wanted)[:5]
    return wanted + tests + code[:max(limit - len(wanted) - len(tests), 0)]


def pull_request_body(result):
    """The pull request text: one line per drafted file, what was detected, and what a person must decide."""
    found = result.get("detected") or {}
    lines = ["Fast Deploy drafted these files because docs/DEVELOPER-CONTRACT.md asks for them and this repository does not have them yet. "
             "No existing file was changed, and nothing deploys until `platform.yaml` is reviewed and says `reviewed: true`.", "",
             "| File | Why |", "|---|---|"]
    lines += [f"| `{target}` | {item['purpose']} |" for target, item in sorted(result["files"].items())]
    if found and found.get("node"):
        route = f"found in `{found['health_route_file']}`" if found.get("health_route_file") else "not found in the code"
        lines += ["", f"Detected: a Node.js service started by `{found['application']}` on port {found['port']}; Node.js {found['node']} "
                      f"(from {found['node_source']}); health path `{found['health_path']}` ({route})."]
    elif found:
        framework = found.get("framework") or "unrecognized"
        route = f"found in `{found['health_route_file']}`" if found.get("health_route_file") else "not found in the code"
        lines += ["", f"Detected: a {framework} application `{found['application']}` served by {found['server']} on port {found['port']}; "
                      f"Python {found['python']} (from {found['python_source']}); health path `{found['health_path']}` ({route})."]
    if result.get("review"):
        lines += ["", "Please decide:"] + [f"- {note}" for note in result["review"]]
    if result.get("analysis"):
        lines += ["", "For `platform.yaml`:"] + [f"- {note}" for note in result["analysis"]["review_required"]]
    lines += ["", "The platform never merges this pull request."]
    return "\n".join(lines) + "\n"


# ---- builds from outside (docs/ROADMAP.md 13.3): the image of a repository taken as it is, made without touching it
# What the platform never copies into an image it makes: what the drafted allow-list keeps out, written as a deny-list,
# because the platform cannot know in advance which of a repository's files the service reads at run time.
# node_modules is the platform's to install (npm ci in the image, docs/ROADMAP.md D45), and .npmrc and .yarnrc.yml can hold
# registry tokens, so none of them ever enters an image the platform makes.
BUILD_EXCLUDE = (".git", ".github", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "id_rsa*", "id_dsa*", "id_ecdsa*",
                 "id_ed25519*", "credentials.json", "*.tfstate", "*.tfstate.*", "*.tf", "*.tfvars", ".venv", "venv", "node_modules",
                 ".npmrc", ".yarnrc.yml", "__pycache__", "*.py[cod]", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
                 "htmlcov", ".coverage", "tests", "test", "test_*.py", "*_test.py", "conftest.py", "*.ipynb", ".ipynb_checkpoints",
                 ".DS_Store", "Dockerfile", "Dockerfile.*", ".dockerignore", "platform.yaml", "compose.yaml", "compose.yml",
                 "docker-compose.yml", "docker-compose.yaml")


# For a repository's own Dockerfile without a .dockerignore (docs/ROADMAP.md 14.2): what never belongs in an image, and
# nothing a Dockerfile might copy on purpose (tests, its own Dockerfile, requirement files). A node_modules folder built on
# someone's machine holds binaries for that machine; the image installs its own.
DENY_DOCKERIGNORE = (".git", "**/.git", "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx", "**/*.jks", "**/id_rsa*",
                     "**/id_dsa*", "**/id_ecdsa*", "**/id_ed25519*", "**/credentials.json", "**/*.tfstate", "**/*.tfstate.*", "**/.venv",
                     "**/node_modules", "**/.npmrc", "**/.yarnrc.yml", "**/__pycache__", "**/*.py[cod]", "**/.pytest_cache", "**/.mypy_cache",
                     "**/.ruff_cache", "**/*.ipynb", "**/.ipynb_checkpoints", "**/.DS_Store")


def deny_dockerignore():
    """The deny-list .dockerignore the build writes beside a repository's own Dockerfile when it has none; never committed."""
    return ("# Written by Fast Deploy for this build only (docs/ROADMAP.md 14.2): the repository has no .dockerignore of its own.\n"
            + "\n".join(DENY_DOCKERIGNORE) + "\n")


def excluded(relative):
    """Whether a path inside the service's folder stays out of an image the platform makes (any segment may match)."""
    import fnmatch
    return any(fnmatch.fnmatch(part, pattern) for part in relative.split("/") for pattern in BUILD_EXCLUDE)


def start_command(entrypoint):
    """build.entrypoint as an exec-form command: its words, or a shell running it when it reads a variable ($PORT) or chains
    commands (a && b, D49, 22.2): the platform's images start under tini, which passes SIGTERM to the shell's last command."""
    if "&&" in entrypoint:
        parts = [part.strip() for part in entrypoint.split("&&")]
        return ["sh", "-c", " && ".join(parts[:-1] + [f"exec {parts[-1]}"])]
    return ["sh", "-c", f"exec {entrypoint}"] if "$" in entrypoint else shlex.split(entrypoint)


# System packages a Python service lists for the platform's image (docs/ROADMAP.md 20.3): Heroku's Aptfile, Railpack's and
# Streamlit's packages.txt, apt.txt. Debian package names only; anything else in the file stops the build rather than
# reaching a shell.
SYSTEM_PACKAGE_FILES = ("apt.txt", "Aptfile", "packages.txt", "apt-packages.txt")
SYSTEM_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,99}(?::[a-z0-9]+)?(?:=[A-Za-z0-9+:~.-]+)?")


def system_packages(text):
    """The package names of an apt.txt, Aptfile or packages.txt: one per line, comments and blank lines left out. Raises
    ValueError on a line that is not a Debian package name (a URL, a shell word, a .deb file)."""
    packages = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for word in line.split():
            if not SYSTEM_PACKAGE.fullmatch(word):
                raise ValueError(f"{word!r} is not a Debian package name; the platform installs named packages only (no URLs or .deb files)")
            packages.append(word)
    return list(dict.fromkeys(packages))


def single_file_packages(lock_text):
    """Packages of a hash-locked requirements file that have exactly one distribution file: usually a source distribution
    alone, which needs a compiler (20.3). A wheel-only package counted here costs a build stage and nothing else."""
    found = []
    for block in re.split(r"\n(?=\S)", lock_text or ""):
        name = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", block)
        if name and block.count("--hash=") == 1:
            found.append(normalize(name.group(1)))
    return found


# The base image's build tools, uninstalled from the final image unless the service's lock names them (1.22.5): pip last, so
# it removes the others before itself.
BUILD_TOOLS = ("setuptools", "wheel", "pip")


def kept_build_tools(lock_text):
    """Which of pip, setuptools and wheel the service's hash-locked requirements pin: a package that imports pkg_resources
    or declares setuptools keeps it in the image."""
    pinned = {normalize(match.group(1)) for match in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", lock_text or "")}
    return [name for name in BUILD_TOOLS if name in pinned]


def build_facts(folder):
    """What the platform's Python Dockerfile needs to know about a build context: the system packages its files list, whether
    the lock holds a source distribution (a build stage with a compiler), whether it is a Django project (collectstatic), and
    which of the base image's build tools the lock names (kept in the image)."""
    folder = Path(folder)
    packages = []
    for name in SYSTEM_PACKAGE_FILES:
        if (folder / name).is_file():
            packages += system_packages((folder / name).read_text(errors="replace"))
    lock = (folder / "requirements.lock").read_text(errors="replace") if (folder / "requirements.lock").is_file() else ""
    return {"system": list(dict.fromkeys(packages)), "sources": single_file_packages(lock), "django": (folder / "manage.py").is_file(),
            "keep": kept_build_tools(lock)}


def python_platform_dockerfile(spec, locked, facts=None):
    """The platform's own Dockerfile for a Python contract (templates/repo/Dockerfile.platform): python:X.Y-slim by digest, the
    listed system packages installed, a build stage with a compiler when the lock holds a source distribution or system
    packages are listed (only its virtual environment enters the image), Django's static files collected at build, a numeric
    non-root user, and the contract's entrypoint as the command (docs/ROADMAP.md 13.3 and 20.3)."""
    facts = facts or {"system": [], "sources": [], "django": False}
    keep = [name for name in BUILD_TOOLS if name in (facts.get("keep") or [])]
    drop = [name for name in BUILD_TOOLS if name not in keep]
    requirements = "requirements.lock" if locked else "requirements.txt"
    install = ("--require-hashes -r " if locked else "-r ") + requirements
    system = ""
    if facts["system"]:
        system = ("# System packages the repository lists (apt.txt, Aptfile or packages.txt), as Heroku and Railpack install them.\n"
                  "RUN apt-get update && apt-get install -y --no-install-recommends " + " ".join(facts["system"])
                  + " \\\n && rm -rf /var/lib/apt/lists/*\n")
    compile_stage = bool(facts["system"] or facts["sources"])
    if compile_stage:
        why = ("the lock holds a source distribution (" + ", ".join(facts["sources"][:4]) + ")" if facts["sources"] else "system packages are listed")
        build_stage = (f"# A build stage with a compiler (docs/ROADMAP.md 20.3): {why}, so the dependencies are built here and only the\n"
                       "# virtual environment enters the image below.\n"
                       f"FROM python:{spec.python}-slim@{BASE_IMAGES[spec.python]} AS build\n"
                       "ENV PYTHONUNBUFFERED=1 HOME=/tmp\n"
                       "RUN apt-get update && apt-get install -y --no-install-recommends build-essential " + " ".join(facts["system"])
                       + " \\\n && rm -rf /var/lib/apt/lists/*\n"
                       "WORKDIR /app\n"
                       f"COPY {requirements} ./\n"
                       f"RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir {install}\n\n")
        install_lines = "COPY --from=build /opt/venv /opt/venv\n"
        venv_path = " PATH=/opt/venv/bin:$PATH"
        # The service's packages live in the virtual environment; the base interpreter's three are never the service's.
        venv_trim = f"/opt/venv/bin/python -m pip uninstall -y {' '.join(drop)} && " if drop else ""
        trim_line = f"RUN {venv_trim}/usr/local/bin/python -m pip uninstall -y setuptools wheel pip\n"
    else:
        build_stage, venv_path = "", ""
        install_lines = ("# Dependencies before the code: a code change then rebuilds one layer instead of reinstalling every dependency.\n"
                         f"COPY {requirements} ./\nRUN pip install --no-cache-dir {install}\n")
        trim_line = f"RUN python -m pip uninstall -y {' '.join(drop)}\n" if drop else ""
    trim = ("# No package manager stays in the image, as in the platform's Node.js image: the base image's pip, setuptools and wheel are\n"
            "# build tools the service never runs, and the scan reads their libraries as the service's (found live: wheel and\n"
            "# jaraco-context in python:3.11-slim, two fixable high findings that stopped a release; docs/CHANGES.md 1.22.5).\n")
    if keep:
        trim += f"# The lock names {' and '.join(keep)}: the service keeps {'it' if len(keep) == 1 else 'them'}.\n"
    trim += trim_line
    collectstatic = ""
    if facts["django"]:
        collectstatic = ("# Django's static files, collected at build as Heroku does (docs/ROADMAP.md 20.3); a settings module that needs\n"
                         "# a value at import stops the build here, and the contract's env is the place to give it.\n"
                         "RUN python manage.py collectstatic --noinput\n")
    return fill(template("Dockerfile.platform"), PYTHON=spec.python, RESOLVED=BASE_IMAGES_RESOLVED, DIGEST=BASE_IMAGES[spec.python],
                BUILD_STAGE=build_stage, VENV_PATH=venv_path, SYSTEM=system, INSTALL=install_lines, COLLECTSTATIC=collectstatic, TRIM=trim,
                PORT=spec.port, CMD=json.dumps(start_command(spec.build.entrypoint)))


# The package managers the platform installs by version and checksum (docs/ROADMAP.md 21.1), never through Corepack (Node.js
# 25 and later no longer ship it): pnpm, Yarn 2+ (its npm package @yarnpkg/cli-dist), Yarn 1 and Bun's Linux x64 binary
# (@oven/bun-linux-x64, the package the bun package's own install script would fetch), each from the npm registry at the
# integrity it published, installed with scripts off and checked against that integrity before it runs. The weekly pin
# refresh keeps each on the newest release of its line at least seven days old (labdeploy/pins.py). Resolved 2026-10-20.
MANAGER_PINS = {
    "pnpm": ("12.12.1", "sha512-t/mJNMCx8ppl9dpS8tf5lOf6rVc//d3qFJf7Tcta7AUVkO58fZWjgXK0v64QOluD7fLuhm4Rcxwsr6lgY2EKxQ=="),  # published 2026-10-11
    "yarn": ("4.18.1", "sha512-EH+A4TxAtBWXKE0DGQxW9Bo9crXHLJ73mfxjTQJuv8XonNWWTpei8BQ5JjczHJNU9GROTycwMFn8iOvRIZXUcA=="),  # published 2026-09-24
    "yarn1": ("1.22.22", "sha512-prL3kGtyG7o9Z9Sv8IPfBNrWTDmXB4Qbes8A9rEzt6wkJV8mUvoirjU0Mp3GGAU06Y0XQyA3/2/RQFVuK7MTfg=="),  # published 2024-03-09
    "bun": ("1.4.3", "sha512-RArgnpjeXjaGzjp5h7Rs89cUYJutO8IyqR3itE8OBaBzEt8KNm2jp/YozxBHq4eFovwrQLryeSwTAcmiT+zcoA=="),  # published 2026-10-10
}
# The npm package each manager comes from, and the file node runs for it (the wrapper the platform writes calls that file).
MANAGER_PACKAGES = {"pnpm": ("pnpm", "bin/pnpm.cjs", "node"), "yarn": ("@yarnpkg/cli-dist", "bin/yarn.js", "node"), "yarn1": ("yarn", "bin/yarn.js", "node"),
                    "bun": ("@oven/bun-linux-x64", "bin/bun", "")}
NODE_CONFIG_FILES = ("tsconfig.json", "tsconfig.base.json", "tsconfig.build.json", "next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs",
                     "svelte.config.js", "nest-cli.json", ".yarnrc.yml", "pnpm-workspace.yaml")


def pinned_package_check(name, version, folder):
    """JavaScript that exits 4 unless the package `name` installed in `folder` is `version` with the integrity given as its
    first argument, read from the lockfile npm wrote there. An empty first argument holds the version only: npm itself
    checked the tarball against the integrity the registry publishes (a packageManager release without a sha512)."""
    return (f"const e = (require('{folder}/package-lock.json').packages || {{}})['node_modules/{name}'] || {{}}; "
            f"if (e.version !== '{version}' || !e.integrity || (process.argv[1] && e.integrity !== process.argv[1])) {{ console.error('{name} ' + e.version + "
            "' does not match its pinned version and integrity'); process.exit(4); }")


def manager_release(manager, release=None):
    """(version, integrity) a manager is installed at: the repository's packageManager release when the reader chose it
    (integrity "" when the field carries no sha512: the registry's integrity, which npm checks, holds it), else the pin."""
    if release and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", str(release.get("version") or "")):
        integrity = release.get("integrity") or ""
        if integrity and not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", integrity):
            raise ValueError("packageManager's hash is not a sha512 the platform can check")
        return release["version"], integrity
    return MANAGER_PINS[manager]


def manager_command(manager, folder):
    """How the manager installed in `folder` is run: node on its command-line file, or Bun's binary itself."""
    package, bin_path, runner = MANAGER_PACKAGES[manager]
    return f"{runner} {folder}/node_modules/{package}/{bin_path}".strip()


def manager_install_lines(manager, folder, wrapper=True, release=None):
    """Shell lines that install a package manager into `folder` by version and integrity, with every script off, and check
    it against that integrity. With `wrapper`, `folder`/bin/<pnpm|yarn|bun> is written for the pipeline's PATH; the image
    runs the command-line file directly (manager_command). npm needs nothing."""
    if manager == "npm":
        return []
    package, bin_path, runner = MANAGER_PACKAGES[manager]
    version, integrity = manager_release(manager, release)
    lines = [f"mkdir -p {folder}/bin",
             f"echo '{{\"private\": true}}' > {folder}/package.json",
             f"cd {folder} && npm install --no-audit --no-fund --ignore-scripts --loglevel=error {package}@{version} >&2 && cd - > /dev/null",
             f"node -e {shlex.quote(pinned_package_check(package, version, folder))} {shlex.quote(integrity)}"]
    if not runner:
        lines.append(f"chmod +x {folder}/node_modules/{package}/{bin_path}")
    if wrapper:
        command = "yarn" if manager == "yarn1" else manager
        lines += [f"printf '%s\\n' '#!/bin/sh' 'exec {manager_command(manager, folder)} \"$@\"' > {folder}/bin/{command}", f"chmod +x {folder}/bin/{command}"]
    return lines


def image_command(manager, text, folder="/opt/manager"):
    """A manager command as the image runs it: the manager's name replaced by its installed file (npm stays npm)."""
    word = "yarn" if manager == "yarn1" else manager
    if manager == "npm" or not text.startswith(word + " "):
        return text
    return manager_command(manager, folder) + text[len(word):]


def node_facts(folder, path="", line="24"):
    """What a Node.js build needs to know about a checkout (21.1 to 21.4): the manager its lockfile names, where that lockfile
    is (the service's folder, or the workspace root above it), the manager's commands, and the build plan. `folder` is the
    checkout root when `path` names a service inside a workspace, else the service's folder; `line` the Node.js line."""
    from . import node as nodejs
    root = Path(folder)
    service = root / path if path else root
    lockfile, where = None, service
    for candidate in (service, root):
        found = next((name for name in (*nodejs.LOCKFILES, *nodejs.MANAGER_LOCKFILES) if (candidate / name).is_file()), None)
        if found:
            lockfile, where = found, candidate
            break
    manager = nodejs.MANAGER_LOCKFILES.get(lockfile, "npm") if lockfile else "npm"
    if manager == "yarn" and "__metadata:" not in (where / "yarn.lock").read_text(errors="replace"):
        manager = "yarn1"
    try:
        package = json.loads((service / "package.json").read_text(errors="replace")) if (service / "package.json").is_file() else {}
    except ValueError:
        package = {}
    package = package if isinstance(package, dict) else {}
    texts = {name: (service / name).read_text(errors="replace") for name in NODE_CONFIG_FILES if (service / name).is_file()}
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    start = nodejs.start_words(scripts["start"])[0] if isinstance(scripts.get("start"), str) else None
    plan = nodejs.build_plan(package, lambda item: (service / item).exists(), texts, manager, line, start_words=(start or {}).get("words"))
    if plan.get("copy"):
        plan["copy"] = [item for item in plan["copy"] if item.startswith(".next/") or (service / item).exists()]
    commands = nodejs.MANAGER_COMMANDS[manager]
    release = None
    if manager != "npm":
        for candidate in (package, json.loads((root / "package.json").read_text(errors="replace")) if path and (root / "package.json").is_file() else None):
            spec = nodejs.manager_spec(candidate if isinstance(candidate, dict) else None)
            if spec and spec["manager"] == manager:
                release = {"version": spec["version"], "integrity": spec["integrity"] or ""}
                break
    version, integrity = manager_release(manager, release) if manager != "npm" else ("", "")
    return {"manager": manager, "lockfile": lockfile, "lock_dir": where.relative_to(root).as_posix() if where != root else ".",
            "member": path if where == root and path else "", "frozen": commands["frozen"], "production": commands["production"],
            "prune": commands["prune"], "rebuild": commands["rebuild"], "run": commands["run"], "test": commands["run"] + " test", "title": commands["title"],
            "plan": plan, "version": version, "integrity": integrity, "release": release}


def node_platform_dockerfile(spec, facts):
    """The platform's own Dockerfile for a Node.js contract. npm without a build: the single stage of the drafted Dockerfile.
    Otherwise templates/repo/Dockerfile.node.platform: node:<line>-trixie-slim by digest; the manager the lockfile names,
    installed by version and integrity (21.1); a build stage with the development dependencies, the build and the prune
    (21.2, 21.3), or a workspace installed and pruned to one package (21.4); the final image with the production
    dependencies alone and no package manager; a numeric non-root user; node itself as the command, the start the contract
    names (build.entrypoint) being the one the build makes."""
    from . import node as nodejs
    manager, plan, member = facts["manager"], facts.get("plan") or {}, facts.get("member") or ""
    commands = nodejs.MANAGER_COMMANDS[manager]
    build_words = plan.get("build")
    run = lambda text: image_command(manager, text)  # noqa: E731 - a one-line rename for readability below
    manager_lines = ""
    if manager != "npm":
        package, bin_path, runner = MANAGER_PACKAGES[manager]
        version, integrity = manager_release(manager, facts.get("release"))
        steps = ["echo '{\"private\": true}' > package.json",
                 f"npm install --no-audit --no-fund --ignore-scripts --loglevel=error {package}@{version} >&2",
                 f"node -e {shlex.quote(pinned_package_check(package, version, '/opt/manager'))} {shlex.quote(integrity)}"]
        if not runner:
            steps.append(f"chmod +x /opt/manager/node_modules/{package}/{bin_path}")
        manager_lines = (f"# {commands['title']} {version}, the manager the lockfile names (docs/ROADMAP.md 21.1), installed by version and integrity "
                         "with scripts off, never through Corepack.\nWORKDIR /opt/manager\nRUN " + " \\\n && ".join(steps) + "\n")
    workdir = f"/app/{member}" if member else "/app"
    install = run(commands["frozen"] if build_words else commands["production"])
    if member and manager == "pnpm":
        install += f" --filter ./{member}..."
    rebuild, joined = "", ""
    if spec.build.scripts:
        if member:
            rebuild = f"WORKDIR {workdir}\nRUN {run(commands['rebuild'])} " + " ".join(spec.build.scripts) + "\nWORKDIR /app\n"
        else:
            joined = f" \\\n && {run(commands['rebuild'])} " + " ".join(spec.build.scripts)
    install_lines = (f"# The lockfile installed frozen, with every install script off{'; only build.scripts runs its own afterwards' if spec.build.scripts else ''}.\n"
                     f"RUN {install}{joined}\n{rebuild}")
    build_lines = ""
    if build_words:
        words = run(" ".join(build_words)) if build_words[0] in ("npm", "pnpm", "yarn", "bun") else " ".join(build_words)
        build_lines = (f"# The build (docs/ROADMAP.md 21.2): {plan.get('why') or 'the build script'}. NODE_ENV is unset here, so the development "
                       "dependencies the build needs are there; the image below sets it.\n"
                       + (f"WORKDIR {workdir}\n" if member else "") + f"RUN {words}\n")
    if member and manager == "pnpm":
        prune_lines = ("# One package out of the workspace (21.4): pnpm deploy copies it with its production dependencies alone.\n"
                       f"WORKDIR /app\nRUN {run(f'pnpm --filter ./{member} deploy --prod --legacy /out')}\n")
        final_copy, final_workdir = "COPY --from=build /out /app\n", "/app"
    elif plan.get("copy"):
        prune_lines = ""
        copies = []
        for item in plan["copy"]:
            target = "/app" if item == ".next/standalone" else f"/app/{item}"
            copies.append(f"COPY --from=build {workdir}/{item} {target}\n")
        final_copy, final_workdir = "".join(copies), "/app"
    else:
        prune_lines = ("# The development dependencies leave before the image is made (21.2).\n"
                       + ("WORKDIR /app\n" if member and build_words else "") + f"RUN {run(commands['prune'])}\n") if build_words else ""
        final_copy, final_workdir = "COPY --from=build /app /app\n", workdir
    return fill(template("Dockerfile.node.platform"), NODE=spec.node, RESOLVED=NODE_IMAGES_RESOLVED, DIGEST=NODE_IMAGES[spec.node],
                MANAGER=manager_lines, INSTALL=install_lines, BUILD=build_lines, PRUNE=prune_lines, FINAL_COPY=final_copy, WORKDIR=final_workdir,
                PORT=spec.port, CMD=json.dumps(start_command(spec.build.entrypoint)))


def platform_dockerfile(spec, locked, lockfile="package-lock.json", facts=None):
    """The platform's own Dockerfile for a contract with build.recipe: platform: the drafted Dockerfile's steps (python:X.Y-slim
    or node:<line>-trixie-slim by digest, dependencies before the code, a numeric non-root user) with the contract's entrypoint as
    its command. `locked` says whether a Python service has requirements.lock; `lockfile` names a Node.js service's lock;
    `facts` (build_facts or node_facts) says what the build context lists and holds."""
    if spec.build.recipe != "platform" or not spec.build.entrypoint:
        raise ValueError("The platform makes a Dockerfile only for build.recipe: platform with build.entrypoint")
    if spec.runtime == "static":
        return static_dockerfile(spec, facts or {"manager": "npm", "lockfile": lockfile, "plan": {}, "member": ""})
    if spec.runtime != "node-http":
        return python_platform_dockerfile(spec, locked, facts)
    facts = facts or {"manager": "npm", "lockfile": lockfile, "plan": {}, "member": ""}
    return node_platform_dockerfile(spec, facts)


def static_dockerfile(spec, facts):
    """The platform's image for a front-end site (runtime: static, docs/ROADMAP.md D55, 22.4): the manager the lockfile names,
    the full install with scripts off, the build script, then Caddy serving build.output from a final image with no source,
    no node_modules and no package manager, under tini, as a user that is not root. Unknown paths fall back to index.html,
    as a single-page application expects."""
    from . import node as nodejs
    manager = facts.get("manager") or "npm"
    commands = nodejs.MANAGER_COMMANDS[manager]
    run = lambda text: image_command(manager, text)  # noqa: E731
    manager_lines = ""
    if manager != "npm":
        package, bin_path, runner = MANAGER_PACKAGES[manager]
        version, integrity = manager_release(manager, facts.get("release"))
        steps = ["echo '{\"private\": true}' > package.json",
                 f"npm install --no-audit --no-fund --ignore-scripts --loglevel=error {package}@{version} >&2",
                 f"node -e {shlex.quote(pinned_package_check(package, version, '/opt/manager'))} {shlex.quote(integrity)}"]
        if not runner:
            steps.append(f"chmod +x /opt/manager/node_modules/{package}/{bin_path}")
        manager_lines = (f"# {commands['title']} {version}, the manager the lockfile names, installed by version and integrity with scripts off.\n"
                         "WORKDIR /opt/manager\nRUN " + " \\\n && ".join(steps) + "\n")
    install = ("# The lockfile installed frozen, with every install script off, then the site's build (its build script).\n"
               f"RUN {run(commands['frozen'])} \\\n && {run(commands['run'] + ' build')}\n")
    build = ""
    return fill(template("Dockerfile.static"), NODE=spec.node, RESOLVED=NODE_IMAGES_RESOLVED, DIGEST=NODE_IMAGES[spec.node], MANAGER=manager_lines,
                INSTALL=install, BUILD=build, OUTPUT=spec.build.output.strip("/"), PORT=spec.port, CMD=json.dumps(start_command(spec.build.entrypoint)))


STATIC_ENTRYPOINT = "caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"


def node_lockfile(folder):
    """The lockfile npm ci reads in a folder: npm-shrinkwrap.json before package-lock.json; None when there is neither."""
    return next((name for name in ("npm-shrinkwrap.json", "package-lock.json") if (Path(folder) / name).is_file()), None)


def render_build(app_dir, out_dir, spec, path=""):
    """Copy the service's files into a fresh build context through the deny-list, then write the platform's Dockerfile beside
    them. Symbolic links are left out, so nothing outside the service's folder can enter. With `path`, `app_dir` is the
    repository root and the service lives in that folder of it (build.context: repository, a workspace member, 21.4).
    Returns the copied paths."""
    import shutil
    source, target = Path(app_dir).resolve(strict=True), Path(out_dir)
    path = clean_path(path)
    if path and not (source / path / "package.json").is_file():
        raise ValueError(f"The service's folder {path} holds no package.json")
    target.mkdir(parents=True, exist_ok=False)
    copied = []
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source).as_posix()
        if item.is_symlink() or not item.is_file() or excluded(relative):
            continue
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target / relative)
        copied.append(relative)
    if spec.runtime in ("node-http", "static"):
        facts = node_facts(target, path)
        service = target / path if path else target
        if not (service / "package.json").is_file() or facts["lockfile"] is None:
            raise ValueError("The service needs package.json and a lockfile (package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml, yarn.lock or "
                             "bun.lock, its own or its workspace root's), and the contract names no lock the platform made")
        (target / "Dockerfile").write_text(platform_dockerfile(spec, True, facts["lockfile"], facts))
        return copied
    locked = (target / "requirements.lock").is_file()
    if not locked and not (target / "requirements.txt").is_file():
        raise ValueError("The service has neither requirements.lock nor requirements.txt, and the contract names no lock the platform made")
    (target / "Dockerfile").write_text(platform_dockerfile(spec, locked, facts=build_facts(target)))
    return copied


def descriptor():
    """The buildpacks' project descriptor (project.toml, schema 0.2) the platform builds with: the same deny-list, so a
    buildpacks image never carries secrets, tests, notebooks or caches either."""
    patterns = ",\n".join(f'  "{pattern}"' for pattern in BUILD_EXCLUDE)  # a Dockerfile is no input of the buildpacks either
    return ('# Written by Fast Deploy for the buildpacks (docs/ROADMAP.md 13.3); a project.toml of the repository\'s own is not read.\n'
            '[_]\nschema-version = "0.2"\n\n[io.buildpacks]\nexclude = [\n' + patterns + "\n]\n")


def export_container(python_version, dev=False):
    """The docker command that exports uv.lock as a hash-locked requirements file in a container of the application's Python:
    pyproject.toml and uv.lock arrive base64-encoded on stdin, uv is installed by hash, and nothing is resolved again."""
    pin = f"uv=={UV_VERSION} " + " ".join(f"--hash=sha256:{digest}" for digest in UV_HASHES)
    groups = "--all-groups" if dev else "--no-dev"
    script = "; ".join((
        "set -eu",
        "base64 -d > input.tar",
        "mkdir project && tar -xf input.tar -C project",
        f"printf '%s\\n' {shlex.quote(pin)} > uv-requirements.txt",
        "python -m venv uv-venv",
        "uv-venv/bin/pip install --quiet --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r uv-requirements.txt",
        f"cd project && ../uv-venv/bin/uv export --frozen {groups} --no-emit-project --no-emit-workspace --format requirements-txt --quiet -o ../requirements.lock",
        "cat ../requirements.lock",
    ))
    return ["docker", "run", "--rm", "-i", *CONTAINER_LIMITS, "--user", "65532:65532", "-e", f"HOME={CONTAINER_HOME}", "-e", "UV_NO_CACHE=1", "-w", CONTAINER_HOME,
            base_image(python_version), "sh", "-c", script]


def export_uv_lock(pyproject_text, uv_lock_text, python_version=DEFAULT_PYTHON, runner=docker_runner, dev=False):
    """uv.lock as a hash-locked requirements file, exported without resolving again (`uv export --frozen`): the main
    dependencies, or every group when `dev` (for the tests). Returns {"text", "error"} like compile_lock."""
    import base64
    import io
    import tarfile
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in (("pyproject.toml", pyproject_text), ("uv.lock", uv_lock_text)):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    try:
        output = runner(export_container(python_version, dev), base64.b64encode(buffer.getvalue()).decode())
    except FileNotFoundError:
        return {"text": None, "error": "Docker is not installed on this machine"}
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        return {"text": None, "error": f"uv export failed: {detail[-1][:300] if detail else exc}"}
    except Exception as exc:  # noqa: BLE001 - a timeout or a daemon that is not running is reported the same way
        return {"text": None, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if "uv export" not in output or ("==" in output and "--hash=sha256:" not in output):
        return {"text": None, "error": "the container did not return a hash-locked requirements file"}
    return {"text": output, "error": None}


def main(argv=None):
    """The build steps' entry points (templates/deploy-gcp.yml): render-build, platform-dockerfile and descriptor."""
    import argparse
    import os
    import sys

    from .models import parse_spec
    parser = argparse.ArgumentParser(prog="python -m labdeploy.scaffold")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("render-build", help="copy the service through the deny-list and write the platform's Dockerfile")
    build.add_argument("app_dir")
    build.add_argument("out_dir")
    build.add_argument("--spec", required=True)
    build.add_argument("--path", default="", help="the service's folder inside app_dir, when app_dir is the repository root (build.context: repository)")
    show = commands.add_parser("platform-dockerfile", help="print the platform's Dockerfile for a contract")
    show.add_argument("app_dir")
    show.add_argument("--spec", required=True)
    show.add_argument("--path", default="")
    facts_command = commands.add_parser("node-facts", help="print, as KEY=value lines for the shell, how a Node.js service installs, builds and tests")
    facts_command.add_argument("app_dir")
    facts_command.add_argument("--path", default="")
    facts_command.add_argument("--node", default="24")
    install = commands.add_parser("manager-install", help="print the shell lines that install a package manager by version and integrity")
    install.add_argument("manager")
    install.add_argument("folder")
    install.add_argument("--version", default="", help="an exact release (package.json's packageManager), else the platform's pin")
    install.add_argument("--integrity", default="", help="the sha512 integrity packageManager carries, if any")
    commands.add_parser("descriptor", help="print the buildpacks' project descriptor")
    commands.add_parser("deny-dockerignore", help="print the deny-list .dockerignore for a repository without one")
    args = parser.parse_args(argv)
    try:
        if args.command == "descriptor":
            print(descriptor(), end="")
            return 0
        if args.command == "deny-dockerignore":
            print(deny_dockerignore(), end="")
            return 0
        if args.command == "manager-install":
            if args.manager not in MANAGER_PINS and args.manager != "npm":
                raise ValueError(f"{args.manager} is not a package manager the platform installs (pnpm, yarn, yarn1, bun)")
            if not re.fullmatch(r"[A-Za-z0-9_./-]+", args.folder):
                raise ValueError("the folder is a plain path")
            release = {"version": args.version, "integrity": args.integrity} if args.version else None
            print("\n".join(manager_install_lines(args.manager, args.folder, release=release)))
            return 0
        if args.command == "node-facts":
            facts = node_facts(args.app_dir, clean_path(args.path), args.node if args.node in NODE_IMAGES else "24")
            plan = facts["plan"]
            if plan.get("problem"):
                raise ValueError(plan["problem"])
            for key in ("manager", "lockfile", "lock_dir", "member", "frozen", "production", "rebuild", "run", "test", "title", "version"):
                value = str(facts[key] or "")
                if not re.fullmatch(r"[A-Za-z0-9 _./@=:-]*", value):
                    raise ValueError(f"{key} holds characters the build cannot pass on")
                print(f"{key.upper()}={shlex.quote(value)}")
            if facts["integrity"] and not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", facts["integrity"]):
                raise ValueError("the manager's integrity is not a sha512")
            print(f"INTEGRITY={shlex.quote(facts['integrity'] or '')}")
            print(f"BUILD={shlex.quote(' '.join(plan.get('build') or []))}")
            print(f"START={shlex.quote(' '.join(plan.get('start') or []))}")
            return 0
        spec = parse_spec(Path(args.spec).read_text())
        if args.command == "render-build":
            copied = render_build(args.app_dir, args.out_dir, spec, args.path)
            print(f"Build context: {len(copied)} files copied through the platform's deny-list, and the platform's Dockerfile", file=sys.stderr)
        elif spec.runtime in ("node-http", "static"):
            path = clean_path(args.path)
            print(platform_dockerfile(spec, True, node_lockfile(args.app_dir) or "package-lock.json", node_facts(args.app_dir, path, spec.node)), end="")
        else:
            lock = os.environ.get("PLATFORM_LOCK")
            locked = (Path(args.app_dir) / "requirements.lock").is_file() or bool(lock and Path(lock).is_file())
            facts = build_facts(args.app_dir)
            if lock and Path(lock).is_file() and not (Path(args.app_dir) / "requirements.lock").is_file():
                facts["sources"] = single_file_packages(Path(lock).read_text(errors="replace"))  # the lock the build step copies in later
            print(platform_dockerfile(spec, locked, node_lockfile(args.app_dir) or "package-lock.json", facts), end="")
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
