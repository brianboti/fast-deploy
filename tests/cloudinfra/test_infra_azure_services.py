"""The Azure adapter's managed services (docs/research/RESEARCH-2026-11-03-azure.md, section 10, step 4): secrets in a
vault per request, queues in a Service Bus namespace per request, a registry as a repository prefix in the operator's
registry, databases and caches private behind an endpoint. Every test runs against the fake Azure in azure_fakes.py."""
import pytest
from azure_fakes import (
    REGISTRY,
    REGISTRY_LOGIN,
    SUBSCRIPTION,
    TENANT,
    USERS,
    make_azure_config,
    make_azure_orchestrator,
    make_azure_provider,
    refusal,
)
from gcp_fakes import config_dict, principal

from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceStatus
from cloudinfra.models.errors import InfrastructureError
from cloudinfra.orchestration import planner
from cloudinfra.providers.azure.access import (
    BUS_RECEIVER,
    BUS_SENDER,
    REPOSITORY_READER,
    REPOSITORY_WRITER,
    SECRETS_OFFICER,
    assignment_name,
    prefixes_of,
)

BUILDER = "77777777-7777-4777-8777-777777777777"


def provision(orchestrator, request: dict, who=None):
    return orchestrator.provision(InfrastructureRequest.model_validate(request), who or principal(orchestrator.config), wait_seconds=45)


def puts(fake, kind: str) -> list[dict]:
    return [call[2] for call in fake.calls if call[0] == "PUT" and call[1].split("?")[0].split("/")[-2] == kind]


def operator_registry(fake):
    hub = REGISTRY.split("/providers/", 1)[0]
    fake.resources[hub.lower()] = {"id": hub, "name": "hub", "type": "Microsoft.Resources/resourceGroups", "_top": False, "tags": {}, "properties": {}}
    fake.resources[REGISTRY.lower()] = {"id": REGISTRY, "name": "platformimages", "type": "Microsoft.ContainerRegistry/registries", "_top": True, "tags": {},
                                        "properties": {"roleAssignmentMode": "AbacRepositoryPermissions", "loginServer": REGISTRY_LOGIN}}


def plan_of(provider, config, request: dict):
    request = InfrastructureRequest.model_validate(request)
    return provider.plan(request, planner.context(request, "0123456789abcdef0123456789abcdef", "app-layer", config, provider))


# ---- what validation refuses before any call

