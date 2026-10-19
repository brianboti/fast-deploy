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
OTHER_LOCKFILES = {"yarn.lock": "Yarn", "pnpm-lock.yaml": "pnpm", "bun.lock": "Bun", "bun.lockb": "Bun"}
MANAGERS = ("npm", "yarn", "pnpm", "bun")
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
)
UNPROVIDED = (("redis", r"^(redis|ioredis)$"), ("mysql", r"^(mysql|mysql2)$"), ("mongodb", r"^(mongodb|mongoose)$"))
WORKERS = r"^(bull|bullmq|bee-queue|agenda|node-cron|cron|node-schedule)$"
SQLITE = re.compile(r"""["'](?:node:sqlite|better-sqlite3|sqlite3)["']""")
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
    if first in START_TOOLS:
        return None, START_TOOLS[first]
    if first != "node":
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
    return {"words": ["node", *flags, *rest], "file": rest[0].removeprefix("./"), "dropped": dropped, "env": env}, None


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
