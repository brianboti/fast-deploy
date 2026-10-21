"""Phase 26 and the rest of Phase 14 (docs/ROADMAP.md 26.1 to 26.4, 14.8 to 14.10). No test touches GitHub or a cloud."""
import base64
import datetime
import io
import json
import tarfile
import types
from pathlib import Path

import httpx
import pytest
from conftest import TARGET, FakeGitHub
from test_onboarding import GOOD_SPEC, PLATFORM, make
from test_phase23 import NOW, FakeObserver, config

from labdeploy import chat, datafiles
from labdeploy.attest import Attester, payload, public_key_id
from labdeploy.cloud import Observer
from labdeploy.config import Config as Configuration
from labdeploy.config import LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.cost import MIN_SIZING_REQUESTS, recommend, summary_lines
from labdeploy.models import AppSpec, spec_hash


def spec(**overrides):
    return AppSpec.model_validate({"name": "demo", "reviewed": True, **overrides})


def block(text, header):
    """One Terraform block, from its header to the closing brace at the start of a line."""
    found = text[text.index(header):]
    return found[:found.index("\n}\n")]


# ---- 26.1: no sizing cut on fewer than 50 user requests
MEASURED = {"window_days": 14, "requests": 24_000, "vcpu_seconds": 9_000, "gib_seconds": 4_500, "billable_instance_seconds": 9_000,
            "cpu_utilization_p95": 0.1, "memory_utilization_p95": 0.2, "max_concurrency_p95": 70, "max_active_instances": 1}


def test_a_window_of_uptime_checks_proposes_no_cut():
    assert MIN_SIZING_REQUESTS == 50
    big = spec(resources={"cpu": 2, "memory_mib": 1024}, concurrency=80)
    busy = recommend(big, {**MEASURED, "user_requests": 400})
    assert {item["setting"] for item in busy["recommendations"]} >= {"resources.memory_mib", "resources.cpu", "concurrency"}
    quiet = recommend(big, {**MEASURED, "user_requests": 12})
    assert [item["setting"] for item in quiet["recommendations"]] == [None]
    assert "only 12 user requests in 14 days, under the 50 a sizing proposal needs" in quiet["recommendations"][0]["reason"]
    assert quiet["observed"]["user_requests"] == 12
    # Without the objective's counter, every request counts: the uptime check's included, so the floor rarely applies.
    assert {item["setting"] for item in recommend(big, MEASURED)["recommendations"]} >= {"resources.cpu"}


def test_more_headroom_is_still_proposed_below_the_floor_and_the_report_says_whose_requests():
    tight = recommend(spec(resources={"cpu": 1, "memory_mib": 512}), {**MEASURED, "memory_utilization_p95": 0.92, "user_requests": 3})
    assert tight["recommendations"][0]["setting"] == "resources.memory_mib" and tight["recommendations"][0]["recommended"] == 768
    report = {"service": "demo", "window_days": 14, "estimate": {"compute_ceiling_usd_month": 1.0, "idle_floor_usd_month": 0.0},
              "rightsizing": recommend(spec(), {**MEASURED, "user_requests": 12})}
    assert any("(24000 requests, 12 of them from users; the rest were the uptime check and health probes)" in line for line in summary_lines(report))


