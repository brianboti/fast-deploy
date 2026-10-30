"""Spending budgets (coverage research, 2026-10-30: managing cloud spend is the first challenge companies name, and 29
percent of it is wasted, Flexera 2026): a monthly amount on the costs carrying one request's label, alerting the request's
addresses as spending, or the month's forecast, crosses each threshold. It stops nothing."""
import pytest
from gcp_fakes import make_config, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureError, InfrastructureRequest

BUDGET = {"monthly_amount": 500, "notify": ["ops@example.com", "lead@example.com"]}


def provision(tmp_path, body, config=None):
    orchestrator, fake = make_orchestrator(tmp_path, config)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_a_budget_counts_the_requests_own_costs_and_alerts_its_addresses(tmp_path):
    _, fake, _, response = provision(tmp_path, {"queue": {"name": "events"}, "budget": BUDGET})
    assert response.error is None, response.error
    [budget] = response.resources.budget
    made = fake.budgets[budget.budget]
    assert budget.budget.startswith("billingAccounts/01ABCD-234567-89EFAB/budgets/"), "the project's own billing account, read from Cloud Billing"
    assert made["budgetFilter"]["labels"] == {"cloudinfra-request": [response.request_id]} and made["budgetFilter"]["projects"] == ["projects/123456789"]
    assert made["amount"] == {"specifiedAmount": {"units": "500"}} and made["budgetFilter"]["calendarPeriod"] == "MONTH"
    assert [(rule["thresholdPercent"], rule["spendBasis"]) for rule in made["thresholdRules"]] == [(0.5, "CURRENT_SPEND"), (0.9, "CURRENT_SPEND"), (1.0, "CURRENT_SPEND"),
                                                                                                  (1.0, "FORECASTED_SPEND")]
    assert made["notificationsRule"]["disableDefaultIamRecipients"] is True, "the team hears of its own budget, not every billing administrator"
    channels = made["notificationsRule"]["monitoringNotificationChannels"]
    assert len(channels) == 2 and {fake.monitoring[name]["labels"]["email_address"] for name in channels} == set(BUDGET["notify"])
    assert budget.watches == response.request_id and "It alerts and stops nothing" in budget.connect.notes


def test_the_operators_billing_account_wins_and_destroy_leaves_nothing(tmp_path):
    config = make_config(tmp_path)
    config.providers.gcp.billing_account = "0A0A0A-1B1B1B-2C2C2C"
    orchestrator, fake, who, response = provision(tmp_path, {"budget": {**BUDGET, "thresholds": [120, 80], "forecast": False}}, config)
    [budget] = response.resources.budget
    assert budget.billing_account == "billingAccounts/0A0A0A-1B1B1B-2C2C2C" and budget.thresholds == [80, 120]
    assert [rule["spendBasis"] for rule in fake.budgets[budget.budget]["thresholdRules"]] == ["CURRENT_SPEND", "CURRENT_SPEND"]
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.budgets and not fake.monitoring


def test_a_project_without_billing_is_refused(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.billing_info = {"billingEnabled": False}
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "budget": BUDGET}), principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and "billing_account" in str(response.error) and not fake.monitoring


def test_create_budget_watches_an_earlier_request_of_the_caller(tmp_path):
    orchestrator, fake, who, first = provision(tmp_path, {"queue": {"name": "events"}})
    added = orchestrator.create_budget(who, request_id=first.request_id, monthly_amount=50, notify=["ops@example.com"], wait_seconds=45)
    [budget] = added.resources.budget
    assert budget.watches == first.request_id and fake.budgets[budget.budget]["budgetFilter"]["labels"] == {"cloudinfra-request": [first.request_id]}
    assert added.region == "us-central1" and added.request_id != first.request_id


def test_a_budget_may_not_watch_someone_elses_request(tmp_path):
    orchestrator, fake, who, first = provision(tmp_path, {"queue": {"name": "events"}})
    stranger = who.model_copy(update={"name": "other-team"})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.create_budget(stranger, request_id=first.request_id, monthly_amount=50, notify=["ops@example.com"])
    assert caught.value.code == "NOT_FOUND"
    request = InfrastructureRequest.model_validate({"region": "us-central1", "budget": {**BUDGET, "request_id": first.request_id}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, stranger)
    assert caught.value.code == "NOT_FOUND" and not fake.budgets, "a full request is held to the same rule"


def test_alerts_go_only_to_allowed_domains(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    found = orchestrator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "budget": {**BUDGET, "notify": ["me@gmail.com"]}}),
                                  principal(orchestrator.config))
    assert not found.valid and any(problem.details.get("field") == "budget.notify" or "budget.notify" in problem.message for problem in found.problems)


def test_a_budget_is_free(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "budget": BUDGET}), principal(orchestrator.config))
    assert [line.monthly_usd for line in plan.estimate.lines] == [0.0] and plan.planned[0].kind == "budget"


@pytest.mark.parametrize("budget, said", [({**BUDGET, "monthly_amount": 0}, "greater than or equal to 1"), ({**BUDGET, "thresholds": [50, 50]}, "distinct"),
                                          ({**BUDGET, "thresholds": [600]}, "1 to 500"), ({**BUDGET, "notify": ["Ops@Example.com"]}, "lowercase"),
                                          ({**BUDGET, "notify": [f"a{i}@example.com" for i in range(6)]}, "at most 5"),
                                          ({**BUDGET, "request_id": "nope"}, "not an identifier")])
def test_a_budget_block_is_checked(budget, said):
    with pytest.raises((ValidationError, InfrastructureError)) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "budget": budget})
    assert said in str(caught.value)