def test_what_azure_does_not_offer_is_refused_with_the_reason_before_any_call():
    provider, fake = make_azure_provider(make_azure_config())

    def problems(**blocks):
        return [(item.field, item.message) for item in provider.validate(InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", **blocks}))]
    assert problems(network={}, database={}) == [] and problems(network={}, database={"engine": "mysql"}) == []
    assert problems(network={}, database={"engine": "mysql", "version": "8.4"}) == [
        ("database.version", "Azure offers MySQL 8.4 only through a preview API, which this adapter does not use; ask for 8.0")]
    assert problems(network={}, database={"version": "13"})[0][1] == "PostgreSQL 13 and older run on Azure under paid extended support only"
    assert problems(network={}, database={"high_availability": True})[0][0] == "database.high_availability", "small is Burstable, with no standby"
    assert problems(network={}, database={"size": "medium", "high_availability": True}) == []
    assert problems(network={}, database={"username": "admin"})[0][1] == "'admin' is a name Azure keeps for itself; choose another username"
    assert problems(network={}, database={"username": "app_user"})[0][1] == "On Azure, a PostgreSQL administrator's name has letters and digits only"
    assert problems(network={}, database={"engine": "mysql", "username": "app_user"}) == []
    assert problems(network={}, cache={"version": "7.2"})[0][0] == "cache.version" and problems(network={}, cache={"memory_gb": 300}) == [], "Balanced_B350 holds it"
    assert problems(queue={"name": "jobs", "ack_deadline_seconds": 600})[0][0] == "queue.ack_deadline_seconds"
    assert problems(registry={"name": "apps"})[0][1].startswith("This operator names no container registry")
    with_registry, _ = make_azure_provider(make_azure_config(azure={"registry": REGISTRY}))
    assert with_registry.validate(InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", "registry": {"name": "apps"}})) == []
    assert "one by one after it is pushed" in with_registry.validate(InfrastructureRequest.model_validate(
        {"provider": "azure", "region": "eastus2", "registry": {"name": "apps", "immutable_tags": True}}))[0].message
    assert fake.calls == []


def test_the_plan_names_the_services_their_sizes_and_the_requests_own_credential_secrets():
    config = make_azure_config(azure={"registry": REGISTRY})
    provider, fake = make_azure_provider(config)
    planned = plan_of(provider, config, {"provider": "azure", "region": "eastus2", "name": "shop", "network": {},
                                         "database": {"storage_gb": 40, "size": "medium", "high_availability": True}, "cache": {"memory_gb": 4},
                                         "queue": [{"name": "orders"}, {"name": "mails", "subscription": False}], "secret": {"name": "api-key"},
                                         "registry": {"name": "apps"}})
    by_name = {item.name: item for item in planned}
    database, cache = by_name["shop-db-01234567"].spec, by_name["shop-01234567"].spec
    assert (database["tier"], database["sku_tier"], database["storage_gb"], database["high_availability_mode"], database["database_version"]) == \
        ("Standard_D2ds_v5", "GeneralPurpose", 64, "ZoneRedundant", "18"), "40 GB is raised to PostgreSQL's 64 GB size"
    assert (cache["tier"], cache["memory_gb"], cache["version"]) == ("Balanced_B5", 6, "7.4")
    assert [by_name["shop-orders"].spec["namespace"], by_name["shop-mails"].spec["namespace"]] == [True, False]
    assert [item.name for item in planned if item.kind == "secret"] == ["shop-api-key", "shop-db-password", "shop-cache-auth"]
    assert by_name["shop-apps"].spec["prefix"] == "shop-apps-01234567" and fake.calls == []
    estimate = provider.estimate_cost(planned)
    lines = {line.resource: line for line in estimate.lines}
    assert lines["shop-orders"].monthly_usd > 0 and lines["shop-mails"].monthly_usd == 0, "the namespace is priced once"
    assert lines["shop-db-01234567"].stopped_usd < lines["shop-db-01234567"].monthly_usd, "a stopped server bills its storage only"


# ---- secrets

def test_secrets_live_in_a_vault_of_the_requests_own_written_through_its_api_and_purged_with_the_request(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "keys", "secret": [{"name": "token", "generate_value": True}, {"name": "empty"}],
                                        "access": [{"member": "user:ada@example.com", "level": "admin"}]})
    assert response.status == RequestStatus.READY
    vault = f"keys-{response.request_id[:8]}"
    [body] = puts(fake, "vaults")
    assert body["properties"] | {"tenantId": TENANT} == body["properties"] and body["properties"]["enableRbacAuthorization"] is True
    assert body["properties"]["softDeleteRetentionInDays"] == 7 and "enablePurgeProtection" not in body["properties"], "one vault for every secret of the request"
    token, empty = (next(item for item in response.resources.secret if item.name == name) for name in ("keys-token", "keys-empty"))
    assert (token.versions, empty.versions) == (1, 0) and token.resource_name == f"https://{vault}.vault.azure.net/secrets/keys-token"
    assert fake.vault_data[vault]["keys-token"][0]["enabled"] is True and len(fake.vault_data[vault]["keys-token"][0]["value"]) >= 32
    assert fake.vault_data[vault]["keys-empty"] == [{"value": "", "enabled": False}], "it exists for its grants; nothing can be read from it"
    ada = USERS["ada@example.com"]
    assert sorted(role for scope, principal_id, role in fake.assignments()) == [SECRETS_OFFICER, SECRETS_OFFICER] and \
        {scope for scope, _, _ in fake.assignments()} == {"secrets/keys-token", "secrets/keys-empty"}, "at each secret's own scope, never the vault's"
    assert all(principal_id == ada for _, principal_id, _ in fake.assignments())
    assert empty.connect.commands["write"] == f"az keyvault secret set --vault-name {vault} --name keys-empty --file FILE" and "no value yet" in empty.connect.notes[0]
    reads = [call for call in fake.calls if ".vault.azure.net" in call[1].split("/")[2] and call[0] == "GET"]
    assert reads == [], "the service never reads a value back"
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED and fake.resources == {} and fake.deleted_vaults == set() and vault not in fake.vault_data


def test_a_database_password_is_written_once_to_its_secret_and_the_server_holds_the_same(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "shop", "network": {}, "database": {"database_name": "orders", "username": "shop"}})
    assert response.status == RequestStatus.READY, response.error
    [database] = response.resources.database
    vault = f"shop-{response.request_id[:8]}"
    [secret] = response.resources.secret
    written = fake.vault_data[vault]["shop-db-password"]
    assert written[-1]["enabled"] and fake.passwords[database.name] == written[-1]["value"] and secret.versions == 1
    password = written[-1]["value"]
    assert any(char.isupper() for char in password) and any(char.islower() for char in password) and any(char.isdigit() for char in password)
    assert response.credentials.database_password is None and response.credentials.database_password_secret == secret.id
    assert password not in str(orchestrator.state.get_request(response.request_id)) and "administratorLoginPassword" not in str(fake.resources)


# ---- databases

def test_a_private_postgresql_server_is_reached_through_its_endpoint_and_named_through_the_networks_zone(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "shop", "network": {}, "database": {"database_name": "orders", "username": "shop"}})
    [database], [network] = response.resources.database, response.resources.network
    [server] = puts(fake, "flexibleServers")
    properties = server["properties"]
    assert server["sku"] == {"name": "Standard_B1ms", "tier": "Burstable"} and properties["administratorLogin"] == "shop" and properties["version"] == "18"
    assert properties["network"] == {"publicNetworkAccess": "Disabled"} and "delegatedSubnetResourceId" not in properties["network"], "never network injection"
    assert properties["storage"] == {"storageSizeGB": 32, "autoGrow": "Enabled"} and properties["highAvailability"] == {"mode": "Disabled"}
    assert properties["authConfig"] == {"activeDirectoryAuth": "Enabled", "passwordAuth": "Enabled", "tenantId": TENANT} and properties["availabilityZone"] == "1"
    assert puts(fake, "databases")[0] == {"properties": {"charset": "UTF8", "collation": "en_US.utf8"}}
    [endpoint] = puts(fake, "privateEndpoints")
    assert endpoint["properties"]["subnet"]["id"] == network.subnet and \
        endpoint["properties"]["privateLinkServiceConnections"][0]["properties"] == {"privateLinkServiceId": database.provider_id, "groupIds": ["postgresqlServer"]}
    zone = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/shop-{response.request_id[:8]}/providers/Microsoft.Network/privateDnsZones/privatelink.postgres.database.azure.com"
    assert puts(fake, "privateDnsZoneGroups")[0]["properties"]["privateDnsZoneConfigs"][0]["properties"]["privateDnsZoneId"] == zone
    [link] = puts(fake, "virtualNetworkLinks")
    assert link["properties"] == {"virtualNetwork": {"id": network.provider_id}, "registrationEnabled": False} and link["tags"]["cloudinfra-request"] == network.request_id
    made = [path.rsplit("/", 2)[-2] for method, path in fake.order if method == "PUT"]
    assert made.index("flexibleServers") < made.index("privateEndpoints") < made.index("locks"), "the lock goes on after the server's last part"
    assert database.host == f"{database.name}.postgres.database.azure.com" and database.port == 5432 and response.endpoints.database_host == database.host
    connect = database.connect
    assert connect.commands["connect"] == f'psql "host={database.host} port=5432 dbname=orders user=shop sslmode=require"'
    assert connect.commands["password"].startswith(f"az keyvault secret show --vault-name shop-{response.request_id[:8]} --name shop-db-password")
    assert "resolves to its private endpoint" in connect.prerequisites[0]
    fake.order.clear()
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}
    deletes = [path.rsplit("/", 2)[-2] for method, path in fake.order if method == "DELETE"]
    assert deletes.index("locks") < deletes.index("privateEndpoints") < deletes.index("flexibleServers") < deletes.index("privateDnsZones")


