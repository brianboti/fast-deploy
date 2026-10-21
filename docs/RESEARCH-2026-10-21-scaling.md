# Scaling fast and cheaply, and a plan for every issue: Fast Deploy 1.27.0 against research and practice

Written 2026-10-21 UTC as a read-only review; nothing in the code, the configuration or the cloud was changed. The
operator asked three things: which optimisation algorithms exist for raising and lowering a service's resources in the
shortest time, and whether any is worth applying here; how many seconds Cloud Run takes to go from zero instances to
one; and whether the applications on Google Cloud are monitored with a plan for every issue that can happen.

Method: the platform's scaling settings were read in the code (`labdeploy/release.py` for the deploy flags and the
start-up probe, `labdeploy/models.py` for the contract's bounds, `labdeploy/cost.py` for the right-sizing rules and
prices, `infra/gcp/modules/service/main.tf` for the service's first revision), then the lab's own services were measured
through the Monitoring API, read-only: every container start of the last 30 days with its duration, the requests each
service received, how long an instance existed, and what it billed. Three themed searches ran in parallel (how Cloud
Run scales and what shortens a cold start; the autoscaling and right-sizing algorithms of the industry and the research
literature; the failure catalogue of a Cloud Run service and its dependencies), each returning only what it had fetched;
section 8 marks each source F (read on the official page), S (a secondary source) or U (unverified). Prices are quoted
from Google's pricing page as read on 2026-10-20 and match the platform's own table (`labdeploy/cost.py`, verified
2026-10-11).

