"""What the run of releases S1 and S2 on Google Cloud found on 2026-11-02 (docs/DELIVERY-SPIKE.md section 16), infrastructure
side, each held by a test that fails without its fix."""
import httpx
import pytest
from gcp_fakes import make_config, make_provider
from test_infra_gcp_provider import planned_for
from test_infra_release_a import orchestrated, provision

from cloudinfra.models import ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.providers.gcp.api import GcpApi


def database_network(refusals):
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "web", "network": {}, "database": {}})
    network = provider.prepare_network_for_database(provider.create_network(request.network, planned[0], ctx), ctx)
    fake.peering_in_use = refusals
    provider.delete_network(network, ctx)
    assert not fake.networks and not fake.subnets and not fake.addresses and not fake.peerings
    assert provider.get_network(network).status == ResourceStatus.DELETED
    return fake


def test_a_database_connection_still_in_use_is_tried_again_until_google_releases_it():
    # Right after its Cloud SQL instance was deleted, Service Networking refused to delete the connection ("Producer
    # services ... are still using this connection"); the destroy failed and the network stayed. Two minutes later it went.
    fake = database_network(refusals=4)
    assert len([call for call in fake.calls if call[1].endswith(":deleteConnection")]) == 5
    assert not any(call[1].endswith("/removePeering") for call in fake.calls)


def test_a_connection_held_past_the_wait_is_let_go_from_the_networks_side():
    fake = database_network(refusals=1000)
    assert any(call[1].endswith("/global/networks/web-net/removePeering") for call in fake.calls)


@pytest.mark.parametrize("module", ["cloudinfra.observability.logging", "appdelivery.events"])
@pytest.mark.parametrize(("text", "kept"), [
    # the node-role check's event and call on the run: the role and its URL were redacted whole
    ("projects/project-3e2167e6-db58-4ab7-a22/roles/cloudinfraGrants", True),
    ("https://iam.googleapis.com/v1/projects/project-3e2167e6-db58-4ab7-a22/roles/cloudinfraGrants", True),
    ("v1/projects/acme-apps/zones/us-central1-a/instances/shop-web-1/setDeletionProtection", True),
    # credentials with slashes stay redacted, a path or not
    ("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/+abcdEFGH=", False),
    ("k9Xq2Lm8/Rt4Pw7Zs3Vn6/Bh1Jd5Fg0Ty8Qw2Er5", False),
    ("projects/acme/keys/q7w8e9r0t1y2u3i4o5p6a7s8d9f0g1h2j3k4l5z6", False),
])
def test_a_resource_path_is_kept_in_the_logs_and_a_credential_is_not(module, text, kept):
    import importlib
    redacted = importlib.import_module(module).redact_text(f"call {text} done")
    assert (redacted == f"call {text} done") is kept, redacted


def api_failing_first(error, answer=None):
    calls = []

    def handle(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise error("down", request=request)
        return httpx.Response(200, json=answer or {"status": "DONE"})
    return GcpApi("test-project", token=lambda: "t", client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=lambda seconds: None), calls


def test_a_read_or_an_unsent_call_that_met_a_network_error_is_tried_again():
    # While a cluster was waited on, one connect timeout on reading its operation failed the request after five minutes.
    api, calls = api_failing_first(httpx.ConnectTimeout)
    assert api.get("https://container.googleapis.com/v1/projects/p/zones/z/operations/o") == {"status": "DONE"} and calls == ["GET", "GET"]
    api, calls = api_failing_first(httpx.ReadTimeout)
    assert api.get("https://container.googleapis.com/v1/projects/p/zones/z/operations/o") == {"status": "DONE"} and calls == ["GET", "GET"]
    api, calls = api_failing_first(httpx.ConnectError)
    assert api.post("https://compute.googleapis.com/compute/v1/projects/p/global/networks", json={}) == {"status": "DONE"} and calls == ["POST", "POST"]


def test_a_write_whose_answer_was_lost_is_not_sent_twice():
    api, calls = api_failing_first(httpx.ReadTimeout)
    with pytest.raises(InfrastructureError) as caught:
        api.post("https://compute.googleapis.com/compute/v1/projects/p/global/networks", json={})
    assert calls == ["POST"] and caught.value.details["maybe_done"] is True


def test_a_cluster_with_private_nodes_needs_the_ranges_that_reach_its_control_plane(tmp_path):
    # With private nodes and no ranges listed, GKE turned authorized networks on with none: the cluster was made, and then
    # neither service could reach its API, so the request failed after five minutes. It is now refused before any call.
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"cluster_authorized_networks": []})
    with pytest.raises(InfrastructureError) as refused:
        orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "network": {}, "cluster": {}}), who)
    assert refused.value.code == ErrorCode.INVALID_CLUSTER_CONFIGURATION and "cluster_authorized_networks" in refused.value.message
    assert not fake.calls
    provision(orchestrator, who, {"name": "open", "network": {}, "cluster": {"private_nodes": False}})


