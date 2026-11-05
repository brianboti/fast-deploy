"""Key Vault secrets behind the generic secret primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.4).

One vault per request, made with its first secret: Azure RBAC for its data (no access policies), soft delete for the
shortest period Azure allows (seven days), no purge protection, standard SKU, a name unique across Azure salted with the
request id. When nothing of the request is left (CloudProvider.finished) the vault is deleted and purged, so no copy of
a value outlives the request.

A value exists only when this service generated it or copied a request's own credential into it, and then it goes
straight to the vault's own API (the Key Vault audience): never returned, never logged, never kept. The service's role
holds the data actions to set, delete and purge a secret and none to read one (REQUIREMENTS.md), so it can write what
it made and remove it, never read anything back. A secret asked for without a value is a disabled empty version, so
that the secret exists for its grants while nobody can read anything from it until a member writes a value of their own.
Members are granted at the secret's own scope (Key Vault Secrets User at use, Officer at admin), never the vault's.
"""
import secrets as random

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ResourceStatus, SecretResource
from ...observability.logging import event
from . import naming, translate
from .api import API_VERSIONS, KEY_VAULT_DATA_VERSION
from .groups import name_of

VERSION = API_VERSIONS["Microsoft.KeyVault"]
SOFT_DELETE_DAYS = 7  # the shortest Azure allows
PURGE_NOT_YET = (6, 10)  # a secret just deleted is purged once Key Vault has it among the deleted ones


def vault_name(ctx) -> str:
    return naming.global_name("key_vault", ctx.name, ctx.request_id)


class AzureSecret:
    def __init__(self, api, settings, cloud, groups, tenant, ceiling):
        self.api, self.settings, self.cloud, self.groups, self.tenant, self.ceiling = api, settings, cloud, groups, tenant, ceiling
        self._vaults: set[str] = set()

    def _vault_id(self, ctx) -> str:
        return f"{self.groups.id(name_of(ctx))}/providers/Microsoft.KeyVault/vaults/{vault_name(ctx)}"

    def _data(self, vault: str, path: str) -> str:
        return f"https://{vault}.{self.cloud.key_vault_suffix}{path}"

    def ensure_vault(self, ctx) -> str:
        """The request's vault, made if this process has not made it yet; its id."""
        vault_id = self._vault_id(ctx)
        if vault_id.lower() in self._vaults:
            return vault_id
        self.groups.ensure(ctx)
        self.api.put(self.api.arm(vault_id), {"location": ctx.region, "tags": translate.tags(ctx), "properties": {
            "tenantId": self.tenant(), "sku": {"family": "A", "name": "standard"}, "enableRbacAuthorization": True, "enableSoftDelete": True,
            "softDeleteRetentionInDays": SOFT_DELETE_DAYS, "publicNetworkAccess": "Enabled"}}, VERSION, timeout=self.ceiling("vault"))
        self._vaults.add(vault_id.lower())
        return vault_id

    def _write(self, vault: str, name: str, value: str, enabled: bool) -> None:
        """A new version of the secret, through the vault's own API: the value is the body of this one call, nowhere else."""
        self.api.call("PUT", self._data(vault, f"/secrets/{name}"), json={"value": value, "attributes": {"enabled": enabled}, "contentType": "text/plain"},
                      params={"api-version": KEY_VAULT_DATA_VERSION}, audience=self.cloud.key_vault)

    def create(self, spec, planned, ctx) -> SecretResource:
        vault = vault_name(ctx)
        name = planned.name
        resource = SecretResource(id=planned.spec["resource_id"], name=name, request_id=ctx.request_id, provider="azure", region=ctx.region,
                                  provider_id=f"{self._vault_id(ctx)}/secrets/{name}", resource_name=self._data(vault, f"/secrets/{name}"), versions=0,
                                  status=ResourceStatus.CREATING, labels=translate.tags(ctx), purpose=planned.spec.get("purpose"))
        ctx.accepted(resource)  # the secret is named in the request's vault: a cleanup finds it, and finished() takes the vault
        self.ensure_vault(ctx)
        if spec.generate_value:
            self._write(vault, name, random.token_urlsafe(32), True)
            return resource.model_copy(update={"status": ResourceStatus.READY, "versions": 1})
        self._write(vault, name, "", False)  # the secret exists for its grants; nothing can be read until a value is written
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def put_value(self, resource: SecretResource, value: str, ctx) -> SecretResource:
        self._write(_vault_of(resource), resource.name, value, True)
        return resource.model_copy(update={"versions": resource.versions + 1})

    def get(self, resource: SecretResource) -> SecretResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)  # Resource Manager answers with no value
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: SecretResource, ctx) -> None:
        """Delete the secret and purge it, through the vault's own API; a secret or vault already gone passes."""
        vault = _vault_of(resource)
        if self.api.find(self.api.arm(resource.provider_id.rsplit("/secrets/", 1)[0]), VERSION) is None:
            return  # the vault is gone, and its secrets with it
        attempts = PURGE_NOT_YET[0]
        try:
            self.api.call("DELETE", self._data(vault, f"/secrets/{resource.name}"), params={"api-version": KEY_VAULT_DATA_VERSION}, audience=self.cloud.key_vault)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            attempts = 1  # nothing deleted now: purge once, for one an earlier delete left among the deleted ones
        for attempt in range(attempts):
            try:
                self.api.call("DELETE", self._data(vault, f"/deletedsecrets/{resource.name}"), params={"api-version": KEY_VAULT_DATA_VERSION},
                              audience=self.cloud.key_vault)
                return
            except InfrastructureError as exc:
                if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.PROVIDER_CONFLICT) and attempt < attempts - 1:
                    # not among the deleted ones yet, or its delete still running (409, every purge of the real run of 2026-11-05)
                    self.api.sleep(PURGE_NOT_YET[1])
                    continue
                if exc.code in (ErrorCode.NOT_FOUND, ErrorCode.PROVIDER_CONFLICT):  # gone, or being deleted still: the vault's purge takes it later
                    event("secret_purge_left", secret=resource.name, reason=exc.code.value)
                    return
                raise

    def remove_vault(self, ctx) -> None:
        """Delete the request's vault, if it has one, and purge it: a deleted vault keeps its secrets for its soft-delete period."""
        vault_id = self._vault_id(ctx)
        self._vaults.discard(vault_id.lower())
        found = self.api.find(self.api.arm(vault_id), VERSION)
        location = (found or {}).get("location") or ctx.region
        deleted = self.api.subscription(f"/providers/Microsoft.KeyVault/locations/{location}/deletedVaults/{vault_name(ctx)}")
        if found is not None:
            self.api.delete(self.api.arm(vault_id), VERSION, timeout=self.ceiling("vault"))
        elif self.api.find(deleted, VERSION) is None:
            return  # the request never had a vault, or it is purged already: nothing to ask for
        purge = f"{deleted}/purge"
        try:
            self.api.post(purge, VERSION, timeout=self.ceiling("vault"))
            event("vault_purged", vault=vault_name(ctx))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:  # never made, or purged already
                raise


def _vault_of(resource: SecretResource) -> str:
    """The vault's name, from the secret's address on the vault's own API (https://VAULT.vault.azure.net/secrets/NAME)."""
    return resource.resource_name.split("//", 1)[1].split(".", 1)[0]
