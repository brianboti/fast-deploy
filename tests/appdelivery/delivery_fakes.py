"""Fakes for the delivery tests: a configuration, a cloudinfra answer in the layer's real JSON shape, an HTTP transport and
a command runner. Self-contained on purpose: nothing here comes from tests/conftest.py or another test directory."""
import copy
import hashlib
import json

import httpx

from appdelivery.config import Config
from appdelivery.remote import Completed

REQUEST_ID = "3f6c" + "0" * 26 + "a1"
DIGEST = "9b2f" + "1" * 60
IMAGE = f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}"
PROD_SA = "appdelivery-prod@infra-project.iam.gserviceaccount.com"
DEV_SA = "appdelivery-dev@infra-project.iam.gserviceaccount.com"
TEAM_TOKEN = "team-token"


def config_dict(**overrides):
    data = {
        "database": ":memory:",
        "cloudinfra": {"url": "https://infra.example.com"},
        "gcp": {"project_id": "infra-project"},
        "environments": {
            "dev": {"token_env": "APPDELIVERY_DEV_INFRA_TOKEN", "service_account": DEV_SA, "allowed_registries": ["us-central1-docker.pkg.dev/acme-apps"]},
            "prod": {"token_env": "APPDELIVERY_PROD_INFRA_TOKEN", "service_account": PROD_SA, "allowed_registries": ["us-central1-docker.pkg.dev/acme-apps"],
                     "require_attestation": True, "trusted_signers": ["/etc/appdelivery/keys/prod.pub"], "require_approval": True},
        },
        "principals": [
            {"name": "shop-team", "token_sha256": hashlib.sha256(TEAM_TOKEN.encode()).hexdigest(), "infra_owner": "shop-team", "environments": ["dev", "prod"],
             "can_deploy": True},
            {"name": "shop-oncall", "token_sha256": "1" * 64, "infra_owner": "shop-team", "environments": ["prod"], "can_approve": True},
        ],
    }
    data.update(overrides)
    return data


def make_config(**overrides) -> Config:
    return Config.model_validate(config_dict(**overrides))


def principal(config, name="shop-team"):
    return next(item for item in config.principals if item.name == name)


def release_dict(**overrides):
    data = {"environment": "prod", "request_id": REQUEST_ID, "target": {"group": "web"}, "image": IMAGE, "port": 8080, "health": {"path": "/healthz"},
            "env": {"LOG_LEVEL": {"value": "info"}, "DB_HOST": {"endpoint": "database_host"}, "DB_PASSWORD": {"secret": "db-password"}}}
    data.update(overrides)
    return data


def _resource(kind, rid, name, **fields):
    return {"id": rid, "kind": kind, "name": name, "request_id": REQUEST_ID, "provider": "gcp", "region": "us-central1", "zone": None, "status": "READY",
            "labels": {}, "created_at": "2026-10-29T10:00:00Z", "updated_at": "2026-10-29T10:00:00Z", "error": None, "access": [], "connect": None, **fields}


def machine(rid, name, **fields):
    base = {"zone": "us-central1-a", "group": "web", "machine_type": "e2-standard-2", "cpu": 2, "memory_gb": 8, "os_image": "cos-133-lts", "boot_disk_gb": 20,
            "accelerator": None, "internal_ip": "10.10.0.2", "public_ip": None, "attached_disks": [], "network": "net-aaaaaaaaaaaa", "profile": "container-host",
            "secure_boot": True, "autostop_idle_minutes": None, "max_run_hours": None, "started_at": None, "stop_reason": None, "instance_id": "1234567890"}
    base.update(fields)
    return _resource("compute", rid, name, **base)


def infra_response(**overrides):
    """GET /infrastructure/{id} as cloudinfra answers it: a READY request of shop-team with a network that admits IAP SSH and
    port 8080 from anywhere, two Container-Optimized OS machines in group web, a database password secret, and the prod
    service account granted admin on everything."""
    network = _resource("network", "net-aaaaaaaaaaaa", "shop-net", subnet="projects/infra-project/regions/us-central1/subnetworks/shop-net-subnet",
                        cidr="10.10.0.0/24", public_access=True, private_services_range="shop-net-services",
                        firewall_rules=[{"name": "shop-net-allow-internal", "direction": "ingress", "ports": [], "sources": ["10.10.0.0/24"], "provider_id": None},
                                        {"name": "shop-net-allow-iap-ssh", "direction": "ingress", "ports": [22], "sources": ["35.235.240.0/20"], "provider_id": None},
                                        {"name": "shop-net-allow-public", "direction": "ingress", "ports": [8080], "sources": ["0.0.0.0/0"], "provider_id": None}])
    machines = [machine("cmp-111111111111", "shop-web-1"), machine("cmp-222222222222", "shop-web-2", internal_ip="10.10.0.3", instance_id="1234567891")]
    secret = _resource("secret", "sec-333333333333", "shop-db-password", resource_name="projects/infra-project/secrets/shop-db-password", versions=1,
                       purpose="database_password")
    data = {
        "request_id": REQUEST_ID, "name": "shop", "provider": "gcp", "region": "us-central1", "zone": "us-central1-a", "status": "READY", "owner": "shop-team",
        "labels": {}, "resources": {"network": [network], "storage": [], "compute": machines, "database": [], "cache": [], "queue": [], "secret": [secret],
                                    "cluster": [], "load_balancer": []},
        "endpoints": {"public_ips": [], "internal_ips": ["10.10.0.2", "10.10.0.3"], "database_host": "10.20.0.5", "database_port": 5432, "bucket_urls": [],
                      "load_balancer_address": None, "cache_host": None, "cache_port": None, "cluster_endpoint": None, "queue_topics": [],
                      "secret_names": ["projects/infra-project/secrets/shop-db-password"]},
        "credentials": None, "lifecycle": None,
        "access": [{"member": f"serviceAccount:{PROD_SA}", "level": "admin", "status": "GRANTED",
                    "resources": ["cmp-111111111111", "cmp-222222222222", "sec-333333333333"], "error": None, "updated_at": "2026-10-29T10:00:00Z"}],
        "warnings": [], "operations": [], "estimated_monthly_cost_usd": 61.2, "error": None, "created_at": "2026-10-29T10:00:00Z", "updated_at": "2026-10-29T10:00:00Z",
    }
    for key, value in overrides.items():
        data[key] = value
    return copy.deepcopy(data)


def infra_transport(answer=None, status=200, seen=None):
    """An httpx transport playing cloudinfra's GET /infrastructure/{id}."""
    def handle(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"status": "FAILED", "error": {"code": "NOT_FOUND", "message": "no"}})
        return httpx.Response(200, json=answer if answer is not None else infra_response())
    return httpx.MockTransport(handle)


class Runner:
    """Records each command and answers from a queue, as remote.run would."""

    def __init__(self, *answers):
        self.answers = list(answers) or [Completed(0, "", "")]
        self.calls = []

    def __call__(self, argv, stdin=None, env=None, timeout=120):
        config = None
        if env and env.get("DOCKER_CONFIG"):
            with open(f"{env['DOCKER_CONFIG']}/config.json") as handle:
                config = json.load(handle)
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": env, "timeout": timeout, "docker_config": config})
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
