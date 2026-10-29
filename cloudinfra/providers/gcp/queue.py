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
        subscription = f"projects/{self.api.project}/subscriptions/{planned.name}" if spec.subscription else None
        resource = QueueResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=topic, topic=topic,
                                 subscription=subscription, retention_hours=spec.retention_hours, status=ResourceStatus.READY, labels=translate.labels(ctx))
        self.api.put(f"{PUBSUB}/{topic}", json={"labels": translate.labels(ctx)})
        ctx.accepted(resource.model_copy(update={"status": ResourceStatus.CREATING}))  # the topic is known from here: a failed subscription still deletes it
        if subscription:
            self.api.put(f"{PUBSUB}/{subscription}", json={"topic": topic, "ackDeadlineSeconds": spec.ack_deadline_seconds, "messageRetentionDuration": f"{spec.retention_hours * 3600}s",
                                                            "expirationPolicy": {}, "labels": translate.labels(ctx)})
        return resource

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
