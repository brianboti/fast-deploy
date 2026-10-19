"""Phase 18: Node.js web services (docs/ROADMAP.md 18.1 to 18.7, decisions D41 to D46). The contract, the reader's Node.js
ladders and refusals, the lock the platform makes, the pipeline and its dependency audit, the image and the drafter. No
test runs npm or touches the network; the drafted health test runs on the Node.js of the machine when there is one."""
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_onboarding import PLATFORM
from test_phase11 import DraftingGitHub
from test_phase13 import controller_with
from test_scaffold import hadolint_command, zizmor_command

from labdeploy import chat, dockerpolicy, node, pins, reader, scaffold, scan
from labdeploy.contract import Contracts, node_lock_path
from labdeploy.models import NODE_END_OF_LIFE, AppSpec, default_node, draft_dump, enforce, parse_contract, parse_spec, spec_hash
from labdeploy.onboarding import contract_check
from labdeploy.release import build_checks
from labdeploy.status import summarize
from labdeploy.workflows import central_deploy, lay_out

SERVER = ("const express = require('express');\nconst app = express();\napp.get('/health', (req, res) => res.send('ok'));\n"
          "const server = app.listen(process.env.PORT || 3000);\nprocess.on('SIGTERM', () => server.close(() => process.exit(0)));\n")
INTEGRITY = "sha512-" + "A" * 86 + "=="


def lockfile(package, packages=None, version=3):
    """An npm lockfile for a package.json: its root mirrors the manifest, and every package comes from the registry."""
    root = {key: package[key] for key in ("name", "version", "dependencies", "devDependencies", "optionalDependencies") if key in package}
    entries = {"": root}
    names = packages if packages is not None else {name: {} for name in package.get("dependencies", {})}
    for name, extra in names.items():
        entries[f"node_modules/{name}"] = {"version": "1.2.3", "resolved": f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-1.2.3.tgz",
                                           "integrity": INTEGRITY, **extra}
    return json.dumps({"name": package.get("name"), "version": package.get("version"), "lockfileVersion": version, "requires": True, "packages": entries})


def repo(start="node server.js", deps=None, extra=None, packages=None, files=None, lock=True):
    """A Node.js service at the root: package.json, a lockfile and an Express server that reads PORT and handles SIGTERM."""
    package = {"name": "svc", "version": "1.0.0", "scripts": {"start": start} if start else {}, "dependencies": {"express": "^4.21.2"} if deps is None else deps}
    package.update(extra or {})
    result = {"package.json": json.dumps(package), "server.js": SERVER}
    if lock:
        result["package-lock.json"] = lockfile(package, packages)
    result.update(files or {})
    return {key: value for key, value in result.items() if value is not None}


def read(files, path="", **kwargs):
    return reader.read(sorted(files) + sorted({p.rsplit("/", 1)[0] for p in files if "/" in p}), files, path, **kwargs)


def choice(result, what):
    return next(item for item in result["choices"] if item["what"] == what)


def refused(result, fragment):
    return any(fragment in item["problem"] for item in result["refusals"])


# ---- 18.1 the contract (D41, D43)
def test_a_node_service_names_its_line_and_never_python():
    spec = AppSpec(name="svc", runtime="node-http", node="24")
    assert spec.node == "24" and spec.python == "3.12"  # python keeps its unused default, left out of every dump
    with pytest.raises(ValueError, match="needs node"):
        AppSpec(name="svc", runtime="node-http")
    with pytest.raises(ValueError, match="names its line in node"):
        AppSpec(name="svc", runtime="node-http", node="24", python="3.13")
    with pytest.raises(ValueError, match="names its version in python"):
        AppSpec(name="svc", node="24")
    for line in ("20", "23", "25", "27", "24.1"):
        with pytest.raises(ValueError):
            AppSpec(name="svc", runtime="node-http", node=line)
    # Existing Python contracts hash as they did; a Node contract's full dump reads back as the same specification.
    assert spec_hash(AppSpec(name="demo")) == spec_hash(AppSpec(name="demo", python="3.12", runtime="python-http"))
    again = AppSpec.model_validate(spec.model_dump())
    assert spec_hash(again) == spec_hash(spec) and spec_hash(parse_spec(yaml.safe_dump(spec.model_dump(exclude_defaults=True)))) == spec_hash(spec)
    data = draft_dump(AppSpec(name="svc", runtime="node-http", node="26"))
    assert "python" not in data and data["node"] == "26" and data["build"] == {"context": "service"}
    assert "node" not in draft_dump(AppSpec(name="demo"))


def test_build_scripts_are_packages_with_exact_versions_and_node_only():
    spec = AppSpec(name="svc", runtime="node-http", node="24", build={"scripts": ["onnxruntime-node@1.30.0", "@img/sharp-linux-x64@0.34.4-rc.1"]})
    assert spec.build.scripts == ["onnxruntime-node@1.30.0", "@img/sharp-linux-x64@0.34.4-rc.1"]
    for bad in ("onnxruntime-node", "--all", "*", "sharp@^0.34.0", "sharp@latest", "Sharp@1.0.0", "a b@1.0.0", "sharp@1.0", "x@1.0.0; rm -rf /"):
        with pytest.raises(ValueError, match="exact version"):
            AppSpec(name="svc", runtime="node-http", node="24", build={"scripts": [bad]})
    with pytest.raises(ValueError, match="each once"):
        AppSpec(name="svc", runtime="node-http", node="24", build={"scripts": ["a@1.0.0", "a@1.0.0"]})
    with pytest.raises(ValueError, match="applies to runtime: node-http"):
        AppSpec(name="svc", build={"scripts": ["a@1.0.0"]})


def test_a_node_service_starts_node_itself_and_installs_from_the_registry():
    for build, command in (({"recipe": "platform", "entrypoint": "npm start"}, None), ({"recipe": "buildpacks", "entrypoint": "yarn start"}, None),
                           ({}, ["npm", "run", "serve"])):
        with pytest.raises(ValueError, match="started by node itself"):
            AppSpec(name="svc", runtime="node-http", node="24", build=build, command=command)
    with pytest.raises(ValueError, match="cannot install Node.js packages"):
        AppSpec(name="svc", runtime="node-http", node="24", build={"recipe": "platform", "entrypoint": "node server.js", "network": "none"})
    assert AppSpec(name="svc", runtime="node-http", node="24", build={"network": "none"})  # its own Dockerfile may vendor them


def test_a_line_is_refused_from_its_end_of_life_and_the_default_follows_active_lts():
    assert NODE_END_OF_LIFE["22"] == datetime.date(2027, 4, 30) and NODE_END_OF_LIFE["24"] == datetime.date(2028, 4, 30)
    for line, last in (("22", datetime.date(2027, 4, 29)), ("24", datetime.date(2028, 4, 29))):
        spec = AppSpec(name="svc", runtime="node-http", node=line, reviewed=True)
        enforce(spec, today=last)
        with pytest.raises(ValueError, match=f"Node.js {line} reached its end of life"):
            enforce(spec, today=last + datetime.timedelta(days=1))
    assert default_node(datetime.date(2026, 10, 19)) == "24" and default_node(datetime.date(2026, 10, 27)) == "24"
    assert default_node(datetime.date(2026, 10, 28)) == "26" and default_node(datetime.date(2028, 6, 1)) == "26"
    runbook = Path("docs/RUNBOOK.md").read_text()
    assert "2027-04-30" in runbook and "2028-04-30" in runbook


def test_a_kept_contract_for_a_node_service_reads_back_the_same():
    text = ("source:\n  repository: lab/notes\n  commit: " + "f" * 40 + "\nname: notes\nruntime: node-http\nnode: '24'\n"
            "build:\n  recipe: platform\n  entrypoint: node server.js\n  scripts:\n  - sharp@0.34.4\n")
    spec, source = parse_contract(text)
    assert spec.runtime == "node-http" and spec.node == "24" and spec.build.scripts == ["sharp@0.34.4"] and spec.reviewed
    assert spec_hash(parse_spec(yaml.safe_dump(spec.model_dump(exclude_defaults=True)))) == spec_hash(spec)