def test_a_public_mysql_server_has_no_endpoint_and_waits_for_a_firewall_rule(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, allow_public_exposure=True))
    response = provision(orchestrator, {"provider": "azure", "region": "westus", "name": "pub", "database": {"engine": "mysql", "public_access": True, "size": "medium",
                                                                                                         "high_availability": True, "storage_gb": 10}})
    assert response.status == RequestStatus.READY, response.error
    [server] = puts(fake, "flexibleServers")
    assert server["properties"]["version"] == "8.0.21" and server["properties"]["network"] == {"publicNetworkAccess": "Enabled"}
    assert server["properties"]["highAvailability"] == {"mode": "SameZone"} and server["properties"]["storage"]["storageSizeGB"] == 20, "westus has no zones"
    assert "authConfig" not in server["properties"] and puts(fake, "privateEndpoints") == [] and puts(fake, "databases")[0]["properties"]["charset"] == "utf8mb4"
    database = response.resources.database[0]
    assert database.port == 3306 and "firewall-rule create" in database.connect.prerequisites[0] and database.connect.commands["connect"].startswith("mysql --host=")


def test_a_database_stops_and_starts_with_its_request_and_its_lock_is_waited_out_on_delete(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "pg", "network": {}, "compute": {"cpu": 2, "memory_gb": 8}, "database": {}})
    stopped = orchestrator.stop(response.request_id, who, wait_seconds=45)
    assert stopped.status == RequestStatus.STOPPED and stopped.resources.database[0].status == ResourceStatus.STOPPED
    assert fake.resources[stopped.resources.database[0].provider_id.lower()]["properties"]["state"] == "Stopped"
    assert orchestrator.start(response.request_id, who, wait_seconds=45).resources.database[0].status == ResourceStatus.READY
    # a lock just removed may still refuse a delete a moment: the delete waits it out
    server = f"/flexibleServers/{response.resources.database[0].name}$"
    fake.refuse[("DELETE", server)] = [refusal(409, "ScopeLocked", "The scope cannot perform delete operation because it is locked")] * 2
    before = fake.now
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}
    assert fake.refuse[("DELETE", server)] == [] and fake.now - before >= 20


