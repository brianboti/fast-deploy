"""Release S2 of the security assessment of 2026-10-30 (docs/research/RESEARCH-2026-10-30-security-assessment.md, section 7):
the provider defaults the benchmarks expect, infrastructure side. Each test names the finding it holds."""
import pytest
from gcp_fakes import config_dict
from test_infra_release_a import orchestrated, provision

from cloudinfra.config import Config
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.providers.gcp.kubernetes import POD_SECURITY, TENANT, no_public_entry
from cloudinfra.providers.gcp.network import GOOGLE_DOMAINS, PRIVATE_GOOGLE_RANGE

CLUSTER = {"name": "shop", "network": {}, "cluster": {}}


def created_cluster(fake) -> dict:
    [body] = fake.clusters.values()
    return body


# ---- M6: Pod Security on tenant namespaces, NodePort refused
def test_a_tenant_namespace_enforces_pod_security_baseline(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, CLUSTER)
    [cluster] = response.resources.cluster
    labels = fake.namespace_labels[(cluster.endpoint, cluster.namespace)]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline" and labels["pod-security.kubernetes.io/warn"] == "restricted"
    assert TENANT.items() <= labels.items() and POD_SECURITY.items() <= labels.items()


def test_the_admission_policy_refuses_a_node_port_service():
    [(_, policy), _] = no_public_entry({})
    messages = {item["message"].split(" is refused")[0]: item["expression"] for item in policy["spec"]["validations"]}
    assert "'NodePort'" in messages["A Service of type NodePort"] and "has(object.spec.type)" in messages["A Service of type NodePort"]


# ---- M7: the cluster's own defaults
def test_a_cluster_is_private_with_dataplane_v2_posture_and_binary_authorization_by_default(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    provision(orchestrator, who, CLUSTER)
    body = created_cluster(fake)
    assert body["privateClusterConfig"] == {"enablePrivateNodes": True, "enablePrivateEndpoint": False}
    assert body["networkConfig"]["datapathProvider"] == "ADVANCED_DATAPATH" and body["securityPostureConfig"] == {"mode": "BASIC"}
    assert body["binaryAuthorization"] == {"evaluationMode": "PROJECT_SINGLETON_POLICY_ENFORCE"}
    assert "masterAuthorizedNetworksConfig" not in body, "no ranges listed: the endpoint stays reachable, behind IAM"


def test_the_operator_lists_who_reaches_the_control_plane_and_may_turn_binary_authorization_off(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"cluster_authorized_networks": ["203.0.113.0/24"], "binary_authorization": False})
    provision(orchestrator, who, CLUSTER)
    body = created_cluster(fake)
    assert body["masterAuthorizedNetworksConfig"] == {"enabled": True, "gcpPublicCidrsAccessEnabled": False,
                                                      "cidrBlocks": [{"cidrBlock": "203.0.113.0/24", "displayName": "operator"}]}
    assert "binaryAuthorization" not in body


def test_a_request_may_still_ask_for_public_nodes(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    provision(orchestrator, who, {**CLUSTER, "cluster": {"private_nodes": False}})
    assert "privateClusterConfig" not in created_cluster(fake)


def test_private_nodes_off_the_requests_network_are_warned_about():
    request = InfrastructureRequest.model_validate({"region": "us-central1", "cluster": {}})
    from cloudinfra.orchestration.planner import warnings
    assert any("not on this request's network with its NAT" in item for item in warnings(request, []))


@pytest.mark.parametrize("value", ["203.0.113.7", "0.0.0.0/33", "not-a-range"])
def test_authorized_networks_must_be_ranges(value):
    data = config_dict()
    data["providers"]["gcp"]["cluster_authorized_networks"] = [value]
    with pytest.raises(ValueError, match="cluster_authorized_networks"):
        Config.model_validate(data)


# ---- M10: flow logs, firewall logging, NAT, and egress denied on request
def test_a_network_logs_its_flows_and_rules_and_has_a_nat(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"name": "web", "network": {"public_access": True}})
    [network] = response.resources.network
    [subnet] = fake.subnets.values()
    assert subnet["logConfig"] == {"enable": True, "aggregationInterval": "INTERVAL_5_MIN", "flowSampling": 0.5, "metadata": "INCLUDE_ALL_METADATA"}
    assert fake.firewalls and all(rule["logConfig"] == {"enable": True} for rule in fake.firewalls.values())
    router = fake.regional["routers"][network.router]
    assert network.nat and router["nats"][0]["sourceSubnetworkIpRangesToNat"] == "ALL_SUBNETWORKS_ALL_IP_RANGES" and router["nats"][0]["natIpAllocateOption"] == "AUTO_ONLY"
    orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert not fake.regional["routers"] and not fake.networks and not fake.firewalls


