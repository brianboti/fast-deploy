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

from . import dockerpolicy
from . import node as nodejs
from .analyzer import COMPOSE_FILES, IMAGE_KINDS, NEEDS_HINTS, TEST_PATH, UNPROVIDED, WORKERS, compose_names, compose_port
from .analyzer import folder as compose_folder
from .models import ENTRYPOINT, NODE_END_OF_LIFE, AppSpec, default_node
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
PYTHON_MANIFESTS = ("requirements.txt", "requirements.lock", "pyproject.toml", "Pipfile", "Pipfile.lock", "uv.lock", "poetry.lock",
                    "environment.yml", "environment.yaml")
NODE_FILES = ("package.json", "package-lock.json", "npm-shrinkwrap.json", ".nvmrc", ".node-version", ".tool-versions", ".npmrc", ".yarnrc.yml")
NODE_ENTRIES = ("server.js", "index.js", "app.js", "main.js", "server.mjs", "index.mjs", "app.mjs", "server.cjs", "index.cjs", "src/index.js",
                "src/server.js", "src/app.js", "src/main.js", "src/index.mjs", "src/server.mjs", "server/index.js", "server/index.mjs",
                "server.ts", "index.ts", "src/index.ts", "src/server.ts", "src/main.ts")
NODE_WORKSPACE_FILES = ("pnpm-workspace.yaml", "lerna.json", "turbo.json", "nx.json", "rush.json")
# What a start script that cannot be run as it is should become, by what it does (D46).
BUILD_FIX = ("commit the built output and start it with node, run TypeScript under Node's type stripping (Node 24 or 26), or build it "
             "with a Dockerfile of your own; a build step in the platform's image is a later roadmap item")
START_FIX = "start the service with node itself in scripts.start (\"start\": \"node server.js\"), and keep the development command under another name (dev)"
STATIC_FIX = ("the platform serves web services: serve the built files from a small web service of your own (an Express static handler, "
              "say), or host them elsewhere")
# Libraries whose use usually means calls to services on the internet (docs/ROADMAP.md 14.5): a note for the reviewer.
INTERNET_CLIENTS = {"openai", "anthropic", "requests", "httpx", "aiohttp", "urllib3", "twilio", "stripe", "slack-sdk", "boto3", "botocore",
                    "sendgrid", "tweepy", "praw", "discord-py", "python-telegram-bot", "mailchimp-marketing", "cohere", "mistralai", "groq"}
# Committed secrets, recognized by name only: environment files (but not their documented examples), private keys and key
# stores, cloud credentials and Terraform state. Never opened.
SECRET_FILE = re.compile(r"(?i)(^|/)(\.env(\.[^/]+)?|[^/]+\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|dsa|ecdsa|ed25519)|credentials\.json|"
                         r"service[-_]?account[^/]*\.json|[^/]+\.tfstate(\.backup)?|"
                         # Environment files by other names (found live: env.prod and env.yaml, gcloud's --env-vars-file format).
                         r"env\.(prod|production|stage|staging|local|dev|development|secret|secrets|ya?ml)|[^/]+\.env|secrets?\.(json|ya?ml|toml))$")
EXAMPLE_ENV = re.compile(r"(?i)(^|/)\.env\.(example|sample|template|dist|defaults?|test)$")
ENVIRONMENTS = re.compile(r"(^|/)(\.?venv|env|site-packages|node_modules)/")
SYSTEM_PACKAGES = ("apt.txt", "Aptfile", "packages.txt", "bindep.txt", "apt-packages.txt")
READINESS = re.compile(r"""["'](/(?:ready|readiness|live|liveness|ping|status))/?["']""")
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
# A setting that holds a secret by its name: one whose last part says so. Flags and limits (CORS_ALLOW_CREDENTIALS,
# MAX_OUTPUT_TOKENS, found live) are not secrets, whatever words they contain.
SECRET_NAME = re.compile(r"(^|_)(SECRET|PASSWORD|PASSWD|TOKEN|KEY|DSN|CREDENTIALS?)$|^DATABASE_URL$")
NOT_SECRET = re.compile(r"(^|_)(ALLOW|ENABLE|ENABLED|DISABLE|MAX|MIN|TTL|TIMEOUT|COUNT|LENGTH|SIZE|TYPE|FILE|PATH|DIR|ID)(_|$)")
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


# ---- the service's name
# Folder names that say what a folder is, not which service it holds: a service there is named after its project or its repository.
GENERIC_FOLDERS = {"api", "app", "apps", "back-end", "backend", "core", "frontend", "main", "server", "service", "services", "site", "src", "web",
                   "webapp", "website"}
NAME = re.compile(r"[a-z][a-z0-9-]{1,19}[a-z0-9]")  # AppSpec.name: what Cloud Run, the registry and the roots accept


