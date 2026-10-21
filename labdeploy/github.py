"""GitHub App credentials stay server-side. No cloud credentials in this process."""
import base64
import datetime
import io
import json
import os
import re
import threading
import time
import types
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from .bake import DEFAULT_BAKE_SECONDS

TOKEN_WARNING_DAYS = 14


def needed_permission(request):
    """The fine-grained token permission a refused request needs, read from its method and path."""
    method, path = request.method, request.url.path
    if "/environments/" in path and "/variables" in path:
        return "Environments: read and write"
    if "/environments/" in path:
        return "Administration: read and write (it creates the deployment environment)"
    if "/actions/variables" in path:
        return "Variables: read and write"
    if path.endswith("/dispatches") or ("/actions/runs/" in path and method != "GET"):
        return "Actions: read and write"
    if "/contents/.github/workflows/" in path and method != "GET":
        return "Workflows: read and write"
    if "/pulls" in path and method != "GET":
        return "Pull requests: read and write"
    if "/issues" in path and method != "GET":
        # The daily digest's issue (docs/ROADMAP.md D60); a pull request's comment goes through the same path.
        return "Issues: read and write (for a pull request's comment, Pull requests: read and write)"
    if method != "GET" and ("/contents/" in path or "/git/" in path):
        return "Contents: read and write"
    return "Contents: read and Actions: read"


def explain(error, repository=None):
    """A plain sentence for a GitHub failure, or None when the error is not a GitHub transport error."""
    where = f" for {repository}" if repository else ""
    if isinstance(error, httpx.HTTPStatusError):
        status, headers = error.response.status_code, error.response.headers
        if status == 401:
            return ("GitHub rejected the controller's credential: a fine-grained token that is expired or revoked (renew it on the controller "
                    "host and restart), or a GitHub App key GitHub no longer accepts (add the new key as the secret's latest version).")
        if status == 403 and headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            when = datetime.datetime.fromtimestamp(int(reset), datetime.UTC).strftime("%H:%M UTC") if reset and reset.isdigit() else "shortly"
            return f"GitHub API rate limit reached; it resets at {when}. Retry then."
        if status == 403:
            return f"GitHub refused access{where}: the credential needs the permission {needed_permission(error.request)} for this request."
        if status == 404:
            return f"GitHub could not find the repository, branch or file{where}; check the registry entry and that the credential can read that repository."
        if status == 422:
            try:
                detail = error.response.json().get("message", "")
            except ValueError:
                detail = ""
            hint = " If this was a workflow dispatch, the app's workflow files may pin an older platform; regenerate them." if "input" in detail.lower() or "workflow" in detail.lower() else ""
            return f"GitHub refused the request{where}: {detail or 'unprocessable'}.{hint}"
        if status >= 500:
            return f"GitHub is unavailable right now (HTTP {status}); retry in a few minutes."
        return f"GitHub answered HTTP {status}{where}."
    if isinstance(error, httpx.TimeoutException):
        return "GitHub did not answer in time; retry in a moment."
    if isinstance(error, httpx.RequestError):
        return f"Could not reach GitHub ({type(error).__name__}); check the controller's network connection."
    return None


def parse_expiration(header):
    """GitHub sends 'YYYY-MM-DD HH:MM:SS UTC' for fine-grained tokens."""
    text = (header or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)
    except ValueError:
        return None



CHECKS_GRACE_SECONDS = 120  # GitHub can take a minute or two to start a pull request's workflows
FINE = ("success", "skipped", "neutral")


def checks_verdict(runs, opened_at=None, now=None):
    """("passed" | "wait" | "failed", why) for a pull request the controller would merge itself: every GitHub Actions run
    on its head must have finished and none may have failed. A repository without required checks lets GitHub call such
    a pull request mergeable while its tests still run (found live: lab-bare#6 merged 12 seconds before its tests ended),
    so the controller asks the runs itself. With no run at all it waits until two minutes after the pull request opened
    (GitHub starts them a little later), then treats the repository as one without checks."""
    pending = [run for run in runs if run.get("status") != "completed"]
    if pending:
        return "wait", f"its checks to finish ({', '.join(sorted({str(run.get('name')) for run in pending}))})"
    failed = [run for run in runs if run.get("conclusion") not in FINE]
    if failed:
        return "failed", f"{failed[0].get('name')} ended {failed[0].get('conclusion')} ({failed[0].get('url')})"
    if not runs and opened_at:
        opened = parse_expiration(opened_at)
        if opened is not None and (now or time.time()) - opened.timestamp() < CHECKS_GRACE_SECONDS:
            return "wait", "GitHub to start its checks"
    return "passed", None


def pipeline_of(app):
    """The repository whose workflows release an application: the platform's for a contract it keeps, else the app's own."""
    return getattr(app, "pipeline_repo", None) or app.repository


def central_of(app):
    """Whether the platform repository's own pipeline releases this application (docs/ROADMAP.md D33)."""
    return bool(getattr(app, "central", False))

REPOSITORY_PATH = re.compile(r"^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/|$)")
TOKEN_MARGIN_SECONDS = 300  # an installation token is renewed five minutes before GitHub ends it (it lives an hour)
INSTALLATION_SECONDS = 3600  # how long the installation that covers a repository is remembered
NOT_INSTALLED_SECONDS = 600  # a repository without the App is asked about again after ten minutes: it may be installed meanwhile


def headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}