def test_logs_off_and_no_nat_when_asked(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"network_logs": False})
    response = provision(orchestrator, who, {"name": "web", "network": {"nat": False}})
    [subnet] = fake.subnets.values()
    assert "logConfig" not in subnet and all("logConfig" not in rule for rule in fake.firewalls.values())
    assert response.resources.network[0].router is None and not fake.regional.get("routers")


def test_denied_egress_leaves_private_ranges_and_googles_private_apis(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"deny_egress": True})
    response = provision(orchestrator, who, {"name": "web", "network": {}})
    [network] = response.resources.network
    deny = fake.firewalls[f"{network.name}-deny-egress"]
    allow = fake.firewalls[f"{network.name}-allow-egress-private"]
    assert deny["direction"] == "EGRESS" and deny["denied"] == [{"IPProtocol": "all"}] and deny["destinationRanges"] == ["0.0.0.0/0"] and deny["priority"] == 65000
    assert allow["direction"] == "EGRESS" and PRIVATE_GOOGLE_RANGE in allow["destinationRanges"] and allow["priority"] < deny["priority"]
    assert network.router is None and not network.nat, "nowhere for a NAT to send anything"
    policy = fake.response_policies[network.dns_policy]
    assert policy["networks"] == [{"networkUrl": network.provider_id}] and set(policy["rules"]) == {"private", *GOOGLE_DOMAINS}
    assert policy["rules"]["googleapis"]["localData"]["localDatas"][0]["rrdatas"] == ["private.googleapis.com."]
    orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert not fake.response_policies and not fake.networks and not fake.firewalls


# ---- M11: one operator switch over every public path
PUBLIC_PATHS = [({"network": {"public_access": True}}, "network.public_access"),
                ({"compute": {"cpu": 2, "memory_gb": 4, "public_ip": True}}, "compute.public_ip"),
                ({"compute": {"cpu": 2, "memory_gb": 4}, "load_balancer": {}}, "load_balancer"),
                ({"cluster": {"public": {}}}, "cluster.public"),
                ({"database": {"public_access": True}}, "database.public_access"),
                ({"service": {"ingress": "all"}}, "service.ingress")]


@pytest.mark.parametrize(("body", "field"), PUBLIC_PATHS, ids=[item[1] for item in PUBLIC_PATHS])
def test_every_public_path_is_refused_unless_the_operator_allows_exposure(body, field):
    from gcp_fakes import make_provider

    from cloudinfra.orchestration import validator
    request = InfrastructureRequest.model_validate({"region": "us-central1", **body})
    closed = Config.model_validate({**config_dict(), "allow_public_exposure": False})
    problems = validator.validate(request, closed, make_provider(closed)[0])
    assert [item.field for item in problems if "allow_public_exposure" in item.message] == [field]
    opened = Config.model_validate(config_dict())
    assert not [item for item in validator.validate(request, opened, make_provider(opened)[0]) if "allow_public_exposure" in item.message]


def test_the_switch_is_off_by_default():
    data = config_dict()
    del data["allow_public_exposure"]
    assert Config.model_validate(data).allow_public_exposure is False


def test_configure_firewall_from_a_public_range_needs_the_switch_too(tmp_path):
    from test_infra_release_a import refused

    from cloudinfra.models.errors import ErrorCode
    orchestrator, fake, _, who = orchestrated(tmp_path, allow_public_exposure=False)
    network_id = orchestrator.create_network(who, region="us-central1", name="shared", public_access=False, wait_seconds=45).resources.network[0].id
    error = refused(ErrorCode.INVALID_NETWORK_CONFIGURATION, orchestrator.configure_firewall, network_id, [443], ["0.0.0.0/0"], who)
    assert error.details["field"] == "sources" and "allow_public_exposure" in error.message
    assert orchestrator.configure_firewall(network_id, [443], ["10.20.0.0/16"], who).resource.firewall_rules[-1].sources == ["10.20.0.0/16"]


# ---- M12: the load balancer
BALANCED = {"name": "web", "network": {"public_access": True}, "compute": {"group": "web", "count": 2, "cpu": 2, "memory_gb": 4}}


