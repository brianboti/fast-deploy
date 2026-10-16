"""The reader (docs/ROADMAP.md 13.5, decision D34): what a repository taken as it is says about one of its services.

Each fact is read by an explicit precedence ladder (docs/RESEARCH-2026-10-16.md, section 4.4): the first rung that answers
wins, the files a higher rung made irrelevant are listed as ignored, and where guessing would start the reader refuses,
naming the file and the fix. Read-only and static like the analyzer and the scaffold: repository code is read as text,
never imported or run, and secret files are recognized by their names, never opened.

`read` returns the choices, refusals, ignored files and notes, the draft of the external contract (apps/<service>/
platform.yaml, D31) and what the platform must make for it: the dependency lock and, for the tests, a lock of the service
with its test tools. The containers that make those locks run in the controller (labdeploy.scaffold), never here.
"""
import fnmatch
import json
import re
import shlex
import tomllib

import yaml

from .analyzer import COMPOSE_FILES, IMAGE_KINDS, NEEDS_HINTS, TEST_PATH, UNPROVIDED, WORKERS, compose_names, compose_port
from .analyzer import folder as compose_folder
from .models import ENTRYPOINT, AppSpec
from .scaffold import (
    DEFAULT_PORT,
    DEFAULT_PYTHON,
    DEV_PINS,
    ENTRY_FILES,
    FRAMEWORK_DEPENDENCIES,
    PYTHONS,
    RESERVED_HEALTH,
    SERVER_PINS,
    Repository,
    existing_spec,
    find_applications,
    find_health,
    pyproject_data,
    pyproject_names,
    requirement_names,
    requires_python,
    satisfies,
    server_command,
    service_name,
)

LARGE_FILE = 100 * 1024 * 1024
# Committed secrets, recognized by name only: environment files (but not their documented examples), private keys and key
# stores, cloud credentials and Terraform state. Never opened.
SECRET_FILE = re.compile(r"(?i)(^|/)(\.env(\.[^/]+)?|[^/]+\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)|credentials\.json|"
                         r"service[-_]?account[^/]*\.json|[^/]+\.tfstate(\.backup)?)$")
EXAMPLE_ENV = re.compile(r"(?i)(^|/)\.env\.(example|sample|template|dist|defaults?|test)$")
ENVIRONMENTS = re.compile(r"(^|/)(\.?venv|env|site-packages|node_modules)/")
SYSTEM_PACKAGES = ("apt.txt", "Aptfile", "packages.txt", "bindep.txt", "apt-packages.txt")
CONDA_FILES = ("environment.yml", "environment.yaml")
LOCK_FILES = ("requirements.lock", "uv.lock", "poetry.lock", "Pipfile.lock", "pylock.toml")
TEST_REQUIREMENTS = ("requirements-dev.txt", "requirements-test.txt", "test-requirements.txt", "dev-requirements.txt",
                     "requirements/dev.txt", "requirements/test.txt")
PYTEST_CONFIG = ("pytest.toml", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py")
SETTINGS_FILE = re.compile(r"(^|/)settings(\.py|/[^/]+\.py)$")
WSGI_APPLICATION = re.compile(r"""^\s*WSGI_APPLICATION\s*=\s*["']([\w.]+)\.application["']""", re.M)
# Where the code reads its configuration: names only, never values.
CONFIG_PATTERNS = (
    re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]{1,63})["']\s*\]"""),
    re.compile(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]{1,63})["']"""),
    re.compile(r"""(?<![\w.])config\(\s*["']([A-Z][A-Z0-9_]{1,63})["']"""),  # python-decouple
    re.compile(r"""(?<![\w.])env(?:\.\w+)?\(\s*["']([A-Z][A-Z0-9_]{1,63})["']"""),  # django-environ
)
SECRET_NAME = re.compile(r"(SECRET|PASSWORD|TOKEN|PRIVATE|CREDENTIAL|API_KEY|_KEY$|DSN)")
TEST_TOOLS = {"httpx": re.compile(r"\bhttpx\b|TestClient"), "pytest-asyncio": re.compile(r"pytest\.mark\.asyncio|pytest_asyncio")}
ROTATE_AND_REMOVE = ("remove it from the repository (and from its history), rotate whatever it held, and give the values to the service as "
              "Secret Manager secrets named under secrets in the contract")


class Ladder:
    """What the reader decided, from where, and what it refused or set aside."""

    def __init__(self):
        self.choices, self.refusals, self.ignored, self.notes = [], [], [], []

    def choose(self, what, value, source):
        self.choices.append({"what": what, "value": value, "from": source})

    def refuse(self, file, problem, fix):
        self.refusals.append({"file": file, "problem": problem, "fix": fix})

    def ignore(self, file, why):
        if not any(entry["file"] == file for entry in self.ignored):
            self.ignored.append({"file": file, "why": why})

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)


# ---- what to fetch
def files_to_read(paths, path="", limit=60):
    """The repository paths whose text `read` uses, so a caller fetches only those: the configuration and lock files of the
    service and the root, its Compose files, Django settings, example environment files, then the most likely code."""
    repo = Repository(paths, {}, path)
    names = ("requirements.txt", "requirements.lock", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "pylock.toml",
             *CONDA_FILES, "Procfile", "Dockerfile", "dockerfile", ".python-version", "runtime.txt", "platform.yaml", "manage.py",
             *TEST_REQUIREMENTS, "pytest.ini", "pytest.toml", "tox.ini", "setup.cfg", *COMPOSE_FILES)
    wanted = [repo.at(name) for name in names if repo.has(name)]
    if repo.path:
        wanted += [name for name in (".python-version", "pyproject.toml", "uv.lock", ".gitattributes", *COMPOSE_FILES) if name in repo.paths]
    elif ".gitattributes" in repo.paths:
        wanted.append(".gitattributes")
    wanted += sorted(repo.at(item) for item in repo.local_paths if EXAMPLE_ENV.search(item) and "/" not in item)
    wanted += sorted(repo.at(item) for item in repo.local_paths if SETTINGS_FILE.search(item) and not ENVIRONMENTS.search(item))[:4]
    tests = sorted(repo.at(item) for item in repo.local_paths if TEST_PATH.search(item) and item.endswith(".py") and not ENVIRONMENTS.search(item))[:4]
    code = [repo.at(item) for item in sorted(repo.local_paths, key=_rank) if item.endswith(".py") and not TEST_PATH.search(item)
            and not ENVIRONMENTS.search(item) and repo.at(item) not in wanted]
    return list(dict.fromkeys(wanted + tests + code[:max(limit - len(wanted) - len(tests), 0)]))