# ---- 26.2: alerts for a declared topic or database
def test_a_declared_topic_and_database_get_their_alerts_under_the_runtime_switch():
    alerts = Path("infra/gcp/modules/service/needs_alerts.tf").read_text()
    topic = block(alerts, 'resource "google_monitoring_alert_policy" "need_topic"')
    assert "for_each     = var.runtime_alerts ? toset(var.needs.topics) : toset([])" in topic
    assert "pubsub.googleapis.com/subscription/oldest_unacked_message_age" in topic and "threshold_value = 3600" in topic
    assert 'google_pubsub_subscription.need[each.key].name' in topic and 'severity              = "WARNING"' in topic
    database = block(alerts, 'resource "google_monitoring_alert_policy" "need_database"')
    assert 'count        = var.runtime_alerts && var.needs.database == "postgres" ? 1 : 0' in database
    assert "cloudsql.googleapis.com/database/disk/utilization" in database and "threshold_value = 0.9" in database
    assert "cloudsql.googleapis.com/database/postgresql/num_backends" in database and "floor(local.database_connection_limit * 0.8)" in database
    assert 'database_connection_limit = local.database_tier == "sla" ? 100 : 25' in alerts
    # Google accepts a rate limit on log-based alerts only: the failed-backup policy, which reads the audit log.
    assert "notification_rate_limit" not in topic + database
    backup = block(alerts, 'resource "google_monitoring_alert_policy" "need_database_backup"')
    assert "cloudaudit.googleapis.com%2Fsystem_event" in backup and 'protoPayload.methodName=\\"cloudsql.instances.automatedBackup\\"' in backup
    assert 'protoPayload.metadata.windowStatus=(\\"STATUS_FAILED\\" OR \\"STATUS_ATTEMPT_FAILED\\")' in backup and "notification_rate_limit" in backup


# ---- 26.4: the domain's own uptime check
def test_a_custom_domain_gets_its_own_check_with_its_certificate_validated():
    domain = Path("infra/gcp/modules/service/domain.tf").read_text()
    check = block(domain, 'resource "google_monitoring_uptime_check_config" "domain"')
    assert "count            = local.domain_enabled && var.runtime_alerts ? 1 : 0" in check
    assert "validate_ssl   = true" in check and 'status_class = "STATUS_CLASS_ANY"' in check
    assert 'type   = "uptime_url"' in check and "host = var.domain" in check
    alert = block(domain, 'resource "google_monitoring_alert_policy" "domain"')
    assert "uptime_check/check_passed" in alert and "uptime_check/time_until_ssl_cert_expires" in alert
    assert "threshold_value = 14" in alert and "A custom domain" in alert


# ---- 26.3: the images that serve, read on Mondays and pulled so Google keeps analysing them



IMAGE = "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "d" * 64