class StaticToken:
    """A fine-grained personal access token (GITHUB_TOKEN): the laptop's credential, and its fallback once the machine runs."""
    kind = "fine_grained_token"

    def __init__(self, value):
        self.value = value

    def token(self, repository=None, method="GET"):
        return self.value


class AppInstallations:
    """GitHub App installation tokens, one per repository (docs/ROADMAP.md D14, 15.1).

    The App signs a ten-minute JWT with its private key, asks GitHub which installation covers a repository, and exchanges
    the JWT for an installation token limited to that one repository, with the permissions the App was created with
    (exactly what onboarding needs, docs/SETUP.md section 4). Tokens are kept until five minutes before GitHub ends them.
    A repository the App is not installed on is read, public data only, with the home repository's token; any other call
    there is refused with what to install. `private_key(refresh)` returns the key's PEM text; it is kept in memory only and
    read again once when GitHub refuses a signature, so a rotated key takes effect without a restart.
    """
    kind = "github_app"

    def __init__(self, app_id, private_key, client, home_repository=None, installation_id=None, clock=time.time):
        self.app_id, self.private_key, self.client = str(app_id), private_key, client
        self.home_repository, self.installation_id, self.clock = home_repository, installation_id, clock
        self.installations, self.tokens = {}, {}
        self.lock = threading.RLock()  # token() reads the home repository's token from inside itself

    def assertion(self, refresh=False):
        now = int(self.clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key(refresh), algorithm="RS256")

    def signed(self, method, path, **kwargs):
        """A request as the App itself. A refused signature (401) reads the key again and retries once: a rotation."""
        response = self.client.request(method, path, headers=headers(self.assertion()), **kwargs)
        if response.status_code == 401:
            response = self.client.request(method, path, headers=headers(self.assertion(refresh=True)), **kwargs)
        return response

    def installation(self, repository):
        """The installation id that covers a repository, or None when the App is not installed on it."""
        if self.installation_id:
            return int(self.installation_id)
        key, now = repository.lower(), self.clock()
        known = self.installations.get(key)
        if known and known[1] > now:
            return known[0]
        response = self.signed("GET", f"/repos/{repository}/installation")
        if response.status_code == 404:
            self.installations[key] = (None, now + NOT_INSTALLED_SECONDS)
            return None
        response.raise_for_status()
        found = int(response.json()["id"])
        self.installations[key] = (found, now + INSTALLATION_SECONDS)
        return found

    def token(self, repository=None, method="GET"):
        repository = repository or self.home_repository
        if not repository:
            raise ValueError("The GitHub App needs platform.repository in the controller configuration for calls that name no repository")
        with self.lock:
            installation = self.installation(repository)
            if installation is None:
                home = self.home_repository
                if method == "GET" and home and home.lower() != repository.lower():
                    return self.token(home, method)  # public data reads with any token
                raise ValueError(f"The Fast Deploy GitHub App is not installed on {repository}. Install it there (GitHub: the App's page, "
                                 "Configure, Repository access) and try again.")
            key, now = (installation, repository.lower()), self.clock()
            known = self.tokens.get(key)
            if known and known[1] > now:
                return known[0]
            response = self.signed("POST", f"/app/installations/{installation}/access_tokens", json={"repositories": [repository.split("/", 1)[1]]})
            response.raise_for_status()
            data = response.json()
            expires = parse_expiration(data.get("expires_at"))
            until = (expires.timestamp() if expires else now + 3600) - TOKEN_MARGIN_SECONDS
            self.tokens[key] = (data["token"], until)
            return data["token"]


def environment_credentials():
    """A fine-grained GITHUB_TOKEN wins, then an App from GITHUB_APP_ID, GITHUB_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY_FILE
    (a container with a mounted key), else None."""
    if os.getenv("GITHUB_TOKEN"):
        return StaticToken(os.environ["GITHUB_TOKEN"])
    app_id, installation_id, key_path = os.getenv("GITHUB_APP_ID"), os.getenv("GITHUB_INSTALLATION_ID"), os.getenv("GITHUB_APP_PRIVATE_KEY_FILE")
    if app_id and installation_id and installation_id.isdigit() and key_path:
        return AppInstallations(app_id, lambda refresh=False: Path(key_path).read_text(), github_client(), installation_id=installation_id)
    return None


def github_client():
    return httpx.Client(base_url="https://api.github.com", timeout=30, follow_redirects=False)


