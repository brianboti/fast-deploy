"""The bake: watch a promoted revision for a few minutes; a misbehaving one gets the previous traffic back.

Runs on the deployment runner inside the release step, after traffic has moved and the post-promotion probe passed. It
reads only Cloud Run's own logs for the new revision (the deployer holds the Logging viewer role) and sends no traffic of
its own. The rules are the operator's decision D22 in docs/ROADMAP.md:

- a crash: Cloud Run's system log shows the revision's container exiting, being stopped for memory or a signal, or a new
  instance failing its startup probe; any system entry at ERROR or above counts too;
- errors: at least MIN_REQUESTS requests other than the health path, and more than ERROR_RATE of them answered 5xx;
- latency: at least MIN_REQUESTS such requests in the window, at least MIN_REQUESTS for the previous revisions in the hour
  before the switch, and the new p95 above LATENCY_FACTOR times the previous one.

Too little traffic is inconclusive and passes with a note; logs that cannot be read leave the bake unobserved, which also
passes, loudly, because restoring a release that passed its probes on the strength of a monitoring outage would do more
harm than good. The health path is left out of both rates: the uptime check and the platform's probes call it every minute.
"""
import datetime
import math
import re
import time

MIN_REQUESTS = 20
ERROR_RATE = 0.05
LATENCY_FACTOR = 3.0
BASELINE_SECONDS = 3600
POLL_SECONDS = 60
MAX_BAKE_SECONDS = 900
DEFAULT_BAKE_SECONDS = 300
# Messages Cloud Run writes to a revision's system log when its container stops serving. "Container called exit" is a
# WARNING (observed in staging, 2026-10-14), so severity alone would miss it; sandbox notices are warnings and are ignored.
CRASH = re.compile(r"Container called exit|Container terminated|Memory limit of .* exceeded|Uncaught signal|"
                   r"STARTUP (HTTP|TCP|GRPC) probe failed|LIVENESS (HTTP|TCP|GRPC) probe failed|instance was terminated", re.IGNORECASE)
SEVERE = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}


class BakeFailed(RuntimeError):
    """The promoted revision broke a bake rule; `report` holds the window's numbers for the receipt."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def why(error):
    """A command's ERROR line when there is one (gcloud says what was refused there), else the error itself."""
    lines = [line.strip() for line in (getattr(error, "stderr", "") or "").splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("ERROR")), lines[-1] if lines else f"{type(error).__name__}: {error}")[:200]


