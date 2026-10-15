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
from .models import AppSpec

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "repo"
PYTHONS = get_args(AppSpec.model_fields["python"].annotation)
DEFAULT_PYTHON = AppSpec.model_fields["python"].default
DEFAULT_PORT = AppSpec.model_fields["port"].default
DEFAULT_HEALTH = AppSpec.model_fields["health_path"].default

# python:X.Y-slim multi-architecture index digests (Debian trixie: 3.11.17, 3.12.15, 3.13.16, 3.14.8 and 3.15.0), resolved
# on the date below with `docker buildx imagetools inspect python:X.Y-slim`: the top "Digest:" line, which is the index
# every architecture shares, never a per-platform manifest. To refresh, run that command for each version, replace the
# digests and the date after review, and run tests/test_scaffold.py. A repository keeps its own pin once drafted; there,
# Dependabot's docker updates refresh it.
BASE_IMAGES_RESOLVED = "2026-10-14"
BASE_IMAGES = {
    "3.11": "sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce",
    "3.12": "sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",
    "3.13": "sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f",
    "3.14": "sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170",
    "3.15": "sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619",
}
# Exact versions checked on PyPI on 2026-10-14 (the demo app and the platform pin the same test tools).
DEV_PINS = {"pytest": "9.1.1", "httpx": "0.28.1"}
SERVER_PINS = {"uvicorn": "0.54.0", "gunicorn": "26.2.0"}
# uv for compile_lock, installed into the container only by these hashes: its manylinux wheels for x86_64 and aarch64
# (Docker on an Apple silicon Mac runs aarch64 Linux). 0.12.24 (released 2026-10-08) rather than the 0.13 line, whose
# first release was five days old when this was pinned and changed archive handling.
UV_VERSION = "0.12.24"
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


def base_image(python_version):
    """The pinned base image for a supported Python: python:X.Y-slim@<multi-architecture index digest>."""
    if python_version not in BASE_IMAGES:
        raise ValueError(f"Python {python_version} is not supported; choose one of {', '.join(PYTHONS)}")
    return f"python:{python_version}-slim@{BASE_IMAGES[python_version]}"


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
def module_of(relative):
    """("", "app.main") for app/main.py; ("src", "pkg.main") for src/pkg/main.py; None when it is not importable by name."""
    parts = relative[:-3].split("/")
    app_dir = ""
    if parts[0] == "src" and len(parts) > 1:
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


