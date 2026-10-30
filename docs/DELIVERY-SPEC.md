# Application delivery service: specification (draft)

Draft of 2026-10-29. Nothing here is built yet. The decision it follows: deploying a container onto what the
infrastructure layer built is the job of a **small separate service**, a new package beside `cloudinfra`
([RESEARCH-2026-10-29-delivery-layer.md](RESEARCH-2026-10-29-delivery-layer.md)). It neither uses nor depends on
`labdeploy`: that package will not be used and is to be removed, so nothing here may rely on it, borrow its code, or
assume it exists.
The working name is `appdelivery` (CLI `app-delivery`); the name is not a decision.

## 1. What it is

The service takes **an image that already exists** and runs it on **resources a `cloudinfra` request already created**,
checks that it is healthy, and can put the previous version back. It is the middle band of three:

```
Application teams      code · CI · build · image in a registry               (their own tools)
        │  image@sha256:… + request_id + port + env bindings
        ▼
appdelivery            verify image · resolve bindings · run · health · roll back · record   (this spec)
        │  reads the request over cloudinfra's REST API; acts on the machines with the IAM grants the request gave it
        ▼
cloudinfra             machines · networks · databases · secrets · clusters, with access grants   (unchanged)
```

| It owns | It does not own |
|---|---|
| Running a given image on a given request's machines (later: clusters) | Building, testing or signing images |
| Turning bindings into environment values at run time | Creating, resizing or deleting infrastructure |
| The health check that decides a rollout, and the rollback | Firewall rules, load balancers, machine sizes |
| Which release runs where, who asked, and the one before it | Cloud Run services (not offered; section 4) |
| Approval of a release in an environment that asks for one | The application's own logs, metrics and alerts beyond the rollout |

### A correction to the research note

The research note put Cloud Run first among targets, on the grounds that existing code already did it. That reason is
gone (`labdeploy` is not used), and the target was wrong for this service anyway: `cloudinfra` has no Cloud Run kind, so a
Cloud Run target would not run on "our resources" at all. The targets are therefore **machines first, clusters second**;
Cloud Run is not planned (section 4).

## 2. Who trusts whom

Three identities, and a release needs all three to agree:

1. **The application team** calls `appdelivery` with its own bearer token. In `appdelivery`'s configuration the team's
   principal names the `cloudinfra` owner it speaks for (`infra_owner`). A team can deploy only onto requests whose
   `owner` is that name: team A can never deploy onto team B's machines.
2. **`appdelivery` in `cloudinfra`** is a read-only principal: `scope: all`, `can_provision: false`, `can_destroy:
   false`, `identity` set to its Google service account. It reads a request's resources, endpoints, access list and
   lifecycle, and nothing else. It never creates or deletes infrastructure.