def iso(moment):
    return datetime.datetime.fromtimestamp(moment, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def p95(values):
    """Nearest-rank 95th percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def request_stats(requests, health_path):
    """Counts over request log lines, the health path apart: what users and programs saw, not what the probes saw."""
    served = [r for r in requests if r.get("path") != health_path]
    health = [r for r in requests if r.get("path") == health_path]
    latencies = [r["latency_ms"] for r in served if isinstance(r.get("latency_ms"), (int, float))]
    errors = sum(1 for r in served if isinstance(r.get("status"), int) and r["status"] >= 500)
    statuses = {}
    for r in served:
        key = f"{r['status'] // 100}xx" if isinstance(r.get("status"), int) else "unknown"
        statuses[key] = statuses.get(key, 0) + 1
    return {"requests": len(served), "errors_5xx": errors, "error_rate": round(errors / len(served), 4) if served else None,
            "p95_ms": None if p95(latencies) is None else round(p95(latencies), 1), "statuses": statuses,
            "health_requests": len(health), "health_errors_5xx": sum(1 for r in health if isinstance(r.get("status"), int) and r["status"] >= 500)}


def crashes(system_lines):
    """The system log lines that mean the revision's container stopped serving."""
    found = []
    for line in system_lines:
        message = str(line.get("message") or "")
        if line.get("severity") in SEVERE or CRASH.search(message):
            found.append({"at": line.get("at"), "severity": line.get("severity"), "message": message[:300]})
    return found


def judge(stats, baseline, crash_lines, final):
    """The verdict on what was observed so far. Errors and crashes stop the bake at once; latency is judged at the end."""
    if crash_lines:
        first = crash_lines[0]
        return {"result": "failed", "rule": "crash", "reason": f"the new revision's container stopped serving ({first['message'][:160]})"}
    requests, errors = stats["requests"], stats["errors_5xx"]
    if requests >= MIN_REQUESTS and errors / requests > ERROR_RATE:
        return {"result": "failed", "rule": "errors",
                "reason": f"{errors} of {requests} requests answered 5xx ({errors / requests:.0%}, above {ERROR_RATE:.0%})"}
    if not final:
        return {"result": "watching"}
    notes = []
    if stats["health_errors_5xx"]:
        notes.append(f"the health path answered 5xx {stats['health_errors_5xx']} time(s) during the bake")
    if requests < MIN_REQUESTS:
        notes.append(f"only {requests} request(s) besides the health path during the bake (at least {MIN_REQUESTS} are needed to judge "
                     "errors and latency); the release passed its probes, nothing more could be judged")
        return {"result": "inconclusive", "notes": notes}
    old = (baseline or {}).get("p95_ms")
    if (baseline or {}).get("requests", 0) >= MIN_REQUESTS and old and stats["p95_ms"] is not None and stats["p95_ms"] > LATENCY_FACTOR * old:
        return {"result": "failed", "rule": "latency",
                "reason": f"p95 latency {stats['p95_ms']:.0f} ms is more than {LATENCY_FACTOR:g} times the previous revision's {old:.0f} ms"}
    if not old or (baseline or {}).get("requests", 0) < MIN_REQUESTS:
        notes.append("latency was not compared: the previous revision served fewer than "
                     f"{MIN_REQUESTS} requests besides the health path in the hour before the switch")
    return {"result": "passed", "notes": notes}


def bake(adapter, revision, previous, health_path, seconds, switched_at, sleep=time.sleep, clock=time.time, interval=POLL_SECONDS, share=None):
    """Watch `revision` for `seconds` after `switched_at`; raise BakeFailed on a broken rule, else return the report.

    `adapter` reads logs: `revision_requests(revisions, since, until)` and `revision_system_logs(revision, since)`.
    `share`: the percent of the traffic the revision holds at a step of a split release (docs/ROADMAP.md D38), which the
    failure names; None when it serves everyone.
    """
    if not 0 <= int(seconds) <= MAX_BAKE_SECONDS:
        raise ValueError(f"bake_seconds must be between 0 and {MAX_BAKE_SECONDS}")
    report = {"seconds": int(seconds), "since": iso(switched_at), "rules": {"min_requests": MIN_REQUESTS, "error_rate": ERROR_RATE,
              "latency_factor": LATENCY_FACTOR, "baseline_seconds": BASELINE_SECONDS}, "checks": 0}
    if not seconds:
        report.update(result="skipped", notes=["bake_seconds is 0 for this application: nothing watched the new revision after its final probe"])
        return report
    try:
        report["baseline"] = request_stats(adapter.revision_requests(sorted(previous), switched_at - BASELINE_SECONDS, switched_at), health_path)
    except Exception as error:  # noqa: BLE001 - without a baseline only the latency rule is lost
        report["baseline"] = None
        report["baseline_warning"] = f"previous revisions' requests unavailable: {why(error)}"
    deadline = switched_at + seconds
    failures = 0
    while True:
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
        final = clock() >= deadline
        try:
            stats = request_stats(adapter.revision_requests([revision], switched_at, None), health_path)
            crash_lines = crashes(adapter.revision_system_logs(revision, switched_at))
        except Exception as error:  # noqa: BLE001 - a log read failing once is retried at the next check
            failures += 1
            report["read_warning"] = f"logs unavailable: {why(error)}"
            if final:
                report.update(result="unobserved", until=iso(clock()),
                              notes=["the bake could not read the revision's logs, so it judged nothing; check the deployer's Logging viewer role"])
                return report
            continue
        report["checks"] += 1
        verdict = judge(stats, report.get("baseline"), crash_lines, final)
        report.update(window=stats, crashes=crash_lines[:5], until=iso(clock()))
        if verdict["result"] == "failed":
            report.update(result="failed", rule=verdict["rule"], reason=verdict["reason"])
            moment = f"the switch to {share} percent of the traffic" if share and share < 100 else "promotion"
            raise BakeFailed(f"Bake failed {int(clock() - switched_at)} seconds after {moment}: {verdict['reason']}", report)
        if final:
            report.update(result=verdict["result"], notes=verdict.get("notes", []))
            if failures:
                report["notes"].append(f"{failures} log read(s) failed during the bake and were retried")
            return report


def summary(report):
    """One plain line for the receipt reader and the notification."""
    if not report:
        return None
    result = report.get("result")
    window = report.get("window") or {}
    if result == "passed":
        return f"bake passed: {window.get('requests', 0)} requests in {report['seconds']} s, {window.get('errors_5xx', 0)} answered 5xx, p95 {window.get('p95_ms')} ms"
    if result == "failed":
        return f"bake failed: {report.get('reason')}"
    if result == "inconclusive":
        return f"bake inconclusive: {window.get('requests', 0)} requests in {report['seconds']} s, too few to judge"
    if result == "unobserved":
        return "bake unobserved: the revision's logs could not be read"
    if result == "skipped":
        return "bake skipped (bake_seconds: 0)"
    return None
