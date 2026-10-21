"""What the platform knows about Node.js services (docs/ROADMAP.md Phase 18, decisions D41 to D46), read as text, never run.

The reader's Node.js ladders are built from these pieces: the line a version file or an npm version range asks for, what
an npm lockfile holds and whether every entry comes from the registry with its integrity hash, which packages carry an
install script, what package.json's start script runs, where the code listens and on which host, its health route, the
settings it reads by name, and how its tests run. Facts about packages and tools carry the date they were checked.
"""
import json
import re
import shlex

from .models import NODE_END_OF_LIFE, SCRIPT_PACKAGE, default_node

NODES = tuple(NODE_END_OF_LIFE)
REGISTRY = "https://registry.npmjs.org/"
# npm reads npm-shrinkwrap.json before package-lock.json; the platform installs either with npm ci.
LOCKFILES = ("npm-shrinkwrap.json", "package-lock.json")
# The other managers' lockfiles (docs/ROADMAP.md 21.1): each installed by its own manager, frozen, with every install
# script off. bun.lockb is Bun's old binary format, which only Bun reads: refused with the command that rewrites it.
OTHER_LOCKFILES = {"yarn.lock": "Yarn", "pnpm-lock.yaml": "pnpm", "bun.lock": "Bun", "bun.lockb": "Bun"}
MANAGER_LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lock": "bun"}
MANAGERS = ("npm", "yarn", "pnpm", "bun")
# npm's registry and Yarn's name for it (yarn.lock v1 files written before 2024 resolve from registry.yarnpkg.com, which
# serves the same packages): the only addresses a lockfile entry may come from.
REGISTRIES = ("https://registry.npmjs.org/", "https://registry.yarnpkg.com/")
# .nvmrc may name an LTS line by its codename (lts/krypton); the lines before 22 are named so the refusal can say which.
CODENAMES = {"hydrogen": "18", "iron": "20", "jod": "22", "krypton": "24"}
PLACEHOLDER_TEST = re.compile(r"""^\s*echo\s+["']?Error: no test specified["']?\s*&&\s*exit\s+1\s*$""")
CODE = re.compile(r"\.(?:c|m)?[jt]s$")
NOT_CODE = re.compile(r"(^|/)(node_modules|dist|build|coverage|\.next|\.nuxt|out|public|static|vendor|bower_components)/|\.d\.[cm]?ts$|\.min\.js$")
# node --test's own default patterns (Node 22 and later), node_modules excluded; TypeScript files count from Node 24, where
# type stripping is on by default.
NODE_TEST = re.compile(r"(^|/)(?:[^/]+[.\-_]test|test-[^/]+|test)\.(?:c|m)?js$|(^|/)test/(?:[^/]+/)*[^/]+\.(?:c|m)?js$")
NODE_TEST_TS = re.compile(r"(^|/)(?:[^/]+[.\-_]test|test-[^/]+|test)\.(?:c|m)?ts$|(^|/)test/(?:[^/]+/)*[^/]+\.(?:c|m)?ts$")
TEST_CONFIGS = (("Jest", re.compile(r"^jest\.config\.(?:c|m)?[jt]s$|^jest\.config\.json$"), "jest"),
                ("Vitest", re.compile(r"^vitest\.config\.(?:c|m)?[jt]s$"), "vitest"),
                ("Mocha", re.compile(r"^\.mocharc(?:\.(?:c?js|json|jsonc|ya?ml))?$"), "mocha"),
                ("AVA", re.compile(r"^ava\.config\.(?:c|m)?js$"), "ava"),
                ("tap", re.compile(r"^\.taprc$"), "tap"))
# What a start script may not be (D46): it watches, transpiles, serves a development build or chains processes.
START_TOOLS = {
    "nodemon": "restarts on file changes (nodemon), a development tool", "node-dev": "restarts on file changes (node-dev), a development tool",
    "ts-node-dev": "transpiles and restarts on file changes (ts-node-dev)", "ts-node": "transpiles TypeScript at start (ts-node)",
    "tsx": "transpiles TypeScript at start (tsx)", "babel-node": "transpiles at start (babel-node)", "esno": "transpiles at start (esno)",
    "vite": "runs Vite's development server", "next": "runs Next.js, which needs a build", "nuxt": "runs Nuxt, which needs a build",
    "nuxi": "runs Nuxt, which needs a build", "astro": "runs Astro, which needs a build", "remix": "runs Remix, which needs a build",
    "react-scripts": "runs Create React App's development server", "ng": "runs Angular's development server",
    "webpack-dev-server": "runs webpack's development server", "webpack": "runs webpack", "parcel": "runs Parcel's development server",
    "nest": "runs the NestJS command line, which builds first", "gatsby": "runs Gatsby, a static site",
    "concurrently": "starts several processes at once (concurrently)", "npm-run-all": "starts several scripts at once (npm-run-all)",
    "run-p": "starts several scripts at once (run-p)", "run-s": "runs several scripts in a row (run-s)",
    "pm2": "runs a process manager (pm2); Cloud Run already restarts the service", "pm2-runtime": "runs a process manager (pm2-runtime); Cloud Run already restarts the service",
    "forever": "runs a process manager (forever); Cloud Run already restarts the service",
}
SHELLS = ("sh", "bash", "zsh", "dash", "ash")
TRANSPILERS = re.compile(r"ts-node|tsx|@babel/register|babel-register|esbuild-register|@swc/register|@swc-node/register")
# Dependencies that mean the service needs a build before it can start (D46): the later roadmap item builds them.
NEEDS_BUILD = {"next": "Next.js", "@nestjs/core": "NestJS", "@remix-run/node": "Remix", "@remix-run/serve": "Remix", "@sveltejs/kit": "SvelteKit",
               "astro": "Astro", "nuxt": "Nuxt"}
# Front-end build tools: with no server of its own, the repository is a static site, which the platform does not serve.
STATIC_TOOLS = ("react-scripts", "@angular/core", "vite", "parcel", "@vue/cli-service", "gatsby", "@docusaurus/core", "vuepress", "@11ty/eleventy")
SERVERS = ("express", "fastify", "koa", "@hapi/hapi", "hono", "@hono/node-server", "restify", "polka", "h3", "micro", "@nestjs/core")
SERVER_CODE = re.compile(r"""\b(?:createServer|createSecureServer)\s*\(|from\s+["'](?:node:)?https?["']|require\(\s*["'](?:node:)?https?["']\s*\)""")
# Libraries whose use usually means calls to services on the internet (docs/ROADMAP.md 14.5): a note for the reviewer.
INTERNET_CLIENTS = {"@anthropic-ai/sdk", "openai", "axios", "node-fetch", "got", "undici", "superagent", "twilio", "stripe", "@slack/web-api",
                    "@sendgrid/mail", "@aws-sdk/client-s3", "aws-sdk", "cohere-ai", "@mistralai/mistralai", "groq-sdk", "@google/generative-ai"}
