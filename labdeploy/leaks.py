"""Committed secrets, found by TruffleHog in the checkout and in the built image (docs/ROADMAP.md 14.2).

TruffleHog writes one JSON object per finding and, when it can, verifies the credential against its provider: a verified
finding is a key that works right now. For code nobody has reviewed (D35: trust `unknown`) a verified finding stops the
release; for the lab's own code it is reported loudly, like every unverified finding. The values TruffleHog found never
leave this module: the annotations and the summary name the detector, the file, the line and the layer, nothing else.

    python -m labdeploy.leaks REPORT.jsonl --trust unknown|reviewed --where checkout|image [--strip PREFIX] [--json OUT]
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Where an image holds what the application installed rather than wrote: packages, the system's own files, the buildpacks'
# layers. An unconfirmed finding there is a pattern in someone else's code (found live: URL patterns in httpx, pip and
# pandas tests, two in dpkg's lists); a working key is reported wherever it is.
THIRD_PARTY = ("/usr/", "/var/lib/", "/lib/", "/opt/conda/", "/layers/", "site-packages/", "dist-packages/")


def third_party(path):
    return any(marker in path if marker.endswith("-packages/") else path.startswith(marker) for marker in THIRD_PARTY)


def findings(lines, strip=""):
    """The findings of a TruffleHog JSON report, without the secrets: detector, verified, file, line, layer."""
    found = []
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict) or "DetectorName" not in item:
            continue
        data = ((item.get("SourceMetadata") or {}).get("Data") or {})
        source = next((value for value in data.values() if isinstance(value, dict)), {})
        path = str(source.get("file") or "")
        if strip and path.startswith(strip):
            path = path[len(strip):].lstrip("/")
        entry = {"detector": str(item.get("DetectorName"))[:60], "verified": item.get("Verified") is True, "file": path[:300],
                 "line": source.get("line") if isinstance(source.get("line"), int) else None}
        if source.get("layer"):
            entry["layer"] = str(source["layer"])[:80]
        if item.get("VerificationError"):
            entry["verification"] = "could not be checked"
        if entry not in found:
            found.append(entry)
    return found


def decide(found, trust):
    """(blocking, lines for people): verified findings block code nobody has reviewed."""
    verified = [item for item in found if item["verified"]]
    blocking = bool(verified) and trust == "unknown"
    return blocking, verified


def escape(text, property=False):
    text = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if property else text


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.leaks")
    parser.add_argument("report")
    parser.add_argument("--trust", choices=("reviewed", "unknown"), required=True)
    parser.add_argument("--where", choices=("checkout", "image"), required=True)
    parser.add_argument("--strip", default="", help="a path prefix to remove so files read as the repository names them")
    parser.add_argument("--json", help="write the summary here (no secret values)")
    args = parser.parse_args(argv)
    found = findings(Path(args.report).read_text(errors="replace").splitlines(), args.strip)
    blocking, verified = decide(found, args.trust)
    installed = [item for item in found if not item["verified"] and third_party(item["file"])]
    for item in found:
        if item in installed:
            continue
        where = item["file"] or "(unknown file)"
        kind = "error" if item["verified"] and blocking else "warning" if item["verified"] else "notice"
        state = "a key that works right now" if item["verified"] else "a possible secret, not confirmed"
        place = f"file={escape(where, True)}" + (f",line={item['line']}" if item["line"] else "") if args.where == "checkout" else ""
        print(f"::{kind} {place + ',' if place else ''}title={escape('Secret scan (' + args.where + '): ' + item['detector'], True)}::"
              f"{escape(f'{state} in {where}' + (' (layer ' + item['layer'][:19] + ')' if item.get('layer') else ''))}")
    line = (f"Secret scan of the {args.where} ({args.trust}): {len(found)} finding{'s' if len(found) != 1 else ''}, {len(verified)} verified"
            + (f", {len(installed)} unconfirmed in installed packages or system files" if installed else "")
            + ("; verified keys stop a release of code nobody has reviewed" if blocking else ""))
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(line + "\n")
    if args.json:
        Path(args.json).write_text(json.dumps({"where": args.where, "trust": args.trust, "findings": found, "blocking": blocking,
                                               "installed": len(installed)}, indent=2) + "\n")
    if blocking:
        many = len(verified) != 1
        print(f"::error title=Secret scan::{len(verified)} working credential{'s' if many else ''} in the {args.where}: remove "
              f"{'them' if many else 'it'} from the repository and its history, rotate {'them' if many else 'it'} at the provider, and give "
              "secrets at run time through the contract's secrets", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