def test_a_load_balancer_with_hostnames_serves_https_with_a_modern_tls_policy_and_redirects_http(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"public_domains": ["example.com"], "cloud_armor_policy": "edge"})
    response = provision(orchestrator, who, {**BALANCED, "load_balancer": {"port": 8080, "hostnames": ["shop.example.com"]}})
    [balancer] = response.resources.load_balancer
    name = balancer.name
    assert fake.sslCertificates[f"{name}-cert"]["managed"]["domains"] == ["shop.example.com"] and balancer.certificate == "PROVISIONING"
    assert fake.sslPolicies[f"{name}-tls"]["profile"] == "MODERN" and fake.sslPolicies[f"{name}-tls"]["minTlsVersion"] == "TLS_1_2"
    https = fake.targetHttpsProxies[f"{name}-https"]
    assert https["sslPolicy"].endswith(f"/sslPolicies/{name}-tls") and https["sslCertificates"][0].endswith(f"/sslCertificates/{name}-cert")
    assert fake.forwardingRules[f"{name}-rule"]["portRange"] == "443" and fake.forwardingRules[f"{name}-http-rule"]["portRange"] == "80"
    assert fake.urlMaps[f"{name}-redirect"]["defaultUrlRedirect"] == {"httpsRedirect": True, "redirectResponseCode": "MOVED_PERMANENTLY_DEFAULT"}
    backend = fake.backendServices[f"{name}-backend"]
    assert backend["logConfig"] == {"enable": True, "sampleRate": 1.0} and backend["securityPolicy"] == "projects/test-project/global/securityPolicies/edge"
    assert balancer.hostnames == ["shop.example.com"]
    orchestrator.destroy(response.request_id, who, wait_seconds=45)
    for store in ("sslCertificates", "sslPolicies", "targetHttpsProxies", "targetHttpProxies", "urlMaps", "forwardingRules", "backendServices"):
        assert not getattr(fake, store), store


def test_without_hostnames_it_stays_plain_http_but_logged(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {**BALANCED, "load_balancer": {"port": 8080}})
    name = response.resources.load_balancer[0].name
    assert not fake.sslCertificates and not fake.targetHttpsProxies and fake.forwardingRules[f"{name}-rule"]["portRange"] == "8080"
    assert fake.backendServices[f"{name}-backend"]["logConfig"]["enable"] and "securityPolicy" not in fake.backendServices[f"{name}-backend"]
    assert any("give it hostnames in the operator's domains for HTTPS" in item for item in response.warnings)


def test_load_balancer_hostnames_must_be_in_the_operators_domains_and_http_only(tmp_path):
    from gcp_fakes import make_provider

    from cloudinfra.orchestration import validator
    config = Config.model_validate({**config_dict(), "providers": {"gcp": {"project_id": "test-project", "public_domains": ["example.com"]}}})
    request = InfrastructureRequest.model_validate({"region": "us-central1", **BALANCED, "load_balancer": {"hostnames": ["shop.example.org"]}})
    [problem] = [item for item in validator.validate(request, config, make_provider(config)[0]) if item.field == "load_balancer.hostnames"]
    assert "shop.example.org" in problem.message
    with pytest.raises(ValueError, match="only an http load balancer terminates TLS"):
        InfrastructureRequest.model_validate({"region": "us-central1", **BALANCED, "load_balancer": {"type": "tcp", "hostnames": ["shop.example.com"]}})


# ---- M13: Cloud SQL
@pytest.mark.parametrize(("engine", "flags"), [("postgresql", {"cloudsql.iam_authentication": "on", "cloudsql.enable_pgaudit": "on", "log_connections": "on",
                                                               "log_disconnections": "on", "log_min_messages": "warning"}),
                                               ("mysql", {"cloudsql_iam_authentication": "on", "local_infile": "off", "skip_show_database": "on"})])
def test_a_database_carries_the_cis_flags_iam_sign_in_and_a_root_password_nobody_keeps(tmp_path, engine, flags):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"name": "db", "network": {}, "database": {"engine": engine}})
    [body] = fake.sql.values()
    sent = {item["name"]: item["value"] for item in body["settings"]["databaseFlags"]}
    assert flags.items() <= sent.items() and len(body["rootPassword"]) >= 32 and "diskEncryptionConfiguration" not in body
    assert body["rootPassword"] not in str(orchestrator.state.recent_events(limit=500)) and body["rootPassword"] not in response.model_dump_json()


