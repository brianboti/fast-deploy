"""Plain-English deployment workspace with evidence-driven progress and progressive detail."""

import datetime
import json
from urllib.parse import urlencode

from . import settings_page as ui
from .deployments import LABELS

STYLE = """
.deployment{--ink:#172e3b;--quiet:#647785;--line:#dfe7eb;--teal:#087f78;color:var(--ink)}

.deployment .d-eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--quiet)}

.deployment .d-top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}

.deployment h1{font-size:32px;letter-spacing:-1.1px;margin:6px 0 8px;line-height:1.16}

.deployment h2{font-size:17px;letter-spacing:-.3px;margin:0}
.deployment h3{font-size:14px;margin:0}

.deployment p{margin:6px 0;color:var(--quiet);line-height:1.6}
.deployment .d-caption{font-size:12px;color:var(--quiet)}

.deployment .d-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.deployment .d-tools a,.deployment .d-tools button{white-space:normal}

.deployment .d-live{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:#176c61;font-weight:600}

.deployment .d-dot{width:7px;height:7px;flex:none;border-radius:50%;background:currentColor}

.deployment .d-hero{border:1px solid #ccdfe0;background:linear-gradient(115deg,#f2fbf9,#fff 65%);border-radius:18px;padding:26px;margin-bottom:20px;box-shadow:0 8px 30px #173a4610}

.deployment .d-hero-head{display:flex;align-items:center;gap:22px}
.deployment .d-hero h2{font-size:23px;line-height:1.3;letter-spacing:-.6px}

.deployment .d-ring{width:94px;height:94px;flex:none}
.deployment .d-ring text{font:600 19px system-ui;fill:var(--ink)}

.deployment .d-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;margin-top:24px;padding-top:18px;border-top:1px solid #dce8e6}

.deployment .d-stat strong{display:block;font-size:19px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}

.deployment .d-columns{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(240px,1fr);gap:20px;align-items:start}

.deployment .d-panel{background:white;border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:18px;min-width:0}

.deployment .d-panel-head{padding:19px 20px;border-bottom:1px solid #edf1f3}
.deployment .d-pad{padding:18px 20px}

.deployment .d-job{border-bottom:1px solid #edf1f3}
.deployment .d-job:last-child{border-bottom:0}

.deployment summary{padding:16px 20px;cursor:pointer;list-style:none;display:flex;gap:10px;align-items:center}

.deployment summary::-webkit-details-marker{display:none}
.deployment summary:after{content:'+';margin-left:auto;color:var(--quiet)}

.deployment details[open]>summary:after{content:'−'}
.deployment summary .d-job-name{font-weight:600;overflow-wrap:anywhere}

.deployment .d-steps{margin:0;padding:0 20px 14px;list-style:none}
.deployment .d-step{display:grid;grid-template-columns:25px minmax(0,1fr) auto;gap:10px;align-items:start;padding:10px 0;position:relative}

.deployment .d-step:not(:last-child):before{content:'';position:absolute;top:36px;bottom:-7px;left:11px;width:1px;background:#dce5e9}

.deployment .d-symbol{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:#eef2f5;color:#647785;font-size:12px;flex:none}

.deployment .done{color:#176d58;background:#e5f3ec}
.deployment .running{color:#126da0;background:#e6f1fa}
.deployment .failed{color:#a43439;background:#fdecef}

.deployment .cancelled,.deployment .skipped{background:#f2f1ed;color:#7a7157}
.deployment .d-step-name{overflow-wrap:anywhere;font-size:13px}

.deployment .d-badge{display:inline-flex;align-items:center;gap:6px;border-radius:6px;padding:4px 8px;font-size:11px;font-weight:600;white-space:normal}

.deployment .d-timing{font-size:11px;color:var(--quiet);font-variant-numeric:tabular-nums;padding-top:3px}

.deployment .d-facts{margin:0}
.deployment .d-facts div{padding:9px 0;border-bottom:1px solid #edf1f3}
.deployment .d-facts div:last-child{border:0}

.deployment dt{font-size:11px;color:var(--quiet);margin-bottom:3px}
.deployment dd{margin:0;font-size:13px;overflow-wrap:anywhere}

.deployment .d-notice{border-left:3px solid #c78b25;background:#fff8e9;padding:14px 17px;border-radius:0 8px 8px 0;margin:16px 0;color:#775319}

.deployment .d-notice p{color:inherit}
.deployment .d-error{border-color:#bd4c4c;background:#fff1f1;color:#873e3e}

.deployment .d-checks{display:grid;gap:9px}
.deployment .d-check{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}

.deployment .d-traffic{display:flex;height:8px;border-radius:6px;overflow:hidden;background:#edf2f4;margin:10px 0}
.deployment .d-traffic span{background:#087f78}

.deployment .d-activity{list-style:none;margin:0;padding:0}
.deployment .d-activity li{display:grid;grid-template-columns:62px 1fr;gap:12px;padding:9px 0;border-bottom:1px solid #edf1f3;font-size:12px}

.deployment .d-activity time{color:var(--quiet);font-variant-numeric:tabular-nums}
.deployment .d-empty{padding:28px 20px;color:var(--quiet);text-align:center}

.deployment pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f5f7f9;border-radius:8px;padding:14px;max-height:420px;overflow:auto}

.deployment .d-run{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:16px;padding:20px;border-bottom:1px solid #edf1f3;text-decoration:none;color:inherit}

.deployment .d-run:hover{background:#f6fafb}
.deployment .d-run strong{font-size:15px;display:block}
.deployment .d-run:last-child{border:0}

.deployment .d-list-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-bottom:22px}
.deployment .d-list-stats>div{padding:18px 20px;background:white;border:1px solid var(--line);border-radius:12px}

.deployment .d-list-stats strong{font-size:26px;display:block;font-weight:600}
.deployment .d-list-stats span{font-size:12px;color:var(--quiet)}

.deployment .d-back{font-size:12px;text-decoration:none;display:inline-block;margin-bottom:12px}
.deployment a{overflow-wrap:anywhere}

.deployment .d-announcement{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}

@media(max-width:1050px){.deployment .d-columns{grid-template-columns:1fr}
.deployment .d-columns>aside{display:contents}.deployment .d-columns>aside>.d-panel:first-child{grid-row:1}
.deployment .d-top{flex-direction:column}
}

@media(max-width:520px){.deployment h1{font-size:27px}
.deployment .d-hero{padding:18px}
.deployment .d-hero-head{gap:12px}
.deployment .d-ring{width:70px;height:70px}
.deployment .d-hero h2{font-size:19px}
.deployment .d-stats{grid-template-columns:1fr;gap:12px}
.deployment .d-stat{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.deployment .d-stat strong{font-size:16px;text-align:right}
.deployment .d-list-stats{gap:8px}
.deployment .d-list-stats>div{padding:12px}
.deployment .d-step{grid-template-columns:25px minmax(0,1fr)}
.deployment .d-timing{grid-column:2}
.deployment .d-run{grid-template-columns:1fr}
}

@media print{.side,.d-tools,.d-back,#deployment-connection{display:none!important}
.shell,.main,.content{display:block!important;margin:0!important;padding:0!important}
.deployment .d-columns{display:block}
}

"""

