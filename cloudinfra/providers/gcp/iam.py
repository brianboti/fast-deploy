"""Grants on the resources this service created, through each resource's own IAM policy. Never project IAM: what a member
needs at project level (a role with compute.projects.get for gcloud, cloudsql.client for the Cloud SQL Auth Proxy,
container.clusterViewer for cluster credentials) is the platform team's one-time setup, named in each connect block.

Each change reads the policy, changes only this service's roles for one member, and writes it back with the etag it read,
retrying a conflicting write. A machine's IAP tunnel grant is bound to the machine's numeric id, so a later machine with
the same name never inherits it."""
from ...models.errors import ErrorCode, InfrastructureError
from .api import COMPUTE, PUBSUB, SECRETS, STORAGE

IAP = "https://iap.googleapis.com/v1"
IAP_TUNNEL = "roles/iap.tunnelResourceAccessor"
ROLES = {
    "compute": {"use": ("roles/compute.osLogin",), "admin": ("roles/compute.osAdminLogin",)},
    "secret": {"use": ("roles/secretmanager.secretAccessor",), "admin": ("roles/secretmanager.secretAccessor", "roles/secretmanager.secretVersionAdder")},
    "bucket": {"use": ("roles/storage.objectUser",), "admin": ("roles/storage.objectAdmin",)},
    "topic": {"use": ("roles/pubsub.publisher",), "admin": ("roles/pubsub.publisher",)},
    "subscription": {"use": ("roles/pubsub.subscriber",), "admin": ("roles/pubsub.subscriber",)},
    "tunnel": {"use": (IAP_TUNNEL,), "admin": (IAP_TUNNEL,)},
}
# The only roles this service ever grants, and so the only ones it ever removes.
GRANTED_ROLES = frozenset(role for levels in ROLES.values() for roles in levels.values() for role in roles)
ATTEMPTS = 3


def same(one: str, other: str) -> bool:
    """Members compare without regard to case: Google does not tell case apart in account and group email addresses."""
    return one.lower() == other.lower()


def with_member(policy: dict, role: str, member: str) -> bool:
    """Add a member to a role's unconditional binding; True when the policy changed."""
    bindings = policy.setdefault("bindings", [])
    for binding in bindings:
        if binding.get("role") == role and not binding.get("condition"):
            members = binding.setdefault("members", [])
            if any(same(item, member) for item in members):
                return False
            members.append(member)
            return True
    bindings.append({"role": role, "members": [member]})
    return True


def without_member(policy: dict, member: str, roles=GRANTED_ROLES) -> bool:
    """Remove a member from the roles this service grants; another role or another member is never touched."""
    changed, kept = False, []
    for binding in policy.get("bindings", []):
        if binding.get("role") in roles and not binding.get("condition") and any(same(item, member) for item in binding.get("members", [])):
            binding["members"] = [item for item in binding["members"] if not same(item, member)]
            changed = True
        if binding.get("members"):
            kept.append(binding)
    policy["bindings"] = kept
    return changed


class GcpIam:
    def __init__(self, api):
        self.api = api

    # ---- one policy endpoint per kind of resource: (read, write)
    def _instance(self, resource):
        base = f"{COMPUTE}/projects/{self.api.project}/zones/{resource.zone}/instances/{resource.name}"
        return (lambda: self.api.get(f"{base}/getIamPolicy", optionsRequestedPolicyVersion=3),
                lambda policy: self.api.post(f"{base}/setIamPolicy", json={"policy": policy}))

    def _tunnel(self, resource):
        name = f"projects/{self.api.project_number()}/iap_tunnel/zones/{resource.zone}/instances/{resource.instance_id}"
        return (lambda: self.api.post(f"{IAP}/{name}:getIamPolicy", json={"options": {"requestedPolicyVersion": 3}}),
                lambda policy: self.api.post(f"{IAP}/{name}:setIamPolicy", json={"policy": policy}))

    def _secret(self, resource):
        return (lambda: self.api.get(f"{SECRETS}/{resource.resource_name}:getIamPolicy"),
                lambda policy: self.api.post(f"{SECRETS}/{resource.resource_name}:setIamPolicy", json={"policy": policy}))

    def _bucket(self, resource):
        return (lambda: self.api.get(f"{STORAGE}/b/{resource.name}/iam", optionsRequestedPolicyVersion=3),
                lambda policy: self.api.put(f"{STORAGE}/b/{resource.name}/iam", json=policy))

    def _pubsub(self, name):
        return (lambda: self.api.get(f"{PUBSUB}/{name}:getIamPolicy"),
                lambda policy: self.api.post(f"{PUBSUB}/{name}:setIamPolicy", json={"policy": policy}))

    def targets(self, resource) -> list[tuple]:
        """(endpoint, role table) pairs for a resource; empty for one that takes no grants (a disk, a network)."""
        kind = resource.kind.value
        if kind == "compute":
            return [(self._instance(resource), "compute"), (self._tunnel(resource), "tunnel")]
        if kind == "secret":
            return [(self._secret(resource), "secret")]
        if kind == "storage" and resource.type == "object":
            return [(self._bucket(resource), "bucket")]
        if kind == "queue":
            found = [(self._pubsub(resource.topic), "topic")]
            if resource.subscription:
                found.append((self._pubsub(resource.subscription), "subscription"))
            return found
        return []

    def _update(self, endpoint, mutate) -> None:
        read, write = endpoint
        for attempt in range(ATTEMPTS):
            policy = read() or {}
            if not mutate(policy):
                return
            try:
                write(policy)
                return
            except InfrastructureError as exc:
                if exc.code != ErrorCode.PROVIDER_CONFLICT or attempt == ATTEMPTS - 1:
                    raise

    def grant(self, resource, member: str, level: str) -> list[str]:
        granted = []
        for endpoint, table in self.targets(resource):
            roles = ROLES[table][level]
            self._update(endpoint, lambda policy, roles=roles: any([with_member(policy, role, member) for role in roles]))  # noqa: C419 - every role, no short circuit
            granted.extend(roles)
        return granted

    def revoke(self, resource, member: str) -> None:
        for endpoint, _ in self.targets(resource):
            try:
                self._update(endpoint, lambda policy: without_member(policy, member))
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
