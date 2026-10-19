"""What follows a platform release on main by itself (docs/RUNBOOK.md, "After a release"), once `platform_updates` is in the
controller's configuration. These are the two chores that were done by hand after every merge into the platform repository.

1. The machine. Main's own checks (Platform tests, the run the merge itself started) must have passed. The change must
   then have been on main for `delay_minutes` (a newer change to what the controller runs starts that delay again), and
   nobody may have used the tools for `quiet_minutes`. Then the controller asks the machine to install main by writing
   the commit into a request file next to its database. The controller cannot restart
   itself: its service runs without privileges, by design. A small service of the machine's own does it instead. That
   service runs as root, a request file starts it (fast-deploy-update.path, written by the machine's startup script), and
   it installs the commit with fast-deploy-install. It takes only a commit on main, never one older than the commit that
   runs. It checks the controller's health for a minute, then for two more, and puts the previous commit back on a failure.
   It leaves its result in a folder only root can write. The controller reads that result once and records it. Main moves
   with every contract release (`apps/`) and every document as well, and a change outside what the controller runs never
   restarts it.
2. The workflows. Once the controller has run a commit for `delay_minutes`, every application whose workflows pin an older
   runner gets the pull request that re-pins its three workflow files to that commit (update_app_workflows). Any older one
   of these pull requests is closed. The controller merges the pull request itself only where it merges an application's
   own pull requests today: the application's auto_merge allows it, the pull request's checks have passed, and it changes
   exactly those three files as the platform rendered them. Otherwise it waits for a person, and a pull request a person
   closes is not opened again for that commit.

One change at a time: first the controller, then the applications once it has run the new commit for a while. A runner is
never rolled out ahead of a controller that could not read what it reports. Nothing here moves platform.minimum_commit.
Every step lands in the audit trail (`platform_update`, `workflow_update`) and in get_controller_status.
"""
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path

from .github import checks_verdict, explain
from .workflows import pinned_commit

IDENTITY = "platform-updates"
# Platform tests (.github/workflows/ci.yml): main's protection requires its test and terraform jobs.
CHECKS_WORKFLOW = "ci.yml"
# What the controller runs from its release folder: its code, the templates it renders, the tool checksums it checks at
# start and its locked dependencies. A change anywhere else (documents, contracts under apps/, scripts, infrastructure)
# changes nothing the machine runs.
CONTROLLER_PATHS = ("labdeploy/", "templates/", "schema/", "requirements.lock")
# What an application's workflows run from the platform commit they pin: templates/deploy-gcp.yml checks the platform out
# at that commit, installs its locks and runs these modules, which import nothing else of the platform's. The test suite
# works this list out again from the templates, so a module that starts to import another one fails a test until it is
# named here. A change to the controller's own modules alone (server, chat, onboarding, this one) re-pins nothing.
RUNNER_MODULES = ("analyzer", "bake", "cost", "dockerpolicy", "journal", "leaks", "models", "node", "provenance", "reader", "release", "sbom",
                  "scaffold", "scan", "target", "telemetry")
RUNNER_PATHS = ("templates/", "requirements.lock", "tests.lock", "audit.lock", *(f"labdeploy/{name}.py" for name in RUNNER_MODULES))
REQUEST = "update-request"  # next to the controller's database, the one folder the controller may write
RESULT = Path("/var/lib/fast-deploy-update/result.json")  # root's folder: the controller reads it, never writes it
UNIT = Path("/etc/systemd/system/fast-deploy-update.path")
OUTCOMES = ("installed", "rolled_back", "failed", "not_installed", "refused", "current")
# That code did not run well, or the machine needs a person: it is not asked for again, nor is a later commit of main that
# leaves what the controller runs as it was.
WENT_BACK = ("rolled_back", "failed")
RETRY_SECONDS = 1800  # nothing changed (GitHub away, the controller held, another install running): asked for again after this
STALE_REQUEST_SECONDS = 1800
TRUSTED_AFTER_SECONDS = 7200  # an install the machine never reported on counts as done once the controller has run it this long
REPIN_FLOOR_MINUTES = 10  # never re-pin while the machine may still be watching a new commit, whatever the delay
MERGEABLE = ("clean", "unstable", "has_hooks")
# Settled for one target commit: nothing more to do for that application until the controller runs another commit.
SETTLED = ("current", "merged", "closed", "ahead", "no_workflows")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def running_commit(package_file=None):
    """The commit this controller runs, read from the release folder the machine installs it into
    (/opt/fast-deploy/releases/<commit>); None anywhere else, such as a laptop's checkout."""
    folder = Path(package_file or __file__).resolve().parent.parent
    return folder.name if folder.parent.name == "releases" and COMMIT.fullmatch(folder.name) else None


