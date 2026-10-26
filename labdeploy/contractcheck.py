"""Offline contract admission. Inputs are data; no repository code or dependency hooks run."""
import argparse
import json
import re
import sys
from pathlib import Path

from .models import enforce, parse_contract
from .node import lock_problems, read_lockfile

MAX_LOCK_BYTES = 4 * 1024 * 1024
LOCK_NAMES = ("requirements.lock", "requirements-test.lock", "package-lock.json")
PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;*]+(?:\s*;\s*[^\r\n]+)?$")
HASH = re.compile(r"\s+--hash=sha256:[a-f0-9]{64}(?=\s|$)")


def read_file(path, root, limit):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("symbolic links and paths outside the registry are refused")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"missing file or file exceeds {limit} bytes")
    return path.read_text(encoding="utf-8")


def python_lock(text):
    """Accept fixed registry requirements with SHA-256 hashes, including environment markers."""
    logical, pending = [], ""
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        pending += " " + line.removesuffix("\\").rstrip()
        if not line.endswith("\\"):
            logical.append(pending.strip())
            pending = ""
    if pending:
        raise ValueError("unfinished lockfile continuation")
    for number, line in enumerate(logical, 1):
        hashes = HASH.findall(line)
        requirement = HASH.sub("", line).strip()
        if not hashes or not PIN.fullmatch(requirement) or " --" in requirement or "://" in requirement or " @ " in requirement:
            raise ValueError(f"requirement {number} must pin a registry version and valid SHA-256 hashes")


def validate(root, previous=None):
    root = Path(root)
    findings, contracts = [], []
    if not root.is_dir() or root.is_symlink():
        return {"contracts": [], "errors": [{"file": str(root), "control": "POL-002", "reason": "contract registry is missing or a symbolic link"}]}
    paths = sorted(root.glob("*/platform.yaml"))
    for path in paths:
        try:
            spec, source = parse_contract(read_file(path, root, 32_768))
            if spec.name != path.parent.name:
                raise ValueError("name must equal the contract directory")
            enforce(spec, "gcp", "staging")
            for name in LOCK_NAMES:
                lock = path.parent / name
                if not lock.exists() and not lock.is_symlink():
                    continue
                text = read_file(lock, root, MAX_LOCK_BYTES)
                if name.endswith(".json"):
                    data, problem = read_lockfile(text)
                    if data is None:
                        raise ValueError(f"{name}: invalid npm lockfile ({problem})")
                    if lock_problems(data):
                        raise ValueError(f"{name}: packages must have approved registry sources and integrity hashes")
                else:
                    python_lock(text)
            contracts.append({"name": spec.name, "repository": source.repository, "commit": source.commit})
        except (ValueError, OSError, UnicodeError) as error:
            # Validation errors identify fields, never interpolate the entire submitted file.
            findings.append({"file": str(path), "control": "POL-002", "reason": str(error)[:1000]})
    # A contract cannot disappear unnoticed. Decommissioning first records a reviewed retirement marker.
    if previous is not None:
        previous = Path(previous)
        old = {path.parent.name for path in previous.glob("*/platform.yaml")}
        current = {path.parent.name for path in paths}
        for name in sorted(old - current):
            marker = root / "retired" / f"{name}.json"
            try:
                data = json.loads(read_file(marker, root, 8192))
                if not isinstance(data, dict) or data.get("service") != name or not all(isinstance(data.get(k), str) and data[k].strip()
                                                                                      for k in ("owner", "reason", "evidence")):
                    raise ValueError("invalid retirement record")
            except (ValueError, OSError, UnicodeError):
                findings.append({"file": str(marker), "control": "POL-002",
                                 "reason": "deletion/rename requires a reviewed retirement record with service, owner, reason and evidence"})
    return {"contracts": contracts, "errors": findings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("apps"))
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    result = validate(args.root, args.previous)
    print(json.dumps(result, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
