"""Phase 11: the platform writes the boilerplate. The repository check (11.1), the drafted files (11.2), the buildpack build
when there is no Dockerfile (11.3) and the onboarding measures (11.5)."""
from pathlib import Path

import pytest
import yaml
from test_onboarding import DOCKERFILE, GOOD_SPEC, PLATFORM, make

from labdeploy import scaffold
from labdeploy.cost import issue_body
from labdeploy.onboarding import contract_check, missing_message, summary
from labdeploy.workflows import lay_out


def test_the_check_lists_required_and_recommended_items_and_what_the_platform_can_draft(tmp_path):
    _, _, gh = make(tmp_path)
    result = contract_check(gh, "lab/newapp")
    assert result["ready"] and result["spec"].name == "newapp"
    recommended = {entry["item"]: entry for entry in result["recommended"]}
    assert set(recommended) == {"requirements.lock", ".dockerignore", "README.md", "LICENSE", "SECURITY.md", ".github/workflows/ci.yml",
                                ".github/dependabot.yml"}
    assert recommended["requirements.lock"]["platform_writes"] and not recommended["README.md"]["platform_writes"]
    # Any CI workflow of the repository counts (the scaffold's rule), and Renovate counts for Dependabot; the platform's own do not.
    gh.repos["lab/newapp"]["files"].update({".github/workflows/deploy-gcp.yml": "x", "renovate.json": "{}"})
    again = {entry["item"]: entry["ok"] for entry in contract_check(gh, "lab/newapp")["recommended"]}
    assert again[".github/dependabot.yml"] and not again[".github/workflows/ci.yml"]
    gh.repos["lab/newapp"]["files"][".github/workflows/tests.yml"] = "x"
    assert {entry["item"]: entry["ok"] for entry in contract_check(gh, "lab/newapp")["recommended"]}[".github/workflows/ci.yml"]


def test_a_procfile_can_stand_in_for_the_dockerfile(tmp_path):
    _, _, gh = make(tmp_path)
    files = gh.repos["lab/newapp"]["files"]
    files.pop("Dockerfile")
    blocked = contract_check(gh, "lab/newapp")
    assert not blocked["ready"] and blocked["checklist"][0] == {"item": "Dockerfile", "ok": False, "platform_writes": True, "why": "the build recipe"}
    assert "The platform can draft Dockerfile" in missing_message(blocked["checklist"]) and "propose_configuration" in missing_message(blocked["checklist"])
    files["Procfile"] = "web: uvicorn app:app --host 0.0.0.0 --port $PORT\n"
    ready = contract_check(gh, "lab/newapp")
    assert ready["ready"] and "buildpacks" in ready["checklist"][0]["detail"]
    assert not any(entry["item"].startswith("Dockerfile exposes") for entry in ready["checklist"])  # no Dockerfile, no port line


def test_check_repository_reads_only_and_needs_an_onboarding_identity(tmp_path):
    c, brian, gh = make(tmp_path)
    result = c.check_repository(brian, "https://github.com/lab/newapp")
    assert result["ready"] and result["next_action"].startswith("Ready to deploy: onboard_application with lab/newapp")
    assert gh.prs == {} and "newapp" not in c.config.applications  # nothing created, nothing registered
    missing = c.check_repository(brian, "lab/newapp", path="services/none")
    assert missing["ready"] is False and "no folder services/none" in missing["error"]
    narrow, someone, _ = make(tmp_path / "narrow", principal_apps=("demo",))
    with pytest.raises(ValueError, match=r"applications: \['\*'\]"):
        narrow.check_repository(someone, "lab/newapp")


def test_onboarding_measures_its_time_and_the_interventions(tmp_path):
    c, brian, gh = make(tmp_path)
    gh.repos["lab/newapp"]["files"].pop("Dockerfile")
    assert c.onboard(brian, "lab/newapp")["state"] == "blocked"
    gh.repos["lab/newapp"]["files"]["Dockerfile"] = DOCKERFILE
    gh.repos["lab/newapp"]["files"]["platform.yaml"] = GOOD_SPEC
    assert c.onboard(brian, "lab/newapp")["state"] == "planning"  # the operator fixed it: one intervention
    gh.runs[(PLATFORM, "infra-plan.yml", "d" * 40)] = [{"id": 1, "status": "completed", "conclusion": "success", "url": "u1", "head_sha": "d" * 40}]
    gh.comments[(PLATFORM, 10)] = ["<!-- lab-deploy-infra:newapp -->\n**Policy: passed.**"]
    c.advance_onboarding("newapp")
    gh.runs[(PLATFORM, "infra-apply.yml", "9" * 40)] = [{"id": 2, "status": "completed", "conclusion": "success", "url": "u2", "head_sha": "9" * 40}]
    c.advance_onboarding("newapp")
    gh.prs[("lab/newapp", 11)]["mergeable_state"] = "clean"
    done = c.advance_onboarding("newapp")
    assert done["state"] == "complete" and done["measures"]["blocked"] == 1 and done["measures"]["interventions"] == 1
    assert done["measures"]["blocked_steps"] == ["validating"] and done["measures"]["minutes"] >= 0
    line = gh.variables[("lab/newapp", "staging")]["ONBOARDING_SUMMARY"]
    assert line.startswith("Onboarded ") and "1 block (validating), 1 resolved by the operator" in line
    assert c.applications(brian)["newapp"]["onboarding"]["summary"] == line
    assert summary({"measures": {"minutes": 9.5}, "completed_at": 0}).endswith("in 9.5 minutes, with no step blocked.")


