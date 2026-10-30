"""The managed job kind (docs/DELIVERY-SPEC.md, phase 4, step 3): a Cloud Run job made empty, with its runtime account
(the request's secrets, nothing else), and with a schedule a Cloud Scheduler job running it as a second account that may
run this job only; grants for running and deploying; and nothing left behind, the schedule going first."""
import pytest
from gcp_fakes import PROJECT, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import InfrastructureError
from cloudinfra.providers.gcp.run import JOB_STAND_IN, runtime_account

DELIVERY = "serviceAccount:appdelivery-dev@example.com"
DATA = "group:data@example.com"
NIGHTLY = {"cron": "0 2 * * *", "time_zone": "Europe/Berlin"}


def provision(tmp_path, job, access=None, secret=None):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    body = {"region": "us-central1", "name": "shop", "job": job, "access": access or []}
    if secret:
        body["secret"] = secret
    return orchestrator, fake, who, orchestrator.provision(InfrastructureRequest.model_validate(body), who, wait_seconds=45)


def test_a_job_is_made_empty_with_its_limits_and_its_own_account(tmp_path):
    _, fake, _, response = provision(tmp_path, {"cpu": 2, "memory_mb": 2048, "tasks": 10, "parallelism": 5, "max_retries": 1, "task_timeout_seconds": 3600})
    assert response.error is None, response.error
    [job] = response.resources.job
    made = fake.run_jobs["shop-job"]
    email = f"{runtime_account(response.request_id, 'job')}@{PROJECT}.iam.gserviceaccount.com"
    task = made["template"]["template"]
    assert job.runtime_service_account == email and task["serviceAccount"] == email and task["containers"][0]["image"] == JOB_STAND_IN
    assert task["containers"][0]["resources"]["limits"] == {"cpu": "2", "memory": "2048Mi"} and task["maxRetries"] == 1 and task["timeout"] == "3600s"
    assert made["template"]["taskCount"] == 10 and made["template"]["parallelism"] == 5
    assert job.status == "READY" and job.schedule is None and job.scheduler is None and not fake.schedules


def test_a_schedule_runs_the_job_as_an_account_that_may_run_it_only(tmp_path):
    _, fake, _, response = provision(tmp_path, {"schedule": NIGHTLY})
    [job] = response.resources.job
    [schedule] = fake.schedules.values()
    runner = f"{runtime_account(response.request_id, 'job', 'sch')}@{PROJECT}.iam.gserviceaccount.com"
    assert schedule["schedule"] == "0 2 * * *" and schedule["timeZone"] == "Europe/Berlin"
    assert schedule["httpTarget"]["uri"] == f"https://run.googleapis.com/v2/{job.provider_id}:run" and schedule["httpTarget"]["httpMethod"] == "POST"
    assert schedule["httpTarget"]["oauthToken"]["serviceAccountEmail"] == runner and job.scheduler_service_account == runner
    assert fake.members(("job", "shop-job"), "roles/run.invoker") == [f"serviceAccount:{runner}"], "on this job, not on the project"
    assert job.schedule == NIGHTLY and job.scheduler == schedule["name"]


def test_the_runtime_account_reads_the_requests_secrets(tmp_path):
    _, fake, _, response = provision(tmp_path, {}, secret=[{"name": "api-key", "generate_value": True}])
    [job], [secret] = response.resources.job, response.resources.secret
    assert f"serviceAccount:{job.runtime_service_account}" in fake.members(("secret", secret.resource_name.rsplit("/", 1)[-1]), "roles/secretmanager.secretAccessor")


def test_use_runs_the_job_and_admin_also_deploys_it(tmp_path):
    _, fake, _, response = provision(tmp_path, {"schedule": NIGHTLY}, access=[{"member": DELIVERY, "level": "admin"}, {"member": DATA, "level": "use"}])
    [job] = response.resources.job
    assert set(fake.members(("job", "shop-job"), "roles/run.invoker")) == {DELIVERY, DATA, f"serviceAccount:{job.scheduler_service_account}"}
    assert fake.members(("job", "shop-job"), "roles/run.developer") == [DELIVERY]
    assert fake.members(("account", job.runtime_service_account), "roles/iam.serviceAccountUser") == [DELIVERY]


def test_destroy_takes_the_schedule_away_first_then_the_job_and_its_accounts(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"schedule": NIGHTLY}, secret=[{"name": "api-key", "generate_value": True}])
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.schedules and not fake.run_jobs and not fake.service_accounts


def test_a_schedule_google_refused_leaves_nothing_behind(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.fail_on.append(("POST", "cloudscheduler"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "job": {"schedule": NIGHTLY}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.run_jobs and not fake.service_accounts and not fake.schedules


def test_a_job_google_refused_leaves_no_account_behind(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.fail_on.append(("POST", "run.googleapis.com"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "job": {}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.service_accounts and not fake.run_jobs


@pytest.mark.parametrize("job, said", [({"cpu": 3}, "1, 2, 4 or 8"), ({"tasks": 2, "parallelism": 3}, "above tasks"),
                                       ({"schedule": {"cron": "every night"}}, "cron"), ({"schedule": {"cron": "0 2 * * *", "time_zone": "Berlin time"}}, "time_zone"),
                                       ([{"name": "a"}, {}], "every job of a list needs a name")])
def test_a_job_block_is_checked(job, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "job": job})
    assert said in str(caught.value)


def test_cpu_and_memory_the_provider_does_not_pair_are_refused(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "job": {"cpu": 8, "memory_mb": 1024}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "job job: 8 CPU takes" in str(caught.value.to_dict()) and not fake.run_jobs


def test_the_connect_block_says_how_to_run_it_and_when_it_runs(tmp_path):
    _, _, _, response = provision(tmp_path, {"schedule": NIGHTLY}, access=[{"member": DATA, "level": "use"}])
    connect = response.resources.job[0].connect
    assert connect.commands["run"].startswith("gcloud run jobs execute shop-job") and "0 2 * * *" in connect.notes[0]
