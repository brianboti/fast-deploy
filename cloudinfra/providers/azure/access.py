"""Who is granted what on the resources this service made: members resolved to Entra object ids, and role assignments
on each resource's own scope, never on the subscription (docs/research/RESEARCH-2026-11-03-azure.md, section 4).

Members. A role assignment names an object id and a principal type, not an e-mail. The orchestrator asks for each
member's id once, before anything of a request is created, and keeps it with the request's access list
(CloudProvider.resolve_member), so a later grant or revoke never looks it up again and a renamed user keeps their grants.
user: and group: addresses are looked up in Microsoft Graph (User.Read.All and Group.Read.All, application permissions a
privileged administrator grants the service's identity once); a member already written as an object id is taken as it
is, for an operator who will not grant those, and for a service principal or managed identity, which has no address.

Grants. One role assignment per (resource scope, member, role), named by a GUID derived from the three, as Microsoft
suggests, so that a retry is the same assignment and a revoke deletes exactly what a grant made without listing. The
scopes are the resource's own: a machine and its network interface (the interface for the Bastion, which reads it), a
bucket's container. Azure's group scope would spend fewer of the subscription's 5,000 assignments, but the orchestrator
grants and revokes resource by resource, and a revoke on one machine must not take a member's access to the others.
principalType is always set, so that an identity made seconds ago is found (ARM waits for its replication instead of
answering PrincipalNotFound). A grant can take up to 10 minutes to apply (Storage and Key Vault say so).
"""
import re
import threading
import uuid
from urllib.parse import quote

from ...models.errors import ErrorCode, InfrastructureError
from ...security import validation
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.Authorization"]
# Built-in roles (learn.microsoft.com/azure/role-based-access-control/built-in-roles), by GUID and name.
VM_USER_LOGIN = "fb879df8-f326-4884-b1cf-06f3ad86be52"
VM_ADMIN_LOGIN = "1c0163c0-47e6-4577-8991-ea5c82e286e4"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
BLOB_OWNER = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
SECRETS_USER = "4633458b-17de-408a-b874-0445c86b69e6"
SECRETS_OFFICER = "b86a8fe4-44ce-4948-aee5-eccb2c155cd7"
BUS_SENDER = "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39"
BUS_RECEIVER = "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0"
REPOSITORY_READER = "b93aa761-3e63-49ed-ac28-beffa264f7ac"  # learn.microsoft.com/azure/role-based-access-control/built-in-roles/containers
REPOSITORY_WRITER = "2a1e307c-b015-4ebd-883e-5b7698a07328"
# step 5's, each read on its built-in roles page on 2026-11-05 (containers, networking)
CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"  # Azure Kubernetes Service Cluster User Role: the cluster's user kubeconfig, nothing in it
AKS_RBAC_READER = "7f6c6a51-bcf8-42ba-9220-52d62157d7db"
AKS_RBAC_WRITER = "a7ffa36f-339b-4b5c-8bdf-e2c188b2c0eb"
NETWORK_CONTRIBUTOR = "4d97b98b-1d4f-4787-a291-c67834d212e7"
APPS_OPERATOR = "f3bd1b5c-91fa-40e7-afe7-0c11d331232c"  # Container Apps Operator: its revisions, logs and console; never its configuration
JOBS_READER = "edd66693-d32a-450b-997d-0158c03976b0"
JOBS_CONTRIBUTOR = "4e3d2b60-56ae-4dc6-a233-09c8e5a82e68"
NAMES = {VM_USER_LOGIN: "Virtual Machine User Login", VM_ADMIN_LOGIN: "Virtual Machine Administrator Login", READER: "Reader",
         BLOB_CONTRIBUTOR: "Storage Blob Data Contributor", BLOB_OWNER: "Storage Blob Data Owner", SECRETS_USER: "Key Vault Secrets User",
         SECRETS_OFFICER: "Key Vault Secrets Officer", BUS_SENDER: "Azure Service Bus Data Sender", BUS_RECEIVER: "Azure Service Bus Data Receiver",
         REPOSITORY_READER: "Container Registry Repository Reader", REPOSITORY_WRITER: "Container Registry Repository Writer",
         CLUSTER_USER: "Azure Kubernetes Service Cluster User Role", AKS_RBAC_READER: "Azure Kubernetes Service RBAC Reader",
         AKS_RBAC_WRITER: "Azure Kubernetes Service RBAC Writer", NETWORK_CONTRIBUTOR: "Network Contributor", APPS_OPERATOR: "Container Apps Operator",
         JOBS_READER: "Container Apps Jobs Reader", JOBS_CONTRIBUTOR: "Container Apps Jobs Contributor"}
