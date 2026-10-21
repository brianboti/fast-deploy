"""Phase 22: run it, then say what to fix (docs/ROADMAP.md 22.1 to 22.6, decisions D49, D51, D52, D54 and D55). The proxy
sidecar for a service bound to loopback, tini in the platform's images and chained starts under it, the wrapper user for a
repository's own image that runs as root, static sites behind IAP, tests: report, and the diagnosis that maps a failed
candidate's log to the line and the contract key to change. No test runs a container, a package manager or gcloud."""
import json
import subprocess
from pathlib import Path

import pytest
from test_phase18 import build_steps, choice, refused
from test_phase18 import read as node_read
from test_phase18 import repo as node_repo
from test_scaffold import hadolint_command

from labdeploy import chat, cost, dockerpolicy, pins, reader, release, scaffold, status
from labdeploy.models import AppSpec, draft_dump, spec_hash

FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n"


def read(files, path="", **kwargs):
    return reader.read(sorted(files) + sorted({p.rsplit("/", 1)[0] for p in files if "/" in p}), files, path, **kwargs)


def spec(**fields):
    base = {"name": "svc", "reviewed": True}
    return AppSpec.model_validate({**base, **fields})


# ---- the contract
def test_the_new_keys_are_checked_and_leave_old_contracts_as_they_were():
    plain = spec()
    assert spec_hash(plain) == spec_hash(AppSpec.model_validate({"name": "svc", "reviewed": True, "proxy": False, "tests": "required"}))
    assert "proxy" not in draft_dump(plain) and "user" not in draft_dump(plain)["build"] and "output" not in draft_dump(plain)["build"]
    assert spec(tests="report").tests == "report" and spec(proxy=True).proxy
    assert spec(build={"user": 65532}).build.user == 65532
    with pytest.raises(ValueError, match="build.user wraps a repository's own Dockerfile"):
        spec(build={"recipe": "platform", "entrypoint": "x", "user": 65532})
    with pytest.raises(ValueError):
        spec(build={"user": 0})  # root is not a wrapper user
    site = {"runtime": "static", "node": "24", "exposure": "lab-members", "build": {"recipe": "platform", "entrypoint": scaffold.STATIC_ENTRYPOINT, "output": "dist"}}
    assert spec(**site).runtime == "static" and "python" not in draft_dump(spec(**site))
    with pytest.raises(ValueError, match="exposure: lab-members"):
        spec(**{**site, "exposure": "authenticated"})
    with pytest.raises(ValueError, match="needs build.output"):
        spec(**{**site, "build": {"recipe": "platform", "entrypoint": "caddy"}})
    with pytest.raises(ValueError, match="build.output applies to runtime: static"):
        spec(build={"recipe": "platform", "entrypoint": "x", "output": "dist"})
    chained = spec(runtime="node-http", node="24", build={"recipe": "platform", "entrypoint": "node migrate.js && node server.js"})
    assert chained.build.entrypoint.count("&&") == 1
    with pytest.raises(ValueError, match="never through npm"):
        spec(runtime="node-http", node="24", build={"recipe": "platform", "entrypoint": "npm run migrate"})


# ---- 22.1 the proxy sidecar (D51)
class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return ""


def test_a_proxy_contract_deploys_nginx_as_the_ingress_container_in_front_of_the_app():
    runner = Runner()
    adapter = release.CloudRun("p", "us-central1", "svc", "probe@p.iam.gserviceaccount.com", runner=runner)
    adapter.deploy("us-docker.pkg.dev/p/r/svc@sha256:" + "a" * 64, spec(runtime="node-http", node="24", port=3000, proxy=True,
                                                                         build={"recipe": "platform", "entrypoint": "node server.js"}), "r1")
    args = runner.calls[-1]
    proxy, app = args.index("--container=proxy"), args.index("--container=app")
    assert proxy < app and args[proxy + 1] == f"--image={release.NGINX_IMAGE}" and "--port=8080" in args[proxy:app] and "--depends-on=app" in args[proxy:app]
    assert "--port" not in args[app:] and args[app + 1:app + 3] == ["--image", "us-docker.pkg.dev/p/r/svc@sha256:" + "a" * 64]
    assert args.index("--revision-suffix") < proxy and "--min-instances" in args[:proxy]  # the service's own flags come first
    assert any(flag.startswith("--clear-env-vars") or flag.startswith("--env-vars-file") for flag in args[app:])  # the app's settings stay the app's
    script = release.proxy_script(spec(port=3000, proxy=True))
    assert "proxy_pass http://127.0.0.1:3000;" in script and "listen 8080;" in script and "," not in script
    assert release.proxy_port(spec(port=8080, proxy=True)) == 8081 and "@sha256:" in release.NGINX_IMAGE
    plain = Runner()
    release.CloudRun("p", "us-central1", "svc", "probe@p.iam.gserviceaccount.com", runner=plain).deploy("img@sha256:" + "b" * 64, spec(port=3000), "r1")
    assert "--container=proxy" not in plain.calls[-1] and plain.calls[-1][plain.calls[-1].index("--port") + 1] == "3000"


