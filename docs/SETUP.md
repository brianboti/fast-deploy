# Setting up both services

How a platform team puts the infrastructure service and the delivery service on a Google Cloud project, makes the
first request and the first release, and connects Claude. The architecture and the reasons are in
[INFRASTRUCTURE-ARCHITECTURE.md](INFRASTRUCTURE-ARCHITECTURE.md) and [DELIVERY-SPEC.md](DELIVERY-SPEC.md); every
version, role and API named here is listed with its source in [REQUIREMENTS.md](REQUIREMENTS.md). The commands were
run as written on a sandbox project between 2026-10-29 and 2026-10-30 ([DELIVERY-SPIKE.md](DELIVERY-SPIKE.md)).

Names used below:

```bash
export PROJECT=your-infrastructure-project REGION=us-central1
```

## 1. The shape

- **One project for what the service provisions**, apart from the company's other projects: the service's identity
  has admin roles there and nowhere else, and Google's quota and billing for the created resources land there.
- **One platform machine** (or any host with a service account) runs both services as containers, side by side,
  each with its own state file, configuration, principals and port; or a laptop runs them from this repository for
  evaluation. The two services talk only over HTTP.
- **One service account per delivery environment** (`appdelivery-dev`, `appdelivery-prod`, …), and one
  `cloudinfra` principal for each. Production is a separate account and a separate `cloudinfra` request; a release
  belongs to a request, whatever environment it names.
- **Teams** are `cloudinfra` principals with `can_provision` and `can_destroy`, and `appdelivery` principals that name
  the `cloudinfra` owner they speak for (`infra_owner`). A team deploys only onto its own requests.

## 2. The project

Enable the APIs for the kinds you will offer (Compute Engine, Cloud SQL Admin, Cloud Storage, Service Networking,
Cloud Resource Manager, Pub/Sub, Secret Manager, Memorystore for Redis, Artifact Registry, Kubernetes Engine, IAM,
Identity-Aware Proxy, Cloud Run Admin, Cloud Scheduler, Cloud Monitoring, IAM Credentials, OS Login, Binary
Authorization):

```bash
gcloud services enable compute.googleapis.com sqladmin.googleapis.com storage.googleapis.com servicenetworking.googleapis.com cloudresourcemanager.googleapis.com pubsub.googleapis.com secretmanager.googleapis.com redis.googleapis.com artifactregistry.googleapis.com container.googleapis.com iam.googleapis.com iap.googleapis.com run.googleapis.com cloudscheduler.googleapis.com monitoring.googleapis.com iamcredentials.googleapis.com oslogin.googleapis.com binaryauthorization.googleapis.com --project $PROJECT
```

Binary Authorization is on for clusters and Cloud Run by default (`providers.gcp.binary_authorization`). The project's
policy starts by allowing every image, so nothing is refused until you write a policy that requires attestations; turn
the setting off only if you will never write one. With `providers.gcp.deny_egress`, also enable `dns.googleapis.com`;
with `providers.gcp.kms_keys`, `cloudkms.googleapis.com`.

Make the infrastructure service's identity:

```bash
gcloud iam service-accounts create cloudinfra --project $PROJECT --display-name "cloudinfra"
```

