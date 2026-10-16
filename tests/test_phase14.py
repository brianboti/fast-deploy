"""Phase 14: safety for code nobody has reviewed (docs/ROADMAP.md D35). The Dockerfile policy, the secret scans, no cache,
the vulnerability gate by trust, the parts list, and the containment of unknown code at run time."""
import json
import time
from pathlib import Path

import pytest
import yaml
from conftest import TARGET

from labdeploy import dockerpolicy, leaks, reader, sbom, scaffold, scan
from labdeploy.controller import recent, release_approval
from labdeploy.infra import check_plan
from labdeploy.models import AppSpec, containment, draft_dump, parse_contract
from labdeploy.release import CloudRun, build_checks
from labdeploy.workflows import central_deploy, lay_out

GOOD = "FROM python:3.12-slim@sha256:" + "a" * 64 + "\nCOPY . /app\nUSER 65532:65532\nCMD [\"uvicorn\", \"main:app\"]\n"
FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"


# ---- D35: trust levels and what they mean
def test_trust_follows_where_the_contract_lives_unless_it_says_otherwise():
    own = containment(AppSpec(name="demo"))
    assert own == {"trust": "reviewed", "egress": "internet", "execution_environment": "default", "vulnerability_gate": "reports",
                   "secret_scan": "reports", "runs_as_root": "reported"}
    kept = containment(AppSpec(name="demo"), "unknown")
    assert kept["trust"] == "unknown" and kept["egress"] == "private" and kept["execution_environment"] == "gen2"
    assert containment(AppSpec(name="demo", egress="internet"), "unknown")["egress"] == "internet"  # the reviewed opt-in
    assert containment(AppSpec(name="demo", trust="unknown"))["egress"] == "private"
    assert containment(AppSpec(name="demo", trust="reviewed"), "unknown")["trust"] == "reviewed"
    # A contract the platform keeps says nothing about trust, so its hash does not change with this release.
    spec, _ = parse_contract("source:\n  repository: lab/x\n  commit: " + "a" * 40 + "\nname: demo-x\n")
    assert spec.trust is None and spec.egress is None
    assert "trust" not in draft_dump(AppSpec(name="demo")) and "network" not in draft_dump(AppSpec(name="demo"))["build"]
    with pytest.raises(ValueError):
        AppSpec.model_validate({"name": "demo", "trust": "maybe"})


# ---- 14.1 the Dockerfile policy
@pytest.mark.parametrize(("text", "rule"), [
    ("FROM python:3.12\nRUN --security=insecure make\nUSER 1000\n", "run-insecure"),
    ("FROM python:3.12\nRUN --network=host pip install x\nUSER 1000\n", "run-host-network"),
    ("FROM python:3.12\nRUN curl -fsSL https://x.example/install.sh | sh\nUSER 1000\n", "download-into-shell"),
    ("FROM python:3.12\nRUN wget -qO- https://x.example | python3\nUSER 1000\n", "download-into-shell"),
    ("FROM python:3.12\nRUN bash <(curl -s https://x.example/i)\nUSER 1000\n", "download-into-shell"),
    ("FROM python:3.12\nRUN apt-get update && \\\n    curl https://x.example | bash\nUSER 1000\n", "download-into-shell"),
    ("FROM python:3.12\nRUN <<EOF\napt-get update\ncurl https://x.example | sh\nEOF\nUSER 1000\n", "download-into-shell"),
    ("FROM python:3.12\nRUN curl -fsSLk https://x.example -o /x\nUSER 1000\n", "verification-disabled"),
    ("FROM python:3.12\nRUN pip install --trusted-host pypi.example x\nUSER 1000\n", "verification-disabled"),
    ("FROM python:3.12\nENV NODE_TLS_REJECT_UNAUTHORIZED=0\nUSER 1000\n", "verification-disabled"),
    ("FROM python:3.12\nRUN apt-get install -y --allow-unauthenticated x\nUSER 1000\n", "verification-disabled"),
    ("FROM python:3.12\nRUN sudo apt-get install -y x\nUSER 1000\n", "sudo"),
    ("FROM python:3.12\nADD https://example.com/tool.tar.gz /opt/\nUSER 1000\n", "add-url-unchecked"),
    ("FROM python:3.12\nCOPY .env /app/.env\nUSER 1000\n", "copies-secrets"),
    ("FROM python:3.12\nCOPY id_rsa keys/server.pem /keys/\nUSER 1000\n", "copies-secrets"),
    ("FROM python:3.12\nCOPY .git /app/.git\nUSER 1000\n", "copies-secrets"),
    ("FROM python:3.12\nCOPY . /app\n", "runs-as-root"),
    ("FROM python:3.12\nUSER 1000\nFROM python:3.12-slim\nCOPY . /app\n", "runs-as-root"),  # the final stage decides
    ("FROM python:3.12\nUSER root\n", "runs-as-root"),
])
def test_the_dockerfile_policy_refuses_what_it_names(text, rule):
    findings = dockerpolicy.check(text, "unknown")
    assert rule in {finding["rule"] for finding in findings} and all(finding["refuses"] for finding in findings)