Acronyms: p50 and p95 (the duration that half, or 95 percent, of the measured starts were faster than), vCPU (one
virtual processor), HPA (Kubernetes' horizontal pod autoscaler), KPA (Knative's pod autoscaler, the open-source relative
of Cloud Run's), SLO (service level objective), IAP (Identity-Aware Proxy).

## 1. The short answer

**Cloud Run already runs the scaling algorithm, and for this lab's traffic it is the right one; what is worth doing
is three settings and one app fix, not an algorithm.** Google's autoscaler decides the instance count every second from
concurrent requests and CPU utilisation against a 60 percent target, starts an instance from zero the moment a request
arrives, and keeps an idle instance for up to 15 minutes before shutting it down (section 4). None of the industry's
cleverer methods (predictive scaling on history, control loops, learning agents) exists for Cloud Run, and all of them
need a traffic pattern to learn from; the lab's services see a few requests a day from people and a request every minute
from the platform's own uptime check (section 3). The weekly right-sizing report the platform already runs is the same
method Kubernetes' resource recommender and Amazon's power-tuning tool use: a percentile of measured use plus a margin
(section 5).

**Zero to one takes about five seconds today, and four of those are the platform's own doing.** Measured over 30 days
on 19 services: the typical container start is 5.0 to 5.2 seconds for nearly every application, including Node.js
services that listen in well under a second, while the two services that use a plain port check instead of an HTTP
health check start in 0.3 to 0.4 seconds (section 3). The cause is the start-up probe the release sets: it checks the
health path first at second zero, when nothing listens yet, and then waits its whole five-second period before trying
again (section 4). Shortening that period to one second takes about four seconds off every cold start of every
application at no cost. Startup CPU boost, which the platform never sets, takes a further 30 to 50 percent off the
application's own start for a fraction of a cent per start. After those two, what remains is the application's own
work at start: 15 seconds for the debate backend, 20 for Sophia's embedding, both already known to their developers.

**Cold starts are rare for people today, because the uptime check keeps the instances warm.** In staging every
service receives about 1,730 requests a day and nearly all are the uptime check (six regions every five minutes);
production's `lab-demo` receives 4,320 a day (three regions every minute). An instance is therefore present almost all
the time, and a container starts only after a release or when Google recycles it: two starts a day on a quiet day for
`lab-demo`. Measured, that costs about five cents a month of CPU per staging service and about thirty cents for production's, inside
the free tier, far below a warm instance at $9.72 a month (section 4). So "how fast from zero to one" matters for the minute after
each release and for the odd recycled instance, not for a person's first click of the morning.

**Monitoring covers the symptoms well; a few causes go unseen.** Of the 46 ways a service like these can fail
(section 6), 25 are seen within a day by something automatic, 9 only in part (the symptom but not its cause, or only
weekly), 11 not until a person notices, and 1 needs no watching. The unseen ones are mostly quiet: requests refused at
the instance limit (a 429 is not counted as an error, and the "at maximum instances" alert waits for three instances
when the default maximum is two), slow starts, a quota running out, Google's own incidents in the region, a role granted
by hand in the console, a new vulnerability in an image that already serves. Eight of the eleven cost nothing to close
with an alert policy or a line in the digest; section 7 ranks them.

## 2. Where the platform stands: what it sets, and what it leaves to Google

| Setting | What the platform does today | Where |
|---|---|---|
| Instances | `scaling.minimum` 0 (the contract allows at most 1), `scaling.maximum` 2 by default (the contract allows up to 5; the policy refuses more than 3 in staging and production); the first revision Terraform creates runs at most 1 | `labdeploy/models.py`, `labdeploy/release.py` |
| Size | 1 vCPU and 512 MiB by default (up to 2 CPUs and 2048 MiB under the policy), 80 requests in parallel per instance, a 60-second request timeout (up to 3600 for a service that streams) | `labdeploy/models.py` |
| Billing | Request-based (`--cpu-throttling`): CPU only while a request is served; an idle instance costs nothing unless it is a minimum instance | `labdeploy/release.py` |
| Environment | First generation (gVisor) for the lab's own code, second generation (a microVM) for code nobody has reviewed (D35) | `labdeploy/release.py` |
| Start-up probe | HTTP on the health path (or a TCP connect when the contract has no health route): first attempt at second 0, then every 5 seconds, 3 seconds to answer, 12 failures allowed (a 60-second window); traffic reaches the instance once it passes | `labdeploy/release.py` |
| Startup CPU boost | Never set | (absent from the deploy flags) |
| Right-sizing | Weekly, from 14 days of measurements: memory to the 95th percentile of use plus 25 percent (never below 512 MiB), one CPU less when the 95th percentile of CPU use is under 40 percent, concurrency doubled when instances reach 80 percent of it with headroom, warm instances off under 1,000 requests a day, a note when instance-based billing would pay (instances busy more than 60 percent of the time); proposals only, a person merges | `labdeploy/cost.py` |
| Alerts on saturation | "At maximum instances" (3 or more active instances for five minutes) and "memory above 80 percent" | `infra/gcp/modules/service/main.tf` |

Nothing in the platform decides instance counts itself: Cloud Run's autoscaler does, within the bounds above.

## 3. What the lab's services actually do: 30 days measured

Read through the Monitoring API on 2026-10-21, read-only: every container start of the last 30 days with its
duration (`run.googleapis.com/container/startup_latencies`, a distribution Google samples every minute), the requests
each service answered, and how long an instance existed. The staging project's data begins on 2026-10-12, when the
state migration of 1.0.0 recreated the services; production's `lab-demo` begins on 2026-10-18.

| Service (staging unless noted) | Container starts | Typical start (p50) | Slow start (p95) | Mean | Requests in 30 days | Starts per 100 requests |
|---|---|---|---|---|---|---|
| `lab-demo` | 52 | 5.2 s | 19.4 s | 8.1 s | 15,601 | 0.3 |
| `lab-demo` (production) | 12 | 5.2 s | 14.6 s | 6.0 s | 13,869 | 0.1 |
| `lab-notebook` | 24 | 5.2 s | 15.1 s | 8.3 s | 13,729 | 0.2 |
| `sophia` (Node.js, loads its embedding model at start) | 20 | 20.1 s | 27.3 s | 20.3 s | 13,017 | 0.2 |
| `llm-debate-backend` (Python) | 8 | 15.4 s | 26.3 s | 15.8 s | 8,384 | 0.1 |
| `lab-bare` | 11 | 5.2 s | 16.1 s | 6.6 s | 9,480 | 0.1 |
| `lab-demo-two` | 9 | 5.1 s | 10.1 s | 5.2 s | 8,382 | 0.1 |
| `mono-admin`, `mono-api` | 10, 5 | 5.1 s | 5.4 s | 4.6, 4.2 s | 10,155, 10,160 | 0.1, 0.0 |
| `node-bare`, `node-docker`, `node-nolock`, `node-onnx`, `node-procfile` (Node.js trials) | 4 to 8 each | 5.1 to 5.2 s | 5.4 to 6.3 s | 4.0 to 5.3 s | about 3,120 each | 0.1 to 0.3 |
| `aiohttp-main`, `django-static`, `node-port` (Phase 20 trials, retired) | 3 each | 5.0 s | 5.4 s | 3.5 to 3.6 s | about 125 each | 2.4 |
| `gradio-hello`, `streamlit-example` (no health route: TCP probe) | 1 each | 0.3 s, 0.4 s | 0.3 s, 0.5 s | 0.3 s, 0.4 s | about 120 each | 0.8 |

Three things stand out.

**The five-second floor is the probe, not the applications.** Fourteen services of two languages and three
frameworks share a typical start of 5.0 to 5.2 seconds, Node.js services that listen in under a second among them;
the means (3.5 to 4.6 seconds for the small ones) say the real start is shorter and the probe reports it late. The two
services probed by a TCP connect instead of an HTTP call start in 0.3 to 0.4 seconds with otherwise identical
settings. Section 4 explains the mechanism; the fix is one number in `labdeploy/release.py`.

**Above the floor, the application's own start is what remains.** Sophia's 20 seconds is the embedding it computes at
start (measured at 94 seconds on a laptop in the 1.25.1 note, and under a second with precomputed vectors); the debate
backend's 15 seconds and `lab-demo`'s slow starts (its 95th percentile, 19 seconds, and a few starts near 53 seconds on
its test days) are their imports and workers. Those are the developers' to shorten, and the platform's diagnosis and
contract notes already say how.

**People almost never meet a cold start, because the uptime check keeps the instances warm.** Staging's 1,730
requests a day per service are the uptime check (six regions, every five minutes, 1.2 a minute); production's 4,320 are
its check (three regions, every minute). Cloud Run keeps an idle instance for up to 15 minutes after a request, so an
instance was present in about 95 percent of the five-minute windows since `lab-demo`'s data begins, and in every
window for `sophia` and production's `lab-demo`. A container starts only after a release (17 starts on the day of the
comprehensive test, 10 on the day of the Node.js trials) or when Google recycles an instance: 2 starts a day on a quiet
day. All of this bills almost nothing under request-based billing: `lab-demo` in staging used 1,955 billable
instance-seconds in 30 days, about five cents of CPU at list price before the free tier.

## 4. How Cloud Run scales, and what shortens zero to one

**The algorithm Google runs.** Cloud Run's autoscaler recomputes the instance count continuously from two drivers and
takes the higher: concurrent requests (the larger of the last minute's and the last ten minutes' average, divided by the
instance's concurrency limit and by a 60 percent target, rounded up) and CPU utilisation (the last minute's average
against the same 60 percent target). The count stays put while utilisation is within 10 percent of the target, grows
immediately above it, and falls only when utilisation drops more than 10 percent below it; surplus instances go in two
groups, one when its utilisation is under 10 percent for a minute, the other after a 15-minute idle timeout. Memory is
never a driver (F). Zero to one is different: "scaling from zero can only be triggered by a request"; the first request
waits in a queue for up to the greater of ten seconds or 3.5 times the service's average start-up time while an
instance starts, and at the instance limit it fails with 429 after that window (F). Since September 2026 the two
targets can be set per service (CPU 10 to 90 percent, concurrency 10 to 95 percent, generally available 2026-09-29)
(F); a service-level maximum (GA 2025-10-06) and manual scaling (GA 2025-08-05, `--scaling=N`, idle instances billed as
minimum instances) exist too (F). Google suggests "starting with a maximum instances value of 3 to protect your budget"
(F), which is the platform's policy bound.

