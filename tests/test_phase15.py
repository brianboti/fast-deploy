"""Phase 15: the controller off the laptop (docs/ROADMAP.md D36): the GitHub App, the machine, durability, the move."""
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from conftest import TARGET, FakeGitHub
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from labdeploy.cloud import GoogleToken, secret_reader
from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.github import AppInstallations, GitHub
from labdeploy.machine import APP_KEY_SECRET, export, export_database, machine_config

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
PUBLIC = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
STARTUP = Path("infra/gcp/modules/project/controller-startup.sh")
CONTROLLER_TF = Path("infra/gcp/modules/project/controller.tf")


def base_config(tmp_path, **extra):
    data = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "brian", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["*"], "can_deploy": True}]}
    data.update(extra)
    return data


def signed_by_the_app(header):
    claims = jwt.decode(header.removeprefix("Bearer "), PUBLIC, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    return claims["iss"] == "123456"


def app_github(installed=("lab/demo", "brianboti/fast-deploy"), expires="2026-10-17T00:00:00Z", refuse=0):
    """GitHub as the App sees it: which repositories have the installation, tokens per repository, and API calls."""
    calls, state = [], {"refuse": refuse, "minted": 0}

    def handler(request):
        auth = request.headers.get("authorization", "")
        calls.append((request.method, request.url.path, auth.removeprefix("Bearer ")))
        if request.url.path.endswith("/installation"):
            if state["refuse"]:
                state["refuse"] -= 1
                return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
            assert signed_by_the_app(auth)
            repository = request.url.path[len("/repos/"):-len("/installation")]
            return httpx.Response(200, json={"id": 42}) if repository in installed else httpx.Response(404, json={"message": "Not Found"})
        if request.url.path == "/app/installations/42/access_tokens":
            assert signed_by_the_app(auth)
            state["minted"] += 1
            name = json.loads(request.content)["repositories"][0]
            return httpx.Response(201, json={"token": f"ghs_{name}_{state['minted']}", "expires_at": expires})
        return httpx.Response(200, json={"token_seen": auth.removeprefix("Bearer ")}, headers={})
    return handler, calls, state


def client_for(handler):
    return httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))


