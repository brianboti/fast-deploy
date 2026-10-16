"""The chat's answers (docs/CLAUDE-CHAT.md, "What answers look like").

Every main tool's result starts with `summary`: a few Markdown lines the controller writes from that result alone, never
from Claude's words, so the operator reads the same plain, exact shape whatever they ask: a bold outcome, the facts that
matter, and one **Next** line. Claude shows it as written and adds only what the question needs; the full result stays
beside it for detail. A renderer that fails leaves the tool's answer as it was (`attach`).
"""
import datetime
import logging

log = logging.getLogger("labdeploy")

PASSED = {"passed", "passed_in_earlier_release"}
CHECK_NAMES = (("tests", "tests"), ("dependency_scan", "library audit"), ("health", "health check"))
CHECK_WORDS = {"passed": "passed", "passed_in_earlier_release": "passed in an earlier release", "skipped_by_config": "skipped by its settings",
               "failed": "failed", "release_failed_health_unconfirmed": "failed", "pending": "pending", "queued": "pending",
               "in_progress": "running", "not_run": "not run", "unconfirmed": "not confirmed", "interrupted": "interrupted", "cancelled": "cancelled"}
# The deploy workflow's steps in plain words, matched by a piece of the step's name, in the order they run.
STEP_WORDS = (
    ("Validate dispatch inputs", "checking the request"),
    ("Validate reviewed specification", "checking platform.yaml"),
    ("Run unit tests", "running the tests"),
    ("Audit Python dependencies", "checking the libraries for known vulnerabilities"),
    ("Lint the Dockerfile", "checking the Dockerfile"),
    ("Build without cloud credentials", "building the image"),
    ("Validate artifact and deployment configuration", "checking the release target"),
    ("google-github-actions/auth", "signing in to Google Cloud"),
    ("Recover any interrupted previous release", "checking for an unfinished earlier release"),
    ("Push image and resolve immutable digest", "uploading the image"),
    ("Choose the provenance path", "choosing how to sign the image"),
    ("Install Cosign", "preparing the signing tool"),
    ("Sign build provenance", "signing the image"),
    ("Attest build provenance", "signing the image"),
    ("Verify build provenance", "verifying the image's signature"),
    ("Summarize image vulnerabilities", "scanning the image"),
    ("Deploy, probe, promote", "releasing"),
)
PHASE_WORDS = {"preparing": "getting the new version ready", "migrating": "updating the database (the migration)",
               "deploying": "starting the new version beside the old one", "promoting": "moving visitors to the new version",
               "baking": "watching the new version with real traffic"}
ONBOARDING_STEPS = (("validating", "check the repository"), ("registering", "register the app"),
                    ("infrastructure", "prepare its cloud setup (a pull request)"), ("planning", "plan the cloud setup and check the safety rules"),
                    ("applying", "build the cloud setup"), ("environment", "prepare its GitHub environment"),
                    ("workflows", "add the release workflows (a pull request)"), ("merging_workflows", "merge the release workflows"),
                    ("releasing", "start the first release"))
# Taken as it is (docs/ROADMAP.md D31 to D33): its contract is proposed to the platform repository instead of workflows to its own.
AS_IS_STEPS = (("validating", "read the repository as it is"), ("contract", "propose its contract (a pull request on the platform)"),
               ("merging_contract", "wait for a person to merge the contract"), ("registering", "register the app"),
               ("infrastructure", "prepare its cloud setup (a pull request)"), ("planning", "plan the cloud setup and check the safety rules"),
               ("applying", "build the cloud setup"), ("environment", "prepare its GitHub environment"), ("releasing", "start the first release"))
CHOICE_ORDER = ("Build", "Python", "Dependencies", "Application", "Start command", "Port", "Health", "Needs", "Tests")
FRAMEWORKS = {"fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette", "litestar": "Litestar", "bottle": "Bottle"}
CHANGE_WORDS = {"application_code": "application code", "dependencies": "dependencies", "dockerfile": "the Dockerfile",
                "platform_config": "platform.yaml (settings)", "workflows": "the release workflows"}
NEED_WORDS = {"postgres": "Postgres database", "firestore": "Firestore database", "bucket": "file bucket", "topic": "message topic"}


# ---- small pieces
def money(value):
    """Dollars as people write them: $0, $0.06, $8.57, $1,234.00."""
    if value is None:
        return "unknown"
    value = float(value)
    return "$0" if value == 0 else f"${value:,.2f}"


def clip(text, limit=200):
    """One line, at most `limit` characters, safe inside Markdown code: no backticks or line breaks."""
    line = " ".join(str(text or "").split()).replace("`", "'")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def title(message, limit=60):
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return clip(first, limit)


def version(sha, message=None):
    """A commit as the operator reads it: the short id, then its first line."""
    if not sha:
        return "an unknown commit"
    first = title(message)
    return f"`{sha[:7]}` ({first})" if first else f"`{sha[:7]}`"