SCRIPT = r"""
(() => {
  const root = document.getElementById('deployment-content');
  const connection = document.getElementById('deployment-connection');
  const announcement = document.getElementById('deployment-announcement');
  if (!root) return;
  // finished: the deployment ended, so nothing polls by itself; Refresh now still asks once.
  let busy = false, timer, errors = 0, stopped = false, finished = root.dataset.terminal === 'true';
  let previous = root.dataset.state;
  function schedule() {
    clearTimeout(timer);
    if (!stopped && !finished && !document.hidden) timer = setTimeout(update, Math.min(60000, 10000 * 2 ** errors));
  }
  async function update() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    connection.textContent = 'Checking for updates…';
    const timeout = new AbortController();
    const deadline = setTimeout(() => timeout.abort(), 20000);
    try {
      const response = await fetch(root.dataset.endpoint, {credentials:'same-origin',cache:'no-store',signal:timeout.signal,headers:{Accept:'application/json'}});
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        stopped = true;
        connection.textContent = response.status === 401 ? 'Sign-in ended. Reload this page to sign in.' : 'This deployment is no longer available to your account.';
        announcement.textContent = connection.textContent;
        return;
      }
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      const expanded = new Map(Array.from(root.querySelectorAll('details[id]'), el => [el.id, el.open]));
      const active = root.contains(document.activeElement) ? document.activeElement : null;
      const focused = active ? {id:active.id,href:active.getAttribute('href')} : null;
      root.innerHTML = data.html;
      for (const [id, open] of expanded) {
        const element = document.getElementById(id);
        if (element) element.open = open;
      }
      if (focused) {
        const target = focused.id ? document.getElementById(focused.id) :
          Array.from(root.querySelectorAll('a[href]')).find(el => el.getAttribute('href') === focused.href);
        target?.focus({preventScroll:true});
      }
      if (data.state !== previous) announcement.textContent = data.label;
      previous = data.state;
      errors = 0;
      finished = data.terminal === true;
      connection.textContent = data.error ? 'Provider connection interrupted · showing last known progress' :
        (finished ? 'Finished · automatic updates off · checked ' : 'Automatic updates on · page checked ') + new Date().toLocaleTimeString();
    } catch (_) {
      errors = Math.min(errors + 1, 3);
      connection.textContent = 'Connection interrupted · progress may be out of date · retrying automatically';
      announcement.textContent = 'Unable to refresh deployment status. Last known progress remains visible.';
    } finally {
      clearTimeout(deadline); busy = false; schedule();
    }
  }
  document.getElementById('deployment-refresh')?.addEventListener('click', update);
  document.addEventListener('visibilitychange', () => {clearTimeout(timer); if (!document.hidden && !finished) update();});
  window.addEventListener('pagehide', () => {stopped = true; clearTimeout(timer);});
  window.addEventListener('pageshow', event => {if(event.persisted){stopped=false; if(!finished) update();}});
  schedule();
})();
"""


