"""Two switches beside the new kinds (coverage research, 2026-10-30): Cloud NAT on the request's network, so machines and
cluster nodes without public addresses reach the internet out and nothing reaches them in; and a daily snapshot schedule
on each disk, kept in the region, deleted with its disk as a database's backups are."""
import pytest
from gcp_fakes import make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureError, InfrastructureRequest

VMS = {"compute": {"cpu": 2, "memory_gb": 8, "count": 3}}


def provision(tmp_path, body):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_nat_gives_the_network_a_router_with_outbound_nat_only(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"network": {"nat": True}, **VMS})
    assert response.error is None, response.error
    router = fake.regional["routers"]["shop-net-router"]
    assert router["network"].endswith("/global/networks/shop-net") and router["region"] == "us-central1"
    [nat] = router["nats"]
    assert nat["natIpAllocateOption"] == "AUTO_ONLY" and nat["sourceSubnetworkIpRangesToNat"] == "ALL_SUBNETWORKS_ALL_IP_RANGES"
    assert response.resources.network[0].nat_router == "shop-net-router"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.regional["routers"] and not fake.networks


def test_nat_is_priced_per_machine_behind_it_and_absent_by_default(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    with_nat = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "network": {"nat": True}, **VMS}), who)
    [line] = [item for item in with_nat.estimate.lines if item.kind == "network"]
    assert line.monthly_usd == round(3 * 0.0014 * 730, 2) and "3 machines" in line.basis
    without = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "network": {}, **VMS}), who)
    assert [item.monthly_usd for item in without.estimate.lines if item.kind == "network"] == [0.0]


def test_a_network_without_nat_has_no_router(tmp_path):
    _, fake, _, response = provision(tmp_path, {"network": {}})
    assert response.error is None and not fake.regional.get("routers") and response.resources.network[0].nat_router is None


def test_each_disk_gets_its_own_daily_schedule_in_the_region(tmp_path):
    _, fake, _, response = provision(tmp_path, {**VMS, "storage": {"size_gb": 20, "snapshots": {"keep_days": 14, "hour_utc": 22}}})
    assert response.error is None, response.error
    policies = fake.regional["resourcePolicies"]
    assert len(policies) == 3 == len(response.resources.storage)
    disk = response.resources.storage[0]
    policy = policies[disk.snapshot_schedule]["snapshotSchedulePolicy"]
    assert policy["schedule"]["dailySchedule"] == {"daysInCycle": 1, "startTime": "22:00"} and policy["retentionPolicy"]["maxRetentionDays"] == 14
    assert policy["snapshotProperties"]["storageLocations"] == ["us-central1"] and policy["snapshotProperties"]["labels"]["cloudinfra-disk"] == disk.name
    assert fake.disks[(disk.zone, disk.name)]["resourcePolicies"][0].endswith(f"/resourcePolicies/{disk.snapshot_schedule}")
    assert disk.snapshot_keep_days == 14 and "snapshots" in disk.connect.commands


def test_deleting_a_disk_deletes_its_schedule_and_its_snapshots_only(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"storage": {"size_gb": 20, "snapshots": {}}})
    [disk] = response.resources.storage
    fake.take_snapshot(disk.name, disk.snapshot_schedule)
    fake.take_snapshot(disk.name, disk.snapshot_schedule)
    fake.snapshots["someone-elses"] = {"name": "someone-elses", "labels": {"cloudinfra-disk": "other"}}
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert set(fake.snapshots) == {"someone-elses"} and not fake.regional["resourcePolicies"] and not fake.disks


def test_snapshots_are_priced_as_one_copy_of_the_disk(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "storage": {"size_gb": 100, "snapshots": {"keep_days": 30}}}),
                             principal(orchestrator.config))
    [line] = plan.estimate.lines
    assert line.monthly_usd == round(100 * 0.10 + 100 * 0.05, 2) and "kept 30 days" in line.basis


def test_a_bucket_takes_no_snapshots():
    with pytest.raises((InfrastructureError, ValidationError)) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "storage": {"type": "object", "snapshots": {}}})
    assert "a bucket is not snapshotted" in str(caught.value)


@pytest.mark.parametrize("snapshots", [{"keep_days": 0}, {"keep_days": 366}, {"hour_utc": 24}])
def test_a_schedule_is_bounded(snapshots):
    with pytest.raises(ValidationError):
        InfrastructureRequest.model_validate({"region": "us-central1", "storage": {"snapshots": snapshots}})