3. **`appdelivery` in Google Cloud** is a service account per environment (dev, staging, prod are separate accounts,
   as Cloud Deploy and Google's enterprise blueprint both insist). Its power on a machine comes **only** from the team
   naming it in the request's access list:

   ```yaml
   access:
     - {member: "serviceAccount:appdelivery-prod@infra-project.iam.gserviceaccount.com", level: admin}
   ```

   `cloudinfra` then grants it `roles/compute.osAdminLogin` and the IAP tunnel on those machines and
   `secretAccessor` on the request's secrets, and nowhere else. `admin` is needed because running containers on the
   machine needs root; the grant is the team's explicit consent to that.

Before every action `appdelivery` checks, from `cloudinfra`'s answer: the request's `owner` is the caller's
`infra_owner`; its status is `READY`; its `access` list holds this environment's service account with status `GRANTED`
and level `admin`. Any "no" is a refusal with a code, before anything touches a machine.

## 3. The contract

### Release request

```yaml
environment: prod                     # one of the operator's environments; picks the identity and the policy
request_id: 3f6c…e2a1                 # the cloudinfra request that holds the target
target: {group: web}                  # a compute group of that request: every machine in it (later: {cluster: clu-…})
image: us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:9b2f…   # a digest is required; a tag is refused
port: 8080                            # the container's port, published on the same port of the machine
health: {path: /healthz, timeout_seconds: 60}
env:                                  # names are the application's; each value comes from exactly one source
  LOG_LEVEL:    {value: info}
  DB_HOST:      {endpoint: database_host}       # a field of cloudinfra's endpoints for this request
  DB_PORT:      {endpoint: database_port}
  DB_PASSWORD:  {secret: db-password}            # a secret of this request, read with appdelivery's own grant
  REDIS_AUTH:   {secret: cache-auth}
note: "fix checkout rounding"         # optional, recorded with the release
```

Rules, all checked before anything runs:

- No field for source, a repository, a Dockerfile, a build, a command, an entrypoint, arguments, volumes, privileges or
  host mounts. Unknown fields are refused (strict models, like `cloudinfra`). The image runs as its author built it.
- `image` must carry `@sha256:`; its registry must be in the operator's `allowed_registries`.
- `env` values are a literal, an endpoint of this request, or a secret of this request; never a secret of another
  request. A literal that looks like a credential (by the same key list `cloudinfra`'s redaction uses) is refused: put it
  in a secret.
- `port` must be one the request's network opens (`allowed_ports`), or the plan warns that only the machine itself can
  reach it.
- In an environment with `require_attestation`, the image must carry an attestation signed by one of the environment's
  `trusted_signers` (Cosign with a KMS key, or a keyless identity), checked by digest.

### Release response

```yaml
release_id: rel-5c1e09ab77d2
status: RUNNING                       # see section 5
environment: prod
request_id: 3f6c…e2a1
image: …@sha256:9b2f…
previous: rel-0a7d3c55e910            # what a rollback restores; null for the first release
targets:
  - {resource: cmp-1a2b3c4d5e6f, name: shop-web-1, status: HEALTHY, started_at: …}
  - {resource: cmp-6f5e4d3c2b1a, name: shop-web-2, status: HEALTHY, started_at: …}
env: [LOG_LEVEL, DB_HOST, DB_PORT, DB_PASSWORD, REDIS_AUTH]   # names only, never values
approved_by: null
error: null                           # {code, message, details} on failure, as in cloudinfra
```

## 4. Targets

### Phase 1: machines

**What the machine must be.** A machine of a `cloudinfra` request built from an operator profile on
Container-Optimized OS, which ships Docker and is Google's supported way to run containers on a VM now that the
container startup agent is gone (shut down 2026-07-31). The profile is `cloudinfra` configuration, no code change:

```yaml
providers:
  gcp:
    profiles:
      container-host:
        image: projects/cos-cloud/global/images/family/cos-133-lts
        description: Container-Optimized OS 133 LTS for appdelivery
        accelerator: none
```

A pinned LTS family, not `cos-stable`: an LTS milestone keeps about two years of support, and the operator moves it on
purpose. Container-Optimized OS supports OS Login and Shielded VM on every current LTS (117 to 133), ships Docker,
containerd and `cloud-init`, has no package manager and no gcloud CLI, and downloads updates weekly that take effect at
the next reboot (section 13).

A plain Ubuntu machine has no Docker; `appdelivery` refuses it with a message that names the profile, rather than
installing software on it.

**Where the machine must be.** The plan refuses a machine that `appdelivery` could not reach or that could not pull:

- **SSH.** IAP needs a firewall rule from `35.235.240.0/20` on port 22. A request's own network has it when `iap_ssh` is
  on (the default). A machine in the operator's default network is accepted only when the operator's configuration says
  that network has the rule (`default_network_iap: true`).
- **Pull.** A request's own subnet has Private Google Access, so Artifact Registry (`*-docker.pkg.dev`) is reachable
  without a public address. Any other registry needs the internet, which a machine without `public_ip` has only when
  its network has Cloud NAT (`network.nat`). So `allowed_registries` defaults to Artifact Registry hosts, and a registry
  outside them is refused for a machine with neither a public address nor NAT.
- **Time.** A target group with `autostop` or `max_run_hours` is one that `cloudinfra` will stop by design; the plan
  warns, since a quiet web service is exactly what idle stop looks for.

**How it reaches the machine.** Over SSH through the IAP tunnel as the environment's service account, through the
gcloud CLI on the host where `appdelivery` runs: `gcloud compute ssh NAME --zone ZONE --project PROJECT
--tunnel-through-iap --impersonate-service-account ENV_SA --command "sudo bash -s"`, the `connect.ssh` command
`cloudinfra` already returns plus impersonation. IAP's tunnel protocol is not documented for other clients, so gcloud
is the only supported way in. OS Login gives the service account a POSIX user named `sa_…`; its key is created per
session and expires with it. Docker on Container-Optimized OS needs root for an OS Login user, which is why the grant is
`admin` (`roles/compute.osAdminLogin`, sudo). The identity `appdelivery` itself runs as needs Token Creator on each
environment's service account, and each environment's service account needs the project-level role with
`compute.projects.get` and `compute.instances.get` that every `cloudinfra` connect block already names.

**What runs on the machine.** One fixed script, a file in `appdelivery`'s package, checked against its recorded hash and
sent on standard input to `sudo bash -s`. Its inputs are prepended as shell variable assignments whose values are
**base64 text only** (image reference, container port, health path, timeout, the environment file, the token), each
validated before encoding, so nothing a caller sends can become shell syntax and the script needs no JSON parser
(Container-Optimized OS has none). Then:

1. Make a temporary `DOCKER_CONFIG` directory (writing `/root/.docker` is not permitted on Container-Optimized OS), log
   in to the registry with `docker login -u oauth2accesstoken --password-stdin https://LOCATION-docker.pkg.dev` using a
   short-lived access token of the environment's service account (valid for 60 minutes; the machine has no service
   account of its own, so it cannot pull a private image by itself), pull `image@digest`, delete the directory.
2. Write the environment to a root-only file under `/run` (memory). Docker's `--env-file` takes one `NAME=value` per
   line with no quoting, so a value with a newline is refused at plan time (store a PEM file in a secret and bind it as
   base64, or wait for the later machine service account).
3. Rename the running container to `app-previous`, take away its restart policy and stop it (a stopped container with
   `--restart=always` comes back when Docker's daemon restarts, so at the next boot it would take the port); start the
   new one as `app` with `--restart=always`, `--env-file`, the port, a release label, and nothing else. A failed pull
   or login stops here, before anything running is touched.
4. Check `http://127.0.0.1:PORT/PATH` with `curl` until it answers 2xx or `timeout_seconds` passes. (The spike found
   that Container-Optimized OS 133 ships curl and that its bash has no `/dev/tcp`.)
5. Healthy: remove the env file, prune images no container uses (keeping `app`'s and `app-previous`'s). Unhealthy:
   remove `app`, make `app-previous` the current one again with its restart policy, report the container's last 50
   log lines.

The values handed to the container stay in Docker's container record on the machine's disk, readable by root on that
machine. That is inherent in environment variables and is stated in the response's notes.

**A group of machines** is released one machine at a time. The first unhealthy machine stops the rollout, and the
machines already done are rolled back. Releases on the same request are serialized.

**What the `cloudinfra` lifecycle does to it.** A stop by `cloudinfra` (asked, idle or expiry) stops the machine;
on start, Docker's restart policy starts `app` again with the values it was given. Destroying the request ends the
release: its targets show `GONE`.

### Phase 2: clusters

**Decided on 2026-10-29: access to one namespace through Kubernetes RBAC.** Checked on Google's pages first
([RBAC](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control),
[IAM](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/iam)): a RoleBinding may name a Google service
account's email as a `User`, and GKE authorizes a request when either RBAC or IAM allows it; `container.clusterViewer`
gives `container.clusters.get` (enough to fetch credentials) and no access to any Kubernetes object; IAM's
`container.developer` cannot be limited to one cluster, it reaches every cluster of the project; Google Groups for RBAC
needs a Workspace domain and a `gke-security-groups` group. So the grant lives in the cluster, not in IAM:

- **`cloudinfra`** creates one namespace per cluster, named after the request, as soon as the cluster runs, and treats a
  cluster as a resource that takes grants. A member at `use` gets a RoleBinding to the built-in ClusterRole `view` in
  that namespace, at `admin` one to `edit` (which covers Deployments, Services and Secrets, and no RBAC or namespace
  change). A `user:` or `serviceAccount:` member is bound as a `User` named by its email, a `group:` member as a `Group`
  (which needs Google Groups for RBAC on the operator's side). It talks to the cluster's own API with its Google token
  and the cluster's CA certificate, only inside the Google adapter. A member still needs the project-level
  `container.clusterViewer` the platform team grants once, as the connect block already says.
- **`appdelivery`** targets `{cluster: true, workload: NAME, replicas: N}`. In the namespace it keeps one Deployment and
  one ClusterIP Service per workload, and one Secret per release holding the environment (so a release's values never
  change under it). The Deployment rolls with `maxUnavailable: 0`, a readiness probe on the health path and a progress
  deadline of the health timeout; the release is healthy when every replica of the new generation is available.
  A failed rollout re-applies the previous release's Deployment, which still points at its own Secret, so on a cluster a
  failure does not take away the rollback (unlike a machine). Rollback re-applies the previous release; remove deletes
  the Deployment, the Service and the release Secrets. Secrets of releases older than the previous one are deleted.
- **Not in phase 2:** exposing a workload outside the cluster (an Ingress or a load balancer), private registries for
  the nodes, several clusters per request.

Steps, each its own pull request:

1. **`cloudinfra`: the namespace and RBAC grants.** `ClusterResource.namespace`, the namespace made after creation,
   clusters in `grantable_kinds`, grant and revoke as RoleBindings, the connect block naming the namespace. Tests
   against a fake Kubernetes API.
2. **`appdelivery`: the cluster target.** Built 2026-10-29. `Target` takes `{cluster: true, workload, replicas}` (keyed
   `cluster:<workload>`, so machines and a workload on one request are kept apart); the plan checks the cluster READY,
   its namespace, its address and CA, and the environment's account granted on it; `appdelivery/kube.py` reaches the
   cluster with the address and CA certificate `cloudinfra` reports and the environment's Google token, and applies the
   release's Secret, the Deployment (`maxUnavailable: 0`, readiness probe, `automountServiceAccountToken: false`, no
   command, volume or privilege) and a ClusterIP Service; a pod stuck in `ImagePullBackOff`, `CrashLoopBackOff` and the
   like fails the rollout at once, and its log comes back scrubbed. Tests against a fake Kubernetes API
   (`tests/appdelivery/kube_fakes.py`).
3. **A real run** on a GKE cluster in the sandbox project, then everything deleted. Done 2026-10-29
   (docs/DELIVERY-SPIKE.md section 11): deploy, a failed rollout put back with its rollback kept, rollback and remove
   all held. It found four defects, all fixed (GKE takes the network by name; gcloud tokens may be near expiry; the
   environment's token needs `userinfo.email` so GKE sees the account's email; a timed-out condition outlives the
   restore), and one still open in `cloudinfra`: a resource whose create fails after Google accepted it is not deleted
   with the request (fixed in #205).

### Phase 3: several clusters, private images, a front door

**Decided on 2026-10-29 by the operator:** the front door (a public address with HTTPS) is infrastructure, made by
`cloudinfra`; team namespaces may not make public load balancers themselves; a request may carry several clusters.
Checked on Google's and Kubernetes' pages first:

- GKE's Gateway API is Google's recommended way in, and GKE Ingress is in maintenance mode
  ([Ingress](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/ingress)). On a Standard cluster it is turned
  on with `networkConfig.gatewayApiConfig.channel: CHANNEL_STANDARD`, needs a VPC-native cluster (ours are) and the
  HTTP load balancing add-on ([deploying Gateways](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploying-gateways)).
  A Gateway in one namespace accepts routes from the namespaces it allows (`allowedRoutes`); a reserved address is given
  by `NamedAddress`; a Google-managed certificate by a pre-shared certificate or a Certificate Manager map
  ([securing Gateways](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway)).
- Kubernetes' Gateway API treats making a Gateway as privileged, and ships no rules that add its kinds to `edit`
  ([security model](https://gateway-api.sigs.k8s.io/concepts/security-model/)); routes need their own Role.
- `edit` lets a namespace make a Service of type `LoadBalancer`, which on GKE is a public network load balancer
  ([Service load balancers](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/service-load-balancer)). A
  ValidatingAdmissionPolicy, stable since Kubernetes 1.30 and built in, can refuse it
  ([admission policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)).
- Nodes pull images as the node pool's service account, which needs `roles/artifactregistry.reader`, grantable on one
  repository; Google recommends a dedicated node account with `roles/container.defaultNodeServiceAccount` instead of
  the Compute Engine default ([service accounts](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/service-accounts)),
  which in a new project has no rights at all. Private nodes reach Artifact Registry through Private Google Access;
  Docker Hub needs NAT or a copy in Artifact Registry.

The design:

- **Several clusters (`cloudinfra`).** `cluster` takes one cluster or a list, as `compute`, `queue` and `secret` do;
  each has a `name` (`cluster` when omitted, so `<request>-cluster` as today), required and unique in a list. Each
  cluster has its own namespace named after the request and its own grants.
- **A cluster target names its cluster (`appdelivery`).** `{cluster: NAME, workload, replicas}`; `cluster: true`
  still means the request's only cluster. Releases are keyed `cluster:<name>:<workload>`; a release stored before keeps
  its key.
- **Each cluster's own node account (`cloudinfra`).** A service account per cluster with
  `roles/container.defaultNodeServiceAccount`, set as the pool's `nodeConfig.serviceAccount`, deleted with the cluster.
  `cluster.registries` lists Artifact Registry repositories (`LOCATION-docker.pkg.dev/PROJECT/REPO`) the nodes may pull
  from; the account gets `roles/artifactregistry.reader` on each, and on nothing else.
- **No public load balancer from a team namespace (`cloudinfra`).** `cloudinfra` labels the namespaces it makes for
  requests, and installs in every cluster a ValidatingAdmissionPolicy and its binding that refuse, in labelled
  namespaces, a Service of type `LoadBalancer` and any Gateway. `edit` cannot change a namespace's labels, so a team
  cannot lift it.
- **The front door (`cloudinfra`).** `cluster.public: {hostnames: [...]}` turns on Gateway API, reserves a global
  address, makes a Google-managed certificate for the hostnames, and puts one Gateway of class
  `gke-l7-global-external-managed` in the platform's own namespace `cloudinfra-gateway`, listening on 443 (and on 80,
  redirecting) and accepting routes only from the request's namespace, matched by its name. Members at `admin` also get
  a Role to write HTTPRoutes and read Gateways in their namespace. The cluster reports the address, the hostnames and
  the certificate's state; the operator points DNS at the address.
- **Exposing a workload (`appdelivery`).** A cluster release may carry `expose: {hostname, path}`; the delivery service
  then keeps an HTTPRoute for the workload, attached to the cluster's Gateway, sending that hostname and path to the
  workload's Service. The plan refuses a hostname the cluster does not serve. Remove deletes the route.
- **Still not here:** an internal (VPC-only) front door, several Gateways per cluster, Docker Hub through NAT.

Steps, each its own pull request:

1. **`cloudinfra`: several clusters per request.** Built 2026-10-29 (#206).
2. **`appdelivery`: a cluster target names its cluster.** Built 2026-10-29. `InfraRequest.cluster(name)` finds a cluster
   by the name its block gave it (`<request>-<name>` in cloudinfra); `cluster: true` with several clusters is refused
   with their names, and with one it is named before the release is kept, so asking by `true` or by name is one target.
3. **`cloudinfra`: each cluster's own node account and the registries it may pull from.** Built 2026-10-29; the
   operator chose one account per cluster over one shared account, accepting the extra rights the service needs
   (docs/INFRASTRUCTURE-ARCHITECTURE.md, "A cluster's nodes").
4. **`cloudinfra`: no public load balancer from a team namespace.** Built 2026-10-29. An Ingress is refused too: on
   GKE it also makes a public load balancer.
5. **`cloudinfra`: the front door.** Built 2026-10-29. Without hostnames it serves plain HTTP on the address; hostnames
   must be in the operator's `public_domains`. The Gateway is deleted first, and the cluster waits for GKE's forwarding
   rules to go, so no load balancer outlives it.
6. **`appdelivery`: exposing a workload.** Built 2026-10-29. The route moves only once a release is healthy, so a
   failed release leaves it on the previous one; a release without `expose` takes the workload off the door, and a
   rollback puts the previous release's route back. GKE's Gateway load balancer checks `/` and does not read the
   readiness probe, so a health path other than `/` also gets a HealthCheckPolicy (which `edit` may now write).
7. **A real run on Google Cloud:** the delivery service from its image, an image from a private repository, two
   clusters, and the workload reached over HTTPS from the internet; then everything deleted. Done 2026-10-29 over plain
   HTTP (docs/DELIVERY-SPIKE.md section 12): everything held; it found one defect, fixed (GKE learns a new node account
   later than IAM). HTTPS on a real domain is still to be run.

### Phase 4: Cloud Run services and jobs

**Decided on 2026-10-29 by the operator:** Cloud Run comes back as the two pieces this section always named, service
first, then job; a job's schedule is infrastructure, made by `cloudinfra`; a public service (no Google sign-in needed to
call it) is allowed only when the operator turns it on in `cloudinfra`'s settings, off by default, as with front door
domains. Checked on Google's pages first (Cloud Run Admin API v2):

- A service needs an image to exist; Google's sample `us-docker.pkg.dev/cloudrun/container/hello` stands in until the
  first release. Create and update are long-running; the service itself reports `reconciling`, `terminalCondition`,
  `latestReadyRevision` and `trafficStatuses`, so polling it needs no operation rights.
  ([services](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services))
- Any change to the template makes a new revision, which can be named. A revision that fails its startup probe never
  gets traffic, and the last serving one keeps it. A traffic split pinned to a named revision persists across later
  deploys, so every deploy sets traffic explicitly. Rollback is traffic on an older revision, which Cloud Run keeps
  (1000 per service). ([rollouts](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration))
- Environment from Secret Manager: `secretKeyRef` read at instance start by the runtime identity, which needs
  `secretmanager.secretAccessor`; Google recommends pinning a version. The value never passes through this service.
- A public service is made by turning off the invoker IAM check (`invokerIamDisabled`), Google's recommended way, which
  needs `run.services.setIamPolicy`; granting `allUsers` is blocked by Domain Restricted Sharing.
  ([public](https://docs.cloud.google.com/run/docs/authenticating/public))
- `roles/run.developer` can be granted on one service or job; deploying also needs Service Account User on the runtime
  identity. It can change ingress, scaling and egress too, and delete the service; it cannot change IAM or the invoker
  check. Only org policies (`run.allowedIngress`, `run.allowedVPCEgress`) or a custom role narrow it further, so this
  service's own code changes nothing but the template's container and the traffic; a custom role for `admin` on
  services is a later option.
- A job is replaced whole on update (no update mask; the etag guards it). `:run` starts an execution, whose counts and
  conditions say how it went; executions are kept (the last 1000, or 7 days). A schedule is a Cloud Scheduler job
  calling `:run` with an OAuth token of an account that may run the job.
  ([jobs](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs),
  [on a schedule](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule))

The design:

- **`cloudinfra` `service` kind.** One service or a list (named, as clusters): CPU (1, 2, 4, 8), memory, minimum and
  maximum instances, concurrency, request timeout, ingress (`all`, `internal`, `internal_and_load_balancer`), `public`
  (refused unless the operator's `allow_public_services`), and Direct VPC egress into the request's network. It runs as
  a service account made for it (`run-<request>-<name>`), which reads the request's secrets and nothing else, and is
  deleted with it. Grants: `use` → `run.invoker` (call it), `admin` → `run.developer` on the service and Service Account
  User on its runtime account. The connect block gives the URL and how to call it.
- **`appdelivery` service target.** `{service: NAME | true}`, keyed `service:<name>`. A release is a revision named after
  it, with the image, port, a startup probe on the health path, plain values, and secrets as `secretKeyRef` pinned to
  the version current at deploy. It is made with traffic pinned where it was; once it is ready, traffic moves to it
  wholly. A revision that does not become ready gets no traffic, so a failure needs no restore. Rollback moves the
  traffic to the previous release's revision; remove puts the stand-in image back and sends traffic to it.
- **`cloudinfra` `job` kind.** One job or a list: CPU, memory, tasks, parallelism, retries, task timeout, egress, and an
  optional `schedule` (cron and time zone) that makes a Cloud Scheduler job running it as a second account made for the
  job, allowed to run that job only. Grants: `use` → `run.invoker` (run it), `admin` → `run.developer` and Service Account
  User on its runtime account.
- **`appdelivery` job target.** `{job: NAME | true}`: a release replaces the job's container (image, values, secrets as
  above) and becomes the job's current release; `run` starts an execution now and reports its counts and log link;
  rollback puts the previous release's container back; remove puts the stand-in back.

Steps, each its own pull request (step 5 done 2026-10-30, docs/DELIVERY-SPIKE.md section 13, which found six defects,
all fixed): 1. `cloudinfra` service kind (built 2026-10-29); 2. delivery to a service (built
2026-10-29); 3.
`cloudinfra` job kind with its schedule (built 2026-10-29); 4. delivery to a job and running it (built 2026-10-29: `run_release`
and `get_release_run`); 5. a real run.

## 5. A release's life

```
deploy ──► AWAITING_APPROVAL ──approve──► ROLLING_OUT ──► RUNNING ──(a newer release runs)──► SUPERSEDED
  │ (environments without approval skip it)   │             │  │                                 │
  │                                           │             │  └─remove──► REMOVING ──► REMOVED  │
  │                                           │             └─rollback──► ROLLING_BACK ──► ROLLED_BACK, and the
  └─ refused (nothing recorded)               └─► FAILED          │       previous one RUNNING again ◄────┘
                                                  (the group is   └─► ROLLBACK_FAILED       (REMOVE_FAILED likewise)
                                                   back on one version)
```

- `plan_release` checks everything in sections 2 and 3, resolves the bindings to names (never values), lists the
  machines and names the release it would replace (`replaces`). Pure: nothing on a machine, nothing recorded.
- `deploy_release` records the release and, without approval, starts the rollout in a background worker; in an
  environment with `require_approval` it waits for `approve_release` by a principal with `can_approve`, of the same
  team, other than the one that asked. One release per request and group moves or waits at a time.
- **The rollout** checks everything again as it is when it starts (time may have passed since the plan or the
  approval: a withdrawn grant, a stopped request), refuses if the group's machines are not the ones recorded, verifies
  the image's attestation where the environment asks, reads the secrets, and deploys one machine at a time. The first
  machine that is unhealthy, fails its pull or cannot be reached stops it: the host script has already put that machine
  back on its previous container, the machines done before it are rolled back (or emptied, for a group's first
  release), the rest are `SKIPPED`, and the release is `FAILED` with `ROLLOUT_FAILED`. A group is never left half on
  one version. On success the release it replaced becomes `SUPERSEDED`.
- `rollback_release` is offered only for the release running on its group when its machines still keep the previous
  container (`can_roll_back`): not for a group's first release, and not for a release a rollback restored, whose
  machines keep nothing earlier, and not after a failed rollout put any of its machines back, since that started their
  previous container. It starts `previous` again on each machine; the rolled-back release becomes `ROLLED_BACK` and the
  previous one `RUNNING` again.
- `remove_release` deletes both containers from each machine and leaves the machine as `cloudinfra` made it.
- A release belongs to a request and a group, whatever its environment: those are the machines. A production release
  on the machines a dev release runs on replaces it. Give each environment its own `cloudinfra` request.
- A container's log, kept when a machine fails, has every secret value the release read replaced by `[redacted]`: an
  application may print its own environment.
- After a restart of the service, a release left `ROLLING_OUT` becomes `FAILED`, `ROLLING_BACK` becomes
  `ROLLBACK_FAILED` and `REMOVING` becomes `REMOVE_FAILED`, each with the cause `INTERRUPTED`; a machine left
  `DEPLOYING` is `FAILED` and one never reached is `SKIPPED`. The host script is idempotent by container name, so the
  next deploy puts each machine right. A release waiting for approval keeps waiting.

## 6. Interface

The same shape as `cloudinfra`, so a caller that knows one knows the other: FastAPI with a Streamable HTTP MCP endpoint
and a small REST API, bearer token per principal stored as a SHA-256 hash, rate limits, one audit event per call,
`{"status": "FAILED", "error": {code, message, details}}` for every failure.

| MCP tool | REST | Needs |
|---|---|---|
| `plan_release` | `POST /releases/plan` | `can_deploy` |
| `deploy_release` | `POST /releases` | `can_deploy` |
| `approve_release` | `POST /releases/{id}/approve` | `can_approve`, another principal |
| `get_release` | `GET /releases/{id}` | read |
| `list_releases` | `GET /releases?request_id=&environment=` | read |
| `get_running` | `GET /running?request_id=` | read: what runs on each machine now |
| `rollback_release` | `POST /releases/{id}/rollback` | `can_deploy` |
| `remove_release` | `DELETE /releases/{id}` | `can_deploy` |
| `get_service_status` | `GET /status` | read |

Also `GET /healthz` (no authentication) and `GET /catalog`. The tools, their inputs, outputs, errors and safety hints
are in [DELIVERY-CATALOG.md](DELIVERY-CATALOG.md), generated from `appdelivery/catalog.py`; a test holds the
registered tools and the document to the records. The MCP endpoint answers only the Host headers in `allowed_hosts`
(DNS rebinding protection) and refuses any argument a tool does not take, naming the one most likely meant.

Claude chat signs in with OAuth only, so the service can be its own small authorization server: with a `sign_in` block
(`url`, the service's public HTTPS address), a person pastes their principal's token on the service's own page and
Claude acts as that principal (`appdelivery/oauth.py`, the same shape as `cloudinfra`'s; see "Connect Claude chat" in
[INFRASTRUCTURE-ARCHITECTURE.md](INFRASTRUCTURE-ARCHITECTURE.md)). Off by default; static tokens keep working.

## 7. State

One SQLite file, its own, never `cloudinfra`'s, in WAL mode, written by one process (a lock file next to it):
`releases` (request, group, environment, team, who asked and who approved, status, image digest, previous,
`can_roll_back`, the request as sent with bindings by name, the plan's binding sources and warnings, the last error),
`targets` (release, position, resource id, name, zone, status, times, last error, the container's scrubbed log), and
`events` (audit). JSON log lines on the `appdelivery` logger carry the release id and identity. No secret
value is ever stored, logged or returned; a test reads the database and the log after a release with a secret binding
and finds no trace of the value, as `cloudinfra`'s does for the database password.

## 8. Operator configuration

```yaml
database: appdelivery.db
cloudinfra:
  url: https://infra.internal.example.com
gcp:
  project_id: infra-project              # the project cloudinfra provisions in
  default_network_iap: false             # true only if the operator's default network admits 35.235.240.0/20 on port 22
environments:
  dev:
    token_env: APPDELIVERY_DEV_INFRA_TOKEN          # cloudinfra token of this environment's read-only principal
    service_account: appdelivery-dev@infra-project.iam.gserviceaccount.com
    allowed_registries: [us-central1-docker.pkg.dev/acme-apps]
  prod:
    token_env: APPDELIVERY_PROD_INFRA_TOKEN
    service_account: appdelivery-prod@infra-project.iam.gserviceaccount.com
    allowed_registries: [us-central1-docker.pkg.dev/acme-apps-release]
    require_attestation: true
    trusted_signers: ["gcpkms://projects/acme-sec/locations/global/keyRings/release/cryptoKeys/attestor"]
    require_approval: true
principals:
  - {name: shop-team, token_sha256: "…", infra_owner: shop-team, environments: [dev, prod], can_deploy: true}
  - {name: shop-oncall, token_sha256: "…", infra_owner: shop-team, environments: [prod], can_approve: true}
```

### Running it on Google Cloud

As proven on 2026-10-29 ([DELIVERY-SPIKE.md](DELIVERY-SPIKE.md) section 10): the image on a machine whose service
account has Token Creator on each environment's service account and read access to the image's repository, and nothing
else for the delivery service. The data volume (`/data`) holds the state, the log and gcloud's SSH key. On
Container-Optimized OS, a service on the host network needs its port opened in the host's firewall (`iptables`); with
`-p` it does not. Reach the service through IAP or an internal load balancer; it holds bearer tokens, not TLS.

## 9. What `cloudinfra` needs

**For phase 1: nothing in code.** One principal per `appdelivery` environment (read-only, `scope: all`, `identity` set)
and the `container-host` profile. Teams add the environment's service account to `access` with `admin`.

**Recommended, small:** a `scope: granted` for principals, which sees only the requests whose access list names the
principal's `identity`. Today `appdelivery` must read every request (`scope: all`) and filter; with it, `cloudinfra`
itself enforces that `appdelivery` sees only what teams handed it.

**For phase 2** (decided 2026-10-29, section 4): a namespace per cluster and grants as RoleBindings in it. The two
alternatives set aside: IAM's `container.developer` for the delivery service account (reaches every cluster of the
project), and Google Groups for RBAC (needs a Workspace domain; it remains usable for `group:` members).

**For phase 4** (decided 2026-10-29, section 4): the `service` and `job` kinds, with their runtime accounts and, for a
job, its schedule. Beyond what `cloudinfra` grants, the operator gives each environment's service account, once,
`roles/artifactregistry.reader` on the repositories in its `allowed_registries`: Cloud Run checks that the account
deploying a revision or a job can read the image, not only the service's own account (found on a real project on
2026-10-30).

**Later, optional:** a machine service account chosen by the request, so an application on the machine can read its
own secrets and pull its own images; then `appdelivery` would pass secret names instead of values.

## 10. Boundaries that tests hold

As `tests/cloudinfra/test_infra_catalog.py` does for `cloudinfra`:

- `appdelivery` imports neither `cloudinfra` nor `labdeploy`; it speaks to `cloudinfra` only over HTTP, and `cloudinfra`
  never imports `appdelivery`. No source, test, configuration or document of `appdelivery` names `labdeploy`, and its
  tests pass with the `labdeploy` directory deleted: they live in `tests/appdelivery/` and use none of the shared test
  helpers under `tests/` (the root `tests/conftest.py` belongs to the old tests).
- It has its own console script and container image; it does not ride in the image that runs `labdeploy` today. It
  needs no Python dependency beyond those `cloudinfra` already pins (mcp, fastapi, httpx, pydantic, PyYAML, uvicorn). Its
  image adds two pinned binaries: the gcloud CLI and Cosign 3.1.x.
- It copies nothing from `cloudinfra` by import. The bearer-token gate, rate limiter and audit wrapper are small and are
  written again in `appdelivery`; a shared library can come later if a third service needs them.
- The release models have no field that could carry source, a build, a command or a mount (section 3).
- A command runs only in one module, with a fixed argument list (`gcloud compute ssh … --tunnel-through-iap`, `cosign
  verify-attestation`); the host script is a file in the package, checked against its hash before it is sent.
- An image without a digest is refused; a secret value never reaches the database, a log line or a response.
- Tests live in `tests/appdelivery/` with a fake `cloudinfra` API and a fake machine session; no cloud.

## 11. Phases and what "done" means

1. **Phase 1, machines.** Plan, deploy, health, rollback, remove on one machine and on a group; attestation and
   approval per environment; the boundary tests. Done when one real run on the company's project takes an image by
   digest onto a `container-host` machine of a `cloudinfra` request, answers on its health path, rolls back on a broken
   image, and leaves no secret value in the state or the log.
2. **Phase 2, clusters,** once section 9's cluster choice is made.
3. **Phase 3, convenience:** a Score file as an alternative input; `cloudinfra`'s `scope: granted`.

## 12. Decisions that are yours

1. The name (`appdelivery` is a placeholder).
2. Whether attestation is required in every environment or only in production.
3. Phase 2's cluster choice (section 9): decided 2026-10-29, a namespace per cluster with RBAC grants.
4. Whether a release on a group may run on several machines at once (faster) or always one at a time (safer; the draft).
5. Whether Cloud Run comes back later, as a `cloudinfra` kind plus a target here (section 4), or stays out.

## 13. Checked before building (2026-10-29)

What the design rests on, checked on the primary page or in `cloudinfra`'s code:

| Fact | Where | Consequence |
|---|---|---|
| Container-Optimized OS LTS 117 to 133 support OS Login and Shielded VM; the gcloud CLI is not installed | [Google's OS details](https://docs.cloud.google.com/compute/docs/images/os-details) | The `container-host` profile works with `cloudinfra`'s OS Login and Shielded VM settings as they are |
| It ships Docker, containerd and `cloud-init`; no package manager; weekly updates applied at reboot | [Container-Optimized OS overview](https://docs.cloud.google.com/container-optimized-os/docs/concepts/features-and-benefits) | Nothing is installed on the machine; `--restart=always` brings `app` back after an update reboot |
| An OS Login user runs Docker with sudo unless added to the `docker` group; `/root/.docker/config.json` is not permitted | [Running containers on Container-Optimized OS](https://docs.cloud.google.com/container-optimized-os/docs/how-to/run-container-instance) | `admin` grant; a temporary `DOCKER_CONFIG` |
| Docker logs in to Artifact Registry with `-u oauth2accesstoken --password-stdin`; the token lasts 60 minutes | [Artifact Registry authentication](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication) | Pull with a fresh token per machine |
| SSH through IAP needs the tunnel role, a project-level role with `compute.projects.get`, and a rule from 35.235.240.0/20 on port 22; a VM with a service account would also need `serviceAccountUser`, and `cloudinfra`'s machines have none | [Set up OS Login](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin), `cloudinfra/providers/gcp/connect.py`, `compute.py` | The checks in section 4 |
| OS Login names a service account's user `sa_…`; 2-step verification is not enforced on service accounts | [OS Login](https://docs.cloud.google.com/compute/docs/oslogin), [Set up OS Login](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin) | Non-interactive SSH is possible |
| A request's subnet has Private Google Access, and Cloud NAT only when asked (`network.nat`) | `cloudinfra/providers/gcp/network.py` | Artifact Registry only, unless the machine has a public address or its network NAT |
| `cloudinfra`'s status answer carries `owner`, `access` (member, level, status), `endpoints`, `lifecycle`, and each machine's zone, numeric id and network | `cloudinfra/models/response.py`, `resources.py` | Every check in section 2 reads one existing endpoint; no `cloudinfra` change for phase 1 |
| A principal's `identity` may be named in `access` even outside `allowed_domains` | `cloudinfra/orchestration/access.py` | The delivery service account needs no domain exception |
| Cosign 3.1.x verifies both the old signature format and the new bundle format; v4 is planned and v3 stays supported | [Cosign releases](https://newreleases.io/project/github/sigstore/cosign/release/v3.1.2) | Pin 3.1.x in the image |
| The container startup agent stopped working on 2026-07-31 | [Google's shutdown notice](https://docs.cloud.google.com/compute/docs/containers/prepare-for-container-agent-shutdown) | Nothing here uses `gce-container-declaration` |

Not confirmed on any page, so they were proven on a real run on 2026-10-29 (section 14, step 0; the commands and the
full results are in [DELIVERY-SPIKE.md](DELIVERY-SPIKE.md)):

1. `gcloud compute ssh … --tunnel-through-iap --impersonate-service-account … --command "bash -s"` forwards standard
   input to the remote shell as a service account, without a prompt. **Proven**, once the IAP grant has propagated
   (4 to 6 minutes after `cloudinfra` grants it).
2. The OS Login user of a service account with `osAdminLogin` gets sudo on Container-Optimized OS 133. **Proven.**
3. `base64` is on the host and bash has `/dev/tcp`. **Half:** `base64`, `curl` and `jq` are there; bash has no
   `/dev/tcp`. The health check uses curl.
4. A container started with `--restart=always` comes back after `cloudinfra` stops and starts the machine. **Proven**;
   SSH answers about 30 seconds after `cloudinfra` reports READY.

Also proven on the way: a machine without a public address pulls from Artifact Registry; `docker login` with the
service account's token works into a root-only temporary directory under `/run`; `/root` is read-only.

## 14. Phase 1 build plan

On a new branch from `main`, in this order; each step ends green on `python -m pytest tests/appdelivery -q` and
`python -m ruff check .`.

0. **Spike, on a real project.** One `container-host` machine made by `cloudinfra`, the delivery service account
   added to its `access` as `admin`, and the four checks of section 13 run by hand, with the commands in
   [DELIVERY-SPIKE.md](DELIVERY-SPIKE.md). **Done 2026-10-29** in a sandbox project; all four hold except bash
   `/dev/tcp` (curl is used instead). Step 6 also retries IAP's `4033` after a fresh grant and `4003` after a start.
1. **Skeleton and boundaries.** Built 2026-10-29. `appdelivery/__init__.py` (0.1.0), `config.py` (section 8, strict),
   `cli.py` (`token`, `check-config`, and `plan FILE --config --as PRINCIPAL`, which reads the request from `cloudinfra`
   and runs every check; the tool catalog moves to step 8 with the server), the `app-delivery` script in
   `pyproject.toml`, `appdelivery.example.yaml`, and `tests/appdelivery/test_delivery_boundaries.py`: no import of
   `cloudinfra` or `labdeploy` in the package and none of `appdelivery` in either of them, `labdeploy` named nowhere in
   the package or its tests, no shared test helper and no `conftest.py`, commands only in `remote.py`, no field in the
   contract that could run or build something, and the whole directory run again in a fresh interpreter where
   importing `labdeploy` fails.
2. **Contract.** Built. `models.py` and `errors.py`: digest required, the three binding sources, credential-looking
   names refused as literals (by whole words, so `AUTHOR_NAME` passes and `API_KEY` does not), plain health paths only,
   port 22 refused. Every refusal of section 3 has a test.
3. **The `cloudinfra` client.** Built. `infra.py` keeps only the fields a release needs and ignores what the layer adds
   later. The fixture every delivery test uses is checked against `cloudinfra`'s own response model (a contract test).
4. **Plan.** Built. `planner.py`: owner (answered NOT_FOUND, as `cloudinfra` answers another caller), READY, the grant
   (admin, GRANTED, covering each machine), Container-Optimized OS, IAP SSH on the machine's network or the operator's
   word for the default network, registry allowed by whole path parts, Artifact Registry unless the machine has a public
   address, bindings that exist, and warnings for a port open only inside the network, autostop and max_run_hours.
   Every problem is reported at once.
5. **Secrets and images.** Built. `google.py`: this host's token (metadata server, else gcloud), each environment's
   token from the IAM Credentials API (15 minutes, cached), and Secret Manager reads; `bindings.py` turns bindings into
   values and the env file; `attest.py` runs `cosign verify-attestation` per trusted signer with a registry login written
   to a private directory for that one call, the token never on a command line. `remote.py` runs only `gcloud` and
   `cosign`.
6. **The machine session.** Built 2026-10-29. `appdelivery/host/release.sh` (deploy, rollback, remove, status; bash
   3.2 compatible; shipped as package data) and `machine.py`, which sends it with its inputs to `gcloud compute ssh`
   (in `remote.py`) after checking it against `SCRIPT_SHA256`, reads its report lines, and retries IAP's `4033` for 10
   minutes and `4003` for 2. Tested by running the real script on the developer's bash against a fake `docker` and
   `curl` (`tests/appdelivery/host_fakes.py`): first deploy, demotion without a restart policy, one previous kept,
   unhealthy and crashed images restored, failed pull and login leaving the running container alone, the token only on
   stdin, rollback, remove, status, and values that look like shell staying values. Then run on a real Container-Optimized
   OS machine (deploy, redeploy, rollback, a broken image restored, a reboot that brings back only the current version,
   remove), with the results in [DELIVERY-SPIKE.md](DELIVERY-SPIKE.md) section 8.
7. **State and the orchestrator.** Built 2026-10-29. `state.py` (section 7, with the process lock), `events.py` (JSON
   log lines, secret-looking fields redacted) and `releases.py` (`Delivery`: plan, deploy, approve, rollout one machine
   at a time with undo, rollback, remove, get, list, running with an optional live check of each machine, recovery).
   Tested on a fake fleet that answers as the host script did on the real machine (`tests/appdelivery/fleet_fakes.py`),
   including a test that reads the database file, the log and every answer after a release with a secret binding and a
   failed machine, and finds neither the secret's value nor the environment's token.
8. **Server.** Built 2026-10-29. `server.py` (FastAPI, the MCP endpoint with `StrictMCP`, the REST routes of section 6,
   a lifespan that takes the process lock, recovers interrupted releases and drains the workers on shutdown),
   `auth.py` (bearer tokens as hashes, rate limits per identity and per failing address, an audit event per call),
   `catalog.py` and `tools.py` (the nine tools from their records), `app-delivery catalog`, and the generated
   [DELIVERY-CATALOG.md](DELIVERY-CATALOG.md). Tested over HTTP with the fake fleet: authentication and throttling, a
   refused Host header, the MCP handshake and tool schemas against the catalog, a release from plan to remove through
   MCP, structured errors, approval by another principal, the REST routes, and audit events.
9. **Image and docs.** Built 2026-10-29. `Dockerfile.appdelivery`: the repository's Python by digest, the gcloud CLI
   591.0.0 from Google's versioned archive, and Cosign v3.1.3, each checked by SHA-256 for amd64 and arm64; a named
   user (OpenSSH refuses a uid without a passwd entry) with a writable home for gcloud's SSH key; nothing of the other
   packages copied in. Built and run for both architectures: gcloud, Cosign and ssh work as that user, and the server
   answers health, status, REST and MCP with a real configuration. Google's apt repository keeps only recent gcloud
   versions and publishes a SHA-256 only for the current unversioned archive; how the pinned sums were derived is
   written in the Dockerfile. The spike ran gcloud 539.0.0; step 10 runs this image's 591.0.0. A README section.
10. **Real run.** Done 2026-10-29 ([DELIVERY-SPIKE.md](DELIVERY-SPIKE.md) section 9): plan, approval by another
    principal, two releases across two machines, a broken release stopped at the first machine and restored, a
    rollback, a remove, and no trace of a bound secret in either service's state or log. It found one defect, fixed
    with a test: a failed rollout that puts a machine back spends that machine's previous container, so the release
    running again can no longer be rolled back there; it now says so (`can_roll_back` false) and a rollback is refused
    up front. The delivery service ran from the repository rather than its image (the operator's token is not handed to a
    container). It then ran from its image on Google Cloud, signed in through the metadata server, on 2026-10-29
    ([DELIVERY-SPIKE.md](DELIVERY-SPIKE.md) section 10).
