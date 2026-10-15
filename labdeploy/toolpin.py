"""Every MCP tool's description and schemas, pinned by hash (docs/ROADMAP.md 12.3).

What Claude reads about a tool steers what it does with it, and tool poisoning works through exactly that text
(docs/RESEARCH-2026-10-14.md, section 4.4). `schema/tools.sha256.json` holds one SHA-256 per tool over its name, title,
description, input and output schemas and annotations; a test compares it with what the server lists, so changing any
of them is a deliberate commit: `python -m labdeploy.cli tools-checksum --write` and a reviewed diff of that file.
"""
import hashlib
import inspect
import json
from pathlib import Path

FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
PINS = Path(__file__).resolve().parent.parent / "schema" / "tools.sha256.json"


def canonical(tool):
    """The fields Claude reads, as compact JSON with sorted keys. A description is compared without its docstring
    indentation, which Python 3.13 and later strip and 3.12 keeps, so the same text hashes the same on either."""
    fields = {field: tool.get(field) for field in FIELDS}
    if isinstance(fields["description"], str):
        fields["description"] = inspect.cleandoc(fields["description"])
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksums(tools):
    """{tool name: SHA-256 of its canonical form}, in name order."""
    return {tool["name"]: hashlib.sha256(canonical(tool).encode()).hexdigest() for tool in sorted(tools, key=lambda tool: tool["name"])}


def compare(found, pinned):
    """Tools whose text or schema differs from the pinned file, and tools added or removed since it was written."""
    return {"changed": sorted(name for name in found.keys() & pinned.keys() if found[name] != pinned[name]),
            "added": sorted(found.keys() - pinned.keys()), "removed": sorted(pinned.keys() - found.keys())}


def listed_tools():
    """Every tool as the server lists it to a client: an in-process controller with a throwaway configuration and an
    empty database; nothing is called on GitHub or Google Cloud. The test compares this with tools/list over HTTP."""
    import asyncio
    import tempfile

    from .config import Config
    from .server import build_app
    with tempfile.TemporaryDirectory(prefix="lab-deploy-tools-") as folder:
        config = Config.model_validate({"database": str(Path(folder) / "tools.db"), "applications": {}, "principals": []})
        app = build_app(config, github=object())
        tools = asyncio.run(app.state.mcp.list_tools())
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def pinned(path=None):
    """The committed checksums; an empty mapping when the file does not exist yet."""
    target = Path(path or PINS)
    return json.loads(target.read_text()) if target.is_file() else {}


def write(found, path=None):
    target = Path(path or PINS)
    target.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
    return target
