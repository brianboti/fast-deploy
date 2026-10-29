"""The plan: every check before anything touches a machine (docs/DELIVERY-SPEC.md sections 2 to 4)."""
import pytest
from delivery_fakes import DEV_SA, DIGEST, PROD_SA, infra_response, machine, make_config, principal, release_dict

from appdelivery import planner
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraRequest
from appdelivery.models import ReleaseRequest


def plan(release=None, answer=None, config=None, who="shop-team"):
    config = config or make_config()
    return planner.plan(ReleaseRequest.model_validate(release or release_dict()), principal(config, who), config, InfraRequest.model_validate(answer or infra_response()))


def refusal(**kwargs):
    with pytest.raises(DeliveryError) as caught:
        plan(**kwargs)
    return caught.value


def codes(error):
    return [item["code"] for item in error.details.get("problems", [])] or [error.code.value]


def test_a_good_release_plans_both_machines_and_names_its_bindings_without_values():
    planned = plan()
    assert [item.name for item in planned.machines] == ["shop-web-1", "shop-web-2"] and planned.machines[0].instance_id == "1234567890"
    assert planned.env == {"LOG_LEVEL": "value", "DB_HOST": "endpoint database_host", "DB_PASSWORD": "secret db-password"}
    assert planned.service_account == PROD_SA and planned.attestation_required and planned.approval_required
    assert planned.warnings == []


def test_a_caller_without_can_deploy_or_outside_the_environment_is_refused_first():
    assert refusal(who="shop-oncall").code == ErrorCode.PERMISSION_DENIED
    config = make_config()
    config.principals[0].environments = ["dev"]
    assert refusal(config=config).code == ErrorCode.PERMISSION_DENIED


def test_an_unknown_environment_is_refused():
    assert refusal(release=release_dict(environment="staging")).code == ErrorCode.UNKNOWN_ENVIRONMENT


def test_another_teams_request_is_not_found_and_nothing_else_is_said_about_it():
    error = refusal(answer=infra_response(owner="payments-team"))
    assert codes(error) == ["NOT_FOUND"] and "payments" not in error.message


def test_a_request_that_is_not_ready_is_refused():
    assert "INVALID_STATE" in codes(refusal(answer=infra_response(status="STOPPED")))


def test_without_the_teams_consent_there_is_no_release():
    error = refusal(answer=infra_response(access=[]))
    assert codes(error) == ["NOT_GRANTED"] and "manage_infrastructure_access" in error.message


@pytest.mark.parametrize(("level", "status"), [("use", "GRANTED"), ("admin", "PARTIAL"), ("admin", "FAILED"), ("admin", "PENDING")])
def test_the_grant_must_be_admin_and_granted(level, status):
    answer = infra_response()
    answer["access"][0].update(level=level, status=status)
    assert "NOT_GRANTED" in codes(refusal(answer=answer))


