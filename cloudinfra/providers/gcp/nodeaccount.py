"""Each cluster's nodes run as a service account made for that cluster (docs/DELIVERY-SPEC.md, phase 3), never the
Compute Engine default: it holds roles/container.defaultNodeServiceAccount on the project, the minimum GKE's nodes need
(https://docs.cloud.google.com/kubernetes-engine/docs/how-to/service-accounts), and roles/artifactregistry.reader on the
Artifact Registry repositories the request names, on nothing else. It is deleted with its cluster.

This needs the service's own account to create and delete service accounts, to act as the node accounts, and to grant
that one role on the project; the operator limits the last with an IAM condition on the roles it may grant
(https://cloud.google.com/iam/docs/setting-limits-on-granting-roles). Project IAM is touched for that role only."""
import re

from ...models.errors import ErrorCode, InfrastructureError
from ...observability.logging import event
from .api import ARTIFACT_REGISTRY, IAM, RESOURCE_MANAGER
from .iam import update_policy, with_member, without_member

NODE_ROLE = "roles/container.defaultNodeServiceAccount"
READER = "roles/artifactregistry.reader"
REGISTRY = re.compile(r"^([a-z0-9-]+)-docker\.pkg\.dev/([a-z][a-z0-9-]{4,28}[a-z0-9])/([a-z0-9][a-z0-9._-]{0,62})$")
# A service account just made is unknown to IAM policies for a few seconds; a grant naming it is tried again meanwhile.
NOT_YET = (12, 5)


def account_id(request_id: str, cluster_name: str) -> str:
    """node-<10 characters of the request id>-<the cluster's name>: 28 characters at most, as IAM allows 30."""
    return f"node-{request_id[:10]}-{cluster_name}"


def repository(registry: str) -> str:
    """LOCATION-docker.pkg.dev/PROJECT/REPO as Artifact Registry's resource name."""
    location, project, repo = REGISTRY.fullmatch(registry).groups()
    return f"projects/{project}/locations/{location}/repositories/{repo}"


class NodeAccounts:
    def __init__(self, api):
        self.api = api

    def _project(self):
        base = f"{RESOURCE_MANAGER}/projects/{self.api.project}"
        return (lambda: self.api.post(f"{base}:getIamPolicy", json={"options": {"requestedPolicyVersion": 3}}),
                lambda policy: self.api.post(f"{base}:setIamPolicy", json={"policy": policy}))

    def _repository(self, registry: str):
        base = f"{ARTIFACT_REGISTRY}/{repository(registry)}"
        return (lambda: self.api.get(f"{base}:getIamPolicy", **{"options.requestedPolicyVersion": 3}),
                lambda policy: self.api.post(f"{base}:setIamPolicy", json={"policy": policy}))

    def create(self, account: str, description: str) -> str:
        """Make the account (an earlier attempt's is reused) and return its email."""
        try:
            self.api.post(f"{IAM}/projects/{self.api.project}/serviceAccounts",
                          json={"accountId": account, "serviceAccount": {"displayName": account, "description": description[:256]}})
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
        return f"{account}@{self.api.project}.iam.gserviceaccount.com"

    def bind(self, email: str, registries: list[str]) -> None:
        member = f"serviceAccount:{email}"
        for endpoint, role in [(self._project(), NODE_ROLE), *[(self._repository(registry), READER) for registry in registries]]:
            for attempt in range(NOT_YET[0]):
                try:
                    update_policy(endpoint, lambda policy, role=role: with_member(policy, role, member))
                    break
                except InfrastructureError as exc:
                    if "does not exist" not in exc.message or attempt == NOT_YET[0] - 1:
                        raise
                    self.api.sleep(NOT_YET[1])

    def remove(self, email: str, registries: list[str]) -> None:
        """Take the account's roles away, then delete it; what is already gone is fine."""
        member = f"serviceAccount:{email}"
        for endpoint, role in [(self._project(), NODE_ROLE), *[(self._repository(registry), READER) for registry in registries]]:
            try:
                update_policy(endpoint, lambda policy, role=role: without_member(policy, member, roles={role}))
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
        try:
            self.api.delete(f"{IAM}/projects/{self.api.project}/serviceAccounts/{email}")
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def forget(self, email: str, registries: list[str]) -> None:
        """remove() after a failed create, where the create's own error is the one to report."""
        try:
            self.remove(email, registries)
        except InfrastructureError as exc:
            event("node_account_left", account=email, code=exc.code.value)