# ---- caches

def test_a_cache_is_azure_managed_redis_private_with_its_key_copied_into_the_requests_secret(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "fast", "network": {}, "cache": {"memory_gb": 4}})
    assert response.status == RequestStatus.READY, response.error
    [cache] = response.resources.cache
    [cluster], [database] = puts(fake, "redisEnterprise"), puts(fake, "databases")
    assert cluster["sku"] == {"name": "Balanced_B5"} and cluster["properties"] == {"minimumTlsVersion": "1.2", "highAvailability": "Disabled", "publicNetworkAccess": "Disabled"}
    assert database["properties"] == {"clientProtocol": "Encrypted", "port": 10000, "clusteringPolicy": "OSSCluster", "evictionPolicy": "VolatileLRU",
                                      "accessKeysAuthentication": "Enabled"}
    assert puts(fake, "privateEndpoints")[0]["properties"]["privateLinkServiceConnections"][0]["properties"]["groupIds"] == ["redisEnterprise"]
    assert (cache.host, cache.port, cache.memory_gb, cache.version) == (f"{cache.name}.eastus2.redis.azure.net", 10000, 6, "7.4")
    vault = f"fast-{response.request_id[:8]}"
    assert fake.vault_data[vault]["fast-cache-auth"][-1] == {"value": f"primary-key-of-{cache.name}", "enabled": True}
    assert response.credentials.cache_auth_secret == next(item.id for item in response.resources.secret if item.purpose == "cache_auth")
    assert cache.connect.commands["connect"] == f"redis-cli -h {cache.host} -p 10000 --tls --askpass"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