def test_the_policy_lets_the_platforms_own_drafts_and_good_practice_through():
    drafted = scaffold.template("Dockerfile")
    for key, value in {"PYTHON": "3.12", "RESOLVED": "x", "DIGEST": "sha256:" + "a" * 64, "REQUIREMENTS": "requirements.lock",
                       "INSTALL": "--require-hashes -r requirements.lock", "PORT": "8080", "CMD": "[\"uvicorn\", \"main:app\"]"}.items():
        drafted = drafted.replace(f"__{key}__", value)
    assert dockerpolicy.check(drafted, "unknown") == []
    assert dockerpolicy.check(GOOD, "unknown") == []
    checked = "FROM python:3.12\nADD --checksum=sha256:" + "b" * 64 + " https://example.com/t.tgz /opt/\nCOPY --from=build /x /x\nUSER 65532\n"
    assert dockerpolicy.check(checked, "unknown") == []
    assert dockerpolicy.check("FROM python:3.12\nCOPY .env.example /app/\nUSER 1\n", "unknown") == []
    # The lab's own code: running as root is reported, not refused; every other rule refuses whoever wrote it.
    root = dockerpolicy.check("FROM python:3.12\nCOPY . .\n", "reviewed")
    assert [(f["rule"], f["refuses"]) for f in root] == [("runs-as-root", False)]
    assert dockerpolicy.check("FROM python:3.12\nRUN curl https://x | sh\nUSER 1\n", "reviewed")[0]["refuses"]


def test_the_policy_command_annotates_and_fails_closed(tmp_path, capsys):
    bad, good = tmp_path / "Dockerfile.bad", tmp_path / "Dockerfile.good"
    bad.write_text("FROM python:3.12\nRUN curl https://x.example | sh\n")
    good.write_text(GOOD)
    trivy = tmp_path / "trivy.json"
    trivy.write_text(json.dumps({"Results": [{"Misconfigurations": [{"ID": "DS-0002", "Severity": "HIGH", "Title": "Image user should not be 'root'",
                                                                      "CauseMetadata": {"StartLine": 1}}, {"ID": "DS-0026", "Severity": "LOW", "Title": "x"}]}]}))
    assert dockerpolicy.main([str(bad), "--trust", "unknown", "--shown", "svc/Dockerfile", "--trivy", str(trivy), "--json", str(tmp_path / "out.json")]) == 1
    out = capsys.readouterr().out
    assert "::error file=svc/Dockerfile,line=2,title=Dockerfile policy%3A download-into-shell::" in out
    assert "::warning file=svc/Dockerfile,line=1,title=Trivy DS-0002::" in out and "DS-0026" not in out  # high and critical only, never blocking
    assert json.loads((tmp_path / "out.json").read_text())["trust"] == "unknown"
    assert dockerpolicy.main([str(good), "--trust", "unknown"]) == 0 and "passed" in capsys.readouterr().out