**The 15-minute rule.** "An instance will never stay idle for more than 15 minutes after processing a request unless it
is kept active using minimum instances", and an idle instance "can be shut down at any time"; no page exposes the
timeout as a setting (F). Under request-based billing an idle instance that is not a minimum instance is not charged
(F), which is why the uptime check's keep-warm effect in section 3 is nearly free: Google documents neither that checks
keep instances warm nor any objection to it, and the community FAQ says periodic requests "can help prevent" scaling to
zero with no guarantee (S, U).

**What a cold start is made of.** Google's own account: the image is fetched by streaming (so "the size of your
container image does not affect container startup times"), the entrypoint runs, and Cloud Run waits for the port, or
for the start-up probe when one is set, before forwarding requests; CPU is always allocated during start, and the
start-up window is billed (F). Its one published breakdown, for GPU services, is about 5 seconds of provisioning, 1 to 2
of image streaming and 5 to 15 of engine start (F). Independent numbers for ordinary services: a Go service 107 ms, a
Node.js one 2.0 s, Java with Spring 11.5 s (S, 2022); Python with FastAPI and its imports 3.1 s on average (S, 2025); a
Node.js service "about 6 s" that fell to 2.2 s with startup CPU boost (S, 2026); a Python service with eight gunicorn
workers that took a minute from cold and 25 seconds with two (S, 2026). Second-generation environments "have longer
cold start times than first generation for some services"; Google names "infrequent traffic that frequently scales out
from zero" as a reason to prefer the first (F).

**The probe explains the floor.** A configured start-up probe runs its first attempt `initialDelaySeconds` after the
container starts (0 by default), then every `periodSeconds` (1 to 600, 10 by default); any answer but 2xx or 3xx,
including a refused connection, is a failure; the instance takes traffic once the probe has succeeded once; the window
is `failureThreshold` times `periodSeconds` (F). The page does not say what follows a failed attempt, but the Kubernetes
model these fields copy retries at the next period (S), and a 2025 write-up of the default period reads the same way:
"if your service fails the first attempt after 1 second, it will wait for 10 seconds before retrying" (S). The platform's
HTTP probe therefore tries at second 0 against a port that is not open, fails at once, and succeeds at second 5. Its
TCP probe with the same settings reports 0.3 seconds: Google's own default probe is a TCP check that waits for the port
rather than polling it (F), and the measured 0.3 seconds says a configured TCP probe behaves the same way; why it does
while the HTTP one does not is not documented (U). Google's own guidance on probes elsewhere: a start-up probe's period
should be short, "shorter is better (3-5 seconds)" in one practitioner guide (S), and one project set period 2 with
failure threshold 120 with the note that refused polls "cost nothing" (S).

**What shortens the rest, and what each costs.**

| Lever | Effect | Cost | Status here |
|---|---|---|---|
| Probe period 1 s (threshold 60 keeps the 60-second window) | About 4 seconds off every cold start of every application | none | not done; one number in `labdeploy/release.py` |
| Startup CPU boost (`--cpu-boost`): 2 vCPUs during start and for 10 seconds after, for a 1-vCPU service | Google measured starts "cut in half" for some workloads, up to 50 percent for Java, up to 30 percent for Node.js (F) | the extra vCPU for the start's duration: $0.000024 a second, about $0.0004 per 15-second start (F, arithmetic) | never set |
| First-generation environment | Faster cold starts "for some services" (F); a third-party guide puts the difference at 100 to 500 ms (S) | none | already the default; gen2 is for unreviewed code (D35) |
| The application's own start: lazy imports, one gunicorn worker with threads, `node index.js` rather than `npm start`, precomputed data on a read-only mount | The 15 to 20 seconds of Sophia and the debate backend | the developer's time | the contract notes of 1.25.1 and the diagnosis of 1.24.0 already say so |
| One warm instance (`scaling.minimum: 1`) | No cold start at all for the instance it keeps | $9.72 a month at list for 1 vCPU and 512 MiB (idle CPU $0.0000025 and memory $0.0000025 a second), before the free tier (F, arithmetic) | allowed per application; the weekly report proposes 0 under 1,000 requests a day |
| Manual scaling by schedule (an instance during lab hours only) | Warm during the day | about a third of the above for an eight-hour day | Google's documented pattern with Cloud Scheduler (F); not built |
| The uptime check | Keeps an instance present between its calls, so a person's request finds one | measured: about $0.05 a month of CPU per staging service, about $0.30 for production's, inside the free tier | already the case, by accident of the monitoring design |

In short: the lab is paying nothing for warm instances and getting them anyway from its uptime checks; the one real
cost of a cold start today is the probe's five seconds and the applications' own start, in that order.

## 5. The algorithms, and whether each is worth applying here

The question was which optimisation algorithms exist for raising and lowering resources in the shortest time. There
are six families. For each: what it does, what it needs, whether it exists for Cloud Run, and the verdict for a lab whose
services see a few human requests a day.