# ---- queues

def test_queues_share_the_requests_namespace_with_no_connection_string(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "bus", "queue": [{"name": "orders", "ack_deadline_seconds": 30, "retention_hours": 48},
                                                                                                        {"name": "mails", "subscription": False}],
                                        "access": [{"member": "user:ada@example.com"}]})
    assert response.status == RequestStatus.READY, response.error
    [namespace] = puts(fake, "namespaces")
    assert namespace["sku"] == {"name": "Standard", "tier": "Standard"} and namespace["properties"]["disableLocalAuth"] is True
    topics, subscriptions = puts(fake, "topics"), puts(fake, "subscriptions")
    assert topics[0]["properties"]["defaultMessageTimeToLive"] == "PT48H" and len(topics) == 2 and len(subscriptions) == 1
    assert subscriptions[0]["properties"] == {"lockDuration": "PT30S", "defaultMessageTimeToLive": "PT48H", "deadLetteringOnMessageExpiration": True, "maxDeliveryCount": 10}
    ada = USERS["ada@example.com"]
    assert sorted((scope, role) for scope, _, role in fake.assignments()) == [("subscriptions/bus-orders", BUS_RECEIVER), ("topics/bus-mails", BUS_SENDER),
                                                                             ("topics/bus-orders", BUS_SENDER)] and {who for _, who, _ in fake.assignments()} == {ada}
    orders = next(item for item in response.resources.queue if item.name == "bus-orders")
    assert orders.connect.commands["publish"].startswith(f"curl -X POST https://bus-{response.request_id[:8]}.servicebus.windows.net/bus-orders/messages")
    assert "receive" in orders.connect.commands and "no connection string" in orders.connect.notes[0]
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


# ---- the registry

def registry_config(tmp_path, **azure):
    return make_azure_config(tmp_path, azure={"registry": REGISTRY, **azure}, principals=config_dict()["principals"],
                             access={"allowed_domains": ["example.com"], "registry_writers": [f"serviceAccount:{BUILDER}"]})


def assignments_on_registry(fake) -> dict[tuple[str, str], set[str]]:
    """(principal, role) -> the prefixes its one assignment on the operator's registry names."""
    return {(item["properties"]["principalId"], item["properties"]["roleDefinitionId"].rsplit("/", 1)[-1]): prefixes_of(item["properties"].get("condition"))
            for item in fake.resources.values() if item["type"] == "Microsoft.Authorization/roleAssignments" and item["properties"]["scope"] == REGISTRY}


def test_a_registry_is_a_prefix_in_the_operators_registry_granted_under_a_condition(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=registry_config(tmp_path))
    operator_registry(fake)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "web", "registry": {"name": "apps"},
                                        "access": [{"member": "user:ada@example.com", "level": "admin"}, {"member": f"serviceAccount:{BUILDER}", "level": "admin"}]})
    assert response.status == RequestStatus.READY, response.error
    [registry] = response.resources.registry
    prefix = f"web-apps-{response.request_id[:8]}"
    assert registry.repository == prefix and registry.address == f"{REGISTRY_LOGIN}/{prefix}" and response.endpoints.registry_addresses == [registry.address], \
        "the address is the registry's own login server, read from Azure"
    ada = USERS["ada@example.com"]
    assert assignments_on_registry(fake) == {(ada, REPOSITORY_READER): {prefix}, (BUILDER, REPOSITORY_WRITER): {prefix}}, \
        "admin is still a pull for anyone but the operator's writers"
    reader = next(item for item in fake.resources.values() if item["type"] == "Microsoft.Authorization/roleAssignments" and item["properties"]["principalId"] == ada)
    assert reader["name"] == assignment_name(REGISTRY, ada, REPOSITORY_READER) and reader["properties"]["conditionVersion"] == "2.0"
    assert f"StringStartsWithIgnoreCase '{prefix}/'" in reader["properties"]["condition"] and "content/write" not in reader["properties"]["condition"]
    assert registry.connect.commands["pull"] == f"docker pull {REGISTRY_LOGIN}/{prefix}/IMAGE@sha256:DIGEST"
    with pytest.raises(InfrastructureError):
        orchestrator.registry.get("azure").access.grant([(REGISTRY, "registry", "")], {"id": ada, "type": "User"}, "use", "never registry-wide")
    with pytest.raises(InfrastructureError):
        orchestrator.registry.get("azure").access.grant([(REGISTRY, "registry", "a/b' OR 'x")], {"id": ada, "type": "User"}, "use", "never another condition")