def test_a_database_that_takes_longer_than_the_operation_timeout_to_make_is_waited_for(tmp_path):
    # The first instance on a private network took 27 minutes; the request failed at the 15-minute operation timeout.
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.sql_create_seconds = 27 * 60
    response = provision(orchestrator, who, {"name": "shop", "network": {}, "database": {"engine": "mysql", "version": "8.0"}})
    assert response.status.value == "READY" and fake.sql


def test_a_cleanup_that_meets_an_instance_still_being_made_waits_for_it_and_deletes_it(tmp_path):
    # Cleanup after the timeout turned deletion protection off while the instance was still being made: Google refused
    # it (409), and the database stayed behind until a later destroy.
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.sql_create_seconds = 60 * 60
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "network": {}, "database": {}}), who, wait_seconds=45)
    assert orchestrator.status(response.request_id, who, refresh=False).status.value == "FAILED"
    assert not fake.sql and not fake.networks


@pytest.mark.parametrize("block", [{"service": {}}, {"job": {}}])
def test_a_service_or_job_made_before_cloud_run_knows_its_new_account_is_tried_again(tmp_path, block):
    # Cloud Run refused the service seconds after its runtime account was made ("Permission 'iam.serviceaccounts.actAs'
    # denied on service account run-... (or it may not exist)"), and the request's database, made for 21 minutes, went with it.
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.run_account_unknown = 3
    provision(orchestrator, who, {"name": "shop", **block})
    assert fake.run_account_unknown == 0 and (fake.run_services or fake.run_jobs)


def test_an_operations_wait_is_a_read_given_the_two_minutes_google_holds_it():
    # Deleting m1-web-1 took over a minute; the operation's wait timed out at the client's 60 seconds and, as a POST that
    # lost its answer, was not tried again, so the destroy failed. A wait changes nothing and is now read as such.
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"]["read"])
        if len(timeouts) == 1:
            raise httpx.ReadTimeout("held", request=request)
        return httpx.Response(200, json={"status": "DONE"})
    api = GcpApi("test-project", token=lambda: "t", client=httpx.Client(timeout=60, transport=httpx.MockTransport(handle)), sleep=lambda seconds: None)
    link = "https://compute.googleapis.com/compute/v1/projects/p/zones/z/operations/o"
    assert api.wait_compute({"status": "RUNNING", "selfLink": link}) == {"status": "DONE"}
    assert timeouts == [150, 150]
    api.post("https://compute.googleapis.com/compute/v1/projects/p/global/networks", json={})
    assert timeouts[-1] == 60


def test_a_network_cloud_run_still_holds_says_when_to_destroy_it_again(tmp_path):
    # The service was deleted, then its subnet was refused: Cloud Run keeps Direct VPC egress addresses 1-2 hours after.
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"name": "shop", "network": {}, "service": {}})
    fake.serverless_address = True
    failed = orchestrator.destroy(response.request_id, who)
    remaining = failed.error.details["remaining"]
    assert failed.status.value == "DESTROY_FAILED" and [item["name"] for item in remaining] == ["shop-net"] and not fake.run_services
    assert "1-2 hours" in remaining[0]["error"]["message"] and remaining[0]["error"]["details"]["retry_after_seconds"] == 7200
    fake.serverless_address = False
    assert orchestrator.destroy(response.request_id, who).status.value == "DESTROYED"