Grant it the roles in [REQUIREMENTS.md](REQUIREMENTS.md) section 6 for the kinds you offer. For machines, disks,
networks, secrets, buckets, queues and access grants that is `roles/compute.instanceAdmin.v1`,
`roles/compute.networkAdmin`, `roles/compute.securityAdmin`, `roles/secretmanager.admin`, `roles/storage.admin`,
`roles/pubsub.editor`, `roles/monitoring.viewer`, `roles/browser`, and a custom role with exactly the `getIamPolicy`
and `setIamPolicy` permissions of instances, IAP tunnel instances, secrets, buckets, topics, subscriptions and
Artifact Registry repositories. Databases add `roles/cloudsql.admin` and `roles/servicenetworking.networksAdmin`;
caches `roles/redis.admin`; registries the `artifactregistry.repositories.*` permissions; clusters
`roles/container.clusterAdmin` and, for node accounts, `roles/iam.serviceAccountCreator`,
`roles/iam.serviceAccountDeleter`, `roles/iam.serviceAccountUser` and `roles/resourcemanager.projectIamAdmin` limited
by the condition below; services and jobs `roles/run.admin`, `roles/iam.serviceAccountAdmin` and
`roles/cloudscheduler.admin`. An HTTPS load balancer also makes SSL certificates and SSL policies
(`compute.sslCertificates.*`, `compute.sslPolicies.*`), and with `cloud_armor_policy` it needs
`compute.securityPolicies.use` on that policy; `deny_egress` adds `roles/dns.admin`. Put the organisation-policy
backstops of REQUIREMENTS.md section 6 on the project.

The project IAM role for node accounts, limited to granting the one node role. The service reads the project's policy
before it writes it and refuses to change project IAM when its own account holds this role, `roles/owner` or
`roles/iam.securityAdmin` without exactly this condition:

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:cloudinfra@$PROJECT.iam.gserviceaccount.com --role roles/resourcemanager.projectIamAdmin --condition='title=node-role-only,expression=api.getAttribute("iam.googleapis.com/modifiedGrantsByRole", []).hasOnly(["roles/container.defaultNodeServiceAccount"])'
```

With `providers.gcp.kms_keys`, each service's own agent encrypts with the region's key. Grant the Compute Engine,
Cloud SQL, Cloud Storage, Secret Manager, Artifact Registry and Kubernetes Engine agents
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on it, for example the Cloud SQL agent:

```bash
gcloud kms keys add-iam-policy-binding data --keyring platform --location $REGION --project $KEYS_PROJECT --member serviceAccount:service-$PROJECT_NUMBER@gcp-sa-cloud-sql.iam.gserviceaccount.com --role roles/cloudkms.cryptoKeyEncrypterDecrypter
```

For a laptop, give yourself the right to act as that account and a quota project:

```bash
gcloud iam service-accounts add-iam-policy-binding cloudinfra@$PROJECT.iam.gserviceaccount.com --project $PROJECT --member user:$(gcloud config get-value account) --role roles/iam.serviceAccountTokenCreator
```

```bash
gcloud auth login
```

## 3. The infrastructure service

```bash
python -m cloudinfra.cli token
```

Keep the token (the first line) where the caller will read it and put only the hash (the second line) in the
configuration:

```bash
cp cloudinfra.example.yaml cloudinfra.local.yaml
```

Set, at least:

```yaml
providers:
  gcp:
    project_id: your-infrastructure-project
    allowed_regions: [us-central1]
    # impersonate_service_account: cloudinfra@your-infrastructure-project.iam.gserviceaccount.com   # on a laptop
    # quota_project: your-infrastructure-project                                                    # on a laptop
    profiles:
      container-host:                                # the profile the delivery service needs for machine targets
        image: projects/cos-cloud/global/images/family/cos-133-lts
        description: Container-Optimized OS 133 LTS
        accelerator: none
access:
  allowed_domains: [your-company.com]               # who may be granted access to what the service creates
principals:
  - {name: shop-team, token_sha256: "<hash>", can_provision: true, can_destroy: true,
     identity: "group:shop-devs@your-company.com"}
  - {name: appdelivery-dev, token_sha256: "<hash>", scope: all,
     identity: "serviceAccount:appdelivery-dev@your-infrastructure-project.iam.gserviceaccount.com"}
  - {name: operator, token_sha256: "<hash>", scope: all}