def test_the_operators_key_for_the_region_encrypts_the_database(tmp_path):
    key = "projects/keys-project/locations/us-central1/keyRings/platform/cryptoKeys/data"
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"kms_keys": {"us-central1": key}})
    provision(orchestrator, who, {"name": "db", "network": {}, "database": {}})
    [body] = fake.sql.values()
    assert body["diskEncryptionConfiguration"] == {"kmsKeyName": key}


def test_a_key_must_be_in_its_region():
    data = config_dict()
    data["providers"]["gcp"]["kms_keys"] = {"europe-west1": "projects/keys-project/locations/us-central1/keyRings/platform/cryptoKeys/data"}
    with pytest.raises(ValueError, match="a key encrypts only resources in its own region"):
        Config.model_validate(data)


# ---- M14: machines
def test_a_machine_has_no_serial_port_and_is_protected_until_an_explicit_delete(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"name": "vm", "compute": {"cpu": 2, "memory_gb": 4}})
    [body] = fake.instances.values()
    items = {item["key"]: item["value"] for item in body["metadata"]["items"]}
    assert items == {"enable-oslogin": "TRUE", "block-project-ssh-keys": "TRUE", "serial-port-enable": "FALSE"} and body["deletionProtection"] is True
    orchestrator.destroy(response.request_id, who, wait_seconds=45)  # the fake refuses a delete while protection is on
    assert not fake.instances


def test_a_machine_never_runs_as_an_admin_identity():
    body = {"region": "us-central1", "identity": {"name": "app", "level": "admin"}, "compute": {"cpu": 2, "memory_gb": 4, "identity": "app"}}
    with pytest.raises(ValueError, match="machines run only as an identity at use"):
        InfrastructureRequest.model_validate(body)
    InfrastructureRequest.model_validate({**body, "identity": {"name": "app", "level": "use"}})


# ---- M15: the one project IAM write, only from an account limited to it
SERVICE = "serviceAccount:cloudinfra@test-project.iam.gserviceaccount.com"


def with_identity(fake, member):
    token = fake.token
    fake.token = type("Token", (), {"__call__": lambda self: token(), "identity": lambda self: member})()


@pytest.mark.parametrize("condition", [None, {"title": "any", "expression": "true"}])
def test_an_account_that_could_grant_any_role_is_refused_before_project_iam_is_touched(tmp_path, condition):
    from cloudinfra.models.errors import ErrorCode
    orchestrator, fake, _, who = orchestrated(tmp_path)
    binding = {"role": "roles/resourcemanager.projectIamAdmin", "members": [SERVICE], **({"condition": condition} if condition else {})}
    fake.policies[("project",)] = {"version": 3, "etag": "1", "bindings": [binding]}
    with_identity(fake, SERVICE)
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", **CLUSTER}), who, wait_seconds=45)
    assert response.error is not None and response.error.details["cause"] == ErrorCode.PROVIDER_PERMISSION_DENIED.value
    assert "without the condition" in response.error.message and fake.policies[("project",)]["etag"] == "1", "the policy was not written"
    assert not fake.service_accounts, "the node account made for it was removed again"


def test_the_conditioned_role_passes_and_a_persons_sign_in_is_not_checked(tmp_path):
    from cloudinfra.providers.gcp.nodeaccount import CONDITION, NODE_ROLE
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.policies[("project",)] = {"version": 3, "etag": "1", "bindings": [
        {"role": "roles/resourcemanager.projectIamAdmin", "members": [SERVICE], "condition": {"title": "node role only", "expression": CONDITION}}]}
    with_identity(fake, SERVICE)
    provision(orchestrator, who, CLUSTER)
    assert any(item["role"] == NODE_ROLE for item in fake.policies[("project",)]["bindings"])
    orchestrator2, fake2, _, who2 = orchestrated(tmp_path / "person")
    fake2.policies[("project",)] = {"version": 3, "etag": "1", "bindings": [{"role": "roles/owner", "members": ["user:ops@example.com"]}]}
    with_identity(fake2, "user:ops@example.com")
    provision(orchestrator2, who2, CLUSTER)