| Family | What it does | What it needs | On Cloud Run | Verdict here |
|---|---|---|---|---|
| **Reactive thresholds** (Kubernetes HPA, KEDA, Knative's KPA, Cloud Run's own) | Every few seconds, instances = current × (measured ÷ target), rounded up, with a tolerance band and cool-downs. HPA: 10 percent tolerance, 15-second cycle, 5-minute scale-down window (F). KPA: a 60-second stable window, a "panic" mode at 200 percent of target over 6 seconds, target 70 percent of concurrency (F). Cloud Run: 60 percent target, 10 percent band, 15-minute idle timeout (F) | a utilisation signal; nothing to learn | **It is the algorithm Cloud Run runs**, now with the target adjustable per service (GA 2026-09-29) (F) | Already in use. Lowering the targets would add instances sooner at the cost of more of them; with at most two or three instances allowed and a handful of users, there is nothing for it to do faster |
| **Predictive scaling** (GCP managed instance groups, AWS EC2, Alibaba's AHPA, Crane, the research PHPAs) | Forecasts load from history (daily and weekly cycles) and scales ahead by the start-up time. GCP's needs 3 days of history and ignores patterns shorter than 10 minutes (F); AWS's needs 24 hours, forecasts 48 hours ahead, hourly, and never scales in by itself (F); Alibaba reports 20 percent lower cost on periodic traffic (F) | weeks of traffic with a repeating shape | **Does not exist for Cloud Run**; Cloud Run's page describes no prediction (F) | Not applicable, and the lab's traffic has no cycle to forecast: 81 percent of Azure's serverless apps run once a minute or less and 45 percent once an hour or less (F), and these run less |
| **Control theory** (PID controllers; ARIMA plus PID) | A feedback loop whose gain, integral and derivative terms are tuned on a real workload; reported gains of about 10 percent in utilisation and 30 percent in response time over plain thresholds (S). The HPA itself is a proportional controller with cool-downs (F) | tuning on the workload; sensitive to noise | not offered | Not worth it: a 2025 comparison of threshold, control-theoretic and learning autoscalers concludes that lifecycle concerns, "initialization overhead, metrics, resource limits, health checks", dominate performance (F). Section 4 found exactly such a concern |
| **Learning agents** (FIRM, AWARE, gym-hpa, the 2026 RLScale-Bench) | Reinforcement learning chooses the scaling action. Research reports up to 16 times fewer objective violations (F), but a 2026 benchmark of six algorithms over 240 runs found "the calibrated controller has the lowest cost on all six workloads", with the learners ahead only on flash traffic (F); the AWARE paper exists because untrained agents cause violations while they learn (F) | a simulator or weeks of production traffic to train on, and tolerance for mistakes | not offered | Not worth it, and not safe for a lab with one operator |
| **Queueing-theory sizing** (Little's law, Erlang C) | Instances = arrival rate × service time, plus a margin; AWS states it for Lambda's provisioned concurrency with a 10 percent buffer (F); Alibaba couples it with its forecaster (S) | an arrival rate and a service time | a formula, not a feature | Worth one line in the weekly report: at the lab's rates (a request every few minutes, served in under a second) the answer is always "one instance", which is what the right-sizing report already assumes when it proposes concurrency |
| **Pre-warming policies** (the Azure "hybrid histogram": a per-app histogram of idle times, pre-warm at the 5th percentile, keep alive to the 99th; the closed-form trade-off; Lambda's provisioned concurrency and SnapStart; Azure's always-ready instances) | Decide when to keep or pre-start an instance. The histogram policy cut memory 15 percent and p99 latency 82 percent on OpenWhisk (F). The maths: with requests arriving at rate λ and a keep-alive of T, the chance a request finds nothing is e^(−λT), and expected cold starts peak when λ = 1/T (F) | the idle-time history | Cloud Run's levers are minimum instances, manual scaling by schedule (Google's documented Cloud Scheduler pattern) and the fixed 15-minute keep-alive (F) | Already solved by accident: the uptime check calls every service at least once a minute on average (1.2 a minute in staging, 3 in production), never 15 minutes apart, so the idle timeout is never reached while the checks pass. A warm instance ($9.72 a month) or a lab-hours schedule buys nothing today |
| **Right-sizing recommenders** (Kubernetes VPA, AWS Compute Optimizer, Lambda Power Tuning, Kubecost) | A percentile of measured use plus a margin: VPA targets the 90th percentile with a 15 percent margin and a 24-hour decay (F); Compute Optimizer uses the maximum of each five-minute interval over 14 days and refuses to recommend under 50 invocations (F); Kubecost uses the 95th percentile divided by a 70 percent target (F); Power Tuning tries every memory size and picks the cheapest or fastest (F) | a few days of utilisation data | Google's own recommender for Cloud Run covers the billing mode and security only (F) | **Already built**: the weekly report's memory rule (95th percentile plus 25 percent) sits between VPA's and Kubecost's; its CPU rule (down under 40 percent) matches Google's words; its 14-day window matches Compute Optimizer's. Two refinements are cheap: refuse a proposal on fewer than 50 user requests in the window, as Compute Optimizer does, and tell the reader the data was mostly uptime checks |

Two further points from the evidence. First, the gains from autoscaling at large are small: Datadog's 2024 cost study
finds 83 percent of container spend idle and calls autoscaling's efficiency gains "often marginal and elusive" (F); a
widely read 2023 essay argues a startup does not need autoscaling at all and lists the ways it hurt (F). The lab's
services cost cents a month; no scaling algorithm can save more than that. Second, the thing that does cost, a cold
start's seconds, is governed here by a probe setting and the applications' own start, which no autoscaler touches.

**Verdict.** No new scaling algorithm is worth building. What is worth doing, in order of value per effort: the
probe period (free, about four seconds per cold start), startup CPU boost (a fraction of a cent per start, up to half
the application's start), a minimum-request floor on the right-sizing proposals, and, for the two applications whose
own start takes 15 to 20 seconds, the fixes their developers already know. A warm instance stays what it is today: an
opt-in per application at $9.72 a month, which the weekly report proposes to turn off under 1,000 requests a day.

## 6. A plan for every issue: the catalogue, and what sees each one

Google publishes no failure catalogue for Cloud Run; its troubleshooting page, with about 39 documented scenarios, is
the closest thing (F). The table below is that page, the quota page, each dependency's own pages and the SRE and
Well-Architected checklists, read against what the platform runs today (1.27.0, with Phase 23's alerts applied in
staging this afternoon). "Seen by" says what notices it without a person: **A** within minutes to a day (an alert, the
bake, the watchdog, the digest, the pipeline), **W** weekly or only when someone asks, **N** nothing until a person
notices. "Plan" names the runbook section that says what to do.

**The service itself**

| # | What can go wrong | How it shows | Seen by | What happens by itself | Plan |
|---|---|---|---|---|---|
| 1 | The new version does not start at a release (wrong port, loopback, out of memory, a root-owned path, a missing module, a probe that never answers) | "failed to start and then listen on the port", "memory limit exceeded", `EACCES` (F) | A: the release's probes | nothing new serves; the candidate's lines and the diagnosis name the fix | the status's next action; "A release stopped by a safety gate" |
| 2 | The new version takes traffic and crashes or errors within minutes | 5xx, a container exit | A: the bake (5 minutes, or each step in production) | the previous traffic comes back | "After a release"; "Undo a promotion" |
| 3 | The new version degrades hours later | 5xx share up, latency up | A: the server-errors alert (more than 5 in five minutes), the latency alert, production's burn alerts, the release report a day later ("worse", posted at once), the digest | nothing; a person rolls back | "After a release"; "An incident" |
| 4 | An instance runs out of memory | "found to be using too much memory and was terminated", in-flight requests end 500 (F) | A: memory above 80 percent for five minutes; the 5xx alert; the health view's diagnosis | the instance restarts | the weekly report proposes the size; the diagnosis names `resources.memory_mib` |
| 5 | The instance limit is reached | 429 "The request was aborted because there was no available instance." (F), logged at WARNING | **N for a default application**: the "at maximum instances" alert needs 3 or more active instances, but `scaling.maximum` is 2 by default; 429 is not a server error, so neither the 5xx alert nor the objective counts it; the digest counts ERROR lines only | requests wait up to 30 seconds, then fail (F) | none today |
| 6 | A request outlives the timeout | 504 "maximum request timeout" (F) | A: 504 is a 5xx: the errors alert, the objective; the diagnosis names `timeout_seconds` | the connection closes while the app may keep working (F) | the diagnosis's next action |
| 7 | Responses slow without failing | p95 up | A: the latency alert (above 2,000 ms for five minutes); the release report's latency rule | nothing | the diagnosis, the weekly report |
| 8 | Cold starts grow long (an app that loads a model at start) | the first request after a release or a recycle waits | **N**: `container/startup_latencies` is not read by any alert, view or report | nothing | none; section 4 gives the levers |
| 9 | The health route breaks while the app still serves | the health path answers 5xx | A: "health check failing" (more than one region for five minutes); the post-switch probe at a release | nothing | "What up means" |
| 10 | The spending cap closes the service | ingress internal-only; 404 from outside | A: the health-check alert; the health view says "ingress is internal"; the budget email | the cap did it on purpose | "The spending cap fired" |
| 11 | Google's native spend cap trips | 5xx from Google's front door for every service; recovery up to 30 minutes after lifting (F) | A: the health-check alert for every service at once; the health view's uptime line | nothing until the month ends or the cap is lifted | "The native spend cap tripped" |
| 12 | A secret's version is disabled or destroyed | the next new instance does not start: "the instance could not start successfully" (F); the running one keeps serving | A: the 5xx alert and the health-check alert once an instance is recycled, so hours later; the release's own pre-check at the next release | nothing | "A new secret version"; the diagnosis has no line for this message |
| 13 | A bucket mount fails at start | the container does not start if the mount takes over 30 seconds (F) | A: as row 12 | nothing | none named; the diagnosis has no line for it |
| 14 | Someone edits the service in the console (size, variables, min instances) | the live service differs from the contract | **W**: the weekly cost report compares CPU, memory, concurrency and instance bounds with `platform.yaml`; the Terraform drift check ignores the revision template by design, so variables, secrets and the environment are not compared | the next release overwrites it | "Drift reported" |
| 15 | The runtime identity loses a role (an IAM edit by hand) | the app's calls to Google answer 403; errors or wrong answers | A for the symptom (5xx) when the app fails; **W** for the cause: the drift check sees a removed grant on Monday | nothing | "Drift reported"; "An incident" |
| 16 | The image of a serving revision is cleaned up | nothing: Cloud Run keeps its own copy as long as a revision serves (F); the registry keeps `serving-<revision>` tags | not needed | | |

**The dependencies**

| # | What can go wrong | How it shows | Seen by | What happens by itself | Plan |
|---|---|---|---|---|---|
| 17 | Firestore contention, deadline or the daily free quota exhausted | ABORTED, DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED from the client (F) | A only when the app turns it into a 5xx; **N** when the app degrades quietly (an empty page with 200) | nothing | none; the health view shows only that the database exists |
| 18 | The Postgres instance is stopped (by the cap, or by hand) | connection refused | A: 5xx; the health view's needs line says "not running" | nothing | "The spending cap fired"; "Retire a database deliberately" |
| 19 | Cloud SQL maintenance drops connections | under 30 seconds of refused connections in the window (F) | A: possibly the 5xx alert (noise) | nothing | none; Google's maintenance emails need an opt-in and a deny period is possible (F) |
| 20 | The database disk fills, or its connections run out (about 25 on the smallest tier) | writes fail; "too many connections" (S) | A for the symptom; **N** for the cause: no alert on disk or connections | nothing | none yet; no production database exists |
| 21 | An automated backup fails | an email only if a log-based alert is configured (F) | **N** | | "Production's data" (the restore drill) |
| 22 | Vertex AI refuses (429 "Resource exhausted") or retires a model | app errors or slow answers | A when the app returns 5xx; **N** when it answers "try later" with 200 | nothing | "An application that calls Claude" |
| 23 | A declared topic backs up (dead letters, old unacknowledged messages) | work not done; nothing fails visibly | **N**: no alert on unacknowledged age or dead-letter count | nothing | none |
| 24 | Data in a bucket deleted or overwritten by the app | nothing fails | **N**; the pipeline cannot delete buckets, but the app can delete objects; no versioning is documented for application buckets | nothing | "Production's data" covers databases only |

**Google's side, and the limits**

| # | What can go wrong | How it shows | Seen by | What happens by itself | Plan |
|---|---|---|---|---|---|
| 25 | A Google incident in us-central1 (Cloud Run for 4 hours on 1 Sep 2026, Firestore for 1 hour the same day) (F) | errors, slowness, failed releases | A for the symptom (the health-check alert); **N** for the cause: Personalized Service Health, Google's own incident feed, is not wired, and Google's monitoring page recommends alerting on it (F) | nothing | "An incident" does not say to check Google's status |
| 26 | Cloud Monitoring itself is degraded (alerts go quiet) | silence | partly: the controller's GitHub ping is outside Google; an application's silence is indistinguishable from health | | the digest says when it could not read |
| 27 | A quota runs out (100 instances per region; 180 Admin API writes a minute; 50 tokens an instance a second) (F) | 429 "no available instance"; failed deploys | **N**: no quota alert; Google's recommended rule is usage over 80 percent of the limit (F) | | none |
| 28 | A regional deploy slowness (a known issue names us-central1) (F) | a release takes longer | A: the run's timeout | the release fails and traffic is restored | "A release is stuck" |

**The delivery path**

| # | What can go wrong | How it shows | Seen by | What happens by itself | Plan |
|---|---|---|---|---|---|
| 29 | The controller's machine, Caddy or its certificate is down | Claude's tools fail | A: the uptime check (every minute, certificate checked) and the GitHub ping every 30 minutes | releases already running finish on GitHub | "The controller's machine" |
| 30 | The controller runs but its background jobs are stuck | no digest, no follow-up, no auto-deploy | A: the watchdog's digest check, the Controller watch's daily issue check (two days) | | "The controller's machine" |
| 31 | GitHub is down or rate-limits the controller | releases and status calls fail with GitHub's words | A at the moment of use (the tool's answer); **N** otherwise | the release stays dispatched; the follow job retries every minute | "A release is stuck" |
| 32 | The GitHub App's key or a token is revoked | 401 (F) | A: the daily watchdog; every tool's answer | | "Rotate credentials" |
| 33 | Workload Identity Federation refuses a run (a pool condition, an attribute over 127 bytes) (F) | the deploy job fails at sign-in | A: the status's next action, the notification | traffic untouched | the status's words |
| 34 | GitHub disables a scheduled workflow after 60 idle days | the weekly report or drift check stops | A: the watchdog (workflow state, report age over eight days) | | the watchdog's line |
| 35 | The billing export stops arriving | the weekly report and the daily cost line say "unknown, not zero" | A: both say so | | "Cost controls" |
| 36 | Budget notifications arrive hours late (F) | the cap acts late | partly: the native cap is the second layer; Phase 24's daily forecast against the budget | | "The spending cap fired" |
| 37 | The cap's workflow fails | | A: "the spending cap could not act" (CRITICAL) | | "Prove the alerts work" |
| 38 | Spend leaks between Mondays | | A once Phase 24's steps are done (the daily cost line; Google's anomaly detection) | | section 7q of the setup guide |

