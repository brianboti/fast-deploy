"""Vulnerability summary for the image just pushed, from Artifact Analysis. Runs on the deployment runner only.

Reporting is always on when scanning is enabled in Terraform. The gate (docs/ROADMAP.md 14.4), in the industry's wording:
for code nobody has reviewed (D35: trust `unknown`) a fixable critical or high finding, or a scan that did not finish, stops
the release; for the lab's own code, blocking on critical findings is the operator's switch, the
BLOCK_ON_CRITICAL_VULNERABILITIES repository variable. A known malicious package (OSV's MAL- advisories, which Artifact
Analysis reports as critical) stops every release.

`python -m labdeploy.scan osv REPORT --exit-code N --lockfile LOCK --json OUT` is a Node.js service's dependency audit
(D44), run in the build job before any of its packages is installed: OSV-Scanner's JSON for its npm lockfile, held to the
same words for every trust level. A known malicious package, a critical or high finding with a fix, an audit that did not
finish, or a lockfile entry npm ci would install without a registry check (D42) stops the release; the rest is listed.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "MINIMAL", "UNKNOWN")
FINISHED = {"FINISHED_SUCCESS", "FINISHED_FAILED", "FINISHED_UNSUPPORTED"}


def fix_available(occurrence):
    details = occurrence.get("vulnerability") or {}
    if details.get("fixAvailable"):
        return True
    for issue in details.get("packageIssue") or []:
        fixed = issue.get("fixedVersion") or {}
        if issue.get("fixAvailable") or (fixed.get("version") and fixed.get("kind", "NORMAL") == "NORMAL"):
            return True
    return False


def malicious(occurrence):
    """Whether a finding is a known malicious package rather than a vulnerability: OSV's MAL- advisories."""
    details = occurrence.get("vulnerability") or {}
    names = (occurrence.get("noteName") or "", details.get("shortDescription") or "", occurrence.get("name") or "")
    return any("MAL-" in str(name) for name in names)


def summarize(package_summary):
    """Counts by effective severity, how many of each have a fix available, and the malicious packages."""
    groups = package_summary.get("vulnerabilities") or {}
    counts = {severity: 0 for severity in SEVERITIES}
    fixable = {severity: 0 for severity in SEVERITIES}
    bad = []
    for severity, items in groups.items():
        bucket = severity if severity in counts else "UNKNOWN"
        counts[bucket] += len(items or [])
        fixable[bucket] += sum(1 for item in items or [] if fix_available(item))
        bad += [str((item.get("vulnerability") or {}).get("shortDescription") or item.get("noteName") or "?")[:80] for item in items or [] if malicious(item)]
    return {"total": sum(counts.values()), "by_severity": counts, "fixable_by_severity": fixable,
            "not_fixed": package_summary.get("not_fixed_vulnerability_count"), "malicious": bad}


def verdict(summary, trust, blocking_critical):
    """Why the release stops, or None: the gate of docs/ROADMAP.md 14.4 for this trust level."""
    if summary.get("malicious"):
        return f"{len(summary['malicious'])} known malicious package{'s' if len(summary['malicious']) != 1 else ''} ({', '.join(summary['malicious'][:3])})"
    if trust == "unknown":
        if summary["analysis_status"] != "FINISHED_SUCCESS":
            return "the vulnerability scan did not finish, and code nobody has reviewed is released only with a finished scan"
        fixable = summary["fixable_by_severity"]["CRITICAL"] + summary["fixable_by_severity"]["HIGH"]
        if fixable:
            return (f"{fixable} critical or high finding{'s' if fixable != 1 else ''} with a fix available, in code nobody has reviewed: update "
                    "the base image or the packages, or have the lab review it (trust: reviewed in the contract)")
        return None
    if blocking_critical and summary["analysis_status"] != "FINISHED_SUCCESS":
        return "Vulnerability scan did not finish and blocking is enabled"
    if blocking_critical and summary["by_severity"]["CRITICAL"]:
        return f"{summary['by_severity']['CRITICAL']} critical vulnerabilities found and blocking is enabled"
    return None