def moment(value, short=False):
    if not value or value <= 0:
        return "Not reported"
    return datetime.datetime.fromtimestamp(value, datetime.UTC).strftime("%H:%M:%S" if short else "%b %d, %H:%M:%S UTC")


def duration(value):
    if value is None or value < 0:
        return "Not reported"
    seconds = int(value)
    return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60:02d}s"


def step_name(name):
    return {"Validate artifact and deployment configuration": "Check the deployment destination",
            "Push image and resolve immutable digest": "Upload the app to Google Cloud",
            "Deploy, probe, promote; restore traffic on failure": "Start the app, check health, and switch traffic",
            "Audit Python dependencies": "Check Python packages for security issues",
            "Audit Node.js dependencies": "Check Node.js packages for security issues",
            "Build without cloud credentials": "Build the app image",
            "Run unit tests": "Run automated tests", "Run unit tests (Node.js)": "Run automated tests"}.get(name, name)


def link(href, label, primary=False):
    return f'<a class="btn {"primary" if primary else "ghost"}" href="{ui.esc(href)}">{ui.esc(label)}</a>'


def tone(state):
    if state in {"succeeded", "done", "passed", "passed_in_earlier_release", "rolled_back"}:
        return "done"
    if state in {"failed", "blocked", "timed_out", "dispatch_lost"}:
        return "failed"
    return "running" if state in {"running", "dispatched", "dispatching", "in_progress"} else "skipped"


def badge(state, label=None):
    plain = {"passed_in_earlier_release": "Passed previously", "skipped_by_config": "Not required", "unconfirmed": "Not verified",
             "release_failed_health_unconfirmed": "Not verified", "not_run": "Did not run", "reported": "Results reported", "pending": "Waiting"}
    return f'<span class="d-badge {tone(state)}">{ui.esc(label or plain.get(state) or LABELS.get(state, state.replace("_", " ").capitalize()))}</span>'


