"""Vulnerability summary for the image just pushed, from Artifact Analysis. Runs on the deployment runner only.

Reporting is always on when scanning is enabled in Terraform. Blocking a release on critical findings is a
policy the operator turns on with the BLOCK_ON_CRITICAL_VULNERABILITIES repository variable.
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


def summarize(package_summary):
    """Counts by effective severity, plus how many of each have a fix available."""
    groups = package_summary.get("vulnerabilities") or {}
    counts = {severity: 0 for severity in SEVERITIES}
    fixable = {severity: 0 for severity in SEVERITIES}
    for severity, items in groups.items():
        bucket = severity if severity in counts else "UNKNOWN"
        counts[bucket] += len(items or [])
        fixable[bucket] += sum(1 for item in items or [] if fix_available(item))
    return {"total": sum(counts.values()), "by_severity": counts, "fixable_by_severity": fixable,
            "not_fixed": package_summary.get("not_fixed_vulnerability_count")}


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
    lines.append("Release blocking on critical findings is " + ("ON" if summary["blocking"] else "OFF (set BLOCK_ON_CRITICAL_VULNERABILITIES=true to block)"))
    return lines


def main():
    from .release import run
    image = os.environ["IMAGE_DIGEST"]
    blocking = os.environ.get("BLOCK_ON_CRITICAL_VULNERABILITIES", "false").lower() == "true"
    status, description = wait_for_scan(image, run)
    summary = {"image": image, "analysis_status": status, "blocking": blocking,
               **summarize(description.get("package_vulnerability_summary") or {})}
    Path("vulnerability-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    lines = report_lines(summary)
    print("\n".join(lines))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(lines[1:]) + "\n")
    if blocking and status != "FINISHED_SUCCESS":
        sys.exit("Vulnerability scan did not finish and blocking is enabled")
    if blocking and summary["by_severity"]["CRITICAL"]:
        sys.exit(f"{summary['by_severity']['CRITICAL']} critical vulnerabilities found and blocking is enabled")


if __name__ == "__main__":
    main()