# ---- 14.2 secrets
REPORT = "\n".join(json.dumps(item) for item in [
    {"DetectorName": "AWS", "Verified": True, "Raw": "THE-SECRET-VALUE", "Redacted": "AKIA...",
     "SourceMetadata": {"Data": {"Filesystem": {"file": "app/svc/leaked.cfg", "line": 3}}}},
    {"DetectorName": "Github", "Verified": False, "Raw": "ANOTHER-SECRET", "SourceMetadata": {"Data": {"Filesystem": {"file": "app/svc/.env", "line": 1}}}},
    {"DetectorName": "Slack", "Verified": False, "VerificationError": "timeout", "Raw": "X",
     "SourceMetadata": {"Data": {"Docker": {"file": "/app/x", "layer": "sha256:" + "c" * 64}}}},
]) + "\nnot json\n"


def test_secret_findings_never_carry_the_secret_and_only_working_keys_stop_unknown_code(tmp_path, capsys):
    found = leaks.findings(REPORT.splitlines(), strip="app/")
    assert [item["file"] for item in found] == ["svc/leaked.cfg", "svc/.env", "/app/x"] and found[2]["verification"] == "could not be checked"
    assert "THE-SECRET-VALUE" not in json.dumps(found) and "Raw" not in json.dumps(found)
    assert leaks.decide(found, "unknown")[0] is True and leaks.decide(found, "reviewed")[0] is False
    report = tmp_path / "report.jsonl"
    report.write_text(REPORT)
    assert leaks.main([str(report), "--trust", "unknown", "--where", "checkout", "--strip", "app/", "--json", str(tmp_path / "s.json")]) == 1
    printed = capsys.readouterr()
    assert "::error file=svc/leaked.cfg,line=3,title=Secret scan (checkout)%3A AWS::a key that works right now" in printed.out
    assert "::notice file=svc/.env,line=1" in printed.out and "THE-SECRET-VALUE" not in printed.out + printed.err
    assert "rotate it at the provider" in printed.err
    assert leaks.main([str(report), "--trust", "reviewed", "--where", "image"]) == 0
    assert "::warning title=Secret scan (image)%3A AWS::a key that works right now" in capsys.readouterr().out


def test_the_deny_list_dockerignore_keeps_secrets_out_and_lets_the_build_copy_its_tests():
    text = scaffold.deny_dockerignore()
    assert "**/.env" in text and "**/*.pem" in text and "**/id_rsa*" in text and ".git" in text and "**/*.ipynb" in text
    assert "tests" not in text.split() and "Dockerfile" not in text.split()  # a Dockerfile may copy those on purpose
    assert scaffold.main(["deny-dockerignore"]) == 0
    assert '"Dockerfile"' in scaffold.descriptor()  # the buildpacks leave it out too (1.15.1)


# ---- 14.3, 14.7 and the pipeline
def test_the_pipeline_runs_every_gate_with_pinned_scanners_and_no_cache():
    for text in (Path("templates/deploy-gcp.yml").read_text(), Path(".github/workflows/deploy.yml").read_text()):
        workflow = yaml.safe_load(lay_out(text) if "__LAB_" in text else text)
        steps = workflow["jobs"]["build"]["steps"]
        names = [step.get("name") for step in steps]
        for gate in ("Install the scanners", "Check the Dockerfile against the platform's policy", "Scan the checkout for secrets",
                     "Scan the image for secrets", "Make the image's parts list (SBOM)"):
            assert gate in names, gate
        # The repository's code runs only after its Dockerfile and its files have been checked.
        assert names.index("Scan the checkout for secrets") < names.index("Run unit tests") < names.index("Build without cloud credentials")
        install = steps[names.index("Install the scanners")]["run"]
        assert "trufflehog_3.97.4_linux_amd64.tar.gz" in install and "dc24007c2f233bd61c05beabeb44aa27ea9b43288166279209abe0458c5ce76b" in install
        assert "trivy_0.74.0_Linux-64bit.tar.gz" in install and "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a" in install
        build = steps[names.index("Build without cloud credentials")]["run"]
        assert build.count("docker build --no-cache") == 3 and "deny-dockerignore" in build and "cap_context" in build
        assert '"${PACK_NETWORK[@]}"' in build and "--network=none" in build
        for forbidden in ("actions/cache", "--cache-from", "--cache-to", "--allow ", "--allow=", "cache: pip"):
            assert forbidden not in text, forbidden
        deploy = workflow["jobs"]["deploy"]["steps"]
        deploy_names = [step.get("name") for step in deploy]
        assert "Attest the parts list (SBOM) with Cloud KMS" in deploy_names and "Attest the parts list (SBOM)" in deploy_names
        scan_step = deploy[deploy_names.index("Summarize image vulnerabilities")]
        assert scan_step["env"]["SPEC_FILE"] == "bundle/platform.yaml"
    assert yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))["env"]["TRUST_DEFAULT"] == "reviewed"
    central = Path(".github/workflows/deploy.yml").read_text()
    assert yaml.safe_load(central)["env"]["TRUST_DEFAULT"] == "unknown" and central_deploy(Path("templates/deploy-gcp.yml").read_text()) == central


