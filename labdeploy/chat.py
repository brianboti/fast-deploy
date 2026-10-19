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
    # A promotion's steps (.github/workflows/promote.yml).
    ("Check the dispatch inputs", "checking the request"),
    ("The commits are on main", "checking the commit is on main"),
    ("Read the reviewed specification", "checking platform.yaml"),
    ("Write the approval block", "writing the approval block"),
    ("Refuse to run without a reviewer's approval", "checking the approval"),
    ("Validate the production target", "checking the production target"),
    ("Verify the staging image's provenance", "verifying the staging image's signature"),
    ("Copy the image into production by digest", "copying the image into production"),
    ("Sign the promotion", "signing the promotion"),
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
LEVEL_WORDS = {"DEFAULT": "log", "DEBUG": "debug", "INFO": "info", "NOTICE": "notice", "WARNING": "warning", "ERROR": "error", "CRITICAL": "critical"}
SERIOUS_LEVELS = {"WARNING", "ERROR", "CRITICAL"}  # lines worth a rollback suggestion; INFO and below are the app's ordinary output
# A plan's state as the operator reads it in the plan list (releases; a rollback's words are in rollback_words).
PLAN_WORDS = {"planned": "planned but not released", "dispatching": "release started", "dispatched": "release started", "awaiting_workflow": "release started",
              "queued": "release started", "running": "in progress", "succeeded": "released", "failed": "failed", "cancelled": "cancelled",
              "timed_out": "timed out", "dispatch_lost": "never started", "dispatch_unknown": "unclear", "completed_unverified": "finished, not verified",
              "release_requested": "waiting for a merge", "expired": "expired"}
PROMOTION_WORDS = {"succeeded": "promoted to production", "failed": "promotion failed", "cancelled": "promotion cancelled", "timed_out": "promotion timed out",
                   "dispatched": "promotion started", "dispatching": "promotion started", "dispatch_unknown": "promotion unclear",
                   "dispatch_lost": "promotion never started", "completed_unverified": "promoted, not verified"}
PRODUCTION_ROLLBACK_WORDS = {"succeeded": "production rolled back", "failed": "production rollback failed", "cancelled": "production rollback cancelled",
                             "timed_out": "production rollback timed out", "dispatched": "production rollback started",
                             "dispatch_unknown": "production rollback unclear"}
ROLLBACK_WORDS = {"succeeded": "rolled back", "failed": "rollback failed", "cancelled": "rollback cancelled", "timed_out": "rollback timed out",
                  "dispatch_unknown": "rollback unclear"}
SETTING_WORDS = {"resources.memory_mib": "memory (MiB)", "resources.cpu": "CPUs", "concurrency": "requests in parallel per instance",
                 "scaling.minimum": "warm instances", "scaling.maximum": "most instances", "billing_model": "billing mode"}
# What to do after a release failed at a step, in plain words (labdeploy/status.py keeps the technical sentence in next_action).
PLAIN_STEP_ADVICE = (
    ("Validate dispatch inputs", "main moved on since the plan, or the request was malformed; ask me for a new plan."),
    ("Validate reviewed specification", "`platform.yaml` on main changed since the plan; ask me for a new plan."),
    ("Run unit tests", "the tests failed on main; the developer fixes them, then release again. The GitHub run names the failing tests."),
    ("Audit Python dependencies", "a library has a known vulnerability; the developer updates the libraries the GitHub run names, then release again."),
    ("Lint the Dockerfile", "the Dockerfile has problems that stop a release; the developer fixes the lines the GitHub run marks, then release again."),
    ("Build without cloud credentials", "the image did not build; the developer checks the Dockerfile and the GitHub run, then release again."),
    ("Validate artifact and deployment configuration", "the GitHub environment's settings differ from the reviewed target; the operator compares them "
     "with the cloud setup's outputs (docs/RUNBOOK.md)."),
    ("google-github-actions/auth", "signing in to Google Cloud failed; the operator checks the GitHub environment's variables and the identity federation."),
    ("Recover any interrupted previous release", "an earlier release left an unfinished checkpoint; the operator runs the recovery workflow before releasing again."),
    ("Push image and resolve immutable digest", "uploading the image failed; the operator checks the deployer's registry permission and the registry name."),
    ("Choose the provenance path", "the image could not be signed the way the plan asked; release again, or ask me for a new plan."),
    ("Install Cosign", "the signing tool could not be downloaded or did not match its checksum; release again, and tell the operator if it repeats."),
    ("Sign build provenance", "signing with the app's cloud key failed; the operator checks that the key exists (docs/SETUP.md, Build provenance)."),
    ("Verify build provenance", "the image's signature did not verify, so nothing was deployed; the GitHub run says which part differed."),
    ("Attest build provenance", "signing the image's provenance failed; the operator checks the workflow's attestations permission."),
    ("Summarize image vulnerabilities", "the image scan found serious problems with fixes available; the developer updates the libraries or the base "
     "image, then release again."),
    # A promotion's steps: nothing reached production before the release step.
    ("Check the dispatch inputs", "the promotion's request was refused before anything ran; ask me to promote again."),
    ("The commits are on main", "the commit is no longer on main, so it is not promoted; release the current main to staging, then promote that."),
    ("Read the reviewed specification", "`platform.yaml` no longer matches the staging release, or production's limits refuse it; release to staging again."),
    ("Refuse to run without a reviewer's approval", "the run had no recorded approval, so it stopped; ask me to add production again (it restores the "
     "environment's reviewers), then promote again."),
    ("Validate the production target", "the production environment's settings differ from the registered target; ask me to add production again, then promote."),
    ("Verify the staging image's provenance", "the staging image's signature did not verify, so nothing was copied; the GitHub run says which part differed."),
    ("Copy the image into production by digest", "copying the image into production failed; ask me to add production again (it checks the read "
     "access), then promote."),
    ("Sign the promotion", "signing the promotion with production's key failed; the operator checks that production's cloud setup was applied."),
)
CANCELLED_ERRORS = ("the run was cancelled", "KeyboardInterrupt", "SystemExit")  # a cancel's exception text is not an error to show