# ---- 18.2 the reader: which Node (D41)
@pytest.mark.parametrize(("files", "expected", "source"), [
    ({".nvmrc": "v24.1.0\n"}, "24", ".nvmrc"),
    ({".nvmrc": "lts/krypton\n"}, "24", ".nvmrc"),
    ({".nvmrc": "lts/*\n"}, None, ".nvmrc"),
    ({".node-version": "26\n"}, "26", ".node-version"),
    ({".tool-versions": "python 3.12.4\nnodejs 22.13.0\n"}, "22", ".tool-versions"),
    ({".nvmrc": "22\n", ".node-version": "26\n"}, "22", ".nvmrc"),
    ({"Dockerfile": "FROM node:22-slim@sha256:" + "a" * 64 + "\nUSER 65532\nCMD [\"node\", \"server.js\"]\n", ".nvmrc": "26\n"}, "22", "Dockerfile (FROM node:22)"),
])
def test_the_node_line_ladder(files, expected, source):
    result = read(repo(files=files))
    assert not result["refusals"], result["refusals"]
    assert choice(result, "Node.js")["value"] == (expected or default_node()) and choice(result, "Node.js")["from"].startswith(source)
    assert result["spec"].node == (expected or default_node())


@pytest.mark.parametrize(("engines", "expected"), [(">=22", "26"), ("^22.13.0", "22"), ("22.x || 24.x", "24"), ("22.13.0", "22"), ("~24.10", "24"),
                                                   (">=20 <25", "24"), ("20 - 24", "24"), ("*", "26")])
def test_engines_node_resolves_to_the_highest_supported_line_it_allows(engines, expected):
    result = read(repo(extra={"engines": {"node": engines}}))
    assert result["spec"].node == expected and choice(result, "Node.js")["from"] == f"package.json (engines.node {engines})"
    assert any("no upper bound" in note for note in result["notes"]) == (engines in (">=22", "*"))


def test_dev_engines_runtime_is_the_last_rung_before_the_default():
    result = read(repo(extra={"devEngines": {"runtime": {"name": "node", "version": "^22.0.0", "onFail": "error"}}}))
    assert result["spec"].node == "22" and "devEngines.runtime" in choice(result, "Node.js")["from"]
    assert any("end of life on 2027-04-30" in note for note in result["notes"])
    plain = read(repo())
    assert plain["spec"].node == default_node() and choice(plain, "Node.js")["from"].startswith("the platform's default")


@pytest.mark.parametrize(("files", "fragment"), [
    ({".nvmrc": "18\n"}, "outside the supported 22, 24 and 26"),
    ({".nvmrc": "lts/iron\n"}, "outside the supported 22, 24 and 26"),
    ({".nvmrc": "node\n"}, "not a Node.js line"),
    ({".node-version": "25.1.0\n"}, "outside the supported 22, 24 and 26"),
    ({"Dockerfile": "FROM node:20-slim\nUSER 65532\nCMD [\"node\", \"server.js\"]\n"}, "builds on Node.js 20"),
])
def test_a_line_outside_22_24_and_26_is_refused(files, fragment):
    result = read(repo(files=files))
    assert result["spec"] is None and refused(result, fragment), result["refusals"]


def test_engines_that_allow_no_supported_line_or_cannot_be_read_are_refused():
    assert refused(read(repo(extra={"engines": {"node": "<22"}})), "allows no supported Node.js line")
    assert refused(read(repo(extra={"engines": {"node": "latest"}})), "cannot be read as an npm version range")
    assert refused(read(repo(extra={"engines": {"node": ">=24", "iojs": ">=1"}})), "names io.js")


def test_npm_ranges_are_read_like_npm_reads_them():
    meets = node.lines_meeting
    assert meets(">=22") == ["22", "24", "26"] and meets("^24") == ["24"] and meets("24.x") == ["24"] and meets("~22.13.0") == ["22"]
    assert meets(">22") == ["24", "26"] and meets("<=24") == ["22", "24"] and meets("<24.0.0") == ["22"] and meets("22 - 24.5") == ["22", "24"]
    assert meets(">=21 <23 || >=26") == ["22", "26"] and meets("v24.1.0") == ["24"] and meets("") == ["22", "24", "26"]
    assert meets("lts") is None and meets(">=banana") is None and meets(None) is None
    assert node.line_of("v26.0.0\n") == ("26", None) and node.line_of("# comment\n24\n") == ("24", None) and node.line_of("lts/jod") == ("22", None)
    assert node.line_of("stable")[0] is None and node.line_of("lts/argon")[0] is None


# ---- 18.2 the packages (D42)
def test_the_repositorys_lockfile_is_used_and_checked_entry_by_entry():
    result = read(repo())
    assert result["lock"] == {"action": "use", "runtime": "node-http", "from": "package-lock.json", "file": "package-lock.json"}
    assert choice(result, "Dependencies")["value"].startswith("package-lock.json, installed with npm ci and every install script off")
    package = json.loads(repo()["package.json"])
    shrinkwrap = read(repo(files={"npm-shrinkwrap.json": lockfile(package)}))
    assert shrinkwrap["lock"]["file"] == "npm-shrinkwrap.json" and shrinkwrap["ignored"][0]["file"] == "package-lock.json"


