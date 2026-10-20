"""Phase 20: read more, refuse less (docs/ROADMAP.md 20.1 to 20.5, decisions D48, D50 and D56). The reader's bugs the survey
of 2026-10-20 found, the Python entries decided by the dependency that names the framework, the manifests the platform now
reads or locks, the Node.js rungs that stopped refusing, dead lines in version files, and everything in one go. Nothing
here runs a repository's code: every input is text."""
import base64
import json

import pytest
from test_phase13 import controller_with
from test_phase18 import choice, refused
from test_phase18 import read as node_read
from test_phase18 import repo as node_repo

from labdeploy import chat, node, reader, scaffold
from labdeploy.github import GitHub
from labdeploy.models import AppSpec

FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n"
FLASK = "from flask import Flask\napp = Flask(__name__)\n"


def read(files, path="", **kwargs):
    return reader.read(sorted(files) + sorted({p.rsplit("/", 1)[0] for p in files if "/" in p}), files, path, **kwargs)


def refusal(result, fragment):
    return any(fragment in item["problem"] for item in result["refusals"])


def note(result, fragment):
    return any(fragment in item for item in result["notes"])


# ---- 20.1 the reader's bugs
def test_an_example_environment_file_by_any_name_is_an_example_not_a_secret():
    result = read({".env.local.example": "OPENAI_API_KEY=\n", "tools/railway/.env.railway.example": "X=1\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert not result["refusals"] and note(result, "OPENAI_API_KEY")
    real = read({"tools/deploy/.env.docker": "KEY=1\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert refusal(real, "committed secret")  # a real environment file by another name is still rotated


def test_a_django_project_is_one_with_django_or_a_django_manage_py_never_a_settings_file_that_mentions_it():
    litestar = read({"manage.py": "# This is here for convenience due to its ubiquitous usage in Django.\nfrom app.cli import run\n",
                     "app/lib/settings.py": "# like django\nX = 1\n", "app/__init__.py": "from litestar import Litestar\napp = Litestar()\n",
                     "requirements.txt": "litestar\n"})
    assert not litestar["refusals"] and litestar["spec"].build.entrypoint.startswith("uvicorn app:app")
    django = read({"manage.py": "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\nfrom django.core.management import execute_from_command_line\n",
                   "config/settings.py": "INSTALLED_APPS = []\n", "config/wsgi.py": "from django.core.wsgi import get_wsgi_application\napplication = get_wsgi_application()\n",
                   "requirements.txt": "django\n"})
    assert not django["refusals"] and django["spec"].build.entrypoint.endswith("config.wsgi:application")
    assert choice(django, "Application")["from"] == "config/wsgi.py beside manage.py"  # no WSGI_APPLICATION needed
    assert note(django, "collectstatic") and note(django, "needs.migrate: [python, manage.py, migrate]")
    asgi = read({"manage.py": "from django.core.management import execute_from_command_line\n", "site/asgi.py": "from django.core.asgi import get_asgi_application\n",
                 "requirements.txt": "django\n"})
    assert asgi["spec"].build.entrypoint.startswith("uvicorn site.asgi:application")
    lost = read({"manage.py": "import django\n", "requirements.txt": "django\n"})
    assert refusal(lost, "no single <package>/wsgi.py beside manage.py")


def test_requirements_includes_and_a_requirements_folder_are_followed():
    included = read({"requirements.txt": "-r requirements/base.txt\ngunicorn\n", "requirements/base.txt": "flask==3.0\n", "app.py": FLASK})
    assert not included["refusals"] and "# from requirements/base.txt\nflask==3.0" in included["lock"]["input"] and "gunicorn" in included["lock"]["input"]
    assert "with what it includes (requirements/base.txt)" in choice(included, "Dependencies")["value"]
    outside = read({"requirements.txt": "-r ../shared.txt\n", "app.py": FLASK})
    assert refusal(outside, "outside the service's folder")
    missing = read({"requirements.txt": "-r requirements/prod.txt\n", "app.py": FLASK})
    assert refusal(missing, "is not in the repository")
    folder = read({"requirements/production.txt": "flask==3.0\n", "requirements/dev.txt": "pytest\n", "app.py": FLASK})
    assert folder["lock"]["from"] == "requirements/production.txt" and not folder["refusals"]
    only = read({"requirements/web.txt": "flask==3.0\n", "app.py": FLASK})
    assert only["lock"]["from"] == "requirements/web.txt"
    variants = read({"requirements/full/requirements.txt": "torch\n", "requirements/full/requirements_amd.txt": "torch\n", "app.py": FLASK, "setup.cfg": "[x]\n"})
    assert refusal(variants, "holds several requirements files (full/requirements.txt, full/requirements_amd.txt)") and refusal(variants, "a person's choice")
    assert "requirements/full/requirements_amd.txt" in reader.files_to_read(sorted({"requirements/full/requirements_amd.txt", "app.py"}))


def test_entry_files_are_ranked_by_name_at_any_depth_before_shallower_helpers():
    paths = [f"helpers/h{i:02d}.py" for i in range(70)] + ["pkg/sub/main.py", "pkg/app.py", "requirements.txt", "examples/main.py"]
    wanted = reader.files_to_read(paths)
    assert wanted[:3] == ["requirements.txt", "pkg/sub/main.py", "pkg/app.py"]  # main.py then app.py by name, wherever they live
    assert "examples/main.py" not in wanted[:60] or wanted.index("examples/main.py") > wanted.index("helpers/h00.py")  # examples come last


def test_a_notebook_is_a_note_and_the_refusal_names_the_entry_point():
    result = read({"analysis.ipynb": "{}", "helpers.py": "x = 1\n", "requirements.txt": "pandas\n"})
    assert result["spec"] is None and refusal(result, "says nothing that starts the service") and not refusal(result, "notebook")
    assert note(result, "analysis.ipynb stay out of the image")


def test_a_root_above_several_services_names_them_in_every_refusal():
    files = {"demos/blog/requirements.txt": "flask\n", "demos/blog/app.py": FLASK, "demos/chat/requirements.txt": "aiohttp\n", "demos/chat/main.py": "x = 1\n",
             "README.md": "x", ".env": "SECRET=1\n"}
    result = read(files)
    assert len(result["refusals"]) >= 2 and all("demos/blog, demos/chat" in item["fix"] for item in result["refusals"])
    assert note(result, "onboard_application with all=true")
    assert reader.service_folders(sorted(files) + ["demos", "demos/blog", "demos/chat"], files) == ["demos/blog", "demos/chat"]
    assert reader.service_folders(sorted(files), files, "demos/blog") == []  # a service in a folder is not a root


def test_only_the_entry_file_and_its_imports_decide_the_host():
    helper = node_read(node_repo(files={"scripts/mock-server.mjs": "require('http').createServer().listen(9000, '127.0.0.1');\n"}))
    assert not helper["refusals"] and helper["spec"].port == 3000
    entry = node_read(node_repo(files={"server.js": "require('http').createServer().listen(process.env.PORT, '127.0.0.1');\n"}))
    assert refused(entry, "listens on 127.0.0.1")


# ---- 20.2 Python entries (D50)
def test_the_dependency_that_names_the_framework_decides_the_command():
    streamlit = read({"requirements.txt": "streamlit==1.40.0\n", "streamlit_app.py": "import streamlit as st\n"})
    assert streamlit["spec"].build.entrypoint == "streamlit run streamlit_app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true"
    assert "streamlit among the dependencies" in choice(streamlit, "Start command")["from"] and note(streamlit, "first revision's log")
    gradio = read({"requirements.txt": "gradio\n", "launch.py": "import gradio as gr\ndemo = gr.Interface(fn=str, inputs='text', outputs='text')\ndemo.launch(server_port=7860)\n"})
    spec = gradio["spec"]
    assert spec.build.entrypoint == "python launch.py" and spec.port == 7860 and spec.env == {"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "7860"}  # noqa: S104
    assert [item for item in gradio["choices"] if item["what"] == "Port"] == [{"what": "Port", "value": 7860, "from": "launch.py (a literal port in the entry file; "
                                                                                                                       "Cloud Run's --port sets the container port and PORT alike)"}]
    webui = read({"requirements.txt": "gradio\n", "webui.py": "import gradio\n", "modules/x.py": "y = 1\n"})
    assert webui["spec"].build.entrypoint == "python webui.py"  # no discovery name: the named entry that imports it
    dash = read({"requirements.txt": "dash\n", "app.py": "import dash\napp = dash.Dash(__name__)\nserver = app.server\n"})
    assert dash["spec"].build.entrypoint == "python app.py" and dash["spec"].env == {"HOST": "0.0.0.0"}  # noqa: S104
    chainlit = read({"requirements.txt": "chainlit\n", "app.py": "import chainlit as cl\n"})
    assert chainlit["spec"].build.entrypoint == "chainlit run app.py --host 0.0.0.0 --port $PORT --headless"


def test_aiohttp_runs_as_a_factory_an_object_or_the_main_file():
    factory = read({"requirements.txt": "aiohttp\n", "main.py": "from aiohttp import web\nasync def init(argv):\n    app = web.Application()\n    return app\n"})
    assert factory["spec"].build.entrypoint == "python -m aiohttp.web -H 0.0.0.0 -P $PORT main:init"
    served = read({"requirements.txt": "aiohttp\n", "app.py": "from aiohttp import web\napp = web.Application()\n"})
    assert served["spec"].build.entrypoint.startswith("gunicorn app:app --bind 0.0.0.0:8080 --worker-class aiohttp.GunicornWebWorker")
    assert served["lock"]["server"].startswith("gunicorn==")
    comfy = read({"requirements.txt": "aiohttp\n", "main.py": "import argparse\np = argparse.ArgumentParser()\np.add_argument('--port', type=int, default=8188)\n",
                  "server.py": "class PromptServer:\n    pass\n"})
    assert comfy["spec"].build.entrypoint == "python main.py" and comfy["spec"].port == 8188
    client_only = read({"requirements.txt": "aiohttp\nfastapi\n", "src/api/main.py": FASTAPI, "src/api/__init__.py": "", "src/__init__.py": ""})
    assert client_only["spec"].build.entrypoint.startswith("uvicorn src.api.main:app")  # aiohttp the client never decides over the object


def test_the_platforms_python_image_keeps_no_package_manager():
    """1.22.5, from the Phase 20 live exercise: python:3.11-slim ships setuptools and wheel, whose libraries the image scan read
    as the service's (two fixable high findings stopped django-static); the final image uninstalls pip, setuptools and wheel
    after the install and collectstatic, before the user line, in both variants."""
    spec = AppSpec.model_validate({"name": "svc", "python": "3.11", "build": {"recipe": "platform", "entrypoint": "python main.py"}})
    plain = scaffold.python_platform_dockerfile(spec, True, {"system": [], "sources": [], "django": True})
    assert "RUN python -m pip uninstall -y setuptools wheel pip\n" in plain  # pip last: it removes the others before itself
    assert plain.index("collectstatic --noinput") < plain.index("pip uninstall") < plain.index("USER 65532:65532")
    staged = scaffold.python_platform_dockerfile(spec, True, {"system": ["libpq5"], "sources": [], "django": False})
    assert "RUN /opt/venv/bin/python -m pip uninstall -y setuptools wheel pip && /usr/local/bin/python -m pip uninstall -y setuptools wheel pip\n" in staged
    assert staged.index("COPY --from=build /opt/venv /opt/venv") < staged.index("pip uninstall") < staged.index("USER 65532:65532")
    # A lock that names one of the three keeps it: a package that imports pkg_resources needs setuptools at run time.
    lock = "fastapi==0.115.0 \\\n    --hash=sha256:" + "1" * 64 + "\nsetuptools==80.9.0 \\\n    --hash=sha256:" + "2" * 64 + "\n"
    assert scaffold.kept_build_tools(lock) == ["setuptools"] and scaffold.kept_build_tools("") == []
    kept = scaffold.python_platform_dockerfile(spec, True, {"system": [], "sources": [], "django": False, "keep": ["setuptools"]})
    assert "RUN python -m pip uninstall -y wheel pip\n" in kept and "The lock names setuptools: the service keeps it." in kept
    kept_staged = scaffold.python_platform_dockerfile(spec, True, {"system": ["libpq5"], "sources": [], "django": False, "keep": ["setuptools", "pip"]})
    assert "RUN /opt/venv/bin/python -m pip uninstall -y wheel && /usr/local/bin/python -m pip uninstall -y setuptools wheel pip\n" in kept_staged
    everything = scaffold.python_platform_dockerfile(spec, True, {"system": [], "sources": [], "django": False, "keep": ["setuptools", "wheel", "pip"]})
    assert "pip uninstall" not in everything and "The lock names setuptools and wheel and pip" in everything


def test_a_python_entry_file_that_reads_host_gets_every_interface():
    """1.22.1, from the Phase 20 live exercise: the aiohttp rung (and every other `python FILE` rung) sets HOST when the file
    reads it, as the Node.js rule does, because its development default (127.0.0.1) cannot be reached on Cloud Run."""
    main = ("import os\nfrom aiohttp import web\n\ndef main():\n    app = web.Application()\n"
            "    web.run_app(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8080')))\n")
    result = read({"requirements.txt": "aiohttp\n", "main.py": main})
    assert result["spec"].build.entrypoint == "python main.py" and result["spec"].env == {"HOST": "0.0.0.0"}  # noqa: S104
    assert choice(result, "Host")["from"] == "main.py" and "development default" in choice(result, "Host")["value"]
    silent = read({"requirements.txt": "aiohttp\n", "main.py": "from aiohttp import web\n\ndef main():\n    web.run_app(web.Application())\n"})
    assert silent["spec"].build.entrypoint == "python main.py" and silent["spec"].env == {}  # nothing read, nothing set
    gradio = read({"requirements.txt": "gradio\n", "app.py": "import os, gradio as gr\nhost = os.getenv('HOST')\n"})
    assert gradio["spec"].env == {"GRADIO_SERVER_NAME": "0.0.0.0", "GRADIO_SERVER_PORT": "8080", "HOST": "0.0.0.0"}  # noqa: S104
    kept = read({"requirements.txt": "aiohttp\n", "main.py": main, "platform.yaml": "name: svc\nenv:\n  HOST: '::'\n"})
    assert kept["spec"].env == {"HOST": "::"}  # the contract's own value stays


def test_a_repository_as_it_is_is_read_and_released_from_its_own_default_branch(tmp_path):
    """1.22.1, found live: streamlit/streamlit-example and HackSoftware/Django-Styleguide-Example keep `master`, and the as-is
    path refused both before the reader ran. The contract pins a commit of the default branch; its own-workflows checklist
    still needs main and says so."""
    c, brian, gh, _ = controller_with(tmp_path)
    gh.repos["lab/old"] = {"files": {"main.py": FASTAPI, "requirements.txt": "fastapi==0.115.0\n"}, "default_branch": "master"}
    asked = []
    original = gh.branch_head
    gh.branch_head = lambda repository, branch="main": (asked.append((repository, branch)), original(repository, branch))[1]
    view = c.contracts.inspect("lab/old")
    assert view["branch"] == "master" and ("lab/old", "master") in asked and view["read"]["spec"] is not None
    check = c.check_repository(brian, "lab/old")
    assert check["ready"] is False and "release only from main" in check["error"] and check["as_it_is"]["ready"]
    assert "as_is true" in check["next_action"] and check["branch"] == "master"
    first = c.onboard(brian, "lab/old", as_is=True)
    assert first["state"] == "merging_contract" and first["steps"]["validating"]["branch"] == "master"
    gh.merge_pull_request(PLATFORM_OF(c), first["steps"]["contract"]["pull_request"], "reviewed by a person")
    assert c.advance_onboarding("old")["state"] == "planning"
    app = c.config.applications["old"]
    assert app.central and app.branch == "master"  # deploy_latest and the comparison with the live release follow master
    assert c.config.applications["asis"].branch == "main" if "asis" in c.config.applications else True


def test_an_asgi_framework_without_an_object_read_is_assumed_by_name_at_any_depth():
    paths = ["backend/open_webui/main.py", "backend/open_webui/__init__.py", "backend/requirements.txt", "backend/open_webui/retrieval/loaders/main.py", "backend", "backend/open_webui"]
    texts = {"backend/requirements.txt": "fastapi\naiohttp\nuvicorn\n", "backend/open_webui/retrieval/loaders/main.py": "import aiohttp\n", "backend/open_webui/__init__.py": ""}
    result = reader.read(paths, texts, "backend", fallback_name="open-webui")  # main.py is over the size the reader fetches: not in texts
    assert result["spec"].build.entrypoint == "uvicorn open_webui.main:app --host 0.0.0.0 --port 8080"
    assert note(result, "backend/open_webui/main.py is larger than the reader fetches")
    plain = read({"requirements.txt": "flask\n", "wsgi.py": "def app(environ, start):\n    return []\n"})
    assert plain["spec"].build.entrypoint.endswith("wsgi:app") and plain["spec"].concurrency == 8


def test_the_last_rung_starts_the_main_file_with_host_and_port_and_the_contract_overrides_it():
    last = read({"requirements.txt": "requests\n", "main.py": "import http.server\n"})
    assert last["spec"].build.entrypoint == "python main.py" and last["spec"].env == {"HOST": "0.0.0.0"} and note(last, "as Railpack does")  # noqa: S104
    assert choice(last, "Start command")["from"] == "main.py (the last rung: nothing names a framework, so the file itself is started)"
    run = read({"requirements.txt": "requests\n", "run.py": "x = 1\n", "lib.py": "y = 2\n"})
    assert run["spec"].build.entrypoint == "python run.py"
    nothing = read({"requirements.txt": "requests\n", "lib.py": "y = 2\n"})
    assert refusal(nothing, "says nothing that starts the service") and nothing["spec"] is None
    overridden = read({"requirements.txt": "streamlit\n", "app.py": "import streamlit\n", "platform.yaml": "name: shown\nbuild:\n  recipe: platform\n  entrypoint: python serve.py\n"})
    assert overridden["spec"].build.entrypoint == "python serve.py"


def test_several_candidates_are_ranked_as_the_frameworks_rank_them_and_a_tie_is_refused():
    ranked = read({"main.py": FASTAPI, "admin.py": FLASK, "requirements.txt": "fastapi\nflask\n"})
    assert ranked["spec"].build.entrypoint.startswith("uvicorn main:app") and note(ranked, "main:app serves, as the frameworks' own discovery")
    deeper = read({"app/main.py": FASTAPI, "app/__init__.py": "", "tools/app.py": FLASK, "requirements.txt": "fastapi\nflask\n"})
    assert deeper["spec"].build.entrypoint.startswith("uvicorn app.main:app")  # a tools/ folder is set aside, whatever its name
    tie = read({"a.py": FASTAPI, "b.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert refusal(tie, "several candidate application objects rank the same: a:app, b:app") and tie["spec"] is None
    factories = read({"main.py": "from flask import Flask\ndef create_app():\n    return Flask(__name__)\n", "other.py": "from flask import Flask\ndef make_app():\n    return Flask(__name__)\n",
                      "requirements.txt": "flask\n"})
    assert factories["spec"].build.entrypoint.endswith("main:create_app()")


# ---- 20.3 Python manifests
def test_manifests_without_a_lock_are_read_as_text_and_compiled_by_the_platform():
    literal = read({"setup.py": "from setuptools import setup\nREQUIRES = ['flask>=3', 'gunicorn']\nsetup(name='x', install_requires=REQUIRES)\n", "app.py": FLASK})
    assert literal["lock"]["action"] == "compile" and literal["lock"]["input"] == "flask>=3\ngunicorn\n" and "never run" in choice(literal, "Dependencies")["value"]
    computed = read({"setup.py": "from setuptools import setup\nsetup(name='x', install_requires=open('r.txt').read().split())\n", "app.py": FLASK})
    assert refusal(computed, "computes install_requires")
    cfg = read({"setup.cfg": "[options]\ninstall_requires =\n    flask>=3\n    gunicorn  # the server\n", "app.py": FLASK})
    assert cfg["lock"]["input"] == "flask>=3\ngunicorn  # the server\n"
    poetry = read({"pyproject.toml": "[tool.poetry]\nname = 'x'\n[tool.poetry.dependencies]\npython = '^3.11'\nflask = '^3.0'\nuvloop = {version = '~0.21', markers = \"sys_platform != 'win32'\"}\n"
                                     "redis = {version = '^5', optional = true}\nshared = {path = '../shared'}\n", "app.py": FLASK})
    assert refusal(poetry, "shared comes from a git, path or URL source")
    poetry = read({"pyproject.toml": "[tool.poetry]\nname = 'x'\n[tool.poetry.dependencies]\npython = '^3.11'\nflask = '^3.0'\nuvloop = {version = '~0.21', python = '>=3.8,<3.13'}\n"
                                     "redis = {version = '^5', optional = true}\n", "app.py": FLASK})
    assert poetry["lock"]["input"] == "flask>=3.0.0,<4.0.0\nuvloop>=0.21.0,<0.22.0 ; python_version >= '3.8' and python_version < '3.13'\n"
    pipfile = read({"Pipfile": "[packages]\nflask = '*'\ngunicorn = '==22.0.0'\nrequests = {version = '>=2', extras = ['socks']}\n", "app.py": FLASK})
    assert pipfile["lock"]["input"] == "flask\ngunicorn==22.0.0\nrequests[socks]>=2\n"
    assert reader.poetry_specifier("^0.2.1") == ">=0.2.1,<0.3.0" and reader.poetry_specifier("~1.2") == ">=1.2.0,<1.3.0" and reader.poetry_specifier("*") == ""


def test_pdm_and_pylock_locks_are_read_at_their_hashes():
    pdm = ("[[package]]\nname = \"flask\"\nversion = \"3.0.0\"\ngroups = [\"default\"]\nfiles = [{file = \"flask-3.0.0-py3-none-any.whl\", hash = \"sha256:" + "a" * 64 + "\"}]\n"
           "[[package]]\nname = \"pytest\"\nversion = \"8.0.0\"\ngroups = [\"dev\"]\nfiles = [{file = \"p.whl\", hash = \"sha256:" + "b" * 64 + "\"}]\n")
    result = read({"pdm.lock": pdm, "pyproject.toml": "[project]\nname = 'x'\ndependencies = ['flask']\n", "app.py": FLASK})
    assert result["lock"]["from"] == "pdm.lock" and "flask==3.0.0 \\\n    --hash=sha256:" + "a" * 64 in result["lock"]["text"] and "pytest" not in result["lock"]["text"]
    assert result["lock"]["action"] == "compile" and result["lock"]["server"].startswith("gunicorn==")  # the lock lacks the server: compiled again with it
    pylock = ("lock-version = \"1.0\"\n[[packages]]\nname = \"flask\"\nversion = \"3.0.0\"\nmarker = \"python_version >= '3.9'\"\n"
              "[[packages.wheels]]\nname = \"flask-3.0.0-py3-none-any.whl\"\n[packages.wheels.hashes]\nsha256 = \"" + "c" * 64 + "\"\n"
              "[[packages]]\nname = \"gunicorn\"\nversion = \"23.0.0\"\n[[packages.wheels]]\nname = \"g.whl\"\n[packages.wheels.hashes]\nsha256 = \"" + "d" * 64 + "\"\n")
    result = read({"pylock.toml": pylock, "app.py": FLASK})
    assert result["lock"]["action"] == "convert" and "flask==3.0.0 ; python_version >= '3.9' \\\n    --hash=sha256:" + "c" * 64 in result["lock"]["text"]
    assert reader.size_limit("pdm.lock") == 5_000_000 and reader.size_limit("app.py") == 65_536


def test_system_packages_and_a_compiler_go_into_the_platforms_image(tmp_path):
    listed = read({"apt.txt": "libgdal-dev\n# a comment\nlibpq-dev\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert not listed["refusals"] and choice(listed, "System packages")["value"].startswith("libgdal-dev, libpq-dev, installed in the platform's image")
    bad = read({"Aptfile": "https://example.com/x.deb\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert refusal(bad, "is not a Debian package name")
    bindep = read({"bindep.txt": "libpq-dev [platform:dpkg]\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert refusal(bindep, "bindep's format")
    spec = AppSpec.model_validate({"name": "svc", "reviewed": True, "build": {"recipe": "platform", "entrypoint": "uvicorn main:app --port $PORT"}})
    plain = scaffold.platform_dockerfile(spec, True)
    assert "AS build" not in plain and "RUN pip install --no-cache-dir --require-hashes -r requirements.lock" in plain and "apt-get" not in plain
    staged = scaffold.platform_dockerfile(spec, True, facts={"system": ["libgdal-dev"], "sources": ["psycopg2"], "django": True})
    assert "AS build\n" in staged and "build-essential libgdal-dev" in staged and "COPY --from=build /opt/venv /opt/venv" in staged
    assert "PATH=/opt/venv/bin:$PATH" in staged and staged.count("apt-get install") == 2 and "collectstatic --noinput" in staged
    assert staged.index("FROM python") < staged.index("AS build") and staged.rstrip().endswith('CMD ["sh", "-c", "exec uvicorn main:app --port $PORT"]')
    lock = "psycopg2==2.9.9 \\\n    --hash=sha256:" + "1" * 64 + "\nflask==3.0.0 \\\n    --hash=sha256:" + "2" * 64 + " \\\n    --hash=sha256:" + "3" * 64 + "\n"
    assert scaffold.single_file_packages(lock) == ["psycopg2"]
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text(FASTAPI)
    (app / "requirements.lock").write_text(lock)
    (app / "packages.txt").write_text("libpq-dev\n")
    (app / "manage.py").write_text("from django.core.management import x\n")
    scaffold.render_build(app, tmp_path / "out", spec)
    text = (tmp_path / "out" / "Dockerfile").read_text()
    assert "build-essential libpq-dev" in text and "psycopg2" in text and "collectstatic" in text
    with pytest.raises(ValueError, match="not a Debian package name"):
        scaffold.system_packages("libpq-dev; rm -rf /\n")


# ---- 20.4 Node.js
def test_a_literal_port_goes_into_the_contract_and_restarters_are_rewritten():
    literal = node_read(node_repo(files={"server.js": "const http = require('http');\nhttp.createServer(() => {}).listen(3000);\n"}))
    assert not literal["refusals"] and literal["spec"].port == 3000 and "Cloud Run's --port sets the container port and PORT alike" in choice(literal, "Port")["from"]
    assert any("never reads PORT: the contract says port: 3000" in item for item in literal["notes"])
    for start, tool in (("nodemon server.js", "nodemon"), ("nodemon -e js,mjs --watch src server.js", "nodemon"), ("node-dev server.js", "node-dev"),
                        ("pm2-runtime start server.js -i max", "pm2-runtime"), ("pm2 start ./server.js --name api", "pm2"), ("forever server.js", "forever")):
        result = node_read(node_repo(start=start))
        assert not result["refusals"] and result["spec"].build.entrypoint == "node server.js", start
        assert choice(result, "Start command")["from"] == f"package.json (scripts.start, {tool} rewritten to node)"
        assert any(f"runs {tool}, which restarts the service" in item for item in result["notes"])
    for start in ("nodemon --exec babel-node server.js", "pm2-runtime ecosystem.config.js", "nodemon", "forever start"):
        assert node_read(node_repo(start=start))["spec"] is None, start
    assert node.start_words("nodemon server.js")[0]["rewritten"] == "nodemon" and node.start_words("node server.js")[0]["rewritten"] is None


def test_main_falls_through_the_entry_list_and_a_stale_lockfile_is_locked_again():
    for name in ("app.js", "main.js", "src/index.js", "server/index.mjs"):
        result = node_read(node_repo(start=None, files={"server.js": None, name: node_repo()["server.js"]}))
        assert result["spec"].build.entrypoint == f"node {name}" and choice(result, "Start command")["from"] == f"{name} (an entry file by name)"
    files = node_repo(start=None, files={"server.js": None, "src/index.js": node_repo()["server.js"]})
    assert reader.more_files_to_read(sorted(files) + ["src"], {"package.json": files["package.json"]}) == ["src/index.js"]
    stale = json.loads(node_repo()["package-lock.json"])
    stale["packages"][""]["dependencies"] = {"express": "^3.0.0"}
    result = node_read(node_repo(files={"package-lock.json": json.dumps(stale)}))
    assert not result["refusals"] and result["lock"]["action"] == "lock" and "locked again by the platform because package-lock.json is stale" in choice(result, "Dependencies")["value"]
    assert any(item["file"] == "package-lock.json" and "does not match package.json" in item["why"] for item in result["ignored"])
    assert reader.size_limit("package-lock.json") == 5_000_000 and reader.LOCK_LIMIT == 5_000_000
    big = node_read(node_repo(files={"package-lock.json": ""}))  # fetched as empty: over the limit
    assert refused(big, "larger than the platform reads (5 MB)")


def test_lockfiles_over_a_megabyte_come_through_the_blobs_endpoint():
    class Response:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    class BlobGitHub(GitHub):
        def __init__(self):
            self.calls = []

        def request(self, method, path, **kwargs):
            self.calls.append(path)
            if "/git/blobs/" in path:
                return Response({"encoding": "base64", "content": base64.b64encode(b'{"lockfileVersion": 3}').decode()})
            return Response({"type": "file", "encoding": "none", "size": 2_000_000, "sha": "abc", "content": ""})

    gh = BlobGitHub()
    assert gh.contents("o/r", "main", "package-lock.json", limit=reader.size_limit("package-lock.json"))["text"] == '{"lockfileVersion": 3}'
    assert gh.calls == ["/repos/o/r/contents/package-lock.json", "/repos/o/r/git/blobs/abc"]
    with pytest.raises(ValueError, match="exceeds 64 KiB"):
        gh.contents("o/r", "main", "server.js")


def test_frameworks_that_read_host_or_hostname_get_every_interface_in_env():
    result = node_read(node_repo(deps={"h3": "^1.13", "express": "^4"}))
    assert result["spec"].env == {"HOST": "0.0.0.0"} and "h3 reads it" in choice(result, "Host")["value"]  # noqa: S104
    kept = node_read(node_repo(deps={"h3": "^1.13", "express": "^4"}, files={"platform.yaml": "name: svc\nruntime: node-http\nnode: '24'\nenv:\n  HOST: '::'\n"}))
    assert kept["spec"].env == {"HOST": "::"}  # the contract's own value stays


# ---- 20.5 dead lines (D48) and everything in one go (D56)
@pytest.mark.parametrize(("files", "line", "since"), [
    ({".nvmrc": "20\n"}, "20", "2026-04-30"),
    ({".nvmrc": "lts/iron\n"}, "20", "2026-04-30"),
    ({".nvmrc": "16\n"}, "16", "2023-09-11"),
    ({".node-version": "25.1.0\n"}, "25", "2026-06-01"),
    ({".tool-versions": "nodejs 18.20.0\n"}, "18", "2025-04-30"),
])
def test_a_version_file_naming_a_dead_node_line_takes_the_lowest_supported_line(files, line, since):
    result = node_read(node_repo(files=files))
    assert not result["refusals"] and result["spec"].node == "22"
    assert choice(result, "Node.js")["from"].endswith(f"(asks for {line}, at end of life since {since}; the lowest supported line runs it)")
    assert any(f"; that line reached its end of life on {since}: the service is built, tested and audited on 22" in item for item in result["notes"])


def test_dead_lines_in_ranges_and_python_files_too_while_a_dockerfiles_from_stays_refused():
    engines = node_read(node_repo(extra={"engines": {"node": "^18.0.0"}}))
    assert engines["spec"].node == "22" and any("engines.node ^18.0.0) allows only Node.js 18; that line reached its end of life on 2025-04-30" in item for item in engines["notes"])
    assert node_read(node_repo(files={".nvmrc": "27\n"}))["spec"] is None  # not at end of life, not supported: refused as before
    assert refused(node_read(node_repo(files={"Dockerfile": "FROM node:20-slim\nUSER 65532\nCMD [\"node\", \"server.js\"]\n"})), "builds on Node.js 20")
    python = read({".python-version": "3.9\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert python["spec"].python == "3.11" and note(python, ".python-version asks for Python 3.9; that line reached its end of life on 2025-10-31")
    runtime = read({"runtime.txt": "python-3.10.12\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"})
    assert runtime["spec"].python == "3.11" and note(runtime, "reached its end of life on 2026-10-01")
    ceiling = read({"pyproject.toml": "[project]\nname = 'x'\nrequires-python = '>=3.8,<3.11'\ndependencies = ['fastapi']\n", "main.py": FASTAPI})
    assert ceiling["spec"].python == "3.11" and note(ceiling, "requires Python >=3.8,<3.11, so at most 3.10; that line reached its end of life on 2026-10-01")
    assert refusal(read({"pyproject.toml": "[project]\nname = 'x'\nrequires-python = '<3.7'\ndependencies = ['fastapi']\n", "main.py": FASTAPI}), "no supported version")
    assert refusal(read({"Dockerfile": "FROM python:3.10-slim\nUSER 65532\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"}), "builds on Python 3.10")
    assert refusal(read({".python-version": "3.6\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"}), "outside the supported 3.11 to 3.15")


def test_deploy_everything_opens_one_contract_per_service(tmp_path):
    c, brian, gh, _ = controller_with(tmp_path)
    gh.repos["lab/mono"] = {"files": {"api/main.py": FASTAPI, "api/requirements.txt": "fastapi==0.115.0\n", "web/app.py": FLASK, "web/requirements.txt": "flask==3.0.0\n",
                                      "README.md": "two services\n"}, "default_branch": "main"}
    view = c.onboard_all(brian, "https://github.com/lab/mono")
    assert view["services"] == ["api", "web"] and [item["application"] for item in view["onboardings"]] == ["mono-api", "mono-web"]
    # 1.22.2: the call returns once each service is read; the lock compiles and the contract pull requests come from the tick.
    assert all(item["state"] == "contract" and item["as_is"] for item in view["onboardings"]) and not gh.prs
    assert "opens its contract pull request within a minute" in view["next_action"]
    answer = chat.onboarding_all(view)
    assert answer.startswith("**Onboarding 2 services of `lab/mono`, each as it is.**") and "`api` → `mono-api`: read as it is; its contract pull request opens within a minute" in answer
    assert c.onboarding_tick() == {"mono-api": "merging_contract", "mono-web": "merging_contract"}
    view = c.onboard_all(brian, "lab/mono")  # asking again shows the same two onboardings, now waiting for a person
    assert all(item["state"] == "merging_contract" for item in view["onboardings"])
    numbers = {item["steps"]["contract"]["pull_request"] for item in view["onboardings"]}
    assert len(numbers) == 2 and {gh.prs[(PLATFORM_OF(c), n)]["branch"] for n in numbers} == {"lab-deploy/contract-mono-api", "lab-deploy/contract-mono-web"}
    assert "`api` → `mono-api`: merging contract, contract" in chat.onboarding_all(view)
    gh.repos["lab/empty"] = {"files": {"README.md": "x\n"}, "default_branch": "main"}
    assert c.onboard_all(brian, "lab/empty")["onboardings"] == [] and "No service was found" in chat.onboarding_all(c.onboard_all(brian, "lab/empty"))


def PLATFORM_OF(c):
    return c.config.platform.repository


def test_a_root_with_one_service_in_a_folder_takes_that_folder_without_asking(tmp_path):
    c, brian, gh, _ = controller_with(tmp_path)
    gh.repos["lab/one"] = {"files": {"service/main.py": FASTAPI, "service/requirements.txt": "fastapi==0.115.0\n", "README.md": "x\n"}, "default_branch": "main"}
    view = c.onboard(brian, "lab/one", as_is=True)
    assert view["state"] == "merging_contract" and view["path"] == "service" and view["steps"]["folder"]["path"] == "service"
    assert view["service"] == "one"