def panel(title, body, sub=""):
    return f'<section class="d-panel"><div class="d-panel-head"><h2>{ui.esc(title)}</h2>{f"<p>{ui.esc(sub)}</p>" if sub else ""}</div>{body}</section>'


def facts(items):
    return '<dl class="d-facts">' + "".join(f"<div><dt>{ui.esc(label)}</dt><dd>{ui.esc(value if value is not None else 'Not reported')}</dd></div>" for label, value in items) + "</dl>"


def ring(done, total):
    fill = round(251.3 * done / total, 2) if total else 0
    return (
        f'<svg class="d-ring" viewBox="0 0 100 100" role="img" aria-label="{done} of {total} reported steps finished">'
        '<circle cx="50" cy="50" r="40" fill="none" stroke="#e1ecec" stroke-width="7"/>'
        f'<circle cx="50" cy="50" r="40" fill="none" stroke="#087f78" stroke-width="7" stroke-dasharray="{fill} 251.3" '
        'transform="rotate(-90 50 50)" stroke-linecap="round"/>'
        f'<text x="50" y="57" text-anchor="middle">{f"{done}/{total}" if total else "—"}</text></svg>'
    )


def workflow_groups(data):
    groups = []
    for index, job in enumerate(data["groups"]):
        steps = []
        for step in job["steps"]:
            state = step["state"]
            mark = {"done": "✓", "running": "•", "failed": "!", "skipped": "−", "cancelled": "×"}.get(state, "○")
            elapsed = step.get("seconds")
            if step.get("started_at"):
                elapsed = (step.get("finished_at") or data["now"]) - step["started_at"]
            state_label = {"done": "Passed", "pending": "Not started", "running": "In progress", "unknown": "Not confirmed"}.get(state, state.capitalize())
            steps.append(
                f'<li class="d-step"><span class="d-symbol {state}" aria-hidden="true">{mark}</span>'
                f'<div class="d-step-name" title="{ui.esc(step["name"])}">{ui.esc(step_name(step["name"]))}<div class="d-caption">{state_label}</div></div>'
                f'<span class="d-timing">{duration(elapsed) if elapsed is not None else ""}</span></li>'
            )
        opened = any(s["state"] in {"running", "failed"} for s in job["steps"]) or (len(data["groups"]) == 1 and not data.get("terminal"))
        job_link = '<div class="d-pad">' + link(job["url"], "Open this job’s logs ↗") + "</div>" if job.get("url") else ""
        job_name = {"build": "Build and test", "deploy": "Deploy and verify", "promote": "Release to production", "notify": "Record and share the result"}.get(job["name"], job["name"])
        groups.append(
            f'<details class="d-job" id="job-{index}"{" open" if opened else ""}><summary id="job-summary-{index}">'
            f'{badge(job["state"])}<span class="d-job-name">{ui.esc(job_name)}</span></summary><ol class="d-steps">{"".join(steps)}</ol>{job_link}</details>'
        )
    return panel("Deployment steps", "".join(groups) or '<div class="d-empty">Waiting for GitHub to report the workflow steps.</div>', "Actual recorded steps. Some jobs may run at the same time.")


