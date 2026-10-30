"""Analytics datasets and document databases (coverage research, 2026-10-30: 71 percent of organizations use a cloud data
warehouse and 52 percent a NoSQL database, Flexera 2026): a BigQuery dataset with access on itself, deleted only when
empty as a bucket is; a Firestore database, deleted with its documents as a SQL database is, whose access is the
platform team's project-level grant because Firestore has no policy per database."""
import pytest
from gcp_fakes import make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest

DATA = "group:data@example.com"
APP = "serviceAccount:app@example.com"


def provision(tmp_path, body):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_a_dataset_is_made_in_the_region_and_granted_on_itself(tmp_path):
    _, fake, _, response = provision(tmp_path, {"warehouse": {"name": "sales", "table_expiration_days": 30},
                                                "access": [{"member": DATA, "level": "admin"}, {"member": APP, "level": "use"}]})
    assert response.error is None, response.error
    [warehouse] = response.resources.warehouse
    made = fake.datasets["shop_sales"]
    assert warehouse.dataset == "shop_sales" and made["location"] == "us-central1" and made["defaultTableExpirationMs"] == str(30 * 86_400_000)
    assert {"role": "OWNER", "groupByEmail": "data@example.com"} in made["access"] and {"role": "WRITER", "userByEmail": "app@example.com"} in made["access"]
    assert {(grant.member, grant.role) for grant in warehouse.access} >= {(DATA, "roles/bigquery.dataOwner"), (APP, "roles/bigquery.dataEditor")}
    assert "bigquery.jobs.create" in warehouse.connect.prerequisites[0]


def test_a_level_change_and_a_revoke_touch_only_that_member(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"warehouse": {}, "access": [{"member": DATA, "level": "admin"}]})
    orchestrator.manage_access(response.request_id, "grant", DATA, who, level="use")
    access = fake.datasets["shop_warehouse"]["access"]
    assert {"role": "WRITER", "groupByEmail": "data@example.com"} in access and {"role": "OWNER", "groupByEmail": "data@example.com"} not in access
    orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    assert [entry for entry in fake.datasets["shop_warehouse"]["access"] if entry.get("groupByEmail")] == []
    assert any(entry.get("userByEmail", "").startswith("cloudinfra@") for entry in fake.datasets["shop_warehouse"]["access"]), "another entry is never touched"


def test_a_dataset_is_deleted_only_when_empty(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"warehouse": {}})
    fake.datasets["shop_warehouse"]["tables"] = 2
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == "DESTROY_FAILED" and "still holds tables" in str(destroyed.error) and "shop_warehouse" in fake.datasets
    fake.datasets["shop_warehouse"]["tables"] = 0
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED" and not fake.datasets


def test_a_document_database_is_made_native_and_deleted_with_its_documents(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"nosql": [{"name": "orders"}, {"name": "carts"}]})
    assert response.error is None, response.error
    assert set(fake.firestore) == {"shop-orders", "shop-carts"} and fake.firestore["shop-orders"]["locationId"] == "us-central1"
    [orders] = [item for item in response.resources.nosql if item.database == "shop-orders"]
    assert "databases/shop-orders" in orders.connect.prerequisites[0], "access is the platform team's, limited to this database"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED" and not fake.firestore


def test_firestore_takes_no_grant_of_its_own(tmp_path):
    _, _, _, response = provision(tmp_path, {"nosql": {}, "access": [{"member": DATA, "level": "admin"}]})
    assert response.resources.nosql[0].access == []


@pytest.mark.parametrize("body, said", [({"warehouse": [{"name": "a"}, {}]}, "every warehouse of a list needs a name"),
                                        ({"nosql": [{"name": "a"}, {"name": "a"}]}, "names must be unique"),
                                        ({"warehouse": {"table_expiration_days": 0}}, "greater than or equal to 1")])
def test_data_blocks_are_checked(body, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    assert said in str(caught.value)