def moment(value):
    """A timestamp (ISO text or seconds since 1970) as an aware datetime in UTC, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.UTC)
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def day(value):
    when = moment(value)
    return f"{when.day} {when:%b %Y}" if when else "unknown"


def duration(start, end):
    first, last = moment(start), moment(end)
    if first is None or last is None or last < first:
        return None
    seconds = int((last - first).total_seconds())
    if seconds < 90:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 90 else f"{minutes // 60} h {minutes % 60} min"


def percent(value):
    return f"{round(float(value) * 100)}%"


def link(text, url):
    return f"[{text}]({url})" if url else text


def plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def access(exposure):
    return {"authenticated": "needs a Google sign-in token; not open in a browser",
            "lab-members": "lab members open it in a browser after a Google sign-in"}.get(exposure, exposure or "unknown")


def compose(head, facts=(), next_line=None, foot=None, block=None):
    """The one shape of every answer: the bold outcome, the facts as bullets (or a ready block), Next, then a small footer."""
    parts = [head]
    if block:
        parts.append(block)
    facts = [fact for fact in facts if fact]
    if facts:
        parts.append("\n".join(f"- {fact}" for fact in facts))
    if next_line:
        parts.append(f"**Next:** {next_line}")
    if foot:
        parts.append(foot)
    return "\n\n".join(parts)


def attach(render, result, **context):
    """The result with `summary` first. A renderer that fails, or a result that already has one, leaves it as it was."""
    if not isinstance(result, dict) or "summary" in result:
        return result
    try:
        text = render(result, **context)
    except Exception:  # noqa: BLE001 - the summary is a convenience; the tool's own answer always goes out
        log.exception("chat summary failed for %s", getattr(render, "__name__", render))
        return result
    return {"summary": text, **result} if text else result


# ---- releases
def step_words(step):
    return next((words for needle, words in STEP_WORDS if needle in (step or "")), (step or "").lower() or "an unnamed step")


def doing(plan):
    """What a running release is doing now, in plain words."""
    phase = (plan.get("phases") or {}).get("state")
    running = next((item for item in plan.get("timeline") or [] if item.get("status") == "in_progress"), None)
    step = (running or {}).get("step") or ""
    if phase in PHASE_WORDS and (not step or "Deploy, probe, promote" in step):
        return PHASE_WORDS[phase]
    return step_words(step) if step else "waiting for a GitHub runner"


def checks_line(checks):
    parts = [f"{name} {CHECK_WORDS.get(checks[key], str(checks[key]).replace('_', ' '))}" for key, name in CHECK_NAMES if checks.get(key)]
    return "Checks: " + " · ".join(parts) if parts else None


def bake_line(phases, minutes):
    bake = phases.get("bake") or {}
    result = bake.get("result")
    window = bake.get("window") or {}
    if result == "passed":
        return (f"Watched {minutes} min with real traffic: {plural(window.get('requests', 0), 'request')}, "
                f"{window.get('errors_5xx', 0)} server errors")
    if result == "inconclusive":
        return f"Watched {minutes} min: too little traffic to judge, so only the health checks vouch for it"
    if result == "unobserved":
        return "The watch after the release could not read its logs, so only the health checks vouch for it"
    if result == "skipped":
        return "No watch after the release (turned off for this app)"
    return None


def scan_line(vulnerabilities):
    if not vulnerabilities or vulnerabilities.get("analysis_status") not in (None, "FINISHED_SUCCESS"):
        return None
    found = vulnerabilities.get("by_severity") or {}
    fixable = vulnerabilities.get("fixable_by_severity") or {}
    critical, high = found.get("CRITICAL", 0), found.get("HIGH", 0)
    if not critical and not high:
        return "Image scan: no critical or high findings"
    fix = fixable.get("CRITICAL", 0) + fixable.get("HIGH", 0)
    fixes = "all fixable" if fix == critical + high else "none fixable yet" if not fix else f"{fix} fixable"
    found_words = " and ".join(part for part in (f"{critical} critical" if critical else "", f"{high} high" if high else "") if part)
    blocked = vulnerabilities.get("blocked")
    if blocked is None:  # a summary from before 1.16.1 says only whether blocking was on
        blocked = bool(vulnerabilities.get("blocking")) and critical > 0
    if blocked:
        verdict = "it stopped the release"
    elif vulnerabilities.get("trust") == "unknown":
        verdict = "the gate let it through, since none can be fixed yet" if not fix else "the gate let it through"
    else:
        verdict = "not blocking"
    return f"Image scan: {found_words} findings ({fixes}); {verdict}"


def gates_line(checks):
    """The checks before the build (docs/ROADMAP.md 14.1, 14.2, 14.7), in words: the Dockerfile, keys, the parts list."""
    if not checks:
        return None
    parts = []
    policy = checks.get("dockerfile_policy")
    if policy:
        parts.append("Dockerfile rules passed" + (f" ({policy['reported']} noted)" if policy.get("reported") else ""))
    found = [checks.get(key) or {} for key in ("secrets_in_checkout", "secrets_in_image")]
    verified = sum(item.get("verified", 0) for item in found)
    possible = sum(item.get("findings", 0) - item.get("in_installed_packages", 0) for item in found) - verified
    if any(found):
        parts.append(f"{plural(verified, 'working key')} found" if verified else "no working keys found"
                     + (f" ({plural(possible, 'possible secret')} to check)" if possible else ""))
    if checks.get("sbom"):
        parts.append(f"parts list recorded ({checks['sbom']['components']} parts)")
    return "Before the build: " + " · ".join(parts) if parts else None


def contained_line(held):
    """How unknown code ran (docs/ROADMAP.md D35); nothing for the lab's own code, which runs as it always did."""
    if not held or held.get("trust") != "unknown":
        return None
    reach = "no internet (Google's APIs and the lab's network only)" if held.get("egress") == "private" else "internet allowed by its contract"
    return f"Runs contained, as code nobody has reviewed: {reach}, second-generation environment"


