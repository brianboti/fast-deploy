# Delivery service: the runs on Google Cloud

The record of every run of the delivery service and the infrastructure layer on a real project, in order: the spike
of step 0 (sections 1 to 7), the machine session (8), the end-to-end run (9), the first run from the image on Google
Cloud (10), the first cluster (11), several clusters with a front door (12), and Cloud Run services and jobs (13).
Each run says what held, what it found and what was fixed, and ends with everything deleted. Sections 1 to 6 are the
spike's procedure as it was written before step 6 was built:

The four facts of [DELIVERY-SPEC.md](DELIVERY-SPEC.md) section 13 that no Google page confirms, proven by hand on a real
project before step 6 is built. About 30 minutes, one small machine for under an hour (a few cents), everything deleted
at the end. Run it from a laptop with the gcloud CLI, `curl` and `jq`, signed in as a person who may administer the
project. Record each result in the table at the end; step 6 is built on what it says.

What it proves, in the order the commands prove it:

| # | Fact | Fallback if it fails |
|---|---|---|
| 1 | `gcloud compute ssh --tunnel-through-iap --impersonate-service-account … --command "bash -s"` forwards standard input, without a prompt | `gcloud compute start-iap-tunnel` to a local port and plain `ssh` with an OS Login key |
| 2 | The service account's OS Login user (`sa_…`) with `osAdminLogin` gets sudo on Container-Optimized OS | None needed: without it the design must change; stop and report |
| 3 | `base64` is on the host and bash has `/dev/tcp` | Run the health check from a pinned helper image with `--network host` |
| 4 | A container with `--restart=always` comes back after `cloudinfra` stops and starts the machine | Start `app` again after every `cloudinfra` start |

It also checks, on the way, that a machine without a public address pulls from Artifact Registry through Private Google
Access, and that `app-delivery plan` (steps 1 to 5) accepts a real `cloudinfra` answer.

## 1. Names used below

Set these once in the shell you will use for everything. `INFRA` is where `cloudinfra` serves; `TEAM_TOKEN` is the
token of a `cloudinfra` principal with `can_provision` and `can_destroy` (the team that owns the request).

```bash
export PROJECT=your-infrastructure-project
```

```bash
export REGION=us-central1 ZONE=us-central1-a
```

```bash
export ME=you@your-company.com
```

```bash
export SA=appdelivery-dev@$PROJECT.iam.gserviceaccount.com
```

```bash
export INFRA=http://127.0.0.1:8100 TEAM_TOKEN=the-team-principals-token
```

## 2. The platform team's part, once

The delivery service account:

```bash
gcloud iam service-accounts create appdelivery-dev --project $PROJECT --display-name "appdelivery dev"
```

You (standing in for the host the service will run on) may act as it:

```bash
gcloud iam service-accounts add-iam-policy-binding $SA --project $PROJECT --member user:$ME --role roles/iam.serviceAccountTokenCreator
```

The project-level role every `cloudinfra` connect block names, so that gcloud can find a machine:

```bash
gcloud iam roles create appdeliveryInstanceViewer --project $PROJECT --title "appdelivery instance viewer" --permissions compute.projects.get,compute.instances.get
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role projects/$PROJECT/roles/appdeliveryInstanceViewer
```

The Container-Optimized OS profile, in `cloudinfra`'s configuration under `providers.gcp.profiles`, then restart
`cloudinfra`:

```yaml
      container-host:
        image: projects/cos-cloud/global/images/family/cos-133-lts
        description: Container-Optimized OS 133 LTS for appdelivery
        accelerator: none
```

Check that the family exists before restarting:

```bash
gcloud compute images describe-from-family cos-133-lts --project cos-cloud --format "value(name,status)"
```

## 3. A machine, made by `cloudinfra` the way a team would

A request with its own network (IAP SSH on), one Container-Optimized OS machine in group `web`, and the delivery
service account in its access list as `admin`. Save it as `spike-request.json`:

```json
{"provider": "gcp", "region": "us-central1", "name": "spike",
 "network": {"iap_ssh": true},
 "compute": {"group": "web", "count": 1, "cpu": 2, "memory_gb": 4, "profile": "container-host"},
 "lifetime": {"hours": 4, "on_expiry": "destroy"},
 "access": [{"member": "serviceAccount:appdelivery-dev@your-infrastructure-project.iam.gserviceaccount.com", "level": "admin"}]}
```

Replace the project in the member before sending. The `lifetime` is a safety net: if the spike is abandoned, the
sweeper destroys everything after four hours.

```bash
curl -s -X POST "$INFRA/infrastructure?wait_seconds=45" -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" --data @spike-request.json | tee spike-created.json | jq '{request_id, status}'
```

```bash
export REQUEST_ID=$(jq -r .request_id spike-created.json)
```

Ask again until `status` is `READY` and the access entry is `GRANTED` (a few minutes):

```bash
curl -s "$INFRA/infrastructure/$REQUEST_ID" -H "Authorization: Bearer $TEAM_TOKEN" | jq '{status, access: [.access[] | {member, level, status}], machine: .resources.compute[0] | {name, zone, os_image, status}}'
```

Expect the machine `spike-web-1` with an `os_image` starting with `cos-`. If the access entry is `FAILED`, its `error`
says which IAM call `cloudinfra`'s own identity was refused; that is a `cloudinfra` setup problem, not a spike result.

## 4. The checks

Every command connects as the delivery service account, never as you.

**Facts 1 and 2: standard input reaches the remote shell, and sudo works.**

```bash
printf 'id -un\nsudo -n true && echo SUDO_OK\n' | gcloud compute ssh spike-web-1 --zone $ZONE --project $PROJECT --tunnel-through-iap --impersonate-service-account $SA --strict-host-key-checking=no --quiet --command "bash -s"
```

