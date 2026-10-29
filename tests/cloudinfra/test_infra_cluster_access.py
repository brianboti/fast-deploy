"""Clusters take grants (docs/DELIVERY-SPEC.md, phase 2): one namespace per cluster, named after the request, and each
member bound in it to the built-in ClusterRole view (use) or edit (admin) through a RoleBinding; never an IAM role that
would reach every cluster of the project."""
import base64
import datetime
import json

import httpx
import pytest
from gcp_fakes import make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.providers.gcp.cluster import binding_name
from cloudinfra.providers.gcp.kubernetes import KubeApi, ca_context

DELIVERY = "serviceAccount:appdelivery-dev@example.com"  # in the fake configuration's allowed domain
GOOGLE_SA = "serviceAccount:appdelivery-dev@test-project.iam.gserviceaccount.com"
DATA = "group:data@example.com"
ENDPOINT = "35.201.0.7"


def setup(tmp_path, access=None):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    body = {"region": "us-central1", "name": "shop", "cluster": {"node_count": 1}}
    if access:
        body["access"] = access
    response = orchestrator.provision(InfrastructureRequest.model_validate(body), who, wait_seconds=45)
    assert response.error is None, response.error
    return orchestrator, fake, who, response


def bindings(fake):
    return {(namespace, item["role"], item["member"]) for (_, namespace, _), item in fake.role_bindings.items()}


def test_a_cluster_gets_one_namespace_named_after_its_request(tmp_path):
    _, fake, _, response = setup(tmp_path)
    [cluster] = response.resources.cluster
    assert cluster.namespace == "shop" and fake.namespaces == {ENDPOINT: {"shop"}}
    assert cluster.ca_certificate == "RkFLRSBDQQ==" and cluster.endpoint == ENDPOINT


def test_members_are_bound_in_the_namespace_at_their_level(tmp_path):
    _, fake, _, response = setup(tmp_path, access=[{"member": DELIVERY, "level": "admin"}, {"member": DATA, "level": "use"}])
    assert bindings(fake) == {("shop", "edit", DELIVERY), ("shop", "view", DATA)}
    [cluster] = response.resources.cluster
    assert {(grant.member, grant.role) for grant in cluster.access} == {(DELIVERY, "kubernetes:edit"), (DATA, "kubernetes:view")}
    assert all(entry.status == "GRANTED" and cluster.id in entry.resources for entry in response.access)


def test_a_level_change_leaves_one_binding_and_a_revoke_none(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path)
    orchestrator.manage_access(response.request_id, "grant", DELIVERY, who, level="admin")
    assert bindings(fake) == {("shop", "edit", DELIVERY)}
    orchestrator.manage_access(response.request_id, "grant", DELIVERY, who, level="use")
    assert bindings(fake) == {("shop", "view", DELIVERY)}, "a lower level takes edit away"
    revoked = orchestrator.manage_access(response.request_id, "revoke", DELIVERY, who)
    assert bindings(fake) == set() and next(item for item in revoked.access if item.member == DELIVERY).status == "REVOKED"


def test_a_grant_the_cluster_refuses_is_recorded_on_the_member(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path)
    fake.kube_fail = InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, "The cluster's API answered 403", {"http_status": 403})
    after = orchestrator.manage_access(response.request_id, "grant", DELIVERY, who, level="admin")
    entry = next(item for item in after.access if item.member == DELIVERY)
    assert entry.status == "FAILED" and entry.error.code == ErrorCode.PROVIDER_PERMISSION_DENIED and after.status == "READY"


def test_the_connect_block_names_the_namespace(tmp_path):
    _, _, _, response = setup(tmp_path, access=[{"member": DELIVERY, "level": "admin"}])
    connect = response.resources.cluster[0].connect
    assert connect.commands["namespace"] == "kubectl config set-context --current --namespace shop"
    assert any("namespace shop only" in note for note in connect.notes)
    assert any("container.clusterViewer" in item for item in connect.prerequisites)


def test_destroying_the_request_needs_no_revoke(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path, access=[{"member": DELIVERY, "level": "admin"}])
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == "DESTROYED" and not fake.clusters


def test_binding_names_carry_no_address():
    name = binding_name("serviceAccount:Someone@Example.com", "admin")
    assert name.startswith("cloudinfra-admin-") and "example" not in name and name == binding_name("serviceAccount:someone@example.com", "admin")


# ---- the Kubernetes API client itself, against a fake HTTP server
def kube(handler):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    return KubeApi(ENDPOINT, "unused", lambda: "google-token", client=httpx.Client(transport=httpx.MockTransport(record))), seen


def test_a_namespace_that_exists_is_fine():
    api, seen = kube(lambda request: httpx.Response(409, json={"reason": "AlreadyExists"}))
    api.ensure_namespace("shop", {"managed-by": "cloudinfra"})
    assert seen[0].url == f"https://{ENDPOINT}/api/v1/namespaces" and seen[0].headers["Authorization"] == "Bearer google-token"
    assert json.loads(seen[0].content)["metadata"] == {"name": "shop", "labels": {"managed-by": "cloudinfra"}}


@pytest.mark.parametrize(("member", "kind", "name"), [(GOOGLE_SA, "User", "appdelivery-dev@test-project.iam.gserviceaccount.com"),
                                                      ("user:dev@example.com", "User", "dev@example.com"), (DATA, "Group", "data@example.com")])
def test_a_binding_is_created_with_the_members_kind(member, kind, name):
    api, seen = kube(lambda request: httpx.Response(404) if request.method == "GET" else httpx.Response(201, json={}))
    api.put_role_binding("shop", "cloudinfra-admin-x", "edit", member, {})
    assert [request.method for request in seen] == ["GET", "POST"]
    body = json.loads(seen[1].content)
    assert body["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "edit"}
    assert body["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name}] and body["metadata"]["namespace"] == "shop"


def test_an_existing_binding_is_replaced_and_a_missing_one_deleted_quietly():
    api, seen = kube(lambda request: httpx.Response(200, json={"metadata": {}}) if request.method in ("GET", "PUT") else httpx.Response(404))
    api.put_role_binding("shop", "b", "view", DATA, {})
    api.delete_role_binding("shop", "gone")
    assert [request.method for request in seen] == ["GET", "PUT", "DELETE"]


@pytest.mark.parametrize(("status", "code"), [(401, ErrorCode.PROVIDER_AUTH_FAILED), (403, ErrorCode.PROVIDER_PERMISSION_DENIED),
                                              (503, ErrorCode.PROVIDER_UNAVAILABLE), (422, ErrorCode.PROVIDER_ERROR)])
def test_the_clusters_refusals_have_codes(status, code):
    api, _ = kube(lambda request: httpx.Response(status, text="no"))
    with pytest.raises(InfrastructureError) as caught:
        api.ensure_namespace("shop", {})
    assert caught.value.code == code


def test_the_cluster_is_trusted_only_through_its_own_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "cluster-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    context = ca_context(base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode())
    assert [item["subject"] for item in context.get_ca_certs()] == [((("commonName", "cluster-ca"),),)]