# Decision D16 for Node.js: the needs the dependencies suggest, named in the review, never added to the draft silently.
NEEDS_HINTS = (
    ("database: postgres", r"^(pg|postgres|pg-promise|@google-cloud/cloud-sql-connector|knex|sequelize|typeorm|@prisma/client)$"),
    ("database: firestore", r"^(@google-cloud/firestore|firebase-admin)$"),
    ("buckets: [NAME] (arrives as BUCKET_NAME)", r"^@google-cloud/storage$"),
    ("topics: [NAME] (arrives as TOPIC_NAME and SUBSCRIPTION_NAME)", r"^@google-cloud/pubsub$"),
    ("models: [claude] (Claude on Vertex AI: no API key, no internet)", r"^@anthropic-ai/vertex-sdk$"),
)
UNPROVIDED = (("redis", r"^(redis|ioredis)$"), ("mysql", r"^(mysql|mysql2)$"), ("mongodb", r"^(mongodb|mongoose)$"))
WORKERS = r"^(bull|bullmq|bee-queue|agenda|node-cron|cron|node-schedule)$"
SQLITE = re.compile(r"""["'](?:node:sqlite|better-sqlite3|sqlite3)["']""")
# Anthropic's own client and its Vertex AI client (D47): the first needs an API key and the internet, the second neither.
CLAUDE_CLIENT, CLAUDE_VERTEX_CLIENT = "@anthropic-ai/sdk", "@anthropic-ai/vertex-sdk"
# transformers.js fetches each model from Hugging Face on first use and caches it inside its own package folder,
# node_modules/@huggingface/transformers/.cache by default, which the platform's non-root user cannot write: the first
# model load stops with EACCES (seen with 4.x in the platform's image on 2026-10-20).
MODEL_DOWNLOADERS = ("@huggingface/transformers", "@xenova/transformers")
# Packages with prebuilt or compiled native code: on an Alpine (musl) image their glibc binaries do not load (D45).
NATIVE = {"onnxruntime-node", "sharp", "bcrypt", "better-sqlite3", "sqlite3", "canvas", "@tensorflow/tfjs-node", "argon2", "re2", "node-sass",
          "@napi-rs/canvas", "lightningcss", "@swc/core", "esbuild"}
# Install scripts a Cloud Run service never needs, with what each does, read from the package itself on 2026-10-19 at the
# version named. The reader leaves them out of the drafted build.scripts and says why (D43); a reviewer can still add one.
NOT_NEEDED = {
    "onnxruntime-node": "its postinstall only downloads the CUDA and TensorRT libraries from NuGet on Linux x64; the CPU binaries ship "
                        "inside the package (read at 1.30.0)",
    "protobufjs": "its postinstall only reads the parent package.json and prints a warning about the version range; it writes nothing "
                  "(read at 7.6.6)",
}
HEALTH_ROUTE = re.compile(r"(^|/)(?:health|healthcheck)/?$")
RESERVED_ROUTE = re.compile(r"z/?$")  # Cloud Run keeps paths ending in z (/healthz) for itself: requests never reach the container
READINESS_ROUTE = re.compile(r"(^|/)(?:ready|readiness|live|liveness|ping|status)/?$")
ROUTE = re.compile(r"""(?:\.(?:get|all|head|route)\(\s*|\b(?:url|pathname|path)\s*===?\s*|\bcase\s+|\b(?:url|path)\s*:\s*)(["'`])(/[A-Za-z0-9/_.-]*)\1""")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
EVERYWHERE = ("0.0.0.0", "::")  # noqa: S104 - Cloud Run's contract: listen on every interface
# Frameworks and serve commands whose host comes from a variable (docs/RESEARCH-2026-10-20.md 6.3): Next.js standalone reads
# HOSTNAME (Docker may set it to the container's id), Astro's Node adapter HOST, Nuxt's Nitro NITRO_HOST then HOST, the
# serve commands of React Router and Remix HOST; the contract's env sets each to 0.0.0.0 (20.4).
HOST_READERS = {"next": ("HOSTNAME",), "astro": ("HOST",), "nuxt": ("NITRO_HOST", "HOST"), "@react-router/serve": ("HOST",),
                "@remix-run/serve": ("HOST",), "@sveltejs/kit": ("HOST",), "h3": ("HOST",), "nitropack": ("NITRO_HOST", "HOST")}


# ---- versions
def line_of(text):
    """(line, problem) for a version file (.nvmrc, .node-version, a .tool-versions entry): "24", "v24.1.0", "24.x",
    "lts/krypton" and "lts/*" name a line; anything else (node, stable, an alias) is refused rather than guessed."""
    value = next((part.split("#", 1)[0].strip() for part in (text or "").splitlines() if part.split("#", 1)[0].strip()), "")
    lowered = value.lower()
    if lowered in ("lts/*", "lts", "lts/latest"):
        return default_node(), None
    match = re.fullmatch(r"v?(\d+)(?:\.(?:\d+|x|\*))?(?:\.(?:\d+|x|\*))?", lowered)
    if match:
        return str(int(match.group(1))), None
    if lowered.startswith("lts/") and lowered[4:] in CODENAMES:
        return CODENAMES[lowered[4:]], None
    return None, f"names {value or 'nothing'}, which is not a Node.js line the platform can read"


VERSION = r"v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
COMPARATOR = re.compile(r"(<=|>=|<|>|=|\^|~>|~)?\s*" + VERSION)


def _number(part):
    return None if part is None or part in ("x", "X", "*") else int(part)


def _bounds(op, major, minor, patch):
    """(low, low inclusive, high, high inclusive) of one npm comparator; high None is unbounded."""
    if major is None:
        return (0, 0, 0), True, None, False
    low = (major, minor or 0, patch or 0)
    if op in (None, "", "="):
        if minor is None:
            return low, True, (major + 1, 0, 0), False
        if patch is None:
            return low, True, (major, minor + 1, 0), False
        return low, True, low, True
    if op == "^":
        high = (major + 1, 0, 0) if major else ((0, minor + 1, 0) if minor else (1, 0, 0))
        return low, True, high, False
    if op in ("~", "~>"):
        return low, True, (major + 1, 0, 0) if minor is None else (major, minor + 1, 0), False
    if op == ">":
        if minor is None:
            return (major + 1, 0, 0), True, None, False
        if patch is None:
            return (major, minor + 1, 0), True, None, False
        return low, False, None, False
    if op == ">=":
        return low, True, None, False
    if op == "<":
        return (0, 0, 0), True, low, False
    if minor is None:  # <=
        return (0, 0, 0), True, (major + 1, 0, 0), False
    if patch is None:
        return (0, 0, 0), True, (major, minor + 1, 0), False
    return (0, 0, 0), True, low, True


def _meets(bounds, line):
    """Whether some release of a line (line.0.0 up to the next line) lies inside every comparator of one set."""
    low, low_in, high, high_in = (int(line), 0, 0), True, (int(line) + 1, 0, 0), False
    for b_low, b_low_in, b_high, b_high_in in bounds:
        if b_low > low or (b_low == low and not b_low_in):
            low, low_in = b_low, b_low_in
        if b_high is not None and (b_high < high or (b_high == high and not b_high_in)):
            high, high_in = b_high, b_high_in
    return low < high or (low == high and low_in and high_in)


def lines_meeting(spec):
    """The supported lines with at least one release inside an npm version range (engines.node, devEngines), or None when the
    range cannot be read. A range resolves to the highest of them (D41), whose newest release is what the platform runs."""
    if not isinstance(spec, str) or len(spec) > 200:
        return None
    fits = set()
    for alternative in spec.split("||"):
        text = re.sub(r"(\S+)\s+-\s+(\S+)", r">=\1 <=\2", alternative.strip())
        if not text or text in ("*", "x", "X"):
            fits |= set(NODES)
            continue
        comparators = list(COMPARATOR.finditer(text))
        if not comparators or COMPARATOR.sub("", text).strip():
            return None
        bounds = [_bounds(m.group(1), _number(m.group(2)), _number(m.group(3)), _number(m.group(4))) for m in comparators]
        fits |= {line for line in NODES if _meets(bounds, line)}
    return sorted(fits, key=int)


def no_upper_bound(spec):
    """Whether a range lets any future line in (">=22", "*"): Heroku warns on it, and the platform notes it."""
    return isinstance(spec, str) and any(not re.search(r"<|\^|~|\s-\s|\b\d+\.x\b|^\s*v?\d+(\.\d+){0,2}\s*$", part) for part in spec.split("||"))


