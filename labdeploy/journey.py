"""A release as stages a person can follow on the Deployments page: GitHub's own steps grouped by what they do, and the one
long release step ("Deploy, probe, promote") split by what Cloud Run shows while it runs: the new revision serving no one,
then its share of the traffic. Evidence only: no stage is lit by a timer. After the run, the release receipt's own phase
times replace what was seen from outside."""

import re
import statistics

from .aftercare import parse_moment

# key, title, what it means, the jobs whose steps it may hold, the step names it holds
STAGES = (
    ("request", "Check the request",
     "The commit, its reviewed settings (platform.yaml) and the destination are the ones that were approved.",
     ("build",), (r"^Validate dispatch inputs$", r"^Install trusted platform dependencies$", r"^Validate reviewed specification$",
                  r"^Read the application's test policy$", r"^Read the contract$", r"^Read the jobs$", r"^Name the application repository$",
                  r"^Token for the application repository$", r"^The commit is on the application's main$")),
    ("code", "Safety checks on the code",
     "The Dockerfile follows the lab's rules, and no password or key was committed by mistake.",
     ("build",), (r"^Install the scanners$", r"^Check the Dockerfile", r"^Scan the checkout for secrets$")),
    ("tests", "Run the tests",
     "The app's own automated tests. If one fails, the release stops here and nobody sees the change.",
     ("build",), (r"^Run unit tests", r"^Install the package manager", r"^Install Node\.js dependencies")),
    ("libraries", "Check the libraries",
     "Every library the app uses is looked up in public lists of known security problems; a serious one that has a fix stops the release.",
     ("build",), (r"^Audit (Python|Node\.js) dependencies$",)),
    ("build", "Build the app",
     "The app is packed into an image without any cloud password, the image is searched for secrets, and its parts are listed.",
     ("build", "base"), (r"^Lint the Dockerfile$", r"^Build without cloud credentials$", r"^Scan the image for secrets$", r"^Make the image's parts list",
                         r"test image the jobs build on$", r"^Build and check the jobs' images$", r"^Read the plan's base$",
                         r"^Token for the base application's repository$", r"^Install the platform's dependencies$")),
    ("sign", "Upload and sign",
     "The image goes to Google Cloud and is signed, so only this exact, checked image can run.",
     ("deploy",), (r"^Validate artifact and deployment configuration$", r"^Choose the provenance path$", r"google-github-actions/",
                   r"^Configure registry access$", r"^Recover any interrupted previous release$", r"^Inspect data files", r"^Push",
                   r"provenance", r"^Install Cosign$", r"^Attest")),
    ("scan", "Scan the image",
     "Google scans the image for known security problems, and the result is kept with the release.",
     ("deploy",), (r"^Summarize (the jobs' )?image vulnerabilities$",)),
    ("start", "Start the new version beside the old one",
     "The new version starts next to the one people use now, with no visitors yet, and must pass its health check.",
     ("deploy",), (r"^Deploy, probe, promote",)),
    ("switch", "Move visitors to the new version",
     "Visitors switch to the new version and its health is checked again; the old version stays ready.",
     (), ()),
    ("watch", "Watch it with real traffic",
     "The platform watches errors and crashes. If anything goes wrong, visitors go back to the old version on their own.",
     (), ()),
    ("record", "Record the result",
     "The address, the checks and the outcome are saved with the release and shared.",
     ("deploy", "notify"), (r"^Record the service URL$", r"^Post the result$")),
)
RELEASE_STEP = re.compile(r"^Deploy, probe, promote")
DONE = {"done", "skipped"}
# The receipt's state when the release step failed, and the stage it failed in.
FAILED_IN = {"preparing": "start", "migrating": "start", "updating_jobs": "start", "deploying": "start", "promoting": "switch", "baking": "watch"}


def job_kind(name):
    name = (name or "").lower()
    return next((kind for kind in ("base", "build", "deploy", "notify") if name == kind or name.startswith(kind + " ")), None)