def slug_of(text):
    """A service name from free text: lowercase words joined by dashes, cut at a dash to at most 21 characters."""
    slug = re.sub(r"^[^a-z]+", "", re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-"))
    while len(slug) > 21 and "-" in slug[:22]:
        slug = slug[:slug[:22].rindex("-")]
    slug = slug[:21].strip("-")
    return slug if NAME.fullmatch(slug) else None


def name_for(name, repo, fallback=None, runtime="python-http"):
    """(name, note): the explicit name (refused when Cloud Run would refuse it); else, at the root, the project's name
    (pyproject.toml's, or package.json's for a Node.js service), then the repository's; in a folder, the folder's own name
    unless it is a generic one (backend, api, src, ...), then the project's, then the repository's with the folder. Every
    candidate is cut at a dash, never in the middle of a word."""
    if name is not None:
        return service_name(name, repo, fallback)
    project = (pyproject_data(repo.text("pyproject.toml")).get("project") or {}).get("name")
    if runtime == "node-http":
        project = (nodejs.package_data(repo.text("package.json")) or {}).get("name")
        project = project.rsplit("/", 1)[-1] if isinstance(project, str) else None  # @lab/notes-api is notes-api
    project = project if isinstance(project, str) and project.lower() not in GENERIC_FOLDERS else None
    folder = repo.path.rsplit("/", 1)[-1] if repo.path else None
    if folder is None:
        candidates = (project, fallback)
    elif folder.lower() in GENERIC_FOLDERS:
        candidates = (project, f"{fallback}-{folder}" if fallback else None, fallback)
    else:
        candidates = (folder, project, fallback)
    for candidate in candidates:
        slug = slug_of(candidate)
        if slug:
            return slug, None
    return "my-service", "No service name was found, so the contract says my-service: choose the real name (3 to 21 characters)."


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
    if repo.has("package.json") and not any(repo.has(name) for name in PYTHON_MANIFESTS):
        # A Node.js service: its manifest, lockfile, version files and npm settings (the root's too, for a service in a
        # folder), then its likeliest code, the entry files first. The file its start names, and its imports, follow in a
        # second read (more_files_to_read).
        wanted += [repo.at(name) for name in NODE_FILES if repo.has(name) and repo.at(name) not in wanted]
        wanted += [name for name in ("package.json", ".nvmrc", ".node-version", ".tool-versions", ".npmrc") if repo.path and name in repo.paths]
        code = [repo.at(item) for item in sorted(repo.local_paths, key=_node_rank) if nodejs.CODE.search(item) and not nodejs.NOT_CODE.search(item)
                and not nodejs.NODE_TEST.search(item) and repo.at(item) not in wanted]
        return list(dict.fromkeys(wanted + code[:max(limit - len(wanted), 0)]))
    tests = sorted(repo.at(item) for item in repo.local_paths if TEST_PATH.search(item) and item.endswith(".py") and not ENVIRONMENTS.search(item))[:4]
    code = [repo.at(item) for item in sorted(repo.local_paths, key=_rank) if item.endswith(".py") and not TEST_PATH.search(item)
            and not ENVIRONMENTS.search(item) and repo.at(item) not in wanted]
    return list(dict.fromkeys(wanted + tests + code[:max(limit - len(wanted) - len(tests), 0)]))


def _rank(relative):
    if relative in ENTRY_FILES:
        return (ENTRY_FILES.index(relative), 0, relative)
    return (len(ENTRY_FILES) + 1, relative.count("/"), relative)


def _node_rank(relative):
    if relative in NODE_ENTRIES:
        return (NODE_ENTRIES.index(relative), 0, relative)
    return (len(NODE_ENTRIES) + 1, relative.count("/"), relative)


IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\(\s*|^\s*import\s+)["'](\.{1,2}/[^"']+)["']""", re.M)


def local_imports(relative, text):
    """The files of the service a module imports by a relative path (./config.mjs), as service-relative paths."""
    import posixpath
    folder = posixpath.dirname(relative)
    found = []
    for target in IMPORT.findall(text or ""):
        path = posixpath.normpath(posixpath.join(folder, target))
        if not path.startswith(".."):
            found.append(path)
    return found


def more_files_to_read(paths, texts, path=""):
    """After a first read of a Node.js service: the file its start names (scripts.start's file, main, server.js, index.js)
    when it was not read yet, else the local modules that file imports, so the listen call and the settings it reads are
    found wherever they live. Empty when there is nothing more to read; a caller reads again until it is."""
    repo = Repository(paths, texts, path)
    package = nodejs.package_data(repo.text("package.json"))
    if not package:
        return []
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    start, _ = nodejs.start_words(scripts["start"]) if isinstance(scripts.get("start"), str) else (None, None)
    entry = next((item for item in ((start or {}).get("file"), str(package.get("main") or "").removeprefix("./") or None, "server.js", "index.js")
                  if item and repo.has(item)), None)
    if entry is None:
        return []
    if repo.at(entry) not in repo.texts:
        return [repo.at(entry)]
    wanted = []
    for item in local_imports(entry, repo.text(entry)):
        candidates = [item] if nodejs.CODE.search(item) else [item + ext for ext in (".js", ".mjs", ".cjs", ".ts", "/index.js")]
        found = next((candidate for candidate in candidates if repo.has(candidate)), None)
        if found and repo.at(found) not in repo.texts:
            wanted.append(repo.at(found))
    return wanted[:12]


def large_files(name):
    """Lock files can be far larger than the 64 KiB other inputs may be; they are read up to GitHub's 1 MB contents limit."""
    return name.rsplit("/", 1)[-1] in LOCK_FILES + nodejs.LOCKFILES


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
    manifests = ("requirements.txt", "pyproject.toml", "Pipfile", "requirements.lock", "package.json")
    folders = sorted({item.rsplit("/", 1)[0] if "/" in item else "" for item in repo.paths
                      if item.rsplit("/", 1)[-1] in manifests and not ENVIRONMENTS.search(item) and item.count("/") <= 3})
    for where in folders:
        prefix = f"{where}/" if where else ""
        entries = [name for name in (*ENTRY_FILES, "manage.py", *NODE_ENTRIES[:9], "Procfile", "Dockerfile") if f"{prefix}{name}" in repo.paths]
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
    objects, factories = find_applications(repo.sources(), "src/__init__.py" in repo.local_paths)
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
    readiness = sorted({path for text in sources.values() for path in READINESS.findall(text)})
    if readiness:
        # Found live: a Flask app answering /ready and / without a /health. Such a route may call other services, so the reader
        # never picks it; a person who knows it answers at once can set health_path in the review.
        ladder.note(f"The code routes {', '.join(readiness[:3])}: if one answers 200 at once without calling other services, set health_path "
                    "to it in the review, and the release checks HTTP instead of TCP")
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


# ---- Node.js (docs/ROADMAP.md D41 to D46)
def runtime_of(repo, local):
    """python-http or node-http, or None when the folder holds neither: the repository's platform.yaml when it says; a
    Python manifest (a package.json beside it serves the front end's tooling); package.json; Python code; Node.js code."""
    declared = existing_spec(repo).get("runtime")
    if declared in ("python-http", "node-http"):
        return declared
    python = any(item.endswith(".py") for item in local) or any(repo.has(n) for n in ("requirements.txt", "pyproject.toml", "Pipfile", *CONDA_FILES))
    if repo.has("package.json") and not any(repo.has(n) for n in PYTHON_MANIFESTS):
        return "node-http"
    if python:
        return "python-http"
    if any(nodejs.CODE.search(item) and not nodejs.NOT_CODE.search(item) for item in local):
        return "node-http"
    return None


def node_line(repo, ladder, base, dockerfile, package):
    """The Node.js line (D41): the Dockerfile's FROM node:X, the contract, .nvmrc, .node-version, .tool-versions,
    engines.node (a range resolves to the highest supported line it allows), devEngines.runtime, else the newest Active LTS."""
    if dockerfile:
        found = re.findall(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?node:(\d+)", repo.text(dockerfile), re.M | re.I)
        if found:
            line = str(int(found[-1]))
            if line in nodejs.NODES:
                ladder.choose("Node.js", line, f"{repo.at(dockerfile)} (FROM node:{line})")
                return line
            ladder.refuse(repo.at(dockerfile), f"the image builds on Node.js {line}, outside the supported 22, 24 and 26",
                          f"move its base image to node:{default_node()}-slim")
            return None
        ladder.note(f"{repo.at(dockerfile)} does not build on a node:X image, so the tests and the audit run on the line the rungs below "
                    "choose; keep the two the same")
    if base.get("node") in nodejs.NODES:
        ladder.choose("Node.js", base["node"], repo.at("platform.yaml"))
        return base["node"]
    files = [(where, text, name) for name in (".nvmrc", ".node-version") for where, text in repo.nearest(name)]
    for where, text in repo.nearest(".tool-versions"):
        match = re.search(r"(?m)^\s*nodejs\s+(\S+)", text)
        if match:
            files.append((where, match.group(1), ".tool-versions"))
    for where, text, name in files:
        line, problem = nodejs.line_of(text)
        if problem:
            ladder.refuse(where, problem, f"write the line's number in {name} ({default_node()})")
            return None
        if line in nodejs.NODES:
            ladder.choose("Node.js", line, where)
            return line
        ladder.refuse(where, f"asks for Node.js {line}, outside the supported 22, 24 and 26", f"change {name} to {default_node()}")
        return None
    ranges = [(f"engines.node {package['engines']['node']}", package["engines"]["node"])] if isinstance((package.get("engines") or {}).get("node"), str) else []
    runtimes = (package.get("devEngines") or {}).get("runtime") if isinstance(package.get("devEngines"), dict) else None
    for item in runtimes if isinstance(runtimes, list) else [runtimes]:
        if isinstance(item, dict) and item.get("name") == "node" and isinstance(item.get("version"), str):
            ranges.append((f"devEngines.runtime {item['version']}", item["version"]))
    for label, spec in ranges[:1]:
        fits = nodejs.lines_meeting(spec)
        if fits is None:
            ladder.refuse(repo.at("package.json"), f"{label} cannot be read as an npm version range", "write a range npm reads, such as \">=22\" or \"24.x\"")
            return None
        if not fits:
            ladder.refuse(repo.at("package.json"), f"{label} allows no supported Node.js line (22, 24, 26)", f"widen it to allow {default_node()}")
            return None
        ladder.choose("Node.js", fits[-1], f"{repo.at('package.json')} ({label})")
        if nodejs.no_upper_bound(spec):
            ladder.note(f"{label} has no upper bound: the platform runs Node.js {fits[-1]}, the highest supported line it allows, and a newer "
                        "line is a change to the contract, never automatic")
        return fits[-1]
    line = default_node()
    ladder.choose("Node.js", line, "the platform's default: the newest Active LTS line, as nothing in the repository names one")
    return line


def npm_lock(repo, ladder, package):
    """How the packages are pinned (D42): {"action": "use", the repository's own npm lockfile, checked entry by entry} or
    {"action": "lock", package.json locked once by the platform}; None after a refusal."""
    others = [name for name in nodejs.OTHER_LOCKFILES if repo.has(name)]
    own = [name for name in nodejs.LOCKFILES if repo.has(name)]
    if len(others) + bool(own) > 1:
        ladder.refuse(repo.at((own + others)[0]), "has more than one lockfile (" + ", ".join(own[:1] + others) + "), so two package managers disagree "
                      "about the versions", "keep one: the platform installs with npm from package-lock.json, so remove the others")
        return None
    if others:
        manager = nodejs.OTHER_LOCKFILES[others[0]]
        ladder.refuse(repo.at(others[0]), f"is {manager}'s lockfile, which this version of the platform does not install",
                      "convert it to a package-lock.json (docs/DEVELOPER-CONTRACT.md lists the commands), commit that and remove "
                      f"{others[0]}; {manager} lockfiles are a later roadmap item")
        return None
    dependencies = {}
    for key in ("dependencies", "optionalDependencies", "devDependencies"):
        table = package.get(key)
        dependencies.update(table if isinstance(table, dict) else {})
    local = sorted(name for name, spec in dependencies.items() if isinstance(spec, str) and re.match(
        r"^(file:|link:|workspace:|portal:|git\+|git:|github:|gitlab:|bitbucket:|https?:|[\w.-]+/[\w.-]+(#.*)?$)", spec.strip()))
    if local:
        ladder.refuse(repo.at("package.json"), "depends on " + ", ".join(local[:4]) + " through a local path, a git repository or a URL, which "
                      "npm installs without a registry check", "publish those packages to the npm registry, or move their code into the service")
        return None
    if not own:
        ladder.choose("Dependencies", "package.json, locked once by the platform: npm 11.10 or later, every install script off, no version "
                      "younger than three days", repo.at("package.json"))
        return {"action": "lock", "runtime": "node-http", "from": repo.at("package.json"), "package": repo.text("package.json")}
    name = own[0]
    text = repo.text(name)
    if not text:
        ladder.refuse(repo.at(name), "is larger than the platform reads (1 MB)", "tell the operator: reading larger lockfiles is a change to the platform")
        return None
    data, problem = nodejs.read_lockfile(text)
    if data is None:
        ladder.refuse(repo.at(name), problem, "lock again with npm 9 or later (npm install --package-lock-only --ignore-scripts) and commit the file")
        return None
    problems = nodejs.lock_problems(data)
    if problems:
        more = f"; and {len(problems) - 4} more" if len(problems) > 4 else ""
        ladder.refuse(repo.at(name), "holds packages npm ci would install without checking them against the npm registry: "
                      + "; ".join(f"{package_name} {why}" for package_name, why in problems[:4]) + more,
                      "install every package from the npm registry (publish or vendor what is your own), then lock again")
        return None
    declared = (data.get("packages") or {}).get("") or {}
    stale = sorted(key for key, table in (("dependencies", package.get("dependencies")), ("devDependencies", package.get("devDependencies")),
                                         ("optionalDependencies", package.get("optionalDependencies")))
                   if (table or {}) != (declared.get(key) or {}))
    if stale:
        ladder.refuse(repo.at(name), f"does not match package.json ({', '.join(stale)} differ), so npm ci would stop",
                      "run npm install and commit package.json and the lockfile together")
        return None
    ladder.choose("Dependencies", f"{name}, installed with npm ci and every install script off", repo.at(name))
    if name == "npm-shrinkwrap.json" and repo.has("package-lock.json"):
        ladder.ignore(repo.at("package-lock.json"), "npm-shrinkwrap.json comes first, as npm reads it")
    return {"action": "use", "runtime": "node-http", "from": repo.at(name), "file": name, "data": data}


def script_ladder(repo, ladder, package, lock):
    """build.scripts (D43): the lockfile's packages with an install script that package.json's allowScripts pins by version,
    less those a Cloud Run service never needs (nodejs.NOT_NEEDED); why the list may stay empty is said."""
    if lock is None or lock.get("action") != "use":
        ladder.choose("Install scripts", "none run: the platform's lock is made with every script off; approve one by version in "
                      "package.json's allowScripts if a package needs its own", repo.at("package.json"))
        return []
    scripts = nodejs.install_scripts(lock["data"])
    pinned, notes = nodejs.allowed_scripts(package)
    for note in notes:
        ladder.note(note)
    keep, needless = [], []
    for entry in scripts:
        if entry not in pinned:
            continue
        reason = nodejs.NOT_NEEDED.get(entry.rsplit("@", 1)[0])
        (needless if reason else keep).append(f"{entry}: {reason}" if reason else entry)
    unapproved = [entry for entry in scripts if entry not in pinned]
    if keep:
        ladder.choose("Install scripts", "run after the install, each by name and version: " + ", ".join(keep), f"{repo.at('package.json')} (allowScripts)")
    elif needless:
        ladder.choose("Install scripts", "none run: " + "; ".join(needless), f"{lock['from']} and {repo.at('package.json')} (allowScripts)")
    else:
        ladder.choose("Install scripts", "none run" + (": no package has one" if not scripts else ": a package whose native binary ships in its "
                      "tarball needs none"), lock["from"])
    if unapproved:
        ladder.note("These packages have install scripts the platform does not run, as package.json's allowScripts does not approve them by "
                    "version: " + ", ".join(unapproved[:8]) + ". If one is needed (a native addon built at install), approve it "
                    "(npm install-scripts approve) or list it under build.scripts in the review.")
    return keep


def start_file(words):
    """The file `node [flags] <file>` runs, or None when the words do not start node."""
    if not words or words[0].rsplit("/", 1)[-1] != "node":
        return None
    rest = [word for word in words[1:]]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if "=" not in flag and flag in ("-r", "--require", "--import", "--loader", "--env-file", "--env-file-if-exists") and rest:
            rest.pop(0)
    return rest[0].removeprefix("./") if rest else None


def start_refusal(repo, ladder, package, what, problem):
    """Refuse a start command, with the fix that suits what it does."""
    first = what.split()[0].rsplit("/", 1)[-1] if what.split() else ""
    if first in ("vite", "react-scripts", "ng", "parcel", "gatsby", "webpack", "webpack-dev-server"):
        fix = STATIC_FIX
    elif first in ("next", "nuxt", "nuxi", "astro", "remix", "nest", "ts-node", "tsx", "babel-node", "esno") or "transpiles" in problem:
        fix = BUILD_FIX
    else:
        fix = START_FIX
    ladder.refuse(repo.at("package.json"), f"scripts.start {problem}: `{what[:120]}`", fix)


def node_start(repo, ladder, base, compose, package, builds_itself, dockerfile, web, trust):
    """(recipe, entrypoint, command, args, words): what builds and starts a Node.js service (D45, D46). The repository's own
    Dockerfile builds itself; a Procfile goes to Google's buildpacks for reviewed code only, since they run install scripts and
    fetch Node.js at build time, and otherwise its web command runs on the platform's image; then Compose's command, the
    contract's build.entrypoint, scripts.start when it runs node, main, server.js and index.js."""
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    entrypoint, command, args, words = None, None, None, None
    recipe = (base.get("build") or {}).get("recipe")
    if recipe in (None, "repository") and builds_itself:
        own = dockerfile_command(repo.text(dockerfile))
        ladder.choose("Build", "the service's own Dockerfile", repo.at(dockerfile))
        if web:
            ladder.ignore(repo.at("Procfile"), "the Dockerfile comes first")
        words = own
        if compose and compose["body"].get("command") is not None:
            replaced = as_words(compose["body"]["command"])
            if replaced and replaced != own:
                args = words = replaced
                ladder.choose("Start command", " ".join(replaced)[:120] + " (replaces the Dockerfile's CMD)", f"{compose['file']} (service {compose['name']})")
        if compose and compose["body"].get("entrypoint") is not None and as_words(compose["body"]["entrypoint"]):
            command = as_words(compose["body"]["entrypoint"])
            ladder.choose("Entrypoint", " ".join(command)[:120] + " (replaces the Dockerfile's ENTRYPOINT)", f"{compose['file']} (service {compose['name']})")
        if args is None:
            ladder.choose("Start command", " ".join(own or [])[:120] or "the image's own", repo.at(dockerfile))
        if words and words[0].rsplit("/", 1)[-1] in ("npm", "yarn", "pnpm") and isinstance(scripts.get("start"), str):
            found, _ = nodejs.start_words(scripts["start"])
            words = found["words"] if found else words  # where npm start leads, for reading the listen call
        return "repository", None, command, args, words
    sources = []
    if web and recipe in (None, "repository", "buildpacks"):
        try:
            web_words = shlex.split(web)
        except ValueError:
            web_words = []
        if trust == "reviewed":
            ladder.choose("Build", f"Google's buildpacks, started by the Procfile's web command ({web[:80]})", repo.at("Procfile"))
            return ("buildpacks" if recipe == "buildpacks" else "repository"), None, None, None, web_words
        ladder.ignore(repo.at("Procfile"), "Google's buildpacks run install scripts and fetch Node.js at build time, so they build reviewed code "
                                           "only (trust: reviewed); the platform's image runs its web command instead")
        sources.append((" ".join(web_words), web_words, repo.at("Procfile")))
    if compose and compose["body"].get("command") is not None and as_words(compose["body"]["command"]):
        compose_words = as_words(compose["body"]["command"])
        sources.append((" ".join(compose_words), compose_words, f"{compose['file']} (service {compose['name']})"))
    if (base.get("build") or {}).get("entrypoint"):
        sources.insert(0, (base["build"]["entrypoint"], base["build"]["entrypoint"].split(), repo.at("platform.yaml")))
    ladder.choose("Build", "the platform's own Node.js image, made at build time; nothing is added to the repository", f"no Dockerfile in {repo.path or 'the repository root'}")
    for text, candidate, where in sources:
        if start_file(candidate) is None:
            ladder.refuse(where, f"starts the service with {text[:80]}, not node itself", START_FIX)
            return "platform", None, None, None, None
        ladder.choose("Start command", text[:120], where)
        return "platform", text, None, None, candidate
    start = scripts.get("start")
    if isinstance(start, str) and start.strip():
        found, problem = nodejs.start_words(start)
        if problem:
            start_refusal(repo, ladder, package, start, problem)
            return "platform", None, None, None, None
        if found["dropped"]:
            ladder.note(f"scripts.start reads an environment file ({', '.join(found['dropped'])}); the image never carries .env, so the platform "
                        "starts node without it: give the values under env and secrets in the contract")
        if found["env"]:
            ladder.note("scripts.start sets " + ", ".join(f"{key}={value}" for key, value in found["env"].items())
                        + " before node; give them under env in the contract if the service needs them")
        if isinstance(scripts.get("prestart"), str):
            ladder.note(f"scripts.prestart (`{scripts['prestart'][:80]}`) runs before npm start only; the platform starts node itself, so it never runs")
        entrypoint = " ".join(shlex.quote(word) for word in found["words"])
        ladder.choose("Start command", entrypoint[:120], f"{repo.at('package.json')} (scripts.start)")
        return "platform", entrypoint, None, None, found["words"]
    main = package.get("main")
    for candidate, where in ((main.removeprefix("./") if isinstance(main, str) else None, f"{repo.at('package.json')} (main)"),
                             ("server.js", None), ("index.js", None)):
        if candidate and repo.has(candidate):
            ladder.choose("Start command", f"node {candidate}", where or repo.at(candidate))
            return "platform", f"node {candidate}", None, None, ["node", candidate]
    return "platform", None, None, None, None


def node_listen(repo, ladder, sources, order, dependencies, base):
    """Where the code listens (D46): the first listen call of the entry file and what it imports. A literal port that never
    reads PORT, a loopback host and Fastify without host: '0.0.0.0' are refused; a host read from a variable whose
    default is loopback gets 0.0.0.0 in the draft's env. Returns (port literal or None, {variable: value})."""
    facts = nodejs.listen_facts(sources, order, dependencies)
    if facts is None:
        ladder.note("No listen call was found in the code read: check that the service listens on PORT (Cloud Run sets it) on 0.0.0.0")
        return None, {}
    where = repo.at(facts["file"])
    if facts["port"] is not None and not facts["port_env"] and not nodejs.any_reads_port(sources):
        ladder.refuse(where, f"listens on port {facts['port']} and never reads PORT, which Cloud Run sets",
                      "listen on process.env.PORT, with the literal as its default: server.listen(process.env.PORT || 8080)")
    env = {}
    if facts["host"] in nodejs.LOOPBACK:
        if facts["host_env"] and facts["host_env"] in (base.get("env") or {}):
            ladder.choose("Host", f"{base['env'][facts['host_env']]} through {facts['host_env']}", repo.at("platform.yaml"))
        elif facts["host_env"]:
            env[facts["host_env"]] = "0.0.0.0"  # noqa: S104 - Cloud Run's contract: listen on every interface
            ladder.choose("Host", f"0.0.0.0 through {facts['host_env']} (the code's default, {facts['host']}, cannot be reached on Cloud Run)", where)
        else:
            ladder.refuse(where, f"listens on {facts['host']}, which Cloud Run cannot reach", "listen on every interface: leave the host out, or give '0.0.0.0'")
    if facts["fastify_without_host"]:
        ladder.refuse(where, "starts Fastify without host: '0.0.0.0', and Fastify listens on localhost unless told otherwise, which Cloud Run cannot reach",
                      "pass host: '0.0.0.0' to fastify.listen()")
    return facts["port"], env


def node_health(repo, ladder, base, compose, sources, order):
    """The health route: the contract, a Compose healthcheck, a /health or /healthcheck route literal (Express, Fastify, Koa,
    Hono, Hapi, req.url === in a plain server), else none: Cloud Run's TCP startup probe. Readiness routes and paths Cloud Run
    reserves are offered to the reviewer, never chosen, as for Python."""
    if "health_path" in base:
        ladder.choose("Health", base["health_path"] or "none (TCP startup probe)", repo.at("platform.yaml"))
        return base["health_path"]
    if compose:
        path = compose_health(compose["body"])
        if path and not nodejs.RESERVED_ROUTE.search(path):
            ladder.choose("Health", path, f"{compose['file']} (service {compose['name']} healthcheck)")
            return path
    seen = nodejs.routes(sources, order)
    healthy = [path for path in seen if nodejs.HEALTH_ROUTE.search(path) and not nodejs.RESERVED_ROUTE.search(path)]
    if healthy:
        # The entry file's own route before one in another file (a tool's server), then /health itself, then the shortest.
        path = min(healthy, key=lambda item: (order.index(seen[item]), item.rstrip("/") != "/health", len(item)))
        ladder.choose("Health", path, repo.at(seen[path]))
        return path
    readiness = sorted(path for path in seen if nodejs.READINESS_ROUTE.search(path))
    if readiness:
        ladder.note(f"The code routes {', '.join(readiness[:3])}: if one answers 200 at once without calling other services, set health_path "
                    "to it in the review, and the release checks HTTP instead of TCP")
    reserved = sorted(path for path in seen if nodejs.RESERVED_ROUTE.search(path) and re.search(r"(health|ready|live)z/?$", path))
    if reserved:
        ladder.note(f"{repo.at(seen[reserved[0]])} routes {reserved[0]}, which Cloud Run keeps for itself (requests from outside never reach the "
                    "container): the release uses the TCP startup probe instead; a /health route would let it check HTTP")
    ladder.choose("Health", "none: Cloud Run's TCP startup probe; the HTTP health probe is off", "no health route found in the code read")
    return None


def node_read(repo, ladder, result, base, compose, name, paths):
    """The Node.js ladders of docs/RESEARCH-2026-10-19.md section 4.2 (D41 to D46) for one service, after the checks every
    service gets. Every ladder runs even after a refusal, so the answer lists everything to fix at once."""
    where = repo.path or "the repository root"
    package = nodejs.package_data(repo.text("package.json"))
    if not repo.has("package.json"):
        ladder.refuse(where, "has Node.js code but no package.json", "add a package.json that lists the dependencies (npm init -y, then npm install each one)")
        return result
    if package is None:
        ladder.refuse(repo.at("package.json"), "is not valid JSON", "fix package.json so npm can read it")
        return result
    root = nodejs.package_data(repo.texts.get("package.json", "")) if repo.path else None
    for folder in ("node_modules", "dist", ".next"):
        if any(item.startswith(folder + "/") for item in repo.local_paths):
            ladder.ignore(repo.at(folder), "committed build output or installed packages: the platform installs the dependencies itself" if folder == "node_modules"
                          else "committed build output, which the platform neither builds nor needs unless the start command runs it")
    members = nodejs.workspace_packages(package, [item for item in repo.local_paths])
    workspace = [repo.at(item) for item in NODE_WORKSPACE_FILES if repo.has(item)]
    if package.get("workspaces") is not None or workspace:
        ladder.refuse(repo.at("package.json") if package.get("workspaces") is not None else workspace[0],
                      "is a workspace of several packages" + (f" ({', '.join(members[:6])})" if members else ""),
                      "take one package as the service" + (f" (path {repo.at(members[0])})" if members else "") + " once it has a lockfile of its own; "
                      "one service per package with the root lockfile is a later roadmap item")
    elif root and root.get("workspaces") is not None and repo.path in nodejs.workspace_packages(root, sorted(repo.paths)):
        ladder.refuse(repo.path, "is a package of the repository's workspace (the root package.json lists it), locked by the root's lockfile",
                      "deploy it once workspaces are supported (a later roadmap item), or give it a lockfile of its own")
    if package.get("bundleDependencies") or package.get("bundledDependencies"):
        ladder.refuse(repo.at("package.json"), "bundles packages (bundleDependencies), which npm installs without checking them against the registry",
                      "list them as ordinary dependencies")
    engines = package.get("engines") if isinstance(package.get("engines"), dict) else {}
    if "iojs" in engines:
        ladder.refuse(repo.at("package.json"), "names io.js in engines, which merged back into Node.js in 2015", "remove engines.iojs; engines.node says the line")
    manager = nodejs.manager_of(package)
    named = [item for item in nodejs.MANAGERS if item in engines and item != manager]
    if manager and named:
        ladder.refuse(repo.at("package.json"), f"names {manager} in packageManager but {named[0]} in engines, two package managers",
                      "keep one: the platform installs with npm, so name npm in both, or remove one")
    elif manager and manager != "npm":
        ladder.note(f"packageManager names {manager}; the platform installs with npm from the lockfile, without Corepack")
    for rc, yarn in ((".npmrc", False), (".yarnrc.yml", True)):
        for location, text in repo.nearest(rc):
            if nodejs.literal_token(text, yarn):
                ladder.refuse(location, "holds a literal registry token", ROTATE_AND_REMOVE)
            elif text.strip():
                ladder.note(f"{location} is not read: the platform installs from the public npm registry with npm's own settings")

    dockerfile = next((n for n in ("Dockerfile", "dockerfile") if repo.has(n)), None)
    web = procfile_web(repo)
    trust = base.get("trust") if base.get("trust") in ("reviewed", "unknown") else "unknown"
    recipe = (base.get("build") or {}).get("recipe")
    builds_itself = recipe in (None, "repository") and bool(dockerfile)
    lock = npm_lock(repo, ladder, package)
    dependencies = nodejs.dependency_names(package, (lock or {}).get("data"))
    if builds_itself:
        native = nodejs.has_native_code((lock or {}).get("data"), dependencies)
        for finding in dockerpolicy.check(repo.text(dockerfile), trust, native=native):
            if finding["refuses"]:
                ladder.refuse(f"{repo.at(dockerfile)}:{finding['line']}", finding["problem"], finding["fix"])
            else:
                ladder.note(f"{repo.at(dockerfile)}:{finding['line']}: {finding['problem']} ({finding['fix']})")
    line = node_line(repo, ladder, base, dockerfile if builds_itself else None, package) or default_node()
    if line == "22":
        ladder.note(f"Node.js 22 is in maintenance and reaches its end of life on {NODE_END_OF_LIFE['22'].isoformat()}; releases are refused "
                    f"from that day, so move to {default_node()} before then")
    scripts = script_ladder(repo, ladder, package, lock)
    before = len(ladder.refusals)
    recipe, entrypoint, command, args, words = node_start(repo, ladder, base, compose, package, builds_itself, dockerfile, web, trust)
    start_refused = len(ladder.refusals) > before
    file = start_file(words)
    build_script = (package.get("scripts") or {}).get("build") if isinstance(package.get("scripts"), dict) else None
    runtime_deps = nodejs.runtime_names(package)
    sources = {item[len(repo.prefix):]: text for item, text in repo.texts.items() if item.startswith(repo.prefix)
               and nodejs.CODE.search(item) and not nodejs.NOT_CODE.search(item) and not nodejs.NODE_TEST.search(item[len(repo.prefix):])}
    if recipe != "repository":
        frameworks = sorted({nodejs.NEEDS_BUILD[item] for item in runtime_deps if item in nodejs.NEEDS_BUILD})
        static = [item for item in nodejs.STATIC_TOOLS if item in dependencies]
        if frameworks:
            ladder.refuse(repo.at("package.json"), f"uses {', '.join(frameworks)}, which needs a build before it can start", BUILD_FIX)
        elif words is None and not start_refused and static and not any(item in dependencies for item in nodejs.SERVERS) \
                and not any(nodejs.SERVER_CODE.search(text) for text in sources.values()):
            ladder.refuse(where, f"looks like a front-end site built with {static[0]}, not a web service", STATIC_FIX)
        elif words is None and not start_refused:
            ladder.refuse(repo.at("package.json"), "says nothing that starts the service: no start script, main, server.js or index.js",
                          "add \"start\": \"node server.js\" to package.json's scripts, or give the start command in the contract (build.entrypoint)")
        if file and not repo.has(file):
            if build_script or file.split("/")[0] in ("dist", "build", "out", "lib", ".next"):
                ladder.refuse(repo.at("package.json"), f"starts {file}, which is not in the repository: the service needs a build first", BUILD_FIX)
            else:
                ladder.refuse(repo.at("package.json"), f"starts {file}, which is not in the repository", "commit it, or correct the start command")
        elif file and re.search(r"\.[cm]?ts$", file):
            if line == "22":
                ladder.refuse(repo.at(file), "is TypeScript, which Node.js 22 cannot run without a build", BUILD_FIX)
            else:
                ladder.note(f"{repo.at(file)} runs under Node's type stripping: enums, namespaces and parameter properties need "
                            "--experimental-transform-types in the start command")
        elif file and build_script:
            ladder.note(f"package.json's build script (`{str(build_script)[:80]}`) is not run: the service starts from {file} as committed; if the "
                        "image needs what the build makes, commit it or build with a Dockerfile of your own")
    if file and repo.has(file) and repo.at(file) not in repo.texts:
        ladder.note(f"{repo.at(file)}, which starts the service, was not read, so where it listens was not checked")
    # The entry file and the modules it imports come first; where it listens is read from them alone when they are known.
    modules = [file] if file in sources else []
    for item in local_imports(file or "", sources.get(file or "", "")):
        found = next((candidate for candidate in ([item] if nodejs.CODE.search(item) else [item + ext for ext in (".js", ".mjs", ".cjs", ".ts", "/index.js")])
                      if candidate in sources), None)
        if found:
            modules.append(found)
    order = list(dict.fromkeys(modules + sorted(sources, key=_node_rank)))
    listen_port, host_env = node_listen(repo, ladder, sources, modules or order, dependencies, base) if sources else (None, {})
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
    elif port_from_command(words):
        port = port_from_command(words)
        ladder.choose("Port", port, "a literal port in the start command")
    elif listen_port and 1024 <= listen_port <= 65535:
        port = listen_port
        ladder.choose("Port", port, f"{repo.at(order[0]) if order else where} (the listen call's default; Cloud Run passes it as PORT)")
    else:
        port = DEFAULT_PORT
        ladder.choose("Port", port, "the platform's default; Cloud Run passes it to the service as PORT")
    if not 1024 <= port <= 65535:
        ladder.refuse(where, f"the service listens on port {port}", "make it listen on PORT (8080 by default); ports below 1024 need root, which "
                                                                    "the platform's images never use")
        port = DEFAULT_PORT
    if entrypoint is not None and not re.fullmatch(ENTRYPOINT, entrypoint):
        ladder.refuse(repo.at("package.json"), "the start command uses quotes or characters the platform cannot pass on",
                      "give a plain start command (node and a file, $PORT allowed) in the contract's build.entrypoint")
        entrypoint = None

    health = node_health(repo, ladder, base, compose, sources, order)
    aliases = nodejs.setting_names(sources)
    names = set(aliases)
    if compose:
        names.update(name_ for name_ in compose_names(compose["body"].get("environment")) if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name_))
    for item in repo.local_paths:
        if EXAMPLE_ENV.search(item) and "/" not in item:
            names.update(re.findall(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,63})\s*=", repo.text(item)))
    names = sorted(names - {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "NODE_ENV"})
    if names:
        secret = [n for n in names if SECRET_NAME.search(n) and not NOT_SECRET.search(n)]
        ladder.note("The code reads these settings: " + ", ".join(names[:20]) + ". Before merging, give plain values under env and name secrets "
                    "under secrets (Secret Manager secrets called <service>-NAME); nothing was added."
                    + (f" These look like secrets: {', '.join(secret[:10])}." if secret else ""))
    if not nodejs.handles_sigterm(sources) and sources:
        ladder.note("No SIGTERM handler was found: Cloud Run sends SIGTERM and stops the container ten seconds later, so requests in flight fail; "
                    "close the server on SIGTERM (process.on('SIGTERM', () => server.close(() => process.exit(0))))")
    if any(nodejs.SQLITE.search(text) for text in sources.values()):
        ladder.note("It keeps a SQLite database on local disk, which Cloud Run loses at every restart and does not share between instances: "
                    "a replicated SQLite file is roadmap Phase 19 (state: sqlite); until then, keep that state in Firestore or Postgres")

    needs = dict(base.get("needs") or {})
    if compose and not needs:
        if any(compose["kinds"].get(n) == "postgres" for n in compose_names(compose["body"].get("depends_on"))):
            needs = {"database": "postgres"}
            if "DATABASE_URL" in compose_names(compose["body"].get("environment")):
                needs["database_auth"] = "password"
            ladder.choose("Needs", "database: postgres" + (" (by password: the code reads DATABASE_URL)" if "database_auth" in needs else ""),
                          f"{compose['file']} (service {compose['name']} depends on a Postgres service)")
    hints = [need for need, pattern in nodejs.NEEDS_HINTS if any(re.search(pattern, item) for item in dependencies)]
    if hints and not needs:
        ladder.note("The dependencies suggest needs; add the ones the service really uses under needs before merging (nothing was added): " + "; ".join(hints))
    for label, pattern in nodejs.UNPROVIDED:
        if any(re.search(pattern, item) for item in dependencies):
            ladder.note(f"{label} appears in the dependencies: the platform does not provide it")
    if any(re.search(nodejs.WORKERS, item) for item in dependencies):
        ladder.note("A job queue or scheduler appears in the dependencies: work that runs on its own schedule needs a Cloud Run job or a Pub/Sub "
                    "push service, since an idle service scales to zero")
    outside = sorted(runtime_deps & (nodejs.INTERNET_CLIENTS | {"@huggingface/transformers"}))
    if outside and not base.get("egress"):
        ladder.note(f"It uses {', '.join(outside[:5])}: if it calls services outside Google Cloud, add egress: internet in the review "
                    "(code nobody has reviewed reaches no internet otherwise)")

    how, what, notes = nodejs.tests_of(package, repo.local_paths, line)
    for note in notes:
        ladder.note(note)
    if how:
        ladder.choose("Tests", f"{what}, run with {how} before every build", f"{repo.at('package.json')} (scripts.test)" if how == "npm test" else where)
    else:
        ladder.choose("Tests", "none found: nothing checks the service's behaviour before a release (tests: none)", where)
    if ladder.refusals or lock is None:
        return result

    build = dict(base.get("build") or {})
    if recipe != "repository":
        build["recipe"] = recipe
    if entrypoint and recipe in ("platform", "buildpacks"):
        build["entrypoint"] = entrypoint
    if scripts and "scripts" not in build:
        build["scripts"] = scripts
    draft = {**{key: value for key, value in base.items() if key != "python"}, "name": name, "runtime": "node-http", "node": line, "port": port,
             "health_path": health, "tests": "required" if how else "none"}
    if build:
        draft["build"] = build
    if host_env:
        draft["env"] = {**host_env, **(base.get("env") or {})}
    if needs:
        draft["needs"] = needs
    if command is not None and "command" not in base:
        draft["command"] = command
    if args is not None and "args" not in base:
        draft["args"] = args
    try:
        spec = AppSpec.model_validate({**draft, "reviewed": True})
    except Exception as exc:  # noqa: BLE001 - the draft must be a valid contract, or nothing is proposed
        ladder.refuse(where, f"the reader's draft is not a valid contract ({str(exc).splitlines()[0][:200]})", "write the contract by hand from the choices above")
        return result
    result["spec"] = spec
    result["lock"] = {key: value for key, value in lock.items() if key != "data"}
    if how:
        result["tests"] = {"how": how, "what": what, "files": [], "tools": [], "own": None}
    return result


# ---- the reader
def read(paths, texts, path="", name=None, fallback_name=None, sizes=None):
    """One service of a repository taken as it is: what each ladder chose and from where, what it refused and why, the
    files it set aside, and the draft of the external contract. `sizes` maps paths to bytes when the caller has them."""
    repo = Repository(paths, texts, path)
    ladder = Ladder()
    where = repo.path or "the repository root"
    local = {item for item in repo.local_paths if item}
    result = {"service": None, "path": repo.path, "runtime": None, "choices": ladder.choices, "refusals": ladder.refusals, "ignored": ladder.ignored,
              "notes": ladder.notes, "spec": None, "lock": None, "tests": None}
    runtime = runtime_of(repo, local)
    if runtime is None:
        ladder.refuse(where, "no Python or Node.js code or manifest", "the platform deploys Python and Node.js web services; give the folder that holds one")
        return result
    result["runtime"] = runtime
    name_given = name
    name, name_note = name_for(name, repo, fallback_name, runtime)
    result["service"] = name
    if name_note:
        ladder.note(name_note)
    if runtime == "python-http" and repo.has("package.json"):
        ladder.note(f"{repo.at('package.json')} is there too: the service is read as Python; a Node.js service says runtime: node-http in its platform.yaml")

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
    if base.get("name") and name_given is None:
        name = result["service"] = base["name"]
    compose = compose_entry(repo, ladder)
    if compose:
        ladder.choose("Compose service", compose["name"], compose["file"])
    if runtime == "node-http":
        return node_read(repo, ladder, result, base, compose, name, paths)

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
    if builds_itself:
        # 14.1: the Dockerfile a repository taken as it is brings meets the platform's policy before its contract is proposed;
        # unless the draft says the lab reviewed it, it is code nobody has reviewed (D35).
        trust = base.get("trust") if base.get("trust") in ("reviewed", "unknown") else "unknown"
        for finding in dockerpolicy.check(repo.text(dockerfile), trust):
            if finding["refuses"]:
                ladder.refuse(f"{repo.at(dockerfile)}:{finding['line']}", finding["problem"], finding["fix"])
            else:
                ladder.note(f"{repo.at(dockerfile)}:{finding['line']}: {finding['problem']} ({finding['fix']})")

    # Every ladder runs even after a refusal, so the answer lists everything to fix at once.
    python = python_ladder(repo, ladder, base, dockerfile if builds_itself else None) or DEFAULT_PYTHON
    lock = lock_ladder(repo, ladder)
    if lock is None and not repo.path:
        # Nothing at the root, but a folder holds a service (a monorepo, a backend/ folder): say which one to take.
        nested = [entry["path"] for entry in services(paths, texts) if entry["path"]]
        for refusal in ladder.refusals:
            if nested and refusal["problem"].startswith("no dependency manifest"):
                refusal["fix"] = (f"the service lives in {', '.join(nested[:3])}: take that folder (path {nested[0]}), "
                                  "or add a requirements.txt with exact pins at the root")
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
        secret = [n for n in names if SECRET_NAME.search(n) and not NOT_SECRET.search(n)]
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
    outside = sorted(dependencies & INTERNET_CLIENTS)
    if outside and not base.get("egress"):
        # D35: code nobody has reviewed reaches Google's APIs and the lab's private network, never the internet, by default.
        ladder.note(f"It uses {', '.join(outside[:5])}: if it calls services outside Google Cloud, add egress: internet in the review "
                    "(code nobody has reviewed reaches no internet otherwise)")

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