def observer(handler):
    cfg = types.SimpleNamespace(observer=types.SimpleNamespace(service_account="lab-deploy-observer@test-project.iam.gserviceaccount.com", cache_seconds=300))
    found = Observer(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    found.token = lambda: "token"
    return found


def test_the_serving_images_are_read_from_the_revisions_with_traffic():
    def handler(request):
        if request.url.path.endswith("/services/demo"):
            return httpx.Response(200, json={"trafficStatuses": [{"revision": "demo-r1", "percent": 100}, {"revision": "demo-r0", "percent": 0}]})
        assert request.url.path.endswith("/revisions/demo-r1")
        return httpx.Response(200, json={"containers": [{"image": IMAGE}, {"image": "nginx:1.29"},
                                                        {"image": "us-central1-docker.pkg.dev/other-project/x/x@sha256:" + "e" * 64}]})
    assert observer(handler).serving_images(types.SimpleNamespace(**TARGET)) == [IMAGE]


def test_an_images_findings_count_fixable_critical_and_high_and_say_whether_google_still_analyses_it():
    def vulnerability(severity, fixed):
        return {"vulnerability": {"effectiveSeverity": severity, "fixAvailable": fixed}}

    def handler(request):
        assert request.url.path == "/v1/projects/test-project/occurrences" and f'resourceUrl="https://{IMAGE}"' in request.url.params["filter"]
        if 'kind="DISCOVERY"' in request.url.params["filter"]:
            return httpx.Response(200, json={"occurrences": [{"discovery": {"continuousAnalysis": "INACTIVE", "analysisStatus": "FINISHED_SUCCESS",
                                                                            "lastScanTime": "2026-09-01T00:00:00Z"}}]})
        if request.url.params.get("pageToken") is None:
            return httpx.Response(200, json={"occurrences": [vulnerability("CRITICAL", True), vulnerability("HIGH", False)], "nextPageToken": "p2"})
        return httpx.Response(200, json={"occurrences": [vulnerability("HIGH", True), vulnerability("LOW", True)]})
    found = observer(handler).image_findings("test-project", IMAGE)
    assert found == {"image": IMAGE, "findings": 4, "critical": 1, "high": 2, "critical_fixable": 1, "high_fixable": 1, "malicious": 0,
                     "analysis": "INACTIVE", "status": "FINISHED_SUCCESS", "last_scan": "2026-09-01T00:00:00Z"}


def test_the_refresh_pulls_the_manifest_and_every_layer_and_follows_the_storage_redirect_without_credentials():
    seen = []
    layer = b"x" * 3000

    def handler(request):
        seen.append((request.url.host, request.url.path, request.headers.get("authorization")))
        if "/manifests/" in request.url.path:
            if request.url.path.endswith("d" * 64):
                return httpx.Response(200, json={"manifests": [{"digest": "sha256:" + "a" * 64, "platform": {"architecture": "amd64"}}]})
            return httpx.Response(200, json={"config": {"digest": "sha256:" + "c" * 64, "size": 10},
                                             "layers": [{"digest": "sha256:" + "f" * 64, "size": len(layer)}]})
        if request.url.host == "storage.example":
            return httpx.Response(200, content=layer)
        if request.url.path.endswith("c" * 64):
            return httpx.Response(200, content=b"0123456789")
        return httpx.Response(307, headers={"location": "https://storage.example/blob?sig=1"})
    pulled = observer(handler).refresh_analysis(IMAGE)
    assert pulled == 10 + len(layer)
    assert seen[0][1] == "/v2/test-project/demo/demo/manifests/sha256:" + "d" * 64 and seen[0][2] == "Basic " + base64.b64encode(b"oauth2accesstoken:token").decode()
    assert seen[1][1].endswith("a" * 64)  # the amd64 manifest of the index
    assert ("storage.example", "/blob", None) in seen  # the redirect gets no credentials


class ImageObserver(FakeObserver):
    def __init__(self, counts):
        super().__init__()
        self.counts, self.pulled = counts, []

    def serving_images(self, target):
        return [IMAGE]

    def image_findings(self, project_id, image):
        return {"image": image, "findings": 9, "critical": 2, "high": 5, "malicious": 0, "analysis": "ACTIVE", "status": "FINISHED_SUCCESS",
                "last_scan": "2026-10-25T00:00:00Z", **self.counts}

    def refresh_analysis(self, image):
        self.pulled.append(image)
        return 1234


def test_monday_names_the_fixable_findings_of_what_serves_and_what_is_new_since_last_monday(tmp_path):
    cfg = config(tmp_path)
    watcher = ImageObserver({"critical_fixable": 2, "high_fixable": 4})
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), watcher)
    c.watchdog_problems = lambda now=None: []
    last_monday = datetime.datetime.fromtimestamp(NOW - 7 * 86_400, datetime.UTC).date().isoformat()
    c.store.save_digest(last_monday, {"serving_images": {"images": [{"image": IMAGE, "critical_fixable": 1, "high_fixable": 4}]}})
    record = c.build_digest(NOW)
    item = record["serving_images"]["images"][0]
    assert (item["application"], item["new_critical_fixable"], item["new_high_fixable"], item["pulled_bytes"]) == ("demo", 1, 0, 1234)
    assert watcher.pulled == [IMAGE] and record["attention"]
    text = chat.digest(record)
    assert "`demo`: its serving image has 2 critical and 4 high findings with a fix, 1 critical new since last Monday" in text
    assert "A serving image's findings" in text
    quiet = ImageObserver({"critical_fixable": 0, "high_fixable": 0})
    calm = Controller(cfg, FakeGitHub(), Store(str(tmp_path / "db2")), quiet)
    calm.watchdog_problems = lambda now=None: []
    assert "Serving images: 1 image read, none with a critical or high finding that has a fix" in chat.digest(calm.build_digest(NOW))
    tuesday = calm.build_digest(NOW + 86_400)
    assert "serving_images" not in tuesday  # Mondays only


# ---- 14.8: Binary Authorization

ATTESTOR = "projects/test-project/attestors/lab-deploy-releases"
KEY_VERSION = "projects/test-project/locations/us-central1/keyRings/lab-deploy/cryptoKeys/release-attestor/cryptoKeyVersions/1"