def analysis_status(description):
    for occurrence in (description.get("discovery_summary") or {}).get("discovery") or []:
        details = occurrence.get("discovery") or occurrence.get("discovered") or {}
        if details.get("analysisStatus"):
            return details["analysisStatus"]
    return "UNKNOWN"


def describe(image, runner):
    raw = runner(["gcloud", "artifacts", "docker", "images", "describe", image, "--show-package-vulnerability", "--format=json"])
    return json.loads(raw or "{}")


def wait_for_scan(image, runner, timeout=180, interval=15, sleep=time.sleep, clock=time.monotonic):
    """Poll until the scan finishes or the timeout passes; the status is reported either way."""
    deadline = clock() + timeout
    while True:
        description = describe(image, runner)
        status = analysis_status(description)
        if status in FINISHED or clock() >= deadline:
            return status, description
        sleep(interval)


def report_lines(summary):
    counts = summary["by_severity"]
    lines = [f"Vulnerability scan for {summary['image']}: {summary['analysis_status']}",
             f"critical {counts['CRITICAL']}, high {counts['HIGH']}, medium {counts['MEDIUM']}, low {counts['LOW']}; "
             f"with a fix available: critical {summary['fixable_by_severity']['CRITICAL']}, high {summary['fixable_by_severity']['HIGH']}"]
    if summary["analysis_status"] != "FINISHED_SUCCESS":
        lines.append("The scan had not finished when this release ran; check Artifact Registry for the final result.")
    if summary.get("trust") == "unknown":
        lines.append("Code nobody has reviewed: fixable critical and high findings, an unfinished scan and malicious packages stop the release")
    else:
        lines.append("Release blocking on critical findings is " + ("ON" if summary["blocking"] else "OFF (set BLOCK_ON_CRITICAL_VULNERABILITIES=true to block)"))
    if summary.get("malicious"):
        lines.append("Known malicious packages, which stop every release: " + ", ".join(summary["malicious"][:5]))
    return lines


def trust_of(env):
    """The trust level of the release's code: the contract's `trust`, else what an unset one means where it lives."""
    from .models import containment, parse_spec
    try:
        spec = parse_spec(Path(env.get("SPEC_FILE", "bundle/platform.yaml")).read_text())
    except (OSError, ValueError):
        return env.get("TRUST_DEFAULT", "reviewed") if env.get("TRUST_DEFAULT") in ("reviewed", "unknown") else "reviewed"
    return containment(spec, env.get("TRUST_DEFAULT", "reviewed"))["trust"]


# ---- a Node.js service's dependency audit (docs/ROADMAP.md D44)
OSV_SCANNER = "OSV-Scanner 2.7.0"


def osv_severity(group, advisories):
    """CRITICAL, HIGH, MEDIUM, LOW or UNKNOWN for one finding: OSV-Scanner's max_severity (a CVSS score, as its own table
    counts it), else the GitHub advisory's own word."""
    try:
        score = float(group.get("max_severity") or "")
    except (TypeError, ValueError):
        score = None
    if score is not None:
        return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW" if score > 0 else "UNKNOWN"
    words = {str((item.get("database_specific") or {}).get("severity") or "").upper() for item in advisories}
    return next((level for word, level in (("CRITICAL", "CRITICAL"), ("HIGH", "HIGH"), ("MODERATE", "MEDIUM"), ("MEDIUM", "MEDIUM"), ("LOW", "LOW"))
                 if word in words), "UNKNOWN")


def osv_fixed(advisories, package):
    """The versions that fix a finding for this package, read from the advisories' affected ranges."""
    fixed = set()
    for advisory in advisories:
        for affected in advisory.get("affected") or []:
            named = affected.get("package") or {}
            if named.get("name") != package.get("name") or str(named.get("ecosystem") or "").lower() != "npm":
                continue
            for span in affected.get("ranges") or []:
                fixed |= {str(event["fixed"]) for event in span.get("events") or [] if isinstance(event, dict) and event.get("fixed")}
    return sorted(fixed)


