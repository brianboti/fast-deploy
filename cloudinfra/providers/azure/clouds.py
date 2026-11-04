"""The Azure clouds the adapter can work in, and where each one's endpoints and token audiences are.

Every value a cloud's metadata document carries was read from that document on 2026-11-03 (an unauthenticated
GET https://<resource manager>/metadata/endpoints?api-version=2022-09-01). The three documents are kept in
tests/cloudinfra/fixtures/, and a test compares them with this table, so a refreshed document that moves a value fails
a test instead of a call. The values the documents do not carry come from Microsoft's pages, named beside each; where
no page names one for a sovereign cloud it is None, and the adapter refuses the call that would need it rather than
guessing an address to send a token to.
"""
from dataclasses import dataclass
from urllib.parse import urlparse

# The AKS API server's Entra application, the same in every cloud (learn.microsoft.com/azure/aks/kubelogin-authentication).
AKS_SERVER_APPLICATION = "6dae42f8-4368-4678-94ff-3960e28e3630"


@dataclass(frozen=True)
class AzureCloud:
    name: str  # the document's own name: AzureCloud, AzureUSGovernment, AzureChinaCloud
    resource_manager: str  # where every control-plane call goes, without a trailing slash
    login: str  # Entra ID's token endpoints live under it
    arm_audience: str  # the resource a token for Resource Manager is asked for; one of the document's audiences
    graph: str  # Microsoft Graph, for resolving members to object ids
    oss_rdbms: str  # a member's Entra sign-in to a PostgreSQL or MySQL flexible server
    key_vault_suffix: str
    storage_suffix: str
    registry_suffix: str
    postgresql_suffix: str
    mysql_suffix: str
    # Not in the documents. Public cloud: the metrics batch plane (learn.microsoft.com/rest/api/monitor/metrics-batch/batch),
    # Storage (…/rest/api/storageservices/authorize-with-azure-active-directory), Service Bus
    # (…/azure/service-bus-messaging/authenticate-application); none of them names a sovereign cloud's.
    metrics: str | None = None
    storage: str | None = None
    service_bus: str | None = None

    @property
    def key_vault(self) -> str:
        """The audience of a key vault's data plane (learn.microsoft.com/azure/key-vault/general/rbac-guide)."""
        return f"https://{self.key_vault_suffix}"

    @property
    def host(self) -> str:
        return urlparse(self.resource_manager).hostname or ""


CLOUDS = {
    "public": AzureCloud(name="AzureCloud", resource_manager="https://management.azure.com", login="https://login.microsoftonline.com",
                         arm_audience="https://management.azure.com/", graph="https://graph.microsoft.com/",
                         oss_rdbms="https://ossrdbms-aad.database.windows.net", key_vault_suffix="vault.azure.net", storage_suffix="core.windows.net",
                         registry_suffix="azurecr.io", postgresql_suffix="postgres.database.azure.com", mysql_suffix="mysql.database.azure.com",
                         metrics="https://metrics.monitor.azure.com", storage="https://storage.azure.com/", service_bus="https://servicebus.azure.net"),
    "usgovernment": AzureCloud(name="AzureUSGovernment", resource_manager="https://management.usgovcloudapi.net", login="https://login.microsoftonline.us",
                               arm_audience="https://management.usgovcloudapi.net", graph="https://graph.microsoft.us/",
                               oss_rdbms="https://ossrdbms-aad.database.usgovcloudapi.net", key_vault_suffix="vault.usgovcloudapi.net",
                               storage_suffix="core.usgovcloudapi.net", registry_suffix="azurecr.us", postgresql_suffix="postgres.database.usgovcloudapi.net",
                               mysql_suffix="mysql.database.usgovcloudapi.net"),
    "china": AzureCloud(name="AzureChinaCloud", resource_manager="https://management.chinacloudapi.cn", login="https://login.chinacloudapi.cn",
                        arm_audience="https://management.chinacloudapi.cn", graph="https://microsoftgraph.chinacloudapi.cn",
                        oss_rdbms="https://ossrdbms-aad.database.chinacloudapi.cn", key_vault_suffix="vault.azure.cn", storage_suffix="core.chinacloudapi.cn",
                        registry_suffix="azurecr.cn", postgresql_suffix="postgres.database.chinacloudapi.cn", mysql_suffix="mysql.database.chinacloudapi.cn"),
}
