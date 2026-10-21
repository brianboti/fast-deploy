"""Personal data in the data files an image carries, found by Sensitive Data Protection before the image is pushed
(docs/ROADMAP.md 14.10).

The deploy job, which holds the application's cloud identity and never runs its code, reads the built image (the
`docker save` tarball the build job made without credentials), takes every data file of at least 64 KiB outside the
system's and the packages' own folders, and sends the first 256 KiB of each, at most 20 files and 2 MiB a release, to
Sensitive Data Protection's content inspection. What comes back is counted by kind (an email address, a phone number,
a card number); the matched values never leave Google's answer, and none is asked for (`includeQuote` is off).

For code nobody has reviewed (D35: trust `unknown`), a likely government identifier, card or bank number, or medical
record stops the release: such data belongs in a bucket or a database the lab controls, not in an image anyone with the
registry's reader role can pull. For the lab's own code every kind is reported and nothing stops. The first GiB a month
of the billing account is free, then $3 a GiB; at most 2 MiB a release is about 500 releases in the free GiB.

    python -m labdeploy.datafiles IMAGE.tar --project P --region R [--trust unknown|reviewed] [--json OUT]

The access token comes from the environment (GOOGLE_ACCESS_TOKEN), never from an argument.
"""
import argparse
import base64
import json
import os
import sys
import tarfile
from pathlib import PurePosixPath

import httpx

THRESHOLD = 64 * 1024  # smaller files are configuration, not data
SAMPLE = 256 * 1024  # the start of each file; Sensitive Data Protection takes at most 0.5 MB a request
MAX_FILES = 20
MAX_BYTES = 2 * 1024 * 1024
# Text the service reads as data. Binary formats (SQLite, Parquet, pickles, NumPy) are listed, not inspected.
TEXT = {".csv": "CSV", ".tsv": "TSV", ".json": "TEXT_UTF8", ".jsonl": "TEXT_UTF8", ".ndjson": "TEXT_UTF8", ".txt": "TEXT_UTF8",
        ".xml": "TEXT_UTF8", ".sql": "TEXT_UTF8", ".log": "TEXT_UTF8", ".yaml": "TEXT_UTF8", ".yml": "TEXT_UTF8"}
BINARY = {".db", ".sqlite", ".sqlite3", ".parquet", ".pkl", ".pickle", ".npy", ".npz", ".feather", ".xlsx", ".xls", ".h5", ".hdf5"}
# Where an image holds what the application installed rather than wrote (as in labdeploy/leaks.py), and lockfiles.
SYSTEM = ("usr/", "lib/", "lib64/", "bin/", "sbin/", "etc/", "var/", "proc/", "sys/", "dev/", "run/", "opt/conda/", "opt/yarn-",
          "layers/", "root/.cache/", "tmp/")
PACKAGES = ("site-packages/", "dist-packages/", "node_modules/", "__pycache__/", ".git/")
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "pdm.lock", "Pipfile.lock",
             "bun.lock", "composer.lock", "requirements.lock", "tsconfig.json", "package.json"}
# Kinds that stop a release of code nobody has reviewed at LIKELY or above: identifiers that let someone act as a person.
# Every name is listed, since Google's default list "may change over time" (its reference, checked 2026-10-21).
BLOCKING = {"US_SOCIAL_SECURITY_NUMBER", "US_PASSPORT", "US_DRIVERS_LICENSE_NUMBER", "CREDIT_CARD_NUMBER", "IBAN_CODE", "FINANCIAL_ACCOUNT_NUMBER",
            "MEDICAL_RECORD_NUMBER", "US_HEALTHCARE_NPI"}
REPORTED = ("EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON_NAME", "STREET_ADDRESS", "DATE_OF_BIRTH", "IP_ADDRESS")
INFO_TYPES = sorted(BLOCKING) + list(REPORTED)
LIKELY = {"LIKELY", "VERY_LIKELY"}
API = "https://dlp.googleapis.com/v2"


def relative(path):
    """A tar member's name without its leading ``./`` or ``/``, which images write either way."""
    while path.startswith(("./", "/")):
        path = path[1:] if path.startswith("/") else path[2:]
    return path


def candidates(paths):
    """Data files of an image, ``{path: size}``, in the order they are inspected: the largest text files first."""
    found = {}
    for path, size in paths.items():
        relative_path = relative(path)
        name = PurePosixPath(relative_path).name
        suffix = PurePosixPath(name).suffix.lower()
        if size < THRESHOLD or (suffix not in TEXT and suffix not in BINARY) or name in LOCKFILES:
            continue
        if relative_path.startswith(SYSTEM) or any(marker in relative_path for marker in PACKAGES):
            continue
        found[relative_path] = size
    return dict(sorted(found.items(), key=lambda item: (PurePosixPath(item[0]).suffix.lower() in BINARY, -item[1], item[0])))


def image_files(tar_path):
    """The files of a `docker save` tarball as its last layer leaves them: ``{path: (layer index, member name, size)}``.
    Whiteouts (``.wh.`` entries) remove what an earlier layer added."""
    files = {}
    with tarfile.open(tar_path) as outer:
        manifest = json.load(outer.extractfile("manifest.json"))
        for index, layer in enumerate(manifest[0]["Layers"]):
            with tarfile.open(fileobj=outer.extractfile(layer), mode="r:*") as inner:
                for member in inner.getmembers():
                    path = relative(member.name)
                    name = PurePosixPath(path).name
                    if name == ".wh..wh..opq":
                        folder = str(PurePosixPath(path).parent) + "/"
                        files = {key: value for key, value in files.items() if not key.startswith(folder)}
                    elif name.startswith(".wh."):
                        gone = str(PurePosixPath(path).parent / name[4:])
                        files = {key: value for key, value in files.items() if key != gone and not key.startswith(gone + "/")}
                    elif member.isfile():
                        files[path] = (index, member.name, member.size)
    return files


