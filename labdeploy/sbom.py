"""The image's parts list (docs/ROADMAP.md 14.7): a CycloneDX SBOM that Trivy makes from the image the build saved, attested
next to the provenance and summarised in the release receipt, so "are we affected?" has an answer without a rebuild.

    python -m labdeploy.sbom SBOM.json     one line for the step summary
"""
import hashlib
import json
import os
import sys
from pathlib import Path


def summary(path):
    """{format, spec_version, components, sha256} of a CycloneDX file, or None when there is none or it is not one."""
    file = Path(path)
    if not file.is_file():
        return None
    raw = file.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("bomFormat") != "CycloneDX":
        return None
    return {"format": "CycloneDX", "spec_version": str(data.get("specVersion"))[:10], "components": len(data.get("components") or []),
            "sha256": hashlib.sha256(raw).hexdigest()}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    found = summary(args[0])
    if found is None:
        print(f"::error title=SBOM::{args[0]} is not a CycloneDX parts list", file=sys.stderr)
        return 1
    line = f"Parts list (SBOM): {found['components']} components, CycloneDX {found['spec_version']}, sha256 {found['sha256'][:12]}"
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