def detail(data):
    state, app = data["state"], data["application"]
    setup = data["kind"] == "setup"
    steps = [s for job in data["groups"] for s in job["steps"]]
    done = sum(s["state"] in {"done", "skipped"} for s in steps)
    total = len(steps)
    elapsed = (data.get("finished_at") or data["now"]) - data["started_at"] if data.get("started_at") else None
    label = data["label"]
    if state == "succeeded" and data.get("kind") in {"rollback", "production_rollback"}:
        label = "Recovery workflow finished"
    elif state == "succeeded" and not data.get("phases"):
        label = "Workflow passed · confirming release"
    explanation = step_name(data.get("current")) or {
        "planned": "The deployment has been prepared. It has not started yet.",
        "succeeded": "Review the checks and current service status below.",
        "completed_unverified": "The workflow ended, but some required checks have not been confirmed.",
        "failed": "Open the failed step to see what happened and what to do next.",
        "awaiting_approval": "A reviewer must approve the request in GitHub before it can continue.",
        "complete": "Setup has finished. Waiting for the first deployment record.",
    }.get(state, "This page updates automatically. You can leave and come back to this deployment.")
    tools = link("/settings?tab=deployments", "All deployments")
    if data.get("run_url"):
        tools += link(data["run_url"], "GitHub logs ↗")
    if data.get("id"):
        tools += link("/settings/deployment/report?" + urlencode({"plan": data["id"]}), "Download report")
    head = (f'<div class="d-top"><div><div class="d-eyebrow">Deployment workspace</div><h1>{ui.esc(app)}</h1>'
            f'<p>{ui.esc(data.get("environment"))} · {ui.esc(data.get("repository"))}</p></div><div class="d-tools">{tools}</div></div>')
    remaining = "Finished" if data.get("terminal") else "Waiting for approval" if state == "awaiting_approval" else "Not estimated"
    if state in {"blocked", "failed", "timed_out", "cancelled", "dispatch_lost"}:
        remaining = "Stopped"
    hero = (
        f'<section class="d-hero"><div class="d-hero-head">{ring(done, total)}<div>{badge(state)}<h2>{ui.esc(label)}</h2><p>{ui.esc(explanation)}</p></div></div>'
        '<div class="d-stats">'
        + "".join(
            f'<div class="d-stat"><span class="d-caption">{k}</span><strong>{ui.esc(v)}</strong></div>'
            for k, v in (("Elapsed time", duration(elapsed)), ("Steps remaining", str(total - done) if total else "Waiting for workflow"), ("Estimated time left", remaining))
        )
        + '</div><p class="d-caption">Progress counts reported steps, including skipped work. Time remaining is not estimated yet.</p></section>'
    )
    notices = ""
    if data.get("error"):
        notices += f'<div class="d-notice d-error" role="status">{ui.esc(data["error"])}</div>'
    if state in {"blocked", "failed", "timed_out", "cancelled", "awaiting_approval", "completed_unverified", "dispatch_unknown", "dispatch_lost"}:
        notices += f'<div class="d-notice"><strong>What needs your attention</strong><p>{ui.esc(data.get("next_action"))}</p>'
        for pending in data.get("pending") or []:
            notices += f"<p>{ui.esc(pending.get('environment'))} · Reviewers: {ui.esc(', '.join(pending.get('reviewers') or []) or 'See GitHub')}</p>"
        notices += "</div>"
    for item in data.get("links") or []:
        notices += link(item["url"], item["name"] + " ↗")
    check_names = {"dependency_scan": "Dependency safety", "health": "Deployment health check", "target": "Destination check"}
    checks = "".join(
        f'<div class="d-check"><span>{ui.esc(check_names.get(k, k.replace("_", " ").capitalize()))}</span>'
        f"{badge(v)}</div>"
        for k, v in data.get("checks", {}).items()
    )
    checks = panel("Checks & safeguards", '<div class="d-pad d-checks">' + (checks or "<p>Checks will appear when the workflow reports them.</p>") + "</div>")
    health = data.get("health") or {}
    if health:
        health_text = "Cloud Run service ready" if health.get("ready") else "Cloud Run service not ready"
        health_body = badge("done" if health.get("ready") else "failed", health_text)
        health_body += f'<p class="d-caption">Checked {moment(health.get("at"))}. Service readiness is separate from the health checks for this deployment.</p>'
        revision = (data.get("phases") or {}).get("revision")
        traffic_rows = health.get("traffic") or []
        if any(t.get("plan_id") for t in traffic_rows) or revision:
            serving = sum(float(t.get("percent") or 0) for t in traffic_rows
                          if t.get("plan_id") == data.get("id") or (revision and t.get("revision") == revision))
            health_body += f'<p><strong>This deployment receives {serving:g}% of traffic.</strong></p>'
        else:
            health_body += '<p class="d-caption">The serving version has not yet been matched to this deployment.</p>'
        for traffic in health.get("traffic") or []:
            share = max(0, min(100, float(traffic.get("percent") or 0)))
            health_body += (f'<div class="d-caption">{ui.esc(traffic.get("revision"))} · {share:g}% of traffic</div>'
                            f'<div class="d-traffic" role="img" aria-label="{share:g} percent of traffic"><span style="width:{share}%"></span></div>')
        app_url = health.get("url") or data.get("app_url")
        if app_url:
            health_body += link(app_url, "Open app ↗", True)
    else:
        health_body = (
            "<p>Live service status is not available yet.</p>"
            if setup or data.get("observer")
            else "<p>Live health monitoring is not connected. Configure the observer account to read Google Cloud status.</p>"
        )
    if data.get("health_error"):
        health_body += f'<div class="d-notice">{ui.esc(data["health_error"])} Last recorded status may be out of date.</div>'
    observation = ((data.get("phases") or {}).get("bake") or {}).get("result")
    observation_notes = {
        "inconclusive": "The startup checks passed, but there was too little traffic to judge real-world errors and response time.",
        "unobserved": "The deployment finished, but post-deployment monitoring could not read the app logs.",
        "skipped": "Post-deployment monitoring was turned off for this release. Review current health before relying on this result.",
    }
    if observation in observation_notes:
        health_body += f'<div class="d-notice"><strong>Monitoring note</strong><p>{observation_notes[observation]}</p></div>'
    if not setup:
        health_body += (
            '<div class="d-tools">'
            + link("/settings?" + urlencode({"tab": "health", "app": app, "env": "production" if data["environment"] == "Production" else "staging"}), "Health charts & recent errors")
            + "</div>"
        )
    health_panel = panel("Is the app working?", f'<div class="d-pad">{health_body}</div>')
    context = panel(
        "Deployment details",
        '<div class="d-pad">'
        + facts(
            [
                ("Application", app),
                ("Repository", data.get("repository")),
                ("Version", data.get("sha")),
                ("Change", data.get("message")),
                ("Google Cloud project", data.get("project")),
                ("Region", data.get("region")),
                ("Environment", data.get("environment")),
                ("Requested by", data.get("owner")),
                ("Started", moment(data.get("started_at"))),
                ("Finished", moment(data.get("finished_at"))),
                ("Deployment ID", data.get("id")),
                ("Workflow last checked", moment(data.get("observed_at"))),
            ]
        )
        + "</div>",
    )
    events = "".join(f"<li><time>{moment(e['at'], True)}</time><span>{ui.esc(e['text'])}</span></li>" for e in data.get("events") or [])
    activity = panel(
        "Activity",
        '<div class="d-pad"><ol class="d-activity">'
        + (events or "<li>No deployment events recorded yet.</li>")
        + '</ol><p class="d-caption">Times shown in UTC. Workflow details are available in the step panels and GitHub logs.</p></div>',
    )
    phases = data.get("phases") or {}
    reports = '<div class="d-pad">' + facts([
        ("Deployment result", label), ("Health check", data.get("checks", {}).get("health", "Not reported")),
        ("Deployed version", phases.get("revision")),
        ("Post-deployment observation",
         {"inconclusive": "Not enough traffic to judge", "unobserved": "Monitoring unavailable", "skipped": "Turned off"}.get(observation, observation or "Not reported")),
    ]) + '</div>'
    diagnosis = phases.get("diagnosis") or {}
    if diagnosis:
        reports += f'<div class="d-pad"><h3>What went wrong</h3><p>{ui.esc(diagnosis.get("cause"))}</p><p>{ui.esc(diagnosis.get("developer"))}</p></div>'
    for key, title in (("phases", "Deployment evidence & captured logs"), ("recovery", "Recovery status"), ("report", "Next-day comparison")):
        if data.get(key):
            reports += (
                f'<details class="d-job" id="report-{key}"><summary id="report-summary-{key}">{title}</summary><div class="d-pad"><pre>{ui.esc(json.dumps(data[key], indent=2))}</pre></div></details>'
            )
    if not data.get("report"):
        reports += '<div class="d-pad"><p>Next-day comparison: available after a successful deployment has been observed for a day, when monitoring is connected.</p></div>'
    reports = panel("Reports & evidence", reports, "Recorded evidence, kept with this deployment.")
    prior_setup = ""
    if data.get("setup"):
        prior_setup = '<details class="d-job" id="prior-setup"><summary id="prior-setup-summary">First-time setup completed</summary>' + workflow_groups(data["setup"]) + "</details>"
    return head + notices + hero + f'<div class="d-columns"><div>{workflow_groups(data)}{prior_setup}{checks}{activity}{reports}</div><aside>{health_panel}{context}</aside></div>'


