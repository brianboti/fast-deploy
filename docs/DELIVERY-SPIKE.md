# Delivery service, step 0: the spike

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
