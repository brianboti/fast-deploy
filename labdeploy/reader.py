"""The reader (docs/ROADMAP.md 13.5, decision D34): what a repository taken as it is says about one of its services.

Each fact is read by an explicit precedence ladder (docs/RESEARCH-2026-10-16.md, section 4.4): the first rung that answers
wins, the files a higher rung made irrelevant are listed as ignored, and where guessing would start the reader refuses,
naming the file and the fix. Read-only and static like the analyzer and the scaffold: repository code is read as text,
never imported or run, and secret files are recognized by their names, never opened.

`read` returns the choices, refusals, ignored files and notes, the draft of the external contract (apps/<service>/
platform.yaml, D31) and what the platform must make for it: the dependency lock and, for the tests, a lock of the service
with its test tools. The containers that make those locks run in the controller (labdeploy.scaffold), never here.
"""
import ast
import configparser
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
from .models import ENTRYPOINT, NODE_END_OF_LIFE, NODE_ENDED, PYTHON_ENDED, AppSpec, default_node
from .scaffold import (
    DEFAULT_PORT,
    DEFAULT_PYTHON,
    DEV_PINS,
    ENTRY_FILES,
    FRAMEWORK_DEPENDENCIES,
    NODE_CONFIG_FILES,
    NOT_APPLICATION,
    PYTHONS,
    RESERVED_HEALTH,
    SERVER_PINS,
    Repository,
    existing_spec,
    find_applications,
    find_health,
    module_of,
    pyproject_data,
    pyproject_names,
    requirement_names,
    requires_python,
    satisfies,
    server_command,
    service_name,
    system_packages,
)

LARGE_FILE = 100 * 1024 * 1024
EVERY_INTERFACE = nodejs.EVERYWHERE[0]  # Cloud Run's contract: a service listens on every interface
PYTHON_MANIFESTS = ("requirements.txt", "requirements.lock", "pyproject.toml", "Pipfile", "Pipfile.lock", "uv.lock", "poetry.lock",
                    "environment.yml", "environment.yaml", "setup.py", "setup.cfg", "pdm.lock", "pylock.toml")
NODE_FILES = ("package.json", "package-lock.json", "npm-shrinkwrap.json", ".nvmrc", ".node-version", ".tool-versions", ".npmrc", ".yarnrc.yml")
NODE_ENTRIES = ("server.js", "index.js", "app.js", "main.js", "server.mjs", "index.mjs", "app.mjs", "server.cjs", "index.cjs", "src/index.js",
                "src/server.js", "src/app.js", "src/main.js", "src/index.mjs", "src/server.mjs", "server/index.js", "server/index.mjs",
                "server.ts", "index.ts", "src/index.ts", "src/server.ts", "src/main.ts")
# A requirements file the service's requirements.txt may include (-r), or that stands in for it when there is none: the
# one the folder names as its production or base list, else its only file (docs/ROADMAP.md 20.1).
REQUIREMENTS_FOLDER = ("requirements/production.txt", "requirements/prod.txt", "requirements/base.txt", "requirements/main.txt",
                       "requirements/requirements.txt", "requirements/common.txt", "requirements/default.txt")
# Python files ranked by their name at any depth (20.1): the frameworks' own discovery names first (FastAPI's main.py, app.py
# and api.py; Flask's app.py and wsgi.py; Litestar's app.py and application.py; Streamlit's streamlit_app.py; the launchers
# of Gradio demos), then the rest of a folder by depth. A file of this name three folders deep is read before sixty
# shallower helpers (found in the survey: open-webui's backend/open_webui/main.py was never read).
NAMED_ENTRIES = ("main.py", "app.py", "wsgi.py", "asgi.py", "server.py", "api.py", "application.py", "run.py", "streamlit_app.py",
                 "launch.py", "webui.py", "demo.py", "index.py", "__main__.py", "manage.py", "app/__init__.py")
# The dependency that names the framework decides the command when no application object was found (D50, 20.2), with the
# framework's own entry-file discovery list and the host and port it reads, in order of precedence. `$PORT` is Cloud Run's
# port; a literal port goes in the contract. `python FILE` rungs set the host through the variable the framework reads.
FRAMEWORK_STARTS = (
    ("streamlit", ("streamlit_app.py", "app.py", "main.py", "Home.py", "Hello.py", "streamlit/app.py"),
     ["streamlit", "run", "{file}", "--server.address", EVERY_INTERFACE, "--server.port", "$PORT", "--server.headless", "true"], {}),
    ("gradio", ("app.py", "main.py", "demo.py", "launch.py", "webui.py", "run.py"), ["python", "{file}"],
     {"GRADIO_SERVER_NAME": EVERY_INTERFACE, "GRADIO_SERVER_PORT": "{port}"}),
    ("chainlit", ("app.py", "main.py", "chainlit_app.py"), ["chainlit", "run", "{file}", "--host", EVERY_INTERFACE, "--port", "$PORT", "--headless"], {}),
    ("dash", ("app.py", "main.py", "index.py", "application.py"), ["python", "{file}"], {"HOST": EVERY_INTERFACE}),
    ("panel", ("app.py", "main.py"), ["panel", "serve", "{file}", "--address", EVERY_INTERFACE, "--port", "$PORT", "--allow-websocket-origin", "*"], {}),
    ("shiny", ("app.py", "main.py"), ["shiny", "run", "{file}", "--host", EVERY_INTERFACE, "--port", "$PORT"], {}),
    ("nicegui", ("main.py", "app.py"), ["python", "{file}"], {}),
    ("reflex", ("rxconfig.py",), ["reflex", "run", "--env", "prod", "--backend-only", "--backend-host", EVERY_INTERFACE, "--backend-port", "$PORT"], {}),
    ("aiohttp", ("main.py", "app.py", "server.py", "run.py"), ["python", "{file}"], {}),
    ("sanic", ("server.py", "main.py", "app.py"), ["python", "{file}"], {"SANIC_HOST": EVERY_INTERFACE}),
    ("tornado", ("main.py", "app.py", "server.py", "run.py"), ["python", "{file}"], {}),
)
# The frameworks with a user interface of their own, which no other framework serves: the dependency alone decides (D50).
UI_FRAMEWORKS = ("streamlit", "gradio", "chainlit", "dash", "panel", "shiny", "nicegui", "reflex")
# The last rung (D50): nothing names a framework, but one of these files is there; the first revision's log says the rest.
LAST_RUNG_FILES = ("main.py", "app.py", "server.py", "run.py")
# A literal port in a Python entry file: .run(port=N), .launch(server_port=N), run_app(app, port=N), or argparse's --port default.
PORT_LITERAL = re.compile(r"(?:\.run|\.launch|run_app|\.listen|\.serve)\(.{0,400}?\b(?:server_)?port\s*=\s*(\d{2,5})\b"
                          r"|add_argument\(\s*[\"']--port[\"'][^)]{0,200}default\s*=\s*(\d{2,5})\b", re.S)
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
# A documented example of an environment file, by any name between .env and its suffix (.env.local.example, 20.1).
EXAMPLE_ENV = re.compile(r"(?i)(^|/)\.env(\.[^/]+)?\.(example|sample|template|dist|defaults?|test)$")
ENVIRONMENTS = re.compile(r"(^|/)(\.?venv|env|site-packages|node_modules)/")
# System packages the platform's own image installs (20.3: Heroku's Aptfile, Railpack's and Streamlit's packages.txt, apt.txt);
# bindep.txt carries profiles the platform does not read, so it is still refused.
SYSTEM_PACKAGES = ("apt.txt", "Aptfile", "packages.txt", "apt-packages.txt")
UNREAD_PACKAGE_FILES = ("bindep.txt",)
READINESS = re.compile(r"""["'](/(?:ready|readiness|live|liveness|ping|status))/?["']""")
CONDA_FILES = ("environment.yml", "environment.yaml")
LOCK_FILES = ("requirements.lock", "uv.lock", "poetry.lock", "Pipfile.lock", "pylock.toml", "pdm.lock")
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
# How many settings the settings note names before it says how many more there are.
SETTINGS_SHOWN = 40
# Responses streamed as server-sent events (docs/ROADMAP.md 19.4): Cloud Run ends every request at the contract's
# timeout_seconds, a stream included.
STREAMS = re.compile(r"text/event-stream|EventSourceResponse")
# Python libraries that fetch models from the Hugging Face Hub on first use, into HOME (/tmp in the platform's images).
HUB_DOWNLOADERS = ("transformers", "sentence-transformers", "huggingface-hub")
# Anthropic's Vertex AI client, in a Python manifest (anthropic[vertex]) or in the code of either runtime (D47).
CLAUDE_VERTEX = re.compile(r"\banthropic\[[^\]]*\bvertex\b", re.I)
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
    names = ("requirements.txt", "requirements.lock", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "pylock.toml", "pdm.lock",
             "setup.py", "setup.cfg", *CONDA_FILES, "Procfile", "Dockerfile", "dockerfile", ".python-version", "runtime.txt", "platform.yaml",
             "manage.py", *SYSTEM_PACKAGES, *TEST_REQUIREMENTS, "pytest.ini", "pytest.toml", "tox.ini", *COMPOSE_FILES)
    wanted = [repo.at(name) for name in names if repo.has(name)]
    # The files a requirements.txt may include (-r requirements/base.txt), and a requirements/ folder in its place (20.1).
    wanted += sorted(repo.at(item) for item in repo.local_paths if re.fullmatch(r"requirements/(?:[^/]+/)?[^/]+\.(txt|in)|requirements[-._][^/]+\.txt", item))[:12]
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
        wanted += [repo.at(name) for name in (*NODE_FILES, *nodejs.MANAGER_LOCKFILES, *NODE_CONFIG_FILES, "ecosystem.config.json")
                   if repo.has(name) and repo.at(name) not in wanted]
        wanted += [name for name in ("package.json", ".nvmrc", ".node-version", ".tool-versions", ".npmrc", ".yarnrc.yml", "pnpm-workspace.yaml",
                                     *nodejs.LOCKFILES, *nodejs.MANAGER_LOCKFILES) if repo.path and name in repo.paths]
        code = [repo.at(item) for item in sorted(repo.local_paths, key=_node_rank) if nodejs.CODE.search(item) and not nodejs.NOT_CODE.search(item)
                and not nodejs.NODE_TEST.search(item) and repo.at(item) not in wanted]
        return list(dict.fromkeys(wanted + code[:max(limit - len(wanted), 0)]))
    tests = sorted(repo.at(item) for item in repo.local_paths if TEST_PATH.search(item) and item.endswith(".py") and not ENVIRONMENTS.search(item))[:4]
    code = [repo.at(item) for item in sorted(repo.local_paths, key=_rank) if item.endswith(".py") and not TEST_PATH.search(item)
            and not ENVIRONMENTS.search(item) and repo.at(item) not in wanted]
    return list(dict.fromkeys(wanted + tests + code[:max(limit - len(wanted) - len(tests), 0)]))