def listing(data):
    rows = data["rows"]
    active, needs = data["counts"]["active"], data["counts"]["attention"]
    head = (
        '<div class="d-top"><div><div class="d-eyebrow">Your delivery workspace</div><h1>Deployments</h1><p>Follow every app from first setup to a running release.</p></div><div class="d-tools">'
        + link("/settings?tab=connections", "Set up an app", True)
        + link("/settings?tab=releases", "History & trends")
        + "</div></div>"
    )
    stats = (
        '<div class="d-list-stats">'
        + "".join(f"<div><span>{name}</span><strong>{value}</strong></div>" for name, value in (("In progress / prepared", active), ("Needs attention", needs), ("Recent deployments", len(rows))))
        + "</div>"
    )
    items = []
    for setup in reversed(data["setups"]):
        href = "/settings?" + urlencode({"tab": "deployments", "onboarding": setup["application"]})
        items.append(
            f'<a class="d-run" href="{ui.esc(href)}"><div><strong>{ui.esc(setup["application"])}</strong>'
            f'<span class="d-caption">First-time setup · {ui.esc(setup["current"])}</span></div>{badge(setup["state"], setup["label"])}</a>'
        )
    for row in rows:
        items.append(
            f'<a class="d-run" href="{ui.esc(row["href"])}"><div><strong>{ui.esc(row["application"])}</strong>'
            f'<span class="d-caption">{ui.esc(row.get("summary") or row.get("kind"))} · {ui.esc(row["sha"][:8])}</span></div>{badge(row["state"], row["label"])}</a>'
        )
    return (
        head
        + stats
        + panel(
            "Recent activity",
            "".join(items) or '<div class="d-empty"><h2>Your first deployment will appear here</h2><p>Connect your accounts and check an app to get started.</p></div>',
            "Open a deployment to follow its live steps, checks, and report. Up to 100 recent deployments.",
        )
    )