**Security**

| # | What can go wrong | How it shows | Seen by | What happens by itself | Plan |
|---|---|---|---|---|---|
| 39 | A credential is committed | | A: GitHub's secret scanning; the pipeline's secret scan of the checkout and the image (a verified hit stops unreviewed code) | the release stops | "Rotate credentials" |
| 40 | A dependency is vulnerable or malicious at release | | A: the Python audit, OSV for Node.js, the image scan | the release stops (unreviewed code) or reports | the status's next action |
| 41 | A new serious finding appears in an image that already serves | | **N**: Artifact Analysis keeps scanning only images pulled within 30 days (F), and Cloud Run never pulls a serving image again (F); nothing re-alerts | | none; Dependabot proposes updates on the repository |
| 42 | Someone grants a role by hand, or makes a service public | a member beside the platform's own | **N**: the drift check sees only the members Terraform manages ("a grant someone adds next to the platform's own is invisible to it", the setup guide); no audit-log alert on `SetIamPolicy` exists | | "Drift reported" would not fire |
| 43 | A leaked service-account key | | A: Google disables leaked keys it finds (an organisation policy since 2024) (F); the platform creates no keys | | "Rotate credentials" |
| 44 | A viewer who should not see a page, or one who should and cannot | a 403 from IAP | **N** for the first; the person for the second | | "Someone cannot open an app's pages" |
| 45 | A custom domain's certificate fails to renew or DNS moves | the domain stops answering while run.app still does | **N**: the uptime check calls the run.app address, not the domain; no domain is in use yet | | "A custom domain" |
| 46 | The organisation's policies or a project setting change | | **N**: Security Command Center's free tier is not enabled (Phase 17) | | Phase 17 |