# ---- package.json and the lockfile
def package_data(text):
    """package.json as a mapping, {} when there is none, None when it cannot be read."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def dependency_names(package, lock=None):
    """Every dependency name of package.json (runtime, optional and development) and of the lockfile, lowercase."""
    names = set()
    for key in ("dependencies", "optionalDependencies", "devDependencies", "peerDependencies"):
        table = (package or {}).get(key)
        if isinstance(table, dict):
            names |= {str(name).lower() for name in table}
    for path, entry in ((lock or {}).get("packages") or {}).items():
        if path and isinstance(entry, dict):
            names.add(str(entry.get("name") or path.rsplit("node_modules/", 1)[-1]).lower())
    return names


def runtime_names(package):
    names = set()
    for key in ("dependencies", "optionalDependencies"):
        table = (package or {}).get(key)
        if isinstance(table, dict):
            names |= {str(name).lower() for name in table}
    return names


def read_lockfile(text):
    """(data, problem): an npm lockfile of version 2 or 3, or None and why it cannot be used."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, "is not valid JSON"
    if not isinstance(data, dict):
        return None, "is not an npm lockfile"
    version = data.get("lockfileVersion")
    if version == 1 or not isinstance(data.get("packages"), dict):
        return None, "is lockfile version 1 (npm 6 or older), which lists no packages the platform can check"
    if version not in (2, 3):
        return None, f"has lockfileVersion {version}, which the platform does not read"
    return data, None


def lock_problems(data):
    """[(package, problem)]: entries npm ci would install without checking them against the registry (D42): no integrity
    hash, a git repository, a tarball outside the registry, a file: or link: path, a workspace folder, a bundled package."""
    problems = []
    for path, entry in sorted((data.get("packages") or {}).items()):
        if not path or not isinstance(entry, dict):
            continue
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        resolved = str(entry.get("resolved") or "")
        if entry.get("link") or "node_modules/" not in f"/{path}":
            problems.append((name, "is a local folder or a link (a workspace, or a file: or link: dependency)"))
        elif entry.get("inBundle"):
            problems.append((name, "is bundled inside another package (bundleDependencies), so it is never checked on its own"))
        elif resolved.startswith(("git", "github:")) or "git+" in resolved or "codeload.github.com" in resolved:
            problems.append((name, "comes from a git repository"))
        elif resolved.startswith("file:"):
            problems.append((name, "comes from a file: path"))
        elif resolved and not resolved.startswith(REGISTRY):
            problems.append((name, f"comes from {resolved.split('/')[2] if '://' in resolved else resolved[:60]}, outside the npm registry"))
        elif not resolved:
            problems.append((name, "has no resolved address (the lock was written with omit-lockfile-registry-resolved)"))
        elif not entry.get("integrity"):
            problems.append((name, "has no integrity hash"))
    return problems


def install_scripts(data):
    """name@version of every lockfile entry that runs an install script (hasInstallScript), each once, sorted."""
    found = set()
    for path, entry in (data.get("packages") or {}).items():
        if path and isinstance(entry, dict) and entry.get("hasInstallScript") and entry.get("version"):
            found.add(f"{entry.get('name') or path.rsplit('node_modules/', 1)[-1]}@{entry['version']}")
    return sorted(found)


def has_native_code(data, names=()):
    """Whether the dependencies hold native code: an install script, a platform-specific binary (os, cpu or libc in the
    lockfile) or a package known to ship one."""
    for path, entry in (data or {}).get("packages", {}).items():
        if path and isinstance(entry, dict) and (entry.get("hasInstallScript") or entry.get("libc") or entry.get("cpu") or entry.get("os")):
            return True
    return bool(set(names) & NATIVE)


def allowed_scripts(package):
    """(name@version entries package.json's allowScripts approves, notes on the approvals the platform does not honour)."""
    table = (package or {}).get("allowScripts")
    if table is None:
        return [], []
    if not isinstance(table, dict):
        return [], ["package.json's allowScripts is not a list of packages by version, so the platform honours none of it: it runs install "
                    "scripts only for name@version entries"]
    pinned, notes = [], []
    for key, value in table.items():
        if value is not True:
            continue
        if SCRIPT_PACKAGE.fullmatch(str(key)):
            pinned.append(str(key))
        else:
            notes.append(f"package.json's allowScripts approves {key} without an exact version, so the platform does not run its script: "
                         "only name@version entries count (npm install-scripts approve writes them)")
    return sorted(pinned), notes


def workspace_packages(package, paths):
    """The folders a workspace's globs name (package.json's workspaces), each holding a package.json."""
    globs = (package or {}).get("workspaces")
    globs = globs.get("packages") if isinstance(globs, dict) else globs
    if not isinstance(globs, list):
        return []
    import fnmatch
    folders = sorted({path.rsplit("/", 1)[0] for path in paths if path.endswith("/package.json") and "node_modules/" not in path})
    return [folder for folder in folders if any(fnmatch.fnmatch(folder, str(glob).rstrip("/")) for glob in globs)]


def manager_spec(package):
    """package.json's packageManager as {"manager", "version", "integrity"} when it names an exact release
    ("pnpm@9.15.0+sha512.<hex>", "yarn@4.5.3"), as Corepack and Railpack read it; None when absent or not exact. The
    manager is the platform's name: yarn1 for Yarn 1, yarn for 2 and later. A sha512 hash is Corepack's hex digest of the
    npm tarball, given back as npm's integrity form (sha512-<base64>) so the install can be held to it."""
    import base64
    value = (package or {}).get("packageManager")
    match = re.fullmatch(r"(npm|yarn|pnpm|bun)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+(sha\d+)\.([0-9a-f]+))?", value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    name, version, algorithm, digest = match.groups()
    manager = "yarn1" if name == "yarn" and version.split(".")[0] == "1" else name
    integrity = None
    if algorithm == "sha512" and len(digest) == 128:
        integrity = "sha512-" + base64.b64encode(bytes.fromhex(digest)).decode()
    return {"manager": manager, "version": version, "integrity": integrity, "algorithm": algorithm}


# What a manager at the platform's pinned major can install frozen, by lockfile format (checked in Docker on 2026-10-20):
# pnpm 12 reads lockfile 9.0 only (pnpm 8's 6.0 is refused); Yarn 4.18 rewrites any other Yarn 2+ lockfile (metadata 4,
# 6 and even Yarn 4.0 to 4.12's 8), which --immutable forbids. A repository that names its manager's exact release in
# packageManager installs with that release instead, so these only decide when packageManager is absent.
PINNED_FORMATS = {"pnpm": ("9",), "yarn": ("10",)}


def lock_format(view):
    """The format a lockfile names: pnpm's lockfileVersion major, Yarn 2+'s __metadata version."""
    version = str(view.get("lockfileVersion") or "")
    return version.split(".")[0] if view.get("manager") == "pnpm" else version


def manager_of(package):
    """The package manager package.json's packageManager names (yarn for "yarn@4.5.0"), or None."""
    value = (package or {}).get("packageManager")
    match = re.match(r"^(npm|yarn|pnpm|bun)@", value) if isinstance(value, str) else None
    return match.group(1) if match else None


def literal_token(text, yarn=False):
    """Whether an .npmrc (or .yarnrc.yml) holds a literal registry token rather than a ${VARIABLE} it reads at install."""
    pattern = r"(?m)^\s*npmAuthToken\s*:\s*[\"']?(?!\$\{)[^\s\"'#]+" if yarn else r"(?m)_auth(?:Token)?\s*=\s*(?!\$\{)\S+"
    return re.search(pattern, text or "") is not None


# ---- what starts it
def start_words(script):
    """(start, problem): package.json's start script as {"words", "file", "dropped", "env"} when it is `node [flags] <file>
    [args]`, else None and what it does instead (D46). Variables set in front of node, cross-env included, are named, and
    --env-file flags are dropped: the image never carries the .env file they read."""
    if re.search(r"&&|\|\||[;|&`<>]|\$\(", script or ""):
        return None, "chains commands through a shell (&&, ;, |)"
    try:
        words = shlex.split(script or "")
    except ValueError:
        return None, "cannot be read as words"
    env = {}
    if words and words[0].rsplit("/", 1)[-1] == "cross-env":
        words = words[1:]
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]):
        key, value = words.pop(0).split("=", 1)
        env[key] = value
    if not words:
        return None, "runs nothing"
    first = words[0].rsplit("/", 1)[-1]
    if first in SHELLS:
        return None, "runs a shell"
    rewritten = None
    if first in RESTARTERS:
        # 20.4: a restarter or process manager watches or restarts a file that node can run as it is; Cloud Run restarts the
        # service itself, so the platform starts node on the same file. Its own flags are dropped; a flag that names another
        # command to run (nodemon --exec, pm2's ecosystem file) is not a file for node, and stays refused below.
        file = restarted_file(words[1:])
        if file is None:
            return None, START_TOOLS[first]
        rewritten, words = first, ["node", file]
    elif first in START_TOOLS:
        return None, START_TOOLS[first]
    elif first != "node":
        return None, f"runs {words[0]}, not node"
    flags, rest, dropped = [], words[1:], []
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        name, _, value = flag.partition("=")
        if name in ("--env-file", "--env-file-if-exists"):
            if not value and rest and not rest[0].startswith("-"):
                rest.pop(0)
            dropped.append(flag)
            continue
        if name in ("--watch", "--watch-path", "--watch-preserve-output") or name.startswith(("--inspect", "--debug")):
            return None, f"watches or debugs ({name}), a development command"
        if name in ("-e", "--eval", "-p", "--print", "-i", "--interactive", "-"):
            return None, f"runs code given on the command line ({name})"
        if name in ("-r", "--require", "--import", "--loader", "--experimental-loader") and not value and rest:
            value = rest.pop(0)
            flag = f"{name}={value}"
        if value and TRANSPILERS.search(value):
            return None, f"transpiles at start ({flag})"
        flags.append(flag)
    if not rest:
        return None, "names no file for node to run"
    return {"words": ["node", *flags, *rest], "file": rest[0].removeprefix("./"), "dropped": dropped, "env": env, "rewritten": rewritten}, None


