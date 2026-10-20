"""The survey as a test (docs/ROADMAP.md 20.6): the 33 public repositories the research review of 2026-10-20 measured
(docs/RESEARCH-2026-10-20.md section 4), recorded under tests/fixtures/survey/ as each repository's file tree and the texts
the reader reads, exactly as the controller feeds the reader (labdeploy/contract.py), from shallow clones of that day.
Each fixture carries the expected contract or refusal, so a change to the reader shows its effect on the sample. Nothing
from the repositories is run: the fixtures are text, and the reader reads text."""
import gzip
import json
from pathlib import Path

import pytest

from labdeploy import reader

FIXTURES = Path(__file__).parent / "fixtures" / "survey"
INDEX = json.loads((FIXTURES / "index.json").read_text())

# What the reader makes of each repository: accepted with the start it chose, or refused with a fragment of each refusal's
# problem. The research review counted 3 accepted and 60 refusals at 1.21.0; Phase 20 (1.22.0) read 8 and 42; Phase 21
# (1.23.0, the Node.js builds and managers) reads 14 accepted and 27 refusals, and the rows below say which rung each one
# stops at. Phase 22 (the root user, the proxy sidecar, static sites) moves more of them.
EXPECTED = {
    # Python
    "fastapi/full-stack-fastapi-template:backend": ["the final image runs as root"],  # D52 (Phase 22): the platform's wrapper user
    "miguelgrinberg/microblog": ["the final image runs as root"],
    "wagtail/bakerydemo": ["the final image runs as root"],  # its `-r requirements/production.txt` is followed now (20.1)
    "streamlit/streamlit-example": "streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true",
    "oobabooga/text-generation-webui": ["holds several requirements files"],  # one file per hardware: a person's choice
    "nsidnev/fastapi-realworld-example-app": ["the final image runs as root", "builds on Python 3.9"],  # the developer's line
    "HackSoftware/Django-Styleguide-Example": "its own Dockerfile",  # `-r requirements/base.txt` followed (20.1)
    "GoogleCloudPlatform/python-docs-samples:run/helloworld": ["the final image runs as root"],
    "litestar-org/litestar-fullstack": ["looks like a committed secret"],  # tools/deploy/docker/.env.docker; the example files pass now
    "aio-libs/aiohttp-demos": ["no dependency manifest", "says nothing that starts the service"],  # a root above the demos: named
    "langchain-ai/chat-langchain": ["says nothing that starts the service"],  # started by the LangGraph command line: a person's
    "openai/openai-quickstart-python": ["says nothing that starts the service"],  # the apps live in examples/: named
    "comfyanonymous/ComfyUI": "python main.py",  # aiohttp among the dependencies, started as its README says (D50)
    "AUTOMATIC1111/stable-diffusion-webui": "python launch.py",  # Gradio, from the launcher that imports it (D50)
    "lm-sys/FastChat": ["says nothing that starts the service"],  # its servers are started by command lines the files do not say
    "open-webui/open-webui:backend": "uvicorn open_webui.main:app --host 0.0.0.0 --port 8080",  # main.py is over the size read; FastAPI decides (D50)
    "langgenius/dify:api": "its own Dockerfile",
    "gradio-app/gradio:demo/chatbot_simple": ["no dependency manifest"],  # a demo folder without one
    # Node.js
    "heroku/node-js-getting-started": ["looks like a committed secret"],  # a real .env
    "nestjs/typescript-starter": "node dist/main.js",  # built with nest build in the platform's image (21.2, 1.23.0)
    "fastify/demo": ["starts Fastify without host"],  # its "build && node" start is built now (21.2); the host stays D51 (Phase 22)
    "remix-run/indie-stack": ["the final image runs as root", "builds on Node.js 18"],
    "sveltejs/realworld": ["is a workspace of several packages and starts nothing itself", "without @sveltejs/adapter-node"],  # D55 (Phase 22)
    "mckaywrigley/chatbot-ui": "node node_modules/next/dist/bin/next start -H 0.0.0.0 -p $PORT",  # Next.js built and served under node (21.2)
    "gothinkster/node-express-realworld-example-app": ["the final image runs as root"],  # its own Dockerfile; nx no longer refuses it
    "hagopj13/node-express-boilerplate": "its own Dockerfile",  # its yarn.lock (Yarn 1) is read now (21.1)
    "socketio/chat-example": "node index.js",
    "vercel/ai-chatbot": "node node_modules/next/dist/bin/next start -H 0.0.0.0 -p $PORT",  # pnpm-lock.yaml installed by pnpm (21.1), Next.js built
    "GoogleCloudPlatform/nodejs-docs-samples:run/helloworld": ["the final image runs as root", "builds on Node.js 20"],
    "huggingface/chat-ui": ["looks like a committed secret", "looks like a committed secret", "copies .env into the image", "the final image runs as root"],
    "shadcn-ui/taxonomy": "node node_modules/next/dist/bin/next start -H 0.0.0.0 -p $PORT",  # pnpm, Next.js; its .nvmrc 16 passes (D48)
    "discord/discord-example-app": "node app.js",
    "langgenius/dify:web": "its own Dockerfile",  # its workspace: dependencies resolve in the root's pnpm-lock.yaml (21.4)
}
ACCEPTED_AT_1_21 = {"langgenius/dify:api", "socketio/chat-example", "discord/discord-example-app"}


