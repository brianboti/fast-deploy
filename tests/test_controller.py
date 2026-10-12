import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_deploy_idempotent_and_status(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    assert plan["checks"]["tests"] == "pending_in_ci"
    assert plan["cost"]["scenario"] is None and plan["cost"]["compute_ceiling_usd_month"] == 132.714
    assert plan["cost"]["ceiling_policy"]["status"] == "not_configured"
    assert c.deploy(p, plan["id"])["state"] == "dispatched"
    c.deploy(p, plan["id"])
    assert gh.calls == 1
    assert c.status(p, plan["id"])["workflow"]["conclusion"] == "success"


def test_concurrent_requests_dispatch_once(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: c.deploy(p, plan["id"]), range(8)))
    assert gh.calls == 1


def test_branch_moved(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    gh.sha = "b" * 40
    with pytest.raises(ValueError, match="Branch moved"):
        c.deploy(p, plan["id"])
    assert gh.calls == 0


def test_timeout_not_retried(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    gh.fail = True
    with pytest.raises(ValueError, match="unknown"):
        c.deploy(p, plan["id"])
    assert c.deploy(p, plan["id"])["state"] == "dispatch_unknown"
    assert gh.calls == 1


def test_plan_identity_boundary(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    other = p.model_copy(update={"name": "other"})
    with pytest.raises(ValueError, match="not found"):
        c.deploy(other, plan["id"])


def test_read_only_identity(setup):
    c, p, gh = setup
    p.can_deploy = False
    plan = c.plan(p, "demo")
    with pytest.raises(ValueError, match="read-only"):
        c.deploy(p, plan["id"])


def test_expired(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    plan["expires_at"] = time.time() - 1
    c.store.finish(p.name, plan, "planned")
    with pytest.raises(ValueError, match="expired"):
        c.deploy(p, plan["id"])


def test_live_dispatch_off(setup):
    c, p, gh = setup
    c.config.live_dispatch = False
    plan = c.plan(p, "demo")
    assert c.deploy(p, plan["id"])["dispatch"] == "disabled"
    assert gh.calls == 0


@pytest.mark.parametrize("change", [{"reviewed": False}, {"gpu": True}, {"dependencies": ["postgres"]}, {"resources": {"cpu": 4}}, {"name": "other"}, {"scaling": {"maximum": 5}}])
def test_disallowed_config(setup, change):
    c, p, gh = setup
    gh.spec.update(change)
    with pytest.raises(ValueError):
        c.plan(p, "demo")


@pytest.mark.parametrize("cloud,environment", [("aws", "staging"), ("gcp", "production")])
def test_unsupported_targets(setup, cloud, environment):
    c, p, gh = setup
    with pytest.raises(ValueError, match="GCP staging"):
        c.plan(p, "demo", cloud, environment)