def test_the_token_tells_its_own_identity():
    import httpx

    from cloudinfra.providers.gcp.auth import GoogleToken
    offline = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    assert GoogleToken(client=offline, impersonate="cloudinfra@p.iam.gserviceaccount.com").identity() == "serviceAccount:cloudinfra@p.iam.gserviceaccount.com"
    assert GoogleToken(client=offline, runner=lambda args: "ops@example.com").identity() == "user:ops@example.com"
    on_google = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="vm@p.iam.gserviceaccount.com")))
    assert GoogleToken(client=on_google).identity() == "serviceAccount:vm@p.iam.gserviceaccount.com"


# ---- L9: Cloud Run
def test_a_service_takes_internal_and_load_balancer_traffic_and_binary_authorization_by_default(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    provision(orchestrator, who, {"name": "api", "service": {}, "job": {}})
    [service], [job] = fake.run_services.values(), fake.run_jobs.values()
    assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" and service["binaryAuthorization"] == {"useDefault": True}
    assert job["binaryAuthorization"] == {"useDefault": True}


def test_a_runtime_account_reads_the_database_password_only_when_named(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"name": "api", "network": {}, "database": {}, "service": [{"name": "web", "secrets": ["db-password"]}, {"name": "worker"}]})
    services = {service.name: service for service in response.resources.service}
    [password] = [secret for secret in response.resources.secret if secret.purpose == "database_password"]
    assert services["api-web"].secrets == [password.resource_name] and services["api-worker"].secrets == []


def test_a_service_may_name_only_secrets_of_its_request():
    with pytest.raises(ValueError, match="service.secrets names db-password, not a secret of this request"):
        InfrastructureRequest.model_validate({"region": "us-central1", "service": {"secrets": ["db-password"]}})


# ---- L10: storage, secrets, registries, and the operator's key on each
KEY = "projects/keys-project/locations/us-central1/keyRings/platform/cryptoKeys/data"


def test_a_bucket_states_its_soft_delete_and_a_registry_reports_what_cleanup_would_take(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    provision(orchestrator, who, {"name": "data", "storage": {"type": "object", "size_gb": 10}, "registry": {"name": "apps"}, "secret": {"name": "token"}})
    [bucket] = fake.buckets.values()
    assert bucket["softDeletePolicy"] == {"retentionDurationSeconds": "604800"} and "encryption" not in bucket
    [registry] = fake.repository_bodies.values()
    [policy] = registry["cleanupPolicies"].values()
    assert registry["cleanupPolicyDryRun"] is True and policy["condition"] == {"tagState": "UNTAGGED", "olderThan": "7776000s"}
    [secret] = fake.secrets.values()
    assert secret["replication"] == {"automatic": {}}


def test_the_operators_key_reaches_every_store_of_the_region(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"kms_keys": {"us-central1": KEY}})
    provision(orchestrator, who, {"name": "data", "network": {}, "compute": {"cpu": 2, "memory_gb": 4}, "storage": {"size_gb": 10},
                                  "registry": {"name": "apps"}, "secret": {"name": "token"}, "cluster": {}})
    [instance] = fake.instances.values()
    [disk] = fake.disks.values()
    [registry] = fake.repository_bodies.values()
    [secret] = fake.secrets.values()
    [cluster] = fake.clusters.values()
    assert instance["disks"][0]["diskEncryptionKey"] == {"kmsKeyName": KEY} and disk["diskEncryptionKey"] == {"kmsKeyName": KEY} and registry["kmsKeyName"] == KEY
    assert secret["replication"] == {"userManaged": {"replicas": [{"location": "us-central1", "customerManagedEncryption": {"kmsKeyName": KEY}}]}}
    assert cluster["databaseEncryption"] == {"state": "ENCRYPTED", "keyName": KEY} and cluster["nodePools"][0]["config"]["bootDiskKmsKey"] == KEY


def test_a_bucket_takes_the_key_too(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"kms_keys": {"us-central1": KEY}})
    provision(orchestrator, who, {"name": "data", "storage": {"type": "object", "size_gb": 10}})
    [bucket] = fake.buckets.values()
    assert bucket["encryption"] == {"defaultKmsKeyName": KEY}