def touches(files, paths):
    return any(name == path or name.startswith(path) for name in files for path in paths)


def stamp(at):
    return datetime.datetime.fromtimestamp(at, datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def seconds(text):
    """An ISO time from GitHub or the machine as seconds since the epoch; None when it is not one."""
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class PlatformUpdates:
    def __init__(self, controller, running=None, started=None, result_path=RESULT, unit_path=UNIT, clock=time.time):
        self.c, self.clock = controller, clock
        self.running = running if running is not None else running_commit()
        self.started = started if started is not None else clock()
        self.result_path, self.unit_path = Path(result_path), Path(unit_path)

    @property
    def settings(self):
        return getattr(self.c.config, "platform_updates", None)

    @property
    def request_path(self):
        return Path(self.c.config.database).resolve().parent / REQUEST

    def event(self, kind, outcome, detail, application=None, name=None):
        self.c.store.record_event(kind, name=name or application, identity=IDENTITY, application=application, outcome=outcome, detail=detail)

    # ---- one pass of the scheduler
    def tick(self):
        settings, platform = self.settings, getattr(self.c.config, "platform", None)
        if settings is None:
            return {"state": "off"}
        if self.c.config.paused:
            return {"state": "paused"}
        if platform is None:
            return {"state": "not_configured", "note": "platform.repository is not set, so main cannot be followed"}
        if platform.commit:
            return {"state": "pinned", "note": f"platform.commit pins the platform to {platform.commit[:12]}; updates follow main only when it is unset"}
        if self.running is None:
            return {"state": "not_on_the_machine", "note": "this controller does not run from a release folder of the machine, so it neither installs main "
                                                          "nor re-pins applications to its own commit"}
        self.collect_result()
        self.note_running()
        return {"machine": self.guarded("machine", self.machine_step) if settings.machine else "off",
                "workflows": self.guarded("workflows", self.workflows_step) if settings.workflows else "off"}

    def guarded(self, part, step):
        """Run one part of the pass; its failure is recorded once and never stops the other part."""
        try:
            outcome = step()
        except Exception as exc:  # noqa: BLE001 - the next pass tries again; the trail and the status keep the reason
            message = (explain(exc) or str(exc))[:300]
            state = self.c.store.platform_update("machine") or {}
            if state.get(f"error_{part}") != message:
                self.event("platform_update", "error", f"{part}: {message}")
                self.save_machine({f"error_{part}": message})
            return f"error: {message[:160]}"
        if (self.c.store.platform_update("machine") or {}).get(f"error_{part}"):
            self.save_machine({f"error_{part}": None})
        return outcome

    def note_running(self):
        """One event whenever the controller finds itself on another commit, whoever installed it (by hand, or the machine).
        What it worked out about main against the commit it ran is forgotten; an automatic install of this very commit
        stays outstanding until the machine reports on its watch."""
        state = self.c.store.platform_update("machine") or {}
        if state.get("runs") != self.running:
            self.event("platform_update", "running", f"the controller runs {self.running[:12]}" + (f", was {state['runs'][:12]}" if state.get("runs") else ""),
                       name=self.running[:12])
            requested = state.get("requested") if state.get("requested") == self.running else None
            self.save_machine({"runs": self.running, "requested": requested, "head": None, "checks": None, "passed_at": None, "relation": None,
                               "relevant": None, "since": None, "blocked_checked": None, "blocked_by": None, "retry": None})

    # ---- the machine
    def collect_result(self):
        """Record what the machine's update service left in its result file, once; returns that result or None."""
        try:
            data = json.loads(self.result_path.read_text()[:4096])
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not COMMIT.fullmatch(str(data.get("commit"))) or data.get("outcome") not in OUTCOMES:
            return None
        state = self.c.store.platform_update("machine") or {}
        seen = f"{data['commit']} {data.get('finished_at')}"
        if state.get("result_seen") == seen:
            return data
        result = {key: str(data.get(key) or "")[:300] for key in ("commit", "previous", "outcome", "detail", "started_at", "finished_at")}
        detail = f"{result['commit'][:12]} (was {result['previous'][:12] or 'nothing'}): {result['detail']}"
        self.event("platform_update", result["outcome"], detail, name=result["commit"][:12])
        listed = "went_back" if result["outcome"] in WENT_BACK else "refused" if result["outcome"] == "refused" else None
        if listed:
            state[listed] = [*[commit for commit in state.get(listed, []) if commit != result["commit"]], result["commit"]][-20:]
            state["blocked_checked"] = None  # worked out again against the longer list
        if result["outcome"] == "not_installed":
            state["retry"] = {"commit": result["commit"], "after": self.clock() + RETRY_SECONDS, "why": result["detail"]}
        state.update(result_seen=seen, last_result=result)
        if state.get("requested") == result["commit"]:
            state["requested"] = None
        self.c.store.save_platform_update("machine", state)
        return data

    def pending_request(self):
        path = self.request_path
        try:
            since = path.lstat().st_mtime
            commit = path.read_text()[:100].strip()
        except OSError:
            return None
        return {"commit": commit, "since": since}

    def write_request(self, commit):
        """The one line the machine's update service waits for, written whole: a temporary file renamed into place."""
        temporary = self.request_path.with_name(REQUEST + ".writing")
        temporary.write_text(commit + "\n")
        os.replace(temporary, self.request_path)

    def busy(self):
        """When the controller may restart, if not now: someone is using it, or a release is on its way to GitHub."""
        now, quiet = self.clock(), self.settings.quiet_minutes * 60
        for kind, outcome in (("tool", None), ("rest", None), ("sign_in", "ok")):  # a refused sign-in from anywhere postpones nothing
            used = self.c.store.newest_event(kind, outcome)
            if used and now - used["at"] < quiet:
                return f"at a quiet moment: the tools were last used at {stamp(used['at'])}, and it waits {self.settings.quiet_minutes} minutes after that"
        if self.c.store.dispatching(now):
            return "once the release being dispatched right now has reached GitHub"
        return None

    def main_checks(self, head):
        """("passed" | "wait" | "failed", when they passed, why) for the run of main's checks that the push of `head` started."""
        repository = self.c.config.platform.repository
        runs = self.c.github.push_runs(repository, CHECKS_WORKFLOW, head)
        if not runs:
            return "wait", None, f"GitHub to start main's checks (Platform tests) on {head[:12]}"
        newest = runs[0]
        if newest.get("status") != "completed":
            return "wait", None, f"main's checks on {head[:12]} to finish: {newest.get('url')}"
        if newest.get("conclusion") != "success":
            return "failed", None, f"main's checks on {head[:12]} ended {newest.get('conclusion')}: {newest.get('url')}"
        return "passed", seconds(newest.get("updated_at")) or self.clock(), None

    def machine_step(self):
        """One look at main; returns what happens next, in a sentence (also kept for get_controller_status)."""
        settings, platform, github = self.settings, self.c.config.platform, self.c.github
        now, running = self.clock(), self.running
        pending = self.pending_request()
        if pending:
            late = "; its update service has not taken it yet (scripts/machine.sh logs update)" if now - pending["since"] > STALE_REQUEST_SECONDS else ""
            return self.next(f"the machine installs {pending['commit'][:12]}, asked for at {stamp(pending['since'])}{late}")
        state = self.c.store.platform_update("machine") or {}
        requested = state.get("requested")
        if requested and now - state["requested_at"] > TRUSTED_AFTER_SECONDS:
            # The machine never reported (its service stopped by its own timeout, say): the controller runs that commit for
            # two hours now, so it counts as installed, or it does not, and the commit is asked for again when it is due.
            if requested == running:
                self.event("platform_update", "installed", f"{running[:12]}: the machine reported nothing, and the controller has run it for two hours",
                           name=running[:12])
            else:
                self.event("platform_update", "not_installed", f"{requested[:12]}: the machine reported nothing for two hours; it is asked for again",
                           name=requested[:12])
            state, requested = self.save_machine({"requested": None}), None
        if requested:  # one request at a time: nothing else is asked for until the machine reports on this one
            late = "; nothing reported for over an hour (scripts/machine.sh logs update)" if now - state["requested_at"] > 2 * STALE_REQUEST_SECONDS else ""
            return self.next(f"the machine installs {requested[:12]}, asked for at {stamp(state['requested_at'])}{late}")
        head = github.branch_head(platform.repository, platform.branch)
        if head == running:
            return self.next(f"nothing: the controller runs the tip of main, {head[:12]}", head=head, checks=None)
        if state.get("head") != head:
            # Main moved: its checks start again for the new tip, and so does the delay, unless the new commits leave what
            # the controller runs alone (a contract release or a document landing behind a release keeps the release's clock).
            since = None
            if state.get("head") and state.get("since") and state.get("relevant"):
                step = github.comparison(platform.repository, state["head"], head)
                if step["status"] == "ahead" and step["complete"] and not touches(step["files"], CONTROLLER_PATHS):
                    since = state["since"]
            state = self.save_machine({"head": head, "seen_at": now, "checks": None, "passed_at": None, "relation": None, "relevant": None, "since": since,
                                       "blocked_checked": None, "blocked_by": None})
        if state.get("checks") != "passed":
            verdict, passed_at, why = self.main_checks(head)
            if verdict == "failed" and state.get("checks") != "failed":
                self.event("platform_update", "checks_failed", why, name=head[:12])
            state = self.save_machine({"checks": verdict, "passed_at": passed_at})
            if verdict == "wait":
                return self.next(f"waits for {why}")
            if verdict == "failed":
                return self.next(f"waits for a newer commit on main: {why}")
        if state.get("relevant") is None:
            comparison = github.comparison(platform.repository, running, head)
            relevant = comparison["status"] == "ahead" and (not comparison["complete"] or touches(comparison["files"], CONTROLLER_PATHS))
            state = self.save_machine({"relation": comparison["status"], "relevant": relevant})
        if state["relation"] != "ahead":
            return self.next(f"nothing: main ({head[:12]}) is {state['relation']} of the commit that runs ({running[:12]}), and the machine never goes back")
        if not state["relevant"]:
            return self.next(f"nothing: the controller keeps {running[:12]}, since nothing it runs changed up to main ({head[:12]})")
        if head in state.get("refused", []):
            return self.next(f"waits for a newer commit on main: the machine refused {head[:12]} (the last result says why)")
        blocked = self.blocked_by(state, head)
        if blocked == head:
            return self.next(f"waits for a newer commit on main: {head[:12]} went back on the machine (the last result says why)")
        if blocked:
            return self.next(f"waits for a change on main to what the controller runs: {head[:12]} runs the same controller code as {blocked[:12]}, which went back")
        retry = state.get("retry") or {}
        if retry.get("commit") == head and now < retry["after"]:
            return self.next(f"asks for {head[:12]} again after {stamp(retry['after'])}: {retry['why']}")
        if not state.get("since"):  # the delay counts from when main's checks first passed on this change to the controller
            state = self.save_machine({"since": state["passed_at"]})
        due = state["since"] + settings.delay_minutes * 60
        if now < due:
            return self.next(f"installs {head[:12]} after {stamp(due)}, unless another change to what the controller runs lands on main first")
        busy = self.busy()
        if busy:
            return self.next(f"installs {head[:12]} {busy}")
        if not self.unit_path.exists():
            return self.next(f"would install {head[:12]}, but the machine's update service is not installed yet: apply the foundation, then run "
                             "scripts/machine.sh startup (docs/RUNBOOK.md, \"After a release\")")
        self.write_request(head)
        self.event("platform_update", "requested", f"{head[:12]} (the controller runs {running[:12]}; main's checks passed at {stamp(state['passed_at'])})",
                   name=head[:12])
        return self.next(f"the machine installs {head[:12]}, asked for at {stamp(now)}", requested=head, requested_at=now)

    def blocked_by(self, state, head):
        """A commit that went back on the machine whose controller code `head` still carries unchanged (itself, or a contract
        release or a document on top of it), or None; worked out once for each tip of main."""
        if state.get("blocked_checked") == head:
            return state.get("blocked_by")
        found = None
        for commit in reversed(state.get("went_back", [])):
            if commit == head:
                found = commit
                break
            comparison = self.c.github.comparison(self.c.config.platform.repository, commit, head)
            if comparison["status"] == "ahead" and comparison["complete"] and not touches(comparison["files"], CONTROLLER_PATHS):
                found = commit
                break
        self.save_machine({"blocked_checked": head, "blocked_by": found})
        return found

    def save_machine(self, changes):
        state = {**(self.c.store.platform_update("machine") or {}), **changes}
        self.c.store.save_platform_update("machine", state)
        return state

    def next(self, sentence, **changes):
        self.save_machine({**changes, "next": sentence})
        return sentence

    # ---- the applications' workflows
    def workflows_step(self):
        if not self.c.config.live_dispatch:
            return "writes are disabled (live_dispatch: false): no workflows are re-pinned"
        target, now = self.running, self.clock()
        machine = self.c.store.platform_update("machine") or {}
        if machine.get("requested") or self.pending_request():
            return f"waits: the machine installs {str(machine.get('requested') or 'a new commit')[:12]} first"
        due = self.started + max(self.settings.delay_minutes, REPIN_FLOOR_MINUTES) * 60
        results = {}
        for app_id, app in sorted(self.c.config.applications.items()):
            if app.central:
                continue  # the platform repository's own pipeline releases it at main: nothing is pinned
            key = f"workflows:{app_id}"
            record = self.c.store.platform_update(key) or {}
            try:
                onboarding = self.c.store.onboarding(app_id)
                if app.paused:
                    results[app_id] = "paused"
                elif onboarding and onboarding.get("state") != "complete":
                    results[app_id] = f"onboarding ({onboarding.get('state')}) adds its workflows"
                elif record.get("target") == target and record.get("state") in SETTLED:
                    results[app_id] = record["state"]
                elif record.get("target") == target and record.get("state") == "open":
                    results[app_id] = self.advance(app_id, app, record)["state"]
                elif now < due:
                    results[app_id] = f"waits until {stamp(due)}: the controller runs {target[:12]} for a while first"
                else:
                    results[app_id] = self.evaluate(app_id, app, target, record)["state"]
            except Exception as exc:  # noqa: BLE001 - one application's trouble must not stop the others; the next pass looks again
                message = (explain(exc) or str(exc))[:300]
                if record.get("error") != message:
                    self.event("workflow_update", "error", message, application=app_id)
                self.c.store.save_platform_update(key, {**record, "error": message})
                results[app_id] = f"error: {message[:160]}"
        return results

    def evaluate(self, app_id, app, target, previous):
        """Whether this application needs its workflows re-pinned to `target`, and the pull request when it does; `previous`
        is its record for an earlier commit, whose re-pin a newer one replaces."""
        platform, github = self.c.config.platform, self.c.github
        found = github.contents(app.repository, app.branch, f".github/workflows/{app.workflow}")
        pin = pinned_commit(found["text"], [platform.repository, *platform.former_repositories]) if found else None
        current_name = found is not None and pinned_commit(found["text"], [platform.repository]) == pin
        record = {"target": target, "from": pin, "checked_at": self.clock()}
        if pin is None:
            return self.settle(app_id, {**record, "state": "no_workflows",
                                        "note": f"{app.workflow} pins no commit of {platform.repository}: onboarding, or update_app_workflows, adds the workflows"})
        if pin == target and current_name:
            return self.settle(app_id, {**record, "state": "current", "note": None})
        if pin != target:
            comparison = github.comparison(platform.repository, pin, target)
            if comparison["status"] == "behind":
                return self.settle(app_id, {**record, "state": "ahead", "note": f"pins {pin[:12]}, newer than the controller's {target[:12]}; left as it is"})
            if comparison["status"] == "ahead" and comparison["complete"] and current_name and not touches(comparison["files"], RUNNER_PATHS):
                return self.settle(app_id, {**record, "state": "current", "note": f"keeps {pin[:12]}: nothing its workflows use changed up to {target[:12]}"})
        superseded = self.close_superseded(app, previous, target)
        files = self.c.rendered_workflows(app, target)
        pull_request = github.update_workflows(app, target, files)
        if pull_request.get("number") is None:
            return self.settle(app_id, {**record, "state": "current", "note": pull_request.get("note")})
        record.update(state="open", pull_request=pull_request["number"], url=pull_request["url"], opened_at=self.clock(), superseded=superseded,
                      files={path: digest(text) for path, text in files.items()}, waiting=None, merged_by=None, note=None)
        self.event("workflow_update", "opened", f"#{pull_request['number']} pins {target[:12]} (was {pin[:12]})"
                   + (f"; closed the older #{superseded}" if superseded else ""), application=app_id)
        return self.advance(app_id, app, self.settle(app_id, record))

    def settle(self, app_id, record):
        record = {**record, "error": None}
        self.c.store.save_platform_update(f"workflows:{app_id}", record)
        return record

    def close_superseded(self, app, previous, target):
        """Close the re-pin this controller opened for an earlier commit, if it is still open: the newer one replaces it. A
        pull request someone else opened is never closed."""
        number = previous.get("pull_request")
        if previous.get("state") != "open" or not number or previous.get("target") == target:
            return None
        if self.c.github.pull_request(app.repository, number)["state"] != "open":
            return None
        self.c.github.close_pull_request(app.repository, number, f"Superseded: Fast Deploy re-pins these workflows to platform `{target[:12]}` instead, a newer release.")
        return number

    def advance(self, app_id, app, record):
        """Look at an open re-pin pull request again: merged or closed by someone, or mergeable by the controller now."""
        github, number = self.c.github, record["pull_request"]
        pr = github.pull_request(app.repository, number)
        if pr["merged"]:
            record.update(state="merged", merged_by=pr.get("merged_by"), waiting=None)
            self.event("workflow_update", "merged", f"#{number} by {pr.get('merged_by') or 'someone'}: pins {record['target'][:12]}", application=app_id)
        elif pr["state"] == "closed":
            record.update(state="closed", waiting=None, note=f"#{number} was closed: not opened again for {record['target'][:12]}")
            self.event("workflow_update", "closed", f"#{number} was closed without a merge; the next platform release opens a new one", application=app_id)
        else:
            waiting = self.merge_gate(app_id, app, record, pr)
            if waiting is None:
                # Exactly the head the gate read: GitHub refuses the merge if anything was pushed in between.
                merged = github.merge_pull_request(app.repository, number, f"Update Fast Deploy workflows to platform {record['target'][:12]}", sha=pr["head_sha"])
                if merged.get("merged"):
                    record.update(state="merged", merged_by="the controller", waiting=None)
                    self.event("workflow_update", "merged", f"#{number} by the controller: pins {record['target'][:12]}", application=app_id)
                else:
                    waiting = f"a person: GitHub refused the merge of {pr['url']}: {merged.get('reason') or 'no reason given'}"
            if waiting is not None:
                if waiting.startswith("a person") and not str(record.get("waiting") or "").startswith("a person"):
                    self.event("workflow_update", "waiting", waiting, application=app_id)
                record["waiting"] = waiting
        return self.settle(app_id, record)

    def merge_gate(self, app_id, app, record, pr):
        """None when the controller may merge this re-pin itself, else what it waits for. The gate of an application's own
        pull requests (onboarding's and the release pull requests'): its auto_merge, its checks, a clean merge; and the pull
        request holds exactly the three files the platform rendered."""
        github, number = self.c.github, record["pull_request"]
        onboarding = self.c.config.onboarding
        allowed = app.auto_merge if app.auto_merge is not None else bool(onboarding and onboarding.auto_merge)
        if not allowed:
            return f"a person to merge {pr['url']}: auto_merge is off for {app_id}"
        verdict, why = checks_verdict(github.head_checks(app.repository, pr["head_sha"]), pr.get("created_at"), self.clock())
        if verdict == "wait":
            return f"its checks: {why}"
        if verdict == "failed":
            return f"a person: {why}"
        if pr.get("mergeable_state") == "dirty":
            return f"a person: {pr['url']} has conflicts"
        if pr.get("mergeable_state") not in MERGEABLE:
            waiting = f"GitHub to call {pr['url']} mergeable (it says {pr.get('mergeable_state')}"
            if pr.get("mergeable_state") in ("blocked", "behind") and self.clock() - record.get("opened_at", self.clock()) > 3600:
                return f"a person: {waiting}: a required review, a required check or an out-of-date branch)"
            return waiting + ")"
        if sorted(github.pull_request_files(app.repository, number)) != sorted(record["files"]):
            return f"a person: {pr['url']} changes more than the three workflow files"
        for path, expected in sorted(record["files"].items()):
            found = github.contents(app.repository, pr["head_sha"], path)
            if found is None or digest(found["text"]) != expected:
                return f"a person: {path} in {pr['url']} differs from what the platform rendered"
        return None

    # ---- what get_controller_status shows; read from the database only, so it costs no call to GitHub
    def status(self):
        settings = self.settings
        if settings is None:
            return {"enabled": False, "note": "platform_updates is not set: after a release the machine's update and the re-pins stay manual"}
        machine = self.c.store.platform_update("machine") or {}
        applications = {key.removeprefix("workflows:"): {field: record.get(field) for field in ("state", "target", "from", "pull_request", "url", "waiting", "note", "error")}
                        for key, record in self.c.store.platform_updates("workflows:").items()}
        return {"enabled": True, "machine": settings.machine, "workflows": settings.workflows, "delay_minutes": settings.delay_minutes,
                "quiet_minutes": settings.quiet_minutes, "runs": self.running, "main": machine.get("head"), "main_checks": machine.get("checks"),
                "next": machine.get("next"), "last_result": machine.get("last_result"),
                "errors": {part: machine[f"error_{part}"] for part in ("machine", "workflows") if machine.get(f"error_{part}")},
                "workflows_due": None if self.running is None else stamp(self.started + settings.delay_minutes * 60), "applications": applications,
                "note": None if self.running else "this controller does not run from a release folder of the machine, so it neither installs main "
                                                  "nor re-pins applications to its own commit"}
