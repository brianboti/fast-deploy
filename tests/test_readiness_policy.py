import json

import pytest

from labdeploy import contractcheck, leaks


def contract(root, name="sample", **extra):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    fields = {"source": {"repository": "company/sample", "commit": "a" * 40}, "name": name, **extra}
    (folder / "platform.yaml").write_text(json.dumps(fields))
    return folder


@pytest.mark.parametrize("trust", ["reviewed", "unknown"])
@pytest.mark.parametrize("where", ["checkout", "image"])
def test_verified_credentials_block_all_admissions(tmp_path, capsys, trust, where):
    report = tmp_path / "scan"
    report.write_text(json.dumps({"DetectorName": "Example", "Verified": True, "Raw": "do-not-print-this",
                                 "SourceMetadata": {"Data": {"Docker": {"file": "/usr/lib/example"}}}}))
    result = tmp_path / "safe.json"
    assert leaks.main([str(report), "--trust", trust, "--where", where, "--json", str(result)]) == 1
    captured = capsys.readouterr()
    assert "do-not-print-this" not in captured.out + captured.err + result.read_text()
    assert json.loads(result.read_text())["policy_version"] == leaks.POLICY_VERSION


@pytest.mark.parametrize("text", ['{"Raw":"do-not-print-this"', '{"DetectorName":"X","Verified":"true"}', "invalid", "[]"])
def test_invalid_scan_is_not_clean(tmp_path, capsys, text):
    report = tmp_path / "scan"
    report.write_text(text)
    assert leaks.main([str(report), "--trust", "reviewed", "--where", "image"]) == 2
    captured = capsys.readouterr()
    assert "do-not-print-this" not in captured.err + captured.out


def test_empty_successful_scan_and_unverified_findings(tmp_path):
    report = tmp_path / "scan"
    report.write_text("")
    assert leaks.main([str(report), "--trust", "reviewed", "--where", "image"]) == 0
    report.write_text('{"DetectorName":"X","Verified":false}')
    assert leaks.main([str(report), "--trust", "reviewed", "--where", "image"]) == 0
    report.unlink()
    assert leaks.main([str(report), "--trust", "reviewed", "--where", "image"]) == 2


def test_contract_checks_human_edits_and_unsafe_locks(tmp_path):
    folder = contract(tmp_path)
    assert not contractcheck.validate(tmp_path)["errors"]
    (folder / "requirements.lock").write_text("library==1.2 --hash=sha256:" + "b" * 64)
    assert not contractcheck.validate(tmp_path)["errors"]
    (folder / "requirements.lock").write_text("-r /etc/passwd")
    assert contractcheck.validate(tmp_path)["errors"]
    (folder / "requirements.lock").unlink()
    contract(tmp_path, cpu=128)
    assert contractcheck.validate(tmp_path)["errors"]


def test_contract_symlinks_and_deleted_services_require_review(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    folder = contract(old)
    new.mkdir()
    assert contractcheck.validate(new, old)["errors"]
    retired = new / "retired"
    retired.mkdir()
    (retired / "sample.json").write_text(json.dumps({"service": "sample", "owner": "operations", "reason": "retired", "evidence": "change-123"}))
    assert not contractcheck.validate(new, old)["errors"]
    (new / "sample").symlink_to(folder, target_is_directory=True)
    assert contractcheck.validate(new)["errors"]


def test_existing_registry_passes():
    assert not contractcheck.validate("apps")["errors"]