def test_the_weekly_issue_repeats_the_onboarding_summary():
    rightsize = yaml.safe_load(lay_out(Path("templates/rightsize-gcp.yml").read_text()))
    step = next(item for item in rightsize["jobs"]["rightsize"]["steps"] if item.get("name") == "Measure usage and compute right-sizing proposals")
    assert step["env"]["ONBOARDING_SUMMARY"] == "${{ vars.ONBOARDING_SUMMARY }}"
    report = {"generated_at": "now", "service": "newapp", "window_days": 14, "rightsizing": {"observed": {}, "recommendations": []},
              "estimate": {"compute_ceiling_usd_month": 1.0, "idle_floor_usd_month": 0.0},
              "onboarding": "Onboarded 2026-10-15 in 12 minutes, with no step blocked."}
    body = issue_body(report, "https://run")
    assert "Onboarded 2026-10-15 in 12 minutes" in body


class DraftingGitHub:
    """Just enough of GitHub for propose_configuration on a repository that is not registered."""

    def __init__(self, files):
        self.files, self.opened = files, []

    def head(self, app):
        return "a" * 40

    def tree(self, repository, ref):
        paths = set(self.files)
        return sorted(paths | {"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)})

    def contents(self, repository, ref, path):
        return {"sha": "s", "text": self.files[path]} if path in self.files else None

    def open_pull_request(self, app, branch, files, title, body, message, draft=False, create_only=False):
        self.opened.append({"repository": app.repository, "branch": branch, "files": files, "draft": draft, "create_only": create_only, "body": body})
        return {"number": 3, "url": f"https://github.com/{app.repository}/pull/3", "branch": branch, "changed": sorted(files)}


def test_the_checker_drafts_only_what_is_missing_as_a_draft_pull_request(tmp_path, monkeypatch):
    c, brian, _ = make(tmp_path)
    gh = c.github = DraftingGitHub({"services/web/app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {}\n",
                                    "services/web/requirements.txt": "fastapi==0.143.1\nuvicorn==0.54.0\n", "README.md": "# web\n"})
    compiled = []

    def compile_lock(text, python, runner=None, path=""):
        compiled.append((text, python, path))
        return {"text": "# uv pip compile requirements.txt --universal\nfastapi==0.143.1 --hash=sha256:" + "0" * 64 + "\n", "command": "uv ...", "error": None}
    monkeypatch.setattr(scaffold, "compile_lock", compile_lock)
    result = c.propose_config(brian, repository="https://github.com/lab/web", path="services/web")
    assert compiled == [("fastapi==0.143.1\nuvicorn==0.54.0\n", "3.12", "services/web")]  # D27: compiled for the app's Python
    opened = gh.opened[0]
    assert opened["draft"] and opened["create_only"] and opened["branch"] == "lab-deploy/contract-files-services-web"
    assert {"services/web/Dockerfile", "services/web/requirements.lock", "services/web/platform.yaml", "services/web/tests/test_health.py",
            ".github/workflows/ci.yml", "SECURITY.md", ".github/dependabot.yml"} <= set(opened["files"])
    assert "reviewed: false" in opened["files"]["services/web/platform.yaml"] and "README.md" not in opened["files"]
    assert "--require-hashes" in opened["files"]["services/web/Dockerfile"] and "The platform never merges this pull request." in opened["body"]
    assert result["pull_request"]["number"] == 3 and result["files"]["services/web/requirements.lock"].startswith("Every dependency pinned")
    assert "name: web" in opened["files"]["services/web/platform.yaml"]  # from the folder; a root service takes the repository's name
    # Everything present: nothing to draft, no pull request.
    gh.files.update({path: content for path, content in opened["files"].items()})
    gh.opened.clear()
    again = c.propose_config(brian, repository="lab/web", path="services/web")
    assert again["pull_request"] is None and gh.opened == [] and "Nothing to draft" in again["note"]


def test_drafting_for_an_unregistered_repository_needs_an_onboarding_identity(tmp_path):
    c, _, _ = make(tmp_path / "narrow", principal_apps=("demo",))
    narrow = c.config.principals[0]
    with pytest.raises(ValueError, match="identity that may onboard"):
        c.propose_config(narrow, repository="lab/web")
    with pytest.raises(ValueError, match="Give an application, or a repository link"):
        c.propose_config(narrow)


def test_without_a_dockerfile_the_build_uses_pinned_buildpacks():
    steps = yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))["jobs"]["build"]["steps"]
    lint = next(step for step in steps if step.get("name") == "Lint the Dockerfile")["run"]
    assert lint.index('if [ ! -f "$APP_DIR/Dockerfile" ]') < lint.index("curl")  # nothing to download or lint without one
    build = next(step for step in steps if step.get("name") == "Build without cloud credentials")
    run = build["run"]
    assert build["env"]["PYTHON_VERSION"] == "${{ steps.policy.outputs.python }}"
    assert "pack-v0.40.9-linux.tgz" in run and "dc0ee1e931cf8a106d7555a01a214864f9acb60b77adf15d69b74df4404758e9" in run
    # Both builders and run images by digest; 3.11 and 3.12 need the Ubuntu 22.04 one.
    assert run.count("gcr.io/buildpacks/builder@sha256:") == 2 and "google-22/run@sha256:" in run and "google-24/run@sha256:" in run
    assert "3.11|3.12)" in run and '--env "GOOGLE_PYTHON_VERSION=$PYTHON_VERSION.x"' in run and "--platform linux/amd64" in run
    assert "No Dockerfile and no Procfile" in run and "build.context: repository needs a Dockerfile" in run
    assert 'cp "$SOURCE/requirements.lock" "$SOURCE/requirements.txt"' in run  # pip installs the audited lock, with its hashes
    assert "--exclude=./.git" in run