**The count.** Of the 46 rows, 25 are seen within a day by something automatic; 9 only in part (the symptom but not
its cause, only weekly, or only at the moment someone uses a tool: rows 14, 15, 17, 20, 22, 25, 26, 31, 36); 11 are not
seen until a person notices (rows 5, 8, 21, 23, 24, 27, 41, 42, 44, 45, 46); and one (16) needs no watching. Watching
symptoms rather than causes is the right design (the SRE book: "spend more effort on symptoms than causes") (F), and
the symptoms are well covered. The holes are of three kinds: requests the platform refuses without calling it an error
(the 429 at the instance limit, a quota), causes that fail quietly (a stuck topic, a slow start, a dependency answering
200 with nothing in it), and edges outside the pipeline (Google's own incidents, a role granted by hand, a new
vulnerability in an image that already serves).

## 7. What would be worth doing, ranked

Nothing below was changed; this is the review's proposal. The roadmap's rule applies: value per effort, cost first.

| Rank | Change | Why | Cost | Effort |
|---|---|---|---|---|
| 1 | **Start-up probe period 1 second**, failure threshold 60 (the same 60-second window) | About 4 of the 5 seconds of every cold start of every application (section 3) | none: refused probes cost nothing (S) | one number in `labdeploy/release.py`, effective at each application's next release |
| 2 | **Startup CPU boost on** (`--cpu-boost`) | Up to half the application's own start (Google measured up to 50 percent for Java, 30 for Node.js) (F); matters for Sophia's 20 seconds and the debate backend's 15 | about $0.0004 per 15-second start, so cents a month at the measured start rates | one flag in the same place |
| 3 | **An alert on refused requests**: any 429 in five minutes, and "at maximum instances" at the application's own maximum rather than a fixed 3 | Row 5: today a default application can turn people away with nothing said | none (inside the free allotment; one policy per service, counted under Google's 2027 alert pricing) | the service module and the root renderer |
| 4 | **Personalized Service Health in the digest and as one alert** on Cloud Run, Firestore and Cloud SQL incidents in us-central1 | Row 25: tells the operator when it is Google, not the app, which changes what to do; Google's own monitoring page recommends it (F) | free; a log-based alert in each project | the foundation |
| 5 | **Cold-start and quota lines in the digest**: each service's slowest start of the day from `container/startup_latencies`, and any quota above 80 percent of its limit (Google's recommended rule) (F) | Rows 8 and 27 | none | the digest's builder |
| 6 | **An audit-log alert on IAM changes** (`SetIamPolicy` by anyone but the platform's apply identity) in both projects | Row 42: a role granted or a service made public by hand is invisible to the drift check | free | the foundation |
| 7 | **The right-sizing report's floor**: no proposal on fewer than 50 user requests in the window (Compute Optimizer's rule) (F), and a note when the measured use was mostly uptime checks | The weekly report sizes from samples that are nearly all health checks (section 3) | none | `labdeploy/cost.py` |
| 8 | **Per-dependency alerts when declared**: Pub/Sub's oldest unacknowledged message and dead letters for a declared topic (F); Cloud SQL disk, connections and a failed backup for a declared database (F) | Rows 20, 21, 23, for the applications that have them | free | the service module, only where `needs` declares them |
| 9 | **A weekly scan of serving images** (an on-demand scan of each live digest) | Row 41: Artifact Analysis stops re-scanning images not pulled in 30 days (F), and Cloud Run never pulls again | $0.26 per scan (S): about $1 a month for the current services | the weekly report |
| 10 | **An uptime check on a custom domain** when one exists | Row 45 | inside the free allotment | `domain.tf` |