# ---- small pieces
def money(value):
    """Dollars as people write them: $0, $0.06, $8.57, $1,234.00."""
    if value is None:
        return "unknown"
    value = float(value)
    if 0 < value < 0.005:
        return "under $0.01"
    return "$0" if value == 0 else f"${value:,.2f}"


def clip(text, limit=200):
    """One line, at most `limit` characters, safe inside Markdown code: no backticks or line breaks."""
    line = " ".join(str(text or "").split()).replace("`", "'")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def sentence_clip(text, limit=240):
    """Like clip, but never mid-sentence: the text up to the last sentence end that fits, else the plain clip."""
    line = clip(text, 10_000)
    if len(line) <= limit:
        return line
    cut = max(line.rfind(". ", 0, limit), line.rfind("! ", 0, limit), line.rfind("? ", 0, limit))
    return line[: cut + 1] if cut >= limit // 3 else clip(line, limit)


def title(message, limit=60):
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return clip(first, limit)


def version(sha, message=None, limit=60):
    """A commit as the operator reads it: the short id, then its first line."""
    if not sha:
        return "an unknown commit"
    first = title(message, limit)
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


def when(value):
    """A moment as a table shows it, in UTC: 16 Oct 04:56."""
    at = moment(value)
    return f"{at.day} {at:%b %H:%M}" if at else "unknown"


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
    if count == 1:
        return f"{count} {word}"
    return f"{count} {word[:-1]}ies" if word.endswith("y") and word[-2] not in "aeiou" else f"{count} {word}s"


def about(value):
    """A rough amount: about $0.78, or under $0.01."""
    text = money(value)
    return text if text.startswith("under") else f"about {text}"


def access(exposure):
    return {"authenticated": "needs a Google sign-in token; not open in a browser",
            "lab-members": "lab members open it in a browser after a Google sign-in"}.get(exposure, exposure or "unknown")