Pass: a user name starting with `sa_`, then `SUDO_OK`, and no prompt. The first run may print that it created an SSH
key and that it is impersonating; those lines are expected.

**Fact 3: the tools the script relies on.**

```bash
printf 'echo hello | base64\ncommand -v curl || echo NO_CURL\nexec 3<>/dev/tcp/127.0.0.1/22 && echo DEVTCP_OK\n' | gcloud compute ssh spike-web-1 --zone $ZONE --project $PROJECT --tunnel-through-iap --impersonate-service-account $SA --quiet --command "bash -s"
```

Pass: `aGVsbG8K` and `DEVTCP_OK`. Whether curl is there is recorded but does not matter.

**On the way: a pull through Private Google Access, a container, and a health check like the script's.** Google's
public sample image on Artifact Registry needs no login; the machine has no public address.

```bash
printf 'sudo docker run -d --name app --restart=always -p 8080:8080 us-docker.pkg.dev/google-samples/containers/gke/hello-app:1.0\nsleep 3\ncurl -s -o /dev/null -w "HTTP %%{http_code}\\n" http://127.0.0.1:8080/\n' | gcloud compute ssh spike-web-1 --zone $ZONE --project $PROJECT --tunnel-through-iap --impersonate-service-account $SA --quiet --command "bash -s"
```

