"""Pub/Sub topics and pull subscriptions behind the generic queue primitives."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import QueueResource, ResourceStatus
from . import translate
from .api import PUBSUB


class GcpQueue:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def create(self, spec, planned, ctx) -> QueueResource:
        topic = f"projects/{self.api.project}/topics/{planned.name}"
        self.api.put(f"{PUBSUB}/{topic}", json={"labels": translate.labels(ctx)})
        subscription = None
        if spec.subscription:
            subscription = f"projects/{self.api.project}/subscriptions/{planned.name}"
            self.api.put(f"{PUBSUB}/{subscription}", json={"topic": topic, "ackDeadlineSeconds": spec.ack_deadline_seconds, "messageRetentionDuration": f"{spec.retention_hours * 3600}s",
                                                            "expirationPolicy": {}, "labels": translate.labels(ctx)})
        return QueueResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=topic, topic=topic,
                             subscription=subscription, retention_hours=spec.retention_hours, status=ResourceStatus.READY, labels=translate.labels(ctx))

    def get(self, resource: QueueResource) -> QueueResource:
        try:
            self.api.get(f"{PUBSUB}/{resource.topic}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: QueueResource, ctx) -> None:
        for name in (resource.subscription, resource.topic):
            if not name:
                continue
            try:
                self.api.delete(f"{PUBSUB}/{name}")
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
