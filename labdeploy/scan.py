"""Vulnerability summary for the image just pushed, from Artifact Analysis. Runs on the deployment runner only.

Reporting is always on when scanning is enabled in Terraform. The gate (docs/ROADMAP.md 14.4), in the industry's wording:
for code nobody has reviewed (D35: trust `unknown`) a fixable critical or high finding, or a scan that did not finish, stops
the release; for the lab's own code, blocking on critical findings is the operator's switch, the
BLOCK_ON_CRITICAL_VULNERABILITIES repository variable. A known malicious package (OSV's MAL- advisories, which Artifact
Analysis reports as critical) stops every release.
"""
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


def main():
    from .release import run
    image = os.environ["IMAGE_DIGEST"]
    blocking = os.environ.get("BLOCK_ON_CRITICAL_VULNERABILITIES", "false").lower() == "true"
    trust = trust_of(os.environ)
    # Code nobody has reviewed waits longer for its scan, since an unfinished scan stops its release.
    status, description = wait_for_scan(image, run, timeout=300 if trust == "unknown" else 180)
    summary = {"image": image, "analysis_status": status, "blocking": blocking or trust == "unknown", "trust": trust,
               **summarize(description.get("package_vulnerability_summary") or {})}
    Path("vulnerability-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = report_lines(summary)
    print("\n".join(lines))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(lines[1:]) + "\n")
    reason = verdict(summary, trust, blocking)
    if reason:
        sys.exit(reason[0].upper() + reason[1:])


if __name__ == "__main__":
    main()
