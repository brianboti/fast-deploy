"""Phase 16, part six: a name of the lab's own for production (docs/ROADMAP.md D39, roadmap item 16.6)."""
import re
import time
from pathlib import Path

import pytest
from conftest import TARGET
from test_promotion import ROOT, STAGING_IMAGE, ready_controller

from labdeploy import infra
from labdeploy.cost import DOMAIN_PRICING, domain_floor, estimate, findings, needs_costs
from labdeploy.infra import IDLE_FLOOR_USD_MONTH, _floor_of, app_root, check_plan, exposure_changes
from labdeploy.models import DOMAIN, AppSpec, spec_hash

PRODUCTION = dict(project_id="lab-deploy-prod-abcd", environment="production", github_environment="production-demo", workflow_files=["promote.yml"],
                  pipeline="platform", **{**ROOT, "state_bucket": "lab-deploy-prod-abcd-tfstate"})
MODULE = Path("infra/gcp/modules/service")


def spec(**extra):
    return AppSpec.model_validate({"name": "demo", "reviewed": True, **extra})


# ---- the contract
def test_a_domain_is_a_host_name_of_the_lab_s_own_and_leaves_other_contracts_as_they_were():
    for good in ("api.example.org", "34-120-1-2.sslip.io", "xn--bcher-kva.example"):
        assert spec(domain=good).domain == good
    for bad in ("*.example.org", "Example.org", "example", "1.2.3.4", "a-.example.org", "demo-abc-uc.a.run.app", "x." * 130 + "org"):
        with pytest.raises(ValueError):
            spec(domain=bad)
    assert spec_hash(spec()) == spec_hash(AppSpec.model_validate({"name": "demo", "reviewed": True}))  # an unused field changes no hash
    assert DOMAIN.pattern == infra.DOMAIN.pattern  # the renderer, standard library only, keeps its own copy


# ---- the root
def test_only_a_production_root_carries_the_domain_and_prints_its_address():
    files = app_root("demo", domain="api.example.org", **PRODUCTION)
    assert 'domain                       = "api.example.org"' in files["main.tf"]
    assert 'output "domain_address" { value = module.service.domain_address }' in files["outputs.tf"]
    plain = app_root("demo", **PRODUCTION)
    assert "domain" not in plain["main.tf"] and "domain_address" not in plain["outputs.tf"]  # other roots stay as they were
    with pytest.raises(ValueError, match="staging keeps its run.app address"):
        app_root("demo", project_id="test-project", domain="api.example.org", **ROOT)
    with pytest.raises(ValueError, match="Invalid domain"):
        app_root("demo", domain="demo.a.run.app", **PRODUCTION)


def test_the_module_puts_a_certificate_and_cloud_armor_in_front_of_production_only():
    domain = (MODULE / "domain.tf").read_text()
    assert 'domain_enabled = var.domain != "" && local.environment_name == "production"' in domain
    gated = len(re.findall(r"count +=  ?local\.domain_enabled \? 1 : 0", domain))
    checked = len(re.findall(r"count += local\.domain_enabled && var\.runtime_alerts \? 1 : 0", domain))  # the domain's check (26.4)
    assert (gated, checked) == (12, 2) and gated + checked == domain.count('resource "')  # every resource is the opt-in's
    assert 'exceed_action  = "deny(429)"' in domain and 'enforce_on_key = "IP"' in domain and "domain_requests_per_minute = 600" in domain
    assert "security_policy       = google_compute_security_policy.domain[0].id" in domain and "sample_rate = 0.1" in domain
    assert 'profile         = "MODERN"' in domain and 'min_tls_version = "TLS_1_2"' in domain and "https_redirect = true" in domain
    assert 'name  = "${local.domain_name}-${substr(sha256(var.domain), 0, 8)}"' in domain and "create_before_destroy = true" in domain
    assert "iap" not in domain.split("locals {", 1)[1].lower()  # IAP stays on the service, which enforces it on every way in
    main = (MODULE / "main.tf").read_text()
    assert 'custom_audiences = local.domain_enabled ? ["https://${var.domain}"] : null' in main
    assert 'ingress             = "INGRESS_TRAFFIC_ALL"' in main  # the release probes run.app, so ingress is not narrowed
    assert 'variable "domain"' in (MODULE / "variables.tf").read_text()
    assert "output \"domain_address\"" in (MODULE / "outputs.tf").read_text()


def test_production_s_pipeline_may_build_a_balancer_and_staging_s_is_unchanged():
    pipeline = Path("infra/gcp/modules/project/pipeline.tf").read_text()
    assert 'domains_enabled = local.pipeline_enabled && var.environment == "production"' in pipeline
    section = pipeline.split("domains_enabled = ", 1)[1].split("# An application root with database_auth", 1)[0]
    assert len(re.findall(r"count += local\.domains_enabled \? 1 : 0", section)) == section.count('resource "') == 5
    assert '"roles/compute.loadBalancerAdmin"' in section and "roles/compute.securityAdmin" not in section  # no firewall rules
    armor = section.split('"armor_manager"', 1)[1].split("}", 1)[0]
    assert "compute.securityPolicies.create" in armor and "firewalls" not in armor
    viewer = section.split('"balancer_viewer"', 1)[1].split("}", 1)[0]
    assert all(".get" in p or ".list" in p for p in viewer.split("permissions = ", 1)[1].strip("[] \n").replace('"', "").split(", "))