def test_the_reader_drafts_the_proxy_for_a_loopback_binder_and_the_cost_counts_it():
    loopback = node_read(node_repo(files={"server.js": "require('http').createServer().listen(3000, '127.0.0.1');\n"}))
    assert not loopback["refusals"] and loopback["spec"].proxy and loopback["spec"].port == 3000
    assert any("adds the platform's proxy sidecar (proxy: true, D51)" in note for note in loopback["notes"])
    with_host = node_read(node_repo(files={"server.js": "require('http').createServer().listen(process.env.PORT, process.env.HOST || '127.0.0.1');\n"}))
    assert not with_host["spec"].proxy and with_host["spec"].env == {"HOST": "0.0.0.0"}  # noqa: S104 - a variable is the better fix
    plain, proxied = cost.estimate(spec()), cost.estimate(spec(proxy=True))
    assert proxied["per_instance_hour_usd"] > plain["per_instance_hour_usd"] and any("sidecar" in item for item in proxied["assumptions"])
    assert cost.allocation(spec(proxy=True)) == (1 + release.PROXY_CPU, 512 + release.PROXY_MEMORY_MIB)


# ---- 22.2 tini and chained starts (D49)
def test_the_platforms_images_start_under_tini_and_a_chain_of_node_commands_runs_as_written():
    tini = pins.mentions("tini")
    python = scaffold.platform_dockerfile(spec(build={"recipe": "platform", "entrypoint": "uvicorn main:app --port $PORT"}), True)
    node = scaffold.platform_dockerfile(spec(runtime="node-http", node="24", build={"recipe": "platform", "entrypoint": "node a.js && node b.js"}), True)
    for text in (python, node):
        assert " AS init\n" in text and 'ENTRYPOINT ["/usr/local/bin/tini", "-g", "--"]' in text and "COPY --from=init /tini /usr/local/bin/tini" in text
        assert all(place in text for place in tini if "{" not in place) and "sha256sum --check --strict" in text
    assert node.rstrip().endswith('CMD ["sh", "-c", "node a.js && exec node b.js"]')
    assert scaffold.start_command("node a.js && node b.js") == ["sh", "-c", "node a.js && exec node b.js"]
    chain = node_read(node_repo(start="node migrate.js && node server.js", files={"migrate.js": "x"}))
    assert not chain["refusals"] and chain["spec"].build.entrypoint == "node migrate.js && node server.js"
    assert any("under its image's init (tini)" in note for note in chain["notes"])
    for start in ("npm run migrate && node server.js", "node a.js & node b.js", "node a.js && node b.js | tee x"):
        assert node_read(node_repo(start=start))["spec"] is None, start


# ---- 22.3 the wrapper user (D52)
def test_a_root_dockerfile_of_unreviewed_code_is_wrapped_not_refused():
    rooted = {"Dockerfile": "FROM python:3.12-slim\nCOPY . .\nCMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\"]\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"}
    result = read(rooted)
    assert not result["refusals"] and result["spec"].build.user == 65532 and choice(result, "User")["value"].startswith("65532, set by the platform's wrapper stage")
    reviewed = read({**rooted, "platform.yaml": "name: svc\ntrust: reviewed\n"})
    assert reviewed["spec"].build.user is None  # the lab's own code: reported, never wrapped unless the contract asks
    findings = dockerpolicy.check(rooted["Dockerfile"], "unknown", wrapped=65532)
    assert [item["rule"] for item in findings] == ["runs-as-root"] and not findings[0]["refuses"] and "USER 65532" in findings[0]["problem"]
    assert dockerpolicy.check(rooted["Dockerfile"], "unknown")[0]["refuses"]
    node = node_read(node_repo(files={"Dockerfile": "FROM node:24-slim\nCOPY . .\nCMD [\"node\", \"server.js\"]\n"}))
    assert not node["refusals"] and node["spec"].build.user == 65532


