# Jobs an application starts itself: what CodeSurgeon needs from Fast Deploy 1.30.0

Written 2026-10-21 UTC. The operator received a request from the developer of CodeSurgeon (its code is called Smart
Dev): beside its always-on part, the application starts two short-lived Cloud Run jobs per GitHub issue, an AI agent
that writes a fix and a checker that runs the tests, and the developer asked for three things to be set up by hand: two
extra images in Artifact Registry, a bucket, and permission for the service to start jobs. A second message asked
whether the test checker could reuse "the image the platform runs the tests in". The operator asked for the features to
be added to the platform instead, quickly, in staging and production.

Method: the application's code (a zip of 28 files, read only, nothing run) was read in full; the platform's own code
was read where a job would touch it (`labdeploy/models.py`, `labdeploy/release.py`, `templates/deploy-gcp.yml`,
`infra/gcp/modules/service/`, `infra/gcp/modules/project/cap-workflow.yaml`, `labdeploy/scheduler.py`,
`labdeploy/scaffold.py`); two searches ran against Google's, GitHub's, Docker's and LiteLLM's documentation and returned
only what they had fetched (section 8). Nothing in the cloud was changed.

## 1. The short answer

**Two things are missing, and both are platform features, not settings to make by hand.** Fast Deploy runs one kind of
workload: a service that answers requests and gets CPU only while it does. CodeSurgeon needs a way to start short jobs
of its own and a clock that wakes it. The bucket already exists (`needs.buckets`). Images pushed by hand would skip the
platform's checks (Dockerfile policy, secret scans, parts list, provenance, the vulnerability gate) and its cost cap, and
the agent image is the one that most needs them: it runs code an AI wrote.

**There is no test image to reuse.** The platform runs an application's tests on a fresh GitHub Actions machine, installs
its packages from scratch with no cache and throws the machine away (`templates/deploy-gcp.yml`, the Node.js test step);
the only image it keeps is the production one, which has no npm and no development dependencies. The platform can make
the image the developer asks for: Node.js and the application's locked packages, from the application's own files, at
the commit that is released, so nobody maintains a copy.

**The largest risk is spend, and it comes from one of Google's permissions.** Starting a job with per-run arguments needs
`run.jobs.runWithOverrides`, and that one permission also lets the caller set the task count (up to 10,000) and the task
timeout (up to 168 hours) of the run. No role separates them. The platform therefore watches every execution and stops
what breaks the contract's limits (section 5.6).

## 2. What CodeSurgeon is

A laptop demo today. `watch.py` loops forever, asking GitHub every 10 seconds for new issues of the repositories in
`apps.yaml`, and handles each one in turn: clone, run the agent in a container, check the fix in fresh containers with no
network, then open a pull request (never merged) or comment. `containers.py` runs every container through `podman run`;
`dashboard.py` serves a page at `127.0.0.1:7070` from the files the others write under `runs/`. The agent is the
OpenHands SDK 1.55.0 on an OpenAI-compatible model server on the laptop, with up to 40 steps. Publishing uses the
developer's own `gh` login.

The two images are `node.Dockerfile` (Node 22 on `node:22-slim`, unpinned; the application's packages installed with
`npm ci --ignore-scripts` into `/deps`; a writable `/work`; the `node` user) and `agent.Dockerfile` (`FROM ${BASE}`, the
test image, plus git, tmux, uv, Python 3.12 and about 1,400 Python packages exported without hashes). Their tags are a
hash of the application's `package-lock.json`, typed by hand in three files.

What the move needs from its own code, whatever the platform does (the operator sent these to the developer): the loop
becomes a request the clock calls, advancing each issue a step at a time; state moves from local files to the bucket;
Podman calls become job starts; the dashboard listens on `0.0.0.0:$PORT`; a GitHub token in Secret Manager replaces the
personal login; runs only for issues from collaborators or with a label; the agent reaches the model and nothing else;
the checker's verdict stays the exit code, read from the execution, never a file the tested code could write.

## 3. What the platform has today