def page(frame, data, query):
    endpoint = "/settings/deployment/status" + ("?" + urlencode(query) if query else "")
    body = detail(data) if query else listing(data)
    terminal = bool(query and data.get("terminal"))
    inner = (
        f'<style>{STYLE}</style><div class="deployment"><div class="d-tools" style="justify-content:space-between;margin-bottom:20px">'
        '<span class="d-live"><span class="d-dot" aria-hidden="true"></span><span id="deployment-connection">'
        + ("Finished · automatic updates off" if terminal else "Automatic updates every 10 seconds") + "</span></span>"
        '<button class="btn ghost" id="deployment-refresh" type="button">Refresh now</button></div>'
        '<div id="deployment-announcement" class="d-announcement" role="status" aria-live="polite"></div>'
        f'<div id="deployment-content" data-state="{ui.esc(data.get("state", "list"))}" data-terminal="{str(terminal).lower()}" '
        f'data-endpoint="{ui.esc(endpoint)}">{body}</div>'
        "<noscript><p>Automatic updates need JavaScript. Reload this page to see the latest status.</p></noscript></div>"
        '<script src="/settings/deployment/script.js" defer></script>'
    )
    return ui.shell(
        frame, "deployments", inner, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'"}, title="Deployments · Fast Deploy"
    )