def fixture(entry):
    item = next(item for item in INDEX if item["entry"] == entry)
    return json.load(gzip.open(FIXTURES / item["file"], "rt", encoding="utf-8"))


def outcome(data):
    result = reader.read(data["paths"], data["texts"], data["path"], fallback_name=data["repository"].split("/")[1], sizes=data["sizes"])
    spec = result["spec"]
    start = None
    if spec is not None:
        start = "its own Dockerfile" if spec.build.recipe == "repository" else spec.build.entrypoint
    return result, start


@pytest.mark.parametrize("entry", list(EXPECTED))
def test_each_surveyed_repository_reads_as_recorded(entry):
    data = fixture(entry)
    result, start = outcome(data)
    expected = EXPECTED[entry]
    if isinstance(expected, str):
        assert not result["refusals"], [item["problem"] for item in result["refusals"]]
        assert start == expected
        assert result["service"] and result["runtime"] in ("python-http", "node-http")
    else:
        assert result["spec"] is None
        problems = [item["problem"] for item in result["refusals"]]
        assert len(problems) == len(expected), problems
        for fragment, problem in zip(expected, problems, strict=True):
            assert fragment in problem, problems
    assert all(item["file"] and item["fix"] for item in result["refusals"])


def test_the_sample_moves_from_three_accepted_toward_the_reviews_estimate():
    accepted, refusals = set(), 0
    for item in INDEX:
        result, _ = outcome(json.load(gzip.open(FIXTURES / item["file"], "rt", encoding="utf-8")))
        refusals += len(result["refusals"])
        if result["spec"] is not None:
            accepted.add(item["entry"])
    assert ACCEPTED_AT_1_21 <= accepted  # nothing that was taken as it is at 1.21.0 is refused now
    assert len(accepted) == sum(isinstance(value, str) for value in EXPECTED.values()) == 14
    assert refusals == sum(len(value) for value in EXPECTED.values() if isinstance(value, list)) == 27
    assert len(INDEX) == 33 and all(item["paths"] > 0 for item in INDEX)


def test_a_root_above_several_services_names_them_in_every_refusal():
    result, _ = outcome(fixture("aio-libs/aiohttp-demos"))
    assert all("demos/blog" in item["fix"] for item in result["refusals"]) and any("onboard_application with all=true" in note for note in result["notes"])
    assert len(reader.service_folders(fixture("aio-libs/aiohttp-demos")["paths"], {})) >= 4  # blog, chat, imagetagger, moderator and more
    # openai-quickstart-python keeps its apps under examples/ with the root's requirements.txt: not services by the manifest rule, so nothing is named.
    result, _ = outcome(fixture("openai/openai-quickstart-python"))
    assert not any("examples/" in item["fix"] for item in result["refusals"])


def test_the_fixtures_are_what_the_controller_would_fetch():
    """Each fixture holds the files the reader asks for (files_to_read and the follow-up passes), within its size limits."""
    data = fixture("huggingface/chat-ui")
    wanted = reader.files_to_read(data["paths"], data["path"])
    assert set(wanted) <= set(data["texts"]) | set(data["skipped"])
    assert all(len(text.encode()) <= reader.size_limit(name) for name, text in data["texts"].items())
    assert data["commit"] and data["cloned"] == "2026-10-20"