def compose(head, facts=(), next_line=None, foot=None, block=None, after=None):
    """The one shape of every answer: the bold outcome, the facts as bullets (or a ready block), Next, then a small footer.
    `block` (a table, a numbered list) comes before the facts; `after` is a block that follows them."""
    parts = [head]
    if block:
        parts.append(block)
    facts = [fact for fact in facts if fact]
    if facts:
        parts.append("\n".join(f"- {fact}" for fact in facts))
    if after:
        parts.append(after)
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
    if plan.get("kind") == "promotion":
        return promotion(plan)
    if plan.get("kind") == "production_rollback":
        return production_rollback(plan)
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
        error = phases.get("error") or receipt.get("error")
        cancelled = bool(receipt.get("cancelled")) or str(error or "").strip().startswith(CANCELLED_ERRORS)  # found live: a raw `KeyboardInterrupt:`
        if cancelled:
            reason, error = "it was cancelled", None
        else:
            reason = {"preparing": "it stopped before deploying anything", "migrating": "the database migration failed, so nothing new was deployed",
                      "baking": "the new version took over, then failed its watch with real traffic"}.get(failed_phase, "the new version failed its health check")
        facts = [f"Why: {reason}", f"Error: `{clip(error, 240)}`" if error else None,
                 "The new version's last log lines are in the release details; ask me to show them" if phases.get("candidate_logs") else None]
        fix = ("fix the migration (check whether part of it ran), then release again." if failed_phase == "migrating"
               else "release again when ready." if cancelled else "the developer fixes the app, then release again.")
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
        return compose(head, [checks_line(checks)], plain_next(plan), foot)
    return compose(f"**`{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


def promotion(plan):
    """promote_to_production, and get_deployment_status or cancel_deployment for a promotion (docs/ROADMAP.md D37)."""
    app = plan.get("application") or "the app"
    state = plan.get("state")
    phases, receipt, workflow, source = plan.get("phases") or {}, plan.get("receipt") or {}, plan.get("workflow") or {}, plan.get("source") or {}
    what = version(plan.get("sha"), (plan.get("commit") or {}).get("message"))
    run = workflow.get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")
    foot = f"Plan `{str(plan.get('id') or '')[:8]}`" + (f" · {link('GitHub run', run)}" if run else "")
    digest = str(source.get("digest") or "").removeprefix("sha256:")[:12]
    image = (f"The exact image staging release `{str(source.get('plan') or '')[:8]}` deployed (`sha256:{digest}…`), copied by digest, never rebuilt"
             if digest else None)
    reviewers = " or ".join(f"`{name}`" for name in plan.get("reviewers") or []) or "a production reviewer"
    project = (plan.get("target") or {}).get("project_id")
    minutes = max(1, round(int(plan.get("bake_seconds") or 300) / 60))
    restored = "traffic_restored" in (phases.get("state"), receipt.get("state"))
    stuck = "rollback_failed_manual_recovery_required" in (phases.get("state"), receipt.get("state"))
    approved = ", ".join(f"`{name}`" for name in (receipt.get("promotion") or {}).get("approved_by") or [])
    steps = [int(step) for step in plan.get("rollout") or [] if isinstance(step, int)]
    in_steps = f"{', '.join(f'{step}%' for step in steps[:-1])} and {steps[-1]}%" if len(steps) > 1 else None
    if state in ("dispatching", "dispatched", "awaiting_workflow", "queued", "awaiting_approval"):
        approve = (f"approve it on GitHub: open {link('the run', run)}, choose Review deployments, read its approval block, then approve or reject."
                   if run else "approve it on GitHub: the platform repository's Actions tab, Promote to production, Review deployments.")
        return compose(f"**Waiting for approval: promote `{app}` to production** with {what}.",
                       [image, cost_line(plan.get("cost")), f"Only {reviewers} may approve it; nothing changes in production until then",
                        *[f"Note: {clip(item, 220)}" for item in (plan.get("warnings") or [])[:2]]],
                       approve, foot)
    if state == "running":
        facts = [f"Now: {doing(plan)}", f"Approved by {approved}" if approved else None,
                 f"It takes {in_steps} of production's traffic in turn, {minutes} min each; a failure at any step brings the previous version back "
                 "for everyone" if in_steps else None]
        if phases.get("state") == "baking" and not in_steps:
            facts.append(f"The new version already serves production; if it fails within {minutes} min, production goes back to its previous version on its own")
        return compose(f"**Promotion in progress for `{app}`**: {what}.", facts, "nothing to do; it continues on its own.", foot)
    if state == "succeeded" or (state in ("failed", "cancelled", "timed_out") and receipt.get("state") == "succeeded" and not restored):
        url = phases.get("url") or receipt.get("url")
        took = duration(workflow.get("created_at"), (plan.get("outcome") or {}).get("completed_at") or workflow.get("updated_at"))
        taken = [f"{step.get('percent')}%" for step in receipt.get("rollout") or [] if isinstance(step, dict)]
        stepped = (f"Took {', '.join(taken[:-1])} and {taken[-1]} of the traffic in turn, watched {minutes} min at each step"
                   if len(taken) > 1 else None)
        facts = [f"Address: {url} ({access((plan.get('spec') or {}).get('exposure'))})" if url else None, image,
                 f"Approved by {approved}" if approved else None, f"In project `{project}`" if project else None, stepped,
                 bake_line(phases, minutes), f"Took {took}, the wait for approval included" if took and state == "succeeded" else None]
        return compose(f"**`{app}` is live in production** with {what}.", facts,
                       f"nothing to do. To undo it, say “roll {app} back in production”.", foot)
    if stuck:
        error = phases.get("restore_error") or receipt.get("restore_error")
        return compose(f"**Needs you now: production for `{app}` could not be put back on its previous version.**",
                       [f"Error: `{clip(error, 240)}`" if error else None, f"Before the promotion it served: {traffic(phases.get('previous_traffic'))}"],
                       "restore production's traffic by hand (docs/RUNBOOK.md, Production), then tell me.", foot)
    if restored:
        error = phases.get("error") or receipt.get("error")
        cancelled = bool(receipt.get("cancelled")) or str(error or "").strip().startswith(CANCELLED_ERRORS)
        failed_phase = phases.get("failed_phase") or receipt.get("failed_phase")
        share = receipt.get("step")
        reason = ("it was cancelled" if cancelled else
                  f"the new version failed its watch with {share}% of the traffic" if share and share < 100 and failed_phase == "baking" else
                  f"the new version failed its health check with {share}% of the traffic" if share and share < 100 and failed_phase == "promoting" else
                  {"preparing": "it stopped before deploying anything", "migrating": "the database migration failed, so nothing new was deployed",
                   "baking": "the new version took over, then failed its watch with real traffic"}.get(failed_phase, "the new version failed its health check"))
        return compose(f"**Promotion stopped for `{app}`: production's previous version is serving.**",
                       [f"Why: {reason}", None if cancelled or not error else f"Error: `{clip(error, 240)}`"],
                       "promote again when ready." if cancelled else "the developer fixes the app and releases it to staging; then promote that release.", foot)
    if state == "dispatch_unknown":
        return compose(f"**It is unclear whether GitHub received the promotion of `{app}`.**", [],
                       "I'll look for its run on the platform repository before trying again.", foot)
    if state == "dispatch_lost":
        return compose(f"**The promotion of `{app}` never started**: GitHub never ran it, so nothing changed.", [], "ask me to promote it again.", foot)
    if state in ("failed", "timed_out", "cancelled"):
        step = (plan.get("failed_step") or {}).get("step")
        head = (f"**The promotion of `{app}` stopped while {step_words(step)}.** Nothing changed in production." if step
                else f"**The promotion of `{app}` did not run.** Nothing changed in production.")
        return compose(head, [], plain_next(plan) if step else plan.get("next_action"), foot)
    return compose(f"**Promotion of `{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


def production_rollback(plan):
    """rollback_production, and get_deployment_status for it: production's traffic back to the split before its live promotion."""
    app = plan.get("application") or "the app"
    state, receipt = plan.get("state"), plan.get("receipt") or {}
    run = (plan.get("workflow") or {}).get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")
    foot = f"Plan `{str(plan.get('id') or '')[:8]}`" + (f" · {link('GitHub run', run)}" if run else "")
    previous = plan.get("previous") or {}
    back_to = version(previous["sha"], previous.get("message")) if previous.get("sha") else f"`{previous.get('revision') or 'the previous revision'}`"
    undone = version(plan.get("sha"), (plan.get("commit") or {}).get("message")) if plan.get("sha") else f"`{plan.get('from_revision')}`"
    reviewers = " or ".join(f"`{name}`" for name in plan.get("reviewers") or []) or "a production reviewer"
    if state in ("dispatching", "dispatched", "awaiting_workflow", "queued", "awaiting_approval"):
        approve = (f"approve it on GitHub: open {link('the run', run)}, choose Review deployments, then approve." if run
                   else "approve it on GitHub: the platform repository's Actions tab, Promote to production, Review deployments.")
        return compose(f"**Waiting for approval: roll `{app}` back in production.**",
                       [f"Traffic returns to {back_to}", f"No longer serving: {undone}",
                        f"Only {reviewers} may approve it; production's traffic stays where it is until then"], approve, foot)
    if state == "running":
        return compose(f"**Rolling `{app}` back in production**: visitors move to {back_to} within a minute.", [], "nothing to do; ask me in a minute.", foot)
    if state == "succeeded" and receipt.get("state") == "rolled_back":
        return compose(f"**`{app}` is back on its previous version in production.**",
                       [f"Now serving: {back_to}", f"No longer serving: {undone}", "Database changes and anything the app already did are not undone"],
                       "the developer fixes the code and releases it to staging; then promote that release.", foot)
    if state in ("failed", "cancelled", "timed_out"):
        return compose(f"**The production rollback of `{app}` did not complete**; production's traffic was not changed.", [],
                       "open the GitHub run: a reviewer may have rejected it, or traffic moved since the promotion and the runner refused.", foot)
    return compose(f"**Production rollback of `{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


def recovery_words(plan):
    """What the recovery check that runs after a stopped release found, in plain words; empty when none ran."""
    recovery = plan.get("recovery_run") or {}
    if not recovery:
        return ""
    if recovery.get("conclusion") is None:
        return " The check that runs after a stopped release is still running."
    found = (recovery.get("receipt") or {}).get("state")
    if recovery.get("conclusion") == "success" and found in (None, "no_recovery_needed"):
        return " The check that followed confirmed nothing needed restoring."
    if recovery.get("conclusion") == "success" and found in ("rolled_back", "traffic_restored"):
        return " The check that followed put visitors back on the previous version."
    if recovery.get("conclusion") == "success":
        return f" The check that followed ended with '{str(found).replace('_', ' ')}'."
    return f" The check that followed did not succeed ('{recovery.get('conclusion')}'); open its run."


def plain_next(plan):
    """The Next line of a failed, cancelled or timed-out release in the operator's words. The pipeline's own sentence
    (next_action, from labdeploy/status.py) names files and run states they cannot open; the GitHub run link in the footer keeps the detail."""
    step = (plan.get("failed_step") or {}).get("step") or ""
    for needle, words in PLAIN_STEP_ADVICE:
        if needle in step:
            return words
    state = plan.get("state")
    if state == "cancelled":
        return "it was cancelled before anything changed, so the previous version kept serving." + recovery_words(plan) + " Release again when ready."
    if state == "timed_out":
        return "the run took too long and GitHub stopped it; nothing new is serving." + recovery_words(plan) + " Release again, and tell the operator if it repeats."
    if state == "failed":
        where = f" while {step_words(step)}" if step else ""
        return f"it failed{where}; the GitHub run has the detail." + recovery_words(plan) + " Release again once that is fixed."
    return plan.get("next_action")


def rollback(plan):
    app = plan.get("application") or "the app"
    state, receipt = plan.get("state"), plan.get("receipt") or {}
    run = (plan.get("workflow") or {}).get("html_url") or (plan.get("dispatch_result") or {}).get("html_url")
    foot = f"Plan `{str(plan.get('id') or '')[:8]}`" + (f" · {link('GitHub run', run)}" if run else "")
    # The commit, not the revision name (found live: `lab-notebook-r9111fd55b0d94066`): the controller resolves the
    # previous version's commit when it has released it; the release rolled back from is the plan's own commit.
    previous = plan.get("previous") or {}
    back_to = version(previous["sha"], previous.get("message")) if previous.get("sha") else "the previous version"
    undone = version(plan.get("sha"), (plan.get("commit") or {}).get("message")) if plan.get("sha") else f"`{receipt.get('rolled_back_from')}`"
    if state == "succeeded" and receipt.get("state") == "rolled_back":
        split = receipt.get("restored_traffic")
        shared = f" (traffic split {traffic(split)})" if isinstance(split, dict) and len(split) > 1 else ""
        return compose(f"**`{app}` is back on its previous version.**",
                       [f"Now serving: {back_to}{shared}", f"No longer serving: {undone}",
                        "Database changes and anything the app already did are not undone"],
                       "the developer fixes the code; then release again.", foot)
    if state in ("dispatching", "dispatched", "awaiting_workflow", "queued", "running"):
        return compose(f"**Rolling `{app}` back**: visitors move to {back_to} within about a minute.", [],
                       "nothing to do; ask me in a minute.", foot)
    if state in ("failed", "cancelled", "timed_out"):
        return compose(f"**The rollback of `{app}` did not complete**; visitors were not moved.", [],
                       "open the GitHub run, then try again or release the wanted commit.", foot)
    return compose(f"**Rollback of `{app}`: {str(state).replace('_', ' ')}.**", [], plan.get("next_action"), foot)


# ---- onboarding and repositories
def onboarding(view, now=None):
    """onboard_application and get_onboarding_status."""
    app, state, steps = view.get("application") or "the app", view.get("state"), view.get("steps") or {}
    where = f"`{view.get('repository')}`" + (f", folder `{view['path']}`" if view.get("path") else "") + (", as it is" if view.get("as_is") else "")
    if state == "registered_by_hand":  # found live: a registered app without an onboarding record was told to onboard itself
        live = view.get("live_release") or {}
        facts = [f"From {where}",
                 f"Live: {version(live['sha'], (live.get('commit') or {}).get('message'))}, released {day(live.get('completed_at'))}" if live.get("sha")
                 else "Not released by this controller yet"]
        return compose(f"**`{app}` was registered by hand**, not onboarded from chat, so there is no onboarding to follow.", facts,
                       f"ask me how `{app}` is doing (its health) or which apps are live.")
    listed = AS_IS_STEPS if view.get("as_is") else ONBOARDING_STEPS
    names, order = dict(listed), [key for key, _ in listed]
    prs = [link(f"#{(steps.get(key) or {})['pull_request']}", (steps.get(key) or {}).get("url"))
           for key in ("contract", "infrastructure", "workflows") if (steps.get(key) or {}).get("pull_request")]
    pull_requests = "Pull requests: " + ", ".join(prs) if prs else None
    if state == "complete":
        measures = view.get("measures") or {}
        took = f" in {measures['minutes']:g} min" if measures.get("minutes") is not None else ""
        plan_id = str(view.get("plan_id") or "")[:8]
        finished = moment(view.get("completed_at"))
        age = ((moment(now) or datetime.datetime.now(datetime.UTC)) - finished).total_seconds() if finished else 0
        recent = age < 3600  # found live: days later the answer still promised to follow the first release
        facts = [f"From {where}", pull_requests,
                 f"Stopped {plural(measures['blocked'], 'time')} on the way; each was fixed and it continued" if measures.get("blocked") else None,
                 (f"First release started: plan `{plan_id}`" if recent else f"Its first release was plan `{plan_id}`") if plan_id else None]
        head = f"**`{app}` is onboarded**{took}." if recent else f"**`{app}` was onboarded** on {day(view.get('completed_at'))}{took}."
        return compose(head, facts, "I'll follow the first release; ask me how it is going." if recent
                       else f"ask me about `{app}`'s health or releases.")
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
    if as_is.get("refusals"):  # one refusal per line (found live: three in one 400-character sentence)
        groups = grouped_refusals(as_is["refusals"])
        block += "\n\nAs it is, it is refused too:\n" + "\n".join(f"{number}. {files} {str(problem).rstrip('.')}. Fix: {str(fix).rstrip('.')}."
                                                               for number, (files, problem, fix) in enumerate(groups[:5], 1))
        if len(groups) > 5:
            block += f"\n…and {len(groups) - 5} more in the details"
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


def production(result):
    """add_production: which of its four steps the application's production is at, and the one thing that is next."""
    app, environment, project = result.get("application"), result.get("environment"), result.get("project_id")
    state = result.get("state")
    pull_request = result.get("pull_request") or {}
    numbered = link(f"#{pull_request['number']}", pull_request.get("url")) if pull_request.get("number") else "the pull request"
    reviewers = " or ".join(f"`{name}`" for name in result.get("reviewers") or [])
    if state == "ready":
        return compose(f"**`{app}` has its production** in `{project}`.",
                       [f"Environment `{environment}` on the platform repository: every promotion waits for {reviewers} to approve it on GitHub",
                        "A promotion copies the exact image staging released, never a rebuild"],
                       f"say “promote {app} to production” after a staging release you are happy with.")
    if state == "applying":
        run = (result.get("apply_run") or {}).get("url")
        waiting = (result.get("apply_run") or {}).get("state") == "waiting"
        return compose(f"**Production for `{app}`, step 2 of 4: its cloud setup is being built in `{project}`.**",
                       ["The production root is merged; the infrastructure pipeline applies it",
                        f"It waits for a person to approve the apply in `infra-production`: {link('the run', run)}" if waiting and run else None],
                       "approve the apply on GitHub, then ask again." if waiting else "ask again once the apply has finished.")
    if state == "access":
        return compose(f"**Production for `{app}`, step 3 of 4: its environment is ready; one pull request left.**",
                       [f"Environment `{environment}`: every promotion waits for {reviewers} to approve it on GitHub",
                        f"Pull request {numbered} lets production's deployer read the images staging released, so a promotion can copy one"],
                       "merge it once its plan reads right, then ask again.")
    again = not (result.get("steps") or {}).get("registered")  # its production existed: the root changes (a newer platform, say)
    return compose(f"**Production for `{app}`, step 1 of 4: its cloud setup in `{project}`.**",
                   [f"Pull request {numbered} {'updates' if again else 'adds'} its production root; the pipeline posts the plan on it",
                    "A person merges it (production infrastructure is never merged by the controller), then approves the apply in `infra-production`"],
                   "merge it once the plan reads right and approve the apply, then ask again: the environment and its approvers come next.")


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
        if app.get("production") is not None:
            promoted = app.get("production_release") or {}
            notes.append(f"`{app_id}` in production: {version(promoted['sha'], (promoted.get('commit') or {}).get('message'))} since "
                         f"{day(promoted.get('completed_at'))}" if promoted.get("sha") else f"`{app_id}` has a production; nothing promoted to it yet")
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


def budget_line(budget):
    """Production's objective and its error budget in plain words, or None where there is none (docs/ROADMAP.md D39)."""
    if not isinstance(budget, dict) or not budget.get("goal"):
        return None
    goal, left, good = budget["goal"], budget.get("budget_remaining"), budget.get("compliance")
    target = f"objective {goal * 100:g}% of requests without a server error over {budget.get('days', 30)} days"
    if left is None:
        return f"Error budget: no data yet ({target})"
    shown = "spent" if left <= 0 else f"{left:.0%} left"
    return f"Error budget: {shown}" + (f", {good * 100:.2f}% of requests fine so far" if good is not None else "") + f" ({target})"


def health(result):
    """get_service_health, for staging or for production (its error budget too)."""
    app = result.get("application") or "the app"
    production = result.get("environment") == "production"
    name = f"`{app}`" + (" in production" if production else "")
    service, metrics = result.get("service") or {}, result.get("metrics") or {}
    window = metrics.get("window_minutes") or 60
    errors, uptime, requests = metrics.get("server_errors"), metrics.get("uptime_checks_passed"), metrics.get("requests")
    unreachable = uptime is not None and uptime < 0.99
    if service.get("ready") is False:
        head = f"**{name} is not ready**" + (f": {clip(service.get('ready_message'), 200)}" if service.get("ready_message") else ".")
    elif errors:
        head = f"**{name} is serving, with {plural(errors, 'server error')} in the last {window} min.**"
    elif unreachable:
        head = f"**{name} may not be reachable**: the uptime check passed in only {percent(uptime)} of its probes."
    else:
        head = f"**{name} is healthy.**"
    serving = (service.get("traffic") or [{}])[0]
    p95 = metrics.get("request_latency_ms_p95")
    facts = [(f"Last {window} min: {plural(requests, 'request')}, {plural(errors or 0, 'server error')}"
              + (f", 95% answered within {round(float(p95), 1):g} ms" if p95 is not None else "")) if requests is not None else f"Last {window} min: no traffic data",
             f"Uptime checks: {percent(uptime)} passed" if uptime is not None else None,
             (f"Serving: {version(serving.get('commit'))}" + (" (the release the controller recorded)" if result.get("agreement")
              else " (not the release the controller recorded; ask me to compare)")) if serving.get("commit") else None,
             f"Access: {access((result.get('access') or {}).get('exposure'))}",
             f"Memory: {percent(metrics['memory_utilization_p95'])} of its limit at the busiest" if metrics.get("memory_utilization_p95") is not None else None,
             budget_line(result.get("error_budget")),
             *[f"Note: {clip(note, 220)}" for note in (result.get("notes") or [])[:3]]]
    trouble = bool(errors) or service.get("ready") is False or unreachable
    where = " in production" if production else ""
    return compose(head, facts, (f"look at the error lines (“show {app}'s errors”); if the latest release caused it, “roll {app} back{where}”."
                                 if trouble else "nothing to do."), link("Cloud console", result.get("console_url")) if result.get("console_url") else None)


def logs(result):
    """get_recent_logs."""
    app, minutes, count = result.get("application") or "the app", result.get("minutes") or 30, result.get("count") or 0
    severity = str(result.get("severity") or "ERROR").upper()
    level = LEVEL_WORDS.get(severity, severity.lower())
    if not count:
        return compose(f"**No {level} lines for `{app}`** in the last {minutes} minutes.", [], "nothing to do.")
    shown = (result.get("lines") or [])[:5]

    def line(item):
        at = moment(item.get("at"))
        return (f"{at:%H:%M:%S} " if at else "") + clip(item.get("message"), 160)
    block = "```\n" + "\n".join(line(item) for item in shown) + "\n```"
    more = f" (the newest {len(shown)} shown)" if count > len(shown) else ""
    # A rollback is worth suggesting for warnings and worse; INFO and DEFAULT lines are the app's ordinary output (found live).
    next_line = (f"if this started with the latest release, “roll {app} back”; otherwise the developer reads the full lines." if severity in SERIOUS_LEVELS
                 else f"nothing to do: these are ordinary lines, not problems. Ask me for `{app}`'s ERROR lines to see only what went wrong.")
    return compose(f"**{plural(count, level + ' line')} for `{app}`** in the last {minutes} minutes{more}:", [], next_line, block=block)


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
    scenario = result.get("scenario") or {}
    if scenario.get("requests_per_month") is not None:
        usd = scenario.get("usd_month") or {}
        facts.insert(0, f"With {int(scenario['requests_per_month']):,} requests a month of {float(scenario.get('average_request_seconds') or 0):g} s each: "
                        f"between {money(usd.get('best_case'))} and {money(usd.get('worst_case'))} a month, depending on how many overlap")
    scan = (result.get("release_overheads") or {}).get("image_scan_usd_per_release")
    if scan:
        facts.append(f"Each release with a new image adds about {money(scan)} for the image scan")
    # The headline already gives the bound, and a needs line the database's price: the findings add the levers.
    said = {"worst_case_bound"} | ({"database_always_on"} if facts else set())
    if not any(item.get("need") in ("bucket", "topic", "firestore") for item in result.get("needs") or []):
        said.add("needs_by_use")  # how buckets, topics and Firestore are billed matters only to an app that has one
    findings = sorted((item for item in result.get("findings") or [] if item.get("id") not in said and item.get("message")),
                      key=lambda item: item.get("severity") != "warning")
    facts += [sentence_clip(item["message"], 240) for item in findings[:3]]  # whole sentences (found live: a finding cut mid-sentence)
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
    elif credential.get("kind") == "github_app":
        facts.append("GitHub access through the GitHub App: its tokens renew every hour")
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
        facts.append("Weekly cost report not run yet for: " + ", ".join(f"`{name}`" for name in reports) + "; it runs on its own every week")
    facts += [f"Watchdog: {clip(problem, 220)}" for problem in others[:5]]
    attention = bool(others or credential.get("warning") or drift.get("drifted") or (status.get("configuration") or {}).get("error"))
    next_line = ("ask me about any item above." if attention
                 else "nothing to do; the weekly cost report runs on its own. Tell the operator only if it is still missing after a week." if reports
                 else "nothing to do.")
    return compose(head, facts, next_line)


# ---- history, costs and the reader (the five tools that answered with raw JSON until 1.17.1)
def started(item):
    """When a plan was made: it expires 15 minutes after that."""
    return item["expires_at"] - 900 if isinstance(item.get("expires_at"), (int, float)) else None


def plan_words(item):
    state = item.get("state")
    if item.get("kind") == "rollback":
        return ROLLBACK_WORDS.get(state, "rollback " + PLAN_WORDS.get(state, str(state).replace("_", " ")))
    if item.get("kind") == "promotion":
        return PROMOTION_WORDS.get(state, "promotion " + PLAN_WORDS.get(state, str(state).replace("_", " ")))
    if item.get("kind") == "production_rollback":
        return PRODUCTION_ROLLBACK_WORDS.get(state, "production rollback " + PLAN_WORDS.get(state, str(state).replace("_", " ")))
    return PLAN_WORDS.get(state, str(state).replace("_", " "))


def plans(result, shown=8):
    """list_plans: the newest plans as one table, and what the whole history adds up to."""
    items = [item for item in result.get("plans") or [] if isinstance(item, dict)]
    if not items:
        return compose("**No plans yet**: nothing has been planned or released from this controller.", [], "say “deploy the latest APP” to make the first.")
    counts = {}
    for item in items:
        counts[plan_words(item)] = counts.get(plan_words(item), 0) + 1
    tally = ", ".join(f"{count} {words}" for words, count in sorted(counts.items(), key=lambda pair: -pair[1]))
    rows = [f"| `{item.get('application')}` | {plan_words(item)} | {version(item.get('sha'), item.get('summary'), 40)} | "
            f"{when(item.get('completed_at') or started(item))} |" for item in items[:shown]]
    table = "| App | Outcome | Version | When (UTC) |\n|---|---|---|---|\n" + "\n".join(rows)
    apps = sorted({item.get("application") for item in items if item.get("application")})
    head = f"**{plural(len(items), 'plan')} for {plural(len(apps), 'app')}**: {tally}." + (f" The newest {len(rows)}:" if len(items) > len(rows) else "")
    newest = items[0]
    if newest.get("kind") != "rollback" and newest.get("state") in ("dispatching", "dispatched", "awaiting_workflow", "queued", "running"):
        next_line = f"the newest release (`{newest.get('application')}`) is still on its way; ask me how it is going."
    else:
        next_line = "ask me about any of these (“where is lab-bare's release?”); the full list is in the details."
    return compose(head, [], next_line, block=table)


def event_words(item):
    kind, name = item.get("kind") or "", item.get("name") or ""
    if kind == "tool":
        return f"`{name}`" if name else "a tool call"
    if kind == "rest":
        return f"REST {name}" if name else "a REST call"
    return (kind.replace("_", " ") + (f" {name}" if name else "")).strip() or "an event"


def events(result, shown=8):
    """list_events: the audit trail as a short table with readable times."""
    items = [item for item in result.get("events") or [] if isinstance(item, dict)]
    if not items:
        return compose("**No events recorded** for this question.", [], "nothing to do.")
    errors = [item for item in items if item.get("outcome") in ("error", "throttled")]
    who = sorted({item.get("identity") or item.get("client") or "unknown" for item in items})
    rows = []
    for item in items[:shown]:
        outcome = str(item.get("outcome") or "")
        said = f"error: {clip(item.get('detail'), 70)}" if outcome == "error" and item.get("detail") else outcome.replace("_", " ") or "—"
        app = f"`{item['application']}`" if item.get("application") else "—"
        rows.append(f"| {when(item.get('at'))} | {item.get('identity') or item.get('client') or 'unknown'} | {event_words(item)} | {app} | {said} |")
    table = "| When (UTC) | Who | What | App | Outcome |\n|---|---|---|---|---|\n" + "\n".join(rows)
    head = (f"**{plural(len(items), 'event')}**, {plural(len(errors), 'error')}, from {plural(len(who), 'identity')}"
            + (f"; the newest {len(rows)}:" if len(items) > len(rows) else ":"))
    next_line = "ask me what went wrong in the errors." if errors else "nothing to do; ask me for one app's events, or for more of them."
    return compose(head, [], next_line, block=table)


def proposal_words(item):
    setting = SETTING_WORDS.get(item.get("setting"), str(item.get("setting")))
    saving = item.get("estimated_monthly_saving_usd")
    saves = f"; saves {about(saving)} a month" if saving and saving > 0 else ""
    return f"{setting} {item.get('current')} to {item.get('recommended')} ({clip(item.get('reason'), 60)}{saves})"


def cost_report(result, application=None):
    """get_cost_report: the weekly measured report; nothing in it is estimated except the bounds it repeats."""
    report = result.get("report")
    app = application or (report or {}).get("service") or "the app"
    if not report:
        return compose(f"**No measured cost report yet for `{app}`.**", [clip(result.get("note"), 200) if result.get("note") else None],
                       "it runs on its own every week; ask again after the first run, or the operator starts the right-sizing workflow in GitHub Actions.")
    window = report.get("window_days")
    made = day(report.get("generated_at") or result.get("generated_at"))
    head = f"**Measured costs for `{app}`**" + (f", the {window} days to {made}." if window else f", from {made}.")
    facts = []
    actual = report.get("actual_spend") or {}
    if actual.get("total_net_cost_usd") is not None:
        line = f"Actual spend, last {actual.get('window_days') or window} days: {money(actual['total_net_cost_usd'])} for the whole project, net of credits"
        if (actual.get("app") or {}).get("net_cost_usd") is not None:
            line += f"; `{app}`'s share {money(actual['app']['net_cost_usd'])}"
        facts.append(line)
    elif actual.get("warning"):
        facts.append(f"Actual spend unknown: {sentence_clip(actual['warning'], 200)}")
    observed = (report.get("rightsizing") or {}).get("observed") or {}
    if observed:
        facts.append(f"Measured use: {plural(observed.get('requests') or 0, 'request')} in the window, worth "
                     f"{about(observed.get('usd_month_at_list_price'))} a month at list prices")
    else:
        facts.append("No usage was measured in the window")
    bounds = report.get("estimate") or {}
    if bounds:
        facts.append(f"Bounds: {money(bounds.get('idle_floor_usd_month'))} a month with no visitors; compute at most {money(bounds.get('compute_ceiling_usd_month'))} a month")
    proposals = [item for item in (report.get("rightsizing") or {}).get("recommendations") or [] if item.get("setting")]
    concrete = [item for item in proposals if item["setting"] in SETTING_WORDS and item["setting"] != "billing_model"]
    if proposals:
        facts.append("Proposals: " + "; ".join(proposal_words(item) for item in proposals[:3]) + (f"; and {len(proposals) - 3} more" if len(proposals) > 3 else ""))
    else:
        facts.append("Proposals: none; the settings fit the measured use")
    drift = report.get("drift")
    if isinstance(drift, list) and drift:
        facts.append("Live settings differ from `platform.yaml`: " + "; ".join(f"{item.get('setting')} is {item.get('live')} live but {item.get('platform_yaml')} reviewed"
                                                                         for item in drift[:3]))
    tolls = report.get("release_tolls") or {}
    if tolls.get("fresh_images") is not None:
        facts.append(f"This month: {plural(tolls['fresh_images'], 'new image')} scanned, {about(tolls.get('image_scans_usd'))}")
    if ((result.get("spending_guards") or {}).get("cloud_run_spend_cap") or {}).get("status") == "not_recorded":
        facts.append("Google's own spend cap is not recorded for this project")
    next_line = (f"say “make {app} cheaper” and I'll open a pull request with these proposals; a person merges it." if concrete
                 else "nothing to do; the report runs again next week.")
    return compose(head, facts, next_line, link("GitHub run", result.get("run_url")) if result.get("run_url") else None)


def analysis(result, application=None):
    """analyze_repository: what the reader found in a registered app's repository, and what a person must confirm."""
    app = application or "the app"
    draft = result.get("draft_spec") or {}
    where = f" at {version(result.get('commit_sha'))}" if result.get("commit_sha") else ""
    folder = f", folder `{result['path']}`" if result.get("path") else ""
    if result.get("supported_candidate"):
        head = f"**`{app}`{folder} read{where}: a Python web service the platform can run.**"
    else:
        head = f"**`{app}`{folder} read{where}: no Python dependency list was found**, so the platform cannot tell what it is."
    resources, scaling = draft.get("resources") or {}, draft.get("scaling") or {}
    settings = [f"port {draft['port']}" if draft.get("port") else None, f"Python {draft['python']}" if draft.get("python") else None,
                f"{draft['concurrency']} requests in parallel per instance" if draft.get("concurrency") else None,
                f"{resources.get('cpu')} CPU and {resources.get('memory_mib')} MiB" if resources.get("cpu") and resources.get("memory_mib") else None,
                f"up to {scaling['maximum']} instances" if scaling.get("maximum") else None]
    facts = ["Draft settings: " + ", ".join(item for item in settings if item) if any(settings) else None]
    facts += [f"Found: {sentence_clip(line, 200)}" for line in (result.get("evidence") or [])[:3]]
    if draft.get("dependencies"):
        facts.append("Services it uses that the platform does not provide: " + ", ".join(str(item) for item in draft["dependencies"]))
    if result.get("suggested_needs"):
        facts.append("Needs the dependencies suggest: " + "; ".join(clip(item, 80) for item in result["suggested_needs"][:3]))
    parts = (result.get("compose") or {}).get("parts")
    if parts:
        facts.append(f"Its Compose file names {plural(len(parts), 'part')}; the details say where each one goes")
    questions = result.get("review_required") or []
    after = ("To confirm before deploying:\n" + "\n".join(f"{number}. {sentence_clip(question, 220)}" for number, question in enumerate(questions[:5], 1))
             + (f"\n…and {len(questions) - 5} more in the details" if len(questions) > 5 else "")) if questions else None
    return compose(head, facts, "nothing changed; a person confirms these and reviews `platform.yaml` (ask me to draft it if the repository has none).", after=after)


def rightsizing(result):
    """propose_rightsizing."""
    app, pr = result.get("application") or "the app", result.get("pull_request")
    if not isinstance(pr, dict) or not pr.get("number"):
        note = clip(result.get("note") or "the latest report proposes no setting change", 200).rstrip(".")
        return compose(f"**Nothing to propose for `{app}`**: {note[0].lower() + note[1:] if note else note}.", [], "nothing to do; the report runs again next week.")
    number = link(f"#{pr['number']}", pr.get("url"))
    if not pr.get("changed"):
        return compose(f"**A right-sizing pull request is already open for `{app}`**: {number}.", [], "review and merge it; the next release applies it.")
    saving = result.get("estimated_monthly_saving_usd")
    changes = ", ".join(f"{SETTING_WORDS.get(item.get('setting'), item.get('setting'))} to {item.get('value')}" for item in result.get("changes") or [])
    cost = result.get("cost") or {}
    facts = [f"Changes: {changes}" if changes else None,
             (f"Compute at most {money(cost.get('worst_case_before_usd_month'))} a month now, {money(cost.get('worst_case_after_usd_month'))} after; "
              f"with no visitors {money(cost.get('idle_floor_before_usd_month'))} now, {money(cost.get('idle_floor_after_usd_month'))} after") if cost else None]
    head = f"**Right-sizing pull request opened for `{app}`**: {number}" + (f", saving about {money(saving)} a month" if saving and saving > 0 else "") + "."
    return compose(head, facts, "a person reviews and merges it; the next release applies it. Nothing changes until then.")