Not worth doing, with the reason: a predictive, control-theoretic or learning autoscaler (section 5: none exists for
Cloud Run, all need a traffic pattern the lab does not have, and a 2026 benchmark found a calibrated threshold
controller cheapest on every workload); a warm instance by default ($9.72 a month per service for a benefit the uptime
check already gives for cents); keep-warm pings beyond the uptime check (Google guarantees nothing about warmth either
way, and the check already does it); lowering Cloud Run's scaling targets (at two or three instances there is nothing
to scale faster); second-generation environments for reviewed code (slower to start, F).

Three cautions. Ranks 1 and 2 change every application's next release, so they belong in a version with its own live
check (a cold start measured before and after on one service). The uptime check's warming effect is a side effect, not
a guarantee; if an application ever drops its uptime check (a trial with `runtime_alerts: false`, D63), its first
request of the day pays the full cold start. And Google's alert pricing from 2027-09-01 counts each new policy, which is
why ranks 3, 4 and 6 are a few policies per project, not per service, where possible.

## 8. Limits of this review

The measurements cover 9 days of staging (the services were recreated on 2026-10-12) and 3 days of production, in a
month with two large trials; a quiet month would show fewer starts. `container/startup_latencies` is a beta metric whose
definition does not say whether it ends at the port or at the probe's success (F), so the explanation of the
five-second floor rests on the probe's documented fields, a practitioner's reading of the retry, and the measured
contrast between HTTP and TCP probes; it was not tested by changing a probe (the review changed nothing). Why a
configured TCP probe with a five-second period reports 0.3 seconds is not documented (U). The cold-start figures of
other platforms are vendors' or practitioners' (S). The failure catalogue is built from Google's documented scenarios
and the platform's own runbook; real incidents will add rows. Prices are list prices before the free tier and before
credits.

## 9. Sources

Marks: F read on the official page or source file on 2026-10-21; S a secondary source; U unverified.