# Development restarters and process managers whose file node runs as it is (docs/ROADMAP.md 20.4, D49).
RESTARTERS = ("nodemon", "node-dev", "pm2", "pm2-runtime", "forever")
RESTARTER_VALUE_FLAGS = ("-e", "--ext", "-w", "--watch", "-i", "--ignore", "-d", "--delay", "-x", "--exec", "-r", "--require", "--signal",
                         "-n", "--name", "--interpreter", "--node-args", "-l", "--log", "-o", "--outFile", "--errFile", "--pidFile", "--sourceDir",
                         "--workingDir", "--uid", "--gid", "-m", "--max", "--spinSleepTime", "--minUptime", "--killSignal", "-c", "--command",
                         "--instances", "--max-memory-restart", "--cron", "--env")


def restarted_file(words):
    """The JavaScript file a restarter's command line names, or None when it runs another command (nodemon --exec, an npm
    script, a pm2 ecosystem file) or names no file."""
    rest = list(words)
    if rest and rest[0] in ("start", "start-dev", "restart", "run"):  # pm2 start FILE, forever start FILE, pm2 run FILE
        rest = rest[1:]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        name, _, value = flag.partition("=")
        if name in ("-x", "--exec", "--interpreter", "-c", "--command"):
            return None
        if name in RESTARTER_VALUE_FLAGS and not value and rest:
            rest.pop(0)
    if not rest:
        return None
    file = rest[0].removeprefix("./")
    if not CODE.search(file) or re.search(r"(^|/)(ecosystem[^/]*|[^/]+\.config)\.[cm]?js$", file):
        return None  # pm2's ecosystem file describes processes; it is not the file node runs
    return file


# ---- where it listens
def call_arguments(text, start, limit=2):
    """The first `limit` top-level arguments of the call whose opening parenthesis ends at `start`, as written, comments
    left out; None when they cannot be read. A callback after them is never scanned."""
    depth, quote, current, args, escaped, index = 0, None, "", [], False, start
    end = min(len(text), start + 4000)
    while index < end:
        char = text[index]
        index += 1
        if quote:
            current += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "/" and text[index:index + 1] in ("/", "*"):
            close = text.find("\n" if text[index] == "/" else "*/", index)
            index = end if close < 0 else close + (1 if text[index] == "/" else 2)
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                args.append(current.strip())
                return [arg for arg in args if arg]
            depth -= 1
        elif char == "," and depth == 0:
            args.append(current.strip())
            if len(args) >= limit:
                return [arg for arg in args if arg]
            current = ""
            continue
        current += char
    return None