def test_the_parts_list_is_summarised_for_the_receipt(tmp_path):
    (tmp_path / "sbom.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.7", "components": [{"name": "a"}, {"name": "b"}]}))
    found = sbom.summary(tmp_path / "sbom.cdx.json")
    assert found["components"] == 2 and found["spec_version"] == "1.7" and len(found["sha256"]) == 64
    assert sbom.summary(tmp_path / "missing.json") is None and sbom.main([str(tmp_path / "sbom.cdx.json")]) == 0
    (tmp_path / "dockerfile-policy.json").write_text(json.dumps({"findings": [{"refuses": False}], "trivy": [{"id": "DS-0002"}]}))
    (tmp_path / "secrets-image.json").write_text(json.dumps({"findings": [{"verified": False}, {"verified": True}]}))
    checks = build_checks(tmp_path)
    assert checks["dockerfile_policy"] == {"refused": 0, "reported": 1, "trivy_high_or_critical": 1}
    assert checks["secrets_in_image"] == {"findings": 2, "verified": 1, "in_installed_packages": 0} and checks["sbom"]["components"] == 2
    assert build_checks(tmp_path / "nothing") == {}


# ---- 14.4 the vulnerability gate
def summary(status="FINISHED_SUCCESS", critical=0, high=0, fixable_critical=0, fixable_high=0, malicious=()):
    zero = dict.fromkeys(scan.SEVERITIES, 0)
    return {"analysis_status": status, "by_severity": {**zero, "CRITICAL": critical, "HIGH": high},
            "fixable_by_severity": {**zero, "CRITICAL": fixable_critical, "HIGH": fixable_high}, "malicious": list(malicious)}


def test_the_vulnerability_gate_blocks_unknown_code_in_the_industrys_wording():
    assert "critical or high finding" in scan.verdict(summary(high=3, fixable_high=1), "unknown", False)
    assert scan.verdict(summary(critical=2, high=5), "unknown", False) is None  # nothing fixable yet: released, reported
    assert "did not finish" in scan.verdict(summary(status="PENDING"), "unknown", False)  # fails closed
    assert scan.verdict(summary(critical=1, fixable_critical=1), "reviewed", False) is None  # the lab's own: the operator's switch
    assert "critical vulnerabilities" in scan.verdict(summary(critical=1), "reviewed", True)
    assert "malicious" in scan.verdict(summary(malicious=["MAL-2026-1234"]), "reviewed", False)  # always
    occurrence = {"noteName": "projects/goog-vulnz/notes/MAL-2026-1234", "vulnerability": {"shortDescription": "MAL-2026-1234"}}
    counted = scan.summarize({"vulnerabilities": {"CRITICAL": [occurrence, {"noteName": "projects/goog-vulnz/notes/CVE-2026-1"}]}})
    assert counted["malicious"] == ["MAL-2026-1234"] and counted["by_severity"]["CRITICAL"] == 2


def test_the_gate_reads_the_trust_level_from_the_release(tmp_path):
    (tmp_path / "p.yaml").write_text("name: demo\nreviewed: true\n")
    assert scan.trust_of({"SPEC_FILE": str(tmp_path / "p.yaml"), "TRUST_DEFAULT": "unknown"}) == "unknown"
    assert scan.trust_of({"SPEC_FILE": str(tmp_path / "p.yaml")}) == "reviewed"
    (tmp_path / "q.yaml").write_text("name: demo\nreviewed: true\ntrust: unknown\n")
    assert scan.trust_of({"SPEC_FILE": str(tmp_path / "q.yaml")}) == "unknown"


