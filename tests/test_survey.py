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

# What the reader of 1.22.0 (Phase 20) makes of each repository: accepted with the start it chose, or refused with a fragment
# of each refusal's problem. The research review counted 3 accepted and 60 refusals at 1.21.0; the sample now reads as 8
# accepted and 42 refusals, and the rows below say which rung each one stops at. Phases 21 and 22 move more of them.
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
    "nestjs/typescript-starter": ["which builds first", "needs a build"],  # Phase 21
    "fastify/demo": ["chains commands through a shell", "starts Fastify without host"],  # Phase 21 and D51
    "remix-run/indie-stack": ["the final image runs as root", "builds on Node.js 18"],
    "sveltejs/realworld": ["is a workspace", "pnpm's lockfile", "front-end site"],  # Phase 21 and D55; its .nvmrc 20 passes now (D48)
    "mckaywrigley/chatbot-ui": ["runs Next.js, which needs a build", "needs a build"],  # .env.local.example and .nvmrc 20 pass now
    "gothinkster/node-express-realworld-example-app": ["is a workspace", "the final image runs as root"],
    "hagopj13/node-express-boilerplate": ["Yarn's lockfile"],  # its literal port 3000 is port: 3000 now (20.4)
    "socketio/chat-example": "node index.js",
    "vercel/ai-chatbot": ["pnpm's lockfile", "runs Next.js, which needs a build", "needs a build"],
    "GoogleCloudPlatform/nodejs-docs-samples:run/helloworld": ["the final image runs as root", "builds on Node.js 20"],
    "huggingface/chat-ui": ["looks like a committed secret", "looks like a committed secret", "copies .env into the image", "the final image runs as root"],
    "shadcn-ui/taxonomy": ["pnpm's lockfile", "runs Next.js, which needs a build", "needs a build"],  # its .nvmrc 16 passes now (D48)
    "discord/discord-example-app": "node app.js",
    "langgenius/dify:web": ["through a local path, a git repository or a URL"],  # workspace: dependencies, Phase 21
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
    assert len(accepted) == sum(isinstance(value, str) for value in EXPECTED.values()) == 8
    assert refusals == sum(len(value) for value in EXPECTED.values() if isinstance(value, list)) == 42
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