def _rank(relative):
    if relative in ENTRY_FILES:
        return (ENTRY_FILES.index(relative), 0, relative)
    return (len(ENTRY_FILES) + 1, relative.count("/"), relative)


def large_files(name):
    """Lock files can be far larger than the 64 KiB other inputs may be; they are read up to GitHub's 1 MB contents limit."""
    return name.rsplit("/", 1)[-1] in LOCK_FILES


# ---- the services of a repository
def services(paths, texts):
    """The services a repository holds (docs/RESEARCH-2026-10-16.md 4.4): one per Compose service with a build, else one per
    folder with its own dependency manifest and an entry file."""
    repo = Repository(paths, texts, "")
    found = []
    for name in COMPOSE_FILES:
        text = repo.texts.get(name)
        if not text:
            continue
        for entry in compose_services(text, ""):
            if entry.get("context") is not None and not any(item["path"] == entry["context"] for item in found):
                found.append({"path": entry["context"], "from": f"{name}, service {entry['name']}"})
        if found:
            return found
    manifests = ("requirements.txt", "pyproject.toml", "Pipfile", "requirements.lock")
    folders = sorted({item.rsplit("/", 1)[0] if "/" in item else "" for item in repo.paths
                      if item.rsplit("/", 1)[-1] in manifests and not ENVIRONMENTS.search(item) and item.count("/") <= 3})
    for where in folders:
        prefix = f"{where}/" if where else ""
        entries = [name for name in (*ENTRY_FILES, "manage.py", "Procfile", "Dockerfile") if f"{prefix}{name}" in repo.paths]
        if entries:
            found.append({"path": where, "from": f"{prefix}{entries[0]} beside its manifest"})
    return found


# ---- Compose
def compose_services(text, base):
    """The services of a Compose file whose folder is `base`, with their build context as a repository folder."""
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    entries = document.get("services") if isinstance(document, dict) else None
    if not isinstance(entries, dict):
        return []
    result = []
    for name, body in entries.items():
        body = body if isinstance(body, dict) else {}
        build = body.get("build")
        context = build if isinstance(build, str) else (build or {}).get("context", ".") if isinstance(build, dict) else None
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
        result.append({"name": str(name), "body": body, "dockerfile": dockerfile,
                       "context": None if context is None else compose_folder(f"{base}/{context}" if base else context)})
    return result


def compose_entry(repo, ladder):
    """The Compose service that builds this folder, read from a Compose file in the folder or at the root, or None."""
    candidates = [(repo.at(name), repo.text(name), repo.path) for name in COMPOSE_FILES if repo.text(name)]
    if repo.path:
        candidates += [(name, repo.texts[name], "") for name in COMPOSE_FILES if repo.texts.get(name)]
    for where, text, base in candidates:
        entries = compose_services(text, base)
        kinds = {entry["name"]: "postgres" for entry in entries
                 if entry["context"] is None and re.search(IMAGE_KINDS[0][1], str(entry["body"].get("image") or ""), re.I)}
        mine = [entry for entry in entries if entry["context"] == repo.path]
        if len(mine) > 1:
            published = [entry for entry in mine if entry["body"].get("ports") or entry["body"].get("expose")]
            if len(published) != 1:
                ladder.refuse(where, f"several Compose services build {repo.path or 'the root'} ({', '.join(e['name'] for e in mine)}) and the file "
                                     "does not say which one answers HTTP", "publish a port (ports) on the web service only, or give the service's "
                                     "start command in the contract (build.entrypoint) by hand")
                return None
            for other in mine:
                if other is not published[0]:
                    ladder.note(f"{where}: service {other['name']} builds the same folder without publishing a port; it looks like a worker, "
                                "which the platform does not run (a Cloud Run job or a Pub/Sub push service instead)")
            mine = published
        if mine:
            return {**mine[0], "file": where, "kinds": kinds}
    return None


def compose_health(body):
    """A path from a Compose healthcheck that requests the service over HTTP (curl, wget or Python), or None."""
    test = (body.get("healthcheck") or {}).get("test") if isinstance(body.get("healthcheck"), dict) else None
    text = " ".join(str(part) for part in test) if isinstance(test, list) else str(test or "")
    match = re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(/[A-Za-z0-9/_-]*)", text)
    return match.group(1) if match else None