# role tables: level -> roles; a revoke removes every role of the table, whichever level was granted
ROLES = {
    "machine": {"use": (VM_USER_LOGIN,), "admin": (VM_ADMIN_LOGIN,)},
    "interface": {"use": (READER,), "admin": (READER,)},  # the Bastion reads a machine's interface to reach it
    "bucket": {"use": (BLOB_CONTRIBUTOR,), "admin": (BLOB_OWNER,)},
    "secret": {"use": (SECRETS_USER,), "admin": (SECRETS_OFFICER,)},  # at the secret's own scope, never the vault's
    "topic": {"use": (BUS_SENDER,), "admin": (BUS_SENDER,)},
    "subscription": {"use": (BUS_RECEIVER,), "admin": (BUS_RECEIVER,)},
    # on the operator's registry, conditioned to the request's repository prefix; a writer pushes, any other member pulls
    "registry": {"use": (REPOSITORY_READER,), "admin": (REPOSITORY_WRITER,)},
    # a cluster: its user kubeconfig at the cluster's scope, and Azure RBAC for Kubernetes at its request's namespace only
    # (learn.microsoft.com/azure/aks/manage-azure-rbac): Reader reads, Writer also changes workloads, services, config maps
    # and secrets, as Kubernetes' view and edit do
    "cluster": {"use": (CLUSTER_USER,), "admin": (CLUSTER_USER,)},
    "namespace": {"use": (AKS_RBAC_READER,), "admin": (AKS_RBAC_WRITER,)},
    # a container service: calling it is its Entra sign-in's allow-list (apps.py), not a role; admin also restarts and
    # activates its revisions, reads its logs and opens a console in it, and cannot change it or its sign-in (a new
    # revision is the delivery layer's): Container Apps Contributor could turn the sign-in off
    "app": {"use": (), "admin": (APPS_OPERATOR,)},
    # a job: use reads it and its runs; admin also starts and changes it. Azure's start takes an image and a command of the
    # caller's choosing, run as the job's identity, so starting is admin's (Jobs Operator would give it to use)
    "job": {"use": (JOBS_READER,), "admin": (JOBS_CONTRIBUTOR,)},
    # never a member's: a cluster's own control-plane identity, on the subnet its nodes join
    "subnet": {"use": (NETWORK_CONTRIBUTOR,), "admin": (NETWORK_CONTRIBUTOR,)},
}
# The data actions a repository role's condition limits to the request's prefix
# (learn.microsoft.com/azure/container-registry/container-registry-rbac-abac-repository-permissions).
REPOSITORY_ACTIONS = {REPOSITORY_READER: ("content/read", "metadata/read"),
                      REPOSITORY_WRITER: ("content/read", "metadata/read", "content/write", "metadata/write")}
