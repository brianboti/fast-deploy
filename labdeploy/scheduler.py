"""Background work inside the controller process: auto-deploy, the daily watchdog, nightly backups.

One daemon thread wakes every fifteen seconds and runs whichever job is due. Each job is a plain method on the
controller, so tests call it directly, and every outcome lands in the audit trail and in get_controller_status.
"""
import datetime
import logging
import threading
import time

log = logging.getLogger("labdeploy")


def now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class Scheduler(threading.Thread):
    def __init__(self, controller, config):
        super().__init__(daemon=True, name="lab-deploy-scheduler")
        self.stop_event = threading.Event()
        started = time.time()
        self.jobs = {
            "auto_deploy": (controller.auto_deploy_tick, lambda: config.auto_deploy_interval_seconds, 60),
            "watchdog": (controller.watchdog_tick, lambda: 86_400, 120),
            "backup": (controller.backup_tick, lambda: 86_400, 300),
        }
        self.state = {name: {"next_run": started + first, "last_run": None, "last_result": None, "last_error": None} for name, (_, _, first) in self.jobs.items()}

    def run_job(self, name):
        fn, interval, _ = self.jobs[name]
        entry = self.state[name]
        try:
            result = fn()
            entry.update(last_run=now_iso(), last_result=result, last_error=None)
        except Exception as exc:  # noqa: BLE001 - a failing job must never stop the thread or the controller
            log.exception("background job %s failed", name)
            entry.update(last_run=now_iso(), last_error=f"{type(exc).__name__}: {str(exc)[:300]}")
        entry["next_run"] = time.time() + interval()
        return entry

    def run(self):
        while not self.stop_event.wait(15):
            now = time.time()
            for name in self.jobs:
                if now >= self.state[name]["next_run"]:
                    self.run_job(name)

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        return {name: {**entry, "next_run": datetime.datetime.fromtimestamp(entry["next_run"], datetime.UTC).isoformat(timespec="seconds")} for name, entry in self.state.items()}