```

**What is closed until you open it.** `allow_public_exposure` is off: a request that asks for a public machine address,
a network or firewall rule open to a public range, a load balancer, a cluster's front door, a public database or a
Cloud Run service taking all traffic is refused before any provider call. Turn it on for the services that need it.
Point `providers.gcp.default_network` at a network you made, with flow logs and no rule open to the internet, not at
Google's `default`, whose `default-allow-ssh` rule admits port 22 from everywhere. The other defaults of the
assessment's release S2 (cluster private nodes, Dataplane V2, Binary Authorization, Pod Security, network logs, Cloud
NAT, HTTPS on load balancers with hostnames) need nothing from you; `kms_keys`, `cluster_authorized_networks`,
`cloud_armor_policy` and `deny_egress` are yours to set. With `cluster_authorized_networks`, list the addresses both
services call clusters from, or neither can reach them.

**Behind a proxy.** Every caller then arrives from the proxy's address, so the per-address budgets (wrong tokens,
registrations, the public OAuth paths) are shared by all. Start uvicorn with `--proxy-headers` and set
`FORWARDED_ALLOW_IPS` to the proxy's own address. Never set it to `*`: any caller could then name its own address, and
the audit trail would record it.

**Keyed token hashes.** A copied configuration with plain SHA-256 hashes can be checked against guesses offline. With
a pepper of 32 characters or more in `CLOUDINFRA_TOKEN_PEPPER`, kept in the host's secret store and never in the
configuration or the state, `python -m cloudinfra.cli token` prints `token_hmac_sha256` instead of `token_sha256`;
put that in the principal. The service will not start with keyed hashes and no pepper. Principals can move one at a
time, and plain and keyed hashes work side by side.

Check the family exists before relying on the profile:

```bash
gcloud compute images describe-from-family cos-133-lts --project cos-cloud --format "value(name,status)"
```

Run it:

```bash
CLOUDINFRA_CONFIG=cloudinfra.local.yaml uvicorn cloudinfra.mcp.server:create_app --factory --host 127.0.0.1 --port 8100
```

**The platform's images** are built like a team's: only in this project, by Cloud Build, from the platform's own
repository linked to a connection (section 4 shows the connection), at one commit, as a build account that may write
logs and push to the platform's repository and nothing else. `cloudbuild.yaml` builds both images, has Cloud Build push
them with signed provenance, and tags each with the build's id. Once:

```bash
gcloud iam service-accounts create platform-build --project $PROJECT --display-name "platform images"
```

```bash
gcloud artifacts repositories create platform --repository-format docker --location $REGION --immutable-tags --project $PROJECT
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:platform-build@$PROJECT.iam.gserviceaccount.com --role roles/logging.logWriter --condition=None
```

```bash
gcloud artifacts repositories add-iam-policy-binding platform --location $REGION --project $PROJECT --member serviceAccount:platform-build@$PROJECT.iam.gserviceaccount.com --role roles/artifactregistry.writer
```

Then, for each release of the platform, a build of its commit; the answer names both images by digest:

```bash
gcloud builds submit projects/$PROJECT/locations/$REGION/connections/github/repositories/infrastructure-layer --revision COMMIT_SHA --region $REGION --config cloudbuild.yaml --service-account projects/$PROJECT/serviceAccounts/platform-build@$PROJECT.iam.gserviceaccount.com --substitutions _REGISTRY=$REGION-docker.pkg.dev/$PROJECT/platform
```

On the platform machine, run the `cloudinfra` image by that digest, mount the configuration at
`/config/cloudinfra.yaml` and a volume at `/data`, and run it as the machine's service account; on Container-Optimized OS a container on the host network needs its port opened
in the host firewall (`iptables -A INPUT -p tcp --dport 8100 -j ACCEPT`), a container started with `-p` does not. Reach
it through IAP or an internal load balancer; the service holds bearer tokens, not TLS.

The first request, with the team's token (the example asks for a network, two machines, a database, a cache, a secret
and a registry):

```bash
curl -s -X POST "http://127.0.0.1:8100/infrastructure?wait_seconds=45" -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" --data-binary @<(python -c 'import json,yaml,sys; print(json.dumps(yaml.safe_load(open("examples/infrastructure-request.yaml"))))')
```

Then read it until `status` is `READY` and each access entry is `GRANTED`:

```bash
curl -s "http://127.0.0.1:8100/infrastructure/$REQUEST_ID" -H "Authorization: Bearer $TEAM_TOKEN"
```

Every resource carries a `connect` block with the commands and the project-level prerequisites a member needs;
`lifecycle` says when the request expires; `credentials` names the secrets that hold the database password and the
cache AUTH string. Destroy it with `DELETE /infrastructure/$REQUEST_ID`.

## 4. The delivery service

For each environment, a service account, the right for the delivery service's host to act as it, the project-level
role every `cloudinfra` connect block names, and, for cluster and Cloud Run targets, two more:

```bash
gcloud iam service-accounts create appdelivery-dev --project $PROJECT --display-name "appdelivery dev"
```

```bash
gcloud iam service-accounts add-iam-policy-binding appdelivery-dev@$PROJECT.iam.gserviceaccount.com --project $PROJECT --member serviceAccount:platform-machine@$PROJECT.iam.gserviceaccount.com --role roles/iam.serviceAccountTokenCreator
```

And a pull account for the environment, which reads the environment's registries and nothing else: its five-minute
token is the only credential a machine ever receives (`pull_service_account`; the environment's own token never leaves
the delivery service).

```bash
gcloud iam service-accounts create pull-dev --project $PROJECT --display-name "appdelivery dev image pulls"
```

```bash
gcloud iam service-accounts add-iam-policy-binding pull-dev@$PROJECT.iam.gserviceaccount.com --project $PROJECT --member serviceAccount:platform-machine@$PROJECT.iam.gserviceaccount.com --role roles/iam.serviceAccountTokenCreator
```

```bash
gcloud artifacts repositories add-iam-policy-binding shop-images --location $REGION --project $PROJECT --member serviceAccount:pull-dev@$PROJECT.iam.gserviceaccount.com --role roles/artifactregistry.reader
```

```bash
gcloud iam roles create appdeliveryInstanceViewer --project $PROJECT --title "appdelivery instance viewer" --permissions compute.projects.get,compute.instances.get
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:appdelivery-dev@$PROJECT.iam.gserviceaccount.com --role projects/$PROJECT/roles/appdeliveryInstanceViewer
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:appdelivery-dev@$PROJECT.iam.gserviceaccount.com --role roles/container.clusterViewer
```

```bash
gcloud artifacts repositories add-iam-policy-binding shop-images --location $REGION --project $PROJECT --member serviceAccount:appdelivery-dev@$PROJECT.iam.gserviceaccount.com --role roles/artifactregistry.reader
```

The last one is per repository in the environment's `allowed_registries`: Cloud Run checks that the account deploying
a revision can read the image. Everything else the account may do on a request comes from the team naming it at
`admin` in the request's `access`.

**Builds.** Images are built only here, by Cloud Build, never on a laptop or in a CI job. Enable the Cloud Build and
Container Analysis APIs, then, once, connect the company's Git host and link each repository a team builds from
(GitHub: the command prints a link to authorise and install the Cloud Build GitHub App; wait for `installationState:
COMPLETE` in `gcloud builds connections describe`):

```bash
gcloud services enable cloudbuild.googleapis.com containeranalysis.googleapis.com --project $PROJECT
```

The Cloud Build service agent creates the secret that holds the GitHub token, so it needs Secret Manager Admin first
(the console offers this grant; the role may be removed once the connection exists):

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format "value(projectNumber)")
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:service-$PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com --role roles/secretmanager.admin --condition=None
```

