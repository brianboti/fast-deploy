"""The quarterly restore drill (docs/RUNBOOK.md, "Quarterly restore drill"; docs/ROADMAP.md 12.5).

A backup nobody has restored is a hope. The drill downloads the newest controller database copy from the backup bucket's
folder for this host (`backup_prefix`: controller/ for a laptop, machine/ for the machine) with this host's own gcloud
sign-in (the way the nightly backup uploads it) into a temporary folder, opens it read-only, checks its integrity and
counts what it holds, checks that the configuration copy beside it still loads, and records the outcome in the audit
trail as a `restore_drill` event. Where the machine keeps a live copy (`litestream_replica`, docs/ROADMAP.md 15.3), the
drill also restores that one with Litestream and says how far it is behind the database it copies. Nothing is restored
into the live database, which only gains that event; the daily watchdog reminds when no drill has passed for a quarter.
"""
import datetime
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

COPY = re.compile(r"/deployments-(\d{8}-\d{6})\.db$")
TABLES = ("plans", "events", "audit", "applications", "onboardings")
STALE_HOURS = 48  # the backup runs nightly inside the controller; an older newest copy means it stopped
REPLICA_BEHIND_MINUTES = 10  # Litestream uploads every minute while the database changes; more than this means it stalls


class DrillFailed(Exception):
    """The restore would not work today; the message says why and what to do."""


def newest_copy(listing):
    """The newest database copy in a `gcloud storage ls` listing of the bucket's backup folder, by the stamp in its name."""
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


def litestream(run, arguments):
    """One Litestream call; a missing binary or a failed restore becomes a sentence with what to do."""
    try:
        return run(arguments, check=True, capture_output=True, text=True, timeout=600).stdout or ""
    except FileNotFoundError:
        raise DrillFailed("Litestream is not installed here; the live copy is the machine's, so run the drill there: scripts/machine.sh drill") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise DrillFailed(f"Litestream could not restore the live copy: {(reason[-1] if reason else 'no detail')[:300]}") from None
    except subprocess.TimeoutExpired:
        raise DrillFailed("Restoring the live copy took longer than ten minutes; check the network and run the drill again") from None


def newest_event_at(database):
    """When the live database last recorded an audit event, read-only; None when it cannot be read."""
    try:
        db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return db.execute("SELECT MAX(at) FROM events").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        return None


def restore_drill(config, store, run=None, now=None, identity=None):
    """Restore the newest backup into a temporary folder, read-only, and record the outcome. Returns the report."""
    from .config import load_config
    run = run or subprocess.run
    bucket = getattr(config, "backup_bucket", None)
    prefix = getattr(config, "backup_prefix", None) or "controller"
    replica = getattr(config, "litestream_replica", None)
    if not bucket:
        raise ValueError("backup_bucket is not set in the controller configuration, so there is no copy off this machine to restore. Set it to the "
                         "foundation's backup_bucket output (docs/SETUP.md 7e), let the nightly backup run (or python -m labdeploy.cli backup), then drill.")
    now = now or time.time()
    report = {"bucket": bucket, "copy": None, "copy_age_hours": None, "database": None, "configuration": None, "replica": None, "warnings": [], "ok": False, "error": None,
              "checked_at": datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(timespec="seconds")}
    try:
        with tempfile.TemporaryDirectory(prefix="lab-deploy-drill-") as folder:
            found = newest_copy(gcloud(run, ["gcloud", "storage", "ls", f"gs://{bucket}/{prefix}/"]))
            if found is None:
                raise DrillFailed(f"No database copy in gs://{bucket}/{prefix}/: the nightly backup has not uploaded one yet; run python -m labdeploy.cli "
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
                gcloud(run, ["gcloud", "storage", "cp", f"gs://{bucket}/{prefix}/config.yaml", str(configuration)])
            except DrillFailed as exc:
                raise DrillFailed(f"The configuration copy gs://{bucket}/{prefix}/config.yaml could not be fetched ({exc}); the nightly backup uploads "
                                  "it with the database, so run python -m labdeploy.cli backup and the drill again") from None
            try:
                restored = load_config(str(configuration))
            except Exception as exc:  # noqa: BLE001 - any parse or validation failure means the restore would stop here
                raise DrillFailed(f"The configuration copy does not load: {str(exc)[:300]}") from None
            report["configuration"] = {"principals": len(restored.principals), "applications": len(restored.applications),
                                       "backup_bucket": restored.backup_bucket}
            if replica:
                live = Path(folder) / "replica.db"
                litestream(run, ["litestream", "restore", "-integrity-check", "quick", "-o", str(live), replica])
                restored_replica = {**inspect_copy(live), "bytes": live.stat().st_size, "url": replica}
                current = newest_event_at(config.database)
                if current is not None and restored_replica["newest_event_at"] is not None:
                    restored_replica["behind_minutes"] = round(max(0.0, current - restored_replica["newest_event_at"]) / 60, 1)
                    if restored_replica["behind_minutes"] > REPLICA_BEHIND_MINUTES:
                        report["warnings"].append(f"The live copy is {restored_replica['behind_minutes']:.0f} minutes behind the database: Litestream may be "
                                                  "stalled (scripts/machine.sh logs litestream)")
                report["replica"] = restored_replica
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
    if report.get("replica"):
        behind = report["replica"].get("behind_minutes")
        text += f"; live copy restored ({report['replica']['counts']['plans']} plans" + (f", {behind} min behind)" if behind is not None else ")")
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
    if report.get("replica"):
        replica, behind = report["replica"], report["replica"].get("behind_minutes")
        out.append(f"  live copy {replica['url']} restored with Litestream, integrity {replica['integrity']}: {replica['counts']['plans']} plans"
                   + (f", {behind} minutes behind the database" if behind is not None else ""))
    out += [f"  warning: {warning}" for warning in report["warnings"]]
    out.append(f"Recorded as audit event restore_drill (ok). Next drill due by {due:%Y-%m-%d}.")
    return out