REPOSITORY_NAME = "@Request[Microsoft.ContainerRegistry/registries/repositories:name] StringStartsWithIgnoreCase"
PREFIX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,200}$")  # a prefix this service made: no slash, no quote, nothing a condition could read otherwise
# The only roles this service assigns; the service's grant right is conditioned on this list (section 4.2).
GRANTED_ROLES = frozenset(role for levels in ROLES.values() for roles in levels.values() for role in roles)
PRINCIPAL_TYPES = {"user": "User", "group": "Group", "serviceAccount": "ServicePrincipal"}
NAMESPACE = uuid.UUID("5f0c7f43-3f7e-4d0e-9a35-0c1d2b7a9e11")  # this service's namespace for assignment names
NOT_YET = (6, 10)  # PrincipalNotFound for a principal made moments ago: tried again, this many times, this many seconds apart
GRAPH_DENIED = ("The service's identity cannot read the directory: grant it Microsoft Graph's User.Read.All and Group.Read.All application permissions once. "
                "Without them, only the operator's principals' identities written as object ids (user:<object id>) can be granted")


def assignment_name(scope: str, principal_id: str, role: str) -> str:
    """The deterministic name of one role assignment: the same scope, principal and role always give the same GUID."""
    return str(uuid.uuid5(NAMESPACE, f"{scope.lower()}|{principal_id.lower()}|{role.lower()}"))


def repository_condition(role: str, prefixes) -> str:
    """The ABAC condition that limits a repository role to the repositories under these prefixes (each ends in /, so
    that app does not also match app-old/)."""
    actions = " AND ".join(f"!(ActionMatches{{'Microsoft.ContainerRegistry/registries/repositories/{action}'}})" for action in REPOSITORY_ACTIONS[role])
    names = " OR ".join(f"{REPOSITORY_NAME} '{prefix}/'" for prefix in sorted(prefixes))
    return f"(({actions}) OR ({names}))"


def prefixes_of(condition: str | None) -> set[str]:
    """The prefixes a condition this service wrote names."""
    return set(re.findall(r"StringStartsWithIgnoreCase '([^'/]+)/'", condition or ""))