def test_the_attestation_signs_the_digest_checks_it_with_the_attestor_and_stores_it_once():
    statement = json.loads(payload(IMAGE))
    assert statement == {"critical": {"identity": {"docker-reference": IMAGE.split("@")[0]}, "image": {"docker-manifest-digest": "sha256:" + "d" * 64},
                                      "type": "Google cloud binauthz container signature"}}
    assert public_key_id(KEY_VERSION) == "//cloudkms.googleapis.com/v1/" + KEY_VERSION
    calls, stored = [], []

    def handler(request):
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.path == f"/v1/{ATTESTOR}":
            return httpx.Response(200, json={"userOwnedGrafeasNote": {"noteReference": "projects/test-project/notes/lab-deploy-releases"}})
        if request.url.path.endswith("/notes/lab-deploy-releases/occurrences"):
            return httpx.Response(200, json={"occurrences": stored[:1]})
        if request.url.path.endswith(":asymmetricSign"):
            assert json.loads(request.content)["digest"]["sha256"]
            return httpx.Response(200, json={"signature": "c2lnbmF0dXJl"})
        if request.url.path.endswith(":validateAttestationOccurrence"):
            body = json.loads(request.content)
            assert body["attestation"]["signatures"][0]["publicKeyId"] == public_key_id(KEY_VERSION) and body["occurrenceResourceUri"] == "https://" + IMAGE
            return httpx.Response(200, json={"result": "VERIFIED"})
        assert request.url.path == "/v1/projects/test-project/occurrences"
        stored.append(json.loads(request.content))
        return httpx.Response(200, json={})
    attester = Attester("token", httpx.Client(transport=httpx.MockTransport(handler)))
    assert attester.attest(IMAGE, ATTESTOR, KEY_VERSION) == "attested"
    assert [call[1] for call in calls] == ["binaryauthorization.googleapis.com", "containeranalysis.googleapis.com", "cloudkms.googleapis.com",
                                           "binaryauthorization.googleapis.com", "containeranalysis.googleapis.com"]
    assert stored[0]["noteName"] == "projects/test-project/notes/lab-deploy-releases" and stored[0]["resourceUri"] == "https://" + IMAGE
    assert attester.attest(IMAGE, ATTESTOR, KEY_VERSION) == "already" and len(stored) == 1


def test_an_attestation_the_attestor_would_refuse_is_never_stored():
    def handler(request):
        if request.url.path.endswith(":validateAttestationOccurrence"):
            return httpx.Response(200, json={"result": "ATTESTATION_NOT_VERIFIABLE", "denialReason": "no key with that id"})
        if request.url.path.endswith(":asymmetricSign"):
            return httpx.Response(200, json={"signature": "c2ln"})
        if "/attestors/" in request.url.path:
            return httpx.Response(200, json={"userOwnedGrafeasNote": {"noteReference": "projects/test-project/notes/n"}})
        if request.method == "POST":
            raise AssertionError("stored although refused")
        return httpx.Response(200, json={})
    with pytest.raises(RuntimeError, match="no key with that id"):
        Attester("token", httpx.Client(transport=httpx.MockTransport(handler))).attest(IMAGE, ATTESTOR, KEY_VERSION)


