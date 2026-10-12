import pytest

from labdeploy.analyzer import analyze, analyze_local
from labdeploy.models import parse_spec


@pytest.mark.parametrize("text", ["name: demo\nname: other", "name: demo\nresources:\n  cpu: 1\n  cpu: 2", "name: demo\nexposure: public",
                                  "name: demo\nreviewed: 'true'", "name: demo\nunknown: 1", "name: demo\nport: 0"])
def test_malformed_spec_rejected(text):
    with pytest.raises(ValueError):
        parse_spec(text)


def test_generated_config_requires_review():
    result = analyze({"requirements.txt": "fastapi\nasyncpg", "Dockerfile": "EXPOSE 8000"}, "demo")
    assert result["draft_spec"]["port"] == 8000
    assert result["draft_spec"]["dependencies"] == ["postgres"]
    assert result["draft_spec"]["reviewed"] is False
    assert len(result["review_required"]) == 6 and result["draft_spec"]["concurrency"] == 80
    assert analyze({"requirements.txt": "flask==3.0"}, "demo")["draft_spec"]["concurrency"] == 8


def test_analyzer_does_not_follow_symlinks(tmp_path):
    (tmp_path / "requirements.txt").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="Symlinks"):
        analyze_local(tmp_path, "demo")

