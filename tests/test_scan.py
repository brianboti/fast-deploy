import json

from labdeploy.scan import analysis_status, report_lines, summarize, wait_for_scan

VULNERABILITIES = {"vulnerabilities": {
    "CRITICAL": [{"vulnerability": {"effectiveSeverity": "CRITICAL", "packageIssue": [{"fixedVersion": {"version": "1.2.3"}}]}}],
    "HIGH": [{"vulnerability": {"packageIssue": [{"fixedVersion": {"kind": "MAXIMUM"}}]}}, {"vulnerability": {"fixAvailable": True}}],
    "LOW": [{"vulnerability": {}}],
    "WEIRD": [{"vulnerability": {}}],
}, "not_fixed_vulnerability_count": 2}


def test_summarize_counts_severity_and_fixability():
    summary = summarize(VULNERABILITIES)
    assert summary["total"] == 5 and summary["by_severity"]["CRITICAL"] == 1 and summary["by_severity"]["HIGH"] == 2
    assert summary["by_severity"]["UNKNOWN"] == 1 and summary["fixable_by_severity"] == {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 0, "LOW": 0, "MINIMAL": 0, "UNKNOWN": 0}
    assert summary["not_fixed"] == 2 and summarize({})["total"] == 0


def test_analysis_status_accepts_both_api_shapes():
    assert analysis_status({"discovery_summary": {"discovery": [{"discovery": {"analysisStatus": "FINISHED_SUCCESS"}}]}}) == "FINISHED_SUCCESS"
    assert analysis_status({"discovery_summary": {"discovery": [{"discovered": {"analysisStatus": "SCANNING"}}]}}) == "SCANNING"
    assert analysis_status({}) == "UNKNOWN"


def test_wait_for_scan_polls_until_finished_or_timeout():
    answers = iter([{"discovery_summary": {"discovery": [{"discovery": {"analysisStatus": "SCANNING"}}]}},
                    {"discovery_summary": {"discovery": [{"discovery": {"analysisStatus": "FINISHED_SUCCESS"}}]}, "package_vulnerability_summary": VULNERABILITIES}])
    calls, slept, now = [], [], {"t": 0.0}

    def runner(args):
        calls.append(args)
        assert args[:5] == ["gcloud", "artifacts", "docker", "images", "describe"] and "--show-package-vulnerability" in args
        return json.dumps(next(answers))

    status, description = wait_for_scan("img@sha256:" + "a" * 64, runner, timeout=100, interval=15, sleep=slept.append, clock=lambda: now["t"])
    assert status == "FINISHED_SUCCESS" and len(calls) == 2 and slept == [15] and summarize(description["package_vulnerability_summary"])["total"] == 5

    def never_done(args):
        now["t"] += 60
        return json.dumps({"discovery_summary": {"discovery": [{"discovery": {"analysisStatus": "SCANNING"}}]}})
    status, _ = wait_for_scan("img", never_done, timeout=100, interval=15, sleep=lambda s: None, clock=lambda: now["t"])
    assert status == "SCANNING"


def test_report_lines_state_blocking_policy_and_unfinished_scans():
    summary = {"image": "img", "analysis_status": "SCANNING", "blocking": False, **summarize(VULNERABILITIES)}
    lines = report_lines(summary)
    assert lines[0].endswith("SCANNING") and "critical 1" in lines[1] and any("had not finished" in line for line in lines)
    assert lines[-1].endswith("to block)")
    assert report_lines({**summary, "analysis_status": "FINISHED_SUCCESS", "blocking": True})[-1].endswith("ON")