def test_the_foundation_attests_in_dry_run_first_and_every_service_opts_in():
    foundation = Path("infra/gcp/modules/project/binauthz.tf").read_text()
    assert 'default     = "dryrun"' in Path("infra/gcp/modules/project/variables.tf").read_text()
    assert '"DRYRUN_AUDIT_LOG_ONLY"' in foundation and '"ENFORCED_BLOCK_AND_AUDIT_LOG"' in foundation
    assert 'name_pattern = "us-docker.pkg.dev/cloudrun/container/hello*"' in foundation  # the reference image of a new service
    assert "gcp-sa-binaryauthorization.iam.gserviceaccount.com" in foundation and "prevent_destroy = true" in foundation
    service = Path("infra/gcp/modules/service/main.tf").read_text()
    assert 'dynamic "binary_authorization"' in service and "use_default = true" in service and 'try(var.binary_authorization.mode, "off") != "off"' in service
    assert 'evaluation_mode         = var.binary_authorization == "off" ? "ALWAYS_ALLOW" : "REQUIRE_ATTESTATION"' in foundation
    assert "count " not in foundation  # the key, the note and the attestor stay whatever the mode
    checks = Path("infra/gcp/modules/service/release_checks.tf").read_text()
    assert 'role          = "roles/cloudkms.signerVerifier"' in checks and "var.binary_authorization.attester_role" in checks
    outputs = Path("infra/gcp/modules/service/outputs.tf").read_text()
    assert "BINAUTHZ_ATTESTOR    = var.binary_authorization.attestor" in outputs
    from labdeploy.infra import app_root
    root = app_root("demo", project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/demo", github_repository_id="1",
                    github_owner_id="2", bootstrap_image="x@sha256:" + "a" * 64)["main.tf"]
    assert "try(data.terraform_remote_state.project.outputs.binary_authorization, null)" in root
    for path in ("templates/deploy-gcp.yml", ".github/workflows/deploy.yml", ".github/workflows/promote.yml"):
        text = Path(path).read_text()
        assert "python -m labdeploy.attest" in text and "BINAUTHZ_ATTESTOR: ${{ vars.BINAUTHZ_ATTESTOR }}" in text, path
    template = Path("templates/deploy-gcp.yml").read_text()
    assert template.index("Verify build provenance with Cloud KMS") < template.index("Attest the image for Binary Authorization") < template.index("Deploy, probe, promote")
    assert '[ "$BINAUTHZ_MODE" = "enforced" ] && exit 1' in template  # in dry run a failed attestation warns