def test_the_pipeline_wraps_the_image_and_tells_the_policy():
    _, steps = build_steps()
    build = next(step for step in steps if step.get("name") == "Build without cloud credentials")
    assert build["env"]["USER_ID"] == "${{ steps.policy.outputs.user }}"
    run = build["run"]
    assert "printf 'FROM lab-app:%s\\nUSER %s:%s\\n' \"$COMMIT_SHA\" \"$USER_ID\" \"$USER_ID\"" in run and "--network=none" in run.split('if [ -n "$USER_ID" ]')[1]
    policy = next(step for step in steps if step.get("name") == "Check the Dockerfile against the platform's policy")
    assert 'WRAPPED=(--wrapped-user "$USER_ID")' in policy["run"] and '"${WRAPPED[@]}"' in policy["run"]
    read_policy = next(step for step in steps if step.get("name") == "Read the application's test policy")["run"]
    assert '"user": str(spec.build.user or "")' in read_policy and "[[ \"$USER_ID\" =~ ^([0-9]{4,10})?$ ]]" in read_policy


# ---- 22.4 static sites (D55)
def site_repo(builder="vite", extra_deps=None, files=None):
    package = {"name": "site", "version": "1.0.0", "scripts": {"build": f"{builder} build", "dev": f"{builder}"},
               "devDependencies": {builder: "^6", **(extra_deps or {})}}
    from test_phase18 import lockfile
    return {"package.json": json.dumps(package), "package-lock.json": lockfile(package, {builder: {}}), "index.html": "<html></html>", **(files or {})}


def test_a_front_end_site_is_drafted_as_static_behind_iap():
    result = node_read(site_repo())
    site = result["spec"]
    assert not result["refusals"] and site.runtime == "static" and site.build.output == "dist" and site.exposure == "lab-members"
    assert site.build.entrypoint == scaffold.STATIC_ENTRYPOINT and site.health_path is None and site.build.recipe == "platform"
    assert choice(result, "Build")["value"].startswith("the platform's static image: the build script writes the site into dist/")
    assert node_read(site_repo("react-scripts"))["spec"].build.output == "build"
    kit = node_read(site_repo("vite", {"@sveltejs/kit": "^2", "@sveltejs/adapter-static": "^3"}))
    assert kit["spec"].build.output == "build"
    vercel = node_read(site_repo("vite", {"@sveltejs/kit": "^2", "@sveltejs/adapter-vercel": "^5"}))
    assert vercel["spec"] is None and refused(vercel, "SvelteKit builds a site only with @sveltejs/adapter-static")
    nobuild = node_read(site_repo(files={"package.json": json.dumps({"name": "site", "devDependencies": {"vite": "^6"}})}))
    assert nobuild["spec"] is None


def test_the_static_image_builds_the_site_and_serves_it_with_caddy_as_a_user_that_is_not_root():
    site = spec(runtime="static", node="24", exposure="lab-members", build={"recipe": "platform", "entrypoint": scaffold.STATIC_ENTRYPOINT, "output": "dist"})
    text = scaffold.platform_dockerfile(site, True, "package-lock.json", {"manager": "npm", "lockfile": "package-lock.json", "plan": {}, "member": ""})
    caddy = pins.mentions("caddy", "templates/repo/Dockerfile.static")
    assert all(place in text for place in caddy) and "sha512sum --check --strict" in text and "COPY --from=build /app/dist /srv\n" in text
    assert "RUN npm ci --ignore-scripts --no-audit --no-fund \\\n && npm run build\n" in text and "try_files {path} /index.html" in text
    assert "admin off" in text and "USER 65532:65532" in text and text.rstrip().endswith(
        'CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]')
    _, steps = build_steps()
    read_policy = next(step for step in steps if step.get("name") == "Read the application's test policy")["run"]
    assert "^(python-http|node-http|static)$" in read_policy and 'echo "js=true"' in read_policy


