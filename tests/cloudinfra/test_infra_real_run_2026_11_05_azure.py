"""What the first real run on Azure found (2026-11-05, a student subscription in denmarkeast and canadacentral), one test
per finding; the client's relative Location is tested beside the client (test_infra_azure.py)."""
import io
import logging

from cloudinfra.observability import logging as observability


def test_an_event_is_written_once_though_a_library_put_a_handler_on_the_root_logger():
    root, service = logging.getLogger(), logging.getLogger("cloudinfra")
    library_output, own_output = io.StringIO(), io.StringIO()
    library = logging.StreamHandler(library_output)  # as the MCP SDK's RichHandler sits on the root logger in the service
    root.addHandler(library)
    try:
        observability.configure()
        streams = [(handler, handler.setStream(own_output)) for handler in service.handlers if type(handler) is logging.StreamHandler]
        try:
            observability.event("probe_event", value=1)
        finally:
            for handler, stream in streams:
                handler.setStream(stream)
        assert own_output.getvalue().count("probe_event") == 1, "the service's own handler writes it"
        assert "probe_event" not in library_output.getvalue(), "and nothing above it writes it again"
    finally:
        root.removeHandler(library)
        service.propagate = True


# ---- a size Azure does not offer the subscription is refused before anything is made

def sku(name, zones=("1", "2", "3"), restrictions=()):
    return {"resourceType": "virtualMachines", "name": name, "locationInfo": [{"location": "eastus2", "zones": list(zones)}], "restrictions": list(restrictions)}


def provisioned(tmp_path, skus, request):
    from azure_fakes import make_azure_config, make_azure_orchestrator
    tmp_path.mkdir(parents=True, exist_ok=True)
    from test_infra_azure_services import provision
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, limits={"max_monthly_cost_usd": 2000}))
    fake.skus = skus
    return provision(orchestrator, {"provider": "azure", "region": "eastus2", **request}), fake


def test_a_size_the_subscription_is_not_offered_is_refused_naming_the_sizes_it_is(tmp_path):
    """The student subscription was offered none of the v5 sizes in canadacentral; Azure said so only at the machine's create."""
    response, fake = provisioned(tmp_path, {"eastus2": [sku("Standard_B1s"), sku("Standard_B1ms"), sku("Standard_D2s_v6")]},
                                 {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}})
    assert response.error.details["cause"] == "INVALID_COMPUTE_CONFIGURATION"
    assert "Azure does not offer Standard_D2s_v5 to this subscription in eastus2 zone 1" in response.error.message
    assert "of the adapter's sizes it offers there Standard_B1ms, Standard_B1s" in response.error.message
    assert [method for method, _ in fake.order] == [], "nothing made"


def test_a_zone_a_size_is_held_back_in_is_refused_naming_the_zones_it_is_offered_in(tmp_path):
    held = {"type": "Zone", "values": ["eastus2"], "restrictionInfo": {"zones": ["1"]}, "reasonCode": "NotAvailableForSubscription"}
    response, fake = provisioned(tmp_path, {"eastus2": [sku("Standard_D2s_v5", restrictions=[held])]}, {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}})
    assert "in eastus2 zone 1; it does in zone 2, 3" in response.error.message and fake.order == []
    response, _ = provisioned(tmp_path / "zone2", {"eastus2": [sku("Standard_D2s_v5", restrictions=[held])]},
                              {"zone": "2", "network": {}, "compute": {"cpu": 2, "memory_gb": 8}})
    assert response.status.value == "READY", "another zone of the same size is offered"


def test_a_size_held_back_in_the_whole_region_and_a_clusters_nodes_are_checked_too(tmp_path):
    region = {"type": "Location", "values": ["eastus2"], "reasonCode": "NotAvailableForSubscription"}
    response, _ = provisioned(tmp_path, {"eastus2": [sku("Standard_D2s_v5", restrictions=[region])]}, {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}})
    assert "it offers none of the adapter's sizes there: try another region or zone" in response.error.message
    response, fake = provisioned(tmp_path / "cluster", {"eastus2": [sku("Standard_B1s")]}, {"network": {}, "cluster": {}})
    assert response.error.details["cause"] == "INVALID_CLUSTER_CONFIGURATION" and "Standard_D4s_v5" in response.error.message and fake.order == []