def test_the_grant_of_another_environment_does_not_count():
    assert "NOT_GRANTED" in codes(refusal(release=release_dict(environment="dev")))
    answer = infra_response()
    answer["access"][0]["member"] = f"serviceAccount:{DEV_SA}"
    assert plan(release=release_dict(environment="dev", image=f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}"), answer=answer).service_account == DEV_SA


def test_a_machine_the_grant_does_not_cover_is_refused():
    answer = infra_response()
    answer["access"][0]["resources"] = ["cmp-111111111111"]
    error = refusal(answer=answer)
    assert codes(error) == ["NOT_GRANTED"] and "shop-web-2" in error.message


def test_an_unknown_group_names_the_groups_there_are():
    error = refusal(release=release_dict(target={"group": "api"}))
    assert codes(error) == ["NOT_FOUND"] and "web" in error.message


def test_a_machine_without_container_os_is_refused_with_the_profile_to_ask_for():
    answer = infra_response()
    answer["resources"]["compute"][0]["os_image"] = "ubuntu-2404-lts-amd64"
    error = refusal(answer=answer)
    assert codes(error) == ["UNSUPPORTED_TARGET"] and "container-host" in error.message


def test_a_stopped_machine_is_refused():
    answer = infra_response()
    answer["resources"]["compute"][1]["status"] = "STOPPED"
    assert codes(refusal(answer=answer)) == ["INVALID_STATE"]


def test_a_network_without_iap_ssh_is_refused():
    answer = infra_response()
    answer["resources"]["network"][0]["firewall_rules"] = [rule for rule in answer["resources"]["network"][0]["firewall_rules"] if 22 not in rule["ports"]]
    assert set(codes(refusal(answer=answer))) == {"UNREACHABLE_TARGET"}


def test_the_default_network_counts_only_when_the_operator_says_it_admits_iap():
    answer = infra_response()
    for item in answer["resources"]["compute"]:
        item["network"] = None
    assert set(codes(refusal(answer=answer))) == {"UNREACHABLE_TARGET"}
    config = make_config(gcp={"project_id": "infra-project", "default_network_iap": True})
    assert len(plan(answer=answer, config=config).machines) == 2


def test_a_network_of_another_request_is_a_warning_not_a_refusal():
    answer = infra_response()
    answer["resources"]["network"] = []
    assert "could not be checked" in plan(answer=answer).warnings[0]


def test_a_port_open_only_inside_the_network_is_a_warning():
    answer = infra_response()
    answer["resources"]["network"][0]["firewall_rules"].pop()
    assert any("open only inside" in warning for warning in plan(answer=answer).warnings)


def test_a_registry_outside_the_environments_list_is_refused():
    error = refusal(release=release_dict(image=f"us-central1-docker.pkg.dev/someone-else/web@sha256:{DIGEST}"))
    assert "REGISTRY_NOT_ALLOWED" in codes(error)


def test_a_prefix_matches_only_whole_path_parts():
    error = refusal(release=release_dict(image=f"us-central1-docker.pkg.dev/acme-apps-evil/web@sha256:{DIGEST}"))
    assert "REGISTRY_NOT_ALLOWED" in codes(error)


def test_a_registry_on_the_internet_needs_a_public_address():
    config = make_config()
    config.environments["prod"].allowed_registries.append("ghcr.io/acme")
    release = release_dict(image=f"ghcr.io/acme/web@sha256:{DIGEST}")
    assert set(codes(refusal(release=release, config=config))) == {"UNREACHABLE_TARGET"}
    answer = infra_response()
    answer["resources"]["compute"] = [machine("cmp-111111111111", "shop-web-1", public_ip="34.1.2.3")]
    answer["access"][0]["resources"] = ["cmp-111111111111"]
    assert len(plan(release=release, answer=answer, config=config).machines) == 1


def test_bindings_must_exist_in_the_request():
    error = refusal(release=release_dict(env={"REDIS": {"endpoint": "cache_host"}, "AUTH_STRING": {"secret": "cache-auth"}}))
    assert codes(error) == ["INVALID_BINDING", "INVALID_BINDING"] and "db-password" in error.details["problems"][1]["message"]


def test_groups_that_stop_themselves_are_warned_about():
    answer = infra_response()
    answer["resources"]["compute"][0]["autostop_idle_minutes"] = 60
    answer["resources"]["compute"][1]["max_run_hours"] = 24
    warnings = plan(answer=answer).warnings
    assert any("autostop" in warning for warning in warnings) and any("max_run_hours" in warning for warning in warnings)


def test_every_problem_is_reported_at_once():
    answer = infra_response(status="STOPPED", access=[])
    error = refusal(answer=answer, release=release_dict(image=f"us-central1-docker.pkg.dev/other/web@sha256:{DIGEST}"))
    assert {"INVALID_STATE", "NOT_GRANTED", "REGISTRY_NOT_ALLOWED"} <= set(codes(error)) and "more problems" in error.message
