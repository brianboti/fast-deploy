"""Workload identities behind the generic identity primitives: a user-assigned managed identity made for the request,
with no secret and no certificate, holding the request's grants at its level as any access member does, and nothing
else (learn.microsoft.com/rest/api/managedidentity/user-assigned-identities/create-or-update). Machines of a group that
names it carry it; a workload there asks the metadata service for tokens with its client id. Its principal id is what
grants name; the orchestrator adds it to the request's members as serviceAccount:<principal id>.

In the request's clusters, the Kubernetes service account of its name in the request's namespace acts as it through
workload identity: each cluster adds a federated credential to it naming the cluster's OIDC issuer and that service
account (cluster.py), as soon as the cluster has an issuer, and removes it before the cluster goes
(learn.microsoft.com/azure/aks/workload-identity-overview)."""
from ...models.resources import IdentityResource, ResourceStatus
from ..kubernetes import tenant_namespace
from . import translate
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.ManagedIdentity"]


class AzureIdentity:
    def __init__(self, api, groups, ceiling):
        self.api, self.groups, self.ceiling = api, groups, ceiling

    def create(self, spec, planned, ctx) -> IdentityResource:
        group = self.groups.ensure(ctx)
        identity_id = f"{self.groups.id(group)}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{planned.name}"
        tags = translate.tags(ctx)
        resource = IdentityResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region,
                                    provider_id=identity_id, status=ResourceStatus.CREATING, level=spec.level, labels=tags)
        ctx.accepted(resource)
        made = self.api.put(self.api.arm(identity_id), {"location": ctx.region, "tags": tags}, VERSION, timeout=self.ceiling("identity"))
        properties = made.get("properties") or {}
        update = {"status": ResourceStatus.READY, "principal_id": properties.get("principalId"), "client_id": properties.get("clientId")}
        if planned.spec.get("kubernetes"):  # unique per request: an issuer and subject name one namespace, and request names repeat
            update["kubernetes_service_account"] = f"{tenant_namespace(ctx)}/{planned.spec['identity']}"
        return resource.model_copy(update=update)

    def get(self, resource: IdentityResource) -> IdentityResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        properties = found.get("properties") or {}
        return resource.model_copy(update={"status": ResourceStatus.READY, "principal_id": properties.get("principalId") or resource.principal_id,
                                           "client_id": properties.get("clientId") or resource.client_id})

    def delete(self, resource: IdentityResource, ctx) -> None:
        """The identity goes; the grants it held went with the resources, destroyed before it."""
        self.api.delete(self.api.arm(resource.provider_id), VERSION, timeout=self.ceiling("identity"))