@pytest.mark.skipif(hadolint_command() is None, reason="the pinned hadolint is not available")
def test_the_phase_22_images_pass_hadolint():
    site = spec(runtime="static", node="24", exposure="lab-members", build={"recipe": "platform", "entrypoint": scaffold.STATIC_ENTRYPOINT, "output": "dist"})
    for text in (scaffold.platform_dockerfile(site, True, "package-lock.json", {"manager": "npm", "plan": {}, "member": ""}),
                 scaffold.platform_dockerfile(spec(build={"recipe": "platform", "entrypoint": "uvicorn main:app"}), True)):
        run = subprocess.run(hadolint_command() + ["-"], input=text, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0 and not run.stdout.strip(), run.stdout + run.stderr


# ---- 22.5 tests: report (D54)
def test_a_suite_that_needs_a_browser_or_a_database_is_reported_not_a_gate():
    playwright = node_read(node_repo(extra={"scripts": {"start": "node server.js", "test": "playwright test"}}))
    assert playwright["spec"].tests == "report" and any("tests: report runs and counts them" in note for note in playwright["notes"])
    plain = node_read(node_repo(extra={"scripts": {"start": "node server.js", "test": "node --test"}}))
    assert plain["spec"].tests == "required"
    python = read({"main.py": FASTAPI, "requirements.txt": "fastapi\n", "tests/test_db.py": "import os\nURL = os.environ['DATABASE_URL']\n"})
    assert python["spec"].tests == "report"
    kept = read({"main.py": FASTAPI, "requirements.txt": "fastapi\n", "tests/test_db.py": "DATABASE_URL = 1\n", "platform.yaml": "name: svc\ntests: required\n"})
    assert kept["spec"].tests == "required"
    _, steps = build_steps()
    for name in ("Run unit tests", "Run unit tests (Node.js)"):
        step = next(item for item in steps if item.get("name") == name)
        assert "steps.policy.outputs.tests != 'none'" in step["if"] and step["env"]["TESTS"] == "${{ steps.policy.outputs.tests }}"
        assert '[ "$TESTS" = "report" ]' in step["run"] and "never a stop" in step["run"] or "the release goes on" in step["run"]
    summary = status.summarize({"status": "completed", "conclusion": "success", "jobs": [
        {"name": "build", "steps": [{"name": "Run unit tests", "status": "completed", "conclusion": "success"},
                                    {"name": "Audit Python dependencies", "status": "completed", "conclusion": "success"}]},
        {"name": "deploy", "steps": [{"name": "Deploy, probe, promote; restore traffic on failure", "status": "completed", "conclusion": "success"},
                                     {"name": "Validate artifact and deployment configuration", "status": "completed", "conclusion": "success"}]}]},
        {"tests": "reported_by_config"})
    assert summary["checks"]["tests"] == "reported" and summary["state"] == "succeeded"
    assert chat.CHECK_WORDS["reported"].startswith("ran and reported")


# ---- 22.6 the diagnosis
@pytest.mark.parametrize(("line", "key"), [
    ("Error: EACCES: permission denied, open '/app/data/cache.json'", "build.user"),
    ("Memory limit of 512 MiB exceeded with 530 MiB used.", "resources.memory_mib"),
    ("Server listening on http://127.0.0.1:3000", "env.HOST or proxy"),
    ("[CRITICAL] WORKER TIMEOUT (pid:7)", "build.entrypoint"),
    ("Error: Cannot find module '/app/dist/server.js'", "build.entrypoint"),
    ("The user-provided container failed to start and listen on the port defined provided by the PORT=8080 environment variable.", "port"),
    ("STARTUP HTTP probe failed 12 times consecutively for container", "health_path"),
])
def test_a_failed_candidate_is_diagnosed_with_the_line_and_the_contract_key(line, key):
    receipt = {"state": "traffic_restored", "failed_phase": "deploying", "error": "Candidate health check failed",
               "candidate_logs": [{"severity": "INFO", "message": "starting"}, {"severity": "ERROR", "message": line}]}
    found = status.diagnose(receipt)
    assert found["contract_key"] == key and found["evidence"] == line[:200]
    advice = status.advice({"state": "failed", "failed_step": {"step": status.RELEASE_STEP}, "receipt": receipt})
    assert f"({key})" in advice and "In the code:" in advice and "previous revision kept serving" in advice
    assert status.release_phases(receipt)["diagnosis"]["contract_key"] == key


def test_nothing_known_means_no_guess():
    receipt = {"state": "traffic_restored", "failed_phase": "deploying", "error": "Candidate health check failed",
               "candidate_logs": [{"message": "all good here"}]}
    assert status.diagnose(receipt) is None and "Fix the application and deploy again" in status.advice(
        {"state": "failed", "failed_step": {"step": status.RELEASE_STEP}, "receipt": receipt})


# ---- the pins
def test_tini_nginx_and_caddy_are_followed_by_the_weekly_refresh():
    current = pins.read()
    assert current["tini"]["version"] == "0.19.0" and current["nginx"]["digest"] in release.NGINX_IMAGE
    assert {path for path, _ in pins.BY_KEY["tini"].places} == {"templates/repo/Dockerfile.platform", "templates/repo/Dockerfile.node.platform",
                                                                  "templates/repo/Dockerfile.static"}
    assert {path for path, _ in pins.BY_KEY["caddy"].places} == {pins.STARTUP, "templates/repo/Dockerfile.static"}
    assert Path(pins.STARTUP).read_text().count(current["caddy"]["sha512"]) == 1 and current["caddy"]["sha512"] in Path("templates/repo/Dockerfile.static").read_text()

