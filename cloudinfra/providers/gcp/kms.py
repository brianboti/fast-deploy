"""A customer-managed encryption key behind the generic encryption_key primitives (Cloud KMS): a key ring and a key in the
request's region, rotated on a period, with which the request's disks (machines' boot disks among them), buckets and
databases are encrypted (https://docs.cloud.google.com/kms/docs/cmek). Each Google service encrypts as a service agent
of its own, which is granted cryptoKeyEncrypterDecrypter on this key only, before anything is made with it.

Cloud KMS never deletes a key ring or a key, by design: destroy schedules the destruction of every version of the key,
after destroy_after_days, and what was encrypted with it is unreadable from then on. The ring's name holds the request
id, so it is never reused."""
from datetime import UTC, datetime, timedelta

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import EncryptionKeyResource, ResourceStatus
from . import translate
from .api import KMS, SERVICE_USAGE, STORAGE
from .iam import update_policy, with_member

ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"


def key_ring(prefix: str, request_id: str) -> str:
    return f"{prefix}-{request_id[:8]}"


def key_name(project: str, region: str, ring: str) -> str:
    return f"projects/{project}/locations/{region}/keyRings/{ring}/cryptoKeys/data"


class GcpKms:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _agents(self, encrypts: list[str]) -> list[str]:
        """The service agents that encrypt the kinds asked for; each is made by its service if it does not exist yet."""
        number, found = self.api.project_number(), []
        if {"storage", "compute"} & set(encrypts):
            found.append(f"serviceAccount:service-{number}@compute-system.iam.gserviceaccount.com")
        if "bucket" in encrypts:
            found.append(f"serviceAccount:{self.api.get(f'{STORAGE}/projects/{self.api.project}/serviceAccount')['email_address']}")
        if "database" in encrypts:
            operation = self.api.post(f"{SERVICE_USAGE}/projects/{self.api.project}/services/sqladmin.googleapis.com:generateServiceIdentity")
            email = (operation.get("response") or {}).get("email") or f"service-{number}@gcp-sa-cloud-sql.iam.gserviceaccount.com"
            found.append(f"serviceAccount:{email}")
        return found

    def create(self, spec, planned, ctx) -> EncryptionKeyResource:
        ring, key = planned.spec["key_ring"], planned.spec["key"]
        parent = f"{KMS}/projects/{self.api.project}/locations/{ctx.region}"
        resource = EncryptionKeyResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=key,
                                         status=ResourceStatus.CREATING, key_ring=ring, rotation_days=spec.rotation_days, destroy_after_days=spec.destroy_after_days,
                                         encrypts=list(planned.spec["encrypts"]), labels=translate.labels(ctx))
        self.api.post(f"{parent}/keyRings", json={}, keyRingId=ring)
        period = timedelta(days=spec.rotation_days)
        self.api.post(f"{parent}/keyRings/{ring}/cryptoKeys", json={
            "purpose": "ENCRYPT_DECRYPT", "labels": translate.labels(ctx), "versionTemplate": {"algorithm": "GOOGLE_SYMMETRIC_ENCRYPTION", "protectionLevel": "SOFTWARE"},
            "rotationPeriod": f"{int(period.total_seconds())}s", "nextRotationTime": (datetime.now(UTC) + period).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "destroyScheduledDuration": f"{spec.destroy_after_days * 86400}s"}, cryptoKeyId="data")
        ctx.accepted(resource)  # the key exists from here: a failure further on still schedules its destruction
        agents = self._agents(resource.encrypts)
        endpoint = (lambda: self.api.get(f"{KMS}/{key}:getIamPolicy", **{"options.requestedPolicyVersion": 3}),
                    lambda policy: self.api.post(f"{KMS}/{key}:setIamPolicy", json={"policy": policy}))
        update_policy(endpoint, lambda policy: any([with_member(policy, ROLE, agent) for agent in agents]))  # noqa: C419 - every agent
        return resource.model_copy(update={"agents": agents, "status": ResourceStatus.READY})

    def get(self, resource: EncryptionKeyResource) -> EncryptionKeyResource:
        try:
            found = self.api.get(f"{KMS}/{resource.provider_id}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        primary = (found.get("primary") or {}).get("state")
        return resource.model_copy(update={"status": ResourceStatus.READY if primary == "ENABLED" else ResourceStatus.DELETED})

    def delete(self, resource: EncryptionKeyResource, ctx) -> None:
        """Schedule the destruction of every version still usable; the key and ring stay, as Cloud KMS keeps them."""
        try:
            versions = self.api.get(f"{KMS}/{resource.provider_id}/cryptoKeyVersions").get("cryptoKeyVersions", [])
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        for version in versions:
            if version.get("state") in ("ENABLED", "DISABLED"):
                self.api.post(f"{KMS}/{version['name']}:destroy", json={})