def test_an_image_over_30_days_old_is_built_and_scanned_again():
    now = time.time()
    assert recent({"outcome": {"recorded_at": now - 5 * 86_400}}, now) and not recent({"outcome": {"recorded_at": now - 31 * 86_400}}, now)
    assert recent({"outcome": {}}, now)  # an older record without a time: the reused image is still scanned at release


# ---- 14.5, 14.6 containment at run time
def test_unknown_code_runs_contained_in_the_second_generation_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    contained = CloudRun("p", "us-central1", "asis", "asis-probe@p.iam.gserviceaccount.com", runner=lambda c: "", default_trust="unknown")
    flags = contained.egress_flags(AppSpec(name="asis"))
    assert flags == ["--network=lab-deploy", "--subnet=lab-deploy-run", "--vpc-egress=all-traffic", "--network-tags=lab-deploy-contained",
                     "--execution-environment=gen2"]
    assert contained.egress_flags(AppSpec(name="asis", egress="internet")) == ["--clear-network", "--execution-environment=gen2"]
    assert contained.egress_flags(AppSpec(name="asis"), job=True)[-1] == "--execution-environment=gen2"  # its migration job too
    own = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=lambda c: "")
    assert own.egress_flags(AppSpec(name="demo")) == ["--clear-network"]  # exactly as before 1.16.0
    database = AppSpec.model_validate({"name": "demo", "needs": {"database": "postgres"}})
    assert own.egress_flags(database) == ["--network=lab-deploy", "--subnet=lab-deploy-run", "--vpc-egress=private-ranges-only"]
    assert own.egress_flags(AppSpec(name="demo"), job=True) == []


def test_the_network_stays_without_a_way_out():
    def plan(kind, after):
        return {"resource_changes": [{"address": f"module.x.{kind}.y", "type": kind, "change": {"actions": ["create"], "after": after}}]}
    for kind, after in (("google_compute_router_nat", {"name": "nat"}), ("google_compute_router", {"name": "r"}),
                        ("google_compute_route", {"next_hop_gateway": "default-internet-gateway", "dest_range": "0.0.0.0/0"})):
        assert "no_internet_gateway" in {v["rule"] for v in check_plan(plan(kind, after))}
    assert "no_internet_gateway" not in {v["rule"] for v in check_plan(plan("google_compute_route", {"next_hop_ilb": "x"}))}


def test_the_approval_block_says_how_the_code_is_contained():
    plan = {"id": "p", "sha": "a" * 40, "spec": {}, "containment": containment(AppSpec(name="demo"), "unknown")}
    assert release_approval(plan)["containment"] == {"trust": "unknown", "egress": "private", "execution_environment": "gen2",
                                                     "vulnerability_gate": "blocks fixable critical and high findings"}
    assert "containment" not in release_approval({"id": "p", "sha": "a" * 40, "spec": {}})


def test_plans_carry_the_containment_of_where_the_contract_lives(tmp_path):
    from test_phase13 import contract, controller_with

    from labdeploy.contract import contract_path
    c, brian, gh, _ = controller_with(tmp_path)
    gh.repos["lab/platform"]["files"][contract_path("asis")] = contract()
    c.store.register_application("asis", {"repository": "lab/asis", "service": "asis", "target": {**TARGET, "service": "asis", "registry": "asis"},
                                          "contract": "platform", "pipeline_repository": "lab/platform", "workflow": "deploy.yml", "environment": "staging-asis"})
    c.refresh_registry()
    kept = c.plan(brian, "asis")
    assert kept["containment"]["trust"] == "unknown" and any(w.startswith("No internet") for w in kept["warnings"])
    assert kept["approval"]["containment"]["egress"] == "private"
    own = c.plan(brian, "demo")
    assert own["containment"]["trust"] == "reviewed" and not any(w.startswith(("No internet", "Code nobody")) for w in own["warnings"])