def assign(groups):
    """Every GitHub step in the stage it belongs to. A step no stage names (setting up a job, a checkout, a cleanup) goes with the
    next named step of its job, since it prepares it, or with the last one when nothing named follows."""
    slots, named = {key: [] for key, *_ in STAGES}, 0
    for job in groups:
        kind = job_kind(job.get("name"))
        allowed = [stage for stage in STAGES if stage[3] and (kind is None or kind in stage[3])]
        waiting, last = [], None
        for step in job.get("steps") or []:
            key = next((stage[0] for stage in allowed if any(re.search(p, step.get("name") or "") for p in stage[4])), None)
            if key is None:
                waiting.append(step)
                continue
            slots[key].extend(waiting)
            slots[key].append(step)
            waiting, last, named = [], key, named + 1
        if waiting:
            slots[last or ("record" if kind == "notify" else allowed[0][0] if allowed else "request")].extend(waiting)
    return slots, named


def rollup(steps):
    states = [step.get("state") for step in steps]
    if not states:
        return "pending"
    for state in ("failed", "cancelled", "running"):
        if state in states:
            return state
    finished = DONE | {"unknown"}  # an unusual conclusion (neutral, say) still ended
    if all(state in finished for state in states):
        return "skipped" if all(state == "skipped" for state in states) else "done"
    return "running" if any(state in finished for state in states) else "pending"  # between two of its steps


def span(steps):
    starts = [step["started_at"] for step in steps if step.get("started_at")]
    ends = [step["finished_at"] for step in steps if step.get("finished_at")]
    seconds = sum(step.get("seconds") or 0 for step in steps) if not starts and any(step.get("seconds") for step in steps) else None
    return (min(starts) if starts else None), (max(ends) if ends else None), seconds


def receipt_times(phases):
    """When each release phase began, from the receipt the release step wrote (exact, written by the step itself)."""
    times = {}
    for item in (phases or {}).get("timestamps") or []:
        at = parse_moment(item.get("at")) if isinstance(item, dict) else None
        if at and item.get("state") not in times:
            times[item["state"]] = at
    return times


def stages(groups, cloud=None, phases=None, bake_seconds=300, state=None, now=None):
    """The release's stages, in order, with their state and times; None when the run has no step a stage names (an older
    workflow, or a promotion or rollback)."""
    slots, named = assign(groups)
    if not named:
        return None
    cloud, phases = cloud or {}, phases or {}
    # A job GitHub already lists the steps of shows which stages this workflow has: one with none of its steps is left out.
    listed = {job_kind(job.get("name")) for job in groups if job.get("steps")}
    result = []
    for key, title, text, jobs, _ in STAGES:
        steps = slots[key]
        if (not steps and jobs and listed & set(jobs)) or (key in ("switch", "watch") and not any(item["key"] == "start" for item in result)):
            continue
        first, last, seconds = span(steps)
        result.append({"key": key, "title": title, "text": text, "state": rollup(steps), "started_at": first, "finished_at": last, "seconds": seconds,
                       "steps": [{"name": s.get("name"), "state": s.get("state"), "started_at": s.get("started_at"), "finished_at": s.get("finished_at"),
                                  "seconds": s.get("seconds")} for s in steps]})
    by = {item["key"]: item for item in result}
    if "start" in by:
        split_release(by, slots, cloud, phases, int(bake_seconds or 0), now)
    terminal = state in {"succeeded", "failed", "cancelled", "timed_out", "rolled_back", "dispatch_lost"}
    stopped = False
    for item in result:
        if item["state"] in {"failed", "cancelled"}:
            stopped = True
        elif stopped or (terminal and item["state"] == "pending"):
            if item["state"] in {"pending", "skipped"}:
                item["state"] = "not_run"
    if not terminal and not any(item["state"] == "running" for item in result):
        # Between two jobs, or waiting for a runner: the stage after the last finished one is the one starting.
        upcoming = next((item for index, item in enumerate(result) if item["state"] == "pending" and all(o["state"] in DONE for o in result[:index])), None)
        if upcoming:
            upcoming["state"] = "next"
    return result