def _rank(relative):
    """The frameworks' discovery files first, by name wherever they live and the shallower first among equals, then the
    rest of the folder by depth; examples, docs and build output after everything (20.1)."""
    name = relative.rsplit("/", 1)[-1]
    aside = 1 if NOT_APPLICATION.search(relative) or re.match(r"(examples?|samples?|demos?|docs?|scripts?|tools?|notebooks?|benchmarks?)/", relative) else 0
    named = relative if relative in NAMED_ENTRIES else name if name in NAMED_ENTRIES else None
    if named:
        return (aside, 0, NAMED_ENTRIES.index(named), relative.count("/"), relative)
    return (aside, 1, 0, relative.count("/"), relative)


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
    entry = next((item for item in ((start or {}).get("file"), str(package.get("main") or "").removeprefix("./") or None, *NODE_ENTRIES)
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


LOCK_LIMIT = 5_000_000  # bytes: a monorepo's lockfile is often 2 to 5 MB (20.4); every other input stays within 64 KiB
TEXT_LIMIT = 65_536


def large_files(name):
    """Lock files can be far larger than the 64 KiB other inputs may be; they are read up to LOCK_LIMIT (5 MB)."""
    return name.rsplit("/", 1)[-1] in LOCK_FILES + nodejs.LOCKFILES + tuple(nodejs.MANAGER_LOCKFILES)


def size_limit(name):
    """How many bytes of a repository file the reader reads: 5 MB for a lockfile, 64 KiB for anything else."""
    return LOCK_LIMIT if large_files(name) else TEXT_LIMIT


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
    manifests = ("requirements.txt", "pyproject.toml", "Pipfile", "requirements.lock", "setup.py", "setup.cfg", "package.json")
    folders = sorted({item.rsplit("/", 1)[0] if "/" in item else "" for item in repo.paths
                      if item.rsplit("/", 1)[-1] in manifests and not ENVIRONMENTS.search(item) and item.count("/") <= 3})
    for where in folders:
        prefix = f"{where}/" if where else ""
        entries = [name for name in (*ENTRY_FILES, "manage.py", "streamlit_app.py", "launch.py", "webui.py", "demo.py", *NODE_ENTRIES[:9], "Procfile",
                                     "Dockerfile") if f"{prefix}{name}" in repo.paths]
        if not entries:
            # A package beside the manifest with an entry of its own (demos/blog/blog/main.py, found in the survey's aiohttp-demos).
            entries = sorted(item[len(prefix):] for item in repo.paths if item.startswith(prefix)
                             and re.fullmatch(r"[A-Za-z_]\w*/(main|app|__main__|server)\.py", item[len(prefix):]))
        if entries:
            found.append({"path": where, "from": f"{prefix}{entries[0]} beside its manifest"})
    return found


def service_folders(paths, texts, path=""):
    """The folders that hold services when the folder asked for holds none of its own: named in every refusal of a root
    above several services (20.1, D56). Empty for a service in a folder, or when the root itself is one."""
    if path:
        return []
    listed = services(paths, texts)
    if any(entry["path"] == "" for entry in listed):
        return []
    return [entry["path"] for entry in listed if entry["path"]]


def name_services(ladder, paths, texts, path=""):
    """A root that holds its services in folders names them in every refusal, not only the manifest one (20.1): each fix
    ends with the folders to take, and a note says that onboard_application with all=true takes every one (D56)."""
    folders = service_folders(paths, texts, path)
    if not folders or not ladder.refusals:
        return
    more = f" and {len(folders) - 4} more" if len(folders) > 4 else ""
    listed = ", ".join(folders[:4]) + more
    for refusal in ladder.refusals:
        if "take that folder" not in refusal["fix"] and "take one of" not in refusal["fix"]:
            refusal["fix"] = refusal["fix"].rstrip(".") + f"; or the service may live in {listed}: take one of them (path {folders[0]})"
    ladder.note(f"The root holds services in folders ({listed}): check or deploy one with its path, or say \"deploy everything\" "
                "(onboard_application with all=true) to open one contract per folder")


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


def pdm_lock_requirements(text):
    """A hash-locked requirements file from pdm.lock's default group (every package without a group, or in `default`), at its
    locked versions with the file hashes PDM recorded (20.3)."""
    data = tomllib.loads(text)
    lines, problems = [], []
    for package in data.get("package") or []:
        groups = package.get("groups") or ["default"]
        if "default" not in groups:
            continue
        name, version = package.get("name"), package.get("version")
        if package.get("git") or package.get("path") or package.get("url") or package.get("editable"):
            problems.append(f"{name or 'a package'} comes from a git, path or URL source")
            continue
        hashes = sorted({str(item.get("hash")) for item in package.get("files") or [] if isinstance(item, dict) and str(item.get("hash", "")).startswith("sha256:")})
        if not (name and version and hashes):
            problems.append(f"{name or 'a package'} has no recorded hashes")
            continue
        marker = package.get("marker")
        lines.append(f"{name}=={version}" + (f" ; {marker}" if marker else "") + " \\\n" + " \\\n".join(f"    --hash={h}" for h in hashes))
    header = "# Converted by Fast Deploy from pdm.lock: the default group at its locked versions and hashes.\n"
    return header + "\n".join(lines) + ("\n" if lines else ""), problems


def pylock_requirements(text):
    """A hash-locked requirements file from a pylock.toml (PEP 751), at its locked versions with the hashes of its wheels and
    source distribution (20.3)."""
    data = tomllib.loads(text)
    lines, problems = [], []
    for package in data.get("packages") or []:
        name, version = package.get("name"), package.get("version")
        if package.get("vcs") or package.get("directory") or package.get("archive"):
            problems.append(f"{name or 'a package'} comes from a git, path or archive source")
            continue
        files = list(package.get("wheels") or []) + ([package["sdist"]] if isinstance(package.get("sdist"), dict) else [])
        hashes = sorted({"sha256:" + str((item.get("hashes") or {}).get("sha256")) for item in files
                         if isinstance(item, dict) and (item.get("hashes") or {}).get("sha256")})
        if not (name and version and hashes):
            problems.append(f"{name or 'a package'} has no exact version or no hashes")
            continue
        marker = package.get("marker")
        lines.append(f"{name}=={version}" + (f" ; {marker}" if marker else "") + " \\\n" + " \\\n".join(f"    --hash={h}" for h in hashes))
    header = "# Converted by Fast Deploy from pylock.toml: every package at its locked version and hashes.\n"
    return header + "\n".join(lines) + ("\n" if lines else ""), problems


# ---- manifests without a lock, read as text and compiled by the platform (20.3)
def poetry_specifier(text):
    """A Poetry version constraint as a PEP 440 specifier: ^1.2 is >=1.2,<2.0; ~1.2 is >=1.2,<1.3; * is anything."""
    text = str(text or "").strip()
    if text in ("", "*"):
        return ""
    parts = []
    for item in text.split(","):
        item = item.strip()
        match = re.fullmatch(r"([\^~])\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", item)
        if not match:
            parts.append(item if re.match(r"[<>=!~]", item) else f"=={item}")
            continue
        op, major, minor, patch = match.group(1), int(match.group(2)), match.group(3), match.group(4)
        low = ".".join(str(part) for part in (major, int(minor or 0), int(patch or 0)))
        if op == "^":
            high = f"{major + 1}.0.0" if major else f"0.{int(minor or 0) + 1}.0" if minor is not None else "1.0.0"
        else:
            high = f"{major}.{int(minor) + 1}.0" if minor is not None else f"{major + 1}.0.0"
        parts.append(f">={low},<{high}")
    return ",".join(parts)


def python_marker(constraint):
    """A Poetry per-dependency `python` constraint as an environment marker (python_version >= '3.8' and python_version < '3.11')."""
    parts = []
    for clause in poetry_specifier(constraint).split(","):
        match = re.fullmatch(r"(>=|<=|==|!=|<|>)\s*v?(\d+(?:\.\d+)*)", clause.strip())
        if match:
            parts.append(f"python_version {match.group(1)} '{'.'.join(match.group(2).split('.')[:2])}'")
    return " and ".join(parts)


def poetry_requirements(data):
    """(requirements text, problems) from [tool.poetry.dependencies] of a project that has no poetry.lock: each dependency
    as a PEP 508 line for the platform to compile; optional ones are left out."""
    table = (((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {})
    lines, problems = [], []
    for name, spec in table.items():
        if not isinstance(name, str) or name.lower() == "python":
            continue
        choices = spec if isinstance(spec, list) else [spec]
        for choice in choices[:1]:
            if isinstance(choice, dict):
                if any(key in choice for key in ("git", "path", "url", "file")):
                    problems.append(f"{name} comes from a git, path or URL source")
                    break
                if choice.get("optional"):
                    break
                extras = f"[{','.join(str(item) for item in choice.get('extras') or [])}]" if choice.get("extras") else ""
                marker = str(choice["markers"]) if choice.get("markers") else python_marker(choice.get("python"))
                lines.append(f"{name}{extras}{poetry_specifier(choice.get('version'))}" + (f" ; {marker}" if marker else ""))
            else:
                lines.append(f"{name}{poetry_specifier(choice)}")
    return "\n".join(lines) + ("\n" if lines else ""), problems


def pipfile_requirements(text):
    """(requirements text, problems) from a Pipfile's [packages] when there is no Pipfile.lock: "*" is the name alone, a
    string is its specifier, a table carries version, extras and markers."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return "", [f"the Pipfile could not be read ({str(exc)[:80]})"]
    lines, problems = [], []
    for name, spec in (data.get("packages") or {}).items():
        if isinstance(spec, dict):
            if any(key in spec for key in ("git", "path", "file", "editable")):
                problems.append(f"{name} comes from a git, path or file source")
                continue
            extras = f"[{','.join(str(item) for item in spec.get('extras') or [])}]" if spec.get("extras") else ""
            version = str(spec.get("version") or "").strip()
            marker = str(spec["markers"]) if spec.get("markers") else ""
            lines.append(f"{name}{extras}{'' if version in ('', '*') else version}" + (f" ; {marker}" if marker else ""))
        else:
            version = str(spec or "").strip()
            lines.append(f"{name}{'' if version in ('', '*') else version}")
    return "\n".join(lines) + ("\n" if lines else ""), problems


def setup_cfg_requirements(text):
    """(requirements text, problems) from setup.cfg's [options] install_requires, read with configparser, never run."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        return "", [f"setup.cfg could not be read ({str(exc)[:80]})"]
    raw = parser.get("options", "install_requires", fallback="") if parser.has_section("options") else ""
    if raw.strip().startswith("file:"):
        return "", ["setup.cfg reads install_requires from another file (file:), which the platform does not follow"]
    lines = [line.strip() for line in raw.replace(";", "\n") .splitlines() if line.strip() and not line.strip().startswith("#")]
    return "\n".join(lines) + ("\n" if lines else ""), []


def setup_py_requirements(text):
    """(requirements text, problems) from setup.py's install_requires, read as a literal list with ast, never run: a list
    written in the setup() call, or a module-level name bound to one. Anything computed is a problem, not a guess."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return "", [f"setup.py could not be parsed ({str(exc)[:80]})"]
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                bindings[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "setup"):
            continue
        for keyword in node.keywords:
            if keyword.arg != "install_requires":
                continue
            value = keyword.value
            try:
                found = ast.literal_eval(value) if not isinstance(value, ast.Name) else bindings.get(value.id)
            except ValueError:
                found = None
            if not isinstance(found, (list, tuple)) or not all(isinstance(item, str) for item in found):
                return "", ["setup.py computes install_requires (it is not a plain list), so the platform cannot read it without running it"]
            lines = [item.strip() for item in found if item.strip()]
            return "\n".join(lines) + ("\n" if lines else ""), []
        return "", ["setup.py's setup() call has no install_requires"]
    return "", ["setup.py has no setup() call"]


def flatten_requirements(repo, relative, seen=None):
    """requirements text with every `-r other` include replaced by that file's text (20.1), following only files inside
    the service's folder that were read. Returns (text, problems)."""
    seen = seen if seen is not None else set()
    if relative in seen or len(seen) > 12:
        return "", [f"{relative} includes files in a loop or too deeply"]
    seen.add(relative)
    lines, problems = [], []
    for line in repo.text(relative).splitlines():
        match = re.match(r"^\s*(?:-r|--requirement)\s+(\S+)", line)
        constraint = re.match(r"^\s*(?:-c|--constraint)\s+(\S+)", line)
        if constraint:
            problems.append(f"{relative} applies constraints from {constraint.group(1)} (-c), which the platform does not follow: write the pins into the requirements")
            continue
        if not match:
            lines.append(line)
            continue
        import posixpath
        target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), match.group(1)))
        if target.startswith("..") or target.startswith("/"):
            problems.append(f"{relative} includes {match.group(1)}, outside the service's folder")
        elif not repo.has(target):
            problems.append(f"{relative} includes {match.group(1)}, which is not in the repository")
        elif not repo.text(target):
            problems.append(f"{relative} includes {match.group(1)}, which was not read (it is not a requirements file the reader fetches)")
        else:
            text, more = flatten_requirements(repo, target, seen)
            lines.append(f"# from {target}")
            lines.append(text.rstrip("\n"))
            problems += more
    return "\n".join(lines) + "\n", problems


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
            return python_ended(ladder, where, match.group(1), f"change {where} to 3.12 or a later supported version")
    if repo.text("Pipfile.lock"):
        try:
            version = ((json.loads(repo.text("Pipfile.lock")).get("_meta") or {}).get("requires") or {}).get("python_version")
        except ValueError:
            version = None
        if isinstance(version, str) and version in PYTHONS:
            ladder.choose("Python", version, repo.at("Pipfile.lock"))
            return version
        if isinstance(version, str) and re.fullmatch(r"3\.\d+", version):
            return python_ended(ladder, repo.at("Pipfile.lock"), version, "lock it again for Python 3.12 or later (pipenv lock --python 3.12)")
    for where, text in repo.nearest("pyproject.toml"):
        specifier = requires_python(pyproject_data(text))
        if not specifier:
            continue
        fits = [version for version in PYTHONS if satisfies(version, specifier)]
        if not fits:
            # D48: a ceiling below 3.11 (<3.11, <=3.10) names a line at its end of life; the lowest supported line runs it, with a note.
            ceilings = [f"3.{int(minor) - (op == '<')}" for op, minor in re.findall(r"(<=?)\s*v?3\.(\d+)", specifier)]
            if ceilings and all(version in PYTHON_ENDED for version in ceilings):
                highest = max(ceilings, key=lambda v: int(v.split(".")[1]))
                return python_ended(ladder, where, highest, "widen requires-python to include 3.12", f"requires Python {specifier}, so at most {highest}")
            ladder.refuse(where, f"requires Python {specifier}, which no supported version (3.11 to 3.15) meets", "widen requires-python to include 3.12")
            return None
        version = DEFAULT_PYTHON if DEFAULT_PYTHON in fits else fits[0]
        ladder.choose("Python", version, f"{where} (requires-python {specifier})")
        return version
    ladder.choose("Python", DEFAULT_PYTHON, "the platform's default: nothing in the repository names a version")
    return DEFAULT_PYTHON


def python_ended(ladder, where, version, fix, asks=None):
    """D48 for Python: a version file that asks for a line at its end of life gets the lowest supported line, with a note
    naming the date; a line the platform does not know (newer than 3.15, or older than any it names) is still refused."""
    if version in PYTHON_ENDED:
        lowest = PYTHONS[0]
        ladder.choose("Python", lowest, f"{where} (asks for {version}, at end of life since {PYTHON_ENDED[version].isoformat()}; the lowest supported line runs it)")
        ladder.note(f"{where} {asks or f'asks for Python {version}'}; that line reached its end of life on {PYTHON_ENDED[version].isoformat()}: the "
                    f"service is built, tested and audited on {lowest}, the lowest supported line, and the first revision's log says whether the code "
                    f"runs on it; update {where} when it does")
        return lowest
    ladder.refuse(where, f"asks for Python {version}, outside the supported 3.11 to 3.15", fix)
    return None


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
        for other in ("requirements.txt", "pyproject.toml", "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml"):
            if repo.has(other):
                ladder.ignore(repo.at(other), "requirements.lock comes first")
        return {"action": "use", "from": repo.at("requirements.lock"), "text": repo.text("requirements.lock")}
    if "--hash=" in requirements:
        ladder.choose("Dependencies", "requirements.txt, already hash-locked", repo.at("requirements.txt"))
        return {"action": "use", "from": repo.at("requirements.txt"), "text": requirements}
    if repo.has("uv.lock") and repo.has("pyproject.toml"):
        ladder.choose("Dependencies", "uv.lock, exported with its hashes (uv export, main dependencies only)", repo.at("uv.lock"))
        for other in ("requirements.txt", "poetry.lock", "Pipfile.lock", "pdm.lock", "pylock.toml"):
            if repo.has(other):
                ladder.ignore(repo.at(other), "uv.lock comes first")
        return {"action": "export", "from": repo.at("uv.lock"), "pyproject": repo.text("pyproject.toml"), "lock": repo.text("uv.lock")}
    for name, convert in (("poetry.lock", poetry_lock_requirements), ("Pipfile.lock", pipfile_lock_requirements), ("pdm.lock", pdm_lock_requirements),
                          ("pylock.toml", pylock_requirements)):
        if not repo.has(name):
            continue
        if not repo.text(name):
            ladder.refuse(repo.at(name), f"is larger than the platform reads ({LOCK_LIMIT // 1_000_000} MB)", "export a requirements.txt with exact pins beside it")
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
    listed = "requirements.txt" if requirements.strip() else None
    if listed is None and not repo.has("requirements.txt"):
        # No requirements.txt, but a requirements/ folder (20.1): the file it names as its production or base list, else its only one.
        folder = sorted(item for item in repo.local_paths if re.fullmatch(r"requirements/(?:[^/]+/)?[^/]+\.txt", item))
        listed = next((name for name in REQUIREMENTS_FOLDER if name in folder), folder[0] if len(folder) == 1 else None)
        if listed is None and folder:
            more = f" and {len(folder) - 6} more" if len(folder) > 6 else ""
            ladder.refuse(repo.at("requirements"), "holds several requirements files (" + ", ".join(item.split("/", 1)[1] for item in folder[:6]) + more
                          + ") and none is named as the production or base list, so which one the service installs is a person's choice",
                          "name the one the service installs: copy it to requirements.txt, or give its lines as pins there")
            return None
    if listed:
        text, problems = flatten_requirements(repo, listed)
        if problems:
            ladder.refuse(repo.at(listed), problems[0], "list every requirement in a file inside the service's folder, or write the pins into requirements.txt")
            return None
        if re.search(r"(?m)^\s*(-e\s|\.\.?/|/|file:|git\+)", text):
            ladder.refuse(repo.at(listed), "installs a local path or a git URL", "publish that package, or vendor it into the service's code")
            return None
        included = sorted(set(re.findall(r"(?m)^# from (\S+)$", text)))
        how = "compiled once by the platform with hashes" + (f", with what it includes ({', '.join(included[:4])})" if included else "")
        ladder.choose("Dependencies", f"{listed}, {how}", repo.at(listed))
        if repo.has("pyproject.toml"):
            ladder.ignore(repo.at("pyproject.toml"), f"{listed} comes first for dependencies")
        return {"action": "compile", "from": repo.at(listed), "input": text}
    dependencies = (pyproject.get("project") or {}).get("dependencies")
    if isinstance(dependencies, list) and dependencies:
        if any(re.search(r"@\s*(file:|git\+|\.)", str(item)) for item in dependencies):
            ladder.refuse(repo.at("pyproject.toml"), "depends on a local path or a git URL", "publish that package, or vendor it into the service's code")
            return None
        ladder.choose("Dependencies", "[project] dependencies of pyproject.toml, compiled once by the platform with hashes", repo.at("pyproject.toml"))
        return {"action": "compile", "from": repo.at("pyproject.toml"), "input": "\n".join(str(item) for item in dependencies) + "\n"}
    # Manifests without a lock (20.3): Poetry's table, a Pipfile, setup.cfg and setup.py are read as text and compiled by the
    # platform, as requirements.txt is; nothing in them is run.
    unlocked = []
    if (((pyproject.get("tool") or {}).get("poetry") or {}).get("dependencies")):
        unlocked.append(("pyproject.toml", "[tool.poetry.dependencies] of pyproject.toml", poetry_requirements(pyproject)))
    if repo.has("Pipfile") and repo.text("Pipfile"):
        unlocked.append(("Pipfile", "the Pipfile's [packages]", pipfile_requirements(repo.text("Pipfile"))))
    if repo.has("setup.cfg") and "install_requires" in repo.text("setup.cfg"):
        unlocked.append(("setup.cfg", "setup.cfg's install_requires", setup_cfg_requirements(repo.text("setup.cfg"))))
    if repo.has("setup.py") and repo.text("setup.py"):
        unlocked.append(("setup.py", "setup.py's install_requires, read as a literal list (never run)", setup_py_requirements(repo.text("setup.py"))))
    for name, label, (text, problems) in unlocked:
        if problems:
            ladder.refuse(repo.at(name), problems[0], "commit a lock the platform reads (uv.lock, poetry.lock, Pipfile.lock, pdm.lock) or a requirements.txt with exact pins")
            return None
        if text.strip():
            ladder.choose("Dependencies", f"{label}, locked once by the platform with hashes (there is no lock in the repository)", repo.at(name))
            return {"action": "compile", "from": repo.at(name), "input": text}
    manifests = [repo.at(name) for name in ("setup.py", "setup.cfg", "Pipfile") if repo.has(name)]
    ladder.refuse(manifests[0] if manifests else (repo.path or "the repository root"),
                  "no dependency manifest the platform reads (requirements.txt or a requirements/ folder, pyproject.toml dependencies, a Pipfile, "
                  "setup.cfg or setup.py with install_requires, or a uv, Poetry, Pipenv, PDM or pylock lock)",
                  "add a requirements.txt with exact pins: guessing packages from the imports is wrong too often")
    return None


def procfile_web(repo):
    match = re.search(r"(?m)^web:\s*(.+?)\s*$", repo.text("Procfile"))
    return match.group(1) if match else None


def django_entry(repo, ladder, dependencies):
    """gunicorn's (or uvicorn's) application for a Django project: WSGI_APPLICATION or ASGI_APPLICATION in its settings, else
    the one <package>/wsgi.py or asgi.py beside manage.py (20.1). A Django project is one with manage.py or django among its
    dependencies, never a settings file that mentions the word. None when it is not Django; False after a refusal."""
    sources = {item: text for item, text in repo.texts.items() if item.startswith(repo.prefix)}
    settings = [(item, text) for item, text in sources.items() if SETTINGS_FILE.search(item)]
    if not ("django" in dependencies or re.search(r"(?m)^\s*(?:from|import)\s+django\b", repo.text("manage.py"))):
        return None  # Django's manage.py imports django.core.management; Litestar's manage.py only mentions Django in a comment
    for item, text in settings:
        match = WSGI_APPLICATION.search(text)
        if match:
            ladder.choose("Application", f"{match.group(1)}:application (Django)", f"{item} (WSGI_APPLICATION)")
            return {"interface": "wsgi", "application": f"{match.group(1)}:application", "framework": "django", "factory": False, "app_dir": ""}
        match = re.search(r"""^\s*ASGI_APPLICATION\s*=\s*["']([\w.]+)\.application["']""", text, re.M)
        if match:
            ladder.choose("Application", f"{match.group(1)}:application (Django, ASGI)", f"{item} (ASGI_APPLICATION)")
            return {"interface": "asgi", "application": f"{match.group(1)}:application", "framework": "django", "factory": False, "app_dir": ""}
    manage = re.search(r"""DJANGO_SETTINGS_MODULE["']\s*,\s*["']([\w.]+)["']""", repo.text("manage.py"))
    package = manage.group(1).split(".")[0] if manage else None
    for kind, interface in (("wsgi", "wsgi"), ("asgi", "asgi")):
        files = sorted(item for item in repo.local_paths if re.fullmatch(rf"[A-Za-z_]\w*/{kind}\.py", item))
        chosen = f"{package}/{kind}.py" if package and f"{package}/{kind}.py" in files else files[0] if len(files) == 1 else None
        if chosen:
            module = chosen[:-3].replace("/", ".")
            ladder.choose("Application", f"{module}:application (Django{', ASGI' if kind == 'asgi' else ''})", f"{repo.at(chosen)} beside manage.py")
            return {"interface": interface, "application": f"{module}:application", "framework": "django", "factory": False, "app_dir": ""}
    where = settings[0][0] if settings else repo.at("manage.py")
    ladder.refuse(where, "is a Django project whose settings do not say WSGI_APPLICATION and which has no single <package>/wsgi.py beside manage.py",
                  "add WSGI_APPLICATION = \"<project>.wsgi.application\" to the settings")
    return False


def entry_rank(file, attribute="app"):
    """How the frameworks themselves would rank a candidate (20.2): their discovery files by name and depth, then the name
    of the object (FastAPI tries `app` then `api`; Flask and Litestar `app` then `application`). A lower tuple wins; two
    candidates with the same tuple are a tie, which is refused."""
    name = file.rsplit("/", 1)[-1]
    named = file if file in NAMED_ENTRIES else name if name in NAMED_ENTRIES else None
    by_name = {"app": 0, "api": 1, "application": 2, "server": 3}.get(attribute, 4)
    aside = 1 if NOT_APPLICATION.search(file) or re.match(r"(examples?|samples?|demos?|docs?|scripts?|tools?|notebooks?|benchmarks?)/", file) else 0
    return (aside, 0 if named else 1, NAMED_ENTRIES.index(named) if named else 0, file.count("/"), by_name)


def best_candidate(ladder, repo, items, what):
    """The candidate the frameworks' ranking puts first, or None after refusing a tie between the first two."""
    ranked = sorted(items, key=lambda item: entry_rank(item["file"], item["attribute"]))
    if len(ranked) > 1 and entry_rank(ranked[0]["file"], ranked[0]["attribute"]) == entry_rank(ranked[1]["file"], ranked[1]["attribute"]):
        ladder.refuse(repo.at(ranked[0]["file"]), f"several candidate {what} rank the same: " + ", ".join(f"{i['module']}:{i['attribute']}" for i in ranked[:6]),
                      "say which one serves the web requests: give the start command in the contract (build.entrypoint)")
        return None
    if len(ranked) > 1:
        ladder.note(f"Several {what} were found (" + ", ".join(f"{i['module']}:{i['attribute']}" for i in ranked[:6]) + f"); {ranked[0]['module']}:"
                    f"{ranked[0]['attribute']} serves, as the frameworks' own discovery would choose it; give another in build.entrypoint if that is wrong")
    return ranked[0]


def port_literal(text):
    """A literal port in a Python entry file: .run(port=N), .launch(server_port=N), run_app(app, port=N), argparse's --port default."""
    match = PORT_LITERAL.search(text or "")
    return int(match.group(1) or match.group(2)) if match else None


def framework_start(repo, ladder, framework, file, words, env, literal, port):
    """The command and environment a framework's own documentation gives for its entry file (D50); aiohttp's factory runs
    under python -m aiohttp.web and its module-level object under gunicorn's aiohttp worker."""
    text = repo.text(file)
    if framework == "aiohttp":
        factory = re.search(r"(?m)^\s*(?:async\s+)?def\s+(init|create_app|make_app|app_factory)\s*\(", text)
        if factory and "web.Application(" in text:
            module = file[:-3].replace("/", ".")
            words = ["python", "-m", "aiohttp.web", "-H", EVERY_INTERFACE, "-P", "$PORT", f"{module}:{factory.group(1)}"]
            ladder.choose("Start command", " ".join(words), f"{repo.at(file)} (aiohttp's factory {factory.group(1)}, as python -m aiohttp.web runs it)")
            return {"command": words, "env": {}, "port": None, "framework": framework, "file": file}
        served = re.search(r"(?m)^(app|application)\s*=\s*(?:web\.|aiohttp\.web\.)?Application\(", text)
        if served:
            module = file[:-3].replace("/", ".")
            words = ["gunicorn", f"{module}:{served.group(1)}", "--bind", f"0.0.0.0:{port}", "--worker-class", "aiohttp.GunicornWebWorker", "--workers", "1", "--timeout", "0"]
            ladder.choose("Start command", " ".join(words), f"{repo.at(file)} (an aiohttp application object, served by gunicorn's aiohttp worker)")
            return {"command": words, "env": {}, "port": None, "framework": framework, "file": file, "server": "gunicorn"}
    command = [word.replace("{file}", file) for word in words]
    chosen = {key: value.replace("{port}", str(literal or port)) for key, value in env.items()}
    manifest = repo.at("requirements.txt") if repo.has("requirements.txt") else repo.at("pyproject.toml")
    ladder.choose("Start command", " ".join(command), f"{manifest} ({framework} among the dependencies; {repo.at(file)} is its entry file)")
    if command[0] == "python" and "HOST" not in chosen and reads_setting(text, "HOST"):
        # 1.22.1: the file itself binds the host it reads from HOST, usually with a development default (127.0.0.1) that Cloud
        # Run cannot reach; the contract sets it, as it does for a Node.js service (1.20.0). The last rung sets it always.
        chosen["HOST"] = EVERY_INTERFACE
        ladder.choose("Host", "0.0.0.0 through HOST (the code reads it; a development default such as 127.0.0.1 cannot be reached on Cloud Run)", repo.at(file))
    if literal and literal != port:
        ladder.note(f"{repo.at(file)} starts {framework} on port {literal}; the contract says port: {literal}, and Cloud Run sends requests there")
    ladder.note(f"No application object was found, so {framework} among the dependencies decides the command, as Google's buildpacks and "
                "Railpack do; the first revision's log says whether it serves, and build.entrypoint in the review overrides it")
    return {"command": command, "env": chosen, "port": literal, "framework": framework, "file": file}


def framework_entry(repo, ladder, dependencies, port):
    """D50: when no application object is found, the framework or server among the dependencies decides the command and the
    environment (Google's buildpacks' and Railpack's rule), with the entry file from the framework's own discovery list;
    then `python <main file>` for the first of main.py, app.py, server.py, run.py. Returns {"command", "env", "port",
    "framework", "file"} or None."""
    sources = repo.sources()
    starts = {name: (files, words, env) for name, files, words, env in FRAMEWORK_STARTS}
    # 1. A framework with a user interface of its own (Streamlit, Gradio, ...): its discovery files, else the named entry
    #    file that was read and imports it (the launcher of a demo is often called launch.py or webui.py).
    for framework in UI_FRAMEWORKS:
        if framework not in dependencies:
            continue
        files, words, env = starts[framework]
        file = next((name for name in files if repo.has(name)), None)
        if file is None:
            imports = re.compile(rf"(?m)^\s*(?:import|from)\s+{framework}\b")
            file = next((name for name in sorted(sources, key=_rank) if name.rsplit("/", 1)[-1] in NAMED_ENTRIES and imports.search(sources[name])), None)
        if file is not None:
            return framework_start(repo, ladder, framework, file, words, env, port_literal(sources.get(file, "")), port)
    # 2. An ASGI or WSGI framework among the dependencies whose object was not read (made inside a function, or in a file over
    #    the size the reader fetches): its entry file by name at any depth, module:app assumed, as the framework's own command
    #    line would (FastAPI's main.py and app.py, Flask's app.py and wsgi.py, Litestar's app.py and app/__init__.py).
    for interface_name, interface in FRAMEWORK_DEPENDENCIES:
        if interface_name not in dependencies or interface_name == "django":
            continue
        for file in sorted((name for name in repo.local_paths if name.endswith(".py") and not TEST_PATH.search(name)
                            and (name.rsplit("/", 1)[-1] in ("main.py", "app.py", "api.py", "wsgi.py", "asgi.py", "application.py", "server.py")
                                 or name == "app/__init__.py")), key=_rank):
            if _rank(file)[0]:
                break  # examples, docs, scripts and build output are not the service
            located = module_of(file, "src/__init__.py" in repo.local_paths)
            if not located:
                continue
            app_dir, module = located
            words = server_command(interface, f"{module}:app", app_dir, False, port)
            ladder.choose("Start command", " ".join(words), f"{repo.at(file)} ({interface_name} among the dependencies, no application object read: {module}:app assumed)")
            ladder.note(f"No application object of {interface_name} was read" + (f" ({repo.at(file)} is larger than the reader fetches)" if repo.at(file) not in repo.texts and repo.has(file) else "")
                        + f", so the platform assumes {module}:app in {repo.at(file)}, as the framework's own command line would; the first revision's log "
                        "says whether it serves, and build.entrypoint in the review overrides it")
            return {"command": words, "env": {}, "port": None, "framework": interface_name, "file": file, "server": words[0]}
    # 3. aiohttp, Sanic and Tornado, which are also client or helper libraries: only a discovery file at the service's root counts.
    for framework in ("aiohttp", "sanic", "tornado"):
        if framework not in dependencies:
            continue
        files, words, env = starts[framework]
        file = next((name for name in files if repo.has(name)), None)
        if file is not None:
            return framework_start(repo, ladder, framework, file, words, env, port_literal(sources.get(file, "")), port)
    file = next((name for name in LAST_RUNG_FILES if repo.has(name)), None)
    if file is None:
        return None
    literal = port_literal(sources.get(file, ""))
    ladder.choose("Start command", f"python {file}", f"{repo.at(file)} (the last rung: nothing names a framework, so the file itself is started)")
    ladder.note(f"Nothing in the repository names a web framework, so the platform starts `python {file}` with HOST=0.0.0.0 and PORT set, as "
                "Railpack does; the first revision's log says whether it listens, and build.entrypoint in the review overrides it")
    return {"command": ["python", file], "env": {"HOST": EVERY_INTERFACE}, "port": literal, "framework": None, "file": file}


def application_ladder(repo, ladder, dependencies, port=DEFAULT_PORT):
    """What the platform starts when nothing else says: [tool.fastapi] entrypoint, Django's application, the application
    object or factory the frameworks' own ranking puts first (a tie is refused), the framework among the dependencies
    (D50), then `python <main file>`. Returns a dict with either an application to serve or a command to run."""
    fastapi = ((pyproject_data(repo.text("pyproject.toml")).get("tool") or {}).get("fastapi") or {}).get("entrypoint")
    if isinstance(fastapi, str) and re.fullmatch(r"[\w.]+:[\w]+", fastapi):
        ladder.choose("Application", fastapi, f"{repo.at('pyproject.toml')} ([tool.fastapi] entrypoint)")
        return {"interface": "asgi", "application": fastapi, "framework": "fastapi", "factory": False, "app_dir": ""}
    django = django_entry(repo, ladder, dependencies)
    if django is not None:
        return django or None
    objects, factories = find_applications(repo.sources(), "src/__init__.py" in repo.local_paths)
    seen = set()
    objects = [item for item in objects if (item["module"], item["attribute"]) not in seen and not seen.add((item["module"], item["attribute"]))]
    if objects:
        item = best_candidate(ladder, repo, objects, "application objects")
        if item is None:
            return None
        interface = item.get("interface") or next((i for name, i in FRAMEWORK_DEPENDENCIES if name in dependencies), None)
        if interface is None:
            ladder.refuse(repo.at(item["file"]), f"{item['module']}:{item['attribute']} is made by a factory of an unrecognized framework",
                          "give the start command in the contract (build.entrypoint)")
            return None
        ladder.choose("Application", f"{item['module']}:{item['attribute']}", repo.at(item["file"]))
        return {"interface": interface, "application": f"{item['module']}:{item['attribute']}", "framework": item.get("framework"),
                "factory": False, "app_dir": item["app_dir"]}
    if factories:
        item = best_candidate(ladder, repo, factories, "application factories")
        if item is None:
            return None
        ladder.choose("Application", f"{item['module']}:{item['attribute']}() (a factory)", repo.at(item["file"]))
        return {"interface": item["interface"], "application": f"{item['module']}:{item['attribute']}", "framework": item["framework"],
                "factory": True, "app_dir": item["app_dir"]}
    notebooks = sorted(item for item in repo.local_paths if item.endswith(".ipynb") and not ENVIRONMENTS.search(item))
    if notebooks:
        ladder.note(f"{repo.at(notebooks[0])}" + (f" and {len(notebooks) - 1} more notebook{'s' if len(notebooks) > 2 else ''}" if len(notebooks) > 1 else "")
                    + " stay out of the image: a notebook is not what answers requests")
    found = framework_entry(repo, ladder, dependencies, port)
    if found:
        return found
    ladder.refuse(repo.path or "the repository root", "says nothing that starts the service: no application object of a known framework (FastAPI, "
                  "Starlette, Flask, Django, ...), no web framework or server among the dependencies with an entry file, and no main.py, app.py, "
                  "server.py or run.py", "give the start command in the contract (build.entrypoint), or add a Procfile with a web: line")
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


def reads_setting(text, name):
    """Whether a Python file reads the setting `name` from the environment, by the same patterns configuration_names uses."""
    return any(name in pattern.findall(text or "") for pattern in CONFIG_PATTERNS)


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


def settings_note(ladder, names):
    """The settings the code reads, by name (never values), up to SETTINGS_SHOWN of them, and those that look like secrets."""
    if not names:
        return
    shown = ", ".join(names[:SETTINGS_SHOWN]) + (f" and {len(names) - SETTINGS_SHOWN} more" if len(names) > SETTINGS_SHOWN else "")
    secret = [n for n in names if SECRET_NAME.search(n) and not NOT_SECRET.search(n)]
    ladder.note(f"The code reads these settings: {shown}. Before merging, give plain values under env and name secrets under secrets "
                "(Secret Manager secrets called <service>-NAME); nothing was added."
                + (f" These look like secrets: {', '.join(secret[:10])}." if secret else ""))


def data_app_notes(ladder, base, sources, direct, vertex, downloaders, node):
    """What a service that calls Claude, loads a model or streams its answers asks of the contract (D47, 19.4), for either
    runtime: notes for the reviewer, nothing added to the draft. `direct` names Anthropic's own client when the code uses it,
    `vertex` says Anthropic's Vertex AI client is there too, `downloaders` names the libraries that fetch models from Hugging
    Face on first use."""
    needs = base.get("needs") or {}
    if direct and not vertex and "claude" not in (needs.get("models") or []):
        client = nodejs.CLAUDE_VERTEX_CLIENT if node else "anthropic[vertex]"
        ladder.note(f"It calls Claude with Anthropic's own client ({direct}), which needs an API key and the internet: with needs.models: "
                    f"[claude] and Anthropic's Vertex AI client ({client}) the same calls go through Google Cloud with neither, billed inside "
                    "the project's budget and spending cap (docs/DEVELOPER-CONTRACT.md, section 4b)")
    for name in downloaders:
        if node:
            ladder.note(f"{name} fetches each model from Hugging Face on first use and keeps it inside node_modules by default, which the "
                        "service cannot write (version 4 stops with EACCES at the first model load): put the model's files in a mounted bucket "
                        "(needs.mounts) and point env.localModelPath at that folder with env.allowRemoteModels = false (no internet, nothing "
                        "fetched at a cold start), or set env.cacheDir to a folder under /tmp and add egress: internet")
        else:
            ladder.note(f"{name} fetches models from the Hugging Face Hub on first use, into HOME (memory on Cloud Run) at every new "
                        "instance, and code nobody has reviewed reaches no internet: put the model's files in a mounted bucket (needs.mounts) "
                        "and load them from that folder with HF_HUB_OFFLINE=1 under env, or add egress: internet")
    timeout = base.get("timeout_seconds") or 60
    if isinstance(timeout, int) and timeout <= 60 and any(STREAMS.search(text) for text in sources.values()):
        ladder.note(f"It streams its responses (server-sent events): Cloud Run closes a request when timeout_seconds runs out ({timeout} "
                    "seconds here), a stream still sending included, so set timeout_seconds in the review to the longest answer it must "
                    "carry (3600 at most)")


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
        return node_ended(ladder, where, line, f"change {name} to {default_node()}")
    engines = package.get("engines") if isinstance(package.get("engines"), dict) else {}
    ranges = [(f"engines.node {engines['node']}", engines["node"])] if isinstance(engines.get("node"), str) else []
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
            ended = sorted({match for match in re.findall(r"\b(\d+)(?:\.\d+)*", spec) if match in NODE_ENDED}, key=int)
            if ended and not re.search(r"\b(2[2-9]|[3-9]\d)\b", spec):
                return node_ended(ladder, repo.at("package.json"), ended[-1], f"widen {label.split()[0]} to allow {default_node()}",
                                  f"({label}) allows only Node.js {ended[-1]}")
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


def node_ended(ladder, where, line, fix, asks=None):
    """D48 for Node.js: a version file or range that asks for a line at its end of life gets the lowest supported line, with a
    note naming the date; a line the platform does not know (27 and later, or older than any it names) is still refused."""
    if line in NODE_ENDED:
        lowest = nodejs.NODES[0]
        ladder.choose("Node.js", lowest, f"{where} (asks for {line}, at end of life since {NODE_ENDED[line].isoformat()}; the lowest supported line runs it)")
        ladder.note(f"{where} {asks or f'asks for Node.js {line}'}; that line reached its end of life on {NODE_ENDED[line].isoformat()}: the service is "
                    f"built, tested and audited on {lowest}, the lowest supported line, and the first revision's log says whether the code runs on it; "
                    f"update {where} when it does")
        return lowest
    ladder.refuse(where, f"asks for Node.js {line}, outside the supported 22, 24 and 26", fix)
    return None


def node_lock(repo, ladder, package, member=False):
    """How the packages are pinned (D42, 21.1): {"action": "use", the repository's own lockfile, installed frozen by the manager
    that wrote it (npm, pnpm, Yarn or Bun) with every entry checked against the registry} or {"action": "lock", package.json
    locked once by the platform with npm}; None after a refusal. A workspace member (`member`, 21.4) installs from the root's
    lockfile, which must list it."""
    own = [name for name in nodejs.LOCKFILES if repo.has(name)][:1] + [name for name in nodejs.OTHER_LOCKFILES if repo.has(name)]
    at, text_of = repo.at, repo.text
    if not own and member:
        own = [name for name in (*nodejs.LOCKFILES, *nodejs.OTHER_LOCKFILES) if name in repo.paths][:1]
        at, text_of = (lambda name: name), (lambda name: repo.texts.get(name, ""))
    if len(own) > 1:
        ladder.refuse(at(own[0]), "has more than one lockfile (" + ", ".join(own) + "), so two package managers disagree about the versions",
                      "keep the one of the manager you use and remove the others")
        return None
    if own and own[0] == "bun.lockb":
        ladder.refuse(at("bun.lockb"), "is Bun's old binary lockfile, which only Bun reads", "run bun install with Bun 1.2 or later, which writes bun.lock in "
                      "text, commit that and remove bun.lockb")
        return None
    dependencies = {}
    for key in ("dependencies", "optionalDependencies", "devDependencies"):
        table = package.get(key)
        dependencies.update(table if isinstance(table, dict) else {})
    in_workspace = bool(own) and (member or package.get("workspaces") is not None or repo.has("pnpm-workspace.yaml"))
    local = sorted(name for name, spec in dependencies.items() if isinstance(spec, str) and re.match(
        r"^(file:|link:|portal:|git\+|git:|github:|gitlab:|bitbucket:|https?:|[\w.-]+/[\w.-]+(#.*)?$)", spec.strip()))
    linked = sorted(name for name, spec in dependencies.items() if isinstance(spec, str) and spec.strip().startswith(("workspace:", "catalog:")))
    if local:
        ladder.refuse(repo.at("package.json"), "depends on " + ", ".join(local[:4]) + " through a local path, a git repository or a URL, which "
                      "the manager installs without a registry check", "publish those packages to the npm registry, or move their code into the service")
        return None
    if linked and not in_workspace:
        ladder.refuse(repo.at("package.json"), "depends on " + ", ".join(linked[:4]) + " through workspace: or catalog:, and no workspace lockfile resolves them",
                      "commit the workspace's lockfile at its root, or publish those packages")
        return None
    if not own:
        ladder.choose("Dependencies", "package.json, locked once by the platform: npm 11.10 or later, every install script off, no version "
                      "younger than three days", repo.at("package.json"))
        return {"action": "lock", "runtime": "node-http", "from": repo.at("package.json"), "package": repo.text("package.json")}
    name = own[0]
    text = text_of(name)
    if not text:
        ladder.refuse(at(name), f"is larger than the platform reads ({LOCK_LIMIT // 1_000_000} MB)", "tell the operator: reading larger lockfiles is a change to the platform")
        return None
    view, problem = nodejs.read_lock(name, text)
    if view is None:
        ladder.refuse(at(name), problem, "lock again with the manager that wrote it (npm 9 or later: npm install --package-lock-only --ignore-scripts) and commit the file")
        return None
    manager = view["manager"]
    commands = nodejs.MANAGER_COMMANDS[manager]
    problems = nodejs.view_problems(view)
    if problems:
        more = f"; and {len(problems) - 4} more" if len(problems) > 4 else ""
        ladder.refuse(at(name), f"holds packages {commands['title']} would install without checking them against the npm registry: "
                      + "; ".join(f"{package_name} {why}" for package_name, why in problems[:4]) + more,
                      "install every package from the npm registry (publish or vendor what is your own), then lock again")
        return None
    if manager == "yarn":
        yarnrc = next((text for _, text in repo.nearest(".yarnrc.yml")), "")
        if not nodejs.yarn_links_node_modules(yarnrc):
            ladder.refuse(at(".yarnrc.yml") if yarnrc else at("yarn.lock"), "installs with Yarn's Plug'n'Play (no nodeLinker: node-modules in .yarnrc.yml), "
                          "whose loader the platform's image does not carry", "set nodeLinker: node-modules in .yarnrc.yml, run yarn install and commit both")
            return None
    if member:
        listed = {item.strip("./") for item in view.get("members", [])}
        if repo.path not in listed:
            ladder.refuse(at(name), f"does not list {repo.path} as a package of the workspace", "run the install at the workspace root so the lockfile lists it, and commit it")
            return None
    data = nodejs.read_lockfile(text)[0] if manager == "npm" else None
    if manager == "npm" and not member:
        declared = (data.get("packages") or {}).get("") or {}
        stale = sorted(key for key, table in (("dependencies", package.get("dependencies")), ("devDependencies", package.get("devDependencies")),
                                             ("optionalDependencies", package.get("optionalDependencies")))
                       if (table or {}) != (declared.get(key) or {}))
        if stale:
            # 20.4: npm ci would stop on a lockfile that disagrees with package.json; the platform locks package.json again instead,
            # as it does for a repository without a lockfile, and the lockfile is set aside.
            ladder.ignore(repo.at(name), f"does not match package.json ({', '.join(stale)} differ), so npm ci would stop; the platform locks package.json again")
            ladder.choose("Dependencies", f"package.json, locked again by the platform because {name} is stale: npm 11.10 or later, every install "
                          "script off, no version younger than three days", repo.at("package.json"))
            ladder.note(f"{repo.at(name)} is behind package.json ({', '.join(stale)} differ): the platform's lock replaces it for the build; run npm "
                        "install and commit the two together to have your own lockfile used")
            return {"action": "lock", "runtime": "node-http", "from": repo.at("package.json"), "package": repo.text("package.json")}
    chosen = None
    if manager != "npm":
        from .scaffold import MANAGER_PINS
        root_package = nodejs.package_data(repo.texts.get("package.json", "")) if member else None
        spec = nodejs.manager_spec(package) or nodejs.manager_spec(root_package)
        named = nodejs.manager_of(package) or nodejs.manager_of(root_package)
        family = "yarn" if manager in ("yarn", "yarn1") else manager
        if named and named != family and named != "npm":
            ladder.refuse(repo.at("package.json"), f"names {named} in packageManager, but the lockfile is {name}, {commands['title']}'s",
                          "keep one manager: remove the other's lockfile, or correct packageManager")
            return None
        if spec and spec["manager"] == manager:
            # The repository's own release of its manager, as Corepack and Railpack honour it: a lockfile is written for it.
            chosen = {"version": spec["version"], "integrity": spec["integrity"]}
            held = "the sha512 packageManager carries" if spec["integrity"] else "the integrity the npm registry publishes"
            how = f"{commands['title']} {spec['version']} from packageManager, installed by version and checked against {held}"
        else:
            if spec and spec["manager"] != manager:
                ladder.note(f"packageManager names {spec['manager']} {spec['version']}, which does not write {name}: the platform's own "
                            f"{commands['title']} is used")
            formats = nodejs.PINNED_FORMATS.get(manager)
            found_format = nodejs.lock_format(view)
            if formats and found_format not in formats:
                pinned = MANAGER_PINS[manager][0]
                fix = (f"name the release that wrote it in package.json (\"packageManager\": \"{'pnpm' if manager == 'pnpm' else 'yarn'}@<version>\"), "
                       f"or run the install with {commands['title']} {pinned.split('.')[0]} and commit the lockfile it writes")
                ladder.refuse(at(name), f"is {'lockfile version' if manager == 'pnpm' else 'Yarn lockfile format'} {found_format or 'unknown'}, which the "
                              f"platform's {commands['title']} {pinned} cannot install frozen, and package.json names no packageManager release", fix)
                return None
            how = f"{commands['title']} {MANAGER_PINS[manager][0]}, the platform's pin, installed by version and integrity"
    if manager == "npm" and nodejs.manager_of(package) not in (None, "npm"):
        ladder.note(f"packageManager names {nodejs.manager_of(package)}, but the lockfile is {name}, npm's: the platform installs it with npm ci")
    if manager == "npm":
        ladder.choose("Dependencies", f"{name}, installed with npm ci and every install script off" + (" (the workspace root's lockfile)" if member else ""), at(name))
    else:
        ladder.choose("Dependencies", f"{name}, installed with `{commands['frozen']}` ({how}; every install script off)"
                      + (" from the workspace root" if member else ""), at(name))
        ladder.note(f"{at(name)} is installed frozen by {commands['title']}: a lockfile behind package.json stops the build, so commit the two together")
    if name == "npm-shrinkwrap.json" and repo.has("package-lock.json"):
        ladder.ignore(repo.at("package-lock.json"), "npm-shrinkwrap.json comes first, as npm reads it")
    return {"action": "use", "runtime": "node-http", "from": at(name), "file": name, "data": data, "view": view, "manager": manager, "member": member,
            "manager_release": chosen}


def script_ladder(repo, ladder, package, lock):
    """build.scripts (D43, D53): the packages the platform's own table says need their install script on Cloud Run (21.5),
    drafted at their locked versions for the merge to approve, and those package.json approves for its manager (npm's
    allowScripts by version; pnpm's onlyBuiltDependencies, Yarn's dependenciesMeta built, Bun's trustedDependencies by name,
    joined with the locked version), less those a Cloud Run service never needs (nodejs.NOT_NEEDED). Why the list may stay
    empty is said."""
    if lock is None or lock.get("action") != "use":
        ladder.choose("Install scripts", "none run: the platform's lock is made with every script off; approve one by version in "
                      "package.json's allowScripts if a package needs its own", repo.at("package.json"))
        return []
    manager, view = lock.get("manager", "npm"), lock.get("view") or {}
    versions = nodejs.view_names(view)
    names, pinned, notes = nodejs.approved_scripts(package, manager)
    for note in notes:
        ladder.note(note)
    approved = list(dict.fromkeys(pinned + [f"{name}@{versions[name]}" for name in names if versions.get(name)]))
    scripted = nodejs.view_scripts(view)  # None: this lockfile's format does not say which packages run a script
    keep, needless, drafted = [], [], []
    for entry, why in nodejs.needed_scripts(versions):
        if scripted is not None and entry not in scripted:
            continue
        keep.append(entry)
        drafted.append(f"{entry} ({why})")
    for entry in approved:
        if entry in keep or (scripted is not None and entry not in scripted):
            continue
        reason = nodejs.NOT_NEEDED.get(entry.rsplit("@", 1)[0])
        (needless if reason else keep).append(f"{entry}: {reason}" if reason else entry)
    unapproved = [entry for entry in (scripted or []) if entry not in keep and entry not in approved]
    approvals = {"npm": "allowScripts", "pnpm": "pnpm.onlyBuiltDependencies", "yarn": "dependenciesMeta", "yarn1": "dependenciesMeta", "bun": "trustedDependencies"}[manager]
    if keep:
        ladder.choose("Install scripts", "run after the install, each by name and version: " + ", ".join(keep)
                      + (f"; from the platform's table of packages that need theirs on Cloud Run, for the merge to approve: {'; '.join(drafted)}" if drafted else ""),
                      f"{repo.at('package.json')} ({approvals})" if approved and not drafted else lock["from"])
    elif needless:
        ladder.choose("Install scripts", "none run: " + "; ".join(needless), f"{lock['from']} and {repo.at('package.json')} ({approvals})")
    else:
        ladder.choose("Install scripts", "none run" + (": no package has one" if scripted == [] else ": no package the platform's table names, and a package "
                      "whose native binary ships in its tarball needs none"), lock["from"])
    if unapproved:
        ladder.note(f"These packages have install scripts the platform does not run, as neither its table nor package.json's {approvals} names them: "
                    + ", ".join(unapproved[:8]) + ". If one is needed (a native addon built at install), list it under build.scripts in the review.")
    if scripted is None and not keep:
        ladder.note(f"{lock['file']} does not record which packages have install scripts, and the platform's table of packages that need theirs on Cloud Run "
                    "names none of these: no script runs; list one under build.scripts in the review if a native addon needs its own")
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


# What a test suite names when it needs what the platform's runner does not have (D54, 22.5): a browser, a database or a
# seed, Docker. Such a suite is drafted as tests: report, counted and never a stop.
NEEDS_MORE_TO_TEST = re.compile(r"(?i:playwright|cypress|selenium|webdriver|puppeteer|db:seed|docker|testcontainers)|\bDATABASE_URL\b|\bPOSTGRES_[A-Z]+|\bMONGO_URL\b|\bREDIS_URL\b")
# The folder each site builder writes into (D55, 22.4), read in their documentation; Angular's depends on the project, so it is not guessed.
SITE_OUTPUTS = (("@docusaurus/core", "build"), ("gatsby", "public"), ("@11ty/eleventy", "_site"), ("react-scripts", "build"), ("@vue/cli-service", "dist"),
                ("astro", "dist"), ("parcel", "dist"), ("vite", "dist"))


def scaffold_static_entrypoint():
    from .scaffold import STATIC_ENTRYPOINT
    return STATIC_ENTRYPOINT


def wrapped_user(base, trust):
    """build.user for a repository's own Dockerfile (D52): the contract's, else 65532 for code nobody has reviewed."""
    user = (base.get("build") or {}).get("user")
    return user if user else (65532 if trust == "unknown" else None)


def static_site(package, dependencies):
    """(output folder, why) for a front-end site with no server of its own (D55, 22.4), or (None, why not)."""
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    if not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
        return None, "it has no build script that makes the site"
    if "@sveltejs/kit" in dependencies:
        if "@sveltejs/adapter-static" in dependencies:
            return "build", "SvelteKit with adapter-static"
        return None, "SvelteKit builds a site only with @sveltejs/adapter-static (adapter-node makes a server); its adapter is another one"
    if "@angular/core" in dependencies:
        return None, "Angular writes the site into a folder its project names (dist/<project>/browser); say it in build.output"
    for name, output in SITE_OUTPUTS:
        if name in dependencies:
            return output, f"{name} writes the site into {output}/"
    return None, "no site builder the platform knows"


def node_chain(script):
    """`node a.js && node b.js` as an entrypoint when every part is node on a file with plain words, else None (D49)."""
    if re.search(r"\|\||[;|`<>]|\$\(|(?<!&)&(?!&)", script):
        return None
    parts = []
    for part in script.split("&&"):
        found, problem = nodejs.start_words(part.strip())
        if problem or found.get("rewritten") or found.get("env"):
            return None
        parts.append(" ".join(shlex.quote(word) for word in found["words"]))
    return " && ".join(parts) if len(parts) > 1 else None


def pnpm_members(text, paths):
    """The folders a pnpm-workspace.yaml names (packages: globs), each holding a package.json."""
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return []
    globs = data.get("packages") if isinstance(data, dict) else None
    return nodejs.workspace_packages({"workspaces": globs}, list(paths)) if isinstance(globs, list) else []


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


def node_start(repo, ladder, base, compose, package, builds_itself, dockerfile, web, trust, plan=None):
    """(recipe, entrypoint, command, args, words): what builds and starts a Node.js service (D45, D46). The repository's own
    Dockerfile builds itself; a Procfile goes to Google's buildpacks for reviewed code only, since they run install scripts and
    fetch Node.js at build time, and otherwise its web command runs on the platform's image; then Compose's command, the
    contract's build.entrypoint, scripts.start when it runs node (or a restarter on a file, or a tool the build plan replaces:
    next start, nest start, tsx; 21.2 and 21.3), main, then the entry files by name; last, the build plan's own start."""
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
        chain = re.fullmatch(r"\s*(?:npm|pnpm|yarn|bun)\s+run\s+build\s*&&\s*node\s+(\S+)\s*", start)
        if chain and plan and plan.get("build"):
            # "build, then node" (fastify's demo): the image's build stage runs the build (21.2), and node starts the file.
            file = chain.group(1).removeprefix("./")
            ladder.choose("Start command", f"node {file}", f"{repo.at('package.json')} (scripts.start builds first; the build runs in the image's build stage)")
            return "platform", f"node {file}", None, None, ["node", file]
        found, problem = nodejs.start_words(start)
        first = start.split()[0].rsplit("/", 1)[-1] if start.split() else ""
        if problem and first in ("pm2", "pm2-runtime"):
            ecosystem = next((word for word in start.split() if word.endswith(".json") and repo.has(word.removeprefix("./"))), None)
            script = nodejs.ecosystem_script(repo.text(ecosystem.removeprefix("./"))) if ecosystem else None
            if script and repo.has(script):
                ladder.note(f"scripts.start runs pm2 on {ecosystem}, whose first app is {script}; Cloud Run restarts the service itself, so the "
                            "platform starts node on that file (D49)")
                ladder.choose("Start command", f"node {script}", f"{repo.at(ecosystem.removeprefix('./'))} (pm2's ecosystem file, rewritten to node)")
                return "platform", f"node {script}", None, None, ["node", script]
        if problem and first in nodejs.BUILT_STARTS and plan and plan.get("build") and plan.get("start"):
            # next start, nest start, remix-serve, tsx: the build stage makes what node then starts (21.2, 21.3).
            entrypoint = " ".join(plan["start"])
            ladder.choose("Start command", entrypoint[:120], f"{repo.at('package.json')} (scripts.start runs {first}; after the build, {plan.get('why')})")
            return "platform", entrypoint, None, None, list(plan["start"])
        chain = node_chain(start) if problem and "&&" in start else None
        if chain:
            # D49, 22.2: a chain of node commands runs as written under tini, the init of the platform's images, which passes
            # SIGTERM on to the last one; a chain through npm or another manager stays refused (no manager stays in the image).
            ladder.note("scripts.start chains node commands; the platform runs the chain as written under its image's init (tini), so the last "
                        "command still gets SIGTERM; the commands before it run at every start of every instance")
            ladder.choose("Start command", chain[:120], f"{repo.at('package.json')} (scripts.start, a chain of node commands)")
            return "platform", chain, None, None, chain.split("&&")[-1].split()
        if problem and first in nodejs.BUILT_STARTS and plan and plan.get("problem"):
            return "platform", None, None, None, None  # the build plan's refusal already says what is missing
        if problem:
            start_refusal(repo, ladder, package, start, problem)
            return "platform", None, None, None, None
        if found["dropped"]:
            ladder.note(f"scripts.start reads an environment file ({', '.join(found['dropped'])}); the image never carries .env, so the platform "
                        "starts node without it: give the values under env and secrets in the contract")
        if found["env"]:
            ladder.note("scripts.start sets " + ", ".join(f"{key}={value}" for key, value in found["env"].items())
                        + " before node; give them under env in the contract if the service needs them")
        if found.get("rewritten"):
            ladder.note(f"scripts.start runs {found['rewritten']}, which restarts the service on changes or crashes; Cloud Run restarts it itself, "
                        f"so the platform starts node on the same file (node {found['file']}), as 20.4 of the roadmap says (D49)")
        if isinstance(scripts.get("prestart"), str):
            ladder.note(f"scripts.prestart (`{scripts['prestart'][:80]}`) runs before npm start only; the platform starts node itself, so it never runs")
        entrypoint = " ".join(shlex.quote(word) for word in found["words"])
        ladder.choose("Start command", entrypoint[:120], f"{repo.at('package.json')} (scripts.start" + (f", {found['rewritten']} rewritten to node" if found.get("rewritten") else "") + ")")
        return "platform", entrypoint, None, None, found["words"]
    main = package.get("main")
    # main, then the entry files the reader already lists (20.4): server.js, index.js, app.js, main.js, src/index.js and the rest.
    for candidate, where in ((main.removeprefix("./") if isinstance(main, str) else None, f"{repo.at('package.json')} (main)"),
                             *((name, None) for name in NODE_ENTRIES)):
        if candidate and repo.has(candidate):
            ladder.choose("Start command", f"node {candidate}", where or f"{repo.at(candidate)} (an entry file by name)")
            return "platform", f"node {candidate}", None, None, ["node", candidate]
    if plan and plan.get("framework") and plan.get("build") and plan.get("start"):
        # No start script, but a framework that builds (a build script alone): its documented start after the build (21.2).
        entrypoint = " ".join(plan["start"])
        ladder.choose("Start command", entrypoint[:120], f"{repo.at('package.json')} ({plan.get('why')})")
        return "platform", entrypoint, None, None, list(plan["start"])
    return "platform", None, None, None, None


def node_listen(repo, ladder, sources, order, dependencies, base):
    """Where the code listens (D46): the first listen call of the entry file and what it imports; a helper script never
    decides (20.1). A literal port that never reads PORT goes into the contract as `port` (20.4: gcloud's --port sets the
    container port and PORT alike); a loopback host and Fastify without host: '0.0.0.0' are refused; a host read from a
    variable whose default is loopback gets 0.0.0.0 in the draft's env; a literal loopback host and Fastify without host get
    the proxy sidecar (D51, 22.1) unless the contract says proxy: false. Returns (port literal or None, {variable: value},
    whether the literal port is fixed in the code, why a proxy is drafted or None)."""
    facts = nodejs.listen_facts(sources, order, dependencies)
    if facts is None:
        ladder.note("No listen call was found in the code read: check that the service listens on PORT (Cloud Run sets it) on 0.0.0.0")
        return None, {}, False, None
    where = repo.at(facts["file"])
    proxy = None
    allowed = base.get("proxy") is not False
    fixed = facts["port"] is not None and not facts["port_env"] and not nodejs.any_reads_port(sources)
    if fixed:
        ladder.note(f"{where} listens on port {facts['port']} and never reads PORT: the contract says port: {facts['port']}, and Cloud Run sends "
                    "requests to that port (its --port flag sets the container port and PORT alike), so nothing changes in the code")
    env = {}
    if facts["host"] in nodejs.LOOPBACK:
        if facts["host_env"] and facts["host_env"] in (base.get("env") or {}):
            ladder.choose("Host", f"{base['env'][facts['host_env']]} through {facts['host_env']}", repo.at("platform.yaml"))
        elif facts["host_env"]:
            env[facts["host_env"]] = EVERY_INTERFACE  # noqa: S104 - Cloud Run's contract: listen on every interface
            ladder.choose("Host", f"0.0.0.0 through {facts['host_env']} (the code's default, {facts['host']}, cannot be reached on Cloud Run)", where)
        elif allowed:
            proxy = f"{where} listens on {facts['host']} and reads no variable for it"
        else:
            ladder.refuse(where, f"listens on {facts['host']}, which Cloud Run cannot reach", "listen on every interface: leave the host out, or give '0.0.0.0'")
    if facts["fastify_without_host"]:
        if allowed:
            proxy = f"{where} starts Fastify without host: '0.0.0.0', and Fastify listens on localhost unless told otherwise"
        else:
            ladder.refuse(where, "starts Fastify without host: '0.0.0.0', and Fastify listens on localhost unless told otherwise, which Cloud Run cannot reach",
                          "pass host: '0.0.0.0' to fastify.listen()")
    if proxy:
        ladder.choose("Proxy", "an Nginx sidecar on the ingress port, forwarding to the service on the loopback the two containers share (proxy: true)", where)
        ladder.note(f"{proxy}, which Cloud Run cannot reach from outside the instance: the contract adds the platform's proxy sidecar (proxy: true, "
                    "D51). It bills as the app does, only while serving (about one more vCPU and 256 MiB per instance); the developer's one line, "
                    "listening on 0.0.0.0, stays the better fix, and then the reviewer removes proxy")
    return facts["port"], env, fixed, proxy


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
        # Nothing at the root, but folders hold services (live 2026-10-19: eight in one repository): say which to take, as
        # for Python, rather than ask for a package.json at the root.
        nested = [entry["path"] for entry in services(paths, repo.texts) if entry["path"]] if not repo.path else []
        more = f" and {len(nested) - 3} more" if len(nested) > 3 else ""
        ladder.refuse(where, "has Node.js code but no package.json",
                      f"the service lives in {', '.join(nested[:3])}{more}: take that folder (path {nested[0]}), or add a package.json at the root"
                      if nested else "add a package.json that lists the dependencies (npm init -y, then npm install each one)")
        return result
    if package is None:
        ladder.refuse(repo.at("package.json"), "is not valid JSON", "fix package.json so npm can read it")
        return result
    root = nodejs.package_data(repo.texts.get("package.json", "")) if repo.path else None
    for folder in ("node_modules", "dist", ".next"):
        if any(item.startswith(folder + "/") for item in repo.local_paths):
            ladder.ignore(repo.at(folder), "committed build output or installed packages: the platform installs the dependencies itself" if folder == "node_modules"
                          else "committed build output, which the platform neither builds nor needs unless the start command runs it")
    scripts_table = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    members = nodejs.workspace_packages(package, [item for item in repo.local_paths]) + pnpm_members(repo.text("pnpm-workspace.yaml"), repo.local_paths)
    workspace = [repo.at(item) for item in NODE_WORKSPACE_FILES if repo.has(item)]
    # 21.4: a root that is a workspace is the service only when it starts or builds something itself; else one of its packages
    # is, installed from the root's lockfile (build.context: repository).
    root_members = (nodejs.workspace_packages(root, sorted(repo.paths)) if root else []) + pnpm_members(repo.texts.get("pnpm-workspace.yaml", ""), sorted(repo.paths))
    root_lock = any(name in repo.paths for name in (*nodejs.LOCKFILES, *nodejs.OTHER_LOCKFILES))
    own_lock = any(repo.has(name) for name in (*nodejs.LOCKFILES, *nodejs.OTHER_LOCKFILES))
    member = bool(repo.path) and repo.path in root_members and root_lock and not own_lock
    own_start = isinstance(scripts_table.get("start"), str) or any(name in nodejs.runtime_names(package) | set(package.get("devDependencies") or {})
                                                                      for name in nodejs.NODE_BUILDS)
    if (package.get("workspaces") is not None or repo.has("pnpm-workspace.yaml")) and not own_start:
        ladder.refuse(repo.at("package.json") if package.get("workspaces") is not None else repo.at("pnpm-workspace.yaml"),
                      "is a workspace of several packages" + (f" ({', '.join(members[:6])})" if members else "") + " and starts nothing itself",
                      "take one package as the service" + (f" (path {repo.at(members[0])})" if members else "") + ": it is installed from this root's lockfile")
    elif package.get("workspaces") is not None or repo.has("pnpm-workspace.yaml"):
        ladder.note("The root is a workspace of several packages" + (f" ({', '.join(members[:6])})" if members else "") + " and the service itself: the "
                    "members install with it")
    elif workspace and not member:
        tool = workspace[0].rsplit("/", 1)[-1]
        if tool in ("nx.json", "lerna.json", "rush.json") and package.get("workspaces") is None and not own_start:
            ladder.refuse(workspace[0], f"is a repository managed by {tool.split('.')[0]}, whose packages the platform does not build or install one by one",
                          "take one package as the service once it has a package.json and a lockfile of its own, or give the start command (build.entrypoint)")
    if member:
        ladder.note(f"{repo.path} is a package of the repository's workspace: it is installed from the root's lockfile with the whole workspace, "
                    "built in its own folder and copied alone into the image (build.context: repository, 21.4)")
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
    lock = node_lock(repo, ladder, package, member=member)
    wrapper = None
    view = (lock or {}).get("view") or {}
    manager = (lock or {}).get("manager") or "npm"
    dependencies = nodejs.dependency_names(package, (lock or {}).get("data")) | set(nodejs.view_names(view))
    if builds_itself:
        native = nodejs.view_native(view, dependencies) if view else nodejs.has_native_code(None, dependencies)
        user = wrapped_user(base, trust)
        for finding in dockerpolicy.check(repo.text(dockerfile), trust, native=native, wrapped=user):
            if finding["refuses"]:
                ladder.refuse(f"{repo.at(dockerfile)}:{finding['line']}", finding["problem"], finding["fix"])
            else:
                ladder.note(f"{repo.at(dockerfile)}:{finding['line']}: {finding['problem']} ({finding['fix']})")
            if finding["rule"] == "runs-as-root" and user:
                wrapper = user
    line = node_line(repo, ladder, base, dockerfile if builds_itself else None, package) or default_node()
    if line == "22":
        ladder.note(f"Node.js 22 is in maintenance and reaches its end of life on {NODE_END_OF_LIFE['22'].isoformat()}; releases are refused "
                    f"from that day, so move to {default_node()} before then")
    scripts = script_ladder(repo, ladder, package, lock)
    # 21.2 and 21.3: what the service needs before it starts, decided once here and again by the image at build time (node_facts).
    plan = None
    site = None
    if not builds_itself:
        start_text = scripts_table.get("start") if isinstance(scripts_table.get("start"), str) else ""
        parsed, _ = nodejs.start_words(start_text) if start_text else (None, None)
        candidate = parsed["words"] if parsed else None
        tool = start_text.split()[0].rsplit("/", 1)[-1] if start_text.split() else ""
        if candidate is None and tool in ("tsx", "ts-node", "ts-node-dev"):
            rest = [word for word in start_text.split()[1:] if not word.startswith("-") and word not in ("watch",)]
            candidate = ["node", rest[0].removeprefix("./")] if rest else None
        chain = re.fullmatch(r"\s*(?:npm|pnpm|yarn|bun)\s+run\s+build\s*&&\s*node\s+(\S+)\s*", start_text)
        if chain:
            candidate = ["node", chain.group(1).removeprefix("./")]
        if candidate is None:
            main_file = str(package.get("main") or "").removeprefix("./")
            fallback = next((name for name in ([main_file] if main_file else []) + list(NODE_ENTRIES) if name and repo.has(name)), None)
            candidate = ["node", fallback] if fallback else None
        configs = {name: repo.text(name) for name in NODE_CONFIG_FILES if repo.has(name)}
        plan = nodejs.build_plan(package, repo.has, configs, manager, line, candidate)
        if plan.get("problem") and "front-end site" in plan["problem"] and not base.get("proxy"):
            output, why = static_site(package, dependencies)
            if output:
                site = (base.get("build") or {}).get("output") or output
                plan = None
            else:
                plan["problem"] += f" ({why})"
        if plan and plan.get("problem"):
            fix = STATIC_FIX if "front-end site" in plan["problem"] else ("give the build its values another way, or build with a Dockerfile of your own" if "secret" in plan["problem"]
                                                                            else "add typescript to the development dependencies, or commit the built output")
            ladder.refuse(repo.at("package.json"), plan["problem"], fix)
    before = len(ladder.refusals)
    if site is None and not builds_itself and not isinstance(scripts_table.get("start"), str) and base.get("runtime") != "node-http":
        static = [item for item in nodejs.STATIC_TOOLS if item in dependencies]
        if static and not any(item in dependencies for item in nodejs.SERVERS):
            output, _ = static_site(package, dependencies)
            site = output and ((base.get("build") or {}).get("output") or output)
    if site:
        ladder.choose("Build", f"the platform's static image: the build script writes the site into {site}/, and Caddy serves it behind IAP "
                      "(runtime: static, exposure: lab-members)", f"{repo.at('package.json')} (scripts.build)")
        ladder.note("A front-end site with no server of its own (D55): it is served to the lab's members through IAP, never publicly; a site "
                    "whose build needs a secret is refused, and the review can correct build.output if the builder writes elsewhere")
        recipe, entrypoint, command, args, words = "platform", scaffold_static_entrypoint(), None, None, None
    else:
        recipe, entrypoint, command, args, words = node_start(repo, ladder, base, compose, package, builds_itself, dockerfile, web, trust, plan)
    start_refused = len(ladder.refusals) > before or bool(plan and plan.get("problem"))
    file = start_file(words)
    build_script = scripts_table.get("build")
    builds = bool(plan and plan.get("build")) and recipe == "platform"
    if builds:
        ladder.choices[:] = [item for item in ladder.choices if item["what"] != "Build"]
        ladder.choose("Build", f"the platform's own Node.js image with a build stage ({plan['why']}; the development dependencies leave before the "
                      "image is made); nothing is added to the repository", f"{repo.at('package.json')} ({nodejs.MANAGER_COMMANDS[manager]['title']})")
    runtime_deps = nodejs.runtime_names(package)
    sources = {item[len(repo.prefix):]: text for item, text in repo.texts.items() if item.startswith(repo.prefix)
               and nodejs.CODE.search(item) and not nodejs.NOT_CODE.search(item) and not nodejs.NODE_TEST.search(item[len(repo.prefix):])}
    if recipe != "repository" and not site:
        static = [item for item in nodejs.STATIC_TOOLS if item in dependencies]
        if words is None and not start_refused and static and not any(item in dependencies for item in nodejs.SERVERS) \
                and not any(nodejs.SERVER_CODE.search(text) for text in sources.values()):
            ladder.refuse(where, f"looks like a front-end site built with {static[0]}, not a web service", STATIC_FIX)
        elif words is None and not start_refused:
            ladder.refuse(repo.at("package.json"), "says nothing that starts the service: no start script, main, server.js or index.js",
                          "add \"start\": \"node server.js\" to package.json's scripts, or give the start command in the contract (build.entrypoint)")
        if file and not repo.has(file) and not builds:
            if build_script or file.split("/")[0] in ("dist", "build", "out", "lib", ".next"):
                ladder.refuse(repo.at("package.json"), f"starts {file}, which is not in the repository: the service needs a build first, and nothing says how",
                              "add a build script that makes it, or commit it")
            else:
                ladder.refuse(repo.at("package.json"), f"starts {file}, which is not in the repository", "commit it, or correct the start command")
        elif file and re.search(r"\.[cm]?ts$", file) and not builds:
            ladder.note(f"{repo.at(file)} runs under Node's type stripping (tsconfig.json says erasableSyntaxOnly): enums, namespaces and parameter "
                        "properties would need a build")
        elif file and build_script and not builds and repo.has(file):
            ladder.note(f"package.json's build script (`{str(build_script)[:80]}`) is not run: the service starts from {file} as committed; if the "
                        "image needs what the build makes, commit it or build with a Dockerfile of your own")
    if file and repo.has(file) and repo.at(file) not in repo.texts:
        ladder.note(f"{repo.at(file)}, which starts the service, was not read, so where it listens was not checked")
    # The entry file and the modules it imports come first; where it listens is read from them alone when they are known, and
    # otherwise from the entry files by name: a helper script (scripts/, tools/, bin/) never decides the host or the port (20.1).
    modules = [file] if file in sources else []
    for item in local_imports(file or "", sources.get(file or "", "")):
        found = next((candidate for candidate in ([item] if nodejs.CODE.search(item) else [item + ext for ext in (".js", ".mjs", ".cjs", ".ts", "/index.js")])
                      if candidate in sources), None)
        if found:
            modules.append(found)
    order = list(dict.fromkeys(modules + sorted(sources, key=_node_rank)))
    entries = modules or [item for item in order if item in NODE_ENTRIES or (file and item == file)]
    scanned = entries or [item for item in order if not re.match(r"(scripts?|tools?|bin|test|tests|examples?|docs?)/", item)]
    listen_port, host_env, fixed_port, proxy = node_listen(repo, ladder, sources, scanned, dependencies, base) if sources and scanned and not site else (None, {}, False, None)
    for dependency, names in nodejs.HOST_READERS.items():
        if dependency in runtime_deps:
            for name_ in names:
                if name_ not in (base.get("env") or {}) and name_ not in host_env:
                    host_env[name_] = EVERY_INTERFACE  # noqa: S104 - Cloud Run's contract: listen on every interface
            ladder.choose("Host", f"0.0.0.0 through {', '.join(names)} ({dependency} reads it)", f"{repo.at('package.json')} ({dependency})")
    for name_, value in ((plan or {}).get("env") or {}).items():
        if name_ not in (base.get("env") or {}) and name_ not in host_env:
            host_env[name_] = value  # the built start's host variable (21.2): Next.js standalone reads HOSTNAME, the serve commands HOST
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
        ladder.choose("Port", port, f"{repo.at(scanned[0]) if scanned else where} (the listen call's "
                      + ("literal: Cloud Run's --port sets the container port and PORT alike" if fixed_port else "default; Cloud Run passes it as PORT") + ")")
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
    settings_note(ladder, sorted(names - {"PORT", "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "NODE_ENV"}))
    if not nodejs.handles_sigterm(sources) and sources:
        ladder.note("No SIGTERM handler was found: Cloud Run sends SIGTERM and stops the container ten seconds later, so requests in flight fail; "
                    "close the server on SIGTERM (process.on('SIGTERM', () => server.close(() => process.exit(0))))")
    if any(nodejs.SQLITE.search(text) for text in sources.values()):
        # D47: a replicated SQLite file (state: sqlite) was dropped before it was built, so the answer is a database need.
        ladder.note("It keeps a SQLite database on local disk, which Cloud Run loses at every restart and does not share between instances, "
                    "and every release runs the old and the new version side by side, each with its own copy: keep that state in Firestore "
                    "or Postgres (needs.database)")

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
    outside = sorted(runtime_deps & nodejs.INTERNET_CLIENTS)
    if outside and not base.get("egress"):
        ladder.note(f"It uses {', '.join(outside[:5])}: if it calls services outside Google Cloud, add egress: internet in the review "
                    "(code nobody has reviewed reaches no internet otherwise)")
    vertex = nodejs.CLAUDE_VERTEX_CLIENT in runtime_deps or any("AnthropicVertex" in text for text in sources.values())
    data_app_notes(ladder, base, sources, nodejs.CLAUDE_CLIENT if nodejs.CLAUDE_CLIENT in runtime_deps else None, vertex,
                   [name for name in nodejs.MODEL_DOWNLOADERS if name in runtime_deps], node=True)

    how, what, notes = nodejs.tests_of(package, repo.local_paths, line)
    for note in notes:
        ladder.note(note)
    if how:
        ladder.choose("Tests", f"{what}, run with {how} before every build", f"{repo.at('package.json')} (scripts.test)" if how == "npm test" else where)
    else:
        ladder.choose("Tests", "none found: nothing checks the service's behaviour before a release (tests: none)", where)
    tests_mode = "required" if how else "none"
    test_text = f"{what or ''} " + " ".join(name for name in (package.get("devDependencies") or {}) if isinstance(package.get("devDependencies"), dict))
    if how and NEEDS_MORE_TO_TEST.search(test_text) and "tests" not in base:
        tests_mode = "report"
        ladder.note("The tests need what the platform's runner does not have (a browser, a database, a seed or Docker): tests: report runs and "
                    "counts them and never stops a release on them (D54); a reviewer who wants them to gate sets tests: required")
    if ladder.refusals or lock is None:
        name_services(ladder, paths, repo.texts, repo.path)
        return result

    build = dict(base.get("build") or {})
    if wrapper and "user" not in build:
        build["user"] = wrapper
        ladder.choose("User", f"{wrapper}, set by the platform's wrapper stage on top of the repository's image, which runs as root (build.user, D52)",
                      repo.at(dockerfile))
    if recipe != "repository":
        build["recipe"] = recipe
    if entrypoint and recipe in ("platform", "buildpacks"):
        build["entrypoint"] = entrypoint
    if scripts and "scripts" not in build:
        build["scripts"] = scripts
    if member and recipe == "platform":
        build["context"] = "repository"  # 21.4: the workspace root is the build context; the image copies one package out of it
    draft = {**{key: value for key, value in base.items() if key != "python"}, "name": name, "runtime": "node-http", "node": line, "port": port,
             "health_path": health, "tests": base.get("tests") or tests_mode}
    if proxy and "proxy" not in base:
        draft["proxy"] = True
    if site:
        build.update(recipe="platform", entrypoint=scaffold_static_entrypoint(), output=site)
        build.pop("scripts", None)
        draft.update(runtime="static", port=base.get("port") or DEFAULT_PORT, health_path=None, exposure="lab-members")
        host_env = {}
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
    wrapper = None
    if builds_itself:
        # 14.1: the Dockerfile a repository taken as it is brings meets the platform's policy before its contract is proposed;
        # unless the draft says the lab reviewed it, it is code nobody has reviewed (D35).
        trust = base.get("trust") if base.get("trust") in ("reviewed", "unknown") else "unknown"
        user = wrapped_user(base, trust)
        for finding in dockerpolicy.check(repo.text(dockerfile), trust, wrapped=user):
            if finding["refuses"]:
                ladder.refuse(f"{repo.at(dockerfile)}:{finding['line']}", finding["problem"], finding["fix"])
            else:
                ladder.note(f"{repo.at(dockerfile)}:{finding['line']}: {finding['problem']} ({finding['fix']})")
            if finding["rule"] == "runs-as-root" and user:
                wrapper = user

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
        packages = [item for item in SYSTEM_PACKAGES if repo.has(item)]
        unread = [repo.at(item) for item in UNREAD_PACKAGE_FILES if repo.has(item)]
        if packages and recipe == "platform":
            # 20.3: the platform's own Dockerfile installs them (Heroku's Aptfile, Railpack's packages.txt), with a compiler in a build stage.
            try:
                names = [name for item in packages for name in system_packages(repo.text(item))]
            except ValueError as exc:
                ladder.refuse(repo.at(packages[0]), f"lists something that is not a Debian package name ({str(exc)[:120]})",
                              "list Debian package names only, one per line")
                names = []
            if names:
                ladder.choose("System packages", ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
                              + ", installed in the platform's image with a compiler at hand in its build stage", repo.at(packages[0]))
        elif packages:
            ladder.note(f"{repo.at(packages[0])} lists system packages, which Google's buildpacks do not install; a Dockerfile of the repository's own "
                        "or the platform's own image (no Procfile) would")
        if unread:
            ladder.refuse(unread[0], "lists system packages in bindep's format, which the platform does not read",
                          "list the Debian package names in apt.txt instead, one per line, or build with a Dockerfile of the repository's own")

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
    env = {}
    framework = None
    if recipe in ("platform", "buildpacks") and entrypoint is None and not web:
        found = application_ladder(repo, ladder, dependencies, port)
        if found and "command" in found:
            # D50: the framework among the dependencies, or `python <main file>`, decided the command, the environment and perhaps the port.
            if found.get("port") and found["port"] != port and "port" not in base and not (compose and compose_port(compose["body"].get("ports"))):
                port = found["port"]
                ladder.choices[:] = [item for item in ladder.choices if item["what"] != "Port"]  # the default gives way to the code's literal
                ladder.choose("Port", port, f"{repo.at(found['file'])} (a literal port in the entry file; Cloud Run's --port sets the container port and PORT alike)")
            entrypoint = " ".join(found["command"])
            env = dict(found["env"])
            framework = found.get("framework")
            found = {"interface": None, "server": found.get("server")} if found.get("server") else None
        elif found:
            framework = found.get("framework")
            words = server_command(found["interface"], found["application"], found["app_dir"], found["factory"], port)
            entrypoint = " ".join(words)
            found = {"interface": found["interface"], "server": words[0]}
        if found and found.get("server") and found["server"] not in dependencies:
            server = found["server"]
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
    if (framework == "django" or (repo.has("manage.py") and "django" in dependencies)) and recipe == "platform":
        # 20.3: Django's chores. collectstatic runs at build in the platform's image (as Heroku does); migrate is offered as needs.migrate.
        ladder.note("Django's static files are collected at build (python manage.py collectstatic --noinput in the platform's image): a settings "
                    "module that needs a value at import stops the build, and the contract's env is where to give it (WhiteNoise serves the files)")
        if not (base.get("needs") or {}).get("migrate"):
            ladder.note("If the schema is created by migrations, add needs.migrate: [python, manage.py, migrate] in the review: it runs as a job "
                        "from the release's image before the candidate gets traffic")

    health = health_ladder(repo, ladder, base, compose, sources)
    settings_note(ladder, configuration_names(repo, compose, sources))

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
    vertex = bool(CLAUDE_VERTEX.search(manifest_text)) or any("AnthropicVertex" in text for text in sources.values())
    data_app_notes(ladder, base, sources, "anthropic" if "anthropic" in dependencies else None, vertex,
                   [name for name in HUB_DOWNLOADERS if name in dependencies], node=False)

    tests = tests_ladder(repo, ladder)
    if ladder.refusals or lock is None:
        name_services(ladder, paths, texts, repo.path)
        return result

    build = dict(base.get("build") or {})
    if recipe != "repository":
        build["recipe"] = recipe
    if entrypoint and recipe in ("platform", "buildpacks"):
        build["entrypoint"] = entrypoint
    if wrapper and "user" not in build:
        build["user"] = wrapper
        ladder.choose("User", f"{wrapper}, set by the platform's wrapper stage on top of the repository's image, which runs as root (build.user, D52)",
                      repo.at(dockerfile))
    tests_mode = "required" if tests else "none"
    suite = " ".join(repo.text(name) for name in (*PYTEST_CONFIG, *TEST_REQUIREMENTS, "pyproject.toml") if repo.has(name)) + " ".join(repo.text(item) for item in tests[:20])
    if tests and "tests" not in base and NEEDS_MORE_TO_TEST.search(re.sub(r"(?m)^\s*#.*$", "", suite)):
        tests_mode = "report"
        ladder.note("The tests need what the platform's runner does not have (a browser, a database, a seed or Docker): tests: report runs and "
                    "counts them and never stops a release on them (D54); a reviewer who wants them to gate sets tests: required")
    draft = {**base, "name": name, "python": python, "port": port, "health_path": health, "tests": base.get("tests") or tests_mode}
    if build:
        draft["build"] = build
    if env:
        draft["env"] = {**env, **(base.get("env") or {})}
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