class GitHub:
    def __init__(self, client=None, credentials=None):
        self.client = client or github_client()
        self.app_credentials = credentials  # the configured GitHub App; GITHUB_TOKEN in the environment still wins
        self.environment = None

    @classmethod
    def from_config(cls, config, client=None):
        """The client for a controller configuration: its GitHub App when `github_app` is set (the machine), else whatever the
        environment provides (the laptop's fine-grained token)."""
        client = client or github_client()
        app = getattr(config, "github_app", None)
        if app is None:
            return cls(client)
        from .cloud import GoogleToken, secret_reader
        if app.private_key_secret:
            key = secret_reader(app.private_key_secret, GoogleToken())
        else:
            path = app.private_key_file
            key = lambda refresh=False: Path(path).read_text()  # noqa: E731 - a one-line key source
        platform = getattr(config, "platform", None)
        return cls(client, AppInstallations(app.app_id, key, client, home_repository=platform.repository if platform else None))

    def credentials(self):
        if os.getenv("GITHUB_TOKEN"):
            return StaticToken(os.environ["GITHUB_TOKEN"])
        if self.app_credentials is not None:
            return self.app_credentials
        if self.environment is None:
            self.environment = environment_credentials()
        if self.environment is None:
            raise ValueError("No GitHub credential: set github_app in the controller configuration (docs/SETUP.md section 4) or a fine-grained "
                             "GITHUB_TOKEN on the controller host")
        return self.environment

    def token(self, path=None, method="GET"):
        """The credential for one call: on a GitHub App, the installation token of the repository the path names."""
        match = REPOSITORY_PATH.match(path or "")
        return self.credentials().token(f"{match.group(1)}/{match.group(2)}" if match else None, method)

    @staticmethod
    def headers(token):
        return headers(token)

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, headers=self.headers(self.token(path, method)), **kwargs)
        response.raise_for_status()
        return response

    def head(self, app):
        return self.commit(app)["sha"]

    def commit(self, app, ref=None):
        """The commit at a ref with the facts people recognize it by: first line of the message, author, date."""
        data = self.request("GET", f"/repos/{app.repository}/commits/{quote(ref or app.branch, safe='')}").json()
        details = data.get("commit") or {}
        message = (details.get("message") or "").strip().splitlines()
        return {"sha": data["sha"], "message": message[0][:200] if message else "", "author": (details.get("author") or {}).get("name"),
                "date": (details.get("author") or {}).get("date"), "url": data.get("html_url")}

    def compare(self, app, base_sha, head=None):
        """Commits and files between base_sha and the branch (or another ref): how far main has moved past the live release."""
        return self.compare_repository(app.repository, base_sha, head or app.branch)

    def compare_repository(self, repository, base, head):
        data = self.request("GET", f"/repos/{repository}/compare/{base}...{quote(head, safe='')}").json()
        commits = [{"sha": c.get("sha"), "message": ((c.get("commit") or {}).get("message") or "").strip().splitlines()[0][:200] if c.get("commit") else ""}
                   for c in data.get("commits", [])]
        files = [{"path": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                 for f in data.get("files", [])][:50]
        return {"status": data.get("status"), "ahead_by": data.get("ahead_by", 0), "behind_by": data.get("behind_by", 0), "commits": commits[-10:], "files": files}

    def changed_files(self, repository, base, head):
        """The paths a comparison touches (both names of a rename), and whether that list is complete: GitHub lists at most
        300 files, and a base that is not an ancestor (a rewritten branch) says nothing reliable."""
        data = self.request("GET", f"/repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}").json()
        listed = data.get("files", [])
        files = sorted({name for f in listed for name in (f.get("filename"), f.get("previous_filename")) if name})
        return files, data.get("status") in ("ahead", "identical") and len(listed) < 300

    def comparison(self, repository, base, head):
        """How `head` stands to `base` (ahead, behind, identical or diverged) and the paths between them; `complete` is false
        when GitHub listed its maximum of 300 files, so a caller cannot rule a path out."""
        data = self.request("GET", f"/repos/{repository}/compare/{quote(base, safe='')}...{quote(head, safe='')}").json()
        listed = data.get("files", [])
        files = sorted({name for f in listed for name in (f.get("filename"), f.get("previous_filename")) if name})
        return {"status": data.get("status"), "files": files, "complete": len(listed) < 300}

    def push_runs(self, repository, workflow, sha):
        """The runs of one workflow that a push of exactly this commit started (a merge into main starts one; scheduled runs
        on the same commit are left out), newest first, with when each was last updated."""
        params = {"head_sha": sha, "event": "push", "per_page": 10}
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{quote(workflow, safe='')}/runs", params=params).json().get("workflow_runs", [])
        return [{"id": run["id"], "status": run.get("status"), "conclusion": run.get("conclusion"), "url": run.get("html_url"),
                 "updated_at": run.get("updated_at"), "head_sha": run.get("head_sha")} for run in runs if run.get("head_sha") == sha]

    def schedule_health(self, app, workflow):
        """Whether GitHub still runs a scheduled workflow and when it last did; GitHub pauses schedules in idle repositories."""
        state = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}").json().get("state")
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs", params={"event": "schedule", "per_page": 1}).json()["workflow_runs"]
        last = runs[0] if runs else None
        return {"workflow": workflow, "state": state, "last_scheduled_run": last and last.get("created_at"), "last_conclusion": last and last.get("conclusion")}

    def cancel_run(self, app, run_id):
        """Ask GitHub to cancel a run; the recovery workflow handles whatever the runner had started."""
        self.request("POST", f"/repos/{pipeline_of(app)}/actions/runs/{int(run_id)}/cancel")
        return {"cancel_requested": True, "workflow_run_id": int(run_id)}

    def dispatch_workflow(self, app, workflow, inputs):
        """Start one of the application's workflows: its own, or the platform repository's for an application whose contract
        the platform keeps, which also learn the service they act for."""
        if central_of(app):
            inputs = {**inputs, "application": app.service}
        response = self.request("POST", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/dispatches",
                                json={"ref": "main" if central_of(app) else app.branch, "inputs": inputs})
        return response.json() if response.content else {}

    def artifact_json(self, app, run_id, name, filenames, limit=1_048_576):
        """Parsed JSON files from one named artifact of a run, or None when the run has no such artifact."""
        artifacts = self.request("GET", f"/repos/{pipeline_of(app)}/actions/runs/{int(run_id)}/artifacts", params={"per_page": 100}).json()["artifacts"]
        artifact = next((a for a in artifacts if a.get("name") == name and not a.get("expired")), None)
        if artifact is None:
            return None
        if artifact.get("size_in_bytes", 0) > limit:
            raise ValueError(f"Artifact {name} exceeds 1 MiB")
        path = f"/repos/{pipeline_of(app)}/actions/artifacts/{int(artifact['id'])}/zip"
        response = self.client.request("GET", path, headers=self.headers(self.token(path)))
        if response.status_code == 302:
            # The signed download URL belongs to another host; the GitHub credential is not forwarded.
            response = self.client.get(response.headers["location"])
        response.raise_for_status()
        found = {}
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for filename in filenames:
                try:
                    info = archive.getinfo(filename)
                except KeyError:
                    continue
                if info.file_size > limit:
                    raise ValueError(f"{filename} exceeds 1 MiB")
                found[filename] = json.loads(archive.read(info))
        return found

    def release_receipt(self, app, run_id):
        """The deploy job's receipt and vulnerability summary for a finished run, or None when nothing was uploaded."""
        files = self.artifact_json(app, run_id, "release-receipt", ("release-receipt.json", "vulnerability-summary.json"))
        if not files:
            return None
        return {"release": files.get("release-receipt.json"), "vulnerabilities": files.get("vulnerability-summary.json")}

    def recovery_after(self, app, run, workflow="recover-gcp.yml", name="recovery-receipt"):
        """The newest recovery run started after a deploy run, with its receipt when uploaded; None while nothing has run yet.
        The platform repository's recovery uploads one receipt per service, named recovery-receipt-<service>."""
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs", params={"branch": "main", "per_page": 10}).json()["workflow_runs"]
        since = run.get("created_at") or ""
        candidates = [r for r in runs if (r.get("created_at") or "") >= since and r.get("id") != run.get("id")]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r.get("created_at") or "")
        result = {"run_id": latest.get("id"), "status": latest.get("status"), "conclusion": latest.get("conclusion"), "html_url": latest.get("html_url"),
                  "created_at": latest.get("created_at"), "receipt": None}
        if latest.get("status") == "completed":
            files = self.artifact_json(app, latest["id"], name, ("recovery-receipt.json",))
            result["receipt"] = (files or {}).get("recovery-receipt.json")
        return result

    def credential_status(self, now=None):
        """What the controller signs in to GitHub with, and when that ends: a fine-grained token's expiry from GitHub's own
        response header, warned two weeks ahead; a GitHub App never expires (its installation tokens renew every hour), so
        reaching GitHub with one is the check."""
        credentials = self.credentials()
        response = self.request("GET", "/rate_limit")
        kind = credentials.kind
        if kind == "github_app":
            return {"kind": kind, "app_id": credentials.app_id, "expires_at": None, "days_left": None, "warning": None}
        expires = parse_expiration(response.headers.get("github-authentication-token-expiration"))
        if expires is None:
            return {"kind": kind, "expires_at": None, "days_left": None, "warning": None}
        days = (expires - (now or datetime.datetime.now(datetime.UTC))).days
        warning = None
        if days < 0:
            warning = "The GitHub credential has expired; every GitHub call will fail until it is renewed."
        elif days < TOKEN_WARNING_DAYS:
            warning = f"The GitHub credential expires in {days} day(s); create a new one and update the controller host before then."
        return {"kind": kind, "expires_at": expires.isoformat(timespec="seconds"), "days_left": days, "warning": warning}

    def file(self, app, sha, filename):
        response = self.request("GET", f"/repos/{app.repository}/contents/{quote(filename, safe='/')}", params={"ref": sha})
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > 65_536:
            raise ValueError("Repository input exceeds 64 KiB")
        return base64.b64decode(data["content"], validate=False).decode("utf-8")

    def contents(self, repository, ref, path, limit=65_536):
        """A regular file from any repository the credential can read, at a ref; None when it does not exist. Lock files may be
        read up to `limit` (5 MB, docs/ROADMAP.md 20.4): past GitHub's 1 MB contents limit the file comes through the blobs
        endpoint by its sha; every other input stays within 64 KiB."""
        try:
            response = self.request("GET", f"/repos/{repository}/contents/{quote(path, safe='/')}", params={"ref": ref})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") not in ("base64", "none"):
            raise ValueError("Expected regular, base64-encoded GitHub file")
        if data.get("size", 0) > min(limit, 5_000_000):
            raise ValueError(f"Repository input exceeds {min(limit, 5_000_000) // 1024} KiB")
        if data.get("encoding") == "none":
            # Over 1 MB the contents endpoint sends no content; the blob, read by its sha, carries up to 100 MB.
            blob = self.request("GET", f"/repos/{repository}/git/blobs/{data.get('sha')}").json()
            if not isinstance(blob, dict) or blob.get("encoding") != "base64":
                raise ValueError("Expected a base64-encoded GitHub blob")
            data = {**data, "content": blob.get("content") or ""}
        return {"sha": data.get("sha"), "text": base64.b64decode(data["content"], validate=False).decode("utf-8")}

    # ---- onboarding: read a repository's shape, follow its pull requests and runs, prepare its environment
    def repository(self, repository):
        data = self.request("GET", f"/repos/{repository}").json()
        return {"repository_id": str(data["id"]), "owner_id": str(data["owner"]["id"]), "default_branch": data.get("default_branch"),
                "private": bool(data.get("private")), "url": data.get("html_url"), "full_name": data.get("full_name")}

    def tree(self, repository, ref, sizes=False):
        """Every path in the repository at a ref (files and directories), for the contract's checks; with `sizes`, a mapping
        of each file to its size in bytes as well, for the reader's refusal of files over 100 MiB."""
        data = self.request("GET", f"/repos/{repository}/git/trees/{quote(ref, safe='')}", params={"recursive": "1"}).json()
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") in ("blob", "tree")]
        if not sizes:
            return paths
        return paths, {item["path"]: int(item.get("size") or 0) for item in data.get("tree", []) if item.get("type") == "blob"}

    def pull_request(self, repository, number):
        data = self.request("GET", f"/repos/{repository}/pulls/{number}").json()
        return {"number": number, "state": data["state"], "merged": bool(data.get("merged")), "mergeable_state": data.get("mergeable_state"),
                "head_sha": data["head"]["sha"], "merge_commit_sha": data.get("merge_commit_sha"), "url": data["html_url"],
                "merged_by": (data.get("merged_by") or {}).get("login"), "head_ref": data["head"].get("ref"), "base_sha": data["base"].get("sha"),
                "created_at": data.get("created_at")}

    def head_checks(self, repository, sha):
        """Every GitHub Actions run on a commit, a pull request's head: what the controller waits for before it merges its own
        pull request (checks_verdict). Read with the Actions permission; check runs would need one more for the App."""
        runs = self.request("GET", f"/repos/{repository}/actions/runs", params={"head_sha": sha, "per_page": 100}).json().get("workflow_runs", [])
        return [{"name": run.get("name"), "status": run.get("status"), "conclusion": run.get("conclusion"), "url": run.get("html_url")} for run in runs]

    def open_pull_requests(self, repository, prefix):
        """The open pull requests whose branch starts with `prefix`: the controller's own, found by its branch names."""
        pulls = self.request("GET", f"/repos/{repository}/pulls", params={"state": "open", "per_page": 100}).json()
        return [{"number": item["number"], "url": item["html_url"], "branch": item["head"]["ref"], "head_sha": item["head"]["sha"]}
                for item in pulls if str(item["head"].get("ref", "")).startswith(prefix)]

    def close_pull_request(self, repository, number, comment):
        """Close a pull request with a comment saying why; it can be reopened, and its branch is kept."""
        self.request("POST", f"/repos/{repository}/issues/{int(number)}/comments", json={"body": comment})
        self.request("PATCH", f"/repos/{repository}/pulls/{int(number)}", json={"state": "closed"})
        return {"number": number, "closed": True}

    def pull_request_files(self, repository, number):
        return [item["filename"] for item in self.request("GET", f"/repos/{repository}/pulls/{number}/files", params={"per_page": 100}).json()]

    def merge_pull_request(self, repository, number, title, sha=None):
        """Squash-merge; a refusal (checks pending, protection, conflicts, a head other than `sha`) is reported, never forced."""
        body = {"merge_method": "squash", "commit_title": title, **({"sha": sha} if sha else {})}
        try:
            data = self.request("PUT", f"/repos/{repository}/pulls/{number}/merge", json=body).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (405, 409, 422):
                try:
                    reason = exc.response.json().get("message", "")
                except ValueError:
                    reason = exc.response.text[:200]
                return {"merged": False, "sha": None, "reason": reason}
            raise
        return {"merged": bool(data.get("merged")), "sha": data.get("sha"), "reason": data.get("message")}

    def issue_comments(self, repository, number):
        return [item.get("body", "") for item in self.request("GET", f"/repos/{repository}/issues/{number}/comments", params={"per_page": 100}).json()]

    def find_issue(self, repository, title):
        """The newest open issue (not a pull request) with exactly this title: {number, url, updated_at}, or None."""
        for item in self.request("GET", f"/repos/{repository}/issues", params={"state": "open", "per_page": 100, "sort": "created", "direction": "desc"}).json():
            if item.get("title") == title and "pull_request" not in item:
                return {"number": item["number"], "url": item.get("html_url"), "updated_at": item.get("updated_at")}
        return None

    def create_issue(self, repository, title, body):
        item = self.request("POST", f"/repos/{repository}/issues", json={"title": title, "body": body}).json()
        return {"number": item["number"], "url": item.get("html_url")}

    def comment_issue(self, repository, number, body):
        """A comment on an issue; GitHub tells the issue's watchers, which is how the digest reaches a person by email."""
        item = self.request("POST", f"/repos/{repository}/issues/{int(number)}/comments", json={"body": body}).json()
        return {"url": item.get("html_url")}

    def recent_runs(self, repository, workflow, limit=20):
        """The newest completed runs of a workflow, on any commit."""
        params = {"per_page": limit, "status": "completed"}
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs", params=params).json().get("workflow_runs", [])
        return [{"id": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "url": run["html_url"], "head_sha": run["head_sha"]} for run in runs]

    def job_conclusions(self, repository, run_id):
        """Each job of a run's latest attempt by name, with its conclusion."""
        jobs = self.request("GET", f"/repos/{repository}/actions/runs/{int(run_id)}/jobs", params={"per_page": 100}).json().get("jobs", [])
        return {job["name"]: job.get("conclusion") for job in jobs}

    def contains(self, repository, ancestor, ref):
        """Whether `ref` already contains commit `ancestor` (the same commit, or one after it)."""
        data = self.request("GET", f"/repos/{repository}/compare/{quote(ancestor, safe='')}...{quote(ref, safe='')}").json()
        return data.get("status") in ("ahead", "identical")

    def workflow_runs(self, repository, workflow, head_sha=None, limit=5):
        params = {"per_page": limit}
        if head_sha:
            params["head_sha"] = head_sha
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs", params=params).json().get("workflow_runs", [])
        return [{"id": run["id"], "status": run["status"], "conclusion": run.get("conclusion"), "url": run["html_url"], "head_sha": run["head_sha"]} for run in runs]

    def ensure_environment(self, repository, name, branch):
        """The deployment environment the workflows run in, restricted to one branch; idempotent."""
        path = f"/repos/{repository}/environments/{quote(name, safe='')}"
        self.request("PUT", path, json={"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
        try:
            self.request("POST", path + "/deployment-branch-policies", json={"name": branch, "type": "branch"})
        except httpx.HTTPStatusError as exc:
            # The policy for this branch already exists: GitHub answers 303 See Other (pointing at it), and once answered 422.
            # A repository onboarded again (Sophia's copy from backend/, 2026-10-16) meets it.
            if exc.response.status_code not in (303, 422):
                raise

    def user_id(self, login):
        """A GitHub user's numeric id, which an environment's required reviewers are named by."""
        return int(self.request("GET", f"/users/{quote(login, safe='')}").json()["id"])

    def ensure_protected_environment(self, repository, name, branch, reviewers):
        """A production environment (docs/ROADMAP.md D37): restricted to one branch, every job in it waits for one of
        `reviewers` (GitHub user names) to approve, whoever started the run may not approve it, and no administrator may
        skip the approval (GitHub's default lets them; found live on 2026-10-18). The controller starts promotions as the
        GitHub App, so its operator approving one is not a self-review. Idempotent."""
        path = f"/repos/{repository}/environments/{quote(name, safe='')}"
        self.request("PUT", path, json={"wait_timer": 0, "prevent_self_review": True, "can_admins_bypass": False,
                                        "reviewers": [{"type": "User", "id": self.user_id(login)} for login in reviewers],
                                        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
        try:
            self.request("POST", path + "/deployment-branch-policies", json={"name": branch, "type": "branch"})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (303, 422):  # the policy for this branch exists already
                raise

    def set_environment_variables(self, repository, name, variables):
        path = f"/repos/{repository}/environments/{quote(name, safe='')}/variables"
        for key, value in variables.items():
            try:
                self.request("POST", path, json={"name": key, "value": value})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                self.request("PATCH", f"{path}/{key}", json={"name": key, "value": value})

    def repository_ids(self, repository):
        """The immutable numeric ids GitHub's OIDC tokens carry; Workload Identity Federation trusts these, not names."""
        data = self.request("GET", f"/repos/{repository}").json()
        return {"repository_id": str(data["id"]), "owner_id": str(data["owner"]["id"])}

    def open_platform_pull_request(self, repository, base_branch, branch, files, title, body, message):
        """A pull request on the platform repository itself (infrastructure roots), with the same no-merge rules."""
        return self.open_pull_request(types.SimpleNamespace(repository=repository, branch=base_branch), branch, files, title, body, message)

    def branch_head(self, repository, branch="main"):
        return self.request("GET", f"/repos/{repository}/commits/{quote(branch, safe='')}").json()["sha"]

    def open_pull_request(self, app, branch, files, title, body, message, draft=False, create_only=False):
        """Open one pull request with the given files on a branch; never merges, never force-pushes, reuses an open one.
        `create_only`: a file that already exists on the branch is left alone (and named), never overwritten."""
        owner = app.repository.split("/")[0]
        open_prs = self.request("GET", f"/repos/{app.repository}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}).json()
        if open_prs:
            return {"number": open_prs[0]["number"], "url": open_prs[0]["html_url"], "branch": branch, "changed": [],
                    "note": "A pull request on this branch is already open and was left as it is (it may hold a person's edits, and it may predate "
                            "this request); close it to have the platform open a fresh one."}
        base = self.head(app)
        if not self.fresh_branch(app.repository, branch, base):
            # The branch is left over from an earlier pull request (after a squash merge its history no longer matches the
            # base): a pull request from it would conflict, and GitHub runs no workflow for a conflicting one. Use a new
            # branch named after the base commit; nothing is deleted or force-pushed.
            branch = f"{branch}-{base[:7]}"
            if not self.fresh_branch(app.repository, branch, base):
                raise ValueError(f"Branches {branch} already exist and differ from {app.branch}; delete the old ones and try again")
        changed, kept = [], []
        for path, text in files.items():
            current = self.contents(app.repository, branch, path)
            if current is not None and current["text"] == text:
                continue
            if current is not None and create_only:
                kept.append(path)
                continue
            payload = {"message": message, "branch": branch, "content": base64.b64encode(text.encode()).decode()}
            if current is not None:
                payload["sha"] = current["sha"]
            self.request("PUT", f"/repos/{app.repository}/contents/{quote(path, safe='/')}", json=payload)
            changed.append(path)
        if not changed:
            return {"number": None, "url": None, "branch": branch, "changed": [], "kept": kept, "note": "The files already have this content."}
        response = self.request("POST", f"/repos/{app.repository}/pulls", json={"title": title, "base": app.branch, "head": branch, "draft": draft, "body": body})
        result = {"number": response.json()["number"], "url": response.json()["html_url"], "branch": branch, "changed": changed}
        if kept:
            result["kept"] = kept
        return result

    def fresh_branch(self, repository, branch, base):
        """Create the branch at `base`, or accept an existing one only when it is exactly `base`; False when it has drifted."""
        try:
            self.request("POST", f"/repos/{repository}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base})
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:  # 422: the branch already exists
                raise
        return self.compare_repository(repository, base, branch)["status"] == "identical"

    def update_workflows(self, app, platform_sha, files):
        """Open the pull request that pins the app's workflow files to a platform commit; never merges, never force-pushes."""
        body = (f"Regenerated from the platform templates at commit `{platform_sha}`.\n\nFiles: " + ", ".join(f"`{p}`" for p in files)
                + "\n\nReview and merge through the normal process; the controller refuses to deploy an app whose pin it does not support. "
                "Nothing is merged or deployed by this pull request itself.")
        result = self.open_pull_request(app, f"lab-deploy/platform-{platform_sha[:12]}{getattr(app, 'suffix', '')}", files,
                                        f"Update Fast Deploy workflows to platform {platform_sha[:12]}" + (f" ({app.service})" if getattr(app, "suffix", "") else ""), body,
                                        f"Pin Fast Deploy workflows to platform {platform_sha[:12]}")
        if result.get("note") == "The files already have this content.":
            result["note"] = "The workflows already match this platform commit."
        elif result.get("note"):
            result["note"] = "A pull request for this platform commit is already open."
        return result

    def dispatch(self, app, plan):
        inputs = {"commit_sha": plan["sha"], "spec_hash": plan["spec_hash"], "plan_id": plan["id"], "target_json": json.dumps(plan["target"], sort_keys=True)}
        if plan.get("reuse_image"):
            inputs["image_digest"] = plan["reuse_image"]
        if plan.get("provenance") == "unavailable":
            # Sent only for the exception: a workflow pinned before 1.8.6 has no such input and GitHub refuses unknown ones.
            inputs["provenance"] = "unavailable"
        if plan.get("bake_seconds", DEFAULT_BAKE_SECONDS) != DEFAULT_BAKE_SECONDS:
            # Sent only when the operator changed it, for the same reason: a workflow pinned before 1.9.0 has no such input.
            inputs["bake_seconds"] = str(int(plan["bake_seconds"]))
        if plan.get("base"):
            # The application the jobs build on (docs/ROADMAP.md D69), sent only then: a workflow pinned before 1.31.0 has no such input.
            inputs["base"] = json.dumps(plan["base"], sort_keys=True)
        if central_of(app):
            # The platform repository's pipeline (docs/ROADMAP.md D33): which application, where its source is, and the
            # platform commit whose apps/<id>/platform.yaml the plan was made from.
            inputs.update(application=app.service, repository=app.repository, app_path=app.path, contract_commit=(plan.get("contract") or {})["commit"])
        response = self.request("POST", f"/repos/{pipeline_of(app)}/actions/workflows/{app.workflow}/dispatches", json={"ref": "main" if central_of(app) else app.branch, "inputs": inputs})
        # Current API returns 200 with run ID; older deployments may still return 204.
        return response.json() if response.content else {}

    def dispatch_promotion(self, repository, plan):
        """Start the platform repository's promote.yml for a promotion plan (docs/ROADMAP.md D37): the staging release, its
        image by digest and how it was built and signed, and the production target the environment must match."""
        source, contract = plan["source"], plan.get("contract") or {}
        inputs = {"plan_id": plan["id"], "application": plan["target"]["service"], "source_plan": source["plan"], "source_image": source["image"],
                  "commit_sha": plan["sha"], "spec_hash": plan["spec_hash"], "target_json": json.dumps(plan["target"], sort_keys=True),
                  "repository": plan["repository"], "app_path": (plan.get("layout") or {}).get("path") or "", "contract_commit": contract.get("commit", ""),
                  "staging_workflow": source["workflow"], "provenance": source["provenance"], "bake_seconds": str(int(plan["bake_seconds"]))}
        if plan.get("rollout"):
            inputs["rollout"] = ",".join(str(int(step)) for step in plan["rollout"])  # the split release's steps (D38)
        if source.get("attested_commit") and source["attested_commit"] != plan["sha"]:
            # GitHub's attestation names the commit its build run started on (docs/CHANGES.md 1.18.2); sent only when it
            # differs, so a promotion of a release built at the tip of main stays as it was.
            inputs["attested_commit"] = source["attested_commit"]
        response = self.request("POST", f"/repos/{repository}/actions/workflows/promote.yml/dispatches", json={"ref": "main", "inputs": inputs})
        return response.json() if response.content else {}

    def dispatch_production_rollback(self, repository, record):
        """Start promote.yml's rollback for a production rollback record: the promotion it undoes and the split coming back."""
        inputs = {"operation": "rollback", "plan_id": record["id"], "application": record["target"]["service"],
                  "target_json": json.dumps(record["target"], sort_keys=True), "rollback_plan": record["from_plan"],
                  "rollback_to": json.dumps(record["to_traffic"], sort_keys=True)}
        response = self.request("POST", f"/repos/{repository}/actions/workflows/promote.yml/dispatches", json={"ref": "main", "inputs": inputs})
        return response.json() if response.content else {}

    def deployment_environment(self, repository, name):
        """A deployment environment with its protection rules, or None when the repository has none of that name."""
        try:
            return self.request("GET", f"/repos/{repository}/environments/{quote(name, safe='')}").json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def last_commit(self, repository, path, branch="main"):
        """The newest commit on a branch that changed a file, or None when none did."""
        commits = self.request("GET", f"/repos/{repository}/commits", params={"sha": branch, "path": path, "per_page": 1}).json()
        return commits[0]["sha"] if commits else None

    def create_tag(self, repository, name, sha, message):
        """An annotated tag at a commit, the release tag of a promotion (docs/ROADMAP.md 16.7): the tag object, then its
        reference. A tag of that name that exists already is left where it is and reported, so asking twice never moves one."""
        url = f"https://github.com/{repository}/releases/tag/{quote(name, safe='/')}"
        tag = self.request("POST", f"/repos/{repository}/git/tags", json={"tag": name, "message": message, "object": sha, "type": "commit"}).json()
        try:
            self.request("POST", f"/repos/{repository}/git/refs", json={"ref": f"refs/tags/{name}", "sha": tag["sha"]})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:
                raise
            self.request("GET", f"/repos/{repository}/git/ref/tags/{quote(name, safe='/')}")  # 422 without the tag is a refusal, raised here
            return {"name": name, "url": url, "existed": True}
        return {"name": name, "url": url, "existed": False}

    def status(self, app, plan):
        result = plan.get("dispatch_result", {})
        run_id = result.get("workflow_run_id")
        rollback, promotion = plan.get("kind") == "rollback", plan.get("kind") in ("promotion", "production_rollback")
        # A promotion, and a production rollback, run in the platform repository's promote.yml, whatever pipeline released
        # the application in staging.
        repository = plan["pipeline"] if promotion else pipeline_of(app)
        if rollback:
            title = f"rollback-{plan['from_plan']}"
        elif promotion:
            title = f"{'rollback' if plan['kind'] == 'production_rollback' else 'promote'}-{plan['id']}"
        else:
            title = f"deploy-{plan['id']}"
        workflow = app.workflows["recover"] if rollback else ("promote.yml" if promotion else app.workflow)
        if run_id:
            run = self.request("GET", f"/repos/{repository}/actions/runs/{int(run_id)}").json()
        else:
            runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs", params={"event": "workflow_dispatch", "per_page": 100}).json()["workflow_runs"]
            run = next((r for r in runs if r.get("display_title") == title), None)
        if run is None:
            return {"status": "not_visible_yet", "note": "Do not redispatch; inspect GitHub Actions if this persists."}
        # The unique plan id in the run title is the identity. The head SHA is the branch tip the workflow ran on, which
        # only equals the plan's commit for a plan of the tip itself: rollbacks and plans pinned to older commits differ.
        # A platform-pipeline run's head is the platform's main, never the application's commit.
        tip_only = not rollback and not promotion and not plan.get("pinned_commit") and not central_of(app)
        if (tip_only and run.get("head_sha") != plan["sha"]) or run.get("display_title") != title:
            # Observed live: for a second or two after dispatch the run exists but its title and commit are not
            # populated yet. Only a run that has started or finished must prove its identity.
            if run_id and run.get("status") in {None, "", "requested", "queued", "pending", "waiting"}:
                return {"status": "not_visible_yet", "note": "GitHub is still creating the run; its identity is checked once it starts."}
            raise ValueError("Workflow identity does not match this deployment plan")
        jobs = []
        page = 1
        while True:
            batch = self.request("GET", f"/repos/{repository}/actions/runs/{int(run['id'])}/attempts/{int(run.get('run_attempt', 1))}/jobs",
                                 params={"per_page": 100, "page": page}).json()["jobs"]
            jobs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        found = {**{key: run.get(key) for key in ("id", "status", "conclusion", "html_url", "head_sha", "run_attempt", "created_at", "run_started_at", "updated_at")}, "jobs": jobs}
        if run.get("status") == "waiting":
            # Held by an environment's required reviewers (a promotion): who may approve it, and in which environment.
            pending = self.request("GET", f"/repos/{repository}/actions/runs/{int(run['id'])}/pending_deployments").json()
            found["pending"] = [{"environment": (item.get("environment") or {}).get("name"),
                                 "reviewers": [(r.get("reviewer") or {}).get("login") or (r.get("reviewer") or {}).get("name") for r in item.get("reviewers") or []]}
                                for item in pending if isinstance(item, dict)]
        return found

    def drift_report(self, repository, workflow="infra-drift.yml", name="drift-report"):
        """The newest report of the platform repository's weekly drift check, or None when it has not produced one yet."""
        runs = self.request("GET", f"/repos/{repository}/actions/workflows/{workflow}/runs",
                            params={"branch": "main", "status": "completed", "per_page": 5}).json()["workflow_runs"]
        holder = types.SimpleNamespace(repository=repository)
        for run in runs:
            files = self.artifact_json(holder, run["id"], name, ("drift-report.json",))
            if files and "drift-report.json" in files:
                return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "conclusion": run.get("conclusion"),
                        "created_at": run.get("created_at"), "report": files["drift-report.json"]}
        return None

    def newest_run_id(self, app, workflow, status="success"):
        """The id of a workflow's newest run on main with this status, or None: one call, so the settings page keeps a weekly
        report until a newer run exists instead of downloading it again."""
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs",
                            params={"status": status, "branch": "main", "per_page": 1}).json()["workflow_runs"]
        return runs[0]["id"] if runs else None

    def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        """The newest cost-report artifact from a successful rightsizing run, or an explicit 'none yet'."""
        runs = self.request("GET", f"/repos/{pipeline_of(app)}/actions/workflows/{workflow}/runs",
                            params={"status": "success", "branch": "main", "per_page": 5}).json()["workflow_runs"]
        for run in runs:
            files = self.artifact_json(app, run["id"], name, ("cost-report.json",), limit)
            if files and "cost-report.json" in files:
                return {"workflow_run_id": run["id"], "run_url": run.get("html_url"), "generated_at": run.get("updated_at"), "report": files["cost-report.json"]}
        return {"report": None, "note": "No successful rightsizing run with a cost-report artifact yet; the workflow runs weekly or on manual dispatch."}