def cost_line(cost):
    if not cost:
        return None
    floor = (cost.get("idle_floor_usd_month") or 0) + (cost.get("needs_floor_usd_month") or 0)
    return f"Cost: {money(floor)} a month with no visitors; compute at most {money(cost.get('compute_ceiling_usd_month'))} a month"


def traffic(split):
    if isinstance(split, dict):
        return ", ".join(f"`{revision}` {share}%" for revision, share in split.items())
    return f"`{split}`" if split else "unknown"


def release(plan):
    """A plan or a release, from planned to live or stopped: create_deployment_plan, deploy_staging, deploy_latest,
    cancel_deployment and get_deployment_status."""
    if plan.get("kind") == "rollback":
        return rollback(plan)
    app = plan.get("application") or "the app"
    state = plan.get("state")
    phases, receipt, workflow, checks = plan.get("phases") or {}, plan.get("receipt") or {}, plan.get("workflow") or {}, plan.get("checks") or {}
    what = version(plan.get("sha"), (plan.get("commit") or {}).get("message"))
    run = workflow.get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")
    foot = f"Plan `{str(plan.get('id') or '')[:8]}`" + (f" · {link('GitHub run', run)}" if run else "")
    minutes = max(1, round(int(plan.get("bake_seconds") or 300) / 60))
    restored = "traffic_restored" in (phases.get("state"), receipt.get("state"))
    stuck = "rollback_failed_manual_recovery_required" in (phases.get("state"), receipt.get("state"))
    if state == "planned":
        return compose(f"**Ready to release `{app}`**: {what}. Nothing has changed yet.",
                       [cost_line(plan.get("cost")), *[f"Note: {clip(item, 220)}" for item in (plan.get("warnings") or [])[:2]]],
                       "say “release it” within 15 minutes, or ask for a new plan later.", foot)
    if state in ("dispatching", "dispatched", "awaiting_workflow", "queued"):
        return compose(f"**Release started for `{app}`**: {what}.", ["GitHub has the job; nothing is live until it finishes"],
                       "nothing to do; it continues on its own. Ask me how it is going at any time.", foot)
    if state == "running":
        done = [name for key, name in CHECK_NAMES if checks.get(key) in PASSED]
        facts = [f"Now: {doing(plan)}", "Passed so far: " + ", ".join(done) if done else None]
        if phases.get("state") == "baking":
            facts.append(f"The new version already serves everyone; if it fails within {minutes} min, visitors go back to the previous version on their own")
        return compose(f"**Release in progress for `{app}`**: {what}.", facts, "nothing to do; it continues on its own.", foot)
    if state == "succeeded" or (state in ("failed", "cancelled", "timed_out") and receipt.get("state") == "succeeded" and not restored):
        url = phases.get("url") or receipt.get("url")
        took = duration(workflow.get("created_at"), (plan.get("outcome") or {}).get("completed_at") or workflow.get("updated_at"))
        facts = [f"Address: {url} ({access((plan.get('spec') or {}).get('exposure'))})" if url else None, checks_line(checks),
                 gates_line(receipt.get("build_checks")), "The database migration ran first" if phases.get("migration") else None,
                 bake_line(phases, minutes), scan_line(plan.get("vulnerabilities")), contained_line(receipt.get("containment")),
                 f"Took {took}" if took and state == "succeeded" else None]
        if state != "succeeded":
            facts.append(f"The GitHub run ended '{state}' after the release itself had finished")
        return compose(f"**`{app}` is live** with {what}.", facts, f"nothing to do. To undo it, say “roll {app} back”.", foot)
    if state == "completed_unverified":
        return compose(f"**`{app}`: the release finished, but not every check reported a result.**", [checks_line(checks)],
                       "open the GitHub run before trusting this release.", foot)
    if stuck:
        error = phases.get("restore_error") or receipt.get("restore_error")
        return compose(f"**Needs you now: `{app}` could not be put back on its previous version.**",
                       [f"Error: `{clip(error, 240)}`" if error else None, f"Before the release it served: {traffic(phases.get('previous_traffic'))}"],
                       "run the recovery workflow and check Cloud Run (docs/RUNBOOK.md), then tell me.", foot)
    if restored:
        failed_phase = phases.get("failed_phase") or receipt.get("failed_phase")
        if receipt.get("cancelled"):
            reason = "it was cancelled"
        else:
            reason = {"preparing": "it stopped before deploying anything", "migrating": "the database migration failed, so nothing new was deployed",
                      "baking": "the new version took over, then failed its watch with real traffic"}.get(failed_phase, "the new version failed its health check")
        error = phases.get("error") or receipt.get("error")
        facts = [f"Why: {reason}", f"Error: `{clip(error, 240)}`" if error else None,
                 "The new version's last log lines are in the release details; ask me to show them" if phases.get("candidate_logs") else None]
        fix = ("fix the migration (check whether part of it ran), then release again." if failed_phase == "migrating"
               else "release again when ready." if receipt.get("cancelled") else "the developer fixes the app, then release again.")
        serving = "is serving again" if failed_phase == "baking" else "is still serving"
        return compose(f"**Release stopped for `{app}`: the previous version {serving}.**", facts, fix, foot)
    if state == "dispatch_lost":
        return compose(f"**The release never started for `{app}`**: GitHub never ran it, so nothing changed.", [], "ask me for a new release.", foot)
    if state == "release_requested":
        request = plan.get("release_request") or {}
        number = link(f"#{request.get('pull_request')}", request.get("url"))
        waiting = request.get("waiting") or ""
        person = waiting.startswith(("a person", "another operator"))
        return compose(f"**Release requested for `{app}`**: pull request {number} moves its contract to {version(plan.get('sha'))}.",
                       [f"Waiting for {clip(waiting, 200)}" if waiting else "It merges itself once the platform's checks pass",
                        "Nothing is live yet: merging it starts the release within a minute"],
                       "a person reviews and merges it." if person else "nothing to do; I'll release it once it merges.")
    if state == "dispatch_unknown":
        return compose(f"**It is unclear whether GitHub received the release for `{app}`.**", [],
                       "I'll check GitHub for a run with this plan before trying again.", foot)
    if state in ("failed", "timed_out", "cancelled"):
        where = step_words((plan.get("failed_step") or {}).get("step"))
        head = {"failed": f"**Release failed for `{app}` while {where}.** Nothing new is serving.",
                "timed_out": f"**Release timed out for `{app}`.** Nothing new is serving.",
                "cancelled": f"**Release cancelled for `{app}`.** Nothing new is serving."}[state]
        return compose(head, [checks_line(checks)], plan.get("next_action"), foot)
    return compose(f"**`{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


def rollback(plan):
    app = plan.get("application") or "the app"
    state, receipt = plan.get("state"), plan.get("receipt") or {}
    run = (plan.get("workflow") or {}).get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")
    foot = f"Plan `{str(plan.get('id') or '')[:8]}`" + (f" · {link('GitHub run', run)}" if run else "")
    if state == "succeeded" and receipt.get("state") == "rolled_back":
        return compose(f"**`{app}` is back on its previous version.**",
                       [f"Now serving: {traffic(receipt.get('restored_traffic'))}", f"No longer serving: `{receipt.get('rolled_back_from')}`",
                        "Database changes and anything the app already did are not undone"],
                       "the developer fixes the code; then release again.", foot)
    if state in ("dispatching", "dispatched", "awaiting_workflow", "queued", "running"):
        return compose(f"**Rolling `{app}` back**: visitors move to the previous version within about a minute.", [],
                       "nothing to do; ask me in a minute.", foot)
    if state in ("failed", "cancelled", "timed_out"):
        return compose(f"**The rollback of `{app}` did not complete**; visitors were not moved.", [],
                       "open the GitHub run, then try again or release the wanted commit.", foot)
    return compose(f"**Rollback of `{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


# ---- onboarding and repositories
def onboarding(view):
    """onboard_application and get_onboarding_status."""
    app, state, steps = view.get("application") or "the app", view.get("state"), view.get("steps") or {}
    where = f"`{view.get('repository')}`" + (f", folder `{view['path']}`" if view.get("path") else "") + (", as it is" if view.get("as_is") else "")
    listed = AS_IS_STEPS if view.get("as_is") else ONBOARDING_STEPS
    names, order = dict(listed), [key for key, _ in listed]
    prs = [link(f"#{(steps.get(key) or {})['pull_request']}", (steps.get(key) or {}).get("url"))
           for key in ("contract", "infrastructure", "workflows") if (steps.get(key) or {}).get("pull_request")]
    pull_requests = "Pull requests: " + ", ".join(prs) if prs else None
    if state == "complete":
        measures = view.get("measures") or {}
        took = f" in {measures['minutes']:g} min" if measures.get("minutes") is not None else ""
        facts = [f"From {where}", pull_requests,
                 f"Stopped {plural(measures['blocked'], 'time')} on the way; each was fixed and it continued" if measures.get("blocked") else None,
                 f"First release started: plan `{str(view.get('plan_id') or '')[:8]}`" if view.get("plan_id") else None]
        return compose(f"**`{app}` is onboarded**{took}.", facts, "I'll follow the first release; ask me how it is going.")
    if state == "blocked":
        at = view.get("blocked_at")
        facts = [f"Why: {clip(view.get('error'), 400)}", "Nothing was created" if at == "validating" else "Nothing after this step was done", pull_requests]
        return compose(f"**Onboarding `{app}` stopped at: {names.get(at, at or 'an unknown step')}.**", facts,
                       "fix what it names, then ask me to continue; it picks up from this step.")
    waiting = (steps.get(state) or {}).get("waiting")
    number = f"step {order.index(state) + 1} of {len(order)}: " if state in order else ""
    person = bool(waiting) and (waiting.startswith("a person") or waiting.startswith("another operator"))
    return compose(f"**Onboarding `{app}`, {number}{names.get(state, str(state))}.**",
                   [f"From {where}", f"Waiting for {clip(waiting, 240)}" if waiting else None, pull_requests],
                   "a person reviews and merges that pull request; then it continues on its own." if person
                   else "nothing to do; it continues on its own every minute.")


def check(result):
    """check_repository."""
    where = f"`{result.get('repository')}`" + (f", folder `{result['path']}`" if result.get("path") else "")
    missing = [item for item in result.get("checklist") or [] if not item.get("ok")]
    extra = [item for item in result.get("recommended") or [] if not item.get("ok")]
    if result.get("ready"):
        facts = ["Everything the platform needs is there"]
        if extra:
            draftable = [item for item in extra if item.get("platform_writes")]
            facts.append("Recommended, not required: " + ", ".join(f"`{item['item']}`" for item in extra)
                         + (" (the platform can draft " + ("them" if len(draftable) == len(extra) else "some of them") + ")" if draftable else ""))
        return compose(f"**Ready to deploy: {where}.**", facts, "say “deploy it” and I'll onboard it; it usually takes 4 to 15 minutes.")
    block = "\n".join(f"{number}. `{item['item']}`: {item.get('why') or 'required'}" + (" (the platform can draft it)" if item.get("platform_writes") else "")
                      for number, item in enumerate(missing, 1))
    can = [item["item"] for item in missing if item.get("platform_writes")]
    next_line = ("I can open a draft pull request with " + ", ".join(f"`{name}`" for name in can) + " and the other files the platform can "
                 "write; a person reviews it, sets `reviewed: true` and merges." if can else "the developer adds them; I can write them a short message.")
    as_is = result.get("as_it_is") or {}
    if as_is.get("ready"):
        facts = ["Or take it as it is, adding nothing to the repository: " + "; ".join(choice_lines(as_is.get("choices"), 3))]
        next_line = ("say “deploy it as it is” and I'll propose its contract to the platform repository (a person's merge is the review), or "
                     + (next_line if next_line.startswith("I ") else next_line[0].lower() + next_line[1:]))
        return compose(f"**Not ready to deploy as it stands: {where}.** {plural(len(missing), 'required item')} missing:", facts, next_line, block=block)
    if as_is.get("refusals"):
        block += "\n\nAs it is, it is refused too: " + "; ".join(f"{files} {problem} (fix: {fix})"
                                                                for files, problem, fix in grouped_refusals(as_is["refusals"])[:3]) + "."
    return compose(f"**Not ready yet: {where}.** {plural(len(missing), 'required item')} missing:", [], next_line, block=block)


def grouped_refusals(refusals):
    """Refusals in words, the ones with the same problem and fix told once with every file they name (found live: two
    committed environment files repeated the same long fix)."""
    groups = {}
    for item in refusals or []:
        groups.setdefault((item.get("problem"), item.get("fix")), []).append(f"`{item.get('file')}`")
    return [(", ".join(files), problem, fix) for (problem, fix), files in groups.items()]


def choice_lines(choices, limit=6):
    """The reader's choices in plain words, most telling first: what builds it, which Python, how it starts, its health check."""
    by_kind = {}
    for item in choices or []:
        by_kind.setdefault(item.get("what"), item)
    lines = []
    for kind in CHOICE_ORDER:
        item = by_kind.get(kind)
        if item:
            source = str(item.get("from") or "")
            where = f" (from `{clip(source, 60)}`)" if "/" in source or "." in source.split(" ")[0] else ""
            lines.append(f"{kind}: {clip(str(item.get('value')), 100)}{where}")
    return lines[:limit] or ["Nothing was chosen yet"]


def contract_import(view):
    """import_contract."""
    where = f"`{view.get('repository')}`" + (f", folder `{view['path']}`" if view.get("path") else "")
    refusals = view.get("refusals") or []
    if refusals:
        block = "\n".join(f"{number}. {files} {problem}. Fix: {fix}." for number, (files, problem, fix) in enumerate(grouped_refusals(refusals), 1))
        return compose(f"**{where} can't be taken as it is yet.** {plural(len(refusals), 'thing')} to fix in the repository:", [],
                       "the developer fixes them (nothing was opened); then ask me again.", block=block)
    if view.get("existing"):
        return compose(f"**The platform already keeps `{view.get('service')}`'s contract** (`{view['existing']}`).", [],
                       "say “deploy it as it is” and I'll onboard it.")
    pr = view.get("pull_request")
    facts = choice_lines(view.get("choices"), 5)
    facts += [f"To check before merging: {clip(note, 200)}" for note in (view.get("notes") or [])[:2]]
    if pr == "disabled":
        return compose(f"**Contract drafted for {where}, not proposed**: the controller's writes are switched off.", facts,
                       "the operator turns writes on in the controller's configuration.")
    if not isinstance(pr, dict) or not pr.get("number"):
        return compose(f"**No contract pull request for {where}**: {clip(view.get('next_action') or 'nothing to import', 220)}")
    number = link(f"#{pr['number']}", pr.get("url"))
    if not pr.get("changed"):
        return compose(f"**A contract pull request is already open for {where}**: {number}.", facts, "review and merge it; merging is the review.")
    return compose(f"**Contract proposed for {where}**: {number}, service `{view.get('service')}`. Nothing was added to the repository.", facts,
                   "a person reviews and merges it (merging is the review); then I onboard it as it is.")


def detected_words(found):
    parts = []
    if found.get("framework"):
        parts.append(f"{FRAMEWORKS.get(found['framework'], found['framework'])} app" + (f" `{found['application']}`" if found.get("application") else ""))
    if found.get("python"):
        parts.append(f"Python {found['python']}")
    if found.get("port"):
        parts.append(f"port {found['port']}")
    if found.get("health_path"):
        parts.append(f"health check at `{found['health_path']}`" + ("" if found.get("health_route_found") else " (no such route found in the code)"))
    return ", ".join(parts)


def draft(view):
    """propose_configuration."""
    where = f"`{view.get('repository')}`" + (f", folder `{view['path']}`" if view.get("path") else "")
    pr, files = view.get("pull_request"), sorted(view.get("files") or {})
    found = detected_words(view.get("detected") or {})
    if pr is None:
        return compose(f"**Nothing to draft for {where}**: every file the contract asks for is there.", [], "say “deploy it” and I'll onboard it.")
    if pr == "disabled":
        return compose(f"**No draft opened for {where}**: the controller's writes are switched off.",
                       ["It would add: " + ", ".join(f"`{name}`" for name in files)] if files else [], "the operator turns writes on in the controller's configuration.")
    number = link(f"#{pr.get('number')}", pr.get("url"))
    if not pr.get("changed"):
        return compose(f"**A draft is already open for {where}**: {number}.",
                       ["It was left as it is: it may hold a person's edits, or predate this request"],
                       "review and merge it, or close it and ask me to draft again.")
    facts = ["Files: " + ", ".join(f"`{name}`" for name in files), f"Found: {found}" if found else None]
    facts += [f"To check: {clip(line, 220)}" for line in (view.get("review") or [])[:4]]
    return compose(f"**Draft pull request opened for {where}**: {number}, {plural(len(files), 'file')}.", facts,
                   "a person reviews it (above all `platform.yaml`: size, cost, who may open it), sets `reviewed: true` and merges; then say “deploy it”.")


def pull_request(result, what="", applies=False):
    """propose_infrastructure and update_app_workflows."""
    app, pr = result.get("application") or "the app", result.get("pull_request") or {}
    if not pr.get("number"):
        return compose(f"**No pull request needed for `{app}`**: {clip(pr.get('note') or result.get('note') or 'nothing changed', 220)}.")
    number = link(f"#{pr['number']}", pr.get("url"))
    if not pr.get("changed"):
        return compose(f"**A pull request is already open for `{app}`**: {number}.", [clip(pr.get("note"), 220) if pr.get("note") else None],
                       "review and merge it.")
    changed = pr["changed"]
    facts = ["Changes: " + ", ".join(f"`{name}`" for name in changed[:6]) + (f" and {len(changed) - 6} more" if len(changed) > 6 else "")]
    return compose(f"**Pull request opened for `{app}`**: {number}{', ' + what if what else ''}.", facts,
                   "a person reviews and merges it" + ("; the merge applies it to the cloud." if applies else "."))


# ---- live apps
def undeployed(entry):
    if not entry:
        return "unknown"
    if entry.get("status") == "identical" or entry.get("ahead_by") == 0:
        return "up to date"
    if entry.get("ahead_by"):
        return plural(entry["ahead_by"], "new commit")
    return clip(entry.get("status") or "unknown", 40).replace("_", " ")


def applications(apps):
    """list_applications: one table, then what needs attention."""
    rows, notes, waiting = [], [], []
    for app_id, app in sorted(apps.items()):
        if not isinstance(app, dict):
            continue
        live = app.get("live_release") or {}
        name = link(f"`{app_id}`", live.get("url"))
        shown = version(live.get("sha"), (live.get("commit") or {}).get("message")) if live.get("sha") else "not released yet"
        state = undeployed(app.get("undeployed_commits")) if live else "—"
        if "new commit" in state:
            waiting.append(app_id)
        rows.append(f"| {name} | {shown} | {day(live.get('completed_at')) if live else '—'} | {state} |")
        if app.get("paused"):
            notes.append(f"`{app_id}` is paused: releases are refused")
        if (app.get("platform_pin") or {}).get("supported") is False:
            notes.append(f"`{app_id}`: its release workflows are too old for this platform; ask me to update them")
    table = "| App | Live version | Released | Main |\n|---|---|---|---|\n" + "\n".join(rows)
    next_line = (f"say “deploy the latest {waiting[0]}” to release what is waiting." if len(waiting) == 1
                 else "say which app to release: " + ", ".join(f"`{name}`" for name in waiting) + "." if waiting
                 else "ask about any app: its health, errors, cost or access.")
    head = (f"**{plural(len(rows), 'application')}**: all up to date." if not waiting
            else f"**{plural(len(rows), 'application')}**: {plural(len(waiting), 'app')} {'has' if len(waiting) == 1 else 'have'} new commits waiting.")
    return compose(head, notes, next_line, block=table)


def health(result):
    """get_service_health."""
    app = result.get("application") or "the app"
    service, metrics = result.get("service") or {}, result.get("metrics") or {}
    window = metrics.get("window_minutes") or 60
    errors, uptime, requests = metrics.get("server_errors"), metrics.get("uptime_checks_passed"), metrics.get("requests")
    unreachable = uptime is not None and uptime < 0.99
    if service.get("ready") is False:
        head = f"**`{app}` is not ready**" + (f": {clip(service.get('ready_message'), 200)}" if service.get("ready_message") else ".")
    elif errors:
        head = f"**`{app}` is serving, with {plural(errors, 'server error')} in the last {window} min.**"
    elif unreachable:
        head = f"**`{app}` may not be reachable**: the uptime check passed in only {percent(uptime)} of its probes."
    else:
        head = f"**`{app}` is healthy.**"
    serving = (service.get("traffic") or [{}])[0]
    p95 = metrics.get("request_latency_ms_p95")
    facts = [(f"Last {window} min: {plural(requests, 'request')}, {plural(errors or 0, 'server error')}"
              + (f", 95% answered within {round(float(p95), 1):g} ms" if p95 is not None else "")) if requests is not None else f"Last {window} min: no traffic data",
             f"Uptime checks: {percent(uptime)} passed" if uptime is not None else None,
             (f"Serving: {version(serving.get('commit'))}" + (" (the release the controller recorded)" if result.get("agreement")
              else " (not the release the controller recorded; ask me to compare)")) if serving.get("commit") else None,
             f"Access: {access((result.get('access') or {}).get('exposure'))}",
             f"Memory: {percent(metrics['memory_utilization_p95'])} of its limit at the busiest" if metrics.get("memory_utilization_p95") is not None else None,
             *[f"Note: {clip(note, 220)}" for note in (result.get("notes") or [])[:3]]]
    trouble = bool(errors) or service.get("ready") is False or unreachable
    return compose(head, facts, (f"look at the error lines (“show {app}'s errors”); if the latest release caused it, “roll {app} back”."
                                 if trouble else "nothing to do."), link("Cloud console", result.get("console_url")) if result.get("console_url") else None)


def logs(result):
    """get_recent_logs."""
    app, minutes, count = result.get("application") or "the app", result.get("minutes") or 30, result.get("count") or 0
    level = (result.get("severity") or "ERROR").lower()
    if not count:
        return compose(f"**No {level} lines for `{app}`** in the last {minutes} minutes.", [], "nothing to do.")
    shown = (result.get("lines") or [])[:5]

    def line(item):
        when = moment(item.get("at"))
        return (f"{when:%H:%M:%S} " if when else "") + clip(item.get("message"), 160)
    block = "```\n" + "\n".join(line(item) for item in shown) + "\n```"
    more = f" (the newest {len(shown)} shown)" if count > len(shown) else ""
    return compose(f"**{plural(count, level + ' line')} for `{app}`** in the last {minutes} minutes{more}:",
                   [], f"if this started with the latest release, “roll {app} back”; otherwise the developer reads the full lines.", block=block)


def compare(result):
    """compare_with_live."""
    app, live = result.get("application") or "the app", result.get("live_release") or {}
    if not live:
        return compose(f"**`{app}` has no live release yet.**", [], f"say “deploy the latest {app}”.")
    ahead = result.get("ahead_by") or 0
    if ahead == 0:
        return compose(f"**Nothing waiting for `{app}`**: main is the live release, {version(live.get('sha'), (live.get('commit') or {}).get('message'))}.",
                       [], "nothing to do.")
    commits = result.get("commits") or []
    facts = [version(item.get("sha"), item.get("message")) + (f" by {item['author']}" if item.get("author") else "") for item in commits[:5]]
    if len(commits) > 5:
        facts.append(f"and {len(commits) - 5} more")
    kinds = [words for key, words in CHANGE_WORDS.items() if (result.get("changes") or {}).get(key)]
    if kinds:
        facts.append("Changes: " + ", ".join(kinds))
    cost = result.get("cost") or {}
    if cost:
        facts.append(f"Cost with no visitors: {money(cost.get('idle_floor_before_usd_month'))} now, {money(cost.get('idle_floor_after_usd_month'))} after; "
                     f"compute at most {money(cost.get('worst_case_before_usd_month'))} now, {money(cost.get('worst_case_after_usd_month'))} after")
    return compose(f"**{plural(ahead, 'new commit')} waiting for `{app}`.**", facts, f"say “deploy the latest {app}” to release them.")


def estimate(result, application=None):
    """estimate_cost."""
    app = application or "the app"
    floor = (result.get("idle_floor_usd_month") or 0) + (result.get("needs_floor_usd_month") or 0)
    facts = [f"{NEED_WORDS.get(item.get('need'), item.get('need'))} `{item.get('name')}`: {money(item.get('floor_usd_month'))} a month from the moment it exists"
             for item in result.get("needs") or [] if item.get("floor_usd_month")]
    scan = (result.get("release_overheads") or {}).get("image_scan_usd_per_release")
    if scan:
        facts.append(f"Each release with a new image adds about {money(scan)} for the image scan")
    # The headline already gives the bound, and a needs line the database's price: the findings add the levers.
    said = {"worst_case_bound"} | ({"database_always_on"} if facts else set())
    if not any(item.get("need") in ("bucket", "topic", "firestore") for item in result.get("needs") or []):
        said.add("needs_by_use")  # how buckets, topics and Firestore are billed matters only to an app that has one
    findings = sorted((item for item in result.get("findings") or [] if item.get("id") not in said and item.get("message")),
                      key=lambda item: item.get("severity") != "warning")
    facts += [clip(item["message"], 240) for item in findings[:3]]
    return compose(f"**Cost of `{app}`: about {money(floor)} a month with no visitors; compute at most "
                   f"{money(result.get('compute_ceiling_usd_month'))} a month even if fully busy.**", facts, None,
                   f"Bounds from Google's list prices (checked {result.get('pricing_verified') or 'unknown'}), not a bill.")


def controller(status):
    """get_controller_status."""
    number = status.get("version")
    if status.get("paused"):
        head = f"**Releases are paused** on the controller (version {number})."
    elif not status.get("live_dispatch"):
        head = f"**Releases are switched off** on the controller (version {number})."
    else:
        head = f"**The controller is running** version {number}; releases are on."
    credential, background = status.get("github_credential") or {}, status.get("background") or {}
    drift, guards = status.get("infrastructure_drift") or {}, status.get("spending_guards") or {}
    facts = []
    if credential.get("days_left") is not None:
        facts.append(f"GitHub access expires in {plural(credential['days_left'], 'day')}" + (f": {clip(credential['warning'], 160)}" if credential.get("warning") else ""))
    backup = (background.get("backup") or {}).get("last_run")
    if backup:
        facts.append(f"Last backup: {day(backup)}")
    if drift.get("status") == "clean":
        facts.append("The cloud setup matches the code" + (f" (checked {drift['age_days']:g} days ago)" if drift.get("age_days") is not None else ""))
    elif drift.get("drifted"):
        facts.append("The cloud setup differs from the code for: " + ", ".join(f"`{name}`" for name in sorted(drift["drifted"])))
    elif drift.get("status"):
        facts.append(f"Weekly cloud-setup check: {str(drift['status']).replace('_', ' ')}")
    if guards.get("missing"):
        facts.append("Google's own spend cap is not recorded for: " + ", ".join(guards["missing"]))
    if status.get("paused_applications"):
        facts.append("Paused apps: " + ", ".join(f"`{name}`" for name in status["paused_applications"]))
    if (status.get("configuration") or {}).get("error"):
        facts.append(f"The configuration file has an error: {clip(status['configuration']['error'], 200)}")
    problems = ((background.get("watchdog") or {}).get("last_result") or {}).get("problems") or []
    reports = sorted({problem.split(":")[0] for problem in problems if "no scheduled run recorded yet" in problem})
    others = [problem for problem in problems if "no scheduled run recorded yet" not in problem]
    if reports:
        facts.append("Weekly cost report not run yet for: " + ", ".join(f"`{name}`" for name in reports))
    facts += [f"Watchdog: {clip(problem, 220)}" for problem in others[:5]]
    attention = bool(others or credential.get("warning") or drift.get("drifted") or (status.get("configuration") or {}).get("error"))
    return compose(head, facts, "ask me about any item above." if attention else "nothing to do.")