def env_aliases(sources):
    """Names bound to process.env (const env = process.env), which then read settings as env.NAME."""
    return sorted({name for text in sources.values() for name in re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*process\.env\b", text)})


CONFIG_HELPERS = r"(?:int|num|number|bool|boolean|str|string|list|flag|env|getEnv|required|optional|float)"


def reads_env(expression, aliases):
    """The variable names an expression reads from the environment: process.env.NAME, process.env["NAME"], an alias of
    process.env (env.NAME), and a configuration helper called with the name (int('PORT', 8787))."""
    names = re.findall(r"process\.env\.([A-Z][A-Z0-9_]{1,63})\b|process\.env\[\s*[\"']([A-Z][A-Z0-9_]{1,63})[\"']\s*\]", expression)
    found = [a or b for a, b in names]
    for alias in aliases:
        found += re.findall(rf"(?<![\w.$]){re.escape(alias)}\.([A-Z][A-Z0-9_]{{1,63}})\b", expression)
    found += re.findall(rf"(?<![\w.$]){CONFIG_HELPERS}\(\s*[\"']([A-Z][A-Z0-9_]{{1,63}})[\"']", expression)
    return list(dict.fromkeys(found))


def definition(name, sources, order):
    """The expression a name or a property is defined with, from the first file in `order` that defines it."""
    member = name.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", member):
        return None
    for item in order:
        text = sources.get(item, "")
        match = re.search(rf"\b(?:const|let|var)\s+{re.escape(member)}\s*=\s*([^;\n]+)", text)
        if match:
            return match.group(1).strip()
        match = re.search(rf"(?:^|[{{,\s]){re.escape(member)}\s*:\s*", text, re.M)  # an object's property: up to its comma or brace
        value = call_arguments(text, match.end(), limit=1) if match else None
        if value:
            return value[0]
        match = re.search(rf"\{{[^{{}}]*\b{re.escape(member)}\s*=\s*([^,}}]+)[^{{}}]*\}}\s*=\s*process\.env", text)
        if match:
            return match.group(1).strip()
    return None


def _value(expression, sources, order, depth=0):
    """An argument read: its text with one level of names and properties resolved."""
    text = (expression or "").strip()
    if depth < 2 and re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?", text) and not text.startswith("process.env"):
        found = definition(text, sources, order)
        if found is not None:
            return _value(found, sources, order, depth + 1) if re.fullmatch(r"[A-Za-z_$][\w$.]*", found) else found
    return text


def listen_facts(sources, order, frameworks):
    """Where the server listens, read from the first listen call of the files in `order` (the entry file first): {"port":
    a literal or None, "port_env": whether PORT is read, "host": the host literal or None, "host_env": the variable a host
    is read from, "fastify_without_host", "file"}; None when no listen call was found."""
    aliases = env_aliases(sources)
    lookup = list(dict.fromkeys(list(order) + sorted(sources)))  # a name is looked up in the scanned files first, then anywhere
    for item in order:
        text = sources.get(item, "")
        for match in re.finditer(r"\.listen\(\s*|\bserve\(\s*(?=\{)|\b[Hh]api\.[Ss]erver\(\s*(?=\{)|new\s+Hapi\.Server\(\s*(?=\{)", text):
            args = call_arguments(text, match.end())
            if not args:
                continue
            facts = {"file": item, "port": None, "port_env": False, "host": None, "host_env": None, "fastify_without_host": False}
            first = args[0]
            if first.startswith("{"):
                port = re.search(r"\bport\s*:\s*([^,}]+)", first)
                host = re.search(r"\b(?:host|hostname)\s*:\s*([^,}]+)", first)
                port_text = _value(port.group(1), sources, lookup) if port else ""
                host_text = _value(host.group(1), sources, lookup) if host else None
                facts["fastify_without_host"] = "fastify" in frameworks and match.group(0).startswith(".listen") and host is None
            else:
                port_text = _value(first, sources, lookup)
                second = args[1] if len(args) > 1 else ""
                host_text = None if not second or re.match(r"^(?:\(|function\b|async\b|[A-Za-z_$][\w$]*\s*=>)", second) else _value(second, sources, lookup)
            if "PORT" in reads_env(port_text, aliases) or re.search(r"\bPORT\b", port_text):
                facts["port_env"] = True
            literal = re.search(r"(?<![\w.])(\d{2,5})(?![\w.])", port_text)
            facts["port"] = int(literal.group(1)) if literal else None
            if host_text is not None:
                quoted = re.search(r"[\"'`]([^\"'`]+)[\"'`]", host_text)
                facts["host"] = quoted.group(1) if quoted else None
                names = reads_env(host_text, aliases)
                facts["host_env"] = names[0] if names else None
            return facts
    return None


def any_reads_port(sources):
    """Whether any of the code reads PORT, however it reads the environment."""
    return "PORT" in setting_names(sources)


# ---- health routes, settings, shutdown
def routes(sources, order):
    """{path: the file that routes it} for route literals of Express, Fastify, Koa, Hono and Hapi and plain servers
    (req.url === '/health', case '/health':), read from the application's own files."""
    seen = {}
    for item in order:
        for _, path in ROUTE.findall(sources.get(item, "")):
            seen.setdefault(path, item)
    return seen


def setting_names(sources):
    """The settings the code reads, by name: what reads_env finds, and names taken apart from process.env
    (const { PORT = 8080 } = process.env). Values are never read."""
    aliases = env_aliases(sources)
    names = set()
    for text in sources.values():
        names |= set(reads_env(text, aliases))
        for block in re.findall(r"\{([^{}]{1,600})\}\s*=\s*process\.env\b", text):
            names |= {part.split("=")[0].split(":")[0].strip() for part in block.split(",")}
    return sorted(name for name in names if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name))


SIGTERM_HANDLER = re.compile(r"""process\.(?:on|once|addListener|prependListener|prependOnceListener)\(\s*["']SIGTERM["']|"""
                             r"""["']SIGTERM["'][^\n;]{0,120}process\.(?:on|once)\(""")


def handles_sigterm(sources):
    """Whether the code listens for SIGTERM (process.on('SIGTERM', ...)), not merely sends it to a child."""
    return any(SIGTERM_HANDLER.search(text) for text in sources.values())


# ---- tests
def tests_of(package, local_paths, node):
    """(how, what, notes): how the tests run ("npm test" or "node --test"), what they are, and notes; how is None when
    there are none, said plainly (D46)."""
    scripts = (package or {}).get("scripts") if isinstance((package or {}).get("scripts"), dict) else {}
    test = scripts.get("test")
    files = sorted(item for item in local_paths if (NODE_TEST.search(item) or (node != "22" and NODE_TEST_TS.search(item))) and not NOT_CODE.search(item))
    configs = [(label, tool) for label, pattern, tool in TEST_CONFIGS for item in local_paths if "/" not in item and pattern.fullmatch(item)]
    configs += [(label, tool) for label, _, tool in TEST_CONFIGS if tool in (package or {}) and (label, tool) not in configs]
    if isinstance(test, str) and test.strip() and not PLACEHOLDER_TEST.fullmatch(test):
        return "npm test", test.strip(), []
    if files:
        return "node --test", f"{len(files)} test file{'s' if len(files) != 1 else ''} node --test finds", []
    notes = [f"A {label} configuration is there but package.json has no test script, so nothing would run it: add \"test\": \"{tool}\" "
             "to its scripts, and the platform runs npm test before every build" for label, tool in configs[:1]]
    return None, None, notes


# ---- the other managers' lockfiles (docs/ROADMAP.md 21.1): each read into the same view as npm's
def jsonc_loads(text):
    """JSON with comments and trailing commas (tsconfig.json, bun.lock), as the tools that write them read them."""
    out, index, quote = [], 0, None
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char == '"':
            quote = char
            out.append(char)
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(char)
        index += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def _registry_source(resolved):
    """registry, git, tarball, file or link, from where a lockfile entry says it comes."""
    text = str(resolved or "")
    if not text:
        return "registry"
    if text.startswith(("git", "github:", "gitlab:", "bitbucket:")) or "git+" in text or "codeload.github.com" in text:
        return "git"
    if text.startswith(("file:", "link:", "portal:")):
        return "file"
    if text.startswith(REGISTRIES):
        return "registry"
    if "://" in text:
        return "tarball"
    return "registry"


def view_of_npm(data):
    """An npm lockfile (version 2 or 3) as the view every manager's lockfile is read into: {"manager", "packages": {key:
    {"name", "version", "source", "resolved", "integrity", "scripts", "native"}}, "members": workspace folders}."""
    packages, members = {}, []
    for path, entry in sorted((data.get("packages") or {}).items()):
        if not path or not isinstance(entry, dict):
            continue
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        resolved = str(entry.get("resolved") or "")
        if entry.get("link") or "node_modules/" not in f"/{path}":
            source = "workspace" if not entry.get("link") and "node_modules/" not in f"/{path}" else "file"
            if source == "workspace":
                members.append(path)
        elif entry.get("inBundle"):
            source = "bundled"
        else:
            source = _registry_source(resolved) if resolved else "unresolved"
        packages[path] = {"name": name, "version": entry.get("version"), "source": source, "resolved": resolved,
                          "integrity": bool(entry.get("integrity")), "scripts": bool(entry.get("hasInstallScript")),
                          "native": bool(entry.get("libc") or entry.get("cpu") or entry.get("os"))}
    return {"manager": "npm", "packages": packages, "members": members}


def read_pnpm_lock(text):
    """(view, problem) for a pnpm-lock.yaml of lockfile version 6 or 9 (pnpm 8 to 12)."""
    import yaml
    try:
        # pnpm 10 and later may write the lockfile as several YAML documents (a `---` line between its sections): one mapping.
        data = {}
        for document in yaml.safe_load_all(text):
            if isinstance(document, dict):
                data.update(document)
    except yaml.YAMLError:
        return None, "is not valid YAML"
    if not isinstance(data, dict) or "lockfileVersion" not in data:
        return None, "is not a pnpm lockfile"
    version = str(data.get("lockfileVersion"))
    if not version.startswith(("6", "9")):
        return None, f"is pnpm lockfile version {version}, which the platform does not read (pnpm 8 and later write 6.0 or 9.0)"
    packages = {}
    for key, entry in (data.get("packages") or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        resolution = entry.get("resolution") if isinstance(entry.get("resolution"), dict) else {}
        key = str(key).lstrip("/")
        match = re.match(r"^((?:@[^/@]+/)?[^@/]+)@(.+)$", key)
        name, ver = (match.group(1), match.group(2)) if match else (key, None)
        if (ver or "").startswith("runtime:"):
            source = "runtime"  # pnpm 10's own Node.js download (executionEnv): the platform brings its own Node.js, so this entry is not installed
        elif "directory" in resolution or key.startswith("file:"):
            source = "file"
        elif "commit" in resolution or "repo" in resolution or (ver or "").startswith(("git", "github:", "https://codeload")):
            source = "git"
        elif "tarball" in resolution:
            source = _registry_source(resolution.get("tarball")) if str(resolution.get("tarball", "")).startswith(REGISTRIES) else "tarball"
        else:
            source = "registry"
        packages[key] = {"name": name, "version": ver if ver and re.match(r"^\d", ver) else None, "source": source,
                         "resolved": str(resolution.get("tarball") or ""), "integrity": bool(resolution.get("integrity")),
                         "scripts": bool(entry.get("requiresBuild")) if "requiresBuild" in entry else None,
                         "native": bool(entry.get("cpu") or entry.get("os") or entry.get("libc"))}
    members = [str(key) for key in (data.get("importers") or {}) if str(key) != "."]
    return {"manager": "pnpm", "packages": packages, "members": members, "lockfileVersion": version}, None


def read_yarn_lock(text):
    """(view, problem) for a yarn.lock: Yarn 1's own format, or Yarn 2 and later's YAML (the manager is "yarn1" or "yarn")."""
    import yaml
    if "__metadata:" in text:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None, "is not valid YAML"
        if not isinstance(data, dict):
            return None, "is not a Yarn lockfile"
        packages, members = {}, []
        for key, entry in data.items():
            if key == "__metadata" or not isinstance(entry, dict):
                continue
            resolution = str(entry.get("resolution") or "")
            match = re.match(r"^((?:@[^/@]+/)?[^@/]+)@([a-z+]+):(.*)$", resolution)
            name, protocol, rest = (match.group(1), match.group(2), match.group(3)) if match else (str(key).split("@npm:")[0], "", "")
            if protocol == "workspace":
                source = "workspace"
                members.append(rest)
            elif protocol == "npm":
                source = "registry"
            elif protocol in ("file", "link", "portal", "exec"):
                source = "file"
            elif protocol == "patch":
                source = "patch"
            elif protocol in ("git", "git+ssh", "git+https", "github") or "github.com" in rest:
                source = "git"
            elif protocol in ("http", "https"):
                source = "tarball"
            else:
                source = "unknown"
            packages[str(key)] = {"name": name, "version": str(entry.get("version") or "") or None, "source": source, "resolved": resolution,
                                  "integrity": bool(entry.get("checksum")), "scripts": None, "native": bool(entry.get("conditions"))}
        return {"manager": "yarn", "packages": packages, "members": members, "lockfileVersion": str((data.get("__metadata") or {}).get("version"))}, None
    if "# yarn lockfile v1" not in text.splitlines()[:5] and not re.search(r"(?m)^\S.*:\n  version ", text):
        return None, "is not a Yarn lockfile"
    packages, current = {}, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            key = line.rstrip()[:-1].strip('"')
            first = key.split(",")[0].strip().strip('"')
            match = re.match(r"^((?:@[^/@]+/)?[^@/]+)@(.*)$", first)
            current = {"name": match.group(1) if match else first, "spec": match.group(2) if match else "", "version": None, "resolved": "",
                       "integrity": False}
            packages[key] = current
            continue
        if current is None or not line.startswith("  ") or line.startswith("    "):
            continue
        field, _, value = line.strip().partition(" ")
        value = value.strip().strip('"')
        if field == "version":
            current["version"] = value
        elif field == "resolved":
            current["resolved"] = value.split("#", 1)[0]
        elif field == "integrity":
            current["integrity"] = bool(value)
    for entry in packages.values():
        spec = entry.pop("spec", "")
        if spec.startswith(("file:", "link:")):
            entry["source"] = "file"
        elif not entry["resolved"]:
            entry["source"] = "unresolved"
        else:
            entry["source"] = _registry_source(entry["resolved"])
        entry.update(scripts=None, native=False)
    return {"manager": "yarn1", "packages": packages, "members": [], "lockfileVersion": "1"}, None


def read_bun_lock(text):
    """(view, problem) for a bun.lock (Bun 1.2 and later; JSON with comments)."""
    try:
        data = jsonc_loads(text)
    except ValueError:
        return None, "is not valid JSON (with comments) as bun.lock is written"
    if not isinstance(data, dict) or "lockfileVersion" not in data or not isinstance(data.get("packages"), dict):
        return None, "is not a Bun lockfile"
    packages, members = {}, [key for key in (data.get("workspaces") or {}) if key]
    for key, entry in data["packages"].items():
        if not isinstance(entry, list) or not entry:
            continue
        spec = str(entry[0])
        match = re.match(r"^((?:@[^/@]+/)?[^@/]+)@(.*)$", spec)
        name, rest = (match.group(1), match.group(2)) if match else (spec, "")
        if rest.startswith("workspace:"):
            source, version = "workspace", None
        elif rest.startswith(("github:", "git+", "git:", "gitlab:", "bitbucket:")):
            source, version = "git", None
        elif rest.startswith(("file:", "link:")):
            source, version = "file", None
        elif rest.startswith(("http://", "https://")):
            source, version = "tarball", None
        elif rest.startswith("npm:"):
            source, version = "registry", rest.split("@")[-1] if "@" in rest else None
        else:
            source, version = "registry", rest
        registry = entry[1] if len(entry) > 1 and isinstance(entry[1], str) else ""
        if source == "registry" and registry and not registry.startswith(REGISTRIES):
            source = "tarball"
        meta = next((item for item in entry if isinstance(item, dict)), {})
        packages[str(key)] = {"name": name, "version": version, "source": source, "resolved": registry,
                              "integrity": any(isinstance(item, str) and item.startswith("sha512-") for item in entry[1:]) or source != "registry",
                              "scripts": None, "native": bool(meta.get("os") or meta.get("cpu"))}
    return {"manager": "bun", "packages": packages, "members": members, "lockfileVersion": str(data.get("lockfileVersion"))}, None


def read_lock(name, text):
    """(view, problem) for any lockfile the platform installs from, by its file name: npm's, pnpm's, Yarn's or Bun's."""
    base = name.rsplit("/", 1)[-1]
    if base in LOCKFILES:
        data, problem = read_lockfile(text)
        return (view_of_npm(data), None) if data is not None else (None, problem)
    if base == "pnpm-lock.yaml":
        return read_pnpm_lock(text)
    if base == "yarn.lock":
        return read_yarn_lock(text)
    if base == "bun.lock":
        return read_bun_lock(text)
    if base == "bun.lockb":
        return None, "is Bun's old binary lockfile, which only Bun reads"
    return None, "is not a lockfile the platform reads"


def view_problems(view):
    """[(package, problem)] for any manager's view: entries the manager would install without checking them against the
    npm registry (D42): a git repository, a tarball elsewhere, a file: or link: path, a patch, no integrity or checksum.
    A workspace member is not a problem: it is the repository's own code (21.4)."""
    problems = []
    for key, entry in sorted(view.get("packages", {}).items()):
        source, name = entry.get("source"), entry.get("name") or key
        if source in ("workspace", "runtime"):
            continue
        if source == "file":
            problems.append((name, "comes from a file: path" if str(entry.get("resolved") or "").startswith("file:")
                             else "is a local folder or a link (a file:, link: or portal: dependency)"))
        elif source == "bundled":
            problems.append((name, "is bundled inside another package (bundleDependencies), so it is never checked on its own"))
        elif source == "git":
            problems.append((name, "comes from a git repository"))
        elif source == "patch":
            problems.append((name, "is patched in the repository (patch:), so what installs is not what the registry published"))
        elif source == "tarball":
            resolved = entry.get("resolved") or ""
            problems.append((name, f"comes from {resolved.split('/')[2] if '://' in resolved else resolved[:60]}, outside the npm registry"))
        elif source == "unresolved":
            problems.append((name, "has no resolved address"))
        elif source == "unknown":
            problems.append((name, f"comes from a source the platform does not read ({(entry.get('resolved') or '')[:60]})"))
        elif not entry.get("integrity"):
            problems.append((name, "has no integrity hash"))
    return problems


def view_scripts(view):
    """name@version of the entries the lockfile says run an install script; None when this manager's lockfile does not
    record it (pnpm 9, Yarn, Bun), so the platform's own table (NEEDED_SCRIPTS) is the only guide."""
    entries = view.get("packages", {}).values()
    if all(entry.get("scripts") is None for entry in entries):
        return None
    return sorted({f"{entry['name']}@{entry['version']}" for entry in entries if entry.get("scripts") and entry.get("version")})


def view_names(view):
    return {str(entry.get("name") or key).lower(): entry.get("version") for key, entry in view.get("packages", {}).items()}


def view_native(view, names=()):
    return any(entry.get("scripts") or entry.get("native") for entry in view.get("packages", {}).values()) or bool(set(names) & NATIVE)


# ---- the managers (21.1): how each installs, frozen and with every script off, how it prunes and how it rebuilds one package
# Each command is run by the platform's pipeline and its image; the manager itself is installed by version and checksum
# (labdeploy/scaffold.py MANAGER_PINS), never through Corepack, which Node.js 25 and later no longer ship.
MANAGER_COMMANDS = {
    "npm": {"frozen": "npm ci --ignore-scripts --no-audit --no-fund", "production": "npm ci --omit=dev --ignore-scripts --no-audit --no-fund",
            "prune": "npm prune --omit=dev --ignore-scripts --no-audit --no-fund", "rebuild": "npm rebuild --ignore-scripts=false", "run": "npm run",
            "lockfiles": LOCKFILES, "title": "npm"},
    "pnpm": {"frozen": "pnpm install --frozen-lockfile --ignore-scripts", "production": "pnpm install --frozen-lockfile --ignore-scripts --prod",
             "prune": "pnpm prune --prod --ignore-scripts", "rebuild": "pnpm rebuild", "run": "pnpm run", "lockfiles": ("pnpm-lock.yaml",), "title": "pnpm"},
    "yarn": {"frozen": "yarn install --immutable", "production": "yarn workspaces focus --all --production", "prune": "yarn workspaces focus --all --production",
             "rebuild": "yarn rebuild", "run": "yarn run", "lockfiles": ("yarn.lock",), "title": "Yarn"},
    "yarn1": {"frozen": "yarn install --frozen-lockfile --ignore-scripts --non-interactive", "production": "yarn install --frozen-lockfile --ignore-scripts --non-interactive --production=true",
              "prune": "yarn install --frozen-lockfile --ignore-scripts --non-interactive --production=true", "rebuild": "npm rebuild --ignore-scripts=false",
              "run": "yarn run", "lockfiles": ("yarn.lock",), "title": "Yarn 1"},
    "bun": {"frozen": "bun install --frozen-lockfile --ignore-scripts", "production": "bun install --frozen-lockfile --ignore-scripts --production",
            "prune": "bun install --frozen-lockfile --ignore-scripts --production", "rebuild": "bun pm trust", "run": "bun run", "lockfiles": ("bun.lock",),
            "title": "Bun"},
}


def yarn_links_node_modules(yarnrc_text):
    """Whether a .yarnrc.yml makes Yarn 2+ write node_modules (nodeLinker: node-modules or pnpm); Plug'n'Play, its default,
    needs its own loader at every start, which the platform's image does not carry."""
    return re.search(r"(?m)^\s*nodeLinker\s*:\s*[\"']?(node-modules|pnpm)[\"']?\s*$", yarnrc_text or "") is not None


# ---- builds (21.2): the frameworks whose service exists only after a build, with the output and the start each documents
# (docs/RESEARCH-2026-10-20.md 6.3, read in the official pages on 2026-10-20). `start` runs node itself on the built file;
# `env` is what the start reads for its host; `check` names a configuration the build needs, read as text.
NODE_BUILDS = {
    "next": {"label": "Next.js", "build": ["next", "build"], "standalone": ["node", "server.js"], "serve": ["node", "node_modules/next/dist/bin/next", "start", "-H", EVERYWHERE[0], "-p", "$PORT"],
             "env": {"HOSTNAME": EVERYWHERE[0]}, "config": ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")},
    "@nestjs/core": {"label": "NestJS", "build": ["nest", "build"], "start": ["node", "dist/main.js"], "env": {}},
    "@react-router/serve": {"label": "React Router", "build": ["react-router", "build"], "start": ["node", "node_modules/@react-router/serve/dist/cli.js", "./build/server/index.js"],
                            "env": {"HOST": EVERYWHERE[0]}},
    "@remix-run/serve": {"label": "Remix", "build": ["remix", "vite:build"], "start": ["node", "node_modules/@remix-run/serve/dist/cli.js", "./build/server/index.js"],
                         "env": {"HOST": EVERYWHERE[0]}},
    "@sveltejs/adapter-node": {"label": "SvelteKit", "build": ["vite", "build"], "start": ["node", "build"], "env": {"HOST": EVERYWHERE[0]}},
    "nuxt": {"label": "Nuxt", "build": ["nuxt", "build"], "start": ["node", ".output/server/index.mjs"], "env": {"NITRO_HOST": EVERYWHERE[0], "HOST": EVERYWHERE[0]}},
    "@astrojs/node": {"label": "Astro", "build": ["astro", "build"], "start": ["node", "./dist/server/entry.mjs"], "env": {"HOST": EVERYWHERE[0]}},
    "@adonisjs/core": {"label": "AdonisJS", "build": ["node", "ace", "build"], "start": ["node", "build/bin/server.js"], "env": {"HOST": EVERYWHERE[0]}},
}
# Frameworks that are a front-end site unless a server adapter is among the dependencies (D55, Phase 22): SvelteKit without
# adapter-node, Astro without @astrojs/node, and the site builders.
SITE_ONLY = {"@sveltejs/kit": "@sveltejs/adapter-node", "astro": "@astrojs/node"}
# A build that needs a secret (docs/RESEARCH-2026-10-20.md 6.3): Strapi and Payload read their keys when they build. Refused
# with the name, because the platform's build has no secrets.
SECRET_BUILDS = {"@strapi/strapi": "Strapi", "payload": "Payload"}
# The start-script tools a build makes unnecessary (21.2, 21.3): each rewritten to node on the built file.
BUILT_STARTS = {"next": "next", "nest": "@nestjs/core", "remix-serve": "@remix-run/serve", "react-router-serve": "@react-router/serve",
                "tsx": None, "ts-node": None, "ts-node-dev": None}


def tsconfig_options(texts, name="tsconfig.json", depth=0):
    """compilerOptions of a tsconfig.json (comments allowed), with `extends` followed inside the folder; {} when unreadable."""
    text = texts.get(name)
    if not text or depth > 4:
        return {}
    try:
        data = jsonc_loads(text)
    except ValueError:
        return {}
    options = {}
    parent = data.get("extends") if isinstance(data, dict) else None
    if isinstance(parent, str) and parent.startswith("./"):
        target = parent[2:] + ("" if parent.endswith(".json") else ".json")
        options.update(tsconfig_options(texts, target, depth + 1))
    options.update((data.get("compilerOptions") or {}) if isinstance(data, dict) else {})
    return options


def built_path(source, options):
    """Where tsc writes a source file: outDir (dist by default) joined with the path relative to rootDir (the longest common
    folder of the sources, which for src/index.ts is src, so dist/index.js)."""
    out = str(options.get("outDir") or "dist").strip("./").rstrip("/") or "dist"
    root = str(options.get("rootDir") or "").strip("./").rstrip("/")
    relative = source
    if root and source.startswith(root + "/"):
        relative = source[len(root) + 1:]
    elif options.get("rootDir") is None and source.startswith("src/"):
        relative = source[4:]  # no rootDir: tsc's root is the common folder of the sources, src/ for the usual layout
    return f"{out}/{re.sub(r'\.[cm]?ts$', lambda m: {'.ts': '.js', '.mts': '.mjs', '.cts': '.cjs'}[m.group(0)], relative)}"


def strips_types(options):
    """Whether tsc's options say the code uses only the syntax Node strips (TypeScript 5.8's erasableSyntaxOnly), so a .ts
    entry runs on Node 24 and 26 without a build (21.3)."""
    return bool(options.get("erasableSyntaxOnly")) and not options.get("emitDecoratorMetadata") and not options.get("experimentalDecorators")


def build_plan(package, repo_has, texts, manager, line, start_words=None):
    """What a service needs before it starts (21.2, 21.3): {"framework", "build": words or None, "start": words, "env", "copy":
    the output paths the final image copies (None: everything), "why", "problem"}. `repo_has(path)` says whether a file is
    in the service; `texts` holds the configuration files read; `start_words` is the start the reader found."""
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    deps = runtime_names(package) | {name.lower() for name in ((package.get("devDependencies") or {}) if isinstance(package.get("devDependencies"), dict) else {})}
    run = MANAGER_COMMANDS[manager]["run"].split()
    for name, label in SECRET_BUILDS.items():
        if name in deps:
            return {"framework": label, "build": None, "start": None, "env": {}, "copy": None, "why": None,
                    "problem": f"uses {label}, whose build reads its secret keys; the platform's build has no secrets (a later roadmap item)"}
    options = tsconfig_options(texts) if repo_has("tsconfig.json") else {}
    for name, entry in NODE_BUILDS.items():
        if name not in deps:
            continue
        # The repository's own build script, as every platform runs it; else the framework's command through its installed file.
        if isinstance(scripts.get("build"), str) and scripts["build"].strip():
            build = [*run, "build"]
        elif entry["build"][0] == "node":
            build = list(entry["build"])
        else:
            build = ["node", f"node_modules/.bin/{entry['build'][0]}", *entry["build"][1:]]
        if name == "next":
            config = next((texts.get(item, "") for item in entry["config"] if repo_has(item)), "")
            standalone = re.search(r"""output\s*:\s*["']standalone["']""", config) is not None
            start = entry["standalone"] if standalone else entry["serve"]
            copy = [".next/standalone", ".next/static", "public"] if standalone else None
            why = f"{entry['label']} ({'output: standalone, started by its server.js' if standalone else 'started by next start under node'})"
            return {"framework": entry["label"], "build": build, "start": start, "env": dict(entry["env"]), "copy": copy, "why": why, "problem": None,
                    "standalone": standalone}
        return {"framework": entry["label"], "build": build, "start": list(entry["start"]), "env": dict(entry["env"]), "copy": None,
                "why": f"{entry['label']}, built with {' '.join(build)} and started by {' '.join(entry['start'])}", "problem": None}
    for name, adapter in SITE_ONLY.items():
        if name in deps and adapter not in deps:
            return {"framework": name, "build": None, "start": None, "env": {}, "copy": None, "why": None,
                    "problem": f"uses {name} without {adapter}: it builds a front-end site with no server of its own"}
    file = start_words[1] if start_words and len(start_words) > 1 and start_words[0] == "node" else None
    if file and re.search(r"\.[cm]?ts$", file):
        if strips_types(options) and line != "22":
            return {"framework": None, "build": None, "start": list(start_words), "env": {}, "copy": None,
                    "why": f"{file} runs under Node's type stripping: tsconfig.json says erasableSyntaxOnly", "problem": None}
        if "typescript" not in deps:
            return {"framework": None, "build": None, "start": None, "env": {}, "copy": None, "why": None,
                    "problem": f"starts {file}, TypeScript that needs a build, and typescript is not among the dependencies"}
        built = built_path(file, options)
        build = [*run, "build"] if isinstance(scripts.get("build"), str) and "tsc" in scripts["build"] else ["node", "node_modules/.bin/tsc"]
        return {"framework": "TypeScript", "build": build, "start": ["node", built], "env": {}, "copy": None, "problem": None,
                "why": f"{file} is TypeScript{'' if line != '22' else ' on Node.js 22'}: built with tsc into {built}"}
    if file and not repo_has(file) and isinstance(scripts.get("build"), str):
        return {"framework": None, "build": [*run, "build"], "start": list(start_words), "env": {}, "copy": None, "problem": None,
                "why": f"{file} is not in the repository: the build script makes it"}
    return {"framework": None, "build": None, "start": list(start_words) if start_words else None, "env": {}, "copy": None, "why": None, "problem": None}


# ---- install scripts the platform's table names (21.5, D53): packages whose script is needed on Cloud Run, read in each
# package's own repository on 2026-10-20, with the versions it holds for. The reader drafts them into build.scripts at the
# locked version for the merge to approve; name-only approvals stay refused.
NEEDED_SCRIPTS = {
    "sharp": ("below 0.33", lambda v: numbers_of(v) < (0, 33), "its install script downloaded or built libvips; 0.33 and later ship the binary as optional dependencies"),
    "bcrypt": ("below 5.1", lambda v: numbers_of(v) < (5, 1), "node-pre-gyp fetched its binary at install; 5.1 and later ship prebuilt binaries"),
    "canvas": ("every version", lambda v: True, "node-pre-gyp fetches or builds its binary at install"),
    "sqlite3": ("every version", lambda v: True, "node-pre-gyp fetches or builds its binary at install"),
    "better-sqlite3": ("every version", lambda v: True, "prebuild-install fetches or builds its binary at install"),
    "argon2": ("every version", lambda v: True, "node-gyp-build fetches or builds its binary at install"),
    "re2": ("every version", lambda v: True, "its install script builds the RE2 binding"),
    "cpu-features": ("every version", lambda v: True, "its install script builds the binding"),
    "node-pty": ("every version", lambda v: True, "node-gyp builds the binding at install"),
    "@tensorflow/tfjs-node": ("every version", lambda v: True, "its install script downloads the TensorFlow binary"),
    "@parcel/watcher": ("every version", lambda v: True, "node-gyp-build fetches or builds its binary at install"),
    "puppeteer": ("every version", lambda v: True, "its install script downloads a browser, which Cloud Run cannot run anyway: use puppeteer-core"),
}


def numbers_of(version):
    return tuple(int(part) for part in re.findall(r"\d+", str(version or ""))[:3])


def needed_scripts(names):
    """[(name@version, why)] from {name: version}: the packages the platform's table says need their install script on Cloud
    Run at that version."""
    found = []
    for name, version in sorted(names.items()):
        entry = NEEDED_SCRIPTS.get(name)
        if entry and version and entry[1](version):
            found.append((f"{name}@{version}", f"{entry[2]} ({entry[0]})"))
    return found


def approved_scripts(package, manager):
    """The install scripts package.json approves for its own manager, as name@version entries where the file gives a version:
    npm's allowScripts (versions required), pnpm's onlyBuiltDependencies, Yarn's dependenciesMeta built, Bun's
    trustedDependencies (names only, so the locked version is joined on by the reader). Returns (names, name@version entries, notes)."""
    if manager == "npm":
        pinned, notes = allowed_scripts(package)
        return [], pinned, notes
    if manager == "pnpm":
        table = (package.get("pnpm") or {}) if isinstance(package.get("pnpm"), dict) else {}
        names = [str(item) for item in (table.get("onlyBuiltDependencies") or []) if isinstance(item, str)]
        return names, [], []
    if manager in ("yarn", "yarn1"):
        meta = package.get("dependenciesMeta") if isinstance(package.get("dependenciesMeta"), dict) else {}
        names, pinned = [], []
        for key, value in meta.items():
            if isinstance(value, dict) and value.get("built"):
                (pinned if SCRIPT_PACKAGE.fullmatch(str(key)) else names).append(str(key))
        return names, pinned, []
    if manager == "bun":
        return [str(item) for item in (package.get("trustedDependencies") or []) if isinstance(item, str)], [], []
    return [], [], []


def ecosystem_script(text):
    """The script a pm2 ecosystem file in JSON starts (apps[0].script), or None (a .js ecosystem file is code, never read)."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    apps = data.get("apps") if isinstance(data, dict) else None
    first = apps[0] if isinstance(apps, list) and apps and isinstance(apps[0], dict) else {}
    script = first.get("script")
    return str(script).removeprefix("./") if isinstance(script, str) and CODE.search(script) and not first.get("interpreter") else None
