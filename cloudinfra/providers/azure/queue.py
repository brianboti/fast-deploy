"""Service Bus topics and subscriptions behind the generic queue primitives (docs/research/RESEARCH-2026-11-03-azure.md,
section 5.4).

One Standard namespace per request, made with its first queue (Basic has no topics; Premium only for private
endpoints): local authentication off, so only Entra sign-in works and no connection string exists, TLS 1.2, a name
unique across Azure salted with the request id (a deleted namespace's name stays taken four hours). Each queue is a
topic <name>-<queue> and, when asked, one subscription of the same name: messages kept for the retention asked, a lock
for the acknowledgement deadline (Service Bus allows five minutes at most), dead-lettering on expiry. Members are granted
Azure Service Bus Data Sender on the topic and Data Receiver on the subscription. The namespace's endpoint is public,
reached with Entra sign-in; when nothing of the request is left, the namespace goes with its resource group.
"""
from ...models.resources import QueueResource, ResourceStatus
from . import naming, translate
from .api import API_VERSIONS
from .groups import name_of

VERSION = API_VERSIONS["Microsoft.ServiceBus"]
MAX_LOCK_SECONDS = 300
MAX_DELIVERIES = 10


def namespace_name(ctx) -> str:
    return naming.global_name("service_bus", ctx.name, ctx.request_id)


class AzureQueue:
    def __init__(self, api, groups, ceiling):
        self.api, self.groups, self.ceiling = api, groups, ceiling
        self._namespaces: set[str] = set()

    def _namespace_id(self, ctx) -> str:
        return f"{self.groups.id(name_of(ctx))}/providers/Microsoft.ServiceBus/namespaces/{namespace_name(ctx)}"

    def ensure_namespace(self, ctx) -> str:
        namespace = self._namespace_id(ctx)
        if namespace.lower() in self._namespaces:
            return namespace
        self.groups.ensure(ctx)
        self.api.put(self.api.arm(namespace), {"location": ctx.region, "tags": translate.tags(ctx), "sku": {"name": "Standard", "tier": "Standard"},
                                               "properties": {"disableLocalAuth": True, "minimumTlsVersion": "1.2", "publicNetworkAccess": "Enabled"}},
                     VERSION, timeout=self.ceiling("namespace"))
        self._namespaces.add(namespace.lower())
        return namespace

    def create(self, spec, planned, ctx) -> QueueResource:
        namespace = self._namespace_id(ctx)
        topic = f"{namespace}/topics/{planned.name}"
        subscription = f"{topic}/subscriptions/{planned.name}" if spec.subscription else None
        retention = f"PT{spec.retention_hours}H"
        resource = QueueResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region, provider_id=topic,
                                 topic=topic, subscription=subscription, retention_hours=spec.retention_hours, status=ResourceStatus.CREATING, labels=translate.tags(ctx))
        ctx.accepted(resource)
        self.ensure_namespace(ctx)
        self.api.put(self.api.arm(topic), {"properties": {"defaultMessageTimeToLive": retention, "maxSizeInMegabytes": 1024}}, VERSION,
                     timeout=self.ceiling("namespace"))
        if subscription:
            self.api.put(self.api.arm(subscription), {"properties": {
                "lockDuration": f"PT{min(spec.ack_deadline_seconds, MAX_LOCK_SECONDS)}S", "defaultMessageTimeToLive": retention,
                "deadLetteringOnMessageExpiration": True, "maxDeliveryCount": MAX_DELIVERIES}}, VERSION, timeout=self.ceiling("namespace"))
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: QueueResource) -> QueueResource:
        found = self.api.find(self.api.arm(resource.topic), VERSION)
        return resource.model_copy(update={"status": ResourceStatus.READY if found is not None else ResourceStatus.DELETED})

    def delete(self, resource: QueueResource, ctx) -> None:
        for item in (resource.subscription, resource.topic):
            if item:
                self.api.delete(self.api.arm(item), VERSION, timeout=self.ceiling("namespace"))