```bash
gcloud builds connections create github github --region $REGION --project $PROJECT
```

```bash
gcloud builds repositories create acme-shop --remote-uri https://github.com/acme/shop.git --connection github --region $REGION --project $PROJECT
```

For each environment a build account that may write logs and push to each source's registry, and the environment's
own account allowed to start builds as it:

```bash
gcloud iam service-accounts create build-dev --project $PROJECT --display-name "appdelivery dev builds"
```

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:build-dev@$PROJECT.iam.gserviceaccount.com --role roles/logging.logWriter
```

```bash
gcloud artifacts repositories add-iam-policy-binding shop-images --location $REGION --project $PROJECT --member serviceAccount:build-dev@$PROJECT.iam.gserviceaccount.com --role roles/artifactregistry.writer
```

That grant is for a repository the platform team made by hand. For a registry a team's `cloudinfra` request makes,
list the build account in `cloudinfra`'s `access.registry_writers` instead, and the team names it at `admin`: it is then
the registry's only writer and is granted nothing else in the request.

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:appdelivery-dev@$PROJECT.iam.gserviceaccount.com --role roles/cloudbuild.builds.editor
```

```bash
gcloud iam service-accounts add-iam-policy-binding build-dev@$PROJECT.iam.gserviceaccount.com --project $PROJECT --member serviceAccount:appdelivery-dev@$PROJECT.iam.gserviceaccount.com --role roles/iam.serviceAccountUser
```