def split_release(by, slots, cloud, phases, bake, now):
    """The release step is three stages to a person: the new version started beside the old one, the switch, the watch.
    While it runs, Cloud Run says which: the new revision present with no share, a share below everyone, or everyone."""
    start, switch, watch = by["start"], by["switch"], by["watch"]
    step = next((s for s in slots["start"] if RELEASE_STEP.search(s.get("name") or "")), None)
    if step is None:
        return
    began, ended = step.get("started_at"), step.get("finished_at")
    times = receipt_times(phases)
    share = float(cloud.get("share") or 0)
    switched = cloud.get("switched_at") or cloud.get("traffic_at")
    if step["state"] == "running":
        if share >= 100 and switched:
            start.update(state="done", finished_at=switched)
            switch.update(state="done", started_at=switched, finished_at=switched)
            if bake:
                watch.update(state="running", started_at=switched, since=switched, window=bake)
            else:
                watch.update(state="skipped")
        elif share > 0:
            start.update(state="done", finished_at=switched)
            switch.update(state="running", started_at=switched, share=share)
        else:
            start["note"] = ("The new version is running with no visitors yet; its health is being checked."
                             if cloud.get("present") else "Getting the new version ready.")
            if cloud.get("restored_at"):
                start["note"] = "Visitors are going back to the previous version."
        return
    if step["state"] in {"failed", "cancelled"}:
        where = FAILED_IN.get(phases.get("failed_phase")) or ("watch" if switched and share >= 100 else "switch" if switched else "start")
        for key in ("start", "switch", "watch"):
            item = by[key]
            if key == where:
                item["state"] = step["state"]
                break
            item["state"] = "done"
        if switched and where != "start":
            start["finished_at"] = switched
        return
    if step["state"] == "done":
        start["state"] = switch["state"] = "done"
        watch["state"] = "done" if bake else "skipped"
        promoted, baking = times.get("promoting"), times.get("baking")
        finished = times.get("succeeded") or ended
        if promoted:
            start["finished_at"] = promoted
            switch.update(started_at=promoted, finished_at=baking or finished)
            if baking:
                watch.update(started_at=baking, finished_at=finished)
        elif switched:
            start["finished_at"] = switched
            switch.update(started_at=switched, finished_at=switched)
            watch.update(started_at=switched, finished_at=ended)
        start["started_at"] = start.get("started_at") or began


def cloud_evidence(seen, plan_id, health, now):
    """What Cloud Run shows of this plan's new revision: whether it is there, its share of the traffic, and, the first time each
    is seen, when the service last changed (its deploy, then its switch). First sightings are kept, never moved."""
    mine = [item for item in health.get("traffic") or [] if item.get("plan_id") == plan_id]
    share = sum(float(item.get("percent") or 0) for item in mine)
    changed = min(health.get("updated_at") or now, now)
    seen = dict(seen or {})
    if mine and "candidate_at" not in seen:
        seen["candidate_at"] = changed
    if share > 0 and "traffic_at" not in seen:
        seen["traffic_at"] = changed
    if share >= 100 and "switched_at" not in seen:
        seen["switched_at"] = changed
    if share == 0 and "traffic_at" in seen and "restored_at" not in seen:
        seen["restored_at"] = now
    seen.update(present=bool(mine), share=share, at=now)
    return seen


def typical(durations):
    """How long this application's releases usually take: the median of its recent successful ones, with how many there were.
    None below three, since fewer say little."""
    durations = [d for d in durations if d and d > 0]
    if len(durations) < 3:
        return None
    return {"seconds": statistics.median(durations), "count": len(durations)}
