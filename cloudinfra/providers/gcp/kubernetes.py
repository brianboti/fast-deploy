"""The GKE parts of a cluster's Kubernetes API: the RoleBindings that grant a request's members access in its namespace,
and the front door's Gateway. GKE accepts the service's Google access token as the bearer and authorizes it through IAM
(it created the cluster). The namespace, the admission policy and the client are every provider's
(cloudinfra/providers/kubernetes.py), imported here for the adapter and its tests."""
from ..kubernetes import (  # noqa: F401 - the shared names, as the GKE adapter and its tests import them
    ADMISSION,
    NO_PUBLIC_ENTRY,
    POD_SECURITY,
    SUBJECT_KINDS,
    TENANT,
    KubeApi,
    ca_context,
    no_public_entry,
    tenant_labels,
    tenant_namespace,
)

# The built-in ClusterRoles a level binds to, in the request's namespace only: view reads; edit also changes workloads,
# services, config maps and secrets, and cannot change RBAC or the namespace itself.
CLUSTER_ROLES = {"use": "view", "admin": "edit"}
GATEWAY_API = "/apis/gateway.networking.k8s.io/v1"
DOOR_NAMESPACE, DOOR = "cloudinfra-gateway", "public"
GATEWAYS = f"{GATEWAY_API}/namespaces/{DOOR_NAMESPACE}/gateways"
ROUTES = f"{GATEWAY_API}/namespaces/{DOOR_NAMESPACE}/httproutes"
GATEWAY_CLASS = "gke-l7-global-external-managed"
WRITE = ["get", "list", "watch", "create", "update", "patch", "delete"]


def routes_role(labels: dict[str, str]) -> tuple[str, dict]:
    """HTTPRoutes, and GKE's HealthCheckPolicies for the Services behind them, added to the built-in edit role by
    aggregation: Kubernetes' Gateway API ships no such rules, and a namespace with edit then attaches its workloads to the
    front door with the health check they need. Gateways stay out of edit."""
    return ("/apis/rbac.authorization.k8s.io/v1/clusterroles",
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
             "metadata": {"name": "cloudinfra-routes", "labels": {**labels, "rbac.authorization.k8s.io/aggregate-to-edit": "true"}},
             "rules": [{"apiGroups": ["gateway.networking.k8s.io"], "resources": ["httproutes"], "verbs": WRITE},
                       {"apiGroups": ["networking.gke.io"], "resources": ["healthcheckpolicies"], "verbs": WRITE}]})


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
