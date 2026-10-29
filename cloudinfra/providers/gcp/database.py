"""Cloud SQL behind the generic database primitives: one instance, one database, one user with a password returned once."""
import secrets

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import DatabaseResource, ResourceStatus
from . import translate
from .api import SQL

STATE = {"PENDING_CREATE": ResourceStatus.CREATING, "RUNNABLE": ResourceStatus.READY, "SUSPENDED": ResourceStatus.STOPPED, "PENDING_DELETE": ResourceStatus.DELETING,
         "MAINTENANCE": ResourceStatus.UPDATING, "FAILED": ResourceStatus.FAILED, "ONLINE_MAINTENANCE": ResourceStatus.UPDATING, "STOPPED": ResourceStatus.STOPPED}


class GcpDatabase:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _instances(self):
        return f"{SQL}/projects/{self.api.project}/instances"

    def create(self, spec, planned, ctx, network) -> tuple[DatabaseResource, str]:
        private = not spec.public_access
        if private and network is None:
            raise InfrastructureError(ErrorCode.INVALID_DATABASE_CONFIGURATION, "A private database needs a network in the same request", resource=planned.name)
        ip_configuration = {"ipv4Enabled": spec.public_access, "sslMode": "ENCRYPTED_ONLY"}
        if private:
            ip_configuration["privateNetwork"] = network.provider_id
        body = {
            "name": planned.name, "region": ctx.region, "databaseVersion": planned.spec["database_version"],
            "settings": {
                "tier": planned.spec["tier"], "edition": "ENTERPRISE", "availabilityType": "REGIONAL" if spec.high_availability else "ZONAL",
                "dataDiskType": "PD_SSD", "dataDiskSizeGb": str(spec.storage_gb), "storageAutoResize": False,
                "backupConfiguration": {"enabled": True, **({"pointInTimeRecoveryEnabled": True} if spec.engine == "postgresql" else {"binaryLogEnabled": True})},
                "ipConfiguration": ip_configuration, "deletionProtectionEnabled": True, "userLabels": translate.labels(ctx),
            },
        }
        self.api.wait_sql(self.api.post(self._instances(), json=body))
        self.api.wait_sql(self.api.post(f"{self._instances()}/{planned.name}/databases", json={"name": spec.database_name}))
        password = ctx.database_password or secrets.token_urlsafe(24)
        self.api.wait_sql(self.api.post(f"{self._instances()}/{planned.name}/users", json={"name": spec.username, "password": password}))
        resource = DatabaseResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                    engine=spec.engine, version=planned.spec["version"], tier=planned.spec["tier"], storage_gb=spec.storage_gb,
                                    high_availability=spec.high_availability, public_access=spec.public_access, port=translate.DB_PORTS[spec.engine],
                                    database=spec.database_name, username=spec.username, labels=body["settings"]["userLabels"],
                                    provider_id=f"{self._instances()}/{planned.name}")
        return self.get(resource), password

    def get(self, resource: DatabaseResource) -> DatabaseResource:
        try:
            instance = self.api.get(f"{self._instances()}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "host": None})
            raise
        addresses = instance.get("ipAddresses") or []
        wanted = "PRIMARY" if resource.public_access else "PRIVATE"
        host = next((item.get("ipAddress") for item in addresses if item.get("type") == wanted), None) or next((item.get("ipAddress") for item in addresses), None)
        settings = instance.get("settings") or {}
        status = STATE.get(instance.get("state", ""), ResourceStatus.UNKNOWN)
        if settings.get("activationPolicy") == "NEVER" and status in (ResourceStatus.READY, ResourceStatus.STOPPED):
            status = ResourceStatus.STOPPED  # Cloud SQL documents the policy, not a state, for a stopped instance
        return resource.model_copy(update={"status": status, "host": host, "connection_name": instance.get("connectionName"),
                                           "tier": settings.get("tier", resource.tier), "storage_gb": int(settings.get("dataDiskSizeGb", resource.storage_gb)),
                                           "created_at": resource.created_at or instance.get("createTime")})

    def stop(self, resource: DatabaseResource, ctx) -> DatabaseResource:
        """Stop the instance: its compute charges stop, storage and address charges continue."""
        self.api.wait_sql(self.api.patch(f"{self._instances()}/{resource.name}", json={"settings": {"activationPolicy": "NEVER"}}))
        return self.get(resource)

    def start(self, resource: DatabaseResource, ctx) -> DatabaseResource:
        self.api.wait_sql(self.api.patch(f"{self._instances()}/{resource.name}", json={"settings": {"activationPolicy": "ALWAYS"}}))
        return self.get(resource)

    def delete(self, resource: DatabaseResource, ctx) -> None:
        url = f"{self._instances()}/{resource.name}"
        try:
            # Deletion protection is on from creation; an explicit destroy is the one place it is turned off.
            self.api.wait_sql(self.api.patch(url, json={"settings": {"deletionProtectionEnabled": False}}))
            self.api.wait_sql(self.api.delete(url))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