def osv_findings(report):
    """One entry per finding (OSV-Scanner groups advisories that alias each other): {id, aliases, package, version,
    severity, fixed, malicious}. A MAL- record is a known malicious package, and so is an advisory GitHub files as
    embedded malicious code (CWE-506, "Malicious Package in ..."), which may have no MAL- record and no fixed version."""
    findings = []
    for result in (report or {}).get("results") or []:
        for item in result.get("packages") or []:
            package = item.get("package") or {}
            advisories = {str(entry.get("id")): entry for entry in item.get("vulnerabilities") or [] if isinstance(entry, dict)}
            groups = item.get("groups") or [{"ids": [identifier]} for identifier in advisories]
            for group in groups:
                ids = [str(identifier) for identifier in group.get("ids") or []]
                members = [advisories[identifier] for identifier in ids if identifier in advisories]
                aliases = sorted(set(ids) | set(group.get("aliases") or []) | {str(alias) for entry in members for alias in entry.get("aliases") or []})
                malicious = any(alias.startswith("MAL-") for alias in aliases) or any(
                    "CWE-506" in ((entry.get("database_specific") or {}).get("cwe_ids") or []) for entry in members)
                findings.append({"id": next((alias for alias in aliases if alias.startswith("MAL-")), ids[0] if ids else "?"), "aliases": aliases[:6],
                                 "package": package.get("name"), "version": package.get("version"), "malicious": malicious,
                                 "severity": "CRITICAL" if malicious else osv_severity(group, members), "fixed": osv_fixed(members, package)})
    return findings