def samples(tar_path, chosen):
    """The first SAMPLE bytes of each chosen text file, within MAX_FILES and MAX_BYTES."""
    files = image_files(tar_path)
    wanted = {path: files[path] for path in chosen if path in files}
    out, total = {}, 0
    with tarfile.open(tar_path) as outer:
        manifest = json.load(outer.extractfile("manifest.json"))
        layers = {}
        for path, (index, member, _) in wanted.items():
            if len(out) >= MAX_FILES or total >= MAX_BYTES:
                break
            if index not in layers:
                layers[index] = tarfile.open(fileobj=outer.extractfile(manifest[0]["Layers"][index]), mode="r:*")
            data = layers[index].extractfile(member).read(min(SAMPLE, MAX_BYTES - total))
            out[path] = data
            total += len(data)
        for layer in layers.values():
            layer.close()
    return out


def inspect(project, region, path, data, token, client=None):
    """Counts by kind of what Sensitive Data Protection finds at LIKELY or above in one sample; values are never returned."""
    kind = TEXT.get(PurePosixPath(path).suffix.lower(), "TEXT_UTF8")
    body = {"item": {"byteItem": {"type": kind, "data": base64.b64encode(data).decode()}},
            "inspectConfig": {"infoTypes": [{"name": name} for name in INFO_TYPES], "minLikelihood": "LIKELY", "includeQuote": False,
                              "limits": {"maxFindingsPerRequest": 1000}}}
    client = client or httpx.Client(timeout=60)
    response = client.post(f"{API}/projects/{project}/locations/{region}/content:inspect", json=body,
                           headers={"Authorization": "Bearer " + token})
    response.raise_for_status()
    counts = {}
    for finding in (response.json().get("result") or {}).get("findings") or []:
        if finding.get("likelihood") in LIKELY:
            name = (finding.get("infoType") or {}).get("name") or "UNKNOWN"
            counts[name] = counts.get(name, 0) + 1
    return counts


def decide(results, trust):
    """(blocking, lines for people) from ``{path: counts}``: likely identifiers stop code nobody has reviewed."""
    lines, blocking = [], False
    for path, counts in results.items():
        if not counts:
            continue
        stops = sorted(name for name in counts if name in BLOCKING)
        words = ", ".join(f"{name.lower().replace('_', ' ')} {count}" for name, count in sorted(counts.items()))
        lines.append(f"{path}: likely personal data in its first {SAMPLE // 1024} KiB ({words})")
        blocking = blocking or (trust == "unknown" and bool(stops))
    if blocking:
        lines.append("Code nobody has reviewed carries identifiers in its image: keep such data in a bucket or a database the lab "
                     "controls (needs.buckets, needs.database), or have the lab review it (trust: reviewed in the contract)")
    return blocking, lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.datafiles", description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--trust", choices=("unknown", "reviewed"), help="default: the contract's (SPEC_FILE, TRUST_DEFAULT), as the scan reads it")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    if args.trust is None:
        from .scan import trust_of
        args.trust = trust_of(os.environ)
    files = image_files(args.image)
    chosen = candidates({path: size for path, (_, _, size) in files.items()})
    text = [path for path in chosen if PurePosixPath(path).suffix.lower() in TEXT]
    binary = [path for path in chosen if PurePosixPath(path).suffix.lower() in BINARY]
    report = {"inspected": {}, "not_inspected": binary, "skipped": [], "blocking": False}
    if text:
        token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
        if not token:
            print("::error title=Data files::no access token for Sensitive Data Protection")
            return 1
        taken = samples(args.image, text)
        report["skipped"] = [path for path in text if path not in taken]
        try:
            for path, data in taken.items():
                report["inspected"][path] = inspect(args.project, args.region, path, data, token)
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            report["error"] = str(status or type(exc).__name__)
            if status in (403, 404):
                # Not set up in this project yet: the operator's state, which the application cannot bring about.
                print("::warning title=Data files::not inspected: Sensitive Data Protection is not set up for this deployer yet (apply the "
                      "foundation, then this application's root; docs/SETUP.md 7t)")
            else:
                print(f"::warning title=Data files::Sensitive Data Protection did not answer ({status or type(exc).__name__}); "
                      "an unfinished inspection stops code nobody has reviewed")
                report["blocking"] = args.trust == "unknown"
    blocking, lines = decide(report["inspected"], args.trust)
    report["blocking"] = report["blocking"] or blocking
    summary = [f"Data files: {len(report['inspected'])} inspected for personal data, {len(binary)} binary not inspected"
               + (f", {len(report['skipped'])} over the release's {MAX_FILES}-file or {MAX_BYTES // (1024 * 1024)} MiB limit" if report["skipped"] else "")]
    summary += lines
    if binary:
        summary.append("Not inspected (binary): " + ", ".join(binary[:5]) + ("" if len(binary) <= 5 else f" and {len(binary) - 5} more"))
    for line in summary:
        print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write("\n".join(["", "### Data files", *summary, ""]))
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=2)
    for line in lines:
        print(f"::{'error' if report['blocking'] else 'warning'} title=Personal data in a data file::{line}")
    return 1 if report["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main())