def test_one_member_on_two_requests_registries_holds_one_assignment_naming_both_prefixes(tmp_path):
    """Azure keeps one assignment of a role per member and scope, so the member's assignment on the operator's registry
    names every prefix it is granted on: a grant widens it, a revoke narrows it, and the last one removes it."""
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=registry_config(tmp_path))
    operator_registry(fake)
    who = principal(orchestrator.config)
    members = [{"member": "user:ada@example.com"}, {"member": f"serviceAccount:{BUILDER}", "level": "admin"}]
    first = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "web", "registry": {"name": "apps"}, "access": members})
    second = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "web", "registry": {"name": "apps"}, "access": members})
    one, two = first.resources.registry[0].repository, second.resources.registry[0].repository
    ada = USERS["ada@example.com"]
    assert one != two and assignments_on_registry(fake) == {(ada, REPOSITORY_READER): {one, two}, (BUILDER, REPOSITORY_WRITER): {one, two}}
    assert all(entry.status == "GRANTED" for entry in [*first.access, *second.access])
    fake.repositories |= {f"{one}/api", f"{one}/web", f"{two}/api", "shared/base", "zz/other"}
    assert orchestrator.destroy(first.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert fake.repositories == {f"{two}/api", "shared/base", "zz/other"}, "only the destroyed request's repositories went, across pages of the catalog"
    assert assignments_on_registry(fake) == {(ada, REPOSITORY_READER): {two}, (BUILDER, REPOSITORY_WRITER): {two}}, "the other request keeps its access"
    revoked = orchestrator.manage_access(second.request_id, "revoke", "user:ada@example.com", who)
    assert [entry.status for entry in revoked.access if entry.member == "user:ada@example.com"] == ["REVOKED"]
    assert assignments_on_registry(fake) == {(BUILDER, REPOSITORY_WRITER): {two}}
    assert orchestrator.destroy(second.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and assignments_on_registry(fake) == {}
    assert all(path in ("/oauth2/exchange", "/oauth2/token") or bearer.startswith("access:") for path, bearer in fake.registry_tokens), \
        "the service's own token is exchanged in a form, never sent as the registry's bearer"


def test_a_registry_is_torn_down_in_the_registry_it_was_made_in_whatever_the_operator_names_now(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=registry_config(tmp_path))
    operator_registry(fake)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "web", "registry": {"name": "apps"},
                                        "access": [{"member": "user:ada@example.com"}]})
    prefix = response.resources.registry[0].repository
    fake.repositories |= {f"{prefix}/api"}
    adapter = orchestrator.registry.get("azure")
    adapter.settings = adapter.settings.model_copy(update={"registry": None})  # the operator has since removed the setting
    adapter.registries.settings = adapter.settings
    assert orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45).status == RequestStatus.DESTROYED
    assert fake.repositories == set() and assignments_on_registry(fake) == {}


# ---- everything together

def test_a_request_with_every_kind_azure_provides_so_far_is_made_and_leaves_nothing(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure={"registry": REGISTRY}))
    operator_registry(fake)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "all", "identity": {}, "network": {}, "storage": {"size_gb": 50},
                                        "compute": {"cpu": 2, "memory_gb": 8, "identity": "app"}, "database": {}, "cache": {}, "queue": {"name": "jobs"},
                                        "secret": {"name": "token", "generate_value": True}, "registry": {"name": "apps"},
                                        "access": [{"member": "user:ada@example.com"}]})
    assert response.status == RequestStatus.READY, response.error
    assert all(item.status == ResourceStatus.READY for item in response.resources.all()) and len(response.resources.all()) == 11
    assert {entry.member: entry.status for entry in response.access} == {"user:ada@example.com": "GRANTED", f"serviceAccount:{response.resources.identity[0].principal_id}": "GRANTED"}
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert set(fake.resources) == {REGISTRY.lower(), REGISTRY.split("/providers/", 1)[0].lower()} and fake.deleted_vaults == set(), \
        "the operator's registry and its group are all that is left"