def find_applications(sources):
    """Module-level application objects (constructed, or made by a factory call) and, separately, factory functions."""
    objects, factories = [], []
    for relative in sorted(sources, key=rank):
        located = module_of(relative)
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
    objects, factories = find_applications(sources)
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
    return ["docker", "run", "--rm", "-i", "--user", "65532:65532", "-e", f"HOME={CONTAINER_HOME}", "-e", "UV_NO_CACHE=1", "-w", CONTAINER_HOME,
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


def platform_draft(repo, name, found, dockerfile_text, planned):
    """The analyzer's draft, so it stays what analyze_repository proposes, with the detected Python and health path; reviewed: false."""
    files = {key: text for key, text in (("requirements.txt", repo.text("requirements.txt")), ("pyproject.toml", repo.text("pyproject.toml")),
                                         ("Dockerfile", dockerfile_text)) if text}
    analysis = analyze(files, name, sorted(planned))
    draft = dict(analysis["draft_spec"], python=found["python"], health_path=found["health_path"], port=found["port"], reviewed=False)
    if found["interface"] == "wsgi":
        draft["concurrency"] = min(draft["concurrency"], 8)  # one gunicorn worker with eight threads serves eight requests at once
    spec = AppSpec.model_validate(draft)
    return fill(template("platform.yaml.tmpl"), SPEC=yaml.safe_dump(spec.model_dump(), sort_keys=False).rstrip("\n")), analysis


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


def service_name(name, repo):
    """(name, note): the explicit name, refused when Cloud Run would refuse it, else one made from the folder or pyproject.toml."""
    if name is not None:
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            raise ValueError(f"'{name}' is not a valid service name: 3 to 21 characters, a lowercase letter first, then lowercase letters, "
                             "digits or dashes, not ending in a dash")
        return name, None
    project = (pyproject_data(repo.text("pyproject.toml")).get("project") or {}).get("name")
    for candidate in (repo.path.rsplit("/", 1)[-1] if repo.path else None, project if isinstance(project, str) else None):
        slug = re.sub(r"[^a-z0-9]+", "-", (candidate or "").lower())
        slug = re.sub(r"^[^a-z]+", "", slug)[:21].strip("-")
        if SERVICE_NAME.fullmatch(slug):
            return slug, None
    return "my-service", "No service name was given or found, so platform.yaml says my-service: choose the real name (3 to 21 characters)."


def ci_file(repo, name):
    """(file to draft or None, note, workflows that already run these tests). A service at the root gets ci.yml; one in a
    folder gets ci.yml while the repository has none, else ci-<name>.yml, unless a workflow already runs pytest there."""
    workflows = sorted(item for item in repo.paths if WORKFLOW.fullmatch(item) and not PLATFORM_WORKFLOW.fullmatch(item))
    covering = [item for item in workflows if "pytest" in repo.texts.get(item, "") and (not repo.path or repo.path in repo.texts[item])]
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


def draft_files(paths, texts, path="", name=None, lock_text=None):
    """D26: the files docs/DEVELOPER-CONTRACT.md asks for that the service lacks, drafted for one pull request.

    `paths` is the repository's file list as its Git tree gives it (relative to the root); `texts` maps some of those paths
    to their text (files_to_read says which); `path` is the service's folder ("" for the root); `name` the service name
    (derived when absent); `lock_text` a requirements.lock that compile_lock produced, drafted when the service has none.

    Returns {"files": {repository path: {"content", "purpose"}}, "existing": [the contract's files already there, left
    untouched], "review": [what a person must decide], "detected": detect()'s answer, "lock_command": the command that
    compiles the lock, "analysis": the analyzer's result when platform.yaml was drafted}. The service's files go under
    `path`; .github/ files and SECURITY.md go at the repository root. A file that exists is never in "files"."""
    repo = Repository(paths, texts, path)
    if not any(item.endswith(".py") for item in repo.local_paths) and not repo.has("requirements.txt") and not repo.has("pyproject.toml"):
        where = f"{repo.path}/" if repo.path else "the repository root"
        return {"files": {}, "existing": [], "review": [f"No Python code or requirements were found at {where}: the platform deploys Python HTTP "
                                                       "services only, so nothing was drafted."], "detected": None, "lock_command": None, "analysis": None}
    found = _detect(repo)
    name, name_note = service_name(name, repo)
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

    if not present(".dockerignore"):
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

    # pytest refuses two test modules with the same name, so any test_health.py under the service counts as the health test.
    health_tests = sorted(item for item in repo.local_paths if item.rsplit("/", 1)[-1] == "test_health.py")
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


def files_to_read(paths, path="", limit=40):
    """The repository paths whose text draft_files uses, so a caller fetches only those: the configuration first, then the
    Python files most likely to hold the application object and its health route."""
    repo = Repository(paths, {}, path)
    names = ("requirements.txt", "requirements.lock", "pyproject.toml", "Dockerfile", "dockerfile", ".python-version", "runtime.txt",
             "platform.yaml", "manage.py")
    wanted = [repo.at(name) for name in names if repo.has(name)]
    if repo.path:
        wanted += [name for name in (".python-version", "pyproject.toml") if name in repo.paths]
    wanted += [item for item in DEPENDABOT if item in repo.paths]
    wanted += sorted(item for item in repo.paths if WORKFLOW.fullmatch(item) and not PLATFORM_WORKFLOW.fullmatch(item))[:5]
    code = [repo.at(item) for item in sorted(repo.local_paths, key=rank) if application_python(item) and repo.at(item) not in wanted]
    return wanted + code[:max(limit - len(wanted), 0)]


def pull_request_body(result):
    """The pull request text: one line per drafted file, what was detected, and what a person must decide."""
    found = result.get("detected") or {}
    lines = ["Lab Deploy drafted these files because docs/DEVELOPER-CONTRACT.md asks for them and this repository does not have them yet. "
             "No existing file was changed, and nothing deploys until `platform.yaml` is reviewed and says `reviewed: true`.", "",
             "| File | Why |", "|---|---|"]
    lines += [f"| `{target}` | {item['purpose']} |" for target, item in sorted(result["files"].items())]
    if found:
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
