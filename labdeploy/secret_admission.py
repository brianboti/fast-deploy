"""Fresh secret admission for reused/promoted images. Scanner input is never executed."""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import leaks, pins

IMAGE = re.compile(r"^[a-z0-9.-]+/[a-zA-Z0-9_./-]+@sha256:[a-f0-9]{64}$")


def install(folder):
    pin = pins.read()["trufflehog"]
    url = f"https://github.com/trufflesecurity/trufflehog/releases/download/v{pin['version']}/trufflehog_{pin['version']}_linux_amd64.tar.gz"
    with httpx.Client(timeout=60, follow_redirects=True) as client, client.stream("GET", url) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 256 * 1024 * 1024:
                raise ValueError("Scanner download exceeds its size limit")
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError("Scanner checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember("trufflehog")
        if not member.isfile() or member.size > 512 * 1024 * 1024:
            raise ValueError("Invalid scanner executable")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("Scanner executable missing")
        executable = folder / "trufflehog"
        executable.write_bytes(source.read())
        executable.chmod(0o700)
    return executable, pin["version"]


def scan(scanner, arguments, folder, where, runner=subprocess.run):
    raw = folder / "scan.jsonl"
    # Stderr can contain a credential or provider response: never stream it into Actions logs.
    with raw.open("w") as output, tempfile.TemporaryFile() as errors:
        runner([str(scanner), *arguments, "--results=verified,unknown,unverified", "--json", "--no-update"],
               stdout=output, stderr=errors, check=True, timeout=600)
    safe = folder / "safe.json"
    try:
        code = leaks.main([str(raw), "--trust", "unknown", "--where", where, "--json", str(safe)])
        if code:
            raise ValueError("Secret admission refused; inspect the sanitized finding summary")
        return json.loads(safe.read_text())
    finally:
        raw.unlink(missing_ok=True)


def admit(*, source=None, images=(), output, commit, spec_hash, runner=subprocess.run, installer=install):
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[a-f0-9]{64}", spec_hash):
        raise ValueError("Secret admission requires the exact commit and specification hash")
    if source is None and not images:
        raise ValueError("No admission input")
    if any(not IMAGE.fullmatch(image) for image in images):
        raise ValueError("Secret admission requires immutable image digests")
    with tempfile.TemporaryDirectory(prefix="secret-admission-") as temporary:
        folder = Path(temporary)
        scanner, version = installer(folder)
        checks = []
        if source is not None:
            source = Path(source)
            if not source.is_dir() or source.is_symlink():
                raise ValueError("Source checkout is missing")
            excludes = folder / "exclude"
            excludes.write_text("(^|/)\\.git/\n")
            result = scan(scanner, ["filesystem", str(source), "--exclude-paths", str(excludes)], folder, "checkout", runner)
            checks.append({"kind": "checkout", "result": result})
        for image in images:
            archive = folder / "image.tar"
            runner(["docker", "pull", image], check=True, capture_output=True, timeout=600)
            runner(["docker", "save", image, "-o", str(archive)], check=True, capture_output=True, timeout=600)
            result = scan(scanner, ["docker", f"--image=file://{archive}"], folder, "image", runner)
            checks.append({"kind": "image", "image": image, "result": result})
            archive.unlink(missing_ok=True)
        evidence = {"policy_version": leaks.POLICY_VERSION, "commit": commit, "spec_hash": spec_hash, "scanner": version,
                    "at": datetime.now(UTC).isoformat(), "checks": checks}
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, indent=2) + "\n")
        return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        images = list(args.image)
        if args.jobs and args.jobs.exists():
            data = json.loads(args.jobs.read_text())
            if not isinstance(data, dict) or len(data) > 4 or not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid job image manifest")
            images.extend(data.values())
        admit(source=args.source, images=images, output=args.output, commit=os.environ.get("COMMIT_SHA", ""), spec_hash=os.environ.get("SPEC_HASH", ""))
    except (KeyError, ValueError, OSError, httpx.HTTPError, subprocess.SubprocessError, tarfile.TarError):
        print("::error title=Secret admission::Required fresh scan failed or refused the release; no raw scanner output is published", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