def test_a_secrets_policy_is_read_at_version_three(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    provision(orchestrator, who, {"name": "data", "secret": {"name": "token"}, "access": [{"member": "user:dev@example.com"}]})
    reads = [params for method, url, params in fake.params if method == "GET" and url.endswith(":getIamPolicy") and "secretmanager" in url]
    assert reads and all(params == {"options.requestedPolicyVersion": 3} for params in reads)


# ---- L8: a create whose answer was lost
@pytest.mark.parametrize(("body", "suffix", "store"), [({"compute": {"cpu": 2, "memory_gb": 4}}, "/instances", "instances"),
                                                       ({"network": {}}, "/global/networks", "networks"),
                                                       ({"cluster": {}, "network": {}}, "/clusters", "clusters")])
def test_a_create_google_took_but_never_answered_is_found_and_then_cleaned_up(tmp_path, body, suffix, store):
    orchestrator, fake, _, who = orchestrated(tmp_path, cleanup_on_failure=True)
    fake.lose_answer.append(("POST", suffix))
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "lost", **body}), who, wait_seconds=45)
    assert not fake.lose_answer, "the planted loss was reached"
    assert response.error is None, "found by its planned name, recorded, and carried on"
    orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert not getattr(fake, store), f"no orphan left in {store}"


def test_a_lost_create_that_google_never_did_keeps_the_first_error():
    import httpx

    from cloudinfra.models.errors import ErrorCode, InfrastructureError
    from cloudinfra.providers.gcp.api import GcpApi

    def answer(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("no answer", request=request)
        return httpx.Response(404, json={"error": {"message": "not found"}})
    api = GcpApi("test-project", token=lambda: "t", client=httpx.Client(transport=httpx.MockTransport(answer)), sleep=lambda seconds: None)
    with pytest.raises(InfrastructureError) as caught:
        api.create("https://compute.googleapis.com/compute/v1/projects/p/zones/z/instances", "https://compute.googleapis.com/compute/v1/projects/p/zones/z/instances/vm",
                   json={"name": "vm"})
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and caught.value.details["maybe_done"] is True
    connect = GcpApi("test-project", token=lambda: "t", sleep=lambda seconds: None,
                     client=httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request)))))
    with pytest.raises(InfrastructureError) as caught:
        connect.post("https://compute.googleapis.com/compute/v1/projects/p/zones/z/instances", json={})
    assert "maybe_done" not in caught.value.details, "a request that never left was not done"


# ---- L7: a keyed hash for principal tokens, the key kept outside the configuration
PEPPER = "p" * 40


def keyed_config(**principal):
    data = config_dict()
    data["principals"][0] = {"name": "app-layer", "can_provision": True, **principal}
    return Config.model_validate(data)


def test_a_keyed_hash_matches_only_with_the_pepper(monkeypatch):
    from cloudinfra.security.auth import find_principal, keyed
    config = keyed_config(token_hmac_sha256=keyed("the-token", PEPPER.encode()))
    monkeypatch.setenv("CLOUDINFRA_TOKEN_PEPPER", PEPPER)
    assert find_principal(config, "the-token").name == "app-layer" and find_principal(config, "other") is None
    monkeypatch.setenv("CLOUDINFRA_TOKEN_PEPPER", "q" * 40)
    assert find_principal(config, "the-token") is None, "another pepper, no match"
    assert find_principal(config, "reader-token").name == "reader", "plain hashes keep working beside keyed ones"


def test_the_service_will_not_start_with_keyed_hashes_and_no_pepper(tmp_path, monkeypatch):
    from cloudinfra.mcp.server import build_app
    from cloudinfra.security.auth import keyed
    monkeypatch.delenv("CLOUDINFRA_TOKEN_PEPPER", raising=False)
    config = keyed_config(token_hmac_sha256=keyed("the-token", PEPPER.encode()))
    config = config.model_copy(update={"database": str(tmp_path / "state.db")})
    with pytest.raises(ValueError, match="CLOUDINFRA_TOKEN_PEPPER"):
        build_app(config)
    monkeypatch.setenv("CLOUDINFRA_TOKEN_PEPPER", "short")
    with pytest.raises(ValueError, match="32 characters or more"):
        build_app(config)


def test_a_principal_has_one_hash_and_the_cli_prints_the_keyed_one(monkeypatch, capsys):
    from cloudinfra import cli
    with pytest.raises(ValueError, match="one of them"):
        keyed_config()
    with pytest.raises(ValueError, match="one of them"):
        keyed_config(token_sha256="0" * 64, token_hmac_sha256="1" * 64)
    monkeypatch.setenv("CLOUDINFRA_TOKEN_PEPPER", PEPPER)
    assert cli.main(["token"]) == 0
    printed = capsys.readouterr().out
    assert "token_hmac_sha256: " in printed and "token_sha256" not in printed