# ---- what the review of 2026-11-05 found

def test_private_dns_zones_follow_the_cloud_and_caches_wait_for_a_documented_one(tmp_path):
    from cloudinfra.providers.azure import private
    from cloudinfra.providers.azure.clouds import CLOUDS
    assert private.zone_of(CLOUDS["usgovernment"], "postgresql") == "privatelink.postgres.database.usgovcloudapi.net"
    assert private.zone_of(CLOUDS["china"], "mysql") == "privatelink.mysql.database.chinacloudapi.cn" and private.zone_of(CLOUDS["china"], "redis") is None
    provider, _ = make_azure_provider(make_azure_config(azure={"cloud": "usgovernment", "allowed_regions": ["usgovvirginia"], "zones": {"usgovvirginia": ["1"]}}))
    found = provider.validate(InfrastructureRequest.model_validate({"provider": "azure", "region": "usgovvirginia", "network": {}, "cache": {}}))
    assert [item.field for item in found] == ["cache"] and "public cloud only" in found[0].message


def test_a_secret_never_written_and_a_request_without_a_vault_cost_no_waits_or_purges(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    plain = provision(orchestrator, {"provider": "azure", "region": "eastus2", "network": {}})
    fake.calls.clear()
    assert orchestrator.destroy(plain.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert not [call for call in fake.calls if call[0] == "POST" and "/purge" in call[1]], "no vault, no purge to ask for (nor its permission)"
    fake.fail_on.append(("PUT", "/secrets/keys-broken"))
    before = fake.now
    failed = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "keys", "secret": [{"name": "broken"}]})
    assert failed.status == RequestStatus.FAILED and failed.error.details["cleanup"]["remaining"] == [] and fake.now - before < 10, \
        "a secret whose value was never written is not waited on to purge"
    assert fake.resources == {} and fake.deleted_vaults == set()


def test_a_grant_that_failed_halfway_leaves_nothing_once_its_resource_goes(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    fake.fail_on.append(("PUT", "/subscriptions/bus-orders/providers/Microsoft.Authorization/roleAssignments/"))
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "bus", "queue": {"name": "orders"},
                                        "access": [{"member": "user:ada@example.com"}]})
    assert [entry.status for entry in response.access] == ["FAILED"] and fake.assignments() == [("topics/bus-orders", USERS["ada@example.com"], BUS_SENDER)], \
        "the topic's assignment was made before the subscription's failed, and nothing records it"
    fake.fail_on.clear()
    assert orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45).status == RequestStatus.DESTROYED
    assert fake.assignments() == [] and fake.resources == {}


def test_a_database_on_another_requests_network_finds_its_zone_with_that_network(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network = orchestrator.create_network(who, region="eastus2", provider="azure", name="shared", wait_seconds=45)
    net = network.resources.network[0]
    database = orchestrator.create_database(who, region="eastus2", provider="azure", network_id=net.id, wait_seconds=45)
    assert database.status == RequestStatus.READY, database.error
    zones = [item for item in fake.resources.values() if item["type"] == "Microsoft.Network/privateDnsZones"]
    assert [(zone["id"].split("/")[4], zone["tags"]["cloudinfra-request"]) for zone in zones] == [(f"shared-{network.request_id[:8]}", network.request_id)], \
        "in the network's group, as the network's request"
    assert orchestrator.destroy(database.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert [item["type"] for item in fake.resources.values() if item["type"].startswith("Microsoft.Network/privateDnsZones")], "the zone stays with its network"
    assert orchestrator.destroy(network.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}
