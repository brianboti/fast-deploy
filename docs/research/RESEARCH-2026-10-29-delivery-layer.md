# A separate delivery service on top of the infrastructure layer: is that the right shape? 2026-10-29

Written 2026-10-29 UTC as a read-only review. Nothing in the code was changed. The question asked was the operator's:
the Application Layer should be able to say "now run this container on what you built for me"; we decided that this
belongs in a **small separate delivery service** that sits between the application teams and `cloudinfra`, rather than
inside `cloudinfra` or inside each team's own tooling. Is that choice right, by what the cloud vendors, the CNCF and the
delivery tools themselves say today?

Method: the reviewer read the primary pages listed in section 7 (the CNCF Platforms white paper, the OpenGitOps
principles, Google's Cloud Deploy documentation, Google's enterprise application blueprint, Google's Compute Engine
container pages, the AWS Deployment Pipeline Reference Architecture, the Score specification, Team Topologies' material
on the thinnest viable platform, Argo CD's core concepts) and compared each with the three-layer picture in
[INFRASTRUCTURE-ARCHITECTURE.md](../INFRASTRUCTURE-ARCHITECTURE.md) and with what `labdeploy` does today. Each source is
marked F (read on the page) or S (a search snippet or a secondary source).

## 1. The short answer

**The choice is right, and it is the mainstream shape.** Every primary source that describes a company platform draws
the same three bands we drew: the application teams own code, build and image; a delivery service moves an image
through environments onto infrastructure that already exists; the infrastructure is provisioned by something else. The
clearest confirmation is that Google sells exactly this middle band as its own product, Cloud Deploy, which "automates
delivery of your applications to a series of target environments" from "references to specific container images", with
targets that are existing clusters and services, never infrastructure it creates (F, section 2).

Two findings change the plan rather than confirm it:

1. **The way we would have put a container on a Compute Engine machine is gone.** Google's container startup agent and
   the `gce-container-declaration` metadata key stopped working on 2026-07-31, three months ago; support for machines that
   still use it ends 2026-07-31 a year later. Google's replacement is a startup script or `cloud-init` on
   Container-Optimized OS, or GKE, Cloud Run or Batch (F, section 4). The delivery service must be designed for the
   replacement from the start.
2. **The delivery service's own identity is the thing to design most carefully.** Cloud Deploy's documentation spends
   most of its security guidance on the execution service account: one per target, a separate one for production, the
   default Compute Engine account explicitly discouraged (F, section 2). The same holds for ours, and it fits what
   `cloudinfra` already does: the delivery service is a principal whose `identity` is granted on each request's resources
   and nothing else.

## 2. What the sources say about the shape

**Google Cloud Deploy (F).** "A managed service that automates delivery of your applications to a series of target
environments in a defined promotion sequence." Its input is a release created from "references to specific container
images to deploy"; "any CI tool can supply the images". Targets "represent GKE clusters, Cloud Run services or jobs, or
GKE attached clusters", or a custom target type; the overview never mentions creating infrastructure. Promotion,
per-target approval, rollback ("triggering a rollout against the last successfully deployed release") and verification
are the service's own concerns. Custom target types are "a render task and a deploy task", each "a container image to
run, and commands to run on that container", executed by Cloud Build with an execution service account per target. This
is the strongest external validation of our choice: Google itself separates build (Cloud Build), delivery (Cloud Deploy)
and provisioning (Terraform, in its blueprint), and the delivery piece takes an image reference and an existing target.

**Google's enterprise application blueprint (F).** Three groups: a developer platform team, one application team per
application, a security operations team. A "multi-tenant infrastructure pipeline" in its own project deploys tenant
infrastructure; each application has "its own CI/CD pipeline, in independent projects to enable separation between
development teams"; the platform team "approving the promotion of applications to the nonproduction environment and
the production environment"; Binary Authorization for image attestation; a separate identity pool per environment.
Infrastructure and application delivery are separate pipelines with separate identities, and a platform team sits
between them with approvals. That is our three bands with the company's names on them.

**CNCF Platforms white paper (F).** A platform is "an integrated collection of capabilities defined and presented
according to the needs of the platform's users"; it lists "build and delivery automation" and "infrastructure" as
distinct capability domains; it should be "the thinnest reasonable layer that provides consistency across provided
implementations"; platform teams "should not always implement" capabilities themselves and should "build their own
capabilities only when they are not available elsewhere"; platforms "separate supporting capabilities from
application-specific logic". Two consequences: a small service is the right size, and the delivery service should use
the cloud's own delivery primitives (Cloud Deploy, GKE Deployments, cloud-init) rather than reimplement them.

**Team Topologies, thinnest viable platform (S).** The platform should be the smallest set of APIs, documentation and
tools that reduces the developers' cognitive load; the first question for a platform leader is "should this platform
exist?". A delivery service exists if it removes a real step from every application team; it does, since otherwise every
team writes its own way to get an image onto a machine or cluster, and nobody knows which version runs where.

**OpenGitOps principles (F).** Desired state "expressed declaratively", "stored in a way that enforces immutability,
versioning and retains a complete version history", "pulled automatically", "continuously reconciled". Argo CD's core
concepts (F) describe only manifests, a target state in Git and a sync to a cluster; the tool "consumes existing files
rather than building anything". A GitOps-style delivery layer never builds: it takes a declared desired state whose
image is a reference. This is the "image reference, never source" rule we set.

**AWS Deployment Pipeline Reference Architecture (F).** Four pipeline types: application, compute image, account fleet
management, dynamic configuration; "the source code should only be built and packaged once", the artifact "staged in a
registry with appropriate metadata"; one application pipeline deploys to every environment; human approvals "before
production deployments only"; each environment "an isolated target" in its own account. Again build once, deploy the
artifact, separate pipelines by concern.

**Score (S).** A CNCF Sandbox workload specification: "developers declare what their workload requires to run. The
platform in the target environment is responsible for resolving individual runtime requirements"; the file "captures
the configuration that stays the same across all environments" and resource needs are placeholders such as
`${resources.db.host}` that the platform fills in. This is close to what the delivery service's input could look like:
a container, its ports, and names of resources that `cloudinfra` already created and whose addresses and secrets the
delivery service resolves.

**Supply chain (S; NIST SP 800-204D's text could not be read as a PDF).** The consistent pattern across SLSA, Kyverno,
Sigstore's policy controller and Argo CD's guidance is: pin the image by digest, verify its provenance before it runs,
enforce at the point of deployment. `labdeploy` already does this for Cloud Run (`provenance.py`, `attest.py`); the
delivery service inherits the requirement.

## 3. The verdict on each part of our choice

| Our decision | Sources | Verdict |
|---|---|---|
| Deploy is a layer of its own, not in `cloudinfra` | Cloud Deploy exists as a separate product; AWS DPRA separates pipelines by concern; CNCF lists delivery and infrastructure as distinct capabilities; blueprint has separate infrastructure and application pipelines | Confirmed |
| Deploy is not left to each application team | CNCF "thinnest reasonable layer that provides consistency"; Team Topologies "reduce cognitive load"; blueprint's platform team approves promotion | Confirmed |
| Input is an image reference, never source or a Dockerfile | Cloud Deploy releases are "references to specific container images"; GitOps desired state; AWS "built and packaged once" | Confirmed |
| It runs as its own principal with grants only on each request's resources | Cloud Deploy's execution service account per target, production separate, default account discouraged; blueprint's separate identity pool per environment | Confirmed, and the part to design most carefully |
| It owns update, rollback and application health | Cloud Deploy owns promotion, approval, rollback, verification | Confirmed |
| It is small | CNCF "build their own capabilities only when they are not available elsewhere" | Confirmed, with a consequence: it should drive the cloud's delivery primitives, not replace them (section 5) |
| It puts a container on a Compute Engine machine | Google's container startup agent shut down 2026-07-31 | **Changed:** must use `cloud-init` or a startup script on Container-Optimized OS, or a cluster |

## 4. The Compute Engine finding, in detail (F)

Google's page "Prepare for the shutdown of the container startup agent": from **2026-07-31** "workflows that rely on the
container startup agent or the `gce-container-declaration` metadata key stop working" and "you can no longer create
new VMs or MIGs that deploy containers this way"; from **2027-07-31** running machines that use it get "no further
updates". Terraform or any automation that sets the key is affected; `gcloud compute instances create-with-container`
and `update-container` and the console's "Deploy container" are deprecated. Container-Optimized OS itself stays:
"Container-Optimized OS images remain available and supported. You must switch to a startup script or `cloud-init` to
deploy containers." Google's own ranking of alternatives: a startup script or `cloud-init` for a single-machine
workload, testing and development; Cloud Run for stateless services and small jobs; Batch for jobs with an end; GKE "for
advanced control and scalability". An organisation policy constraint, `compute.managed.disableVmsWithContainerStartupAgent`,
blocks new machines that use the old key.

What this means for us: a `cloudinfra` machine is a plain Ubuntu, Debian or operator-profile image today; the delivery
service would reach it through the access `cloudinfra` grants (OS Login and the IAP tunnel) and run the container with
Docker or Podman through a startup script or `cloud-init`, which is exactly Google's replacement. Two shapes are possible
and both stay on Google's supported path:

- **Declare at creation.** The delivery service asks `cloudinfra` for a machine from a Container-Optimized OS profile and
  passes a `cloud-init` document that starts the container; later versions are a new `cloud-init` and a restart, or a
  replacement machine. This needs one new, bounded field in `cloudinfra`'s compute request: the `cloud-init` text, which
  is a provider-neutral "first boot configuration" that every cloud takes (AWS and Azure take the same format). It is
  still a step over the line we drew, since `cloudinfra` would carry a document that runs on the machine, and it would
  need a test that the document is opaque to the service.
- **Reach after creation.** `cloudinfra` stays exactly as it is; the delivery service connects over the IAP tunnel as its
  own identity and runs `docker run` (or `podman`), the way Google's startup-script path does at boot. Nothing in
  `cloudinfra` changes; the delivery service holds a login grant, which is what the access model was built for.

The second keeps `cloudinfra`'s guarantee ("the infrastructure service cannot execute anything on your machines") intact
and is the recommendation. The first is faster for fleets of identical machines and can come later as an opt-in profile.

## 5. What the delivery service should and should not be

Taking the CNCF's "thinnest reasonable layer" and "do not implement what is available elsewhere" seriously:

- **Targets, in order.** Corrected the same day in [DELIVERY-SPEC.md](../DELIVERY-SPEC.md): machines over the IAP tunnel
  first, then GKE clusters created by `cloudinfra`, and Cloud Run never. `cloudinfra` has no Cloud Run kind, so a Cloud
  Run target would not run on its resources and would only duplicate `labdeploy`. Managed instance groups are not a
  `cloudinfra` kind today and wait.
- **Input.** An image reference pinned by digest, a port, environment variable names bound to `cloudinfra` secrets and
  endpoints (the Score pattern), the `request_id` whose resources it runs on, and the environment. Never a repository,
  Dockerfile, build or shell command. This is `labdeploy`'s first deployment invariant already ("callers cannot supply an
  arbitrary repository, workflow, shell command, image, cloud account or local path"), kept but with the image reference
  allowed because it is now the artifact boundary.
- **Identity.** One principal in `cloudinfra`'s configuration per environment of the delivery service, with `identity`
  set to its service account and `scope: own`; `cloudinfra` grants it `use` on the request's machines, secrets and
  clusters. Production's principal is separate, as Cloud Deploy and the blueprint both insist.
- **Verification before deploy.** The provenance check `labdeploy` has (`provenance.py`) runs on every image before it
  reaches any target; a digest without attestation is refused. Binary Authorization where the target supports it.
- **State.** Which release runs on which request, when, by whom, and the previous one for rollback: its own SQLite,
  the same shape `labdeploy` uses for plans and receipts. Never in `cloudinfra`'s state.
- **Not its job.** Building images (CI), provisioning (`cloudinfra`), the application's own observability beyond the
  health and bake it needs to decide a rollout, and choosing machine sizes.

Decided the same day: a new package, and `labdeploy` is neither used nor touched; it is to be removed, so the delivery
service has no dependency on it ([DELIVERY-SPEC.md](../DELIVERY-SPEC.md)). The rest of this section records the reasoning
as it stood before that decision.

Whether this is a new package beside `labdeploy` or `labdeploy` with new targets is a packaging question the sources do
not settle. The honest reading of the code is that `labdeploy` is already a delivery service for one target, with the
GitHub and analysis parts being its CI front end. The cleanest cut would move the delivery core (release, promotion,
probe, bake, provenance, receipts) into the new service and leave repository analysis and GitHub dispatch as an optional
CI front end, so that "an image from anywhere" and "an image labdeploy built" go through one path.

## 6. What this review did not do

It did not read NIST SP 800-204D (the PDF had no text layer for the fetch tool); its guidance is reported from
secondary summaries. It did not test any `cloud-init` or IAP path against a real project; `cloudinfra`'s own access and
connect blocks have not yet run against one either. It did not measure Cloud Deploy's custom target path for a Compute
Engine machine; Google's quickstart "doesn't deploy an application to a runtime" and no official Compute Engine sample
was found, so using Cloud Deploy as the engine for machine targets is possible but unproven.

## 7. Sources

- F https://tag-app-delivery.cncf.io/whitepapers/platforms/ — CNCF Platforms white paper: capabilities, thinnest reasonable layer, platform as a product.
- F https://opengitops.dev/ — OpenGitOps principles v1.0.0.
- F https://argo-cd.readthedocs.io/en/stable/core_concepts/ — Argo CD core concepts: manifests, target state, sync.
- F https://docs.cloud.google.com/deploy/docs/overview — Cloud Deploy overview: releases from image references, targets, promotion, rollback.
- F https://docs.cloud.google.com/deploy/docs/deploy-app-custom-target — Cloud Deploy custom target types: render and deploy tasks on Cloud Build.
- F https://docs.cloud.google.com/deploy/docs/execution-environment — Cloud Deploy execution environment: worker pool, service account, per-target configuration.
- S https://docs.cloud.google.com/deploy/docs/cloud-deploy-service-account — execution service account per target, production separate, default account discouraged.
- F https://docs.cloud.google.com/architecture/blueprints/enterprise-application-blueprint/developer-platform-controls — Google's enterprise application blueprint: platform team, tenant teams, separate pipelines and projects, approvals, Binary Authorization.
- F https://docs.cloud.google.com/compute/docs/containers/prepare-for-container-agent-shutdown — the container startup agent shutdown: 2026-07-31 and 2027-07-31, replacements.
- F https://docs.cloud.google.com/compute/docs/containers/configuring-options-to-run-containers — running containers on Compute Engine with startup scripts and `cloud-init`; deprecation of `create-with-container`.
- F https://aws-samples.github.io/aws-deployment-pipeline-reference-architecture/ — AWS Deployment Pipeline Reference Architecture: pipeline types, build once, environments.
- S https://docs.score.dev/docs and https://github.com/score-spec/spec — Score workload specification.
- S https://teamtopologies.com/key-concepts-content/what-is-a-thinnest-viable-platform-tvp — thinnest viable platform.
- S https://csrc.nist.gov/pubs/sp/800/204/d/final — NIST SP 800-204D, reported from summaries only.
- S https://oneuptime.com/blog/post/2026-03-13-how-to-implement-supply-chain-levels-for-software-artifacts-slsa-with-flux/view and https://argo-cd.readthedocs.io/en/latest/operator-manual/signed-release-assets/ — verify provenance by digest before deployment.