**Cloud Run.** [Instance autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling) F ·
[Scaling controls](https://docs.cloud.google.com/run/docs/configuring/scaling-controls) F · [Minimum
instances](https://docs.cloud.google.com/run/docs/configuring/min-instances) F · [Maximum
instances](https://docs.cloud.google.com/run/docs/configuring/max-instances) F · [Manual
scaling](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling) F · [Billing
settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings) F · [CPU and startup CPU
boost](https://docs.cloud.google.com/run/docs/configuring/cpu) F · [Health checks](https://docs.cloud.google.com/run/docs/configuring/healthchecks) F ·
[Container contract](https://docs.cloud.google.com/run/docs/container-contract) F · [Execution
environments](https://docs.cloud.google.com/run/docs/about-execution-environments) F · [General
tips](https://docs.cloud.google.com/run/docs/tips/general) F · [Python tips](https://docs.cloud.google.com/run/docs/tips/python) F ·
[Node.js tips](https://docs.cloud.google.com/run/docs/tips/nodejs) F · [Java tips](https://docs.cloud.google.com/run/docs/tips/java) F ·
[Cost optimization](https://docs.cloud.google.com/run/docs/tips/services-cost-optimization) F ·
[Recommender](https://docs.cloud.google.com/run/docs/recommender) F · [Troubleshooting](https://docs.cloud.google.com/run/docs/troubleshooting) F ·
[Known issues](https://docs.cloud.google.com/run/docs/known-issues) F · [Quotas](https://docs.cloud.google.com/run/quotas) F ·
[Secrets](https://docs.cloud.google.com/run/docs/configuring/services/secrets) F · [Cloud Storage
mounts](https://docs.cloud.google.com/run/docs/configuring/services/cloud-storage-volume-mounts) F ·
[Deploying](https://docs.cloud.google.com/run/docs/deploying) F · [Monitoring](https://docs.cloud.google.com/run/docs/monitoring) F ·
[Release notes](https://docs.cloud.google.com/run/docs/release-notes) F · [Pricing](https://cloud.google.com/run/pricing) F ·
[Startup CPU boost announcement (2022)](https://cloud.google.com/blog/products/serverless/announcing-startup-cpu-boost-for-cloud-run--cloud-functions) F ·
[Min instances announcement (2020)](https://cloud.google.com/blog/products/serverless/cloud-run-adds-min-instances-feature-for-latency-sensitive-apps) F ·
[Lifecycle of a container (2021)](https://cloud.google.com/blog/topics/developers-practitioners/lifecycle-container-cloud-run) F ·
[AI cold starts on Cloud Run (2026)](https://cloud.google.com/blog/topics/developers-practitioners/a-guide-to-ai-cold-starts-on-cloud-run/) F ·
[Health checks launch (2022)](https://cloud.google.com/blog/products/serverless/cloud-run-healthchecks) F.

**Monitoring and Google's side.** [Cloud Monitoring metrics](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z) F ·
[Uptime checks](https://docs.cloud.google.com/monitoring/uptime-checks) F · [Observability pricing](https://cloud.google.com/stackdriver/pricing) F ·
[Quota metrics](https://docs.cloud.google.com/monitoring/alerts/using-quota-metrics) F · [Log-based
alerts](https://docs.cloud.google.com/logging/docs/alerting/log-based-alerts) F · [Personalized Service
Health](https://docs.cloud.google.com/service-health/docs/overview) F · [Its alerts](https://docs.cloud.google.com/service-health/docs/configure-alerts-cloud-logging) F ·
[Incident communication](https://docs.cloud.google.com/service-health/docs/incident-communication) F · [Status
summary](https://status.cloud.google.com/summary) F · [Reliability framework](https://docs.cloud.google.com/architecture/framework/reliability) F ·
[SRE: monitoring distributed systems](https://sre.google/sre-book/monitoring-distributed-systems/) F · [SRE: emergency
response](https://sre.google/sre-book/emergency-response/) F · [AWS: monitor workload
resources](https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/monitor-workload-resources.html) F.

**Dependencies and security.** [Firestore error codes](https://docs.cloud.google.com/firestore/docs/understand-error-codes) F ·
[Cloud SQL maintenance](https://docs.cloud.google.com/sql/docs/postgres/maintenance) F · [Cloud SQL
troubleshooting](https://docs.cloud.google.com/sql/docs/postgres/troubleshooting) F · [Cloud SQL
metrics](https://docs.cloud.google.com/sql/docs/postgres/admin-api/metrics) F · [Pub/Sub dead
letters](https://docs.cloud.google.com/pubsub/docs/handling-failures) F · [Pub/Sub monitoring](https://docs.cloud.google.com/pubsub/docs/monitoring) F ·
[Vertex AI 429](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/error-code-429) F · [Workload Identity
troubleshooting](https://docs.cloud.google.com/iam/docs/troubleshooting-workload-identity-federation) F ·
[Artifact Registry cleanup](https://docs.cloud.google.com/artifact-registry/docs/repositories/cleanup-policy-overview) F ·
[Artifact Analysis scanning](https://docs.cloud.google.com/artifact-analysis/docs/scanning-types) F · [Leaked keys
disabled](https://cloud.google.com/blog/products/identity-security/automatically-disabling-leaked-service-account-keys-what-you-need-to-know) F ·
[Load balancer certificates](https://docs.cloud.google.com/load-balancing/docs/ssl-certificates/troubleshooting) F ·
[Budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets) F.

**Algorithms.** [Kubernetes HPA](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/) F ·
[KEDA](https://keda.sh/docs/2.17/concepts/scaling-deployments/) F · [Knative KPA](https://knative.dev/docs/serving/autoscaling/kpa-specific/) F ·
[Knative concurrency](https://knative.dev/docs/serving/autoscaling/concurrency/) F · [GCP predictive
autoscaling](https://docs.cloud.google.com/compute/docs/autoscaler/predictive-autoscaling) F · [AWS predictive
scaling](https://docs.aws.amazon.com/autoscaling/ec2/userguide/predictive-scaling-policy-overview.html) F · [AHPA
(Alibaba)](https://arxiv.org/abs/2303.03640) F · [Predictive HPA](https://predictive-horizontal-pod-autoscaler.readthedocs.io/en/latest/) F ·
[Google Autopilot](https://research.google/pubs/autopilot-workload-autoscaling-at-google-scale/) F · [Autoscaler
comparison (2025)](https://arxiv.org/abs/2510.02585) F · [FIRM](https://arxiv.org/abs/2008.08509) F ·
[AWARE](https://par.nsf.gov/biblio/10465144-aware-automate-workload-autoscaling-reinforcement-learning-production-cloud-systems) F ·
[RLScale-Bench (2026)](https://arxiv.org/abs/2605.26418) F · [Serverless in the Wild](https://ar5iv.labs.arxiv.org/html/2003.03423) F ·
[Keep-alive trade-off](https://arxiv.org/html/2409.09202v3) F · [Lambda provisioned
concurrency](https://docs.aws.amazon.com/lambda/latest/dg/provisioned-concurrency.html) F · [Kubernetes VPA
flags](https://raw.githubusercontent.com/kubernetes/autoscaler/master/vertical-pod-autoscaler/docs/flags.md) F · [AWS Compute
Optimizer for Lambda](https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-lambda-recommendations.html) F · [Lambda Power
Tuning](https://github.com/alexcasalboni/aws-lambda-power-tuning) F · [Datadog State of Cloud
Costs](https://www.datadoghq.com/state-of-cloud-costs/) F · [Why your startup doesn't need
autoscaling](https://madewithlove.com/blog/why-your-startup-doesnt-need-autoscaling/) F.

**Cold-start measurements and practice.** [Seroter: startup by language (2022)](https://seroter.com/2022/04/18/measuring-container-size-and-startup-latency-for-serverless-apps-written-in-c-node-js-go-and-java/) S ·
[Cloudflare: Python cold starts (2025)](https://blog.cloudflare.com/python-workers-advancements) S · [A Node.js cold-start
incident (2026)](https://www.72technologies.com/blog/gcp-cloud-run-cold-start-incident) S · [FastAPI workers and cold starts
(2026)](https://github.com/silvxlabs/FastFuels-API-v2/issues/693) S · [The default probe's period
(2025)](https://esg.dev/posts/cloudrun-startup-probes-default/) S · [A 2-second probe period
(2026)](https://github.com/bohemian-miser/Spectacle/pull/85) S · [Cloud Run FAQ](https://github.com/ahmetb/cloud-run-faq/blob/master/README.md) S ·
[Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/) S.