Pass: a container id, then `HTTP 200`. (The first version of this check used bash `/dev/tcp`, which Container-Optimized
OS's bash does not have; see the results.)

**Fact 4: the container survives a stop and start by `cloudinfra`.**

```bash
curl -s -X POST "$INFRA/infrastructure/$REQUEST_ID/actions" -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" -d '{"action": "stop", "wait_seconds": 45}' | jq .status
```

Wait for `STOPPED` with the status command of section 3, then:

```bash
curl -s -X POST "$INFRA/infrastructure/$REQUEST_ID/actions" -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" -d '{"action": "start", "wait_seconds": 45}' | jq .status
```

Once it is `READY` again:

```bash
printf 'sudo docker ps --format "{{.Names}} {{.Status}}"\n' | gcloud compute ssh spike-web-1 --zone $ZONE --project $PROJECT --tunnel-through-iap --impersonate-service-account $SA --quiet --command "bash -s"
```

Pass: `app Up …` with a recent time.

## 5. Steps 1 to 5 against the real answer

Copy `appdelivery.example.yaml` to `appdelivery.spike.yaml`, set `gcp.project_id`, the `dev` environment's
`service_account` to `$SA` and its `allowed_registries` to `[us-docker.pkg.dev/google-samples]`, and fill in one
principal (`shop-team` with `infra_owner` set to the name of the `cloudinfra` principal whose token is `TEAM_TOKEN`).
Then a release file `spike-release.yaml`, with the digest of the sample image:

```bash
gcloud artifacts docker images describe us-docker.pkg.dev/google-samples/containers/gke/hello-app:1.0 --format "value(image_summary.digest)"
```

```yaml
environment: dev
request_id: PASTE_REQUEST_ID
target: {group: web}
image: us-docker.pkg.dev/google-samples/containers/gke/hello-app@sha256:PASTE_DIGEST_WITHOUT_PREFIX
port: 8080
```

```bash
APPDELIVERY_DEV_INFRA_TOKEN=$TEAM_TOKEN app-delivery plan spike-release.yaml --config appdelivery.spike.yaml --as shop-team
```

Pass: `"status": "PLANNED"` with `spike-web-1`, and a warning that port 8080 is open only inside the network. (In
production the environment's token is the delivery service's own read-only principal; the team's token is used here
only because it can read the request too.)

## 6. Clean up

```bash
curl -s -X DELETE "$INFRA/infrastructure/$REQUEST_ID?wait_seconds=45" -H "Authorization: Bearer $TEAM_TOKEN" | jq .status
```

Ask with the status command until it is `DESTROYED`. Keep the service account, the custom role and the profile: step 10
uses them. Delete `spike-created.json`; it names the machine's address.

## 7. Results

Run on 2026-10-29 between 16:13 and 16:30 UTC by Claude for the operator, in a sandbox project (not the company's), with
`cloudinfra` 1.37.0 on the operator's laptop and the gcloud CLI 539.0.0. Everything was destroyed or deleted afterwards:
the request through `cloudinfra` (DESTROYED; no instance, network or firewall rule left), then the service account, the
custom role and its project binding. The Compute, IAP, OS Login and IAM Credentials APIs were left enabled.

| # | Fact | Result | Notes |
|---|---|---|---|
| 1 | stdin through `gcloud compute ssh` as the service account | **Pass** | Commands sent on stdin ran; no prompt. The first attempts failed with IAP `4033: 'not authorized'` for about 4 to 6 minutes after `cloudinfra` granted the tunnel role (the policy was already correct when read); one attempt also hit a 120-second token refresh timeout. |
| 2 | sudo for the `sa_…` user | **Pass** | User `sa_109096497593721885629`; `sudo -n true` succeeded. |
| 3 | `base64` and bash `/dev/tcp` (curl: present or not) | **Changed** | `base64` works. **Bash has no `/dev/tcp`** on COS 133 (`No such file or directory`), but `/usr/bin/curl` and `/usr/bin/jq` are there. The health check uses curl. Bash 5.3.3, COS 133, build 19999.44.170. |
| – | pull from Artifact Registry without a public address | **Pass** | Google's public `hello-app:1.0` pulled through Private Google Access; the container answered `HTTP 200` to curl. A `docker login -u oauth2accesstoken --password-stdin` with the service account's token into a root-only directory made by `mktemp -d /run/appdelivery-XXXXXX` succeeded and was removed. `/root` is read-only; `/run` and `/tmp` are tmpfs; `/home` is ext4. |
| 4 | `--restart=always` after `cloudinfra` stop and start | **Pass** | Stopped and started through `cloudinfra`; after the boot the container was `Up` again and answered `HTTP 200`. SSH refused with IAP `4003: 'failed to connect to backend'` for about 30 seconds after `cloudinfra` reported READY. |
| – | `app-delivery plan` on the real answer | **Pass** | With the delivery service's own read-only principal (`scope: all`): `PLANNED`, the one machine with its zone and numeric id, and the warning that port 8080 is open only inside the network. |

What changes for step 6:

- The health check runs `curl` on the machine, not bash `/dev/tcp`.
- The machine session retries IAP `4033` for up to 10 minutes after a fresh grant, and `4003` for up to 2 minutes after a
  start, with the reason in the release's progress; any other failure is not retried.

## 8. Step 6 on a real machine

Run on 2026-10-29 between 16:50 and 17:02 UTC by Claude for the operator, in the same sandbox project, with the step 6
code: `MachineSession` and `host/release.sh` as committed, the token issued to the delivery service account through the
IAM Credentials API, and `cloudinfra` 1.37.0 making one `container-host` machine. Google's public `hello-app` 1.0 and
2.0, by digest. Everything was destroyed or deleted afterwards, as in section 7.

| Step | Result |
|---|---|
| status, right after the grant | `status`, empty. IAP refused twice with 4033; the session waited 30 seconds each time and got through on the third attempt (73 seconds). |
| deploy 1.0 (registry login with the token) | `healthy`, 6 seconds |
| deploy 2.0 | `healthy`, `previous` = 1.0, 16 seconds |
| rollback | `rolled_back` to 1.0 and its release id, 5 seconds |
| deploy 2.0 broken (env `PORT=9999`, so nothing answers on 8080) | `unhealthy`, `restored=yes` with 1.0, and the container's log line `Server listening on port 9999`, which also shows the env file reached the container; 38 seconds (30 of them the health timeout) |
| status | 1.0 running, no previous |
| deploy 2.0 again, then `cloudinfra` stop and start | `app` (2.0) `restart=always` running, `app-previous` (1.0) `restart=no` stopped; after the reboot only 2.0 came back and answered `Version: 2.0.0`. Each container held its own `GREETING`. No `appdelivery` file was left in `/run`. |
| remove, status | `removed`; then nothing running and no previous |

One `cloudinfra` finding, not a delivery one: the first grant failed with HTTP 429 from Resource Manager ("Read requests
per minute" for the consumer project gcloud's own sign-in bills to, since the operator's laptop credentials name no quota
project). `manage_infrastructure_access` granted again a minute later. Running `cloudinfra` on a laptop needs a quota
project (`gcloud auth application-default set-quota-project` or `billing/quota_project`), and its grant could retry 429s.

## 9. Step 10, the end-to-end run

Run on 2026-10-29 between 17:57 and 18:08 UTC by Claude for the operator, in the same sandbox project. `cloudinfra` and
the delivery service ran from the repository on the operator's laptop, the delivery service with its own read-only
`cloudinfra` principal, an environment that requires approval, a team principal and an on-call principal, and every call
over its own HTTP interface (REST, and MCP for `plan_release` and `rollback_release`). `cloudinfra` made a request with
two `container-host` machines and a generated secret, and granted the delivery service account on all three. The
releases bound one literal and that secret. Google's public `hello-app` 1.0 and 2.0, by digest.

The delivery service ran from the repository, not from its image: handing the operator's own Google token to a
container was refused by the session's safety rules, and rightly. The image was built and served in step 9; on Google
Cloud it signs in through the metadata server, which this run could not stand in for.

| Step | Result |
|---|---|
| `plan_release` over MCP | `PLANNED`, both machines, the bindings' sources, approval required, the port warning |
| deploy 1.0 | `AWAITING_APPROVAL`; approval by the requester refused (403); approved by the on-call principal; `RUNNING` on both machines |
| deploy 2.0 | `RUNNING`, `previous` 1.0, which became `SUPERSEDED`; `running?live=true`: both machines run 2.0 |
| deploy 2.0 with `PORT=9999` | `FAILED` with `ROLLOUT_FAILED`: the first machine `UNHEALTHY` and restored to 2.0 (its log: `Server listening on port 9999`), the second `SKIPPED`; 2.0 still `RUNNING` |
| **Defect found** | the restore had started the first machine's previous container, so it kept nothing older, while 2.0 still said `can_roll_back`. Asking the machines confirmed it: the first had no previous container, the second still had 1.0. A rollback would have stopped half way. |
| fix, then the broken deploy again | `FAILED` as before, and now 2.0's `can_roll_back` became false; `rollback_release` on 2.0 was refused up front with `INVALID_STATE` |
| deploy 1.0 again, then roll it back | `RUNNING` with `previous` 2.0; the rollback brought 2.0 back on both machines, which `running?live=true` confirmed |
| remove 2.0 | `REMOVED`; nothing runs on the group |
| the secret | the generated value was read only by the service account, on its way to the machines; it appeared in neither the delivery service's database or log nor `cloudinfra`'s |
| audit | every call recorded, the five refused self-approvals and the refused rollback among them |

Also fixed: the warning that a port is open only inside the network was repeated once per machine.

Everything was destroyed or deleted afterwards: the request through `cloudinfra` (`DESTROYED`; no instance, network,
firewall rule or secret left), then the service account, the custom role and its binding. Compute, IAP, OS Login, IAM
Credentials and Secret Manager APIs stay enabled.

## 10. The first run on Google Cloud

Run on 2026-10-29 between 18:20 and 19:00 UTC by Claude for the operator, in the same sandbox project. This is the path
section 9 could not take: the delivery service from its own image, signed in through the metadata server.

- **Images.** Cloud Build built `Dockerfile.appdelivery` and a `cloudinfra`-only image in the project, as a build service
  account allowed only to read the uploaded source, write to one Artifact Registry repository and write logs (the
  project's default Compute Engine account has no rights in a new project). The checksums held.
- **Platform machine.** One Container-Optimized OS machine with its own service account (Compute Admin, IAP Admin,
  Secret Manager Admin and Browser for `cloudinfra`; Token Creator on the environment's service account for the delivery
  service; reader on the repository). Both services ran as containers on the host network. Its boot configuration
  carried token hashes only; the token the delivery service uses to read `cloudinfra` was made on the machine at boot.
  The operator's laptop reached the two services through IAP tunnels.

| Step | Result |
|---|---|
| `cloudinfra` makes two `container-host` machines and a secret, as its own service account | `READY`, the delivery service account `GRANTED` on all three; no rate limit this time (a service account's calls count against its own project) |
| `plan_release` (MCP) | `PLANNED`, both machines |
| deploy 1.0, approved by the on-call principal | `RUNNING` on both machines after 52 seconds: the image signed in through the metadata server, took the environment's token, read the secret, and ran `gcloud compute ssh` through IAP as the named user |
| deploy 2.0 | `RUNNING`; `running?live=true` matches on both machines |
| roll back 2.0 | `ROLLED_BACK`; 1.0 `RUNNING` again |
| remove 1.0 | `REMOVED`; nothing runs |
| the secret | absent from every file both services wrote (databases, logs, gcloud's own files) and from the container's output |

Found on the way:

- **Container-Optimized OS closes every port but SSH on the host.** A container on the host network (as both services
  were) needs an `iptables -A INPUT -p tcp --dport PORT -j ACCEPT`; a container started with `-p`, as the host script
  starts applications, goes through Docker's own rules and needs none.
- **gcloud wrote a log file for every SSH call** into the service's data volume. The image now sets
  `CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1`.
- **Cloud Build's docker step has no BuildKit**, so it does not set `TARGETARCH`: the build passes
  `--build-arg TARGETARCH=amd64`, and the Dockerfile says so.

Everything was destroyed or deleted afterwards: the target machines, network and secret through `cloudinfra`; then the
platform machine, its firewall rule, the image repository, Cloud Build's bucket, the three service accounts, the custom
role and every binding. Artifact Registry and Cloud Build APIs stay enabled with the others.

## 11. Phase 2 step 3, the first run on a cluster

Run on 2026-10-29 between 19:41 and 20:35 UTC by Claude for the operator, in the same sandbox project. `cloudinfra` and
the delivery service ran from the repository on the operator's laptop, as in section 9, with the same principals and an
environment that requires approval. `cloudinfra` made a request with a one-node GKE cluster and a generated secret, and
named the environment's service account `admin` in `access`; that account held `container.clusterViewer` on the
project and nothing else. The releases were Google's `hello-app` 1.0 and 2.0 by digest, two replicas, binding one literal
and the secret.

| Step | Result |
|---|---|
| `cloudinfra` makes the cluster | `READY` after about 7 minutes, with the namespace `shop` and one RoleBinding: the service account's email as a `User`, bound to the ClusterRole `edit`, in that namespace only |
| `plan_release` (MCP) | `PLANNED`: target `cluster`, namespace `shop`, workload `web`, 2 replicas |
| deploy 1.0, approved by the on-call principal | `RUNNING`: Deployment 2/2 ready, a ClusterIP Service, the release's own Secret `web-env-<release>` |
| deploy 2.0 | `RUNNING`, `previous` 1.0, `can_roll_back`; the Service kept its address; two release Secrets kept |
| deploy 2.0 with `PORT=9999` | `FAILED` with `ROLLOUT_FAILED`: Kubernetes gave up (`ReplicaSet … has timed out progressing`), the log line `Server listening on port 9999` reported; the old replicas served throughout (maxUnavailable 0); 2.0 put back and still `RUNNING` with `can_roll_back` |
| roll back 2.0 | `ROLLED_BACK`; 1.0 `RUNNING` on 2 new replicas; 2.0's Secret deleted |
| remove 1.0 | `REMOVED`: no Deployment, Service, Secret or pod left; the namespace and its RoleBinding stay, as they belong to `cloudinfra` |
| the secret | each release Secret held the Secret Manager value; the value appeared in neither service's database or log |

Found on the way, and fixed:

- **GKE takes the network and subnetwork by name only** (`cloudinfra`). The adapter sent the network's self link, and
  GKE refused it as a malformed value. Fixed in the adapter, and the fake now refuses a path.
- **gcloud can hand back a token with only minutes left** (`cloudinfra` and the delivery service). Both kept a token
  from `gcloud auth print-access-token` for 30 minutes; one expired while the cluster was being made, failing the request.
  Both now keep it 5 minutes, and on a 401 drop it and try once more.
- **The environment's token needs the `userinfo.email` scope** (delivery service). With `cloud-platform` alone GKE sees
  a service account as its number (`User "1043…"`), so a RoleBinding that names its email never matches and every call is
  refused with 403. A token from gcloud carries the scope, which is why a call by hand worked. `cloudinfra` is not
  affected: it reaches the cluster through its IAM rights, not RBAC.
- **A timed-out rollout's condition outlives the restore** (delivery service). Kubernetes keeps `ProgressDeadlineExceeded`
  on the Deployment until it has seen the next change, so the restore read it as its own failure and 2.0 lost its
  rollback, while the cluster ran 2.0 on 2/2 replicas. The condition now counts only for the generation being waited for,
  as `kubectl rollout status` does.

Found and still open:

- **A cluster whose creation fails after Google accepted it is left behind** (`cloudinfra`). When the expired token
  failed the request while it waited for GKE, the cluster had no provider id in `cloudinfra`'s state yet, so destroying
  the request deleted the secret and skipped the cluster, which kept running. It was deleted by hand. The same can happen
  to any resource whose create returns a long-running operation; the restart-recovery message already says so.

Everything was destroyed or deleted afterwards: the request through `cloudinfra` (`DESTROYED`: the cluster and the
secret), the cluster left by the failed attempt by hand, then the environment's service account and its
`container.clusterViewer` binding. No cluster, machine, disk, secret or GKE firewall rule is left. The Kubernetes Engine
API stays enabled with the others.

## 12. Phase 3, the run on Google Cloud

Run on 2026-10-29 between 22:28 and 23:55 UTC by Claude for the operator, in the same sandbox project, from branch
`phase3-expose` (PRs #206 to #211). As in section 10, Cloud Build built both services' images, and a Container-Optimized
OS platform machine ran them as containers, signed in through the metadata server as its own service account. That
account held exactly the roles the architecture document lists for node accounts, Project IAM Admin among them limited
by the condition `modifiedGrantsByRole … hasOnly(['roles/container.defaultNodeServiceAccount'])`. Cloud Build also
copied Google's `hello-app` 1.0 and 2.0 into a private Artifact Registry repository, `apps`, which the operator's
`providers.gcp.registries` listed. The front door had no hostnames: the operator chose plain HTTP for this run, since
HTTPS needs a domain whose DNS they control.

`cloudinfra` made one request with two clusters: `blue` (one node, the `apps` registry, a front door) and `green` (one
node, the `apps` registry), a generated secret, and the environment's service account at `admin`.

| Step | Result |
|---|---|
| the two clusters | `READY`: `shop-blue` and `shop-green`, each with the namespace `shop` and its own node account (`node-<request>-blue`, `node-<request>-green`, as GKE's `nodeConfig.serviceAccount` confirmed); `blue` with Gateway API on and its front door on a reserved address |
| `cluster: true` with two clusters | refused, `INVALID_RELEASE`: "The request has 2 clusters (blue, green); name one" |
| `expose` on `green` | refused, `UNSUPPORTED_TARGET`: no front door |
| the guard, in `shop` on `blue` | a Service of type `LoadBalancer`, an Ingress and a Gateway each refused (422) by `cloudinfra-no-public-entry`, even for the project's owner, since the policy binds by namespace; a ClusterIP Service and an HTTPRoute allowed (dry runs) |
| deploy 1.0 on `blue`, exposed at `/` | `RUNNING` from the private repository; the HTTPRoute `Accepted` and `ResolvedRefs`; Google's load balancer answered `http://<address>/` with "Version: 1.0.0" from the internet 347 seconds later |
| deploy 2.0, health path `/healthz` | `RUNNING`; a HealthCheckPolicy for `/healthz`, written as the environment's account through the role aggregated into `edit`; the address answered "Version: 2.0.0" within a second |
| deploy 1.0 on `green`, not exposed | `RUNNING` |
| 2.0 with `PORT=9999` | `FAILED` after 54 seconds; the address kept answering 2.0, and 2.0 kept its rollback |
| roll back 2.0 | `ROLLED_BACK`; the address answered 1.0 twenty seconds later |
| remove both | `REMOVED`; the route gone from the door; nothing runs |
| the secret | in no file and no log line of either service |
| destroy the request | the Gateway deleted at 23:32:59; `cloudinfra` waited 46 seconds until GKE had deleted the forwarding rule on the address; the cluster deleted at 23:33:45, then the address and the node accounts. No forwarding rule, proxy, URL map, backend service, health check, network endpoint group, GKE firewall rule, address, node account, project binding or repository grant was left |

Found on the way, and fixed:

- **GKE does not know a new node account as soon as IAM does.** The first request's second cluster was refused with
  "failed to check status for node-…; Verify if principal exists", after IAM had already taken the account's grants.
  The cluster create is now tried again for up to a minute (commit 74fee98). The failed request's cleanup then deleted
  everything it had made, the first cluster with its front door included, which this run counts as a check of #205 and
  of the front door's delete order on a real project.

Not checked here: a Google-managed certificate on a real domain (HTTPS), which needs a short run with a domain the
operator controls. Acting as the environment's account from the laptop was refused by the session's safety rules (a
grant of Token Creator to the operator), so the guard was checked as the owner; the delivery service itself wrote the
route and the health check as the environment's account.

Everything was destroyed or deleted afterwards: the request through `cloudinfra`; then the platform machine, its
firewall rule, both repositories, the build bucket, the three service accounts and every binding, the conditional one
included. Kubernetes Engine, Artifact Registry and Cloud Build APIs stay enabled with the others.

## 13. Phase 4, Cloud Run on Google Cloud

Run on 2026-10-30 between 04:20 and 05:50 UTC by Claude for the operator, in the same sandbox project, from branch
`phase4-job-delivery` (PRs #213 to #216). As in sections 10 and 12, both services ran from their own images on a platform
machine, `cloudinfra` with the roles the architecture document lists for services and jobs (`run.admin`,
`iam.serviceAccountAdmin` and `serviceAccountUser`, `secretmanager.admin`, `cloudscheduler.admin`, and readers), and with
`allow_public_services` on. The environment's account had no project role at all. Cloud Build copied `hello-app` 1.0 and
2.0, Google's sample job and `busybox` into a private repository.

`cloudinfra` made one request with two services, `api` (private) and `web` (public), a job `nightly` with two tasks on
the schedule `*/2 * * * *`, a generated secret, and the environment's account at `admin`.

| Step | Result |
|---|---|
| the request | `READY`: both services and the job each with its own runtime account; the job's schedule enabled, running as its own account |
| `service: true` with two services | refused with their names |
| `api` with no sign-in, then signed in | 403, then 200 |
| deploy 1.0, then 2.0, on `api` | each a revision named after its release, proved under a `candidate` tag, then given all traffic; the address answered 1.0.0, then 2.0.0 |
| a release of `busybox` (exits at once) | `FAILED` with Cloud Run's own reason ("failed the configured startup probe checks"); 2.0 kept all traffic and its rollback |
| roll back 2.0 | `ROLLED_BACK`; the address answered 1.0.0 |
| deploy 1.0 on `web` | answered with no sign-in at all |
| the schedule | ran the job by itself every two minutes, two tasks succeeding each time |
| the job: deploy, run | the release became the job's container; `run_release` → `SUCCEEDED`, 2 of 2 tasks, with the log page |
| a release with `FAIL_RATE=1`, run | `FAILED`, 0 of 2 tasks; running the release the job no longer held was refused (`INVALID_STATE`) |
| roll back the job, run | the previous container again, its secret pinned anew; `SUCCEEDED` |
| remove both services | the stand-in page again |
| the secret | in no file and no log line of either service |
| destroy | both services, the job, the schedule, every runtime and schedule account, and the secret gone |

Found on the way, and fixed (each with a test that fails without the fix):

- **Cloud Run checks that the deployer can read the image**, not only the service's account: every release was refused
  with `artifactregistry.repositories.downloadArtifacts` until the environment's account could read the repository.
  An operator's one-time grant (section 9); the refusal now says so.
- **A revision with no traffic and no tag is retired at once**, reported Ready without ever starting, so a container
  that exits looked healthy. The new revision now gets a `candidate` tag at 0%, as Google's no-traffic deploy does, and
  its readiness is read from the revision itself.
- **Right after a change, a service may report the state before it.** It is settled only once `observedGeneration` has
  reached `generation`.
- **A service whose newest revision failed reports a failed condition while it serves on.** `cloudinfra` called it
  FAILED, and since a FAILED resource is never read again, the delivery service refused the rollback for good. A service
  with a revision serving all traffic is READY.
- **A fresh grant reaches a service's revisions minutes after the service**: two minutes after the request a PATCH went
  through while reading the new revision was refused; five minutes after, both worked. A 403 that is not about the
  image is tried again for up to five minutes.
- **After moving traffic, the first reads may still report it where it was**, and the failed condition above stopped the
  wait at once, so a rollback that worked was called failed. The wait now looks only at the traffic.

Not checked here: removing a job's release (the script lost its id; the same whole-job write served deploy and rollback,
and the tests cover it), and a custom domain or HTTPS beyond Cloud Run's own address.

Everything was deleted afterwards: the requests through `cloudinfra`; then the platform machine, its firewall rule, both
repositories, the build bucket, the three service accounts and every binding. Cloud Run and Cloud Scheduler APIs stay
enabled with the others.

## 14. Builds only in the trusted project, on Google Cloud

Run on 2026-10-30 between 18:40 and 19:30 UTC by Claude for the operator, in the same sandbox project, from branch
`registry-kind` at `e5643a2` ([DELIVERY-SPEC.md](DELIVERY-SPEC.md) section 3;
[research/RESEARCH-2026-10-30-trusted-build.md](research/RESEARCH-2026-10-30-trusted-build.md)). Both services ran on
the operator's laptop against the project, `cloudinfra` with `access.registry_writers` naming one build account, the
delivery service as an environment account it impersonated. The operator granted the Cloud Build service agent its
Secret Manager role and authorised the Cloud Build GitHub App on `brianboti/fast-deploy`; everything else was done as
the documents say.

| Step | Result |
|---|---|
| `cloudinfra` makes a registry with immutable tags; the build account, the delivery account and the requester all at `admin` | `READY`, and the plan warned who pushes. On the repository's real policy only the build account is `artifactregistry.writer`; the requester and the delivery account are readers |
| the build account named outside `access.allowed_domains` | accepted, as a registry writer |
| an image built on the laptop and pushed by hand into the same repository, as the project's Owner | the push went through (a project Owner is above any repository policy); it carries no provenance |
| plan and deploy of that digest | `NOT_BUILT_HERE`, HTTP 409, before the infrastructure layer was read |
| a build of a source whose repository was not linked yet | Cloud Build accepted it, held it `QUEUED` for a minute and ended it `INTERNAL_ERROR` with no detail and an empty log; the service recorded `BUILD_FAILED` |
| `build_image` of `aafd68b`, `Dockerfile.appdelivery`, `E2_MEDIUM` | Cloud Build ran exactly the composed build (the linked repository at that commit, the build account, Cloud Logging only, `VERIFIED`), `SUCCESS` in two minutes; the service recorded `SUCCEEDED` and the digest, by its follower |
| the image's provenance (`--show-provenance`) | two occurrences, SLSA v0.1 and v1, signed by keys in Google's `verified-builder` project. The v1 statement names the builder `https://cloudbuild.googleapis.com/GoogleHostedWorker`, the build as its `invocationId`, and the source as `git+https://github.com/brianboti/fast-deploy.git` at `gitCommit: aafd68b…`, though the build was started by the API, not a trigger |
| plan of the built digest | passed the gate; stopped only because the request has no machines (`NOT_FOUND` for the group), as expected here |

Found on the way:

- **The Cloud Build service agent needs `roles/secretmanager.admin`**, not `secretAccessor`, to create a connection:
  it creates the secret holding the GitHub token and sets its policy (`secretmanager.secrets.create`,
  `setIamPolicy`). The console offers this grant; with gcloud the operator makes it first. Corrected in
  [REQUIREMENTS.md](REQUIREMENTS.md), [SETUP.md](SETUP.md) and the research note. It can be removed again once the
  connection exists.
- **Cloud Build fails an unlinked source slowly and without a reason.** A check that the repository exists before the
  build is sent would answer at once; not built yet.
- **A project Owner can push into any repository**, so a registry's policy alone cannot keep outside images out. The
  release gate does: it is what refused the pushed image.
- **The provenance's source is the GitHub URI** for an API build from a connected repository, so `slsa-verifier
  --source-uri github.com/ORG/REPO` should apply (not run here). The v1 envelope is signed by a key named
  `google-hosted-worker`, not `provenanceSigner` as the documentation's example has it. The answer is kept as a test
  fixture, `tests/appdelivery/fixtures/cloudbuild-provenance.json`, for the provenance check still to be built.

Not checked here: a release that runs the built image on a target (phases 1 to 4 ran those paths), and a build of a
private repository.

Everything was deleted afterwards: the request and its registry with both images through `cloudinfra`; then the linked
repository, the connection and its secret, the two service accounts, and every binding this run made, the service
agent's Secret Manager role among them. The Cloud Build GitHub App stays installed on the GitHub account until the
operator removes it there. The Container Analysis API stays enabled with the others.

## 15. The platform's own images, built by Cloud Build

Run on 2026-10-30 between 19:30 and 19:45 UTC by Claude for the operator, in the same sandbox project, with
`cloudbuild.yaml` from the working copy of `registry-kind` (not yet pushed, so submitted from the working copy through
`.gcloudignore`, which uploaded only the two Dockerfiles, `requirements.lock` and the two packages). The build ran as a
`platform-build` account that could write logs, read its own source bucket and push to a `platform` repository with
immutable tags, and nothing else.

| Step | Result |
|---|---|
| the build | `SUCCESS` in two minutes, as `platform-build`; both images pushed by Cloud Build, tagged with the build's id |
| provenance | each image has SLSA v0.1 and v1 statements; `appdelivery/provenance.py` verified Google's `google-hosted-worker` signature on both and read `GoogleHostedWorker` and this build as builder and invocation |
| the images | `cloudinfra` imports its server and reports 1.37.1; `appdelivery` imports its server, has gcloud 591.0.0 and Cosign v3.1.3, and runs as uid 65532 |

Found: a build from uploaded source records the source as the uploaded archive (`gs://…tgz`), not a commit, so
`provenance.verify` would refuse it against a recorded commit. That is why the documented path builds the platform's
repository at a commit through its connection (docs/SETUP.md section 3), as the delivery service does for a team.

Everything was deleted afterwards: the repository with both images, the source bucket, the build account and its
binding, and the local copies.


## 16. Releases S1 and S2 of the security assessment, on Google Cloud

Run on 2026-11-02 between 16:45 and 21:30 UTC by Claude for the operator, in the same sandbox project, from branch
`real-run-s1-s2` (from `main` at `54c4ee2`, with S1 and S2 merged), with the setup of [SETUP.md](SETUP.md) done by hand
([research/RESEARCH-2026-10-30-security-assessment.md](research/RESEARCH-2026-10-30-security-assessment.md)). Both
services ran on the operator's laptop: `cloudinfra` as its own `cloudinfra` account, the delivery service as
`appdelivery-dev`, whose `pull-dev` account the machines pull with and whose `build-dev` account Cloud Build ran as.
The operator authorised and installed the Cloud Build GitHub App on `brianboti/fast-deploy`. The application released
is `examples/hello` at `437d6f9`, a standard-library server that reports its user, its effective capabilities and the
names of its variables. Every fix the run called for is listed in [CHANGES.md](CHANGES.md); the requests that failed
on the way are in the second table.

**Refused before any call to Google** (`allow_public_exposure` off): a machine with a public address, a network open to
`0.0.0.0/0` and a firewall rule for port 22, a cluster with `private_nodes: false`, and a Cloud Run service with
`ingress: all`.

| Step | Result |
|---|---|
| machine `m1-web-1` (S1 H4, S2) | no service account, no external address, Shielded VM, `serial-port-enable` false, deletion protection on. Subnet flow logs (5-minute interval), firewall rule logging and NAT logging on; port 22 open only to IAP's range |
| build of `437d6f9` (`./examples/hello/Dockerfile`, context `./examples/hello`) | `SUCCEEDED` in 90 seconds inside Google, as `build-dev`; the release's provenance check read the build, the commit and `git+https://github.com/brianboti/fast-deploy.git` and passed |
| release onto `m1` with no `containeranalysis.occurrences.viewer` for `appdelivery-dev` (missed in the setup; SETUP.md has it) | `PROVENANCE_FAILED` before any machine was touched, naming the role |
| release onto `m1` while `pull-dev` may not read `apps` (S1 H1) | `PULL_FAILED`, nothing changed on the machine. Docker's words, kept on the target since this run: `downloadArtifacts` denied on `apps`, an authenticated refusal, so the machine had pulled as `pull-dev` with the short-lived token |
| the same release once `pull-dev` is a reader | `RUNNING`, the machine `HEALTHY` |
| the container on the machine (S1 M4) | user `65532:65532`, `--cap-drop ALL` with the eight default capabilities added back, `no-new-privileges`, 1 GiB, one CPU, 512 processes, `json-file` logs of 3 × 50 MB, restart `always`. The application reports effective capabilities `0000000000000000` and sees `API_KEY` (a secret) by name. No docker login and no file of the service's are left in `/run` |
| the delivery account's OS Login keys (S1 M3) | one key, expiring 15 minutes after the session that registered it |
| cluster `k5-blue` (S2) | private nodes, Dataplane V2 (`ADVANCED_DATAPATH`), Binary Authorization `PROJECT_SINGLETON_POLICY_ENFORCE`, security posture `BASIC`, authorized networks set to the laptop's address, shielded nodes, Workload Identity, a node account of its own |
| the team's namespace (S2) | Pod Security `enforce=baseline`, `warn` and `audit` `restricted`; the admission policy `cloudinfra-no-public-entry` with `failurePolicy: Fail`. Dry runs: a `LoadBalancer` and a `NodePort` Service refused (422), a `ClusterIP` Service allowed, a privileged pod and a `hostNetwork` pod refused (403) |
| release of two replicas onto `k5` | `FAILED`: one node, and the second replica's 1 GiB found no room. The message said only "has timed out progressing"; since this run it also says "found no node: 0/1 nodes are available: 1 Insufficient memory" (seen live) |
| release of one replica onto `k5` | `RUNNING`. The pod: seccomp `RuntimeDefault`, `drop: [ALL]` with the eight added back, `allowPrivilegeEscalation: false`, no service account token, limits of one CPU and 1 GiB; the application reports uid 65532 and effective capabilities `0` |
| Binary Authorization on `k5` | the project's policy was Google's default, `ALWAYS_ALLOW`; writing the rule is the operator's (S3). Set to `ALWAYS_DENY` for a minute, a pod of the same image was refused by `imagepolicywebhook` (`VIOLATES_POLICY`); the policy was put back as it was |
| database and Cloud Run `d3` (S2) | `READY` in 21 minutes. MySQL with `cloudsql_iam_authentication` on, `local_infile` off, `skip_show_database` on, no public address, `ENCRYPTED_ONLY`, backups and binary logs on, deletion protection on. Its users are `app` and Google's `root@%`, whose password is a random value nobody keeps |
| a service release binding `DB_PASSWORD: {secret: db-password}`, which the service's block does not name | first refused for the wrong reason, "the request has no secret 'db-password'" (fixed, below); then refused for the right one, "d3-api's runtime account may not read secret db-password" |
| release onto the service `api` with `API_KEY` (named in its block) and `DB_HOST` | `RUNNING`, its revision with all traffic. The service: ingress `internal_and_load_balancer` (a call from the internet gets Cloud Run's 404), Binary Authorization `useDefault`, egress to private ranges through `d3-net`, its own runtime account, which reads `d3-api-key` and not the database's password |

What the run found, request by request. Each fix is in [CHANGES.md](CHANGES.md) with a test that fails without it.

| Request | What happened | Fix |
|---|---|---|
| `k1`, a cluster | GKE ended the create with an `INTERNAL` error after five minutes; the request cleaned up after itself | none; Google's, and the next cluster with the same settings went through |
| `d1`, a database | the first private-address instance took 27 minutes and the request failed at the 15-minute operation timeout. Its cleanup could not turn deletion protection off while the instance was being made (409), and a later destroy met Service Networking still holding the connection for about two minutes | the create waits up to 40 minutes, a cleanup waits for an instance still being made, and the connection's delete is tried again, then the peering is removed from the network's side |
| `k2`, a cluster | with private nodes and no `cluster_authorized_networks`, GKE turned authorized networks on with none listed; neither service could reach the control plane | refused before any call while the list is empty |
| `k3`, a cluster | one connect timeout while the operation was read failed the request | a read, or a call that never connected, is tried again |
| `k4`, a cluster | 403 on the admission policy: `container.clusterAdmin` gives no right inside the cluster, and no IAM permission covers admission policies | the documents say `roles/container.admin` |
| the node-role check | it could not read the service's own custom role without `iam.roles.get`, and the log redacted the role's URL whole | the permission is in the custom role of SETUP.md; paths made of names stay in the logs |
| `d2`, a database and a service | Cloud Run refused the service seconds after its runtime account was made (`actAs` denied, "or it may not exist"), and the finished database went with the request | the create is tried again while Cloud Run does not know the account (on `d3` it was accepted at once, so the retry was not exercised here) |
| a release onto `m1` refused its pull | the target said only "could not pull the image" | docker's last lines are kept on the target |
| two replicas onto `k5` | "has timed out progressing", with no word of the node that had no room | the scheduler's reason is added |
| a binding of `db-password` | refused: cloudinfra has named the database's secret `<name>-db-password-<6 hex>` since 1.37.0, and the delivery service still looked for `<name>-db-password`, so the binding DELIVERY-SPEC.md shows failed against any real request. The fakes still had the old name, which is why no test saw it | the request's own credentials are found by their purpose; the fakes use the real name |
| destroy of `m1` | `DESTROY_FAILED`: deleting the machine took over a minute, and Compute Engine's operation `wait`, which Google holds up to two minutes, timed out at the client's 60 seconds; as a POST that lost its answer it was not tried again | an operation's `wait` is a read, with 150 seconds; the second destroy went through |
| destroy of `d3` | everything went but the network: Cloud Run keeps a deleted service's Direct VPC egress address (`serverless-ipv4-cloudrun-…`) on the subnet, and Google says to wait 1-2 hours before deleting the subnet. Nobody may delete that address | none possible on our side; the error now says so and when to call destroy again (`retry_after_seconds`) |

Setup steps the documents had wrong, fixed in [SETUP.md](SETUP.md): the conditional `projectIamAdmin` grant with
`--condition` (gcloud splits its expression at the commas; it is now read from a file), `iam.roles.get`, and
`roles/container.admin`.
