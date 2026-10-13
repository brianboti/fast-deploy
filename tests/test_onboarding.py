"""One-sentence onboarding: the state machine over a fake GitHub, the registry, wildcard identities and the contract check."""
import hashlib

import pytest
from conftest import TARGET, FakeGitHub

from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.onboarding import normalize_repository, slug

PLATFORM = "lab/platform"
TEMPLATE = "name: X\njobs:\n  j:\n    steps:\n      - uses: actions/checkout@abc\n        with:\n          repository: PLATFORM_REPOSITORY\n          ref: PLATFORM_COMMIT\n"
GOOD_SPEC = ("api_version: lab/v1\nname: newapp\nport: 8080\nhealth_path: /health\nreviewed: true\n"
             "needs: {buckets: [uploads], topics: [], database: none, migrate: []}\n")
DOCKERFILE = "FROM python:3.12-slim@sha256:" + "a" * 64 + "\nEXPOSE 8080\nCMD [\"uvicorn\"]\n"


class OnboardingGitHub(FakeGitHub):
    """A fake with repositories, pull requests, runs and environments in memory."""

    def __init__(self):
        self.repos = {
            "lab/newapp": {"files": {"Dockerfile": DOCKERFILE, "platform.yaml": GOOD_SPEC, "requirements.txt": "fastapi==0.1\n", "requirements-dev.txt": "pytest\n",
                                     "tests/test_app.py": "def test(): pass\n"}, "default_branch": "main"},
            PLATFORM: {"files": {f"templates/{n}": TEMPLATE for n in ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml")} | {"infra/gcp/project/.terraform.lock.hcl": "# lock\n"},
                       "default_branch": "main"},
        }
        self.prs = {}
        self.runs = {}
        self.comments = {}
        self.environments = {}
        self.variables = {}
        self.merges = []
        self.next_pr = 10

    # reads
    def repository(self, repository):
        repo = self.repos[repository]
        return {"repository_id": "123", "owner_id": "45", "default_branch": repo["default_branch"], "private": False, "url": f"https://github.com/{repository}"}

    def tree(self, repository, ref):
        return sorted({p for p in self.repos[repository]["files"]} | {"tests"})

    def contents(self, repository, ref, path):
        text = self.repos.get(repository, {}).get("files", {}).get(path)
        return None if text is None else {"sha": "s", "text": text}

    def branch_head(self, repository, branch="main"):
        return "f" * 40

    def file(self, app, sha, filename):
        return self.repos[app.repository]["files"][filename]

    # pull requests
    def _open(self, repository, branch, files, title):
        number = self.next_pr
        self.next_pr += 1
        self.prs[(repository, number)] = {"files": dict(files), "merged": False, "head_sha": "d" * 40, "mergeable_state": "unknown", "branch": branch,
                                          "url": f"https://github.com/{repository}/pull/{number}", "title": title}
        return {"number": number, "url": f"https://github.com/{repository}/pull/{number}", "branch": branch, "changed": sorted(files)}

    def open_platform_pull_request(self, repository, base_branch, branch, files, title, body, message):
        return self._open(repository, branch, files, title)

    def update_workflows(self, app, platform_sha, files):
        return self._open(app.repository, f"lab-deploy/platform-{platform_sha[:12]}", files, "Update workflows")

    def pull_request(self, repository, number):
        pr = self.prs[(repository, number)]
        return {"number": number, "state": "closed" if pr["merged"] else "open", "merged": pr["merged"], "mergeable_state": pr["mergeable_state"],
                "head_sha": pr["head_sha"], "merge_commit_sha": "9" * 40 if pr["merged"] else None, "url": pr["url"]}

    def pull_request_files(self, repository, number):
        return sorted(self.prs[(repository, number)]["files"])

    def merge_pull_request(self, repository, number, title):
        pr = self.prs[(repository, number)]
        pr["merged"] = True
        self.repos[repository]["files"].update(pr["files"])
        self.merges.append((repository, number))
        return {"merged": True, "sha": "9" * 40, "reason": None}

    def issue_comments(self, repository, number):
        return self.comments.get((repository, number), [])

    def workflow_runs(self, repository, workflow, head_sha=None, limit=5):
        return self.runs.get((repository, workflow, head_sha), [])

    def ensure_environment(self, repository, name, branch):
        self.environments[(repository, name)] = branch

    def set_environment_variables(self, repository, name, variables):
        self.variables[(repository, name)] = dict(variables)


def make(tmp_path, onboarding=True, principal_apps=("*",)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = {"live_dispatch": True, "database": str(tmp_path / "db"),
           "platform": {"repository": PLATFORM},
           "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
           "principals": [{"name": "brian", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": list(principal_apps), "can_deploy": True}]}
    if onboarding:
        raw["onboarding"] = {"project_id": "test-project", "project_number": "123456789", "region": "us-central1"}
    config = LiveConfig(Config.model_validate(raw))
    github = OnboardingGitHub()
    controller = Controller(config, github, Store(config.database))
    return controller, config.current.principals[0], github


def test_repository_links_and_ids_are_normalised():
    assert normalize_repository("https://github.com/lab/newapp") == "lab/newapp"
    assert normalize_repository("https://github.com/lab/newapp.git/") == "lab/newapp"
    assert normalize_repository("lab/newapp") == "lab/newapp"
    assert slug("My_Service.v2") == "my-service-v2"
    with pytest.raises(ValueError):
        normalize_repository("gitlab.com/lab/newapp")


def test_one_sentence_onboarding_runs_through_every_step(tmp_path):
    c, brian, gh = make(tmp_path)
    first = c.onboard(brian, "https://github.com/lab/newapp")
    # validated, registered, the infrastructure pull request opened; now waiting for the plan run
    assert first["state"] == "planning" and first["service"] == "newapp" and first["steps"]["infrastructure"]["pull_request"] == 10
    assert all(item["ok"] for item in first["steps"]["validating"]["checklist"])
    assert "newapp" in c.config.applications and c.config.applications["newapp"].target.wif_provider.endswith("/newapp-gh/providers/github")
    assert "newapp" in c.applications(brian)  # a `*` identity sees the registered application at once
    assert set(gh.prs[(PLATFORM, 10)]["files"]) == {"infra/gcp/apps/newapp/backend.tf", "infra/gcp/apps/newapp/main.tf", "infra/gcp/apps/newapp/outputs.tf",
                                                    "infra/gcp/apps/newapp/.terraform.lock.hcl"}
    assert c.advance_onboarding("newapp")["state"] == "planning"  # nothing to do yet: the plan has not run
    gh.runs[(PLATFORM, "infra-plan.yml", "d" * 40)] = [{"id": 1, "status": "completed", "conclusion": "success", "url": "u1", "head_sha": "d" * 40}]
    gh.comments[(PLATFORM, 10)] = ["<!-- lab-deploy-infra:newapp -->\n### Infrastructure plan for `newapp`\n\n12 to add.\n\n**Policy: passed.** Merging applies exactly this plan."]
    assert c.advance_onboarding("newapp")["state"] == "applying" and (PLATFORM, 10) in gh.merges
    gh.runs[(PLATFORM, "infra-apply.yml", "9" * 40)] = [{"id": 2, "status": "in_progress", "conclusion": None, "url": "u2", "head_sha": "9" * 40}]
    assert c.advance_onboarding("newapp")["state"] == "applying"
    gh.runs[(PLATFORM, "infra-apply.yml", "9" * 40)][0].update(status="completed", conclusion="success")
    status = c.advance_onboarding("newapp")
    # environment and variables written, the workflows pull request opened, waiting for it to be mergeable
    assert status["state"] == "merging_workflows"
    assert gh.environments[("lab/newapp", "staging")] == "main"
    assert gh.variables[("lab/newapp", "staging")]["WIF_PROVIDER"] == "projects/123456789/locations/global/workloadIdentityPools/newapp-gh/providers/github"
    assert set(gh.variables[("lab/newapp", "staging")]) == {"GCP_PROJECT_ID", "GCP_REGION", "CLOUD_RUN_SERVICE", "ARTIFACT_REPOSITORY", "WIF_PROVIDER",
                                                            "DEPLOY_SERVICE_ACCOUNT", "PROBE_SERVICE_ACCOUNT", "RECOVERY_BUCKET"}
    gh.prs[("lab/newapp", 11)]["mergeable_state"] = "clean"
    done = c.advance_onboarding("newapp")
    assert done["state"] == "complete" and done["plan_id"] and ("lab/newapp", 11) in gh.merges
    assert gh.repos["lab/newapp"]["files"][".github/workflows/deploy-gcp.yml"].count("f" * 40) == 1  # pinned to the platform tip
    owner, plan = c.store.fetch(done["plan_id"])
    assert owner == "onboarding:brian" and plan["state"] == "dispatched"
    assert c.onboarding_status(brian, "newapp")["next_action"].startswith("Onboarding is complete")
    assert c.onboard(brian, "lab/newapp")["state"] == "complete"  # a second sentence changes nothing
    kinds = [e["outcome"] for e in c.store.recent_events(20) if e["kind"] == "onboarding"]
    assert "started" in kinds and "complete" in kinds
    # the registry survives a restart of the controller
    again = Controller(LiveConfig(c.config.current), gh, Store(c.config.database))
    assert "newapp" in again.config.applications


def test_contract_violations_block_with_a_checklist(tmp_path):
    c, brian, gh = make(tmp_path)
    gh.repos["lab/newapp"]["files"].pop("Dockerfile")
    gh.repos["lab/newapp"]["files"]["platform.yaml"] = GOOD_SPEC.replace("reviewed: true", "reviewed: false")
    result = c.onboard(brian, "lab/newapp")
    assert result["state"] == "blocked" and result["blocked_at"] == "validating"
    failed = [item["item"] for item in result["steps"]["validating"]["checklist"] if not item["ok"]]
    assert failed == ["Dockerfile", "platform.yaml says reviewed: true"]
    assert "docs/DEVELOPER-CONTRACT.md" in result["error"]
    assert "newapp" not in c.config.applications  # nothing registered before the contract holds
    gh.repos["lab/newapp"]["files"]["Dockerfile"] = DOCKERFILE
    gh.repos["lab/newapp"]["files"]["platform.yaml"] = GOOD_SPEC
    assert c.onboard(brian, "lab/newapp")["state"] == "planning"  # the same sentence continues after the fix


def test_a_refused_plan_blocks_and_nothing_is_merged(tmp_path):
    c, brian, gh = make(tmp_path)
    c.onboard(brian, "lab/newapp")
    gh.runs[(PLATFORM, "infra-plan.yml", "d" * 40)] = [{"id": 1, "status": "completed", "conclusion": "success", "url": "u1", "head_sha": "d" * 40}]
    gh.comments[(PLATFORM, 10)] = ["<!-- lab-deploy-infra:newapp -->\n**Policy: blocked.**\n- `x`: no public access (no_public_access)"]
    result = c.advance_onboarding("newapp")
    assert result["state"] == "blocked" and "no_public_access" in result["error"] and gh.merges == []
    assert c.onboarding_tick() == {}  # a blocked record is not retried on its own


def test_onboarding_needs_the_block_and_a_star_identity(tmp_path):
    c, brian, _ = make(tmp_path, onboarding=False)
    with pytest.raises(ValueError, match="onboarding"):
        c.onboard(brian, "lab/newapp")
    c, narrow, _ = make(tmp_path / "b", principal_apps=("demo",))
    with pytest.raises(ValueError, match=r"applications: \['\*'\]"):
        c.onboard(narrow, "lab/newapp")
    assert c.visible_apps(narrow) == ["demo"]
    with pytest.raises(ValueError):
        Config.model_validate({"applications": {}, "principals": [{"name": "x", "token_sha256": "0" * 64, "applications": ["ghost"]}]})


def test_a_refused_write_is_explained_in_plain_words(tmp_path):
    import httpx
    c, brian, gh = make(tmp_path)

    def refused(*args, **kwargs):
        request = httpx.Request("POST", "https://api.github.com/repos/lab/platform/git/refs")
        raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
    gh.open_platform_pull_request = refused
    result = c.onboard(brian, "lab/newapp")
    assert result["state"] == "blocked" and result["blocked_at"] == "infrastructure"
    assert "GitHub refused access for lab/platform" in result["error"] and "section 7g" in result["error"]