class AzureAccess:
    def __init__(self, api, cloud):
        self.api, self.cloud = api, cloud
        self._shared = threading.Lock()  # a registry assignment is shared by every request of a member: changed one at a time

    # ---- members
    def resolve(self, member: str) -> dict:
        """{"id", "type"} of a member in the directory; INVALID_ACCESS when it has none."""
        kind, rest = member.split(":", 1)
        if validation.is_object_id(member):
            return {"id": rest.lower(), "type": PRINCIPAL_TYPES[kind]}
        if kind == "serviceAccount":
            raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"{member}: on Azure a service principal or managed identity has no address; name it "
                                      "serviceAccount:<object id>", {"field": "access", "member": member})
        graph = self.cloud.graph.rstrip("/")
        try:
            if kind == "user":
                found = self._one(f"{graph}/v1.0/users/{quote(rest, safe='@')}", {"$select": "id,userPrincipalName"})
                if found is None:  # a guest's sign-in name differs from their address; their address is their mail
                    found = self._first(f"{graph}/v1.0/users", {"$filter": f"mail eq '{rest}'", "$select": "id,mail"}, member)
            else:
                found = self._first(f"{graph}/v1.0/groups", {"$filter": f"mail eq '{rest}'", "$select": "id,mail"}, member)
        except InfrastructureError as exc:
            if exc.code in (ErrorCode.PROVIDER_PERMISSION_DENIED, ErrorCode.PROVIDER_AUTH_FAILED):
                raise InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, GRAPH_DENIED, {**exc.details, "member": member}) from None
            raise
        if found is None or not found.get("id"):
            raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"{member}: Azure's directory has no such {kind}", {"field": "access", "member": member})
        return {"id": str(found["id"]).lower(), "type": PRINCIPAL_TYPES[kind]}

    def _one(self, url: str, params: dict) -> dict | None:
        try:
            return self.api.call("GET", url, params=params, audience=self.cloud.graph).body
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return None
            raise

    def _first(self, url: str, params: dict, member: str) -> dict | None:
        found = (self.api.call("GET", url, params=params, audience=self.cloud.graph).body or {}).get("value") or []
        if len(found) > 1:
            raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"{member}: several directory objects have this address; name it by object id",
                                      {"field": "access", "member": member, "matches": len(found)})
        return found[0] if found else None

    # ---- role assignments
    def _url(self, scope: str, name: str) -> str:
        return self.api.arm(f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}")

    def _body(self, principal: dict, role: str, description: str) -> dict:
        return {"properties": {"roleDefinitionId": f"/subscriptions/{self.api.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{role}",
                               "principalId": principal["id"], "principalType": principal["type"], "description": description[:1024]}}

    def _put(self, url: str, body: dict, conditioned: bool) -> None:
        for attempt in range(NOT_YET[0]):
            try:
                self.api.call("PUT", url, json=body, params={"api-version": VERSION})
                return
            except InfrastructureError as exc:
                reason = exc.details.get("reason")
                if reason == "RoleAssignmentExists" and conditioned:
                    # the same role for this member under another name, made by hand: its condition is not this service's to read
                    raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Azure already holds this repository role for this member on the operator's registry "
                                              "under an assignment this service did not make; remove it, then grant again", dict(exc.details)) from None
                if reason == "RoleAssignmentExists":  # the same grant under another name, made by hand or before: it is there
                    return
                if reason != "PrincipalNotFound" or attempt == NOT_YET[0] - 1:
                    raise
                self.api.sleep(NOT_YET[1])

    def assign(self, scope: str, principal: dict, role: str, description: str, prefix: str = "") -> None:
        url = self._url(scope, assignment_name(scope, principal["id"], role))
        if not prefix:
            self._put(url, self._body(principal, role, description), False)
            return
        # Azure keeps one assignment of a role per member and scope, so a member's repository role on the operator's registry
        # is one assignment whose condition names every prefix it holds: read, widened, written back, one change at a time
        with self._shared:
            current = self.api.find(url, VERSION)
            prefixes = prefixes_of(((current or {}).get("properties") or {}).get("condition"))
            if prefix in prefixes:
                return
            body = self._body(principal, role, "cloudinfra: repository prefixes of the requests this member is granted on")
            body["properties"].update(condition=repository_condition(role, prefixes | {prefix}), conditionVersion="2.0")
            self._put(url, body, True)

    def unassign(self, scope: str, principal: dict, role: str, prefix: str = "") -> None:
        url = self._url(scope, assignment_name(scope, principal["id"], role))
        if not prefix:
            self.api.delete(url, VERSION)
            return
        with self._shared:
            current = self.api.find(url, VERSION)
            prefixes = prefixes_of(((current or {}).get("properties") or {}).get("condition"))
            if prefix not in prefixes:
                return
            if prefixes == {prefix}:
                self.api.delete(url, VERSION)
                return
            body = self._body(principal, role, "cloudinfra: repository prefixes of the requests this member is granted on")
            body["properties"].update(condition=repository_condition(role, prefixes - {prefix}), conditionVersion="2.0")
            self._put(url, body, True)

    def grant(self, targets: list[tuple], principal: dict, level: str, description: str) -> list[str]:
        """Assign the level's roles of each (scope, table[, repository prefix]); the roles' names, in order."""
        granted = []
        for scope, table, *prefix in targets:
            if table == "registry" and not (prefix and PREFIX.fullmatch(prefix[0])):
                # never a registry-wide grant: the condition is what keeps a member inside the request's repositories
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"A registry grant needs the request's repository prefix; {prefix!r} is not one")
            for role in ROLES[table][level]:
                self.assign(scope, principal, role, description, *prefix)
                granted.append(NAMES[role])
        return granted

    def revoke(self, targets: list[tuple], principal: dict) -> None:
        """Remove every role this service grants on each scope from one principal; another role or principal is never touched."""
        for scope, table, *prefix in targets:
            for role in sorted({role for roles in ROLES[table].values() for role in roles}):
                self.unassign(scope, principal, role, *prefix)