# ---- what it costs, where it is shown
def test_a_domain_costs_its_floor_in_production_and_nothing_in_staging():
    assert domain_floor() == 25.25 == round(DOMAIN_PRICING["forwarding_rules_usd_month"] + 5 + 2, 2)
    chosen = spec(domain="api.example.org")
    item = next(i for i in needs_costs(chosen, environment="production") if i["need"] == "domain")
    assert item == {"need": "domain", "name": "api.example.org", "floor_usd_month": 25.25, "pricing": DOMAIN_PRICING}
    assert not any(i["need"] == "domain" for i in needs_costs(chosen))  # staging keeps its run.app address
    assert estimate(chosen, environment="production")["needs_floor_usd_month"] == 25.25 and estimate(chosen)["needs_floor_usd_month"] == 0.0
    warning = next(f for f in findings(chosen, environment="production") if f["id"] == "domain_floor")
    assert warning["severity"] == "warning" and "more than a $20 monthly budget" in warning["message"]
    assert not any(f["id"] == "domain_floor" for f in findings(chosen))


def test_the_plan_prices_the_balancer_as_the_catalog_does():
    rules = IDLE_FLOOR_USD_MONTH["google_compute_global_forwarding_rule"]
    assert rules["443"] == DOMAIN_PRICING["forwarding_rules_usd_month"] and rules["80"] == 0.0  # the first five rules share one charge
    assert _floor_of("google_compute_global_forwarding_rule", {"port_range": "443"}) == 18.25
    assert _floor_of("google_compute_global_forwarding_rule", {"port_range": "443-443"}) == 18.25  # as Google stores it
    assert _floor_of("google_compute_global_forwarding_rule", {"port_range": "80"}) == 0.0
    assert IDLE_FLOOR_USD_MONTH["google_compute_security_policy"] == {"policy": DOMAIN_PRICING["armor_policy_usd_month"], "rule": DOMAIN_PRICING["armor_rule_usd_month"]}
    assert _floor_of("google_compute_security_policy", {"rule": [{"priority": 1000}, {"priority": 2147483647}]}) == 7.0


def change(kind, after, unknown=None, actions=("create",), address=None):
    return {"address": address or f"module.service.{kind}.domain[0]", "type": kind,
            "change": {"actions": list(actions), "before": None, "after": after, "after_unknown": unknown or {}}}


def test_the_policy_keeps_cloud_armor_and_modern_tls_in_front_of_an_application():
    bare = check_plan({"resource_changes": [change("google_compute_backend_service", {"security_policy": None, "load_balancing_scheme": "EXTERNAL_MANAGED"})]})
    assert [v["rule"] for v in bare] == ["armored_balancer"]
    created = check_plan({"resource_changes": [change("google_compute_backend_service", {}, {"security_policy": True})]})  # built in the same apply
    assert created == []
    proxy = check_plan({"resource_changes": [change("google_compute_target_https_proxy", {"ssl_policy": None})]})
    old = check_plan({"resource_changes": [change("google_compute_ssl_policy", {"min_tls_version": "TLS_1_0", "profile": "COMPATIBLE"})]})
    assert [v["rule"] for v in proxy] == ["modern_tls"] and [v["rule"] for v in old] == ["modern_tls"]
    assert check_plan({"resource_changes": [change("google_compute_ssl_policy", {"min_tls_version": "TLS_1_2", "profile": "MODERN"})]}) == []
    shown = exposure_changes({"resource_changes": [change("google_compute_managed_ssl_certificate", {"managed": [{"domains": ["api.example.org"]}]})]})
    assert shown == [{"address": "module.service.google_compute_managed_ssl_certificate.domain[0]", "action": "create",
                      "detail": "a load balancer answers at api.example.org"}]


# ---- the controller
def test_production_s_root_is_rendered_with_the_contract_s_domain_and_staging_s_without(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    gh.spec = {"name": "demo", "reviewed": True, "domain": "api.example.org"}
    production = c.render_infrastructure(brian, "demo", environment="production")["files"]
    assert 'domain                       = "api.example.org"' in production["main.tf"]
    assert "domain" not in c.render_infrastructure(brian, "demo")["files"]["main.tf"]


def release(c, chosen):
    c.store.add("brian", {"id": "e" * 32, "application": "demo", "repository": "lab/demo", "branch": "main", "sha": "a" * 40, "commit": {"sha": "a" * 40},
                          "spec_hash": spec_hash(chosen), "spec": chosen.model_dump(), "cloud": "gcp", "environment": "staging", "target": TARGET,
                          "expires_at": time.time(), "checks": {}, "layout": {"path": "", "workflow": "deploy-gcp.yml", "github_environment": "staging"},
                          "receipt": {"state": "succeeded", "image": STAGING_IMAGE, "provenance": "github", "revision": "demo-r1"},
                          "outcome": {"state": "succeeded", "recorded_at": time.time()}}, "succeeded")


def test_a_promotion_shows_the_domain_s_floor_and_whether_production_builds_it_yet(tmp_path):
    c, brian, gh = ready_controller(tmp_path)  # production's root on main was rendered without a domain
    chosen = spec(domain="api.example.org")
    release(c, chosen)
    plan = c.promote(brian, "demo")
    assert plan["cost"]["needs_floor_usd_month"] == 25.25 and plan["approval"]["cost"]["production"]["needs_floor_usd_month"] == 25.25
    assert any("which production's root on main does not build yet: ask for add_production" in w for w in plan["warnings"])
    assert any(f["id"] == "domain_floor" for f in plan["cost"]["findings"])
    gh.spec = chosen.model_dump()
    gh.on_main["infra/gcp/apps-production/demo/main.tf"] = c.render_infrastructure(brian, "demo", environment="production")["files"]["main.tf"]
    again = c.promote(brian, "demo")
    assert any(w.startswith("Also answers at https://api.example.org through a load balancer with Cloud Armor (about $25.25 a month)")
               for w in again["warnings"])
