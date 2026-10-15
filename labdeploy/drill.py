"""The quarterly restore drill (docs/RUNBOOK.md, "Quarterly restore drill"; docs/ROADMAP.md 12.5).

A backup nobody has restored is a hope. The drill downloads the newest controller database copy from the backup bucket
with the operator's own gcloud sign-in (the way the nightly backup uploads it) into a temporary folder, opens it
read-only, checks its integrity and counts what it holds, checks that the configuration copy beside it still loads, and
records the outcome in the audit trail as a `restore_drill` event. Nothing is restored into the live database, which
only gains that event; the daily watchdog reminds when no drill has passed for a quarter.
"""
import datetime
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

COPY = re.compile(r"/controller/deployments-(\d{8}-\d{6})\.db$")
TABLES = ("plans", "events", "audit", "applications", "onboardings")
STALE_HOURS = 48  # the backup runs nightly inside the controller; an older newest copy means it stopped


class DrillFailed(Exception):
    """The restore would not work today; the message says why and what to do."""


def newest_copy(listing):
    """The newest database copy in a `gcloud storage ls` listing of the bucket's controller folder, by the stamp in its name."""
    found = sorted((match.group(1), line.strip()) for line in listing.splitlines() if (match := COPY.search(line.strip())))
    return found[-1] if found else None


def inspect_copy(path):
    """Open a database copy read-only, check it, and count what it holds. Raises DrillFailed when it is not usable."""
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DrillFailed(f"The copy does not open as a database: {exc}") from None
    try:
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [table for table in TABLES if table not in present]
        if missing:
            raise DrillFailed(f"The copy lacks the table(s) {', '.join(missing)}: it is not a controller database of this version")
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}  # noqa: S608 - fixed table names
        newest = db.execute("SELECT MAX(at) FROM events").fetchone()[0]
    except sqlite3.Error as exc:
        raise DrillFailed(f"The copy could not be read: {exc}") from None
    finally:
        db.close()
    if integrity != "ok":
        raise DrillFailed(f"The copy fails SQLite's integrity check: {integrity}")
    return {"integrity": integrity, "counts": counts, "newest_event_at": newest}


def gcloud(run, arguments):
    """One gcloud call as the operator; a refusal or a missing gcloud becomes a sentence with what to do."""
    try:
        return run(arguments, check=True, capture_output=True, text=True, timeout=300).stdout or ""
    except FileNotFoundError:
        raise DrillFailed("gcloud is not installed or not on PATH; install the Google Cloud CLI and sign in with gcloud auth login") from None
    except subprocess.CalledProcessError as exc:
        reason = next((line for line in (exc.stderr or "").splitlines() if line.startswith("ERROR")), (exc.stderr or "").strip()[-300:])
        raise DrillFailed(f"{' '.join(arguments[:3])} failed: {reason or 'no detail'}. Sign in as the operator (gcloud auth login) and check "
                          "that you may read the backup bucket.") from None
    except subprocess.TimeoutExpired:
        raise DrillFailed(f"{' '.join(arguments[:3])} took longer than five minutes; check the network and run the drill again") from None


