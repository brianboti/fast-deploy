"""What every provider's cluster adapter does on a cluster's own Kubernetes API, the same on GKE and AKS: one namespace
per request, the admission policy that keeps a request's namespace from opening a public address of its own, and the
few calls that make them. The provider gives the endpoint, the cluster's CA certificate and a token its control plane
accepts (Google's access token on GKE, an Entra token for AKS's server application on AKS); the server is checked
against the cluster's own CA. Nothing here deploys or reads anything an application put in the cluster.
"""
import base64
import ssl

import httpx

from ..models.errors import ErrorCode, InfrastructureError
from ..observability.logging import event

SUBJECT_KINDS = {"user": "User", "serviceAccount": "User", "group": "Group"}  # a RoleBinding names a Google identity by its email


def tenant_namespace(ctx) -> str:
    """The namespace a request gets in each of its clusters: its name plus the start of its id. Request names are not
    unique, and a workload identity's subject (PROJECT.svc.id.goog[NAMESPACE/NAME] on Google,
    system:serviceaccount:NAMESPACE:NAME on Azure) names the namespace, so a namespace named after the request alone would
    let another request of the same name act as this one's identities (found in the review of 2026-10-30)."""
    return f"{ctx.name}-{ctx.request_id[:6]}"


# The label on the namespaces made for requests. `edit` cannot change a namespace, so a team cannot take it off.
TENANT = {"cloudinfra-tenant": "true"}
# Pod Security admission on the same namespaces (https://kubernetes.io/docs/concepts/security/pod-security-standards/):
# baseline refuses privileged, hostNetwork, hostPID, hostPath and added capabilities beyond the default set, which `edit`
# could otherwise use to reach the node and its account's token; restricted is reported as a warning and in the audit.
# Labels on the namespace, which `edit` cannot change (found in the review of 2026-10-30, M6).
POD_SECURITY = {"pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/warn": "restricted", "pod-security.kubernetes.io/audit": "restricted"}


def tenant_labels(labels: dict[str, str]) -> dict[str, str]:
    """The labels a request's namespace carries: the caller's, the tenant mark the admission policy matches, and Pod Security."""
    return {**labels, **TENANT, **POD_SECURITY}


ADMISSION = "/apis/admissionregistration.k8s.io/v1"
NO_PUBLIC_ENTRY = "cloudinfra-no-public-entry"


def no_public_entry(labels: dict[str, str]) -> list[tuple[str, dict]]:
    """A ValidatingAdmissionPolicy and its binding (Kubernetes 1.30 and later, built in) that refuse, in a request's
    namespace, the three things that make a public load balancer on a managed cluster: a Service of type LoadBalancer, an
    Ingress and a Gateway; and a Service of type NodePort, which opens a port on every node. A public address is the
    infrastructure layer's to make (docs/DELIVERY-SPEC.md, phase 3)."""
    said = "is refused in a namespace the infrastructure layer made for a request: a public address is the infrastructure layer's to make"
    policy = {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy", "metadata": {"name": NO_PUBLIC_ENTRY, "labels": labels},
              "spec": {"failurePolicy": "Fail",
                       "matchConstraints": {"resourceRules": [
                           {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"], "resources": ["services"]},
                           {"apiGroups": ["networking.k8s.io"], "apiVersions": ["*"], "operations": ["CREATE", "UPDATE"], "resources": ["ingresses"]},
                           {"apiGroups": ["gateway.networking.k8s.io"], "apiVersions": ["*"], "operations": ["CREATE", "UPDATE"], "resources": ["gateways"]}]},
                       "validations": [
                           {"expression": "object.kind != 'Service' || !has(object.spec.type) || object.spec.type != 'LoadBalancer'",
                            "message": f"A Service of type LoadBalancer {said}"},
                           {"expression": "object.kind != 'Service' || !has(object.spec.type) || object.spec.type != 'NodePort'",
                            "message": f"A Service of type NodePort {said}"},
                           {"expression": "object.kind != 'Ingress'", "message": f"An Ingress {said}"},
                           {"expression": "object.kind != 'Gateway'", "message": f"A Gateway {said}"}]}}
    binding = {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": NO_PUBLIC_ENTRY, "labels": labels},
               "spec": {"policyName": NO_PUBLIC_ENTRY, "validationActions": ["Deny"], "matchResources": {"namespaceSelector": {"matchLabels": TENANT}}}}
    return [(f"{ADMISSION}/validatingadmissionpolicies", policy), (f"{ADMISSION}/validatingadmissionpolicybindings", binding)]


def ca_context(ca_certificate: str) -> ssl.SSLContext:
    """A TLS context that trusts only the cluster's own CA (base64-encoded PEM, as GKE gives it and a kubeconfig holds it)."""
    return ssl.create_default_context(cadata=base64.b64decode(ca_certificate).decode())


class KubeApi:
    def __init__(self, endpoint: str, ca_certificate: str, token, client: httpx.Client | None = None, provider: str = "gcp"):
        self.base, self.token, self.provider = f"https://{endpoint}", token, provider
        self.client = client or httpx.Client(verify=ca_context(ca_certificate), timeout=30, follow_redirects=False)

    def request(self, method: str, path: str, json=None, missing_ok: bool = False, exists_ok: bool = False) -> dict:
        for refreshed in (False, True):
            try:
                response = self.client.request(method, self.base + path, json=json, headers={"Authorization": "Bearer " + self.token()})
            except httpx.RequestError as exc:
                raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"The cluster's API could not be reached ({type(exc).__name__})", {"path": path}) from None
            event("provider_call", provider=self.provider, method=method, url=f"kubernetes:{path}", status=response.status_code)
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

    def put(self, collection: str, body: dict) -> None:
        """Create an object, or replace the one of its name with the version read."""
        path = f"{collection}/{body['metadata']['name']}"
        found = self.request("GET", path, missing_ok=True)
        if found:
            self.request("PUT", path, {**body, "metadata": {**body["metadata"], "resourceVersion": found["metadata"]["resourceVersion"]}})
        else:
            self.request("POST", collection, body)

    def delete(self, path: str) -> None:
        self.request("DELETE", path, missing_ok=True)

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