@pytest.mark.parametrize(("entry", "fragment"), [
    ({"integrity": None}, "has no integrity hash"),
    ({"resolved": "git+ssh://git@github.com/lab/x.git#abc"}, "comes from a git repository"),
    ({"resolved": "https://example.com/x-1.2.3.tgz"}, "outside the npm registry"),
    ({"resolved": "file:../x"}, "comes from a file: path"),
    ({"link": True}, "a local folder or a link"),
    ({"inBundle": True}, "bundled inside another package"),
    ({"resolved": None}, "has no resolved address"),
])
def test_a_lockfile_entry_npm_ci_would_not_check_against_the_registry_is_refused(entry, fragment):
    package = {"name": "svc", "version": "1.0.0", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4.21.2"}}
    data = json.loads(lockfile(package))
    item = data["packages"]["node_modules/express"]
    for key, value in entry.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    result = read(repo(files={"package-lock.json": json.dumps(data)}))
    assert result["spec"] is None and refused(result, fragment), result["refusals"]
    assert node.lock_problems(data)


def test_lockfiles_that_npm_ci_cannot_trust_or_use_are_refused():
    v1 = json.dumps({"name": "svc", "lockfileVersion": 1, "dependencies": {}})
    assert refused(read(repo(files={"package-lock.json": v1})), "lockfile version 1")
    assert refused(read(repo(files={"package-lock.json": "{not json"})), "not valid JSON")
    stale = json.loads(repo()["package-lock.json"])
    stale["packages"][""]["dependencies"] = {"express": "^4.0.0"}
    assert refused(read(repo(files={"package-lock.json": json.dumps(stale)})), "does not match package.json")
    assert refused(read(repo(deps={"mylib": "github:lab/mylib", "express": "^4"})), "through a local path, a git repository or a URL")
    assert refused(read(repo(deps={"shared": "workspace:*"})), "through a local path")
    assert refused(read(repo(extra={"bundleDependencies": ["express"]})), "bundleDependencies")


def test_other_package_managers_are_refused_with_the_conversion():
    yarn = read(repo(lock=False, files={"yarn.lock": "# yarn lockfile v1\n"}))
    assert refused(yarn, "Yarn's lockfile") and "docs/DEVELOPER-CONTRACT.md" in yarn["refusals"][0]["fix"]
    assert refused(read(repo(files={"pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})), "more than one lockfile")
    assert refused(read(repo(lock=False, files={"bun.lock": "{}"})), "Bun's lockfile")
    two = read(repo(extra={"packageManager": "yarn@4.5.0", "engines": {"npm": ">=10"}}))
    assert refused(two, "names yarn in packageManager but npm in engines")
    assert any("packageManager names pnpm" in note for note in read(repo(extra={"packageManager": "pnpm@9.1.0"}))["notes"])


def test_without_a_lockfile_the_platform_locks_it():
    result = read(repo(lock=False))
    assert result["lock"]["action"] == "lock" and result["lock"]["package"] == repo()["package.json"]
    assert "locked once by the platform" in choice(result, "Dependencies")["value"] and "three days" in choice(result, "Dependencies")["value"]
    assert choice(result, "Install scripts")["value"].startswith("none run")


# ---- 18.2 install scripts (D43)
def test_install_scripts_run_only_for_packages_allow_scripts_pins_by_version():
    packages = {"express": {}, "sharp": {"hasInstallScript": True}, "esbuild": {"hasInstallScript": True}, "bcrypt": {"hasInstallScript": True}}
    deps = {name: "^1.2.3" for name in packages}
    result = read(repo(deps=deps, packages=packages, extra={"allowScripts": {"sharp@1.2.3": True, "esbuild": True, "bcrypt@9.9.9": True}}))
    assert result["spec"].build.scripts == ["sharp@1.2.3"]
    assert choice(result, "Install scripts")["value"] == "run after the install, each by name and version: sharp@1.2.3"
    assert any("approves esbuild without an exact version" in note for note in result["notes"])
    assert any("bcrypt@1.2.3" in note and "esbuild@1.2.3" in note for note in result["notes"] if note.startswith("These packages have install scripts"))
    nothing = read(repo())
    assert nothing["spec"].build.scripts == [] and choice(nothing, "Install scripts")["value"] == "none run: no package has one"


def test_scripts_a_cloud_run_service_never_needs_are_left_out_and_said_why():
    # Sophia's two: onnxruntime-node's postinstall fetches CUDA libraries, protobufjs's only prints a warning.
    packages = {"onnxruntime-node": {"hasInstallScript": True, "version": "1.30.0"}, "protobufjs": {"hasInstallScript": True, "version": "7.6.6"}}
    deps = {"onnxruntime-node": "^1.30.0", "protobufjs": "^7.6.6"}
    result = read(repo(deps=deps, packages=packages, extra={"allowScripts": {"onnxruntime-node@1.30.0": True, "protobufjs@7.6.6": True}}))
    assert result["spec"].build.scripts == []
    why = choice(result, "Install scripts")["value"]
    assert why.startswith("none run: onnxruntime-node@1.30.0: its postinstall only downloads the CUDA and TensorRT libraries")
    assert "protobufjs@7.6.6: its postinstall only reads the parent package.json" in why


# ---- 18.2 what starts it (D45, D46)
@pytest.mark.parametrize(("start", "fragment", "fix"), [
    ("nodemon server.js", "restarts on file changes (nodemon)", "start the service with node itself"),
    ("ts-node src/index.ts", "transpiles TypeScript at start (ts-node)", "commit the built output"),
    ("tsx watch src/index.ts", "transpiles TypeScript at start (tsx)", "commit the built output"),
    ("node --watch server.js", "watches or debugs (--watch)", "start the service with node itself"),
    ("node --inspect=0.0.0.0 server.js", "watches or debugs (--inspect)", "start the service with node itself"),
    ("node -r ts-node/register src/index.ts", "transpiles at start", "commit the built output"),
    ("vite", "runs Vite's development server", "the platform serves web services"),
    ("next start", "runs Next.js, which needs a build", "commit the built output"),
    ("concurrently \"node api.js\" \"node worker.js\"", "starts several processes at once", "start the service with node itself"),
    ("node migrate.js && node server.js", "chains commands through a shell", "start the service with node itself"),
    ("sh -c 'node server.js'", "runs a shell", "start the service with node itself"),
    ("pm2-runtime server.js", "process manager", "start the service with node itself"),
    ("npm run serve", "runs npm, not node", "start the service with node itself"),
])
def test_a_start_script_that_watches_transpiles_or_chains_is_refused(start, fragment, fix):
    result = read(repo(start=start))
    assert result["spec"] is None
    item = next(item for item in result["refusals"] if "scripts.start" in item["problem"])
    assert fragment in item["problem"] and item["fix"].startswith(fix), item
    assert len([item for item in result["refusals"] if item["file"] == "package.json"]) == 1  # one refusal for one start script


def test_the_start_ladder_runs_node_on_a_file_of_the_repository():
    env_file = read(repo(start="cross-env NODE_ENV=production node --env-file=.env --enable-source-maps server.js --verbose"))
    spec = env_file["spec"]
    assert spec.build.recipe == "platform" and spec.build.entrypoint == "node --enable-source-maps server.js --verbose"
    assert any("--env-file=.env" in note for note in env_file["notes"]) and any("NODE_ENV=production" in note for note in env_file["notes"])
    main = read(repo(start=None, extra={"main": "./app.js"}, files={"app.js": SERVER, "server.js": None}))
    assert main["spec"].build.entrypoint == "node app.js" and choice(main, "Start command")["from"] == "package.json (main)"
    fallback = read(repo(start=None, files={"index.js": SERVER, "server.js": None}))
    assert fallback["spec"].build.entrypoint == "node index.js"
    nothing = read(repo(start=None, files={"server.js": None, "lib/util.js": "module.exports = 1;\n"}))
    assert refused(nothing, "says nothing that starts the service")
    prestart = read(repo(extra={"scripts": {"start": "node server.js", "prestart": "node migrate.js"}}))
    assert any("scripts.prestart" in note for note in prestart["notes"])


def test_a_service_that_needs_a_build_is_refused_until_the_later_item():
    built = read(repo(start="node dist/index.js", extra={"scripts": {"start": "node dist/index.js", "build": "tsc"}}))
    assert refused(built, "starts dist/index.js, which is not in the repository: the service needs a build first")
    assert refused(read(repo(deps={"next": "^15.0.0", "express": "^4"})), "uses Next.js, which needs a build")
    assert refused(read(repo(start="node src/index.ts", files={"src/index.ts": SERVER, ".nvmrc": "22\n"})), "Node.js 22 cannot run without a build")
    typescript = read(repo(start="node src/index.ts", files={"src/index.ts": SERVER, ".nvmrc": "24\n"}))
    assert not typescript["refusals"] and any("type stripping" in note for note in typescript["notes"])
    # A build script for something else (Sophia's builds its browser extension) is noted, not refused.
    other = read(repo(extra={"scripts": {"start": "node server.js", "build": "node tools/build-extension.mjs"}}))
    assert not other["refusals"] and any("build script" in note and "is not run" in note for note in other["notes"])
    # The repository's own Dockerfile builds whatever it needs.
    own = read(repo(deps={"next": "^15.0.0"}, files={"Dockerfile": "FROM node:24-slim\nUSER 65532\nCMD [\"node\", \"server.js\"]\n"}))
    assert not own["refusals"] and own["spec"].build.recipe == "repository"


def test_a_static_site_is_refused_and_a_folder_with_neither_runtime_too():
    site = read(repo(start=None, deps={"react": "^19.0.0", "react-scripts": "^5.0.1"}, files={"server.js": None, "src/App.js": "export default 1;\n"}))
    assert refused(site, "front-end site built with react-scripts") and site["refusals"][0]["fix"].startswith("the platform serves web services")
    empty = read({"README.md": "# notes\n", "docs/guide.md": "x"})
    assert empty["refusals"] == [{"file": "the repository root", "problem": "no Python or Node.js code or manifest",
                                  "fix": "the platform deploys Python and Node.js web services; give the folder that holds one"}]
    loose = read({"server.js": SERVER})
    assert refused(loose, "has Node.js code but no package.json")


def test_a_procfile_goes_to_the_buildpacks_for_reviewed_code_only():
    unknown = read(repo(start=None, files={"Procfile": "web: node server.js\n"}))
    assert unknown["spec"].build.recipe == "platform" and unknown["spec"].build.entrypoint == "node server.js"
    assert "build reviewed code only" in next(item["why"] for item in unknown["ignored"] if item["file"] == "Procfile")
    reviewed = read(repo(start=None, files={"Procfile": "web: node server.js\n", "platform.yaml": "name: svc\nruntime: node-http\nnode: '24'\ntrust: reviewed\n"}))
    assert reviewed["spec"].build.recipe == "repository" and "Google's buildpacks" in choice(reviewed, "Build")["value"]
    assert refused(read(repo(start=None, files={"Procfile": "web: npm start\n"})), "not node itself")


def test_the_repositorys_own_dockerfile_meets_the_policy_and_its_node_notes():
    dockerfile = "FROM node:24-alpine\nWORKDIR /app\nCOPY . .\nRUN npm ci --omit=dev\nUSER node\nCMD [\"npm\", \"start\"]\n"
    packages = {"express": {}, "sharp": {"hasInstallScript": True}}
    result = read(repo(deps={"express": "^4", "sharp": "^0.34"}, packages=packages, files={"Dockerfile": dockerfile}))
    assert not result["refusals"] and result["spec"].build.recipe == "repository" and result["spec"].node == "24"
    assert any("starts through a package manager" in note for note in result["notes"])
    assert any("builds on Alpine (musl)" in note for note in result["notes"])


# ---- 18.2 port, host and health (D46)
def test_a_server_must_read_port_and_listen_on_every_interface():
    literal = read(repo(files={"server.js": "const http = require('http');\nhttp.createServer(() => {}).listen(3000);\n"}))
    assert refused(literal, "listens on port 3000 and never reads PORT")
    loopback = read(repo(files={"server.js": "require('http').createServer().listen(process.env.PORT, '127.0.0.1');\n"}))
    assert refused(loopback, "listens on 127.0.0.1, which Cloud Run cannot reach")
    fastify = read(repo(deps={"fastify": "^5.0.0"}, files={"server.js": "const app = require('fastify')();\napp.listen({ port: process.env.PORT || 3000 });\n"}))
    assert refused(fastify, "starts Fastify without host: '0.0.0.0'")
    good = read(repo(deps={"fastify": "^5.0.0"}, files={"server.js": "const app = require('fastify')();\napp.listen({ port: process.env.PORT || 3000, host: '0.0.0.0' });\n"}))
    assert not good["refusals"] and good["spec"].port == 3000
    destructured = read(repo(files={"server.js": "const { PORT = 8081 } = process.env;\nrequire('http').createServer().listen(PORT);\n"}))
    assert destructured["spec"].port == 8081 and not destructured["refusals"]
    assert read(repo())["spec"].port == 3000  # the default in process.env.PORT || 3000; Cloud Run passes it as PORT


def test_a_host_read_from_a_variable_with_a_loopback_default_gets_0_0_0_0():
    # Sophia's shape: the entry imports a config module that reads an alias of process.env and a helper with a default.
    config = ("const env = process.env;\nconst int = (name, fallback) => Number(env[name] || fallback);\n"
              "export const config = {\n  PORT: int('PORT', 8787),\n  HOST: env.HOST || '127.0.0.1',\n  MODEL: env.ANTHROPIC_MODEL || 'x',\n};\n")
    entry = ("import { createServer } from 'node:http';\nimport { config } from './config.mjs';\n// Sophia's server: it answers the chat's API\n"
             "const server = createServer((req, res) => {\n  const url = new URL(req.url, 'http://x');\n  if (url.pathname === '/api/health') { res.end('ok'); }\n});\n"
             "server.listen(config.PORT, config.HOST, () => {\n  console.log(`up ${config.HOST === '127.0.0.1' ? `local` : 'x'}`);\n});\n")
    tool = "import { createServer } from 'node:http';\ncreateServer().listen(9999, '127.0.0.1');\nconst x = { path: '/health' };\n"
    files = repo(start="node --env-file-if-exists=.env server/index.mjs", deps={}, extra={"type": "module"},
                 files={"server.js": None, "server/index.mjs": entry, "server/config.mjs": config, "tools/receiver.mjs": tool})
    result = read(files)
    assert not result["refusals"], result["refusals"]
    spec = result["spec"]
    assert spec.port == 8787 and spec.env == {"HOST": "0.0.0.0"} and spec.health_path == "/api/health"  # noqa: S104 - never the tool's server
    assert choice(result, "Host")["value"].startswith("0.0.0.0 through HOST (the code's default, 127.0.0.1")
    assert any("ANTHROPIC_MODEL" in note and "HOST" in note for note in result["notes"] if note.startswith("The code reads these settings"))
    assert any("No SIGTERM handler" in note for note in result["notes"])


@pytest.mark.parametrize(("code", "expected", "note"), [
    ("app.get('/health', (q, r) => r.send('ok'));\n", "/health", None),
    ("router.get('/api/v1/healthcheck', h);\n", "/api/v1/healthcheck", None),
    ("fastify.route({ method: 'GET', url: '/health', handler });\n", "/health", None),
    ("server.route({ method: 'GET', path: '/health', handler });\n", "/health", None),
    ("if (req.url === '/health') { res.end('ok'); }\n", "/health", None),
    ("switch (req.url) {\n  case '/health': res.end('ok');\n}\n", "/health", None),
    ("app.get('/healthz', h);\n", None, "Cloud Run keeps for itself"),
    ("app.get('/ready', h);\napp.get('/ping', h);\n", None, "set health_path to it in the review"),
])
def test_the_health_route_ladder(code, expected, note):
    server = "const express = require('express');\nconst app = express();\n" + code + "app.listen(process.env.PORT || 8080);\n"
    result = read(repo(files={"server.js": server}))
    assert result["spec"].health_path == expected
    if note:
        assert any(note in item for item in result["notes"]) and "TCP startup probe" in choice(result, "Health")["value"]


# ---- 18.2 settings, tests, workspaces and secrets
def test_settings_are_names_only_and_secrets_are_called_secrets():
    code = "const { STRIPE_KEY, MODE = 'x' } = process.env;\nconst db = process.env['DATABASE_URL'];\n" + SERVER
    result = read(repo(files={"server.js": code, ".env.example": "OPENAI_API_KEY=\n"}))
    note = next(note for note in result["notes"] if note.startswith("The code reads these settings"))
    assert "DATABASE_URL, MODE, OPENAI_API_KEY, STRIPE_KEY" in note and "These look like secrets: DATABASE_URL, OPENAI_API_KEY, STRIPE_KEY" in note
    assert "PORT" not in note.split(".")[0]


def test_the_tests_ladder_says_none_plainly():
    script = read(repo(extra={"scripts": {"start": "node server.js", "test": "node --test"}}))
    assert script["spec"].tests == "required" and choice(script, "Tests")["value"] == "node --test, run with npm test before every build"
    placeholder = read(repo(extra={"scripts": {"start": "node server.js", "test": "echo \"Error: no test specified\" && exit 1"}},
                            files={"test/health.test.js": "x", "lib/a.test.mjs": "x"}))
    assert placeholder["spec"].tests == "required" and choice(placeholder, "Tests")["value"].startswith("2 test files node --test finds")
    jest = read(repo(files={"jest.config.js": "module.exports = {};\n"}))
    assert jest["spec"].tests == "none" and any("A Jest configuration is there but package.json has no test script" in note for note in jest["notes"])
    none = read(repo())
    assert none["spec"].tests == "none" and choice(none, "Tests")["value"].startswith("none found")
    assert node.tests_of({}, ["src/a.test.ts"], "24")[0] == "node --test" and node.tests_of({}, ["src/a.test.ts"], "22")[0] is None


def test_workspaces_tokens_and_committed_installs():
    workspace = read(repo(extra={"workspaces": ["packages/*"]}, files={"packages/api/package.json": "{}", "packages/web/package.json": "{}"}))
    assert refused(workspace, "is a workspace of several packages (packages/api, packages/web)") and "path packages/api" in workspace["refusals"][0]["fix"]
    assert refused(read(repo(files={"turbo.json": "{}"})), "is a workspace")
    token = read(repo(files={".npmrc": "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnop\n"}))
    assert refused(token, "holds a literal registry token") and "rotate" in token["refusals"][0]["fix"]
    variable = read(repo(files={".npmrc": "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n"}))
    assert not variable["refusals"] and any(".npmrc is not read" in note for note in variable["notes"])
    assert refused(read(repo(files={".yarnrc.yml": "npmAuthToken: abc123\n"})), "literal registry token")
    committed = read(repo(files={"node_modules/express/index.js": "x", "dist/bundle.js": "x"}))
    assert {item["file"] for item in committed["ignored"]} >= {"node_modules", "dist"} and not committed["refusals"]


def test_a_package_in_a_folder_of_a_workspace_is_refused_until_workspaces_are_supported():
    files = {"package.json": json.dumps({"name": "mono", "workspaces": ["apps/*"]}), "apps/api/server.js": SERVER}
    package = {"name": "api", "version": "1.0.0", "scripts": {"start": "node server.js"}, "dependencies": {"express": "^4"}}
    files.update({"apps/api/package.json": json.dumps(package), "apps/api/package-lock.json": lockfile(package)})
    assert refused(read(files, "apps/api"), "is a package of the repository's workspace")


def test_needs_sqlite_egress_and_a_python_repository_with_front_end_tooling():
    code = "import { DatabaseSync } from 'node:sqlite';\n" + SERVER
    deps = {"express": "^4", "pg": "^8", "@anthropic-ai/sdk": "^0.128.0", "ioredis": "^5"}
    result = read(repo(deps=deps, files={"server.js": code}))
    notes = " ".join(result["notes"])
    assert "SQLite database on local disk" in notes and "database: postgres" in notes and "add egress: internet" in notes
    assert "redis appears in the dependencies" in notes and "@anthropic-ai/sdk" in notes
    python = read({"requirements.txt": "fastapi==0.115.0\nuvicorn==0.30.0\n", "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                   "package.json": json.dumps({"name": "assets", "devDependencies": {"tailwindcss": "^4"}})})
    assert python["runtime"] == "python-http" and python["spec"].runtime == "python-http"
    assert any("package.json is there too" in note for note in python["notes"])


def test_the_name_comes_from_package_json_and_the_entry_is_read_in_more_passes():
    assert read(repo(extra={"name": "@lab/notes-api"}))["service"] == "notes-api"
    files = repo(start="node src/main.mjs", files={"src/main.mjs": "import { port } from './settings.mjs';\nimport './log.js';\n",
                                                   "src/settings.mjs": "export const port = 1;\n", "src/log.js": "x", "server.js": None})
    paths = sorted(files) + ["src"]
    texts = {"package.json": files["package.json"]}
    assert reader.more_files_to_read(paths, texts) == ["src/main.mjs"]
    texts["src/main.mjs"] = files["src/main.mjs"]
    assert reader.more_files_to_read(paths, texts) == ["src/settings.mjs", "src/log.js"]
    assert "package-lock.json" in reader.files_to_read(paths) and reader.large_files("svc/package-lock.json")


# ---- 18.3 the lock the platform makes (D42, D27)
def fake_npm(calls, output=None):
    package = {"name": "svc", "version": "1.0.0", "dependencies": {"express": "^4.21.2"}}

    def run(args, stdin, timeout=900):
        calls.append((args, stdin))
        return output if output is not None else lockfile(package)
    return run


def test_the_lock_is_made_by_npm_11_10_or_later_with_every_script_off():
    calls = []
    package = json.dumps({"name": "svc", "version": "1.0.0", "dependencies": {"express": "^4.21.2"}})
    for line, image in (("22", "node:22-slim@"), ("24", "node:24-slim@"), ("26", "node:26-slim@")):
        made = scaffold.node_lock(package, line, fake_npm(calls))
        assert made["error"] is None and json.loads(made["text"])["lockfileVersion"] == 3
        args, stdin = calls[-1]
        assert args[:4] == ["docker", "run", "--rm", "-i"] and "65532:65532" in args and any(arg.startswith(image) for arg in args)
        assert stdin == package and "express" not in " ".join(args)  # package.json travels on stdin, never on a command line
        script = args[-1]
        assert "npm install --package-lock-only --ignore-scripts --min-release-age=3" in script and "--no-audit" in script
        assert 'if [ "$major" -lt 11 ] || { [ "$major" -eq 11 ] && [ "$minor" -lt 10 ]; }' in script  # npm 10 would ignore the hold
        # Node 22's npm 10.9.9 has no min-release-age: its container installs the pinned npm 11 first and checks its integrity hash.
        version, integrity = scaffold.LOCK_NPM["22"]
        assert (f"npm@{version}" in script and integrity in script) == (line == "22")
    assert scaffold.node_lock_command("api") == "cd api && npm install --package-lock-only --ignore-scripts --min-release-age=3"
    assert scaffold.node_lock(package, "24", fake_npm([], "not json"))["error"].startswith("the container did not return a usable package-lock.json")
    bad = json.loads(lockfile({"name": "svc", "dependencies": {"x": "1"}}))
    bad["packages"]["node_modules/x"]["resolved"] = "https://example.com/x.tgz"
    assert "outside the npm registry" in scaffold.node_lock(package, "24", fake_npm([], json.dumps(bad)))["error"]
    assert "not valid JSON" in scaffold.node_lock("{", "24", fake_npm([]))["error"]

    def missing(args, stdin):
        raise FileNotFoundError("docker")

    def failing(args, stdin):
        raise subprocess.CalledProcessError(3, args, stderr="npm 10.9.9 cannot hold back new releases: min-release-age needs npm 11.10 or later\n")

    assert scaffold.node_lock(package, "24", missing)["error"] == "Docker is not installed on this machine"
    assert "min-release-age needs npm 11.10" in scaffold.node_lock(package, "24", failing)["error"]


def test_importing_a_node_service_proposes_its_contract_and_the_platforms_lock(tmp_path):
    c, brian, gh, calls = controller_with(tmp_path)
    files = repo(lock=False)
    gh.repos["lab/notes"] = {"files": files, "default_branch": "main"}
    c.contracts.runner = fake_npm(calls)
    bodies, opener = [], gh.open_platform_pull_request
    gh.open_platform_pull_request = lambda *args: bodies.append(args[5]) or opener(*args)
    view = c.import_contract(brian, "lab/notes")
    assert not view["refusals"], view["refusals"]
    pr = gh.prs[(PLATFORM, view["pull_request"]["number"])]
    assert set(pr["files"]) == {"apps/svc/platform.yaml", node_lock_path("svc")}
    spec, _ = parse_contract(pr["files"]["apps/svc/platform.yaml"])
    assert spec.runtime == "node-http" and spec.node == default_node() and spec.build.entrypoint == "node server.js"
    assert "npm 11.10 or later" in bodies[0] and "every install script off" in bodies[0] and " uv" not in bodies[0]
    assert "| Node.js | " + default_node() + " |" in bodies[0] and "| Start command | node server.js |" in bodies[0]
    assert any("min-release-age=3" in args[-1] for args, _ in calls)
    # A repository with its own lockfile adds only the contract.
    gh.repos["lab/locked"] = {"files": {**repo(), "package.json": repo(extra={"name": "locked"})["package.json"],
                                        "package-lock.json": lockfile(json.loads(repo(extra={"name": "locked"})["package.json"]))}, "default_branch": "main"}
    locked = c.import_contract(brian, "lab/locked")
    assert set(gh.prs[(PLATFORM, locked["pull_request"]["number"])]["files"]) == {"apps/locked/platform.yaml"}


def test_lock_files_raise_with_the_command_to_run_by_hand():
    contracts = Contracts.__new__(Contracts)
    result = {"spec": AppSpec(name="svc", runtime="node-http", node="24", reviewed=True), "lock": {"action": "lock", "package": "{}"}, "tests": None}

    def failing(args, stdin):
        raise subprocess.CalledProcessError(1, args, stderr="npm error 404 Not Found - GET https://registry.npmjs.org/nope\n")

    with pytest.raises(ValueError, match="Run `npm install --package-lock-only --ignore-scripts --min-release-age=3` with npm 11.10"):
        Contracts.lock_files(contracts, {**result, "lock": {"action": "lock", "package": "{\"name\": \"svc\"}"}}, failing)
    assert Contracts.lock_files(contracts, {**result, "lock": {"action": "use"}}, failing) == {}


# ---- 18.4 the pipeline (D43, D44)
def build_steps(text=None):
    workflow = yaml.safe_load(lay_out(text or Path("templates/deploy-gcp.yml").read_text()))
    return workflow, workflow["jobs"]["build"]["steps"]


def test_the_node_steps_are_pinned_and_run_for_node_services_only():
    for text in (Path("templates/deploy-gcp.yml").read_text(), Path(".github/workflows/deploy.yml").read_text()):
        workflow, steps = build_steps(text) if "__LAB_" in text else (yaml.safe_load(text), yaml.safe_load(text)["jobs"]["build"]["steps"])
        names = [step.get("name") or step.get("id") for step in steps]
        setup = next(step for step in steps if step.get("id") == "app-node")
        assert setup["uses"] == "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0".split(" # ")[0]
        assert setup["with"] == {"node-version": "${{ steps.policy.outputs.node }}", "package-manager-cache": False}
        assert setup["if"] == "steps.policy.outputs.runtime == 'node-http'"
        for name in ("Run unit tests", "Audit Python dependencies"):
            assert "steps.policy.outputs.runtime == 'python-http'" in next(step for step in steps if step.get("name") == name)["if"]
        for name in ("Audit Node.js dependencies", "Install Node.js dependencies with their scripts off", "Run unit tests (Node.js)"):
            assert "steps.policy.outputs.runtime == 'node-http'" in next(step for step in steps if step.get("name") == name)["if"]
        # The audit reads the lockfile before any package is installed or run; the build comes last.
        order = [names.index(name) for name in ("Scan the checkout for secrets", "app-node", "Audit Node.js dependencies",
                                                "Install Node.js dependencies with their scripts off", "Run unit tests (Node.js)", "Build without cloud credentials")]
        assert order == sorted(order)
        audit = next(step for step in steps if step.get("name") == "Audit Node.js dependencies")["run"]
        url, checksum = pins.mentions("osv-scanner", "templates/deploy-gcp.yml")  # by version and SHA-256, as labdeploy/pins.py reads them
        assert url in audit and audit.index(url) < audit.index(checksum + " | sha256sum --check --strict") < audit.index('chmod +x "$RUNNER_TEMP/osv-scanner"')
        assert '--config "$RUNNER_TEMP/osv-scanner.toml"' in audit and ': > "$RUNNER_TEMP/osv-scanner.toml"' in audit  # nothing of the repository's
        assert "python -m labdeploy.scan osv" in audit and "bundle/dependency-audit.json" in audit
        install = next(step for step in steps if step.get("name") == "Install Node.js dependencies with their scripts off")["run"]
        assert 'npm ci --ignore-scripts "${NPM_FLAGS[@]}"' in install and "--registry=https://registry.npmjs.org/" in install
        assert "npm audit signatures" in install and "a note, not a gate" in install
        tests = next(step for step in steps if step.get("name") == "Run unit tests (Node.js)")
        assert tests["env"]["SCRIPTS"] == "${{ steps.policy.outputs.scripts }}" and 'npm rebuild --ignore-scripts=false "${PACKAGES[@]}"' in tests["run"]
        assert "npm test" in tests["run"] and "--test-reporter=junit" in tests["run"] and "found no test files" in tests["run"]
        for forbidden in ("cache: npm", "cache: 'npm'", "npm install --", "npm start"):
            assert forbidden not in text, forbidden
    central = Path(".github/workflows/deploy.yml").read_text()
    assert central_deploy(Path("templates/deploy-gcp.yml").read_text()) == central
    assert yaml.safe_load(central)["env"]["PLATFORM_NODE_LOCK"] == "platform/apps/${{ inputs.application }}/package-lock.json"
    assert yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))["env"]["PLATFORM_NODE_LOCK"] == ""


def test_the_policy_step_reads_the_runtime_its_line_and_its_scripts(tmp_path):
    _, steps = build_steps()
    run = next(step for step in steps if step.get("id") == "policy")["run"]
    code = run.split("python -c '", 1)[1].split("' \"$1\"", 1)[0]
    contract = tmp_path / "platform.yaml"
    contract.write_text("name: svc\nruntime: node-http\nnode: '26'\nreviewed: true\nbuild:\n  scripts: [sharp@0.34.4, '@img/x@1.0.0']\n")

    def ask(key):  # the step's own read_spec, run on this contract
        env = {"SPEC_SOURCE": str(contract), "TRUST_DEFAULT": "unknown", "PYTHONPATH": str(Path.cwd())}
        return subprocess.run([sys.executable, "-c", code, key], capture_output=True, text=True, env=env, check=True).stdout.strip()

    assert (ask("runtime"), ask("node"), ask("scripts"), ask("python")) == ("node-http", "26", "sharp@0.34.4 @img/x@1.0.0", "3.12")
    assert '[[ "$NODE" =~ ^(22|24|26)$ ]]' in run and 'PACKAGE=' in run and "build.scripts holds an entry the build cannot pass on" in run


def test_the_build_installs_the_platforms_lock_and_keeps_buildpacks_for_reviewed_code():
    _, steps = build_steps()
    build = next(step for step in steps if step.get("name") == "Build without cloud credentials")
    assert build["env"]["RUNTIME"] == "${{ steps.policy.outputs.runtime }}" and build["env"]["TRUST"] == "${{ steps.policy.outputs.trust }}"
    run = build["run"]
    assert 'cp "$PLATFORM_NODE_LOCK" "$LOCK_FILE"' in run and 'LOCK_FILE="$APP_DIR/npm-shrinkwrap.json"' in run
    assert 'if [ "$RUNTIME" = "node-http" ] && [ "$TRUST" != "reviewed" ]' in run and "build reviewed code only" in run
    assert '--env "GOOGLE_NODEJS_VERSION=$NODE_VERSION.x.x"' in run and 'BUILDS_ON="node-$NODE_VERSION"' in run
    assert 'python3 - "$DOCKERFILE_USED" "$LOCK_FILE" "$LOCK_SOURCE"' in run  # the provenance records the lockfile that was installed
    policy = next(step for step in steps if step.get("name") == "Check the Dockerfile against the platform's policy")["run"]
    assert '"${LOCKFILE[@]}"' in policy and "package-lock.json" in policy


def osv_report(*findings):
    packages = []
    for name, version, ident, score, fixed, aliases in findings:
        advisory = {"id": ident, "aliases": aliases, "affected": [{"package": {"name": name, "ecosystem": "npm"},
                                                                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}] + ([{"fixed": fixed}] if fixed else [])}]}]}
        packages.append({"package": {"name": name, "version": version, "ecosystem": "npm"}, "vulnerabilities": [advisory],
                         "groups": [{"ids": [ident], "aliases": aliases, "max_severity": score}]})
    return {"results": [{"source": {"path": "/tmp/osv/package-lock.json", "type": "lockfile"}, "packages": packages}]}


LOCK = lockfile({"name": "svc", "version": "1.0.0", "dependencies": {"express": "^4", "left-pad": "^1"}})


def test_the_dependency_audit_stops_malicious_and_fixable_serious_findings_for_every_trust_level():
    clean = scan.osv_summary({"results": []}, 0, LOCK)
    assert clean["status"] == "finished" and clean["packages"] == 2 and scan.osv_verdict(clean) is None
    malicious = scan.osv_summary(osv_report(("left-pad", "1.2.3", "MAL-2026-1234", "", None, [])), 1, LOCK)
    assert malicious["malicious"] == ["MAL-2026-1234 (left-pad@1.2.3)"] and "1 known malicious package" in scan.osv_verdict(malicious)
    high = scan.osv_summary(osv_report(("express", "1.2.3", "GHSA-aaaa-bbbb-cccc", "7.5", "4.21.2", ["CVE-2026-1"])), 1, LOCK)
    assert high["by_severity"]["HIGH"] == 1 and "1 critical or high finding with a fix available: express@1.2.3 (GHSA-aaaa-bbbb-cccc, fixed in 4.21.2)" in scan.osv_verdict(high)
    unfixed = scan.osv_summary(osv_report(("express", "1.2.3", "GHSA-x", "9.8", None, [])), 1, LOCK)
    assert unfixed["by_severity"]["CRITICAL"] == 1 and scan.osv_verdict(unfixed) is None  # listed, not blocking: there is nothing to move to
    medium = scan.osv_summary(osv_report(("express", "1.2.3", "GHSA-y", "5.3", "2.0.0", [])), 1, LOCK)
    assert medium["by_severity"]["MEDIUM"] == 1 and scan.osv_verdict(medium) is None
    word = osv_report(("express", "1.2.3", "GHSA-z", "", "2.0.0", []))
    word["results"][0]["packages"][0]["vulnerabilities"][0]["database_specific"] = {"severity": "HIGH"}
    assert scan.osv_summary(word, 1, LOCK)["by_severity"]["HIGH"] == 1


def test_an_audit_that_did_not_finish_or_a_lockfile_from_elsewhere_stops_the_release():
    assert f"did not finish ({scan.OSV_SCANNER} exited 127)" in scan.osv_verdict(scan.osv_summary(None, 127, LOCK))
    assert "did not finish" in scan.osv_verdict(scan.osv_summary(None, 128, LOCK))  # no packages found, but the lockfile has some
    empty = lockfile({"name": "svc", "version": "1.0.0"}, packages={})
    assert scan.osv_verdict(scan.osv_summary(None, 128, empty)) is None  # a service with no dependencies at all
    assert "could not be read" in scan.osv_verdict(scan.osv_summary({"results": []}, 0, "{"))
    data = json.loads(LOCK)
    data["packages"]["node_modules/left-pad"]["resolved"] = "git+https://github.com/x/left-pad.git"
    assert "comes from a git repository" in scan.osv_verdict(scan.osv_summary({"results": []}, 0, json.dumps(data)))


def test_the_audit_command_writes_its_json_and_the_receipt_counts_it(tmp_path, capsys):
    report, lock, out = tmp_path / "osv.json", tmp_path / "package-lock.json", tmp_path / "bundle" / "dependency-audit.json"
    out.parent.mkdir()
    report.write_text(json.dumps(osv_report(("express", "1.2.3", "GHSA-q", "6.1", "2.0.0", []))))
    lock.write_text(LOCK)
    assert scan.main(["osv", str(report), "--exit-code", "1", "--lockfile", str(lock), "--json", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["blocked"] is False and written["by_severity"]["MEDIUM"] == 1 and written["tool"] == scan.OSV_SCANNER
    assert "medium: express@1.2.3 GHSA-q, fixed in 2.0.0" in capsys.readouterr().out
    assert build_checks(str(out.parent))["dependency_audit"] == {"tool": scan.OSV_SCANNER, "status": "finished", "packages": 2, "critical": 0,
                                                                  "high": 0, "medium": 1, "low": 0, "malicious": 0}
    report.write_text(json.dumps(osv_report(("left-pad", "1.2.3", "MAL-2026-9", "", None, []))))
    assert scan.main(["osv", str(report), "--exit-code", "1", "--lockfile", str(lock), "--json", str(out)]) == 1
    assert "::error title=Dependency audit::1 known malicious package" in capsys.readouterr().out


def test_a_node_release_proves_its_tests_and_its_audit_in_the_node_steps():
    def steps(**outcomes):
        return [{"name": name, "status": "completed", "conclusion": conclusion} for name, conclusion in outcomes.items()]
    workflow = {"status": "completed", "conclusion": "success", "jobs": [
        {"name": "build", "steps": steps(**{"Run unit tests": "skipped", "Audit Python dependencies": "skipped", "Audit Node.js dependencies": "success",
                                            "Run unit tests (Node.js)": "success"})},
        {"name": "deploy", "steps": steps(**{"Validate artifact and deployment configuration": "success", "Deploy, probe, promote; restore traffic on failure": "success"})}]}
    result = summarize(workflow, {})
    assert result["state"] == "succeeded" and result["checks"]["tests"] == "passed" and result["checks"]["dependency_scan"] == "passed"
    workflow["jobs"][0]["steps"][2]["conclusion"] = "failure"
    workflow["conclusion"] = "failure"
    failed = summarize(workflow, {})
    assert failed["checks"]["dependency_scan"] == "failed" and failed["failed_step"]["step"] == "Audit Node.js dependencies"
    text = chat.release({"application": "notes", "state": "failed", "failed_step": failed["failed_step"], "checks": failed["checks"]})
    assert "known malicious package" in text and "commits the lockfile" in text


def test_the_chat_names_the_runtime_and_the_audit():
    planned = chat.release({"application": "notes", "state": "planned", "spec": {"runtime": "node-http", "node": "24"}})
    assert "- A Node.js 24 web service" in planned
    assert "- A Python 3.12 web service" in chat.release({"application": "demo", "state": "planned", "spec": {"runtime": "python-http", "python": "3.12"}})
    gates = chat.gates_line({"dependency_audit": {"packages": 206, "critical": 0, "high": 1}})
    assert gates == "Before the build: dependency audit passed (206 packages, 1 critical or high finding without a fix yet)"
    lines = chat.choice_lines([{"what": "Build", "value": "the platform's own Node.js image", "from": "no Dockerfile in the repository root"},
                               {"what": "Node.js", "value": "24", "from": ".nvmrc"}, {"what": "Host", "value": "0.0.0.0 through HOST", "from": "server.js"}])
    assert lines[1] == "Node.js: 24 (from `.nvmrc`)" and lines[2].startswith("Host: 0.0.0.0")
    assert "Node.js 24" in chat.detected_words({"node": "24", "port": 8080})
    assert chat.step_words("Audit Node.js dependencies") == "checking the libraries for known vulnerabilities and malicious packages"


# ---- 18.5 the image (D45)
def test_every_supported_line_has_a_well_formed_index_digest():
    assert tuple(scaffold.NODE_IMAGES) == node.NODES == ("22", "24", "26")
    assert all(digest.startswith("sha256:") and len(digest) == 71 for digest in scaffold.NODE_IMAGES.values())
    # Resolved no earlier than the Node.js lines arrived, and never in the future (the weekly pin refresh dates each change).
    assert len(set(scaffold.NODE_IMAGES.values())) == 3 and "2026-10-19" <= scaffold.NODE_IMAGES_RESOLVED <= datetime.datetime.now(datetime.UTC).date().isoformat()
    assert set(scaffold.LOCK_NPM) == {"22"} and scaffold.LOCK_NPM["22"][1].startswith("sha512-")
    with pytest.raises(ValueError, match="not supported"):
        scaffold.node_base_image("20")


def node_spec(**build):
    return AppSpec(name="svc", runtime="node-http", node="24", port=8787, build={"recipe": "platform", "entrypoint": "node server/index.mjs", **build})


def test_the_platforms_node_dockerfile():
    text = scaffold.platform_dockerfile(node_spec(scripts=["sharp@0.34.4"]), True, "package-lock.json")
    assert text.startswith("# Made by Fast Deploy at build time") and f"FROM {scaffold.node_base_image('24')}\n" in text
    assert "ENV NODE_ENV=production HOME=/tmp\n" in text and "COPY package.json package-lock.json ./\n" in text
    assert "RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm rebuild sharp@0.34.4 && npm cache clean --force\n" in text
    assert text.index("RUN npm ci") < text.index("COPY . .") < text.index("USER 65532:65532") and "EXPOSE 8787\n" in text
    assert text.rstrip().endswith('CMD ["node", "server/index.mjs"]') and "npm start" not in text.split("never npm start")[0]
    plain = scaffold.platform_dockerfile(node_spec(), True, "npm-shrinkwrap.json")
    assert "COPY package.json npm-shrinkwrap.json ./" in plain and "npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean" in plain
    assert not [finding for finding in dockerpolicy.check(plain, "unknown")]  # the platform's own image meets its own policy


@pytest.mark.skipif(hadolint_command() is None, reason="the pinned hadolint is not available")
def test_the_node_dockerfiles_pass_hadolint():
    for text in (scaffold.platform_dockerfile(node_spec(scripts=["sharp@0.34.4"]), True), scaffold.node_dockerfile(
            {"node": "22", "port": 8080, "command": ["node", "server.js"], "lockfile": "package-lock.json", "scripts": []})):
        run = subprocess.run(hadolint_command() + ["-"], input=text, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0 and not run.stdout.strip(), run.stdout + run.stderr


def test_render_build_copies_a_node_service_through_the_deny_list(tmp_path):
    app = tmp_path / "app"
    for relative, text in {"package.json": "{}", "package-lock.json": "{}", "server/index.mjs": "x", "node_modules/x/index.js": "x", ".npmrc": "t",
                           ".env": "SECRET=1", "test/a.test.mjs": "x", "public/logo.svg": "x"}.items():
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text(text)
    copied = scaffold.render_build(app, tmp_path / "out", node_spec())
    assert sorted(copied) == ["package-lock.json", "package.json", "public/logo.svg", "server/index.mjs"]
    assert (tmp_path / "out" / "Dockerfile").read_text().startswith("# Made by Fast Deploy")
    (app / "package-lock.json").unlink()
    with pytest.raises(ValueError, match="needs package.json and a lockfile"):
        scaffold.render_build(app, tmp_path / "again", node_spec())
    assert "**/node_modules" in scaffold.DENY_DOCKERIGNORE and "**/.npmrc" in scaffold.DENY_DOCKERIGNORE
    assert '"node_modules"' in scaffold.descriptor() and '".npmrc"' in scaffold.descriptor()


def test_the_dockerfile_policys_two_node_notes_never_refuse(tmp_path, capsys):
    started = dockerpolicy.check("FROM node:24-slim\nUSER 65532\nCMD npm start\n", "unknown")
    assert [(item["rule"], item["refuses"]) for item in started] == [("start-through-npm", False)]
    assert dockerpolicy.check("FROM node:24-slim\nUSER 65532\nCMD [\"sh\", \"-c\", \"exec yarn start\"]\n")[0]["rule"] == "start-through-npm"
    alpine = "FROM node:24-alpine AS run\nUSER 65532\nCMD [\"node\", \"server.js\"]\n"
    assert [item["rule"] for item in dockerpolicy.check(alpine, "unknown", native=True)] == ["alpine-native-code"]
    assert not dockerpolicy.check(alpine, "unknown", native=False) and not dockerpolicy.check(alpine, "unknown")
    dockerfile, lock = tmp_path / "Dockerfile", tmp_path / "package-lock.json"
    dockerfile.write_text(alpine)
    lock.write_text(lockfile({"name": "svc", "dependencies": {"sharp": "1"}}, packages={"sharp": {"libc": ["glibc"]}}))
    assert dockerpolicy.main([str(dockerfile), "--trust", "unknown", "--lockfile", str(lock)]) == 0
    out = capsys.readouterr().out
    assert "alpine-native-code" in out and out.rstrip().endswith("Dockerfile policy (unknown): 1 reported")


# ---- 18.6 the drafter
def drafted(files, **kwargs):
    paths = sorted(files) + sorted({p.rsplit("/", 1)[0] for p in files if "/" in p})
    return scaffold.draft_files(paths, files, **kwargs)


def test_a_bare_node_service_gets_every_file_the_standard_asks_for():
    result = drafted(repo(lock=False, extra={"name": "notes"}), lock_text=repo()["package-lock.json"])
    assert sorted(result["files"]) == [".dockerignore", ".github/dependabot.yml", ".github/workflows/ci.yml", "Dockerfile", "SECURITY.md",
                                       "package-lock.json", "platform.yaml", "test/health.test.mjs"]
    files = {target: item["content"] for target, item in result["files"].items()}
    assert f"FROM {scaffold.node_base_image(default_node())}" in files["Dockerfile"] and 'CMD ["node", "server.js"]' in files["Dockerfile"]
    assert "!package.json\n!package-lock.json\n!server.js" in files[".dockerignore"] and "**/node_modules" in files[".dockerignore"]
    spec = parse_spec(files["platform.yaml"])
    assert (spec.runtime, spec.node, spec.reviewed, spec.tests, spec.build.recipe) == ("node-http", default_node(), False, "required", "repository")
    ci = yaml.safe_load(files[".github/workflows/ci.yml"])
    setup = ci["jobs"]["tests"]["steps"][1]
    assert setup["uses"].startswith("actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1") and setup["with"]["package-manager-cache"] is False
    assert "npm ci --ignore-scripts" in files[".github/workflows/ci.yml"] and "node --test" in files[".github/workflows/ci.yml"]
    dependabot = yaml.safe_load(files[".github/dependabot.yml"])
    npm = next(entry for entry in dependabot["updates"] if entry["package-ecosystem"] == "npm")
    assert npm["cooldown"] == {"default-days": 7} and npm["directory"] == "/"
    assert result["detected"]["node"] == default_node() and "Node.js service started by `node server.js`" in scaffold.pull_request_body(result)
    without = drafted(repo(lock=False))
    assert "package-lock.json" not in without["files"] and any("There is no package-lock.json yet" in line for line in without["review"])


def test_the_drafter_keeps_what_exists_and_says_what_it_cannot_draft():
    files = repo(files={"Dockerfile": "FROM node:24-slim\nUSER 65532\nCMD [\"node\", \"server.js\"]\n", "test/health.test.js": "fetch('/health')\n",
                        ".github/workflows/ci.yml": "jobs:\n  t:\n    steps:\n      - run: npm test\n"})
    result = drafted(files)
    assert "Dockerfile" not in result["files"] and "test/health.test.mjs" not in result["files"] and ".github/workflows/ci.yml" not in result["files"]
    assert "Dockerfile" in result["existing"] and "test/health.test.js" in result["existing"]
    refused_draft = drafted(repo(start="nodemon server.js"))
    assert refused_draft["files"] == {} and "scripts.start" in refused_draft["review"][1]


def test_the_drafted_ci_and_dependabot_pin_what_the_platform_pins():
    template = (scaffold.TEMPLATES / "ci-node.yml").read_text()
    assert "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0" in template
    assert "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0" in Path("templates/deploy-gcp.yml").read_text()
    checkout = Path(".github/workflows/ci.yml").read_text().split("actions/checkout@")[1].split(" ")[0]
    assert f"actions/checkout@{checkout}" in template


@pytest.mark.skipif(zizmor_command() is None, reason="zizmor is not installed (it comes with requirements-dev.lock)")
def test_the_drafted_node_workflows_pass_zizmor(tmp_path):
    files = drafted(repo(extra={"name": "notes"}))["files"]
    inputs = []
    for target, item in files.items():
        if target.startswith(".github/"):
            destination = tmp_path / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(item["content"])
            inputs.append(str(destination))
    inputs += [str(scaffold.TEMPLATES / "ci-node.yml"), str(scaffold.TEMPLATES / "dependabot-node.yml")]
    run = subprocess.run(zizmor_command() + ["--persona", "auditor"] + inputs, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "No findings" in run.stdout + run.stderr, run.stdout + run.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed here")
def test_the_drafted_health_test_runs_against_a_real_server(tmp_path):
    server = ("const http = require('node:http');\nconst server = http.createServer((req, res) => { res.statusCode = req.url === '/api/health' ? 200 : 404; res.end(); });\n"
              "server.listen(process.env.PORT || 8080);\nprocess.on('SIGTERM', () => server.close(() => process.exit(0)));\n")
    result = drafted(repo(deps={}, start="node server.js", files={"server.js": server}))
    test = result["files"]["test/health.test.mjs"]["content"]
    assert "GET /api/health answers 200" in test and '["server.js"]' in test
    (tmp_path / "test").mkdir()
    (tmp_path / "server.js").write_text(server)
    (tmp_path / "test" / "health.test.mjs").write_text(test)
    run = subprocess.run([shutil.which("node"), "--test"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    (tmp_path / "server.js").write_text(server.replace("? 200 : 404", "? 503 : 404"))
    assert subprocess.run([shutil.which("node"), "--test"], cwd=tmp_path, capture_output=True, text=True, timeout=120).returncode != 0


def test_check_repository_asks_a_node_service_for_its_lockfile(tmp_path):
    class Repo:
        files = {**repo(extra={"scripts": {"start": "node server.js", "test": "node --test"}}), "Dockerfile": "FROM node:24-slim\nEXPOSE 8080\n",
                 "platform.yaml": "name: svc\nruntime: node-http\nnode: '24'\nreviewed: true\n"}

        def repository(self, repository):
            return {"repository_id": "1", "owner_id": "2", "default_branch": "main", "full_name": repository}

        def tree(self, repository, ref):
            return sorted(self.files)

        def contents(self, repository, ref, path):
            return {"sha": "s", "text": self.files[path]} if path in self.files else None

    view = contract_check(Repo(), "lab/svc")
    items = {item["item"]: item["ok"] for item in view["checklist"]}
    assert items == {"Dockerfile": True, "platform.yaml": True, "package.json": True, "package-lock.json": True, "platform.yaml is valid": True,
                     "platform.yaml says reviewed: true": True, "tests (a test script, or files node --test finds)": True,
                     "Dockerfile exposes port 8080": True}
    assert view["ready"] and "requirements.lock" not in {item["item"] for item in view["recommended"]}
    del Repo.files["package-lock.json"]
    missing = contract_check(Repo(), "lab/svc")
    lock = next(item for item in missing["checklist"] if item["item"] == "package-lock.json")
    assert not lock["ok"] and lock["platform_writes"] and not missing["ready"]


def test_propose_configuration_locks_a_node_service_on_the_controllers_machine(tmp_path, monkeypatch):
    from test_onboarding import make
    c, brian, _ = make(tmp_path)
    c.github = DraftingGitHub(repo(lock=False, extra={"name": "notes"}))
    made = []
    monkeypatch.setattr(scaffold, "node_lock", lambda text, line, runner=None, path="": made.append(line) or {"text": repo()["package-lock.json"], "command": "npm ...", "error": None})
    view = c.propose_config(brian, repository="lab/notes")
    assert made == [default_node()] and "package-lock.json" in view["files"] and view["detected"]["node"] == default_node()
    opened = c.github.opened[0]
    assert opened["draft"] and json.loads(opened["files"]["package-lock.json"])["lockfileVersion"] == 3


def test_the_analyzer_recognises_a_node_service():
    from labdeploy.analyzer import analyze
    result = analyze({"package.json": repo(extra={"engines": {"node": "^22.13.0"}})["package.json"], "Dockerfile": "EXPOSE 3000\n"}, "notes", ["package.json"])
    assert result["supported_candidate"] and result["runtime"] == "node-http"
    assert result["draft_spec"]["runtime"] == "node-http" and result["draft_spec"]["node"] == "22" and result["draft_spec"]["port"] == 3000
    assert "python" not in result["draft_spec"] and any("No package-lock.json" in line for line in result["evidence"])
    text = chat.analysis(result, "notes")
    assert "a Node.js web service the platform can run" in text and "Node.js 22" in text


def test_compose_hono_and_hapi_say_where_a_node_service_listens():
    compose = ("services:\n  api:\n    build: .\n    command: node server.js --port 4000\n    ports: ['8000:4000']\n"
               "    healthcheck:\n      test: ['CMD', 'wget', '-q', 'http://localhost:4000/status/health']\n    depends_on: [db]\n  db:\n    image: postgres:17\n")
    result = read(repo(start=None, files={"compose.yaml": compose}))
    spec = result["spec"]
    assert (spec.port, spec.health_path, spec.build.entrypoint, spec.needs.database) == (4000, "/status/health", "node server.js --port 4000", "postgres")
    hono = "import { serve } from '@hono/node-server';\nimport { Hono } from 'hono';\nconst app = new Hono();\napp.get('/health', (c) => c.text('ok'));\n" \
           "serve({ fetch: app.fetch, port: Number(process.env.PORT) || 8788 });\n"
    assert read(repo(deps={"hono": "^4", "@hono/node-server": "^1"}, files={"server.js": hono}))["spec"].port == 8788
    hapi = "const Hapi = require('@hapi/hapi');\nconst server = Hapi.server({ port: process.env.PORT || 3000, host: 'localhost' });\n"
    assert refused(read(repo(deps={"@hapi/hapi": "^21"}, files={"server.js": hapi})), "listens on localhost")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed here")
def test_the_pinned_npm_is_checked_against_its_integrity_hash(tmp_path):
    version, integrity = scaffold.LOCK_NPM["22"]
    code = scaffold.pinned_npm_check(version, str(tmp_path))
    for found, value, expected in ((version, integrity, 0), (version, "sha512-other", 4), ("10.9.9", integrity, 4), (None, None, 4)):
        lock = {"packages": {"node_modules/npm": {"version": found, "integrity": value}}} if found else {"packages": {}}
        (tmp_path / "package-lock.json").write_text(json.dumps(lock))
        assert subprocess.run([shutil.which("node"), "-e", code, integrity], capture_output=True, text=True, timeout=60).returncode == expected