In an environment with `require_provenance`, its account also reads the provenance Cloud Build records:

```bash
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:appdelivery-prod@$PROJECT.iam.gserviceaccount.com --role roles/containeranalysis.occurrences.viewer --condition=None
```

In `cloudinfra`'s configuration the environment is a read-only principal (section 3 above); its token goes into the
variable the environment names. Then:

```bash
app-delivery token
```

```bash
cp appdelivery.example.yaml appdelivery.local.yaml
```

```yaml
cloudinfra: {url: http://127.0.0.1:8100}
gcp: {project_id: your-infrastructure-project}
environments:
  dev:
    token_env: APPDELIVERY_DEV_INFRA_TOKEN
    service_account: appdelivery-dev@your-infrastructure-project.iam.gserviceaccount.com
    pull_service_account: pull-dev@your-infrastructure-project.iam.gserviceaccount.com
    allowed_registries: [us-central1-docker.pkg.dev/your-infrastructure-project/shop-images]
    build: {service_account: build-dev@your-infrastructure-project.iam.gserviceaccount.com}
  prod:
    token_env: APPDELIVERY_PROD_INFRA_TOKEN
    service_account: appdelivery-prod@your-infrastructure-project.iam.gserviceaccount.com
    pull_service_account: pull-prod@your-infrastructure-project.iam.gserviceaccount.com
    allowed_registries: [us-central1-docker.pkg.dev/your-infrastructure-project/shop-images]
    build: {service_account: build-prod@your-infrastructure-project.iam.gserviceaccount.com}
    require_attestation: true
    trusted_signers: ["gcpkms://projects/your-security-project/locations/global/keyRings/release/cryptoKeys/attestor"]
    require_approval: true
sources:
  shop-web:
    repository: projects/your-infrastructure-project/locations/us-central1/connections/github/repositories/acme-shop
    infra_owner: shop-team
    registry: us-central1-docker.pkg.dev/your-infrastructure-project/shop-images
principals:
  - {name: shop-team, token_sha256: "<hash>", infra_owner: shop-team, environments: [dev, prod], can_deploy: true, can_build: true}
  - {name: shop-oncall, token_sha256: "<hash>", infra_owner: shop-team, environments: [prod], can_approve: true}
```

`cloudinfra.url` must be `https` unless both services share the host (`localhost` or `127.0.0.1`): each environment's
token travels on every call. Keyed token hashes work here as in section 3, with the pepper in
`APPDELIVERY_TOKEN_PEPPER`. A Cloud Run release can bind a secret only when the service's or job's block names it in
`secrets` on the `cloudinfra` side; the plan says so otherwise.

```bash
app-delivery check-config appdelivery.local.yaml
```

```bash
APPDELIVERY_DEV_INFRA_TOKEN=… APPDELIVERY_CONFIG=appdelivery.local.yaml uvicorn appdelivery.server:create_app --factory --host 127.0.0.1 --port 8200
```

