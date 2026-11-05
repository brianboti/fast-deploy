"""Azure Database for PostgreSQL and MySQL flexible servers behind the generic database primitives
(docs/research/RESEARCH-2026-11-03-azure.md, section 5.4).

One server, one database, one user with a password returned once. Azure has no API to make a database user, and the
service cannot reach a private server to run SQL, so the request's user is the server's administrator (as a user Cloud
SQL makes is a cloudsqlsuperuser on Google): its name the request's username, its password the one the orchestrator
generated and writes to the request's db-password secret. PostgreSQL also takes Entra sign-in beside the password.

A private database has public network access off and a private endpoint in the request network's subnet, found by its
own name through the service's private DNS zone (private.py); a public one has public access on and no firewall rule,
so nothing reaches it until the Application Layer adds one. Never network injection: a server made that way can never
take a private endpoint. Backups are kept seven days, storage grows by itself, high availability is zone-redundant
where the region has zones. A CanNotDelete lock is its deletion protection, removed by a delete before anything else,
and waited out: a lock just removed may still refuse a delete for a moment (ScopeLocked), which is tried again.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import DatabaseResource, ResourceStatus
from ...observability.logging import event
from . import private, translate
from .api import API_VERSIONS

NAMESPACES = {"postgresql": "Microsoft.DBforPostgreSQL", "mysql": "Microsoft.DBforMySQL"}
LOCK_VERSION = API_VERSIONS["Microsoft.Authorization/locks"]
LOCK = "cloudinfra-deletion-protection"
STATES = {"ready": ResourceStatus.READY, "stopped": ResourceStatus.STOPPED, "starting": ResourceStatus.UPDATING, "stopping": ResourceStatus.UPDATING,
          "updating": ResourceStatus.UPDATING, "provisioning": ResourceStatus.CREATING, "dropping": ResourceStatus.DELETING, "disabled": ResourceStatus.FAILED}
CHARSETS = {"postgresql": ("UTF8", "en_US.utf8"), "mysql": ("utf8mb4", "utf8mb4_0900_ai_ci")}
LOCKED = (6, 10)  # a delete a lock still refuses is tried again, this many times, this many seconds apart


class AzureDatabase:
    def __init__(self, api, groups, tenant, ceiling):
        self.api, self.groups, self.tenant, self.ceiling = api, groups, tenant, ceiling

    @staticmethod
    def _version(engine: str) -> str:
        return API_VERSIONS[NAMESPACES[engine]]

    def create(self, spec, planned, ctx, network) -> tuple[DatabaseResource, str]:
        private_access = not spec.public_access
        if private_access and network is None:
            raise InfrastructureError(ErrorCode.INVALID_DATABASE_CONFIGURATION, "A private database needs a network in the same request", resource=planned.name)
        group = self.groups.ensure(ctx)
        server = f"{self.groups.id(group)}/providers/{NAMESPACES[spec.engine]}/flexibleServers/{planned.name}"
        version = self._version(spec.engine)
        password = ctx.database_password or ""
        sku, tier = translate.DB_SKUS[spec.size]
        tags = translate.tags(ctx)
        properties = {"version": planned.spec["database_version"], "administratorLogin": spec.username, "administratorLoginPassword": password,
                      "storage": {"storageSizeGB": planned.spec["storage_gb"], "autoGrow": "Enabled"},
                      "backup": {"backupRetentionDays": 7, "geoRedundantBackup": "Disabled"},
                      "highAvailability": {"mode": planned.spec["high_availability_mode"]},
                      "network": {"publicNetworkAccess": "Enabled" if spec.public_access else "Disabled"},
                      "createMode": "Create" if spec.engine == "postgresql" else "Default"}
        if spec.engine == "postgresql":
            properties["authConfig"] = {"activeDirectoryAuth": "Enabled", "passwordAuth": "Enabled", "tenantId": self.tenant()}
        if ctx.zone:
            properties["availabilityZone"] = ctx.zone
        resource = DatabaseResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region, zone=ctx.zone,
                                    engine=spec.engine, version=planned.spec["version"], tier=sku, storage_gb=planned.spec["storage_gb"],
                                    high_availability=spec.high_availability, public_access=spec.public_access, port=translate.DB_PORTS[spec.engine],
                                    database=spec.database_name, username=spec.username, labels=tags, provider_id=server, status=ResourceStatus.CREATING)
        ctx.accepted(resource)  # the server, its endpoint and its lock are named here: a cleanup finds them all
        self.api.put(self.api.arm(server), {"location": ctx.region, "tags": tags, "sku": {"name": sku, "tier": tier}, "properties": properties}, version,
                     timeout=self.ceiling("database"))
        charset, collation = CHARSETS[spec.engine]
        self.api.put(self.api.arm(f"{server}/databases/{spec.database_name}"), {"properties": {"charset": charset, "collation": collation}}, version,
                     timeout=self.ceiling("database"))
        if private_access:
            private.make_endpoint(self.api, private.endpoint_id(self.groups, ctx, planned.name), ctx, tags, network, server, spec.engine, self.ceiling("network"))
        self.api.put(self.api.arm(f"{server}/providers/Microsoft.Authorization/locks/{LOCK}"), {"properties": {
            "level": "CanNotDelete", "notes": f"Deletion protection of cloudinfra request {ctx.request_id}; the service removes it when it deletes the server"}},
            LOCK_VERSION, timeout=self.ceiling("network"))
        return self.get(resource), password

    def get(self, resource: DatabaseResource) -> DatabaseResource:
        found = self.api.find(self.api.arm(resource.provider_id), self._version(resource.engine))
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED, "host": None})
        properties = found.get("properties") or {}
        state = str(properties.get("state") or properties.get("provisioningState") or "").lower()
        status = STATES.get(state, ResourceStatus.READY if state == "succeeded" else ResourceStatus.UNKNOWN)
        return resource.model_copy(update={"status": status, "host": properties.get("fullyQualifiedDomainName") or resource.host,
                                           "storage_gb": int((properties.get("storage") or {}).get("storageSizeGB") or resource.storage_gb),
                                           "tier": (found.get("sku") or {}).get("name") or resource.tier})

    def stop(self, resource: DatabaseResource, ctx) -> DatabaseResource:
        """Stop the server: its compute stops billing, its storage does not. Azure starts a stopped server again by itself
        after seven days (PostgreSQL) or thirty (MySQL)."""
        self.api.post(self.api.arm(f"{resource.provider_id}/stop"), self._version(resource.engine), timeout=self.ceiling("power"))
        return self.get(resource)

    def start(self, resource: DatabaseResource, ctx) -> DatabaseResource:
        self.api.post(self.api.arm(f"{resource.provider_id}/start"), self._version(resource.engine), timeout=self.ceiling("power"))
        return self.get(resource)

    def delete(self, resource: DatabaseResource, ctx) -> None:
        lock = self.api.arm(f"{resource.provider_id}/providers/Microsoft.Authorization/locks/{LOCK}")
        self.api.delete(lock, LOCK_VERSION)
        for _ in range(LOCKED[0]):  # read until Azure no longer has it: a deleted lock may still be enforced a moment
            if self.api.find(lock, LOCK_VERSION) is None:
                break
            self.api.sleep(LOCKED[1])
        endpoint = f"{resource.provider_id.split('/providers/', 1)[0]}/providers/Microsoft.Network/privateEndpoints/{resource.name}-pe"
        private.remove_endpoint(self.api, endpoint, self.ceiling("network"))
        for attempt in range(LOCKED[0]):
            try:
                self.api.delete(self.api.arm(resource.provider_id), self._version(resource.engine), timeout=self.ceiling("delete"))
                return
            except InfrastructureError as exc:
                if exc.details.get("reason") != "ScopeLocked" or attempt == LOCKED[0] - 1:
                    raise
                event("database_delete_locked", resource=resource.id, attempt=attempt + 1)
                self.api.sleep(LOCKED[1])
