"""A spending budget behind the generic budget primitives (Cloud Billing budgets): a monthly amount for the costs that
carry one request's label, and an email to the request's addresses as spending, or the month's forecast, crosses each
threshold (https://docs.cloud.google.com/billing/docs/how-to/budgets). Every resource this service makes carries the
label, so the budget counts what the request costs and nothing else. It alerts and stops nothing.

The billing account is the operator's providers.gcp.billing_account, or the project's own, read from Cloud Billing.
Emails go through Cloud Monitoring channels, as a monitor's do, and not to the account's billing administrators, who
would otherwise hear of every request. Billing data arrives some hours late, so an alert is too."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import BudgetResource, ResourceStatus
from . import translate
from .api import BILLING, BILLING_BUDGETS, MONITORING
from .monitor import delete_quietly, email_channel


class GcpBudget:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _account(self) -> str:
        if self.settings.billing_account:
            return f"billingAccounts/{self.settings.billing_account}"
        found = self.api.get(f"{BILLING}/projects/{self.api.project}/billingInfo")
        if not found.get("billingEnabled") or not found.get("billingAccountName"):
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Project {self.api.project} has no billing account to hold a budget; the operator sets "
                                      "providers.gcp.billing_account", {"project": self.api.project})
        return found["billingAccountName"]

    def create(self, spec, planned, ctx) -> BudgetResource:
        account, watched = self._account(), planned.spec["watch"]
        resource = BudgetResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=account,
                                  status=ResourceStatus.CREATING, monthly_amount=spec.monthly_amount, thresholds=list(spec.thresholds), forecast=spec.forecast,
                                  notify=list(spec.notify), watches=watched, billing_account=account, labels=translate.labels(ctx))
        for address in spec.notify:
            resource = resource.model_copy(update={"channels": [*resource.channels, email_channel(self.api, f"{planned.name} {address}", address, translate.labels(ctx))]})
            ctx.accepted(resource)  # each channel is known as Google takes it, so a failure further on deletes it
        rules = [{"thresholdPercent": percent / 100, "spendBasis": "CURRENT_SPEND"} for percent in spec.thresholds]
        if spec.forecast:
            rules.append({"thresholdPercent": 1.0, "spendBasis": "FORECASTED_SPEND"})
        made = self.api.post(f"{BILLING_BUDGETS}/{account}/budgets", json={
            "displayName": planned.name[:60],
            "budgetFilter": {"projects": [f"projects/{self.api.project_number()}"], "labels": {"cloudinfra-request": [translate.label_value(watched)]},
                             "calendarPeriod": "MONTH", "creditTypesTreatment": "INCLUDE_ALL_CREDITS"},
            "amount": {"specifiedAmount": {"units": str(spec.monthly_amount)}},  # in the billing account's own currency
            "thresholdRules": rules,
            "notificationsRule": {"monitoringNotificationChannels": list(resource.channels), "disableDefaultIamRecipients": True}})
        return resource.model_copy(update={"budget": made["name"], "provider_id": made["name"], "status": ResourceStatus.READY})

    def get(self, resource: BudgetResource) -> BudgetResource:
        if not resource.budget:
            return resource
        try:
            self.api.get(f"{BILLING_BUDGETS}/{resource.budget}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: BudgetResource, ctx) -> None:
        if resource.budget:
            delete_quietly(self.api, f"{BILLING_BUDGETS}/{resource.budget}")
        for channel in resource.channels:
            delete_quietly(self.api, f"{MONITORING}/{channel}")