- One Cloud Run job exists already: the migration job (`needs.migrate`). Terraform creates it with the bootstrap image and
  ignores its template (`infra/gcp/modules/service/needs.tf`); each release points it at the release's image with the
  contract's environment, secrets and egress and runs it before any new code serves (`labdeploy/release.py`, `migrate`).
  New jobs follow the same pattern.
- Private egress (`egress: private`, the default for code nobody has reviewed) sends all traffic into the lab's network,
  which has Private Google Access and no Cloud NAT: Google's APIs, the bucket and Claude on Vertex AI's global endpoint
  are reachable, the internet is not (`egress_flags`). That is what the agent needs with `models: [claude]`.
- The release probe already reaches a service behind Identity-Aware Proxy programmatically: a Google-signed identity token
  for the probe identity, with the foundation's IAP OAuth client as its audience, and `iap.httpsResourceAccessor` on the
  service (`health` in `labdeploy/release.py`, `main.tf`). The clock uses the same path.
- The controller's scheduler (`labdeploy/scheduler.py`) wakes every 15 seconds and runs due jobs; the machine's identity
  holds `roles/run.viewer` in each project, so it can read every execution already.
- The spending cap (`cap-workflow.yaml`) closes the ingress of every managed service and stops Cloud SQL; it does not
  touch jobs.
- Each application has its own Cloud KMS signing key when the foundation has its key ring (`provenance.tf`), whatever the
  repository's visibility, so job images can be signed in a loop with Cosign.

## 4. Google's facts that shape the design

Checked on 2026-10-21; each line names its source in section 8.

- **Starting a job.** `run.jobs.run` starts it as defined; `run.jobs.runWithOverrides` starts it with overrides, which
  change the arguments, the environment (merged), the task count and the task timeout of that execution only; the image,
  CPU, memory, identity, network and retries cannot be overridden [1][2]. `roles/run.jobsExecutorWithOverrides` holds
  run, run with overrides and cancel; reading executions and tasks needs `run.viewer` [3]. Job-level grants are documented
  for `run.invoker`, `run.developer` and `run.viewer`; for the executor roles no lowest-level resource is listed, which
  the live exercise checks (a custom role is the fallback) [3].
- **Results.** An execution reports succeeded, failed and cancelled counts and its times; each task's last attempt
  reports its exit code [4]. Cancelling does not refund what already ran [2].
- **Limits.** Default task timeout 10 minutes, maximum 168 hours; default retries 3 (0 to 10); jobs always run in the
  second-generation environment; memory 512 MiB to 32 GiB (2 vCPU allows up to 8 GiB); the in-memory filesystem counts
  against memory; 1,000 running executions and 180 runs a minute per project and region; the newest 1,000 executions of
  a job are kept [5][6][7].
- **Network.** Jobs support Direct VPC egress (GA 2025-04-07); with all traffic into a network without Cloud NAT and with
  Private Google Access, Google's APIs are reachable and the internet is not [8]. Cloud Run has no "no network" setting for
  a job; sandboxes (Preview) would give one and are not used [9].
- **Storage.** Cloud Storage volume mounts work for jobs, read-write or read-only (GA 2024-08-27); Google advises against
  keeping a Git repository on one [10]. A job copies its work into its own memory or ephemeral disk (Preview) instead.
- **Price.** Jobs bill instance-based for the instance's whole life with a minimum of one minute: $0.000018 per
  vCPU-second and $0.000002 per GiB-second in us-central1, with a monthly free tier of 240,000 vCPU-seconds and 450,000
  GiB-seconds per billing account [11]. A job that polled GitHub every minute would bill at least 43,200 minutes a month
  (about $47 at 1 vCPU, $49.93 for an always-on instance-based service), which is why the clock wakes the service
  instead: a short request-billed request is free within the request-based free tier.
- **Cost control.** A job has no "paused" state [12]. Google's own Cloud Run spend caps became generally available on
  2026-10-12 and pause every Cloud Run workload of a project, services included; the Terraform provider has no field for
  them yet [13]. The platform's cap stays and learns jobs (section 5.7); adopting Google's cap is the operator's
  separate decision.