def restore_drill(config, store, run=None, now=None, identity=None):
    """Restore the newest backup into a temporary folder, read-only, and record the outcome. Returns the report."""
    from .config import load_config
    run = run or subprocess.run
    bucket = getattr(config, "backup_bucket", None)
    if not bucket:
        raise ValueError("backup_bucket is not set in the controller configuration, so there is no copy off this machine to restore. Set it to the "
                         "foundation's backup_bucket output (docs/SETUP.md 7e), let the nightly backup run (or python -m labdeploy.cli backup), then drill.")
    now = now or time.time()
    report = {"bucket": bucket, "copy": None, "copy_age_hours": None, "database": None, "configuration": None, "warnings": [], "ok": False, "error": None,
              "checked_at": datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(timespec="seconds")}
    try:
        with tempfile.TemporaryDirectory(prefix="lab-deploy-drill-") as folder:
            found = newest_copy(gcloud(run, ["gcloud", "storage", "ls", f"gs://{bucket}/controller/"]))
            if found is None:
                raise DrillFailed(f"No database copy in gs://{bucket}/controller/: the nightly backup has not uploaded one yet; run python -m labdeploy.cli "
                                  "backup, then the drill again")
            stamp, source = found
            report["copy"] = source
            taken = datetime.datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=datetime.UTC).timestamp()
            report["copy_age_hours"] = round((now - taken) / 3600, 1)
            if report["copy_age_hours"] > STALE_HOURS:
                report["warnings"].append(f"The newest copy is {report['copy_age_hours']:.0f} hours old: the nightly backup may have stopped (it runs inside the "
                                          "controller); run python -m labdeploy.cli backup and check get_controller_status")
            local = Path(folder) / source.rsplit("/", 1)[1]
            gcloud(run, ["gcloud", "storage", "cp", source, str(local)])
            report["database"] = {**inspect_copy(local), "bytes": local.stat().st_size}
            configuration = Path(folder) / "config.yaml"
            try:
                gcloud(run, ["gcloud", "storage", "cp", f"gs://{bucket}/controller/config.yaml", str(configuration)])
            except DrillFailed as exc:
                raise DrillFailed(f"The configuration copy gs://{bucket}/controller/config.yaml could not be fetched ({exc}); the nightly backup uploads "
                                  "it with the database, so run python -m labdeploy.cli backup and the drill again") from None
            try:
                restored = load_config(str(configuration))
            except Exception as exc:  # noqa: BLE001 - any parse or validation failure means the restore would stop here
                raise DrillFailed(f"The configuration copy does not load: {str(exc)[:300]}") from None
            report["configuration"] = {"principals": len(restored.principals), "applications": len(restored.applications),
                                       "backup_bucket": restored.backup_bucket}
        report["ok"] = True
    except DrillFailed as exc:
        report["error"] = str(exc)
    detail = summary(report)
    store.record_event("restore_drill", name=(report["copy"] or "").rsplit("/", 1)[-1] or None, identity=identity,
                       outcome="ok" if report["ok"] else "failed", detail=detail)
    return report


def summary(report):
    """One line for the audit trail."""
    if not report["ok"]:
        return f"failed: {report['error']}"
    counts = report["database"]["counts"]
    text = (f"restored {report['copy'].rsplit('/', 1)[-1]} ({report['copy_age_hours']} h old): {counts['plans']} plans, {counts['events']} audit events, "
            f"{counts['applications']} registered applications; configuration with {report['configuration']['principals']} principal(s)")
    return text + (f"; {len(report['warnings'])} warning(s)" if report["warnings"] else "")


def lines(report, next_due_days=92):
    """The short report the command prints."""
    if not report["ok"]:
        return [f"Restore drill FAILED: {report['error']}", "Recorded as audit event restore_drill (failed); the watchdog keeps reminding until a drill passes."]
    database, counts = report["database"], report["database"]["counts"]
    due = datetime.datetime.fromisoformat(report["checked_at"]) + datetime.timedelta(days=next_due_days)
    out = [f"Restore drill passed: {report['copy']} ({database['bytes'] / 1_000_000:.1f} MB, {report['copy_age_hours']} hours old)",
           f"  opened read-only, integrity {database['integrity']}: {counts['plans']} plans, {counts['events']} audit events, "
           f"{counts['audit']} plan state changes, {counts['applications']} registered applications, {counts['onboardings']} onboarding records",
           f"  configuration copy loads: {report['configuration']['principals']} principal(s), {report['configuration']['applications']} application(s) in the file"]
    out += [f"  warning: {warning}" for warning in report["warnings"]]
    out.append(f"Recorded as audit event restore_drill (ok). Next drill due by {due:%Y-%m-%d}.")
    return out
