"""Background work inside the controller process: auto-deploy, following unfinished releases, the daily watchdog, nightly
backups, what follows a platform release (the machine's update and the re-pins), the report of each release a day later
and the daily digest.

A dispatcher wakes every five seconds, reads the configuration file again if it changed (so a pause or a switch
applies to the jobs without waiting for a request), and submits due work to bounded lanes. Due times and outcomes survive restarts. Each job is a plain method on the
controller, so tests call it directly, and every outcome lands in the audit trail and in get_controller_status.
"""
import datetime
import json
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
            "onboarding": (controller.onboarding_tick, lambda: 60, 30),
            "follow": (controller.follow_tick, lambda: 60, 45),
            "resolver": (controller.resolver_tick, lambda: 60, 35),
            # Contracts the platform keeps (docs/ROADMAP.md D32): release pull requests, and merged contracts not yet released.
            "contracts": (controller.contract_tick, lambda: 60, 50),
            # After a platform release (labdeploy/updates.py): the machine's update, then the re-pins; off without platform_updates.
            "platform_updates": (controller.platform_updates_tick, lambda: 300, 90),
            # After a release (docs/ROADMAP.md Phase 23): each release's report a day later, and the daily digest at its hour.
            "release_reports": (controller.release_report_tick, lambda: 900, 180),
            "digest": (controller.digest_tick, lambda: 300, 240),
            # The jobs applications start (docs/ROADMAP.md D66, D70): the clock that wakes them, and the guard of their limits.
            "wake": (controller.wake_tick, lambda: 60, 20),
            "job_guard": (controller.job_guard_tick, lambda: 60, 25),
            "jobs_base": (controller.base_tick, lambda: 3600, 600),
        }
        self.config = config
        self.store = controller.store
        self.lock = threading.RLock()
        self.running = set()
        self.workers = set()
        # Safety tasks have individual slots; slow reporting/lifecycle work cannot consume them.
        self.lanes = {name: threading.BoundedSemaphore(capacity) for name, capacity in
                      (("follow", 1), ("job_guard", 1), ("backup", 1), ("watchdog", 1), ("lifecycle", 2), ("reporting", 1))}
        self.state = {name: {"next_run": started + first, "last_run": None, "last_result": None, "last_error": None} for name, (_, _, first) in self.jobs.items()}

        with self.store.connect() as db:
            for name, due, payload in db.execute("SELECT name,next_run,payload FROM schedules"):
                if name in self.state:
                    self.state[name].update(json.loads(payload), next_run=due)

    @staticmethod
    def lane(name):
        return name if name in {"follow", "job_guard", "backup", "watchdog"} else "reporting" if name in {"digest", "release_reports"} else "lifecycle"

    def persist(self, name):
        entry = dict(self.state[name])
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO schedules VALUES (?, ?, ?)", (name, entry["next_run"], json.dumps(entry, default=str)))

    def refresh(self):
        """Read the configuration file again when it changed; an invalid one is ignored and reported, as for a request."""
        refresh = getattr(self.config, "refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001 - the previous configuration stays in force
                log.exception("reading the configuration again failed")

    def run_job(self, name):
        with self.lock:
            if name in self.running:
                return dict(self.state[name])
            self.running.add(name)
        fn, interval, _ = self.jobs[name]
        entry = self.state[name]
        try:
            result = fn()
            with self.lock:
                entry.update(last_run=now_iso(), last_result=result, last_error=None)
        except Exception as exc:  # noqa: BLE001 - isolation between scheduled tasks
            log.exception("background job %s failed", name)
            with self.lock:
                entry.update(last_run=now_iso(), last_error=f"{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            with self.lock:
                # Coalesce missed periodic runs. Durable application workflows carry their own progress.
                entry["next_run"] = time.time() + interval()
                try:
                    self.persist(name)
                finally:
                    self.running.discard(name)
        return dict(entry)

    def submit(self, name):
        slot = self.lanes[self.lane(name)]
        with self.lock:
            if self.stop_event.is_set() or name in self.running or any(t.name == "schedule-" + name for t in self.workers):
                return False
            if not slot.acquire(blocking=False):
                return False
            def work():
                try:
                    self.run_job(name)
                finally:
                    slot.release()
                    with self.lock:
                        self.workers.discard(threading.current_thread())
            worker = threading.Thread(target=work, name="schedule-" + name, daemon=True)
            self.workers.add(worker)
            worker.start()
            return True

    def run(self):
        while not self.stop_event.wait(5):
            self.refresh()
            now = time.time()
            for name in sorted(self.jobs, key=lambda n: (self.lane(n) in {"lifecycle", "reporting"}, self.state[n]["next_run"])):
                if now >= self.state[name]["next_run"]:
                    self.submit(name)

    def stop(self, timeout=30):
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        if self.is_alive() and threading.current_thread() is not self:
            self.join(max(0, deadline - time.monotonic()))
        with self.lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))

    def snapshot(self):
        with self.lock:
            return {name: {**entry, "running": name in self.running, "lane": self.lane(name),
                           "lag_seconds": max(0, int(time.time() - entry["next_run"])),
                           "next_run": datetime.datetime.fromtimestamp(entry["next_run"], datetime.UTC).isoformat(timespec="seconds")}
                    for name, entry in self.state.items()}
