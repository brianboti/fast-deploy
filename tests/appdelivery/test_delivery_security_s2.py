"""Release S2 of the security assessment of 2026-10-30 (docs/research/RESEARCH-2026-10-30-security-assessment.md, section 7),
delivery side, and the gate both services share. Each test names the finding it holds."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The parts of the gate both services carry, written twice because neither package imports the other (L14).
SHARED = ("pepper", "keyed", "check_pepper", "find_principal", "RateLimiter", "Authenticate", "failed", "as_tool_error")
NAMES = {"cloudinfra": "SERVICE", "appdelivery": "SERVICE", "CLOUDINFRA": "SERVICE", "APPDELIVERY": "SERVICE"}


def shared_parts(path: Path) -> dict[str, str]:
    """Each shared function or class without its docstrings and comments, with the service's own names made one."""
    tree = ast.parse(path.read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in SHARED:
            for inner in ast.walk(node):
                body = getattr(inner, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    inner.body = body[1:] or [ast.Pass()]
            text = ast.unparse(node)
            for name, same in NAMES.items():
                text = text.replace(name, same)
            found[node.name] = text
    return found


def test_the_two_gates_have_not_drifted():
    infrastructure, delivery = shared_parts(ROOT / "cloudinfra/security/auth.py"), shared_parts(ROOT / "appdelivery/auth.py")
    assert set(infrastructure) == set(delivery) == set(SHARED)
    for name in SHARED:
        assert infrastructure[name] == delivery[name], f"{name} differs between cloudinfra/security/auth.py and appdelivery/auth.py: change both"


@pytest.mark.parametrize("path", ["cloudinfra/observability/logging.py", "appdelivery/events.py"])
def test_both_logs_redact_credentials_by_their_shape(path):
    parts = {node.name: ast.unparse(node) for node in ast.parse((ROOT / path).read_text()).body if isinstance(node, ast.FunctionDef)}
    assert "redact_text" in parts and "redact_text(value)" in parts["redact"]


def test_the_delivery_log_redacts_a_token_in_free_text():
    from appdelivery.events import redact
    assert redact({"detail": "gcloud said ya29.secret-token and Bearer abc"}) == {"detail": "gcloud said [redacted] and [redacted]"}


# ---- L13: a live read is an action on the target, checked again first
def test_a_live_read_after_the_grant_is_gone_reaches_no_target():
    from fleet_fakes import World, dev_answer
    from test_delivery_cluster import deploy
    world = World(cluster=True)
    first = deploy(world)
    reads = world.infra.reads
    world.infra.answers["dev"]["access"] = []
    [group] = world.delivery.running(first.request_id, world.team, live=True)["groups"]
    assert world.infra.reads > reads, "the infrastructure layer was read again"
    assert group["error"]["code"] in ("NOT_GRANTED", "PERMISSION_DENIED") and "running" not in group["machines"][0]
    world.infra.answers["dev"] = dev_answer(cluster=True)
    [group] = world.delivery.running(first.request_id, world.team, live=True)["groups"]
    assert "error" not in group and group["machines"][0]["running"]


# ---- what this service reads from cloudinfra, and where it sends its token (L11)
def answer_with(**changes):
    from delivery_fakes import cluster_response
    answer = cluster_response()
    answer["resources"]["cluster"][0].update(changes)
    return answer


@pytest.mark.parametrize("changes", [{"endpoint": "35.201.0.7/../evil"}, {"endpoint": "https://35.201.0.7"}, {"ca_certificate": "not base64!"},
                                     {"name": "shop cluster; rm"}, {"namespace": "../kube-system"}])
def test_an_answer_value_of_the_wrong_shape_is_refused(changes):
    from pydantic import ValidationError

    from appdelivery.infra import InfraRequest
    with pytest.raises(ValidationError):
        InfraRequest.model_validate(answer_with(**changes))


@pytest.mark.parametrize("field, value", [("zone", "us-central1-a --flags-file=/x"), ("name", "-oProxyCommand=evil")])
def test_a_machine_name_or_zone_that_would_reach_gcloud_as_a_flag_is_refused(field, value):
    from delivery_fakes import infra_response
    from pydantic import ValidationError

    from appdelivery.infra import InfraRequest
    answer = infra_response()
    answer["resources"]["compute"][0][field] = value
    with pytest.raises(ValidationError):
        InfraRequest.model_validate(answer)


def test_cloudinfra_is_reached_over_https_only_except_on_this_host():
    from delivery_fakes import make_config
    from pydantic import ValidationError

    for url in ("https://infra.example.com", "http://localhost:8100", "http://127.0.0.1:8100"):
        make_config(cloudinfra={"url": url})
    with pytest.raises(ValidationError, match="must be https"):
        make_config(cloudinfra={"url": "http://infra.internal:8100"})


@pytest.mark.parametrize("execution", ["shop-job-x/../../services/other", "shop-job-1?alt=media", "shop-job-00001/cancel"])
def test_a_run_name_that_is_not_a_name_is_not_found_and_reaches_no_url(execution):
    from fleet_fakes import World
    from test_delivery_job import deploy

    from appdelivery.errors import DeliveryError
    world = World(job=True)
    done = deploy(world)
    asked, request = [], world.run_job.request
    world.run_job.request = lambda *args, **kwargs: asked.append(args) or request(*args, **kwargs)
    with pytest.raises(DeliveryError) as caught:
        world.delivery.get_run(done.release_id, execution, world.team)
    assert caught.value.code.value == "NOT_FOUND" and asked == [], "Cloud Run was not asked"


# ---- L9 seen from here: a Cloud Run runtime account reads only what it names
def test_a_binding_to_a_secret_the_runtime_account_does_not_read_is_refused():
    from delivery_fakes import DIGEST, make_config, principal, release_dict, service_response

    from appdelivery import planner
    from appdelivery.errors import DeliveryError
    from appdelivery.infra import InfraRequest
    from appdelivery.models import ReleaseRequest
    config = make_config()
    release = ReleaseRequest.model_validate(release_dict(environment="prod", image=f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}",
                                                         target={"service": True}))
    answer = service_response()
    answer["resources"]["service"][0]["secrets"] = []
    with pytest.raises(DeliveryError) as caught:
        planner.plan(release, principal(config), config, InfraRequest.model_validate(answer))
    assert "may not read secret db-password" in str(caught.value.details)


# ---- M10 seen from here: a network with a NAT pulls from the internet
def test_a_machine_without_a_public_address_pulls_from_the_internet_through_its_networks_nat():
    from delivery_fakes import DIGEST, infra_response, make_config, principal, release_dict

    from appdelivery import planner
    from appdelivery.infra import InfraRequest
    from appdelivery.models import ReleaseRequest
    config = make_config()
    config.environments["prod"].allowed_registries.append("ghcr.io/acme")
    release = ReleaseRequest.model_validate(release_dict(image=f"ghcr.io/acme/web@sha256:{DIGEST}"))
    answer = infra_response()
    answer["resources"]["network"][0]["nat"] = True
    assert len(planner.plan(release, principal(config), config, InfraRequest.model_validate(answer)).machines) == 2


# ---- L7 here too
def test_a_keyed_hash_works_for_the_delivery_service(monkeypatch):
    from delivery_fakes import make_config

    from appdelivery.auth import find_principal, keyed
    config = make_config()
    data = config.model_dump()
    data["principals"][0].update(token_sha256=None, token_hmac_sha256=keyed("team-token", b"k" * 40))
    keyed_config = type(config).model_validate(data)
    monkeypatch.setenv("APPDELIVERY_TOKEN_PEPPER", "k" * 40)
    assert find_principal(keyed_config, "team-token").name == data["principals"][0]["name"]
    monkeypatch.delenv("APPDELIVERY_TOKEN_PEPPER")
    assert find_principal(keyed_config, "team-token") is None