def test_a_sign_in_is_bound_to_the_keyed_hash_too():
    config = keyed_config(token_hmac_sha256="1" * 64)
    assert config.principals[0].credential == "1" * 64 and config.principals[1].credential == config.principals[1].token_sha256


# ---- L6: credentials by their shape, wherever they appear
@pytest.mark.parametrize("text", ["ya29.a0AfB_byC-secret", "Authorization: Bearer abc.def-ghi", "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
                                  "token=Zx9Kq2Lm8Np4Rt6Vw1Yb3Dc5Fg7Hj0Kl2Mn4Pq6Rs8Tu"])
def test_credential_shaped_text_is_redacted_in_any_field(text):
    from cloudinfra.observability.logging import redact
    assert "[redacted]" in redact({"detail": f"before {text} after"})["detail"]


def test_ids_digests_and_urls_are_kept():
    from cloudinfra.observability.logging import redact
    kept = ["sha256:" + "a" * 64, "https://compute.googleapis.com/compute/v1/projects/test-project/zones/us-central1-a/instances/infra-884f4198-vm-1",
            "0123456789abcdef0123456789abcdef", "rel-0a1b2c3d4e5f"]
    assert redact({"value": kept}) == {"value": kept}


def test_an_internal_error_logs_its_class_and_keeps_its_text_for_debug(caplog):
    from cloudinfra.security.auth import invoke

    def boom():
        raise RuntimeError("the database password is hunter2")
    with caplog.at_level("INFO", logger="cloudinfra"):
        result = invoke(None, "tool", "explode", boom)
    assert result["error"]["code"] == "INTERNAL_ERROR" and "hunter2" not in caplog.text and "RuntimeError" in caplog.text


# ---- L20 and L4: the sign-in's resource and issuer, and a budget per public path
@pytest.fixture
def sign_in_client(tmp_path):
    from fastapi.testclient import TestClient
    from gcp_fakes import make_config, make_provider
    from test_infra_sign_in import BASE

    from cloudinfra.mcp.server import build_app
    from cloudinfra.orchestration.provisioner import InlineExecutor
    from cloudinfra.providers import ProviderRegistry
    config = make_config(tmp_path, sign_in={"url": BASE})
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": make_provider(config)[0]}), executor=InlineExecutor())
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        yield client


def test_an_authorization_for_another_resource_is_refused_and_the_answer_names_its_issuer(sign_in_client):
    from urllib.parse import parse_qs, urlparse

    from test_infra_sign_in import BASE, CALLBACK, CHALLENGE, register
    served = sign_in_client
    client_id = register(served).json()["client_id"]
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    elsewhere = served.get("/authorize", params={**query, "resource": "https://other.example.com/mcp"})
    assert elsewhere.status_code == 302 and parse_qs(urlparse(elsewhere.headers["location"]).query)["error"] == ["invalid_request"]
    for resource in (BASE + "/mcp", BASE + "/mcp/", BASE):
        assert served.get("/authorize", params={**query, "resource": resource}).headers["location"].startswith(BASE + "/sign-in?login=")
    metadata = served.get("/.well-known/oauth-authorization-server").json()
    assert metadata["authorization_response_iss_parameter_supported"] is True and metadata["token_endpoint_auth_methods_supported"] == ["none"]


def test_the_redirect_back_carries_iss(sign_in_client):
    from urllib.parse import parse_qs, urlparse

    from test_infra_sign_in import BASE, register, sign_in_page
    served = sign_in_client
    client_id = register(served).json()["client_id"]
    back = served.post("/sign-in", data={"login": sign_in_page(served, client_id), "token": "test-token"})
    assert parse_qs(urlparse(back.headers["location"]).query)["iss"] == [BASE]


def test_one_public_path_running_out_leaves_the_others(tmp_path):
    from fastapi.testclient import TestClient
    from gcp_fakes import make_config, make_provider

    from cloudinfra.mcp.server import build_app
    from cloudinfra.providers import ProviderRegistry
    config = make_config(tmp_path, sign_in={"url": "http://localhost:8000"}, rate_limit_per_minute=3)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": make_provider(config)[0]}))
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as client:
        codes = [client.get("/.well-known/oauth-authorization-server").status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        assert client.get("/sign-in", params={"login": "x"}).status_code != 429, "the sign-in page has its own budget"
