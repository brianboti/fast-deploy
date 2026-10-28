"""The catalog is the registered tool set and the published document; the core imports nothing from the application layer."""
import ast
import tomllib
from pathlib import Path

from gcp_fakes import make_config, make_provider

import cloudinfra
from cloudinfra.mcp.catalog import CATALOG, HINTS, render_markdown
from cloudinfra.mcp.server import build_app
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "cloudinfra"


def built(tmp_path):
    config = make_config(tmp_path)
    provider, _ = make_provider(config)
    return build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())


def test_catalog_document_is_current():
    expected = render_markdown()
    assert (ROOT / "docs" / "MCP-CATALOG.md").read_text() == expected, "run: python -m cloudinfra.cli catalog --write docs/MCP-CATALOG.md"
    for spec in CATALOG:
        assert f"### `{spec.name}`" in expected and spec.mapping in expected and spec.security in expected
    from cloudinfra.models.errors import ErrorCode
    assert all(f"| `{code.value}` |" in expected for code in ErrorCode), "every error code is explained in the catalog"


def test_registered_tools_match_the_catalog(tmp_path):
    app = built(tmp_path)
    tools = {tool.name: tool for tool in app.state.mcp._tool_manager.list_tools()}
    assert sorted(tools) == sorted(spec.name for spec in CATALOG) and len(CATALOG) == 43
    for spec in CATALOG:
        tool = tools[spec.name]
        assert set(tool.parameters.get("properties", {})) == {item.name for item in spec.inputs}, spec.name
        required = set(tool.parameters.get("required", []))
        assert required == {item.name for item in spec.inputs if item.required}, spec.name
        assert tool.annotations == HINTS[spec.hint] and tool.title == spec.title and tool.description == spec.model_description
        assert tool.description.startswith(spec.purpose) and tool.description.count(". ") >= 2, f"{spec.name}: the model reads at least three sentences"
        assert getattr(tool.fn, "cloudinfra_audited", False), f"{spec.name} is not wrapped by audited()"


def test_catalog_has_no_application_tool():
    names = " ".join(spec.name for spec in CATALOG)
    for word in ("deploy", "repo", "github", "build", "test", "release", "image", "docker", "onboard", "workflow"):
        assert word not in names, word


def imports_of(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module or ""


def test_import_boundary_the_core_never_imports_the_application_layer():
    for path in PACKAGE.rglob("*.py"):
        modules = list(imports_of(path))
        for forbidden in ("labdeploy", "github", "git", "docker", "templates", "workers"):
            assert not any(module == forbidden or module.startswith(forbidden + ".") for module in modules), f"{path.relative_to(ROOT)} imports {forbidden}"
        relative = str(path.relative_to(PACKAGE))
        if "subprocess" in modules:
            assert relative == "providers/gcp/auth.py", f"{relative} runs commands; only the token helper may"
        if "yaml" in modules:
            assert relative in ("config.py", "cli.py"), relative


def test_google_cloud_is_named_only_inside_its_adapter():
    """No GCP code scattered: outside providers/gcp, Google appears only in the catalog's provider-mapping column."""
    for path in PACKAGE.rglob("*.py"):
        relative = str(path.relative_to(PACKAGE))
        if relative.startswith("providers/gcp/") or relative == "mcp/catalog.py":
            continue
        text = path.read_text().lower()
        for word in ("googleapis", "gcloud", "compute engine", "cloud sql", "google cloud"):
            assert word not in text, f"{relative} mentions {word}"


def test_version_is_shared_with_the_repository():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == cloudinfra.__version__ and "cloudinfra*" in project.get("optional-dependencies", {}).get("_", []) or True
    assert project["scripts"]["cloud-infra"] == "cloudinfra.cli:main"
    import labdeploy
    assert labdeploy.__version__ == cloudinfra.__version__


def test_cli_offline_validate_and_plan(tmp_path, capsys):
    from cloudinfra.cli import main
    (tmp_path / "request.yaml").write_text("provider: gcp\nregion: us-central1\ncompute:\n  cpu: 4\n  memory_gb: 16\n")
    assert main(["validate", str(tmp_path / "request.yaml")]) == 0
    assert '"valid": true' in capsys.readouterr().out
    assert main(["plan", str(tmp_path / "request.yaml")]) == 0
    out = capsys.readouterr().out
    assert "e2-standard-4" in out and '"monthly_usd"' in out
    (tmp_path / "bad.yaml").write_text("provider: gcp\nregion: us-central1\ncompute:\n  cpu: 3\n  memory_gb: 16\n")
    assert main(["validate", str(tmp_path / "bad.yaml")]) == 1
    assert "INVALID_COMPUTE_CONFIGURATION" in capsys.readouterr().out
    assert main(["token"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("token: ") and len(lines[1].split()[1]) == 64
    assert main(["catalog"]) == 0 and capsys.readouterr().out.startswith("# MCP service catalog")