def as_words(value):
    """A Compose command or entrypoint as a list of words: the list form as it is, the string form split like a shell."""
    if isinstance(value, list):
        return [str(part) for part in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return None
    return None


# ---- lock files the platform converts without running anything
def pins_of(lock_text):
    """`name==version ; marker` lines of a hash-locked requirements file, for compiling it again with something added."""
    joined = re.sub(r"\\\n", " ", lock_text or "")
    pins = []
    for line in joined.splitlines():
        line = re.sub(r"\s--hash=\S+", "", line.split(" #", 1)[0]).strip()
        if line and not line.startswith(("#", "-")) and "==" in line:
            pins.append(re.sub(r"\s+", " ", line))
    return pins


def poetry_lock_requirements(text):
    """A hash-locked requirements file from poetry.lock: its main-group packages at their locked versions with every file
    hash Poetry recorded, never re-resolved. Returns (text, problems): problems name packages that cannot come from PyPI."""
    data = tomllib.loads(text)
    metadata_files = (data.get("metadata") or {}).get("files") or {}
    lines, problems = [], []
    for package in data.get("package") or []:
        groups = package.get("groups") or [package.get("category") or "main"]
        if "main" not in groups or package.get("optional"):
            continue
        name, version = package.get("name"), package.get("version")
        source = package.get("source") or {}
        if source.get("type") in ("directory", "file", "git", "url"):
            problems.append(f"{name} comes from a {source['type']} source")
            continue
        files = package.get("files") or metadata_files.get(name) or []
        hashes = sorted({item.get("hash") for item in files if isinstance(item, dict) and str(item.get("hash", "")).startswith("sha256:")})
        if not (name and version and hashes):
            problems.append(f"{name or 'a package'} has no recorded hashes")
            continue
        markers = package.get("markers")
        marker = markers.get("main") if isinstance(markers, dict) else markers
        lines.append(f"{name}=={version}" + (f" ; {marker}" if marker else "") + " \\\n" + " \\\n".join(f"    --hash={h}" for h in hashes))
    header = "# Converted by Fast Deploy from poetry.lock: the main group at its locked versions and hashes.\n"
    return header + "\n".join(lines) + ("\n" if lines else ""), problems


def pipfile_lock_requirements(text):
    """A hash-locked requirements file from Pipfile.lock's default section, at its locked versions and hashes."""
    data = json.loads(text)
    lines, problems = [], []
    for name, entry in sorted((data.get("default") or {}).items()):
        entry = entry if isinstance(entry, dict) else {}
        version, hashes = entry.get("version"), sorted(entry.get("hashes") or [])
        if not (isinstance(version, str) and version.startswith("==") and hashes):
            problems.append(f"{name} has no exact version or no hashes (a path, git or editable dependency)")
            continue
        marker = entry.get("markers")
        lines.append(f"{name}{version}" + (f" ; {marker}" if marker else "") + " \\\n" + " \\\n".join(f"    --hash={h}" for h in hashes))
    header = "# Converted by Fast Deploy from Pipfile.lock: the default packages at their locked versions and hashes.\n"
    return header + "\n".join(lines) + ("\n" if lines else ""), problems


# ---- the ladders
def python_ladder(repo, ladder, base, dockerfile):
    """The Python version: the service's Dockerfile, the contract, .python-version, runtime.txt, Pipfile.lock, requires-python
    (the platform's default when it allows it, else the oldest supported version it allows), else the default."""
    if dockerfile:
        found = re.findall(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?python:(3\.\d+)", repo.text(dockerfile), re.M | re.I)
        if found:
            if found[-1] in PYTHONS:
                ladder.choose("Python", found[-1], f"{repo.at(dockerfile)} (FROM python:{found[-1]})")
                return found[-1]
            ladder.refuse(repo.at(dockerfile), f"the image builds on Python {found[-1]}, outside the supported 3.11 to 3.15",
                          "move its base image to python:3.12-slim or a later supported version")
            return None
        ladder.note(f"{repo.at(dockerfile)} does not build on a python:X.Y image, so the tests and the audit run on the Python the "
                    "rungs below choose; keep the two the same")
    if base.get("python") in PYTHONS:
        ladder.choose("Python", base["python"], repo.at("platform.yaml"))
        return base["python"]
    for name in (".python-version", "runtime.txt"):
        for where, text in repo.nearest(name):
            match = re.search(r"^\s*(?:cpython-|python-?)?(3\.\d+)", text, re.M | re.I)
            if not match:
                continue
            if match.group(1) in PYTHONS:
                ladder.choose("Python", match.group(1), where)
                return match.group(1)
            ladder.refuse(where, f"asks for Python {match.group(1)}, outside the supported 3.11 to 3.15", f"change {where} to 3.12 or a later supported version")
            return None
    if repo.text("Pipfile.lock"):
        try:
            version = ((json.loads(repo.text("Pipfile.lock")).get("_meta") or {}).get("requires") or {}).get("python_version")
        except ValueError:
            version = None
        if isinstance(version, str) and version in PYTHONS:
            ladder.choose("Python", version, repo.at("Pipfile.lock"))
            return version
        if isinstance(version, str) and re.fullmatch(r"3\.\d+", version):
            ladder.refuse(repo.at("Pipfile.lock"), f"was locked for Python {version}, outside the supported 3.11 to 3.15",
                          "lock it again for Python 3.12 or later (pipenv lock --python 3.12)")
            return None
    for where, text in repo.nearest("pyproject.toml"):
        specifier = requires_python(pyproject_data(text))
        if not specifier:
            continue
        fits = [version for version in PYTHONS if satisfies(version, specifier)]
        if not fits:
            ladder.refuse(where, f"requires Python {specifier}, which no supported version (3.11 to 3.15) meets", "widen requires-python to include 3.12")
            return None
        version = DEFAULT_PYTHON if DEFAULT_PYTHON in fits else fits[0]
        ladder.choose("Python", version, f"{where} (requires-python {specifier})")
        return version
    ladder.choose("Python", DEFAULT_PYTHON, "the platform's default: nothing in the repository names a version")
    return DEFAULT_PYTHON


def conda_packages(text):
    """(pip requirements, conda packages other than Python and pip) from an environment.yml."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return [], ["(unreadable file)"]
    pip, conda = [], []
    for item in data.get("dependencies") or [] if isinstance(data, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("pip"), list):
            pip += [str(entry) for entry in item["pip"]]
        elif isinstance(item, str):
            name = re.split(r"[=<>!~ ]", item.split("::")[-1], maxsplit=1)[0].lower()
            if name not in ("python", "pip"):
                conda.append(item)
    return pip, conda


def lock_ladder(repo, ladder):
    """How the service's dependencies are pinned: {"action": use | export | convert | compile, "from": file, ...}, or None
    after a refusal. use: the repository's own hash-locked file; export: uv.lock through `uv export`; convert: poetry.lock or
    Pipfile.lock read as they are; compile: an unlocked list compiled once by the platform with uv."""
    requirements = repo.text("requirements.txt")
    pyproject = pyproject_data(repo.text("pyproject.toml"))
    if repo.has("requirements.lock"):
        ladder.choose("Dependencies", "the repository's own requirements.lock, installed with --require-hashes", repo.at("requirements.lock"))
        for other in ("requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock"):
            if repo.has(other):
                ladder.ignore(repo.at(other), "requirements.lock comes first")
        return {"action": "use", "from": repo.at("requirements.lock"), "text": repo.text("requirements.lock")}
    if "--hash=" in requirements:
        ladder.choose("Dependencies", "requirements.txt, already hash-locked", repo.at("requirements.txt"))
        return {"action": "use", "from": repo.at("requirements.txt"), "text": requirements}
    if repo.has("pylock.toml"):
        ladder.ignore(repo.at("pylock.toml"), "pip reads pylock.toml only experimentally; the rungs below are used")
    if repo.has("uv.lock") and repo.has("pyproject.toml"):
        ladder.choose("Dependencies", "uv.lock, exported with its hashes (uv export, main dependencies only)", repo.at("uv.lock"))
        for other in ("requirements.txt", "poetry.lock", "Pipfile.lock"):
            if repo.has(other):
                ladder.ignore(repo.at(other), "uv.lock comes first")
        return {"action": "export", "from": repo.at("uv.lock"), "pyproject": repo.text("pyproject.toml"), "lock": repo.text("uv.lock")}
    for name, convert in (("poetry.lock", poetry_lock_requirements), ("Pipfile.lock", pipfile_lock_requirements)):
        if not repo.has(name):
            continue
        if not repo.text(name):
            ladder.refuse(repo.at(name), "is larger than the platform reads (1 MB)", "export a requirements.txt with exact pins beside it")
            return None
        try:
            text, problems = convert(repo.text(name))
        except (ValueError, tomllib.TOMLDecodeError) as exc:
            ladder.refuse(repo.at(name), f"could not be read ({str(exc)[:120]})", "lock again with the tool that wrote it, or export a requirements.txt")
            return None
        if problems:
            ladder.refuse(repo.at(name), "holds packages the platform cannot install from PyPI by hash: " + "; ".join(problems[:4]),
                          "publish those packages or vendor them into the service's code")
            return None
        ladder.choose("Dependencies", f"{name}, read at its locked versions and hashes", repo.at(name))
        if repo.has("requirements.txt"):
            ladder.ignore(repo.at("requirements.txt"), f"{name} comes first")
        return {"action": "convert", "from": repo.at(name), "text": text}
    for name in CONDA_FILES:
        if not repo.has(name):
            continue
        pip, conda = conda_packages(repo.text(name))
        if conda and not requirements.strip():
            ladder.refuse(repo.at(name), "needs conda packages (" + ", ".join(conda[:5]) + "), which pip cannot install",
                          "list the pip equivalents in a requirements.txt, or build with a Dockerfile of your own that installs them")
            return None
        if pip and not requirements.strip():
            ladder.choose("Dependencies", "the pip section of " + name + ", compiled once by the platform with hashes", repo.at(name))
            return {"action": "compile", "from": repo.at(name), "input": "\n".join(pip) + "\n"}
    if requirements.strip():
        if re.search(r"(?m)^\s*(-e\s|\.\.?/|/|file:|git\+)", requirements):
            ladder.refuse(repo.at("requirements.txt"), "installs a local path or a git URL", "publish that package, or vendor it into the service's code")
            return None
        included = re.findall(r"(?m)^\s*(?:-r|--requirement)\s+(\S+)", requirements)
        if included:
            ladder.refuse(repo.at("requirements.txt"), f"includes {', '.join(included)}", "list every requirement in requirements.txt itself")
            return None
        ladder.choose("Dependencies", "requirements.txt, compiled once by the platform with hashes", repo.at("requirements.txt"))
        if repo.has("pyproject.toml"):
            ladder.ignore(repo.at("pyproject.toml"), "requirements.txt comes first for dependencies")
        return {"action": "compile", "from": repo.at("requirements.txt"), "input": requirements}
    dependencies = (pyproject.get("project") or {}).get("dependencies")
    if isinstance(dependencies, list) and dependencies:
        if any(re.search(r"@\s*(file:|git\+|\.)", str(item)) for item in dependencies):
            ladder.refuse(repo.at("pyproject.toml"), "depends on a local path or a git URL", "publish that package, or vendor it into the service's code")
            return None
        ladder.choose("Dependencies", "[project] dependencies of pyproject.toml, compiled once by the platform with hashes", repo.at("pyproject.toml"))
        return {"action": "compile", "from": repo.at("pyproject.toml"), "input": "\n".join(str(item) for item in dependencies) + "\n"}
    if (((pyproject.get("tool") or {}).get("poetry") or {}).get("dependencies")):
        ladder.refuse(repo.at("pyproject.toml"), "declares Poetry dependencies without a poetry.lock", "commit poetry.lock (poetry lock), or export requirements.txt")
        return None
    manifests = [repo.at(name) for name in ("setup.py", "setup.cfg", "Pipfile") if repo.has(name)]
    ladder.refuse(manifests[0] if manifests else (repo.path or "the repository root"),
                  "no dependency manifest the platform reads (requirements.txt, pyproject.toml dependencies, or a uv, Poetry or Pipenv lock)",
                  "add a requirements.txt with exact pins: guessing packages from the imports is wrong too often")
    return None


def procfile_web(repo):
    match = re.search(r"(?m)^web:\s*(.+?)\s*$", repo.text("Procfile"))
    return match.group(1) if match else None


def django_entry(repo, ladder, dependencies):
    """gunicorn's application for a Django project, from WSGI_APPLICATION in its settings, or None (with a refusal when Django
    is there but does not say)."""
    sources = {item: text for item, text in repo.texts.items() if item.startswith(repo.prefix)}
    settings = [(item, text) for item, text in sources.items() if SETTINGS_FILE.search(item)]
    uses_django = "django" in dependencies or repo.has("manage.py") or any("django" in text for _, text in settings)
    if not uses_django:
        return None
    for item, text in settings:
        match = WSGI_APPLICATION.search(text)
        if match:
            ladder.choose("Application", f"{match.group(1)}:application (Django)", f"{item} (WSGI_APPLICATION)")
            return {"interface": "wsgi", "application": f"{match.group(1)}:application", "framework": "django", "factory": False, "app_dir": ""}
    where = settings[0][0] if settings else repo.at("manage.py")
    ladder.refuse(where, "is a Django project whose settings do not say WSGI_APPLICATION", "add WSGI_APPLICATION = \"<project>.wsgi.application\" to the settings")
    return False


def application_ladder(repo, ladder, dependencies):
    """What the platform starts when nothing else says: [tool.fastapi] entrypoint, Django's WSGI_APPLICATION, one module-level
    application object of a known framework, or one factory. Several candidates, or several frameworks, are refused."""
    fastapi = ((pyproject_data(repo.text("pyproject.toml")).get("tool") or {}).get("fastapi") or {}).get("entrypoint")
    if isinstance(fastapi, str) and re.fullmatch(r"[\w.]+:[\w]+", fastapi):
        ladder.choose("Application", fastapi, f"{repo.at('pyproject.toml')} ([tool.fastapi] entrypoint)")
        return {"interface": "asgi", "application": fastapi, "framework": "fastapi", "factory": False, "app_dir": ""}
    django = django_entry(repo, ladder, dependencies)
    if django is not None:
        return django or None
    objects, factories = find_applications(repo.sources())
    candidates = sorted({(item["module"], item["attribute"]) for item in objects})
    if len(candidates) > 1:
        ladder.refuse(repo.at(objects[0]["file"]), "several candidate application objects: " + ", ".join(f"{m}:{a}" for m, a in candidates[:6]),
                      "say which one serves the web requests: give the start command in the contract (build.entrypoint)")
        return None
    frameworks = sorted({item["framework"] for item in objects + factories if item.get("framework")})
    if len(frameworks) > 1:
        ladder.refuse(repo.path or "the repository root", "several web frameworks (" + ", ".join(frameworks) + ")",
                      "keep one framework per service, or give the start command in the contract (build.entrypoint)")
        return None
    if objects:
        item = objects[0]
        interface = item.get("interface") or next((i for name, i in FRAMEWORK_DEPENDENCIES if name in dependencies), None)
        if interface is None:
            ladder.refuse(repo.at(item["file"]), f"{item['module']}:{item['attribute']} is made by a factory of an unrecognized framework",
                          "give the start command in the contract (build.entrypoint)")
            return None
        ladder.choose("Application", f"{item['module']}:{item['attribute']}", repo.at(item["file"]))
        return {"interface": interface, "application": f"{item['module']}:{item['attribute']}", "framework": item.get("framework"),
                "factory": False, "app_dir": item["app_dir"]}
    if len(factories) > 1:
        ladder.refuse(repo.at(factories[0]["file"]), "several application factories: " + ", ".join(f"{f['module']}:{f['attribute']}" for f in factories[:6]),
                      "say which one serves the web requests: give the start command in the contract (build.entrypoint)")
        return None
    if factories:
        item = factories[0]
        ladder.choose("Application", f"{item['module']}:{item['attribute']}() (a factory)", repo.at(item["file"]))
        return {"interface": item["interface"], "application": f"{item['module']}:{item['attribute']}", "framework": item["framework"],
                "factory": True, "app_dir": item["app_dir"]}
    notebooks = sorted(item for item in repo.local_paths if item.endswith(".ipynb"))
    if notebooks:
        ladder.refuse(repo.at(notebooks[0]), "the code is a notebook, which is not a web service",
                      "move the code that answers requests into a module (main.py with a FastAPI or Flask app); a notebook stays a notebook")
        return None
    ladder.refuse(repo.path or "the repository root", "no application object of a known framework (FastAPI, Starlette, Flask, Django, ...) "
                                                       "in main.py, app.py, wsgi.py, asgi.py, server.py or application.py",
                  "give the start command in the contract (build.entrypoint), or add a Procfile with a web: line")
    return None


def port_from_command(words):
    """A literal port in a start command: --port N, --bind HOST:N, -b HOST:N, -p N."""
    text = " ".join(words or [])
    for pattern in (r"--port[= ](\d{2,5})\b", r"(?:--bind|-b)[= ]\S*:(\d{2,5})\b", r"(?<!\S)-p[= ]?(\d{2,5})\b", r"\b0\.0\.0\.0:(\d{2,5})\b"):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def dockerfile_command(text):
    """The words of a Dockerfile's last CMD (or ENTRYPOINT when there is no CMD), exec or shell form, or None."""
    joined = re.sub(r"\\\n", " ", text or "")
    found = None
    for kind in ("CMD", "ENTRYPOINT"):
        matches = re.findall(rf"(?mi)^\s*{kind}\s+(.+?)\s*$", joined)
        if matches:
            found = matches[-1]
            break
    if found is None:
        return None
    if found.startswith("["):
        try:
            return [str(part) for part in json.loads(found)]
        except ValueError:
            return None
    try:
        return shlex.split(found)
    except ValueError:
        return None


def health_ladder(repo, ladder, base, compose, sources):
    """The health route: the contract, a Compose healthcheck, a literal route in the code, else none: Cloud Run's TCP
    startup probe, with the release saying the HTTP probe is off rather than inventing a route."""
    if "health_path" in base:
        ladder.choose("Health", base["health_path"] or "none (TCP startup probe)", repo.at("platform.yaml"))
        return base["health_path"]
    if compose:
        path = compose_health(compose["body"])
        if path and path not in RESERVED_HEALTH:
            ladder.choose("Health", path, f"{compose['file']} (service {compose['name']} healthcheck)")
            return path
    seen = find_health(sources)
    for candidate in ("/health", "/health/", "/healthcheck", "/healthcheck/"):
        if candidate in seen:
            ladder.choose("Health", candidate, repo.at(seen[candidate]))
            return candidate
    reserved = next((candidate for candidate in RESERVED_HEALTH if candidate in seen), None)
    if reserved:
        ladder.note(f"{repo.at(seen[reserved])} routes {reserved}, which Cloud Run keeps for itself (requests from outside never reach the "
                    "container): the release uses the TCP startup probe instead; a /health route would let it check HTTP")
    ladder.choose("Health", "none: Cloud Run's TCP startup probe; the HTTP health probe is off", "no health route found in the code read")
    return None


def configuration_names(repo, compose, sources):
    """Configuration the service reads, by name: its code, the Compose service's environment, example environment files."""
    names = set()
    for text in sources.values():
        for pattern in CONFIG_PATTERNS:
            names.update(pattern.findall(text))
    if compose:
        names.update(name for name in compose_names(compose["body"].get("environment")) if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name))
    for item in repo.local_paths:
        if EXAMPLE_ENV.search(item) and "/" not in item:
            names.update(re.findall(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,63})\s*=", repo.text(item)))
    return sorted(names - {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION"})


def tests_ladder(repo, ladder):
    """Whether the service has tests: pytest's own configuration, noxfile.py, a tests folder or test files. None: no tests,
    said plainly, never fabricated."""
    tests = sorted(item for item in repo.local_paths if TEST_PATH.search(item) and item.endswith(".py") and not ENVIRONMENTS.search(item))
    config = next((repo.at(name) for name in PYTEST_CONFIG if repo.has(name)), None)
    if not config and "[tool.pytest" in repo.text("pyproject.toml"):
        config = repo.at("pyproject.toml")
    if tests:
        ladder.choose("Tests", f"{len(tests)} test file{'s' if len(tests) != 1 else ''}, run with pytest before every build", config or repo.at(tests[0]))
        return tests
    ladder.choose("Tests", "none found: nothing checks the service's behaviour before a release (tests: none)", repo.path or "the repository root")
    return []


def test_tools(repo, tests):
    """What the tests need besides the service: the repository's own test requirements, else pytest and what the test files use."""
    own = next((repo.at(name) for name in TEST_REQUIREMENTS if repo.has(name)), None)
    groups = pyproject_data(repo.text("pyproject.toml"))
    extra = []
    for table in ((groups.get("dependency-groups") or {}), ((groups.get("project") or {}).get("optional-dependencies") or {})):
        for group in ("test", "tests", "dev", "testing"):
            extra += [str(item) for item in table.get(group) or [] if isinstance(item, str)]
    if own:
        extra += [line for line in repo.texts.get(own, "").splitlines() if line.strip() and not line.strip().startswith(("#", "-"))]
    text = " ".join(repo.text(item) for item in tests)
    names = {re.split(r"[\[<>=!~ ;]", item.strip(), maxsplit=1)[0].lower() for item in extra}
    tools = list(dict.fromkeys(extra))
    if "pytest" not in names:
        tools.append(f"pytest=={DEV_PINS['pytest']}")
    for tool, pattern in TEST_TOOLS.items():
        if tool not in names and pattern.search(text):
            tools.append(f"httpx=={DEV_PINS['httpx']}" if tool == "httpx" else tool)
    return tools, own


# ---- the reader
def read(paths, texts, path="", name=None, fallback_name=None, sizes=None):
    """One service of a repository taken as it is: what each ladder chose and from where, what it refused and why, the
    files it set aside, and the draft of the external contract. `sizes` maps paths to bytes when the caller has them."""
    repo = Repository(paths, texts, path)
    ladder = Ladder()
    where = repo.path or "the repository root"
    local = {item for item in repo.local_paths if item}
    result = {"service": None, "path": repo.path, "choices": ladder.choices, "refusals": ladder.refusals, "ignored": ladder.ignored,
              "notes": ladder.notes, "spec": None, "lock": None, "tests": None}
    if not any(item.endswith(".py") for item in local) and not any(repo.has(n) for n in ("requirements.txt", "pyproject.toml", "Pipfile", *CONDA_FILES)):
        ladder.refuse(where, "no Python code or dependency manifest", "the platform deploys Python web services only; give the folder that holds one")
        return result
    name, name_note = service_name(name, repo, fallback_name)
    result["service"] = name
    if name_note:
        ladder.note(name_note)

    # Refusals that do not depend on any ladder (D34).
    for item in sorted(local):
        if SECRET_FILE.search(item) and not EXAMPLE_ENV.search(item) and not ENVIRONMENTS.search(item):
            if TEST_PATH.search(item):
                ladder.note(f"{repo.at(item)} looks like a test key or environment file: the platform's builds leave tests out of the image")
            else:
                ladder.refuse(repo.at(item), "looks like a committed secret (an environment file, a private key or a credentials file)", ROTATE_AND_REMOVE)
    committed_environment = sorted({item.split("/")[0] for item in local if re.match(r"(\.?venv|env)/(bin|lib|Lib|Scripts)/", item)})
    for folder in committed_environment:
        ladder.ignore(repo.at(folder), "a committed virtual environment; the platform installs the dependencies itself")
    attributes = repo.texts.get(".gitattributes", "") + "\n" + repo.text(".gitattributes")
    lfs = [line.split()[0] for line in attributes.splitlines() if "filter=lfs" in line and line.strip() and not line.lstrip().startswith("#")]
    stored = sorted(item for item in local if any(fnmatch.fnmatch(item, p) or fnmatch.fnmatch(item.rsplit("/", 1)[-1], p) for p in lfs))
    if stored:
        ladder.refuse(repo.at(stored[0]), f"is stored with Git LFS ({len(stored)} file{'s' if len(stored) != 1 else ''}): the build checks out a "
                                          "small pointer, not the file", "keep large data and models in a bucket (needs.buckets) and read them at run time")
    for item, size in sorted((sizes or {}).items()):
        if item.startswith(repo.prefix) and isinstance(size, int) and size > LARGE_FILE:
            ladder.refuse(item, f"is {size // (1024 * 1024)} MiB", "keep files over 100 MiB in a bucket (needs.buckets) and read them at run time")

    # The contract in the repository, when there is one, is imported once as the draft (D31).
    base = {}
    declared = existing_spec(repo)
    if declared:
        try:
            AppSpec.model_validate({**declared, "reviewed": False})
            base = {key: value for key, value in declared.items() if key != "reviewed"}
            ladder.choose("Contract", "the repository's platform.yaml, imported as the draft", repo.at("platform.yaml"))
        except Exception as exc:  # noqa: BLE001 - an invalid file is set aside, never half-used
            ladder.ignore(repo.at("platform.yaml"), f"not valid ({str(exc).splitlines()[0][:160]}); the reader drafts the contract instead")
    if base.get("name"):
        name = result["service"] = base["name"]
    compose = compose_entry(repo, ladder)
    if compose:
        ladder.choose("Compose service", compose["name"], compose["file"])

    # How the image is made.
    dockerfile = next((n for n in ("Dockerfile", "dockerfile") if repo.has(n)), None)
    web = procfile_web(repo)
    recipe = ((base.get("build") or {}).get("recipe")) or None
    if recipe in (None, "repository") and dockerfile:
        recipe = "repository"
        ladder.choose("Build", "the service's own Dockerfile", repo.at(dockerfile))
        if compose and compose.get("dockerfile") and str(compose["dockerfile"]).strip("./") not in ("Dockerfile", dockerfile):
            ladder.note(f"{compose['file']} builds service {compose['name']} with {compose['dockerfile']}; the platform builds {repo.at(dockerfile)}")
        if web:
            ladder.ignore(repo.at("Procfile"), "the Dockerfile comes first")
    elif recipe in (None, "repository", "buildpacks") and web:
        recipe = "buildpacks" if recipe == "buildpacks" else "repository"
        ladder.choose("Build", f"Google's buildpacks, started by the Procfile's web command ({web[:80]})", repo.at("Procfile"))
    elif recipe is None or recipe == "repository":
        recipe = "platform"
        ladder.choose("Build", "the platform's own Dockerfile, made at build time; nothing is added to the repository", "no Dockerfile or Procfile in " + where)
    for other in sorted(local):
        if "/" not in other and re.fullmatch(r"(?i)(dockerfile\..+|.+\.dockerfile|containerfile)", other) and other != dockerfile:
            ladder.ignore(repo.at(other), "the platform builds the file named Dockerfile only")
    builds_itself = recipe == "repository" and bool(dockerfile)

    # Every ladder runs even after a refusal, so the answer lists everything to fix at once.
    python = python_ladder(repo, ladder, base, dockerfile if builds_itself else None) or DEFAULT_PYTHON
    lock = lock_ladder(repo, ladder)
    dependencies = requirement_names(repo.text("requirements.txt")) | requirement_names(repo.text("requirements.lock")) | pyproject_names(pyproject_data(repo.text("pyproject.toml")))
    if lock and lock.get("text"):
        dependencies |= requirement_names("\n".join(pins_of(lock["text"])))
    if not builds_itself:
        packages = [repo.at(item) for item in SYSTEM_PACKAGES if repo.has(item)]
        if packages:
            ladder.refuse(packages[0], "lists system packages, which the platform's own builds do not install",
                          "build with a Dockerfile of the repository's own that installs them")

    # What starts the service, and on which port.
    entrypoint, command, args, start_words = None, None, None, None
    sources = repo.sources()
    own_command = dockerfile_command(repo.text(dockerfile)) if builds_itself else None
    if compose and compose["body"].get("command") is not None:
        words = as_words(compose["body"]["command"])
        if words is None:
            ladder.refuse(compose["file"], f"service {compose['name']}'s command cannot be read as words", "write it as a list of words")
        elif builds_itself:
            start_words = words
            if words != own_command:  # Compose's command replaces the image's CMD, as `docker compose up` runs it
                args = words
                ladder.choose("Start command", " ".join(words)[:120] + " (replaces the Dockerfile's CMD)", f"{compose['file']} (service {compose['name']})")
        elif not web:
            entrypoint, start_words = " ".join(words), words
            ladder.choose("Start command", entrypoint[:120], f"{compose['file']} (service {compose['name']})")
    if compose and compose["body"].get("entrypoint") is not None and builds_itself:
        words = as_words(compose["body"]["entrypoint"])
        if words:
            command = words
            ladder.choose("Entrypoint", " ".join(words)[:120] + " (replaces the Dockerfile's ENTRYPOINT)", f"{compose['file']} (service {compose['name']})")
    if builds_itself and start_words is None:
        start_words = own_command
        ladder.choose("Start command", " ".join(start_words or [])[:120] or "the image's own", repo.at(dockerfile))
    elif recipe != "platform" and web and start_words is None:
        try:
            start_words = shlex.split(web)
        except ValueError:
            start_words = None
    elif recipe in ("platform", "buildpacks") and (base.get("build") or {}).get("entrypoint") and entrypoint is None:
        entrypoint = base["build"]["entrypoint"]
        ladder.choose("Start command", entrypoint[:120], repo.at("platform.yaml"))
    port = None
    if isinstance(base.get("port"), int):
        port = base["port"]
        ladder.choose("Port", port, repo.at("platform.yaml"))
    elif compose and (compose_port(compose["body"].get("ports")) or compose_port(compose["body"].get("expose"))):
        port = compose_port(compose["body"].get("ports")) or compose_port(compose["body"].get("expose"))
        ladder.choose("Port", port, f"{compose['file']} (service {compose['name']}, container side)")
    elif builds_itself and len(re.findall(r"(?mi)^\s*EXPOSE\s+(\d+)", repo.text(dockerfile))) == 1:
        port = int(re.findall(r"(?mi)^\s*EXPOSE\s+(\d+)", repo.text(dockerfile))[0])
        ladder.choose("Port", port, f"{repo.at(dockerfile)} (EXPOSE)")
    elif port_from_command(start_words or (entrypoint or "").split()):
        port = port_from_command(start_words or (entrypoint or "").split())
        ladder.choose("Port", port, "a literal port in the start command")
    else:
        port = DEFAULT_PORT
        ladder.choose("Port", port, "the platform's default; Cloud Run passes it to the service as PORT")
        if builds_itself:
            literal = next((match for text in sources.values() for match in re.findall(r"\.run\([^)]*\bport\s*=\s*(\d{2,5})", text)), None)
            if literal and int(literal) != port:
                ladder.note(f"The code starts a server on port {literal} when run directly: if the image starts it that way, set port: {literal}")
    if not 1024 <= port <= 65535:
        ladder.refuse(where, f"the service listens on port {port}", "make it listen on PORT (8080 by default); ports below 1024 need root, which "
                                                                    "the platform's images never use")
        port = DEFAULT_PORT
    if recipe in ("platform", "buildpacks") and entrypoint is None and not web:
        found = application_ladder(repo, ladder, dependencies)
        if found:
            words = server_command(found["interface"], found["application"], found["app_dir"], found["factory"], port)
            entrypoint = " ".join(words)
            server = words[0]
            if server not in dependencies:
                ladder.note(f"The repository does not list {server}, which starts the service: the platform adds {server}=={SERVER_PINS[server]} to the lock it makes")
                if lock:
                    lock["server"] = f"{server}=={SERVER_PINS[server]}"
    if entrypoint is not None and not re.fullmatch(ENTRYPOINT, entrypoint):
        ladder.refuse(compose["file"] if compose else where, "the start command uses quotes or shell operators the platform cannot pass on",
                      "give a plain start command (words, $PORT allowed) in the contract's build.entrypoint")
        entrypoint = None
    if lock and lock.get("server") and lock["action"] in ("use", "convert"):
        # The server is not in the repository's lock: compile it again with the server added, its pins kept as they are.
        lock = {**lock, "action": "compile", "input": "\n".join(pins_of(lock["text"])) + "\n"}

    health = health_ladder(repo, ladder, base, compose, sources)
    names = configuration_names(repo, compose, sources)
    if names:
        secret = [n for n in names if SECRET_NAME.search(n)]
        ladder.note("The code reads these settings: " + ", ".join(names[:20]) + ". Before merging, give plain values under env and name secrets "
                    "under secrets (Secret Manager secrets called <service>-NAME); nothing was added."
                    + (f" These look like secrets: {', '.join(secret[:10])}." if secret else ""))

    # Needs: what Compose says explicitly goes in the draft; what the dependencies suggest is a note (D16).
    needs = dict(base.get("needs") or {})
    if compose and not needs:
        if any(compose["kinds"].get(n) == "postgres" for n in compose_names(compose["body"].get("depends_on"))):
            needs = {"database": "postgres"}
            if "DATABASE_URL" in compose_names(compose["body"].get("environment")):
                needs["database_auth"] = "password"
            ladder.choose("Needs", "database: postgres" + (" (by password: the code reads DATABASE_URL)" if "database_auth" in needs else ""),
                          f"{compose['file']} (service {compose['name']} depends on a Postgres service)")
    manifest_text = repo.text("requirements.txt") + "\n" + repo.text("pyproject.toml") + "\n" + "\n".join(sorted(dependencies))
    hints = [need for need, pattern in NEEDS_HINTS if re.search(pattern, manifest_text, re.I)]
    if hints and not needs:
        ladder.note("The dependencies suggest needs; add the ones the service really uses under needs before merging (nothing was added): " + "; ".join(hints))
    if repo.has("alembic.ini") and not (needs.get("migrate")):
        ladder.note("alembic.ini is there: if the schema is created by migrations, add needs.migrate: [alembic, upgrade, head]")
    for label, pattern in UNPROVIDED:
        if re.search(pattern, manifest_text, re.I):
            ladder.note(f"{label} appears in the dependencies: the platform does not provide it")
    if re.search(WORKERS, manifest_text, re.I):
        ladder.note("A background-worker library appears in the dependencies: workers run as Cloud Run jobs or Pub/Sub push services, not here")

    tests = tests_ladder(repo, ladder)
    if ladder.refusals or lock is None:
        return result

    build = dict(base.get("build") or {})
    if recipe != "repository":
        build["recipe"] = recipe
    if entrypoint and recipe in ("platform", "buildpacks"):
        build["entrypoint"] = entrypoint
    draft = {**base, "name": name, "python": python, "port": port, "health_path": health, "tests": "required" if tests else "none"}
    if build:
        draft["build"] = build
    if needs:
        draft["needs"] = needs
    if command is not None and "command" not in base:
        draft["command"] = command
    if args is not None and "args" not in base:
        draft["args"] = args
    if "concurrency" not in base and (re.search(r"\b(flask|django)\b", manifest_text, re.I) or "gunicorn" in (entrypoint or "")):
        draft["concurrency"] = 8  # one gunicorn worker with eight threads, or a synchronous framework
    try:
        spec = AppSpec.model_validate({**draft, "reviewed": True})
    except Exception as exc:  # noqa: BLE001 - the draft must be a valid contract, or nothing is proposed
        ladder.refuse(where, f"the reader's draft is not a valid contract ({str(exc).splitlines()[0][:200]})", "write the contract by hand from the choices above")
        return result
    result["spec"] = spec
    result["lock"] = lock
    if tests:
        tools, own = test_tools(repo, tests)
        result["tests"] = {"files": [repo.at(item) for item in tests[:20]], "tools": tools, "own": own}
    return result
