"""A reviewed cloud destination, independently checked on the deployment runner."""
import os
from typing import Literal

from pydantic import Field, model_validator

from .models import StrictModel


class Target(StrictModel):
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: Literal["us-central1"] = "us-central1"
    service: str = Field(pattern=r"^[a-z][a-z0-9-]{2,20}$")
    registry: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    deploy_service_account: str
    probe_service_account: str
    wif_provider: str = Field(pattern=r"^projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+$")
    recovery_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

    @model_validator(mode="after")
    def identities(self):
        import re
        for value in (self.deploy_service_account, self.probe_service_account):
            if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@" + re.escape(self.project_id) + r"\.iam\.gserviceaccount\.com", value):
                raise ValueError("Service accounts must belong to the reviewed GCP project")
        return self


ENV_FIELDS = {
    "project_id": "GCP_PROJECT_ID", "region": "GCP_REGION", "service": "SERVICE",
    "registry": "REGISTRY_NAME", "deploy_service_account": "DEPLOY_SERVICE_ACCOUNT",
    "probe_service_account": "PROBE_SERVICE_ACCOUNT", "wif_provider": "WIF_PROVIDER",
    "recovery_bucket": "RECOVERY_BUCKET",
}


def environment_target(environ=None):
    env = os.environ if environ is None else environ
    return Target.model_validate({field: env[name] for field, name in ENV_FIELDS.items()})


def verify_target(expected_json, actual):
    expected = Target.model_validate_json(expected_json)
    if expected != actual:
        raise ValueError("GitHub environment differs from the reviewed deployment target; create a new plan")
    return expected


if __name__ == "__main__":
    print(environment_target().model_dump_json())
