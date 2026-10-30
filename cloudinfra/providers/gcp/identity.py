"""Workload identities behind the generic identity primitives: a service account made for the request, never given a key,
that holds the request's grants at its level as any access member does, and nothing else. Machines of a group run as it.
In the request's clusters, the Kubernetes service account of the same name in the request's namespace may act as it
through Workload Identity Federation for GKE (roles/iam.workloadIdentityUser for the principal
PROJECT.svc.id.goog[NAMESPACE/NAME] on the account), so a pod needs no key either
(https://docs.cloud.google.com/kubernetes-engine/docs/how-to/workload-identity)."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import IdentityResource, ResourceStatus
from . import translate
from .api import IAM
from .iam import GcpIam, update_policy, with_member
from .kubernetes import tenant_namespace
from .nodeaccount import NOT_YET, NodeAccounts

WORKLOAD_USER = "roles/iam.workloadIdentityUser"


def account_id(request_id: str, name: str) -> str:
    """id-<10 characters of the request id>-<the identity's name>: 26 characters at most, as IAM allows 30."""
    return f"id-{request_id[:10]}-{name}"


def email(project: str, request_id: str, name: str) -> str:
    return f"{account_id(request_id, name)}@{project}.iam.gserviceaccount.com"


class GcpIdentity:
    def __init__(self, api, settings):
        self.api, self.settings, self.accounts, self.iam = api, settings, NodeAccounts(api), GcpIam(api)

    def create(self, spec, planned, ctx) -> IdentityResource:
        name = planned.spec["identity"]
        found = self.accounts.create(account_id(ctx.request_id, name), f"Workload identity {name} of request {ctx.name}. {translate.description(ctx)}")
        resource = IdentityResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                    provider_id=f"projects/{self.api.project}/serviceAccounts/{found}", status=ResourceStatus.CREATING, email=found,
                                    level=spec.level, labels=translate.labels(ctx))
        ctx.accepted(resource)  # known from here: a failed binding still deletes the account
        if planned.spec.get("kubernetes"):
            namespace = tenant_namespace(ctx)  # unique per request: a principal is project-wide, a request name is not
            principal = f"serviceAccount:{self.api.project}.svc.id.goog[{namespace}/{name}]"
            for attempt in range(NOT_YET[0]):  # a new account is unknown to IAM policies for a few seconds
                try:
                    update_policy(self.iam._account(found), lambda policy: with_member(policy, WORKLOAD_USER, principal))
                    break
                except InfrastructureError as exc:
                    if "does not exist" not in exc.message or attempt == NOT_YET[0] - 1:
                        raise
                    self.api.sleep(NOT_YET[1])
            resource = resource.model_copy(update={"kubernetes_service_account": f"{namespace}/{name}"})
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: IdentityResource) -> IdentityResource:
        try:
            found = self.api.get(f"{IAM}/{resource.provider_id}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.FAILED if found.get("disabled") else ResourceStatus.READY})

    def delete(self, resource: IdentityResource, ctx) -> None:
        """The account goes, and with it its own policy; the grants it held went with the resources, destroyed before it."""
        self.accounts.remove(resource.email, [])