- **Cloud Scheduler** costs $0.10 a job a month beyond three free per billing account and reaches a service with an OIDC
  token and `run.invoker` [14]. Calling a service behind IAP needs a custom OAuth client on IAP's programmatic allowlist,
  and no page documents Scheduler with IAP [15]. A same-project Scheduler job also passes an internal-only ingress, so
  the cap would have to pause it too [16]. The operator chose the platform's own clock (D66).

## 5. The design

### 5.1 The contract

```yaml
jobs:                        # at most 4; each a Cloud Run job named <service>-<name>
  agent:
    dockerfile: sandbox/agent.Dockerfile      # in the service's folder; its own folder is the build context
    base: bettercare                          # optional: the platform's test image of an onboarded Node.js application
    command: [python, -m, smartdev.sandbox_agent]
    resources: { cpu: 2, memory_mib: 4096 }   # policy: 2 CPUs and 4096 MiB at most (D67)
    timeout_minutes: 60                       # 1 to 60; one try, never retried
    runs_per_day: 30                          # 1 to 30 (D65); the guard stops runs beyond it
    buckets: { runs: write }                  # read or write, declared buckets only
    models: [claude]
    egress: private                           # private (the default) or internet
  checker:
    dockerfile: sandbox/checker.Dockerfile
    base: bettercare
    resources: { cpu: 2, memory_mib: 2048 }
    timeout_minutes: 15
    buckets: { runs: read }
wake:
  path: /tick                                 # the clock sends POST here
  every_minutes: 5                            # 1 to 60
```

The service receives `JOB_<NAME>` (the job's full resource name) for each job; each job receives the variables of
what it was given (`BUCKET_<NAME>`, the Vertex variables) and nothing else of the service's.

### 5.2 Identities and permissions

One identity per job, `<service>-<name>` (so the service name and the job name together stay within the 30 characters
of an account id), holding only its own buckets (object viewer for read, object user for write), the model role for
`models`, and the secrets it names. The service's runtime identity may start each declared job with overrides, cancel it
and read its executions, on those jobs only. The deployer may update each job and act as its identity. The machine may
cancel any execution of the application's jobs (a foundation custom role, `labDeployJobGuard`: executions get, list and
cancel). The cap's role gains the same three permissions.

### 5.3 Images

Each job's Dockerfile is held to the same policy, lint, secret scans and parts list as the service's, built without cloud
credentials, saved into the release bundle, pushed as `<registry>/<service>-<name>`, signed with the application's Cloud
KMS key (provenance and parts list, in a loop, for any repository), verified, and summarised by the vulnerability step
under the contract's trust. The registry's cleanup keeps the newest versions per package, so job images get the same
retention as the service's.

### 5.4 The test image of another application (`base`)

`base: <service>` names an onboarded Node.js application. The platform renders a Dockerfile from that application's
contract (its Node line, `build.scripts`) and builds it in the job's own release from the application's repository at the
commit its last release deployed: `node:<line>-trixie-slim` by digest, tini, the locked packages with every install
script off (then only `build.scripts` runs its own) in `/deps`, npm kept, a writable `/work`, user 65532, no code of the
application. The job's Dockerfile receives it as the build argument `BASE`. The application named by `base` is not
changed and its release does not wait. When that application's lockfile, Node line or script list changes in a release,
the controller releases every application whose job names it again, so the test image follows it.

### 5.5 Releases

Before the candidate is deployed, the release points each job at its new image (as for the migration job), records the
previous images in the receipt, and on any later failure (candidate, promotion, bake, cancel) points them back. The rule
is the migration's: a job's interface stays compatible with the previous service for one release. Production gets
exactly the staging images, copied by digest and signed again with production's key, through the promotion.

### 5.6 The clock and the guard (the controller)

Every minute the controller wakes each application with `wake` whose interval has passed, through a thread per call so a
slow application never holds the scheduler: an identity token for the application's `<service>-wake` identity (the
machine may mint it), the run.app URL or the IAP client as audience, `POST <path>`, the request timeout of the contract.
Failures are counted, never retried in a loop, and shown in the health view and the digest. A service the cap has closed
is not woken.

