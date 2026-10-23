# Deployment visibility

## Implemented workspace

The app now includes **Deployments** in the sidebar at `/settings?tab=deployments`. Opening a run shows a visual completion ring, expandable job/step timeline, completed and remaining work, elapsed time, check results, cloud readiness and traffic allocation, deployment context, recorded activity, provider log links, and downloadable JSON reports. App setup redirects here and follows its exact first release, rather than whichever release is newest.

`deployments.py` makes authenticated read-only provider observations; `deployment_page.py` renders the workspace and its same-origin update script. Browser updates run every 10 seconds while visible, share a provider read budget across viewers, back off after connection failures, resume on return, and preserve expanded details. Cloud service reads have a separate 30-second cadence. Setup records still advance on the existing roughly one-minute background cycle. Last-check timestamps distinguish those sources.

Observations and step/state transitions are stored separately in SQLite. A stale snapshot never overrides the controller's terminal result. Provider outages retain the last evidence with an explicit warning; missing checks never become a verified success. Viewing a run does not dispatch, approve, retry, or change the cloud. All data/report endpoints check the current signed-in identity's application scope. Other settings pages retain their script-free policy.

Since 1.31.9 a release reads as eleven plain-language stages (`journey.py`), each opening to its exact GitHub steps. The combined deploy/verify step is split live by what Cloud Run reports (the new revision present with no share, then its share, with the service's own change time marking the switch), and the receipt's phase times replace those after the run; nothing is lit by a timer. A "Who gets the visitors" bar shows the previous version's share and this release's, and **Full screen** hides the sidebar for a projector. "Usually takes" is the median of the application's last successful releases (shown from three).

Since 1.31.10 the live part is drawn for a room: a board with a sliding progress ring and a track of every stage grouped by what the group does, a spotlight card for the stage happening now (what it does, why it is there, what happens if it fails, with a moving-visitors picture during the start and the switch and a beating line during the watch), a visitors picture with traffic moving to the version that serves it, and a journey whose every stage carries the same three plain lines. A stage that changes between two refreshes gets a one-time effect; "reduce motion" turns every effect off.

Remaining limitations: time remaining is explicitly **Not estimated**; the screen shows remaining stages and the usual length instead. A database migration or a jobs update inside the release step shows as part of "Start the new version beside the old one". Full build logs open in GitHub; captured failure logs and follow-up reports appear when available. Cloud readiness is shown separately from probe results and traffic ownership. The preview on port 8769 uses clearly labeled sample providers, not a real deployment.

Validation includes provider failures/recovery, missing evidence, authorization, session revocation, exact setup-to-release handoff, persisted observations, terminal-state precedence, script injection/credential filtering, health freshness, approvals, cancellations, timeouts, and browser automatic-update checks.

Real-data verification on October 22: the existing GitHub workflow was read with the saved local CLI login and confirmed completed/successful. Google Cloud was read using the configured observer identity and reported the service ready. A separate session-protected, loopback-only read-only viewer on port 8770 displays original deployment records plus live cloud reads; the original database is not migrated or modified by that viewer. The latest visible registered app's release has 61 recorded workflow steps and receives 100% of traffic. Its receipt's post-deployment observation was inconclusive due to insufficient traffic; the UI states that limitation explicitly. No new deployment was started.

## Original audit and research

Reviewed October 22, 2026. The following records the findings before the workspace implementation. It is not verification of a real cloud deployment.

## Answer

The project already has substantial deployment evidence and monitoring. It does not yet present the requested unified, automatically updating deployment journey in the new account/setup UI. Extend the existing controller and monitor rather than add a second deployment system.

## What exists

| Capability | Evidence | Practical limitation |
| --- | --- | --- |
| Setup progress and blockers | `onboarding.py:describe`, `Onboarder.advance`; stored steps and transition events | Setup completion hands off to a release plan; it does not mean the app is live. |
| Workflow progress | `github.py:GitHub.status`, `status.py:timeline`, `summarize` | GitHub job/step status and completed-step durations are available, but not presented as a dedicated browser deployment detail screen. |
| Success and failure evidence | `controller.py:refresh`, `status.py:summarize` | Required checks are assessed separately from overall workflow success. `completed_unverified` must remain visibly uncertain. |
| Detailed deployment phases | `status.py:release_phases`, release receipts | The controller fetches the receipt when recording an outcome. Internal deploy/probe/traffic phases should not be portrayed as continuously live without another evidence source. |
| Background tracking | `scheduler.py` onboarding and follow jobs | Both normally run every 60 seconds. Refreshing the browser faster alone would not make this evidence newer. |
| Release history and charts | `monitor_page.py:releases`, `release_table`, `timeline_figure` | Shows recorded outcomes, durations, commits, actors, GitHub links, and aggregate delivery measures. Manual Refresh link. |
| App health and logs | `monitor.py`, `monitor_page.py:health`, `logs_card` | Needs the configured observer identity and permissions. Metrics cached for 300 seconds; logs separately cached for 60 seconds. Not a streaming build-log viewer. |
| Follow-up reports | `controller.py:release_report_tick`, `digest_tick`; monitor Cost and Activity views | Release comparison eligible after one day, scheduler checks every 15 minutes; also daily summaries, weekly cost reports, and audit history. Configuration and evidence availability apply. |
| ETA | No estimator found in inspected status/monitor path | Existing durations are useful inputs, not a reliable remaining-time forecast. |
| Automatic browser updates | Settings pages explicitly use script-free HTML and manual Refresh links | Requires a deliberate frontend/endpoint addition; preserve authentication and restrictive security policy. |

The actual monitor navigation is under `/settings?tab=overview`, `health`, `releases`, `cost`, and `activity`. The sample preview does not implement these real monitor destinations.

## Recommended experience

Starting setup or a release opens one persistent deployment page. Returning later opens the same run, even after a browser reload or controller restart. A visible active-deployment link remains available from the app overview.

At the top: app, repository/commit, Google Cloud project, environment, overall state, current action, elapsed time, last successful observation, and steps remaining. Show an estimated remaining range only when supported. The next action appears prominently only when the user must do something.

Use simple stages, adapting to the actual path:

1. Check the app.
2. Prepare Google Cloud (first setup only).
3. Build and test.
4. Start the new version.
5. Check health and switch traffic.
6. Confirm the result.

Some work can run in parallel. Group it for readability without inventing a strictly sequential execution order. Already prepared or reused work should say "Already ready" or "Reused", not imply it ran again. Approval waits belong beside the stage they block. "Deployment finished" is a summary, not an artificial extra unit of work used to inflate progress.

Keep operational states explicit: queued, running, needs approval, failed, cancelled, timed out, checking result, succeeded, restoring previous version, restored, and status unavailable. A failed attempt and a healthy previous version can both be true. Show deployment outcome and current service health separately, with observation timestamps.

Expandable details provide real step names, per-step durations, sanitized evidence, relevant logs, GitHub run links, and Cloud Run links. The default view uses ordinary English and hides technical detail. An immediate report records the result and available evidence; the existing next-day comparison remains a separate follow-up report.

## Honest progress and time remaining

- Initially show completed/current/remaining stages and elapsed time. Do not treat a fraction of completed stages as a fraction of elapsed work.
- Preserve timestamps and attempts per step. Account for skipped/reused steps, first setup versus repeat release, environment, cache behavior, parallel jobs, and configured observation windows.
- After collecting sufficient comparable runs, use robust historical duration ranges for the remaining critical path, with a visible sample count and "Estimated" label. A specific minimum sample count needs calibration from actual run variability.
- Queue and approval waits have no guaranteed duration. Present them separately; suppress or qualify ETA. First runs should say "Estimate unavailable".
- If a stage exceeds its usual range, say "Taking longer than usual" and retain the real status. Never count down to zero while still running.
- Distinguish "No new events" from "Unable to check status". Show last successful observation and reconnect state. A connection problem is not deployment failure.

## Implementation order

### 1. One useful deployment page

Create an authenticated read-only status endpoint and detail page scoped to the signed-in user's applications. Link setup ID/application to the exact release plan ID; never silently follow a different latest run. Reuse controller checks, events, advice, and provider links. Persist active workflow snapshots and terminal evidence so reopening the page remains useful. Handle an application not yet registered during setup without exposing another user's pending setup.

Add an immediate human-readable result report: who started it, source version, destination, timestamps, outcome, required checks, warnings, previous-version/recovery status, and app URL where available. Do not label an authentication-protected app "public".

### 2. Updates that reflect new evidence

Choose same-origin polling first (for example every 5–10 seconds while visible), backed by a shared, rate-limited provider refresh cadence. That browser interval is a proposed UI target, not the existing backend's freshness guarantee. Slow down when hidden or after repeated failures; preserve manual refresh and stop aggressive polling on terminal states. SSE can be a later improvement if justified.

Serve a small same-origin script under a narrowly updated CSP, not unrestricted inline script. GET reads must not dispatch, retry, approve, or deploy. Retain per-user authorization on snapshots, logs, exports, and reconnects. Cap and redact logs; never expose tokens or unrelated app data.

For granular live Cloud Run rollout substeps, add durable phase events from the existing deployment process or a verified checkpoint reader. Until available, use a truthful broader "Deploying and checking health" state. Do not manufacture substeps from a timer.

### 3. History, estimates, and notifications

Store comparable timing samples and introduce calibrated ETA ranges. Connect current run, previous attempts, immediate report, existing health graphs, and next-day report. Add in-app notices for completion, failure, or required approval. External notification destinations require explicit configuration/authorization. Retrying or rolling back remains a separately authorized action with duplicate-dispatch protection.

## Acceptance checks

- Test first setup, repeat release, reused image, skipped checks, approval waits, parallel jobs, failure before deploy, cancellation, timeout, missing workflow, and recovery outcomes.
- Verify a completed workflow with missing evidence stays "Checking result"/unverified; setup completion never becomes "App live" by itself.
- Verify refresh/reconnect/reopen and controller restart preserve identity and progress without starting another deployment.
- Verify provider failures and stale data remain distinguishable from deployment failures; polling does not overload providers.
- Verify role/application boundaries, safe rendering of untrusted log text, and secret redaction.
- Test keyboard access, narrow screens, reduced motion, meaningful text alongside colors, and polite screen-reader status announcements without moving focus.
- Measure ETA error and coverage using held-out historical runs before presenting estimates as useful.

## Research and design rationale

These are sources for design patterns, not a recommendation to migrate this project to Cloud Deploy or Argo CD.

- [GitHub Actions visualization graph](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-the-visualization-graph): real-time job status, dependencies, and direct access to job logs. Supports an overview plus optional drill-down.
- [GitHub Actions workflow logs](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs): failed-step evidence, step durations, searchable/downloadable logs, and exact log links. Supports actionable failure reporting.
- [Google Cloud Deploy release details](https://docs.cloud.google.com/deploy/docs/view-release): rollout metadata/status, phases/jobs, logs, and artifacts. Supports grouping the evidence for one deployment together.
- [Google Cloud Deploy platform logs](https://docs.cloud.google.com/deploy/docs/platform-logs): distinguishes rollout progress, success/failure, cancellation, approval, and phase advancement. Supports explicit waiting and recovery states rather than one spinner.
- [Argo CD resource health](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/): evaluates resource health from underlying resource status. Supports retaining health as separate evidence from a completed delivery operation.
- [Nielsen Norman Group: long waits and interruptions](https://www.nngroup.com/articles/designing-for-waits-and-interruptions/): show elapsed and remaining work, use remaining steps when reliable timing is unavailable, support background work, and provide contextual completion details and history.
- [W3C: status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html): expose progress and results to assistive technology without moving focus. Supports polite status announcements instead of disruptive page reloads.

This audit inspected source paths and primary documentation. It did not validate provider credentials, a running production controller, actual Google Cloud metrics, or historical ETA accuracy. The accompanying interactive illustration is proposed UI with sample states only.
