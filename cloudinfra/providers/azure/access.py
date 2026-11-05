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
NAMES = {VM_USER_LOGIN: "Virtual Machine User Login", VM_ADMIN_LOGIN: "Virtual Machine Administrator Login", READER: "Reader",
         BLOB_CONTRIBUTOR: "Storage Blob Data Contributor", BLOB_OWNER: "Storage Blob Data Owner"}
# role tables: level -> roles; a revoke removes every role of the table, whichever level was granted
ROLES = {
    "machine": {"use": (VM_USER_LOGIN,), "admin": (VM_ADMIN_LOGIN,)},
    "interface": {"use": (READER,), "admin": (READER,)},  # the Bastion reads a machine's interface to reach it
    "bucket": {"use": (BLOB_CONTRIBUTOR,), "admin": (BLOB_OWNER,)},
}
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


class AzureAccess:
    def __init__(self, api, cloud):
        self.api, self.cloud = api, cloud

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

    def assign(self, scope: str, principal: dict, role: str, description: str) -> None:
        body = {"properties": {"roleDefinitionId": f"/subscriptions/{self.api.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/{role}",
                               "principalId": principal["id"], "principalType": principal["type"], "description": description[:1024]}}
        url = self._url(scope, assignment_name(scope, principal["id"], role))
        for attempt in range(NOT_YET[0]):
            try:
                self.api.call("PUT", url, json=body, params={"api-version": VERSION})
                return
            except InfrastructureError as exc:
                reason = exc.details.get("reason")
                if reason == "RoleAssignmentExists":  # the same grant under another name, made by hand or before: it is there
                    return
                if reason != "PrincipalNotFound" or attempt == NOT_YET[0] - 1:
                    raise
                self.api.sleep(NOT_YET[1])

    def unassign(self, scope: str, principal: dict, role: str) -> None:
        self.api.delete(self._url(scope, assignment_name(scope, principal["id"], role)), VERSION)

    def grant(self, targets: list[tuple[str, str]], principal: dict, level: str, description: str) -> list[str]:
        """Assign the level's roles of each (scope, table); the roles' names, in order."""
        granted = []
        for scope, table in targets:
            for role in ROLES[table][level]:
                self.assign(scope, principal, role, description)
                granted.append(NAMES[role])
        return granted

    def revoke(self, targets: list[tuple[str, str]], principal: dict) -> None:
        """Remove every role this service grants on each scope from one principal; another role or principal is never touched."""
        for scope, table in targets:
            for role in sorted({role for roles in ROLES[table].values() for role in roles}):
                self.unassign(scope, principal, role)
