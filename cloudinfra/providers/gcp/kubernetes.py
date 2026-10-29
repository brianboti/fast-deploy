"""The few calls the cluster adapter makes to a cluster's own Kubernetes API: one namespace per cluster, and the
RoleBindings that grant a request's members access in it. GKE accepts the service's Google access token as the bearer
and authorizes it through IAM (it created the cluster); the server is checked against the cluster's own CA certificate.
Nothing here deploys or reads anything an application put in the cluster."""
import base64
import ssl

import httpx

from ...models.errors import ErrorCode, InfrastructureError
from ...observability.logging import event

# The built-in ClusterRoles a level binds to, in the request's namespace only: view reads; edit also changes workloads,
# services, config maps and secrets, and cannot change RBAC or the namespace itself.
CLUSTER_ROLES = {"use": "view", "admin": "edit"}
SUBJECT_KINDS = {"user": "User", "serviceAccount": "User", "group": "Group"}  # GKE names a Google identity by its email


def ca_context(ca_certificate: str) -> ssl.SSLContext:
    """A TLS context that trusts only the cluster's own CA (GKE gives it base64-encoded PEM)."""
    return ssl.create_default_context(cadata=base64.b64decode(ca_certificate).decode())


class KubeApi:
    def __init__(self, endpoint: str, ca_certificate: str, token, client: httpx.Client | None = None):
        self.base, self.token = f"https://{endpoint}", token
        self.client = client or httpx.Client(verify=ca_context(ca_certificate), timeout=30, follow_redirects=False)

    def request(self, method: str, path: str, json=None, missing_ok: bool = False, exists_ok: bool = False) -> dict:
        for refreshed in (False, True):
            try:
                response = self.client.request(method, self.base + path, json=json, headers={"Authorization": "Bearer " + self.token()})
            except httpx.RequestError as exc:
                raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"The cluster's API could not be reached ({type(exc).__name__})", {"path": path}) from None
            event("provider_call", provider="gcp", method=method, url=f"kubernetes:{path}", status=response.status_code)
            if response.status_code != 401 or refreshed or not hasattr(self.token, "invalidate"):
                break
            self.token.invalidate()  # the token expired; one more try with a fresh one
        status = response.status_code
        if (status == 404 and missing_ok) or (status == 409 and exists_ok):
            return {}
        if status < 400:
            return response.json() if response.content else {}
        details = {"http_status": status, "path": path, "provider_message": response.text[:300]}
        codes = {401: ErrorCode.PROVIDER_AUTH_FAILED, 403: ErrorCode.PROVIDER_PERMISSION_DENIED, 404: ErrorCode.NOT_FOUND, 409: ErrorCode.PROVIDER_CONFLICT}
        code = codes.get(status, ErrorCode.PROVIDER_UNAVAILABLE if status >= 500 or status == 429 else ErrorCode.PROVIDER_ERROR)
        raise InfrastructureError(code, f"The cluster's API answered {status} to {method} {path}", details)

    def ensure_namespace(self, name: str, labels: dict[str, str]) -> None:
        self.request("POST", "/api/v1/namespaces", {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": labels}}, exists_ok=True)

    def put_role_binding(self, namespace: str, name: str, cluster_role: str, member: str, labels: dict[str, str]) -> None:
        """Create or replace one RoleBinding of one member to one built-in ClusterRole, in the namespace only."""
        prefix, _, email = member.partition(":")
        body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role},
                "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": SUBJECT_KINDS[prefix], "name": email}]}
        path = f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings"
        if self.request("GET", f"{path}/{name}", missing_ok=True):
            self.request("PUT", f"{path}/{name}", body)
        else:
            self.request("POST", path, body)

    def delete_role_binding(self, namespace: str, name: str) -> None:
        self.request("DELETE", f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings/{name}", missing_ok=True)