def test_a_listing_azure_does_not_answer_holds_nothing_up(tmp_path):
    response, _ = provisioned(tmp_path, None, {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}})
    assert response.status.value == "READY", "best effort, as quota: Azure still refuses at the create"


def test_a_requests_status_is_read_while_its_database_is_ready_and_its_password_secret_is_not_made_yet():
    """The status read crashed with IndexError during run 2: the database was READY, its db-password secret still a placeholder."""
    from azure_fakes import make_azure_config, make_azure_provider

    from cloudinfra.models.resources import DatabaseResource, ResourceStatus, SecretResource
    provider, _ = make_azure_provider(make_azure_config())
    secret = SecretResource(id="sec-0123456789ab", name="r2-db-password", request_id="0" * 32, provider="azure", region="eastus2", provider_id="",
                            resource_name="pending/r2-db-password", status=ResourceStatus.PENDING, purpose="database_password")
    database = DatabaseResource(id="db-0123456789ab", name="r2-db", request_id="0" * 32, provider="azure", region="eastus2", engine="postgresql", version="18",
                                tier="Standard_B1ms", storage_gb=32, port=5432, database="app", username="app", provider_id="x", status=ResourceStatus.READY,
                                host="r2-db.postgres.database.azure.com", password_secret=secret.id)
    info = provider.connect_info(database, [database, secret])
    assert "connect" in info.commands and "password" not in info.commands, "no command for a secret not made yet, and no crash"
    assert provider.connect_info(secret, [secret]).notes[-1] == "It is still being made"
    made = secret.model_copy(update={"resource_name": "https://r2-0123abcd.vault.azure.net/secrets/r2-db-password", "status": ResourceStatus.READY})
    assert "--vault-name r2-0123abcd" in provider.connect_info(database, [database, made]).commands["password"]


def test_a_load_balancer_whose_pool_azure_left_empty_fails_instead_of_serving_nowhere(tmp_path):
    """Run 4's load balancer was READY with an empty pool: Azure drops IP-based members named in the load balancer's own PUT."""
    from azure_fakes import make_azure_config, make_azure_orchestrator
    from test_infra_azure_services import provision
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, allow_public_exposure=True))
    original = fake._pool

    def keeps_nothing(path, key, name, resource, body, existing):
        original(path, key, name, resource, body, existing)
        resource["properties"]["loadBalancerBackendAddresses"] = []
    fake._pool = keeps_nothing
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "lb", "network": {}, "compute": {"count": 2, "cpu": 2, "memory_gb": 8},
                                        "load_balancer": {}})
    assert response.status.value == "FAILED" and "Azure kept 0 of the load balancer's 2 machines in its pool" in response.error.message


def test_a_zone_azure_still_holds_its_link_for_is_deleted_on_a_later_try(tmp_path):
    """Run 2's network was left: the redis zone's delete came a second after its link's and Azure still counted the link."""
    from azure_fakes import make_azure_config, make_azure_orchestrator
    from gcp_fakes import principal
    from test_infra_azure_services import provision
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path))
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "db", "network": {}, "database": {}, "cache": {}})
    assert response.status.value == "READY", response.error
    fake.zone_lag = 2
    destroyed = orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45)
    assert destroyed.status.value == "DESTROYED", destroyed.error
    assert fake.resources == {} and fake.zone_lag == 0


def test_a_purge_azure_answers_409_while_the_delete_runs_is_tried_again(tmp_path):
    """Every secret purge of the run met 409 at once (the secret's delete still running) and was left to the vault's purge."""
    from azure_fakes import make_azure_config, make_azure_orchestrator, refusal
    from test_infra_azure_services import provision

    from cloudinfra.providers.base import ProvisionContext
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path))
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "keys", "secret": {"name": "token", "generate_value": True}})
    [secret] = response.resources.secret
    vault = f"keys-{response.request_id[:8]}"
    fake.refuse[("DELETE", "/deletedsecrets/keys-token")] = [refusal(409, "Conflict", "Secret is currently being deleted")]
    provider = orchestrator.registry.get("azure")
    provider.delete_secret(secret, ProvisionContext(request_id=response.request_id, name="keys", region="eastus2", zone="1"))
    assert "keys-token" not in fake.deleted_secrets.get(vault, set()), "purged on the second try, not left soft-deleted for seven days"
