"""Log export (coverage research, 2026-10-30: companies run eight observability tools on average, Grafana 2025, and keeping
logs is a common compliance duty): the logs of what the request runs, routed to its own bucket or dataset by a sink whose
own identity is granted on that destination only."""
import pytest
from gcp_fakes import make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest

WRITER = "serviceAccount:service-123456789@gcp-sa-logging.iam.gserviceaccount.com"
RUNS = {"compute": {"cpu": 2, "memory_gb": 8}, "service": {"name": "web"}}


def provision(tmp_path, body):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_logs_go_to_the_requests_bucket_as_a_writer_granted_there_only(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {**RUNS, "storage": {"type": "object"}, "log_export": {"to": "storage", "min_severity": "WARNING"}})
    assert response.error is None, response.error
    [export] = response.resources.log_export
    [bucket] = response.resources.storage
    sink = fake.sinks[export.provider_id]
    assert sink["destination"] == f"storage.googleapis.com/{bucket.name}" and export.writer_identity == WRITER
    assert sink["filter"].startswith("severity>=WARNING AND (") and 'labels."compute.googleapis.com/resource_name"="shop-vm-1"' in sink["filter"]
    assert 'resource.labels.service_name="shop-web"' in sink["filter"] and "cloud_run_revision" in sink["filter"]
    assert WRITER in fake.members(("bucket", bucket.name), "roles/storage.objectCreator"), "it may add objects, nothing more"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.sinks and not fake.buckets


def test_logs_go_to_a_named_dataset_in_partitioned_tables(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {**RUNS, "warehouse": [{"name": "sales"}, {"name": "audit"}], "log_export": {"to": "warehouse:audit"}})
    [export] = response.resources.log_export
    sink = fake.sinks[export.provider_id]
    assert sink["destination"].endswith("/datasets/shop_audit") and sink["bigqueryOptions"] == {"usePartitionedTables": True}
    assert not sink["filter"].startswith("severity"), "every entry when no severity is asked"
    assert {"role": "WRITER", "userByEmail": WRITER.split(":", 1)[1]} in fake.datasets["shop_audit"]["access"]
    assert not [entry for entry in fake.datasets["shop_sales"]["access"] if WRITER.split(":", 1)[1] in str(entry)], "the other dataset is untouched"
    orchestrator.delete_resource(export.id, who, export.kind, wait_seconds=45)
    assert not fake.sinks and not [entry for entry in fake.datasets["shop_audit"]["access"] if WRITER.split(":", 1)[1] in str(entry)], "the grant goes with the sink"


def test_a_database_and_a_cluster_are_selected_by_their_own_names(tmp_path):
    _, fake, _, response = provision(tmp_path, {"network": {}, "database": {}, "storage": {"type": "object"}, "log_export": {"to": "storage"}})
    [database] = response.resources.database
    [export] = response.resources.log_export
    assert f'resource.labels.database_id="{fake.project}:{database.name}"' in export.filter
    assert "read" in export.connect.commands


@pytest.mark.parametrize("body, said", [
    ({**RUNS, "log_export": {"to": "storage"}}, "has no bucket"),
    ({**RUNS, "storage": {"type": "persistent"}, "log_export": {"to": "storage"}}, "has no bucket"),
    ({**RUNS, "warehouse": [{"name": "a"}, {"name": "b"}], "log_export": {"to": "warehouse"}}, "name one"),
    ({**RUNS, "warehouse": {}, "log_export": {"to": "warehouse:other"}}, "no such warehouse"),
    ({"storage": {"type": "object"}, "log_export": {"to": "storage"}}, "runs nothing that writes logs"),
    ({**RUNS, "storage": {"type": "object"}, "log_export": {"to": "bucket"}}, "should match pattern"),
    ({**RUNS, "storage": {"type": "object"}, "log_export": {"to": "storage", "min_severity": "LOUD"}}, "DEBUG"),
])
def test_a_log_export_block_is_checked(body, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", **body})
    assert said in str(caught.value)
