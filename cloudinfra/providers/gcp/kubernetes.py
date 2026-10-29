"""The few calls the cluster adapter makes to a cluster's own Kubernetes API: one namespace per cluster, the
RoleBindings that grant a request's members access in it, and the admission policy that keeps a request's namespace
from opening a public address of its own. GKE accepts the service's Google access token as the bearer
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
# The label on the namespaces made for requests. `edit` cannot change a namespace, so a team cannot take it off.
TENANT = {"cloudinfra-tenant": "true"}
ADMISSION = "/apis/admissionregistration.k8s.io/v1"
NO_PUBLIC_ENTRY = "cloudinfra-no-public-entry"


def no_public_entry(labels: dict[str, str]) -> list[tuple[str, dict]]:
    """A ValidatingAdmissionPolicy and its binding (Kubernetes 1.30 and later, built in) that refuse, in a request's
    namespace, the three things that make a public Google load balancer on GKE: a Service of type LoadBalancer, an
    Ingress and a Gateway. A public address is the infrastructure layer's to make (docs/DELIVERY-SPEC.md, phase 3)."""
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
                           {"expression": "object.kind != 'Ingress'", "message": f"An Ingress {said}"},
                           {"expression": "object.kind != 'Gateway'", "message": f"A Gateway {said}"}]}}
    binding = {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": NO_PUBLIC_ENTRY, "labels": labels},
               "spec": {"policyName": NO_PUBLIC_ENTRY, "validationActions": ["Deny"], "matchResources": {"namespaceSelector": {"matchLabels": TENANT}}}}
    return [(f"{ADMISSION}/validatingadmissionpolicies", policy), (f"{ADMISSION}/validatingadmissionpolicybindings", binding)]


GATEWAY_API = "/apis/gateway.networking.k8s.io/v1"
DOOR_NAMESPACE, DOOR = "cloudinfra-gateway", "public"
GATEWAYS = f"{GATEWAY_API}/namespaces/{DOOR_NAMESPACE}/gateways"
ROUTES = f"{GATEWAY_API}/namespaces/{DOOR_NAMESPACE}/httproutes"
GATEWAY_CLASS = "gke-l7-global-external-managed"


def routes_role(labels: dict[str, str]) -> tuple[str, dict]:
    """HTTPRoutes added to the built-in edit role by aggregation: Kubernetes' Gateway API ships no such rules, and a
    namespace with edit then attaches its workloads to the front door. Gateways stay out of edit."""
    return ("/apis/rbac.authorization.k8s.io/v1/clusterroles",
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
             "metadata": {"name": "cloudinfra-routes", "labels": {**labels, "rbac.authorization.k8s.io/aggregate-to-edit": "true"}},
             "rules": [{"apiGroups": ["gateway.networking.k8s.io"], "resources": ["httproutes"],
                        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]}]})


def front_door(tenant: str, address_name: str, certificate_name: str | None, labels: dict[str, str]) -> list[tuple[str, dict]]:
    """The Gateway on the reserved address, taking routes from the request's namespace only (matched by its name, which
    no tenant can change), and with a certificate a route that sends plain HTTP to HTTPS."""
    from_tenant = {"namespaces": {"from": "Selector", "selector": {"matchLabels": {"kubernetes.io/metadata.name": tenant}}}}
    if certificate_name:
        listeners = [{"name": "https", "protocol": "HTTPS", "port": 443, "allowedRoutes": from_tenant,
                      "tls": {"mode": "Terminate", "options": {"networking.gke.io/pre-shared-certs": certificate_name}}},
                     {"name": "http", "protocol": "HTTP", "port": 80, "allowedRoutes": {"namespaces": {"from": "Same"}}}]
    else:
        listeners = [{"name": "http", "protocol": "HTTP", "port": 80, "allowedRoutes": from_tenant}]
    found = [(GATEWAYS, {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "Gateway", "metadata": {"name": DOOR, "namespace": DOOR_NAMESPACE, "labels": labels},
                         "spec": {"gatewayClassName": GATEWAY_CLASS, "addresses": [{"type": "NamedAddress", "value": address_name}], "listeners": listeners}})]
    if certificate_name:
        found.append((ROUTES, {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": {"name": "to-https", "namespace": DOOR_NAMESPACE, "labels": labels},
                               "spec": {"parentRefs": [{"name": DOOR, "sectionName": "http"}],
                                        "rules": [{"filters": [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}]}]}}))
    return found


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
