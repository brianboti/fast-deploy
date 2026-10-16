"""The platform repository renamed on GitHub (1.14.1: brianboti/lab-deploy became brianboti/fast-deploy): its former name
keeps working, for the pipeline's sign-in and for the apps' pins, while everything moves to the new one."""
from pathlib import Path

import pytest
from conftest import FakeGitHub
from test_phase2 import make

from labdeploy.config import Platform
from labdeploy.workflows import pinned_commit, render

PIN = "3" * 40
TEMPLATE = Path("templates/deploy-gcp.yml").read_text()


def test_a_pin_is_read_under_the_repository_s_name_or_a_former_one_whatever_the_case():
    text = render(TEMPLATE, "lab/lab-deploy", PIN)
    assert pinned_commit(text, "lab/lab-deploy") == PIN and pinned_commit(text, "lab/fast-deploy") is None
    assert pinned_commit(text, ["lab/fast-deploy", "lab/lab-deploy"]) == PIN and pinned_commit(text, ["Lab/Lab-Deploy"]) == PIN


def test_a_former_name_must_be_another_repository():
    assert Platform(repository="lab/fast-deploy", former_repositories=["lab/lab-deploy"]).former_repositories == ["lab/lab-deploy"]
    assert Platform(repository="lab/fast-deploy").former_repositories == []
    for wrong in ("Lab/Fast-Deploy", "not a repository"):
        with pytest.raises(ValueError, match="another owner/name"):
            Platform(repository="lab/fast-deploy", former_repositories=[wrong])


def test_an_app_pinned_under_the_former_name_stays_supported_and_is_re_pinned_under_the_new_one(tmp_path):
    class Renamed(FakeGitHub):
        opened = None

        def contents(self, repository, ref, path):
            if path == ".github/workflows/deploy-gcp.yml":
                return {"sha": "w", "text": render(TEMPLATE, "lab/lab-deploy", PIN)}
            if path.startswith("templates/"):
                assert repository == "lab/fast-deploy"  # the templates come from the repository under its new name
                return {"sha": "t", "text": Path(path).read_text()}
            return None

        def update_workflows(self, app, sha, files):
            self.opened = files
            return {"number": 9, "url": "https://github.com/lab/demo/pull/9", "changed": sorted(files)}
    c, p, gh = make(tmp_path, Renamed(), platform={"repository": "lab/fast-deploy", "commit": PIN, "former_repositories": ["lab/lab-deploy"]})
    assert c.applications(p)["demo"]["platform_pin"] == {"commit": PIN, "supported": True, "note": None}
    result = c.update_workflows(p, "demo")  # the same commit, but under the former name: rewritten, not "already pinned"
    assert result["pull_request"]["number"] == 9 and result["current_pin"] == PIN
    deploy = gh.opened[".github/workflows/deploy-gcp.yml"]
    assert "repository: lab/fast-deploy" in deploy and "lab/lab-deploy" not in deploy
    (tmp_path / "without").mkdir()
    c2, p2, _ = make(tmp_path / "without", Renamed(), platform={"repository": "lab/fast-deploy", "commit": PIN})
    assert c2.applications(p2)["demo"]["platform_pin"]["supported"] is False  # without the former name the old pin is not read


def test_the_pipeline_s_sign_in_accepts_the_repository_s_name_and_each_former_one_only_by_its_ids():
    pipeline = Path("infra/gcp/modules/project/pipeline.tf").read_text()
    assert "concat([var.pipeline.platform_repository], var.pipeline.former_repositories)" in pipeline
    assert "assertion.repository_id == '${var.pipeline.platform_repository_id}'" in pipeline  # the ids stay the anchor
    assert "'${local.plan_workflows}'" not in pipeline and "(${local.plan_workflows})" in pipeline
    assert "assertion.workflow_ref in [${local.apply_workflows}]" in pipeline and "pipeline_repo" not in pipeline
    for path in ("infra/gcp/modules/project/variables.tf", "infra/gcp/project/variables.tf"):
        assert "former_repositories    = optional(list(string), [])" in Path(path).read_text()
