"""1.25.1: what an application that calls Claude, loads a model or streams its answers asks of its contract, said by the
reader for both runtimes (docs/ROADMAP.md D47, 19.4), and IAP_AUDIENCE for a lab-members app that verifies who is signed in.
Found by putting the BetterCare chatbot's code through the platform's own image on 2026-10-20: its embedding model's first
load stopped with EACCES, its chat streams for longer than the default timeout, and it calls Claude with an API key."""
import json
from pathlib import Path

import pytest
from test_phase18 import SERVER, read, repo

from labdeploy import reader
from labdeploy.models import AppSpec
from labdeploy.release import CloudRun

FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"
SSE = "res.writeHead(200, { 'Content-Type': 'text/event-stream' });\n"


def notes(result, fragment):
    return [note for note in result["notes"] if fragment in note]


def node(deps, code=""):
    return read(repo(deps={"express": "^4.21.2", **deps}, files={"server.js": code + SERVER}))


def python(requirements, code=""):
    return read({"main.py": FASTAPI + code, "requirements.txt": requirements})


# ---- Claude: Anthropic's own client, or the Vertex AI client the platform's models need serves
def test_a_service_on_anthropics_own_client_is_shown_the_way_through_google_cloud():
    result = node({"@anthropic-ai/sdk": "^0.128.0"}, "const Anthropic = require('@anthropic-ai/sdk');\n")
    [claude] = notes(result, "It calls Claude with Anthropic's own client (@anthropic-ai/sdk)")
    assert "needs.models: [claude]" in claude and "@anthropic-ai/vertex-sdk" in claude and "spending cap" in claude
    assert notes(result, "It uses @anthropic-ai/sdk: if it calls services outside Google Cloud, add egress: internet")
    py = python("fastapi==0.115.0\nanthropic==0.40.0\n", "import anthropic\n")
    [claude] = notes(py, "It calls Claude with Anthropic's own client (anthropic)")
    assert "anthropic[vertex]" in claude


def test_a_service_on_the_vertex_client_is_told_its_need_not_the_internet():
    result = node({"@anthropic-ai/sdk": "^0.128.0", "@anthropic-ai/vertex-sdk": "^0.22.0"})
    assert not notes(result, "It calls Claude with Anthropic's own client")
    [hint] = notes(result, "The dependencies suggest needs")
    assert "models: [claude] (Claude on Vertex AI: no API key, no internet)" in hint
    py = python("fastapi==0.115.0\nanthropic[vertex]==0.40.0\n")
    assert not notes(py, "It calls Claude with Anthropic's own client")
    assert "models: [claude]" in notes(py, "The dependencies suggest needs")[0]
    # The client's class in the code says the same when the manifest does not name the extra.
    assert not notes(python("fastapi==0.115.0\nanthropic==0.40.0\n", "from anthropic import AnthropicVertex\n"), "Anthropic's own client")


# ---- models fetched from Hugging Face on first use
def test_transformers_js_is_told_where_its_model_can_live_without_the_internet():
    result = node({"@huggingface/transformers": "^4.3.0"})
    [model] = notes(result, "@huggingface/transformers fetches each model from Hugging Face on first use")
    assert "EACCES" in model and "needs.mounts" in model and "env.localModelPath" in model and "env.allowRemoteModels = false" in model
    assert "env.cacheDir" in model and "/tmp" in model
    assert not notes(result, "if it calls services outside Google Cloud")  # the model note says where egress fits


def test_the_python_hub_libraries_are_told_the_same():
    [model] = notes(python("fastapi==0.115.0\nsentence-transformers==3.3.1\n"), "sentence-transformers fetches models from the Hugging Face Hub")
    assert "needs.mounts" in model and "HF_HUB_OFFLINE=1" in model and "egress: internet" in model


# ---- streamed answers and the request timeout
def test_a_streaming_service_is_told_the_timeout_ends_its_streams():
    [stream] = notes(node({}, SSE), "It streams its responses (server-sent events)")
    assert "(60 seconds here)" in stream and "3600 at most" in stream
    assert notes(python("fastapi==0.115.0\nsse-starlette==2.1.0\n", "from sse_starlette.sse import EventSourceResponse\n"), "It streams its responses")
    assert not notes(node({}), "It streams its responses")
    ladder = reader.Ladder()
    reader.data_app_notes(ladder, {"timeout_seconds": 900}, {"server.js": SSE}, None, False, [], node=True)
    assert not ladder.notes  # a contract that already allows long requests hears nothing


# ---- the notes that were wrong or short
def test_the_sqlite_note_names_the_database_need_not_a_dropped_item():
    [sqlite] = notes(node({}, "import { DatabaseSync } from 'node:sqlite';\n"), "It keeps a SQLite database on local disk")
    assert "needs.database" in sqlite and "side by side" in sqlite and "Phase 19" not in sqlite and "state: sqlite" not in sqlite


def test_every_setting_is_named_up_to_forty_and_the_rest_counted():
    code = "".join(f"const s{i} = process.env.SETTING_{i:02d};\n" for i in range(45))
    [settings] = notes(node({}, code), "The code reads these settings")
    assert "SETTING_00" in settings and "SETTING_39" in settings and "SETTING_40" not in settings and "and 5 more." in settings
    short = notes(node({}, "const a = process.env.ALPHA;\n"), "The code reads these settings")[0]
    assert "ALPHA." in short and "more" not in short.split(".")[0]


# ---- IAP_AUDIENCE: what a lab-members app checks the proxy's signed header against
def test_a_lab_members_revision_gets_the_audience_of_the_proxys_signed_header(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    described = []

    def runner(args):
        if "describe" in args:
            described.append(args)
            return json.dumps({"metadata": {"namespace": "12851477137", "annotations": {"run.googleapis.com/iap-enabled": "true"}}})
        return ""
    cloud = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=runner)
    people = AppSpec.model_validate({"name": "demo", "exposure": "lab-members", "reviewed": True})
    cloud.configuration(people)
    assert json.loads(Path("revision-env.yaml").read_text()) == {"IAP_AUDIENCE": "/projects/12851477137/locations/us-central1/services/demo"}
    cloud.configuration(people)
    assert len(described) == 1  # read once per release
    programs = AppSpec.model_validate({"name": "demo", "reviewed": True})
    fresh = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=runner)
    assert "--clear-env-vars" in fresh.configuration(programs) and len(described) == 1  # no proxy, no audience, no lookup


def test_the_audience_is_the_platforms_to_set():
    with pytest.raises(ValueError, match="'IAP_AUDIENCE' is set by the platform"):
        AppSpec.model_validate({"name": "demo", "exposure": "lab-members", "reviewed": True, "env": {"IAP_AUDIENCE": "/projects/1/x"}})
    assert AppSpec.model_validate({"name": "demo", "reviewed": True, "env": {"IAP_AUDIENCE": "x"}})  # no proxy: an ordinary name


def test_a_service_description_without_a_project_number_stops_the_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cloud = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=lambda args: json.dumps({"metadata": {"namespace": "p"}}))
    with pytest.raises(ValueError, match="no project number"):
        cloud.configuration(AppSpec.model_validate({"name": "demo", "exposure": "lab-members", "reviewed": True}))