def test_the_release_keeps_the_service_opted_in(monkeypatch):
    from labdeploy.models import AppSpec as Spec
    from labdeploy.release import CloudRun
    calls = []
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=lambda args: calls.append(args) or "")
    spec_ = Spec.model_validate({"name": "demo", "reviewed": True})
    for mode in (None, "off", "dryrun", "enforced"):
        if mode is None:
            monkeypatch.delenv("BINAUTHZ_MODE", raising=False)
        else:
            monkeypatch.setenv("BINAUTHZ_MODE", mode)
        adapter.deploy("us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "b" * 64, spec_, "r1")
    deploys = [call for call in calls if call[:3] == ["gcloud", "run", "deploy"]]
    assert ["--binary-authorization=default" in deploy for deploy in deploys] == [False, False, True, True]


# ---- 14.10: personal data in the image's data files




def docker_save(path, *layers):
    """A `docker save` tarball: manifest.json and one uncompressed tar per layer, each {name: bytes or None for a whiteout}."""
    with tarfile.open(path, "w") as outer:
        names = []
        for index, files in enumerate(layers):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as inner:
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data or b"")
                    inner.addfile(info, io.BytesIO(data or b""))
            name = f"blobs/sha256/layer{index}"
            info = tarfile.TarInfo(name)
            info.size = buffer.tell()
            buffer.seek(0)
            outer.addfile(info, buffer)
            names.append(name)
        manifest = json.dumps([{"Layers": names}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        outer.addfile(info, io.BytesIO(manifest))


def test_the_data_files_of_the_image_are_found_after_whiteouts_and_outside_system_and_package_folders(tmp_path):
    big = b"name,email\n" + b"a,b@example.org\n" * 6000
    image = tmp_path / "image.tar"
    docker_save(image,
                {"usr/share/zoneinfo/data.csv": big, "app/data/people.csv": big, "app/data/old.csv": big, "app/node_modules/x/data.json": big,
                 "app/package-lock.json": big, "app/small.json": b"{}", "./app/model.parquet": big},
                {"app/data/.wh.old.csv": None})
    files = datafiles.image_files(image)
    assert "app/data/old.csv" not in files and "app/model.parquet" in files
    chosen = datafiles.candidates({path: size for path, (_, _, size) in files.items()})
    assert list(chosen) == ["app/data/people.csv", "app/model.parquet"]  # text first, then binary, which is only listed
    samples = datafiles.samples(image, ["app/data/people.csv"])
    assert samples["app/data/people.csv"] == big[:datafiles.SAMPLE]


def test_likely_identifiers_stop_code_nobody_has_reviewed_and_are_reported_for_the_lab_s_own():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v2/projects/test-project/locations/us-central1/content:inspect"
        assert body["inspectConfig"]["includeQuote"] is False and body["inspectConfig"]["minLikelihood"] == "LIKELY"
        assert {"name": "CREDIT_CARD_NUMBER"} in body["inspectConfig"]["infoTypes"] and body["item"]["byteItem"]["type"] == "CSV"
        return httpx.Response(200, json={"result": {"findings": [{"infoType": {"name": "CREDIT_CARD_NUMBER"}, "likelihood": "VERY_LIKELY"},
                                                                 {"infoType": {"name": "EMAIL_ADDRESS"}, "likelihood": "LIKELY"},
                                                                 {"infoType": {"name": "PHONE_NUMBER"}, "likelihood": "POSSIBLE"}]}})
    counts = datafiles.inspect("test-project", "us-central1", "app/data/people.csv", b"x", "token", httpx.Client(transport=httpx.MockTransport(handler)))
    assert counts == {"CREDIT_CARD_NUMBER": 1, "EMAIL_ADDRESS": 1}
    blocking, lines = datafiles.decide({"app/data/people.csv": counts}, "unknown")
    assert blocking and "credit card number 1, email address 1" in lines[0]
    assert datafiles.decide({"app/data/people.csv": counts}, "reviewed")[0] is False
    assert datafiles.decide({"app/data/people.csv": {"EMAIL_ADDRESS": 3}}, "unknown")[0] is False  # emails alone are reported


def test_the_release_inspects_before_it_pushes_and_the_plan_names_the_files(tmp_path, monkeypatch):
    template = Path("templates/deploy-gcp.yml").read_text()
    assert template.index("name: Inspect data files for personal data") < template.index("name: Push image and resolve immutable digest")
    assert "python -m labdeploy.datafiles bundle/image.tar" in template and "GOOGLE_ACCESS_TOKEN=\"$TOKEN\"" in template
    assert 'role    = "roles/dlp.user"' in Path("infra/gcp/modules/service/release_checks.tf").read_text()
    from labdeploy.infra import ALLOWED_ROLES
    assert "roles/dlp.user" in ALLOWED_ROLES
    big = b"id,ssn\n" + b"1,123-45-6789\n" * 9000
    image = tmp_path / "image.tar"
    docker_save(image, {"app/people.csv": big})
    monkeypatch.setattr(datafiles, "inspect", lambda *args, **kwargs: {"US_SOCIAL_SECURITY_NUMBER": 40})
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "report.json"
    assert datafiles.main([str(image), "--project", "test-project", "--region", "us-central1", "--trust", "unknown", "--json", str(out)]) == 1
    assert json.loads(out.read_text())["inspected"] == {"app/people.csv": {"US_SOCIAL_SECURITY_NUMBER": 40}}
    assert datafiles.main([str(image), "--project", "test-project", "--region", "us-central1", "--trust", "reviewed"]) == 0

    def refused(status):
        def answer(*args, **kwargs):
            raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://dlp.googleapis.com"), response=httpx.Response(status))
        return answer
    monkeypatch.setattr(datafiles, "inspect", refused(403))  # not set up yet: the operator's state, a warning
    assert datafiles.main([str(image), "--project", "test-project", "--region", "us-central1", "--trust", "unknown"]) == 0
    monkeypatch.setattr(datafiles, "inspect", refused(503))  # an inspection that did not finish stops code nobody has reviewed
    assert datafiles.main([str(image), "--project", "test-project", "--region", "us-central1", "--trust", "unknown"]) == 1
    from labdeploy.reader import read
    plan = read(["app.py", "data/people.csv", "requirements.txt"], {"requirements.txt": "flask\n", "app.py": "import flask\n"},
                sizes={"data/people.csv": 300 * 1024})
    assert any("1 data file of 64 KiB or more may go into the image (data/people.csv (300 KiB))" in note for note in plan["notes"])


# ---- 14.9: a project of its own



def test_the_contract_may_ask_for_a_project_of_its_own_and_old_hashes_hold():
    plain = AppSpec.model_validate({"name": "demo", "reviewed": True})
    own = AppSpec.model_validate({"name": "demo", "reviewed": True, "project": "own"})
    assert plain.project is None and own.project == "own"
    assert spec_hash(plain) == spec_hash(AppSpec.model_validate({"name": "demo", "reviewed": True}))
    assert spec_hash(plain) != spec_hash(own)
    with pytest.raises(ValueError):
        AppSpec.model_validate({"name": "demo", "reviewed": True, "project": "elsewhere"})


def test_the_settings_never_name_the_shared_project_as_one_of_its_own(tmp_path):
    base = {"project_id": "test-project", "project_number": "123456789"}
    with pytest.raises(ValueError, match="names the shared staging project"):
        Configuration.model_validate({"applications": {}, "principals": [], "onboarding": {**base, "own_projects": {"x": {"project_id": "test-project", "project_number": "223456789"}}}})
    with pytest.raises(ValueError, match="no project twice"):
        Configuration.model_validate({"applications": {}, "principals": [], "onboarding": {**base, "own_projects": {
            "x": {"project_id": "own-project-1", "project_number": "223456789"}, "y": {"project_id": "own-project-1", "project_number": "223456789"}}}})


def test_onboarding_puts_an_own_project_contract_in_its_project_and_waits_for_one(tmp_path):
    c, brian, gh = make(tmp_path)
    gh.repos["lab/newapp"]["files"]["platform.yaml"] = GOOD_OWN
    blocked = c.onboard(brian, "lab/newapp")
    assert blocked["state"] == "blocked" and "onboarding.own_projects.newapp" in blocked["error"] and "newapp" not in c.config.applications
    raw = c.config.current.model_dump()
    raw["onboarding"]["own_projects"] = {"newapp": {"project_id": "newapp-own-1234", "project_number": "987654321"}}
    c2 = Controller(LiveConfig(Configuration.model_validate(raw)), gh, Store(c.config.database))
    result = c2.onboard(brian, "lab/newapp")
    target = c2.config.applications["newapp"].target
    assert result["state"] == "planning" and target.project_id == "newapp-own-1234"
    assert target.wif_provider == "projects/987654321/locations/global/workloadIdentityPools/newapp-gh/providers/github"
    root = gh.prs[(PLATFORM, max(number for _, number in gh.prs))]["files"]
    assert 'bucket = "newapp-own-1234-tfstate"' in root["infra/gcp/apps/newapp/backend.tf"].replace("  ", " ").replace("  ", " ")
    assert 'project = "newapp-own-1234"' in root["infra/gcp/apps/newapp/main.tf"]


def test_an_own_project_contract_is_never_rendered_into_the_shared_project(tmp_path):
    c, brian, gh = make(tmp_path)
    c.onboard(brian, "lab/newapp")  # registered in the shared project
    gh.repos["lab/newapp"]["files"]["platform.yaml"] = GOOD_OWN
    with pytest.raises(PermissionError, match="asks for a project of its own"):
        c.render_infrastructure(brian, "newapp")


def test_the_pipeline_signs_in_to_a_project_of_its_own_with_its_identities():
    for path, kind in ((".github/workflows/infra-plan.yml", "plan"), (".github/workflows/infra-apply.yml", "apply"), (".github/workflows/infra-drift.yml", "plan")):
        text = Path(path).read_text()
        assert f"fromJSON(vars.INFRA_OWN_PROJECTS || '{{}}')[matrix.app].{kind}_provider || vars.INFRA_{kind.upper()}_WIF_PROVIDER" in text, path
    script = Path("scripts/apply-foundation.sh").read_text()
    assert 'VARS=own-$PROJECT.tfvars' in script and "INFRA_OWN_PROJECTS entry" in script and '[[ "$MODE" == "own" ]] && ENVIRONMENT=staging' in script



GOOD_OWN = GOOD_SPEC.replace("reviewed: true\n", "reviewed: true\nproject: own\n")