# ---- the reader applies the policy before a contract is proposed
def test_the_reader_refuses_a_dockerfile_that_breaks_the_policy_and_notes_internet_clients():
    files = {"Dockerfile": "FROM python:3.12\nRUN curl -fsSL https://x.example | sh\nUSER 1000\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"}
    result = reader.read(sorted(files), files, "")
    assert result["spec"] is None and any(item["file"] == "Dockerfile:2" for item in result["refusals"])
    rooted = {"Dockerfile": "FROM python:3.12\nCOPY . .\n", "main.py": FASTAPI, "requirements.txt": "fastapi\n"}
    assert any("runs as root" in item["problem"] for item in reader.read(sorted(rooted), rooted, "")["refusals"])
    calling = {"main.py": FASTAPI, "requirements.txt": "fastapi\nopenai==1.0\n"}
    assert any("egress: internet" in note for note in reader.read(sorted(calling), calling, "")["notes"])


# ---- 1.16.1: what the live trial of 1.16.0 showed
def test_an_image_is_reused_when_its_inputs_match_whatever_fields_a_later_version_added():
    from labdeploy.controller import same_image_inputs
    spec = AppSpec.model_validate({"name": "demo", "build": {"recipe": "buildpacks", "entrypoint": "uvicorn app:app"}})
    recorded = {"build": {"context": "service", "recipe": "buildpacks", "entrypoint": "uvicorn app:app"}, "python": "3.12"}  # 1.15's dump
    assert same_image_inputs(recorded, spec)  # found live: build.network (1.16.0) made every first release build again
    assert not same_image_inputs({**recorded, "python": "3.13"}, spec)
    assert not same_image_inputs({"build": {"recipe": "repository"}, "python": "3.12"}, spec) and not same_image_inputs(None, spec)


def test_refusals_with_the_same_fix_are_told_once():
    from labdeploy import chat
    fix = "remove it and rotate what it held"
    refusals = [{"file": "env.prod", "problem": "looks like a committed secret", "fix": fix},
                {"file": "env.yaml", "problem": "looks like a committed secret", "fix": fix},
                {"file": "Dockerfile:1", "problem": "the final image runs as root", "fix": "add USER 65532:65532 (a numeric user)"}]
    text = chat.contract_import({"repository": "lab/x", "refusals": refusals})
    assert text.count(fix) == 1 and "1. `env.prod`, `env.yaml` looks like a committed secret. Fix: remove it and rotate what it held." in text
    assert "2. `Dockerfile:1` the final image runs as root. Fix: add USER 65532:65532 (a numeric user)." in text


def test_the_scan_answer_says_whether_the_gate_stopped_the_release():
    from labdeploy import chat
    base = {"analysis_status": "FINISHED_SUCCESS", "by_severity": {"CRITICAL": 0, "HIGH": 5}, "fixable_by_severity": {"CRITICAL": 0, "HIGH": 0}}
    # Found live: 1.16.0 said "it blocked the release" for every unknown app, since its gate is always on.
    assert chat.scan_line({**base, "trust": "unknown", "blocking": True, "blocked": False}).endswith("the gate let it through, since none can be fixed yet")
    assert chat.scan_line({**base, "trust": "unknown", "blocking": True, "blocked": True}).endswith("it stopped the release")
    assert chat.scan_line({**base, "trust": "reviewed", "blocking": False, "blocked": False}).endswith("not blocking")
    assert chat.scan_line({**base, "blocking": True}).endswith("not blocking")  # before 1.16.1: blocking stopped only critical findings


def test_unconfirmed_patterns_in_installed_packages_are_counted_not_listed(tmp_path, capsys):
    lines = [json.dumps({"DetectorName": "URI", "Verified": False, "SourceMetadata": {"Data": {"Docker": {"file": path}}}})
             for path in ("/usr/local/lib/python3.12/site-packages/httpx/_urls.py", "/var/lib/dpkg/available", "/app/settings.py")]
    lines.append(json.dumps({"DetectorName": "AWS", "Verified": True, "SourceMetadata": {"Data": {"Docker": {"file": "/usr/local/lib/x/creds.py"}}}}))
    report = tmp_path / "image.jsonl"
    report.write_text("\n".join(lines))
    assert leaks.main([str(report), "--trust", "unknown", "--where", "image", "--json", str(tmp_path / "s.json")]) == 1
    out = capsys.readouterr().out
    assert "httpx" not in out and "dpkg" not in out and "/app/settings.py" in out and "/usr/local/lib/x/creds.py" in out  # a working key anywhere
    assert "2 unconfirmed in installed packages or system files" in out and json.loads((tmp_path / "s.json").read_text())["installed"] == 2
