"""Customer-managed encryption keys (coverage research, 2026-10-30: 48 percent of companies manage their keys in the cloud
provider's console, Thales 2025; compliance asks for keys the company controls): a key in the request's region with which
its disks, machines' boot disks, buckets and databases are encrypted, each service's agent granted on that key only before
anything is made with it. Cloud KMS keeps keys forever; destroy schedules the destruction of their versions."""
import pytest
from gcp_fakes import make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest

ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
EVERYTHING = {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}, "storage": {"size_gb": 20}, "database": {}}


def provision(tmp_path, body):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_disks_machines_and_databases_are_encrypted_with_the_requests_key(tmp_path):
    _, fake, _, response = provision(tmp_path, {**EVERYTHING, "encryption_key": {"rotation_days": 30, "destroy_after_days": 7}})
    assert response.error is None, response.error
    [key] = response.resources.encryption_key
    assert key.provider_id == f"projects/{fake.project}/locations/us-central1/keyRings/{key.key_ring}/cryptoKeys/data" and key.key_ring.startswith("shop-")
    made = fake.crypto_keys[key.provider_id]
    assert made["rotationPeriod"] == f"{30 * 86400}s" and made["destroyScheduledDuration"] == f"{7 * 86400}s" and made["purpose"] == "ENCRYPT_DECRYPT"
    assert set(key.encrypts) == {"storage", "compute", "database"}
    assert set(fake.members(("key", key.provider_id), ROLE)) == {"serviceAccount:service-123456789@compute-system.iam.gserviceaccount.com",
                                                                 "serviceAccount:service-123456789@gcp-sa-cloud-sql.iam.gserviceaccount.com"}
    [disk] = response.resources.storage
    [machine] = response.resources.compute
    [database] = response.resources.database
    assert fake.disks[(disk.zone, disk.name)]["diskEncryptionKey"] == {"kmsKeyName": key.provider_id}
    assert fake.instances[(machine.zone, machine.name)]["disks"][0]["diskEncryptionKey"] == {"kmsKeyName": key.provider_id}, "the boot disk too"
    assert fake.sql[database.name]["diskEncryptionConfiguration"] == {"kmsKeyName": key.provider_id}


def test_a_bucket_is_encrypted_by_default_with_the_key(tmp_path):
    _, fake, _, response = provision(tmp_path, {"storage": {"type": "object"}, "encryption_key": {}})
    [bucket] = response.resources.storage
    [key] = response.resources.encryption_key
    assert fake.buckets[bucket.name]["encryption"] == {"defaultKmsKeyName": key.provider_id} and key.encrypts == ["bucket"]
    assert fake.members(("key", key.provider_id), ROLE) == ["serviceAccount:service-123456789@gs-project-accounts.iam.gserviceaccount.com"]


def test_destroy_schedules_the_keys_versions_last_and_keeps_the_key(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {**EVERYTHING, "encryption_key": {}})
    [key] = response.resources.encryption_key
    calls = len(fake.calls)
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert [version["state"] for version in fake.crypto_keys[key.provider_id]["versions"]] == ["DESTROY_SCHEDULED"]
    destroyed = [index for index, (method, url, _) in enumerate(fake.calls[calls:]) if url.endswith(":destroy")]
    deletes = [index for index, (method, url, _) in enumerate(fake.calls[calls:]) if method == "DELETE"]
    assert destroyed and max(deletes) < min(destroyed), "nothing it encrypts outlives its key"
    assert key.provider_id in fake.crypto_keys and not fake.disks and not fake.sql, "Cloud KMS keeps the key; the rest is gone"


def test_the_key_is_priced_per_version(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "storage": {"type": "object"}, "encryption_key": {}}),
                             principal(orchestrator.config))
    [line] = [item for item in plan.estimate.lines if item.kind == "encryption_key"]
    assert line.monthly_usd == 0.06 and plan.planned[0].kind == "encryption_key", "made before what it encrypts"


@pytest.mark.parametrize("body, said", [({"encryption_key": {}, "queue": {"name": "q"}}, "nothing it encrypts"),
                                        ({"encryption_key": {"rotation_days": 0}, "storage": {}}, "greater than or equal to 1"),
                                        ({"encryption_key": {"destroy_after_days": 121}, "storage": {}}, "less than or equal to 120")])
def test_an_encryption_key_block_is_checked(body, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", **body})
    assert said in str(caught.value)