def osv_summary(report, exit_code, lock_text, lock_name="package-lock.json"):
    """What the audit found and whether it finished. OSV-Scanner exits 0 with no findings, 1 with findings, and 128 when it
    found no packages, which counts as finished only for a lockfile that lists none; anything else did not finish. The
    lockfile is npm's, pnpm's, Yarn's or Bun's (21.1), read into the same view and checked entry by entry (D42)."""
    from .node import read_lock
    from .node import view_problems as lock_problems
    data, _ = read_lock(lock_name or "package-lock.json", lock_text or "")
    packages = sum(1 for path in (data or {}).get("packages", {}) if path)
    finished = exit_code in (0, 1) and isinstance(report, dict) or (exit_code == 128 and data is not None and packages == 0)
    findings = osv_findings(report) if finished and isinstance(report, dict) else []
    counts = {severity: sum(1 for item in findings if item["severity"] == severity) for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")}
    fixable = {severity: sum(1 for item in findings if item["severity"] == severity and item["fixed"]) for severity in ("CRITICAL", "HIGH")}
    malicious = {}  # one entry per package, named by its MAL- record when it has one: a package can carry several advisories
    for item in findings:
        key = f"{item['package']}@{item['version']}"
        if item["malicious"] and (key not in malicious or (item["id"].startswith("MAL-") and not malicious[key].startswith("MAL-"))):
            malicious[key] = item["id"]
    return {"tool": OSV_SCANNER, "status": "finished" if finished else "did not finish", "exit_code": exit_code, "packages": packages,
            "by_severity": counts, "fixable_by_severity": fixable,
            "malicious": [f"{ident} ({key})" for key, ident in malicious.items()],
            "lock_problems": [f"{name} {why}" for name, why in (lock_problems(data) if data else [])][:20],
            "lockfile": "read" if data is not None else "unreadable", "findings": findings[:50]}


def osv_verdict(summary):
    """Why the release stops, or None: the gate of 14.4's words, for every trust level (D44)."""
    if summary["malicious"]:
        count = len(summary["malicious"])
        return f"{count} known malicious package{'s' if count != 1 else ''} in the lockfile ({', '.join(summary['malicious'][:3])})"
    if summary["lockfile"] != "read":
        return "the lockfile could not be read (npm's version 2 or 3, pnpm's 6 or 9, Yarn's, or Bun's text format), so its packages could not be checked"
    if summary["status"] != "finished":
        return f"the dependency audit did not finish ({summary['tool']} exited {summary['exit_code']}), and a release needs a finished audit"
    if summary["lock_problems"]:
        return "the lockfile holds packages npm ci would install without checking them against the npm registry: " + "; ".join(summary["lock_problems"][:3])
    fixable = summary["fixable_by_severity"]["CRITICAL"] + summary["fixable_by_severity"]["HIGH"]
    if fixable:
        named = [f"{item['package']}@{item['version']} ({item['id']}, fixed in {item['fixed'][0]})" for item in summary["findings"]
                 if item["severity"] in ("CRITICAL", "HIGH") and item["fixed"]]
        return (f"{fixable} critical or high finding{'s' if fixable != 1 else ''} with a fix available: " + ", ".join(named[:3])
                + "; update those packages (npm install <package>@<fixed version>) and commit the lockfile")
    return None


def osv_main(argv):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.scan osv")
    parser.add_argument("report", help="OSV-Scanner's --format json output")
    parser.add_argument("--exit-code", type=int, required=True, help="OSV-Scanner's exit code")
    parser.add_argument("--lockfile", required=True, help="the npm lockfile that was scanned")
    parser.add_argument("--json", help="write the summary here (the release bundle)")
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.report).read_text())
    except (OSError, ValueError):
        report = None
    lock = Path(args.lockfile).read_text() if Path(args.lockfile).is_file() else ""
    summary = osv_summary(report, args.exit_code, lock, Path(args.lockfile).name)
    reason = osv_verdict(summary)
    summary["blocked"] = reason is not None
    counts, fixable = summary["by_severity"], summary["fixable_by_severity"]
    lines = [f"Dependency audit ({summary['tool']}) of {Path(args.lockfile).name}: {summary['status']}, {summary['packages']} packages",
             f"critical {counts['CRITICAL']}, high {counts['HIGH']}, medium {counts['MEDIUM']}, low {counts['LOW']}; "
             f"with a fix available: critical {fixable['CRITICAL']}, high {fixable['HIGH']}",
             "Stops every release: a known malicious package, a critical or high finding with a fix, an audit that did not finish, "
             "a package from outside the npm registry"]
    lines += [f"{item['severity'].lower()}: {item['package']}@{item['version']} {item['id']}" + (f", fixed in {item['fixed'][0]}" if item["fixed"] else ", no fix yet")
              for item in summary["findings"][:10]]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(f"- {line}" for line in lines[1:]) + "\n")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2) + "\n")
    if reason:
        print(f"::error title=Dependency audit::{reason}")
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["osv"]:
        return osv_main(argv[1:])
    from .release import run
    image = os.environ["IMAGE_DIGEST"]
    blocking = os.environ.get("BLOCK_ON_CRITICAL_VULNERABILITIES", "false").lower() == "true"
    trust = trust_of(os.environ)
    # Code nobody has reviewed waits longer for its scan, since an unfinished scan stops its release.
    status, description = wait_for_scan(image, run, timeout=300 if trust == "unknown" else 180)
    summary = {"image": image, "analysis_status": status, "blocking": blocking or trust == "unknown", "trust": trust,
               **summarize(description.get("package_vulnerability_summary") or {})}
    # A job's image (docs/ROADMAP.md 27.3) writes its own summary beside the service's.
    output = Path(os.environ.get("SUMMARY_FILE") or "vulnerability-summary.json")
    output.write_text(json.dumps(summary, indent=2) + "\n")
    lines = report_lines(summary)
    print("\n".join(lines))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(lines[1:]) + "\n")
    reason = verdict(summary, trust, blocking)
    summary["blocked"] = reason is not None
    output.write_text(json.dumps(summary, indent=2) + "\n")
    if reason:
        sys.exit(reason[0].upper() + reason[1:])


if __name__ == "__main__":
    sys.exit(main())