On the platform machine, run the `appdelivery` image from the same build by its digest, mount the configuration at
`/config/appdelivery.yaml` and a volume at `/data`, pass the token variables, and run it as the machine's service
account (which holds Token Creator on each environment's account and reads the image's repository).

The first build, as the team (a copy of `examples/build.yaml` with a real commit, as JSON); `built` in the answer is
the digest to release, once `status` is `SUCCEEDED` (`GET /builds/{id}` follows it):

```bash
curl -s -X POST "http://127.0.0.1:8200/builds?wait_seconds=45" -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" --data-binary @build.json
```

The first release, as the team, onto the request made in section 3 (put its id and the built digest in a copy of
`examples/release.yaml`; any digest this service did not build is refused with `NOT_BUILT_HERE`):

```bash
app-delivery plan release.yaml --config appdelivery.local.yaml --as shop-team
```

```bash
curl -s -X POST http://127.0.0.1:8200/releases -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" --data-binary @release.json
```

A release in an environment with `require_approval` waits in `AWAITING_APPROVAL` for `POST /releases/{id}/approve`
by a principal with `can_approve` other than the one that asked. `GET /releases/{id}` follows the rollout; `GET
/running?request_id=…` says what runs on each target now; `POST /releases/{id}/rollback` puts the previous release
back; `DELETE /releases/{id}` removes it. On a cluster a release is `{cluster: NAME, workload: NAME, replicas: N}`,
optionally `expose: {hostname, path}` through the cluster's front door; on a Cloud Run service `{service: NAME}`; on
a job `{job: NAME}`, then `POST /releases/{id}/runs` starts it.

## 5. Connect Claude

Claude chat signs in to a connector with OAuth only. For each service:

1. Serve it on a public HTTPS address Claude's servers reach (a TLS proxy or load balancer in front of the port).
2. Add the host to `allowed_hosts` and turn sign-in on:

   ```yaml
   allowed_hosts: ["infra.example.com"]
   sign_in:
     url: https://infra.example.com
   ```

3. In Claude: Customize, Connectors, Add custom connector, URL `https://infra.example.com/mcp`. Claude registers
   itself, the browser opens the service's sign-in page, and the person pastes the token of the principal Claude
   should act as. Claude then has that principal's rights for an hour at a time, renewed for thirty days.

Give each person their own principal (`scope: all` without `can_destroy` for someone who only looks). A new token for
the principal, or its removal, ends every sign-in made with it. Claude Code can use the same sign-in or a bearer header
(`claude mcp add --transport http NAME https://HOST/mcp --header "Authorization: Bearer $TOKEN"`).

## 6. Day to day

- **Tokens.** Rotate by generating a new one and replacing the hash; a principal's old token stops at the next
  request. The delivery service's environment tokens live only in the variables the configuration names.
- **State.** One SQLite file per service, in WAL mode, with a lock file beside it; one process per file. Back it up by
  copying the file while the service is stopped, or with a tool that reads WAL files consistently; a stale copy
  restored over a live one loses what happened since.
- **Logs.** `log_file` gives one JSON line per event with the request or release id and the identity; secret-looking
  fields are redacted. Every call is also an audit event in the state (`events`).
- **Time.** Nothing stops or expires unless a request asks or the operator sets `limits.default_lifetime_hours`,
  `max_lifetime_hours` or `default_autostop_idle_minutes`; leave them unset in production. The sweeper runs every
  `sweep_interval_seconds` and warns 48 and 2 hours before an expiry.
- **Upgrades.** Pull the new version, rebuild the image or update the checkout, restart. At start the infrastructure
  service recovers requests it was interrupted on; the delivery service marks interrupted releases failed and the next
  deploy puts each target right. Read [CHANGES.md](CHANGES.md) for what a version changes for callers.
- **Leaving.** Destroy requests through the service, so grants, credentials and node accounts go with them; a
  resource created outside the service is never touched by it.