Every minute the guard reads the running executions of every application's jobs and cancels one that runs more than one
task, has run past its `timeout_minutes`, or is beyond the day's `runs_per_day` (UTC), and records each cancel in the audit
trail and the digest. The worst case per job and day is therefore `runs_per_day` times its timeout, which every plan shows
in dollars.

### 5.7 The cap

The cap workflow also cancels every running execution of a managed job; the clock stops waking an application whose
service the cap closed. What already ran is billed.

## 6. Cost

At the list prices of section 4, a job of 2 vCPU and 4 GiB costs $0.000044 a second, about $0.16 an hour; CodeSurgeon's
agent at 30 runs of at most 60 minutes is at most $4.75 a day of compute, and a 15-minute run about $0.04. The checker at
2 vCPU and 2 GiB is about $0.14 an hour. The model's tokens bill separately to the project, inside its budget and cap,
and are the larger part: the developer's run summary records them, and the operator asked for real numbers. The clock
and the guard cost nothing; the wake requests fit the request-based free tier. Registry storage for two more images is
cents a month.

## 7. Checked in the live exercise, not in the documentation

Whether `run.jobsExecutorWithOverrides` and `run.viewer` granted on a job cover its executions and tasks (else a custom
role at job level); whether running a job needs act-as on its identity (the docs name it for create and update only);
the self-made test image with BetterCare's lock; a wake through IAP with the probe's audience; the guard's cancel; the
cap drill with a running execution.

Found on 2026-10-21: the executor role granted on a job does start it with overrides, but `run.viewer` granted on the job
does not let the caller read the job's executions or tasks (403), while the same role on the project does. Reading how a run
ended therefore goes through a narrow project-level role, `labDeployJobReader` (1.31.6).

## 8. Sources

1. Cloud Run REST, jobs.run: https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs/run
2. Execute jobs: https://docs.cloud.google.com/run/docs/execute/jobs
3. Cloud Run IAM roles: https://docs.cloud.google.com/run/docs/reference/iam/roles and permissions https://docs.cloud.google.com/run/docs/reference/iam/permissions
4. Executions and tasks: https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions and https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions.tasks
5. Task timeout: https://docs.cloud.google.com/run/docs/configuring/task-timeout; retries https://docs.cloud.google.com/run/docs/configuring/max-retries
6. Container contract: https://docs.cloud.google.com/run/docs/container-contract; memory https://docs.cloud.google.com/run/docs/configuring/jobs/memory-limits
7. Quotas: https://docs.cloud.google.com/run/quotas
8. Direct VPC egress: https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc; networking best practices https://docs.cloud.google.com/run/docs/configuring/networking-best-practices
9. Sandboxes: https://docs.cloud.google.com/run/docs/code-execution
10. Cloud Storage volume mounts for jobs: https://docs.cloud.google.com/run/docs/configuring/jobs/cloud-storage-volume-mounts; FUSE overview https://docs.cloud.google.com/storage/docs/cloud-storage-fuse/overview
11. Pricing: https://cloud.google.com/run/pricing; billing settings https://docs.cloud.google.com/run/docs/configuring/billing-settings
12. Jobs resource: https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs
13. Release notes: https://docs.cloud.google.com/run/docs/release-notes; spend caps https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps
14. Cloud Scheduler pricing: https://cloud.google.com/scheduler/pricing; HTTP target auth https://docs.cloud.google.com/scheduler/docs/http-target-auth
15. IAP programmatic access: https://docs.cloud.google.com/iap/docs/authentication-howto; IAP on Cloud Run https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run
16. Ingress: https://docs.cloud.google.com/run/docs/securing/ingress
17. Docker multi-stage builds: https://docs.docker.com/build/building/multi-stage/
18. OpenHands with Vertex AI: https://docs.openhands.dev/openhands/usage/llms/google-llms.md; LiteLLM Vertex https://docs.litellm.ai/docs/providers/vertex
19. GitHub App tokens: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app; issues API (`author_association`) https://docs.github.com/en/rest/issues/issues