# ---- 15.1 the GitHub App: an installation token per repository
def test_the_app_mints_a_token_per_repository_and_keeps_it_until_shortly_before_it_ends(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    handler, calls, state = app_github()
    now = [1_792_108_800.0]  # 2026-10-16T00:00:00Z; the tokens end at 2026-10-17T00:00:00Z
    keys = []
    app = AppInstallations("123456", lambda refresh=False: keys.append(refresh) or PRIVATE, client_for(handler), home_repository="brianboti/fast-deploy",
                           clock=lambda: now[0])
    gh = GitHub(client_for(handler), app)
    assert gh.request("GET", "/repos/lab/demo/commits/main").json()["token_seen"] == "ghs_demo_1"
    assert gh.request("POST", "/repos/lab/demo/actions/workflows/deploy-gcp.yml/dispatches").json()["token_seen"] == "ghs_demo_1"
    assert state["minted"] == 1 and [c[1] for c in calls].count("/repos/lab/demo/installation") == 1  # both remembered
    assert gh.request("GET", "/rate_limit").json()["token_seen"] == "ghs_fast-deploy_2"  # no repository: the home one's token
    now[0] += 86_400 - 299  # within five minutes of the end: a new token
    assert gh.request("GET", "/repos/lab/demo/pulls").json()["token_seen"] == "ghs_demo_3"
    assert keys and not any(keys)  # the key was only read, never asked for again


def test_a_repository_without_the_app_is_read_with_the_home_token_and_refused_for_anything_else(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    handler, calls, _ = app_github(installed=("brianboti/fast-deploy",))
    gh = GitHub(client_for(handler), AppInstallations("123456", lambda refresh=False: PRIVATE, client_for(handler), home_repository="brianboti/fast-deploy"))
    assert gh.request("GET", "/repos/someone/public-repo/contents/README.md").json()["token_seen"] == "ghs_fast-deploy_1"
    with pytest.raises(ValueError, match="not installed on someone/public-repo"):
        gh.request("PUT", "/repos/someone/public-repo/contents/x")
    # asked once, remembered for ten minutes
    assert [c[1] for c in calls].count("/repos/someone/public-repo/installation") == 1


def test_a_refused_signature_reads_the_key_again_once_so_a_rotation_needs_no_restart(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    handler, _, _ = app_github(refuse=1)
    reads = []
    gh = GitHub(client_for(handler), AppInstallations("123456", lambda refresh=False: reads.append(refresh) or PRIVATE, client_for(handler),
                                                      home_repository="brianboti/fast-deploy"))
    assert gh.request("GET", "/repos/lab/demo/commits/main").json()["token_seen"] == "ghs_demo_1"
    assert reads[:2] == [False, True]


def test_a_fine_grained_token_still_wins_so_a_laptop_keeps_working(monkeypatch):
    handler, calls, _ = app_github()
    gh = GitHub(client_for(handler), AppInstallations("123456", lambda refresh=False: PRIVATE, client_for(handler), home_repository="brianboti/fast-deploy"))
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_laptop")
    assert gh.request("GET", "/repos/lab/demo/commits/main").json()["token_seen"] == "github_pat_laptop"
    assert not any(c[1].endswith("/installation") for c in calls)
    monkeypatch.delenv("GITHUB_TOKEN")
    status = gh.credential_status()
    assert status == {"kind": "github_app", "app_id": "123456", "expires_at": None, "days_left": None, "warning": None}


def test_without_any_credential_the_error_says_what_to_set(monkeypatch):
    for name in ("GITHUB_TOKEN", "GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="github_app in the controller configuration"):
        GitHub(client_for(lambda request: httpx.Response(200))).request("GET", "/rate_limit")


def test_the_configuration_builds_the_app_and_the_key_name_is_out_of_every_applications_reach(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    key = tmp_path / "app.pem"
    key.write_text(PRIVATE)
    cfg = Config.model_validate(base_config(tmp_path, platform={"repository": "brianboti/fast-deploy"},
                                            github_app={"app_id": "123456", "private_key_file": str(key)}))
    handler, _, _ = app_github()
    gh = GitHub.from_config(cfg, client_for(handler))
    assert gh.request("GET", "/repos/lab/demo/commits/main").json()["token_seen"] == "ghs_demo_1"
    # Secret Manager names: an uppercase first letter, because app runtimes read "<service>-NAME" (lowercase services).
    good = {"app_id": "Iv23liABCDEFGH", "private_key_secret": f"projects/lab-deploy-staging-f29a/secrets/{APP_KEY_SECRET}"}
    assert Config.model_validate(base_config(tmp_path, github_app=good)).github_app.app_id == "Iv23liABCDEFGH"
    for bad, message in (({**good, "private_key_secret": "projects/p-123456/secrets/fast-deploy-github-app-key"}, "uppercase"),
                         ({**good, "private_key_file": "/k.pem"}, "exactly one"), ({"app_id": "123456"}, "exactly one"),
                         ({**good, "app_id": "my-app"}, "app_id")):
        with pytest.raises(ValueError, match=message):
            Config.model_validate(base_config(tmp_path, github_app=bad))
    service = re.compile(r"^[a-z][a-z0-9-]{2,20}$")  # labdeploy/target.py: what a service may be called
    assert not service.match(APP_KEY_SECRET[:1]) and APP_KEY_SECRET[0].isupper()
    assert f'github_app_key_secret = "{APP_KEY_SECRET}"' in CONTROLLER_TF.read_text()


def test_a_changed_github_app_is_reported_as_needing_a_restart(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base_config(tmp_path)))
    live = LiveConfig(Config.model_validate(base_config(tmp_path)), str(path))
    path.write_text(yaml.safe_dump(base_config(tmp_path, github_app={"app_id": "1", "private_key_file": "/k.pem"})))
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert live.refresh() and "github_app changes need a restart" in live.error


def test_the_app_key_is_read_from_secret_manager_kept_in_memory_and_read_again_on_request():
    reads = []

    def handler(request):
        reads.append((request.url.path, request.headers["authorization"]))
        if "denied" in request.url.path:
            return httpx.Response(403, json={"error": {"message": "Permission denied"}})
        return httpx.Response(200, json={"payload": {"data": base64.b64encode(PRIVATE.encode()).decode()}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    read = secret_reader(f"projects/p/secrets/{APP_KEY_SECRET}", lambda: "google-token", client)
    assert read() == PRIVATE and read() == PRIVATE and len(reads) == 1
    assert reads[0] == (f"/v1/projects/p/secrets/{APP_KEY_SECRET}/versions/latest:access", "Bearer google-token")
    assert read(refresh=True) == PRIVATE and len(reads) == 2
    with pytest.raises(ValueError, match="add the key as a version of that secret"):
        secret_reader("projects/p/secrets/denied", lambda: "t", client)()


def test_the_google_token_is_the_machines_own_and_on_a_laptop_the_operators_sign_in():
    def metadata(request):
        assert request.headers["Metadata-Flavor"] == "Google"
        return httpx.Response(200, json={"access_token": "machine-token", "expires_in": 3599})
    commands = []
    machine = GoogleToken(httpx.Client(transport=httpx.MockTransport(metadata)), runner=lambda args: commands.append(args) or "x")
    assert machine() == "machine-token" and machine() == "machine-token" and commands == []

    def unreachable(request):
        raise httpx.ConnectError("no metadata server here")
    laptop = GoogleToken(httpx.Client(transport=httpx.MockTransport(unreachable)), runner=lambda args: commands.append(args) or "operator-token")
    assert laptop() == "operator-token" and commands == [["gcloud", "auth", "print-access-token"]]  # the operator, not an impersonation


# ---- 15.5 the move: the machine's copy of the configuration and the database
def laptop_config(tmp_path):
    return base_config(tmp_path, backup_bucket="lab-deploy-staging-f29a-lab-deploy-backups", backup_dir=str(tmp_path / "copies"),
                       machine_url="https://deploy.lab.example.edu", notify_webhook_url="https://hooks.example.com/secret-path",
                       onboarding={"project_id": "lab-deploy-staging-f29a", "project_number": "12851477137"},
                       oauth={"public_url": "https://abc.ngrok-free.dev", "refresh_token_days": 14},
                       allowed_hosts=["127.0.0.1:*", "localhost:*", "abc.ngrok-free.dev"])


def test_the_machine_configuration_moves_paths_address_backups_and_the_app_and_keeps_the_rest(tmp_path):
    laptop = laptop_config(tmp_path)
    machine = machine_config(laptop, "deploy.lab.example.edu", "123456")
    assert machine["database"] == "/var/lib/fast-deploy/deployments.db" and machine["log_file"] == "/var/lib/fast-deploy/controller.log"
    assert "backup_dir" not in machine and "machine_url" not in machine and machine["backup_prefix"] == "machine"
    assert machine["litestream_replica"] == "gs://lab-deploy-staging-f29a-lab-deploy-backups/machine/litestream/deployments.db"
    assert machine["oauth"] == {"public_url": "https://deploy.lab.example.edu", "refresh_token_days": 14}
    assert machine["allowed_hosts"] == ["127.0.0.1:*", "localhost:*", "deploy.lab.example.edu"]
    assert machine["github_app"] == {"app_id": "123456", "private_key_secret": f"projects/lab-deploy-staging-f29a/secrets/{APP_KEY_SECRET}"}
    assert machine["principals"] == laptop["principals"] and machine["notify_webhook_url"] == laptop["notify_webhook_url"]
    assert laptop["database"] == str(tmp_path / "db")  # the laptop's own settings are left as they are
    Config.model_validate(machine)
    with pytest.raises(ValueError, match="neither the machine's address"):
        machine_config(laptop, "https://deploy.lab.example.edu", "123456")
    with pytest.raises(ValueError, match="App ID"):
        machine_config(laptop, "deploy.lab.example.edu")
    with pytest.raises(ValueError, match="backup_bucket"):
        machine_config({**laptop, "backup_bucket": None}, "deploy.lab.example.edu", "123456")


def test_without_a_name_claude_reaches_the_machine_at_its_own_public_ipv4_address(tmp_path):
    machine = machine_config(laptop_config(tmp_path), "34.123.45.67", "123456")
    assert machine["oauth"]["public_url"] == "https://34.123.45.67" and machine["allowed_hosts"][-1] == "34.123.45.67"
    Config.model_validate(machine)
    for refused in ("10.9.0.2", "192.168.1.5", "127.0.0.1", "2600:1900::1", "34.123.45"):
        with pytest.raises(ValueError):
            machine_config(laptop_config(tmp_path), refused, "123456")


def test_the_export_writes_a_whole_database_and_a_private_configuration_and_prints_no_setting(tmp_path):
    cfg = Config.model_validate(laptop_config(tmp_path))
    controller = Controller(cfg, FakeGitHub(), Store(cfg.database))
    controller.plan(cfg.principals[0], "demo")
    source = tmp_path / "config.local.yaml"
    source.write_text(yaml.safe_dump(laptop_config(tmp_path)))
    report = export(source, "deploy.lab.example.edu", tmp_path / "import", "123456")
    copy = sqlite3.connect(tmp_path / "import" / "deployments.db")
    assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok" and copy.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    copy.close()
    written = tmp_path / "import" / "config.yaml"
    assert (written.stat().st_mode & 0o777) == 0o600 and (written.parent.stat().st_mode & 0o777) == 0o700
    assert yaml.safe_load(written.read_text())["oauth"]["public_url"] == "https://deploy.lab.example.edu"
    assert report["plans"] == 1 and "secret-path" not in json.dumps(report) and "token_sha256" not in json.dumps(report)
    with pytest.raises(ValueError, match="already exists"):
        export_database(cfg.database, tmp_path / "import" / "deployments.db")


# ---- 15.3 durability: the nightly copy, the live copy, the drill and the watchdog
def test_the_nightly_copy_is_made_with_vacuum_into_and_goes_to_the_hosts_own_folder(tmp_path, monkeypatch):
    cfg = Config.model_validate(base_config(tmp_path, backup_bucket="lab-backups", backup_prefix="machine"))
    controller = Controller(cfg, FakeGitHub(), Store(cfg.database))
    controller.plan(cfg.principals[0], "demo")
    uploads = []
    monkeypatch.setattr(subprocess, "run", lambda args, **options: uploads.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    settings = tmp_path / "config.yaml"
    settings.write_text("x: 1\n")
    result = controller.backup_tick(config_path=str(settings))
    copy = Path(result["backup"])
    assert sqlite3.connect(copy).execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert [args[-1] for args in uploads] == [f"gs://lab-backups/machine/{copy.name}", "gs://lab-backups/machine/config.yaml"]
    laptop = Controller(Config.model_validate(base_config(tmp_path / "l", backup_bucket="lab-backups")), FakeGitHub(), Store(str(tmp_path / "l.db")))
    assert laptop.config.backup_prefix == "controller"


def machine_bucket(files, names, live_database):
    """The machine's folder and its Litestream replica, as gcloud and litestream see them."""
    calls = []

    def run(arguments, **options):
        calls.append(arguments)
        if arguments[0] == "litestream":
            shutil.copyfile(live_database, arguments[arguments.index("-o") + 1])
            return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
        if arguments[:3] == ["gcloud", "storage", "ls"]:
            return subprocess.CompletedProcess(arguments, 0, stdout="".join(f"gs://lab-backups/machine/{name}\n" for name in names), stderr="")
        shutil.copyfile(files[arguments[3].rsplit("/", 1)[1]], arguments[4])
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")
    return run, calls


def test_the_drill_on_the_machine_restores_its_folder_and_the_live_copy(tmp_path):
    from test_phase12 import at

    from labdeploy.drill import lines, restore_drill
    replica = "gs://lab-backups/machine/litestream/deployments.db"
    cfg = Config.model_validate(base_config(tmp_path, backup_bucket="lab-backups", backup_prefix="machine", litestream_replica=replica))
    controller = Controller(cfg, FakeGitHub(), Store(cfg.database))
    controller.plan(cfg.principals[0], "demo")
    controller.store.record_event("tool", name="list_applications", outcome="ok")
    nightly = tmp_path / "nightly.db"
    sqlite3.connect(cfg.database).execute("VACUUM INTO ?", (str(nightly),))
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"applications": {}, "principals": [{"name": "brian", "token_sha256": "0" * 64, "applications": ["*"]}],
                                        "backup_bucket": "lab-backups", "backup_prefix": "machine"}))
    run, calls = machine_bucket({"deployments-20261016-031500.db": nightly, "config.yaml": settings}, ["deployments-20261016-031500.db"], cfg.database)
    report = restore_drill(cfg, controller.store, run=run, now=at("20261016-031500", 2))
    assert report["ok"], report["error"]
    assert report["copy"] == "gs://lab-backups/machine/deployments-20261016-031500.db" and calls[0] == ["gcloud", "storage", "ls", "gs://lab-backups/machine/"]
    assert ["litestream", "restore", "-integrity-check", "quick"] == calls[-1][:4] and calls[-1][-1] == replica
    assert report["replica"]["counts"]["plans"] == 1 and report["replica"]["behind_minutes"] == 0.0 and report["warnings"] == []
    printed = "\n".join(lines(report))
    assert f"live copy {replica} restored with Litestream, integrity ok: 1 plans, 0.0 minutes behind the database" in printed
    assert "live copy restored (1 plans, 0.0 min behind)" in controller.store.newest_event("restore_drill")["detail"]

    def no_litestream(arguments, **options):
        if arguments[0] == "litestream":
            raise FileNotFoundError("litestream")
        return run(arguments, **options)
    assert "scripts/machine.sh drill" in restore_drill(cfg, controller.store, run=no_litestream, now=at("20261016-031500", 2))["error"]


def test_the_watchdog_says_when_the_live_copy_stops_and_where_to_drill(tmp_path):
    replica = "gs://lab-backups/machine/litestream/deployments.db"
    cfg = Config.model_validate(base_config(tmp_path, backup_bucket="lab-backups", backup_prefix="machine", litestream_replica=replica))
    controller = Controller(cfg, FakeGitHub(), Store(cfg.database))
    answer = lambda state: lambda args, **options: subprocess.CompletedProcess(args, 3, stdout=state + "\n", stderr="")  # noqa: E731
    assert controller.replication_problem(run=answer("active")) is None
    assert "Litestream) is inactive" in controller.replication_problem(run=answer("inactive"))
    assert "scripts/machine.sh drill" in controller.restore_drill_reminder()
    laptop = Controller(Config.model_validate(base_config(tmp_path / "l", backup_bucket="lab-backups")), FakeGitHub(), Store(str(tmp_path / "l.db")))
    assert laptop.replication_problem(run=answer("inactive")) is None
    assert "python -m labdeploy.cli restore-drill" in laptop.restore_drill_reminder()


# ---- 15.2 and 15.4 the machine, read as text: what it may do and what it costs
def test_the_machine_is_the_free_shape_with_ssh_only_through_identity_aware_proxy():
    text = CONTROLLER_TF.read_text()
    assert 'machine_type = "e2-micro"' in text and 'image  = "debian-cloud/debian-13"' in text
    assert re.search(r'size\s+= 30\n\s+type\s+= "pd-standard"', text)
    assert "enable_secure_boot          = true" in text and "enable_vtpm                 = true" in text
    assert 'enable-oslogin         = "TRUE"' in text and 'block-project-ssh-keys = "TRUE"' in text and 'serial-port-enable     = "FALSE"' in text
    assert "metadata_startup_script" not in text  # changing that one replaces the machine; the metadata key updates in place
    assert 'startup-script         = file("${path.module}/controller-startup.sh")' in text
    ssh = text[text.index('resource "google_compute_firewall" "controller_ssh"'):]
    assert 'source_ranges = ["35.235.240.0/20"]' in ssh[:400] and 'ports    = ["22"]' in ssh[:600]
    web = text[text.index('resource "google_compute_firewall" "controller_web"'):]
    assert 'ports    = ["80", "443"]' in web[:600] and '"22"' not in web[:600]
    assert 'account_id   = "lab-deploy-controller"' in text and "toset(local.observer_roles)" in text
    assert ('controller_host = local.controller_enabled ? (var.controller.domain != "" ? var.controller.domain : '
            'google_compute_address.controller[0].address) : ""') in text
    assert "fast-deploy-host       = local.controller_host" in text and "host = local.controller_host" in text
    assert '"projects/_/buckets/${google_storage_bucket.backups.name}/objects/machine/' in text  # its own folder only
    # gcloud lists a folder by its name without the slash (1.17.1: the live drill was refused for `prefix=machine`)
    assert 'objectListPrefix\\", \\"\\").startsWith(\\"machine\\"))"' in text
    assert 'role    = "roles/logging.logWriter"' in text
    assert 'role      = "roles/secretmanager.secretAccessor"' in text and "google_secret_manager_secret.github_app_key[0].id" in text


def test_the_uptime_check_runs_every_minute_from_three_places_and_alerts_after_five_minutes():
    text = CONTROLLER_TF.read_text()
    check = text[text.index('resource "google_monitoring_uptime_check_config" "controller"'):]
    assert 'period           = "60s"' in check and 'selected_regions = ["USA_OREGON", "USA_IOWA", "USA_VIRGINIA"]' in check
    assert 'path           = "/healthz"' in check and "validate_ssl   = true" in check and 'type   = "uptime_url"' in check
    alert = text[text.index('resource "google_monitoring_alert_policy" "controller"'):]
    assert 'duration        = "300s"' in alert and "REDUCE_COUNT_FALSE" in alert and "threshold_value = 1" in alert
    assert "google_monitoring_notification_channel.alert_email[*].id" in alert
    main = Path("infra/gcp/modules/project/main.tf").read_text()
    assert 'matches_prefix             = ["machine/litestream/"]' in main and "days_since_noncurrent_time = 1" in main


def test_the_startup_script_pins_its_tools_and_never_starts_the_controller_without_a_database():
    text = STARTUP.read_text()
    if shutil.which("bash"):
        subprocess.run(["bash", "-n", str(STARTUP)], check=True)
    assert re.search(r"^CADDY_SHA512=[0-9a-f]{128}$", text, re.M) and re.search(r"^LITESTREAM_SHA256=[0-9a-f]{64}$", text, re.M)
    assert '| "${algorithm}sum" --check --strict --quiet' in text
    units = re.findall(r"cat > /etc/systemd/system/([a-z-]+)\.service <<'EOF'\n(.*?)\nEOF", text, re.S)
    assert sorted(name for name, _ in units) == ["caddy", "fast-deploy", "litestream"]
    assert all("Restart=always" in body for _, body in units)
    controller = dict(units)["fast-deploy"]
    for condition in ("ConditionPathExists=/var/lib/fast-deploy/config.yaml", "ConditionPathExists=/var/lib/fast-deploy/deployments.db",
                      "ConditionPathExists=!/var/lib/fast-deploy/held"):
        assert condition in controller
    assert "--host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1" in controller and "ProtectSystem=strict" in controller
    assert "User=fastdeploy" in dict(units)["litestream"] and "--environ" not in text
    assert 'echo "	admin off"' in text and 'echo "		protocols h1 h2"' in text
    assert "merge-base --is-ancestor \"$COMMIT\" origin/main" in text and "--require-hashes -r \"$release/requirements.lock\"" in text
    assert "url: gs://$BUCKET/machine/litestream/deployments.db" in text and "sync-interval: 1m" in text and "l0-retention-check-interval: 5m" in text
    assert "gs://$BUCKET/machine/config.yaml" in text and "/controller/" not in text  # a fresh machine never wakes up with a laptop's state
    # Without a name: Let's Encrypt certifies the address itself only in its short-lived profile, and a client that
    # connects by address sends no server name, so Caddy is told which certificate to show.
    assert 'HOST=$(meta fast-deploy-host)' in text and 'if [[ -n "$ADDRESS_ONLY" ]]; then echo "	default_sni $HOST"; fi' in text
    assert 'echo "			profile shortlived"' in text and 'echo "		issuer acme {"' in text
    assert 'no database to restore: the controller stays off' in text


def test_every_trip_to_the_machine_goes_through_identity_aware_proxy():
    for script in (Path("scripts/machine.sh"), Path("scripts/install-machine.sh")):
        text = script.read_text()
        if shutil.which("zsh"):
            subprocess.run(["zsh", "-n", str(script)], check=True)
        trips = re.findall(r"gcloud compute (?:ssh|scp)[^\n]*", text)
        assert trips and all("--tunnel-through-iap" in trip for trip in trips), script
    move = Path("scripts/install-machine.sh").read_text()
    assert 'USAGE="usage: scripts/install-machine.sh GITHUB_APP_ID [--domain NAME] [--replace]"' in move
    assert 'if [[ -z "$DOMAIN" ]]; then\n  CONTROLLER_HOST=$ADDRESS' in move and '--host "$CONTROLLER_HOST"' in move
    start = Path("scripts/start.sh").read_text()
    assert "sed -n 's|^machine_url: *||p' config.local.yaml" in start and 'curl -fsS --max-time 10 "$MACHINE/healthz"' in start
    assert "grep -q '^github_app:' config.local.yaml" in start


def test_lock_containers_on_the_machine_never_reach_its_identity_and_run_without_privileges():
    from labdeploy import scaffold
    for command in (scaffold.lock_container("3.13"), scaffold.export_container("3.13")):
        joined = " ".join(command)
        assert command[:4] == ["docker", "run", "--rm", "-i"] and "--cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory 640m" in joined
    text = STARTUP.read_text()
    assert 'iifname "docker0" ip daddr { 169.254.0.0/16, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 } drop' in text
    assert '{"dns": ["8.8.8.8", "8.8.4.4"], "log-driver": "local"}' in text  # the metadata server is also the machine's DNS
    assert "systemctl disable --quiet --now docker.service" in text and "systemctl enable --quiet --now docker.socket" in text
    assert "SupplementaryGroups=docker" in text and "usermod -aG docker fastdeploy" in text


def test_the_operator_scripts_call_the_machine_once_the_move_names_it():
    for script in (Path("scripts/tool.sh"), Path("scripts/ctl.sh")):
        body = script.read_text()
        if shutil.which("zsh"):
            subprocess.run(["zsh", "-n", str(script)], check=True)
        assert 'os.environ.get("FAST_DEPLOY_URL")' in body and '.get("machine_url")' in body and '"http://127.0.0.1:8000"' in body
        assert "base() +" in body and "print(token" not in body


def test_the_setup_guide_creates_the_app_with_exactly_the_permissions_the_controller_uses():
    guide = Path("docs/SETUP.md").read_text()
    link = re.search(r"https://github.com/settings/apps/new\?\S+", guide).group(0)
    permissions = dict(part.split("=", 1) for part in link.split("?", 1)[1].split("&") if "=" in part)
    assert {key: value for key, value in permissions.items() if value in ("read", "write")} == {
        "actions": "write", "administration": "write", "contents": "write", "environments": "write", "metadata": "read",
        "pull_requests": "write", "workflows": "write"}
    assert permissions["public"] == "false" and permissions["webhook_active"] == "false"
    assert "**Credentials**, **Key pairs**,\n   **New key**" in guide
