"""Provider discovery for the settings UI. Browser Google tokens are session-bound, read-only and never persisted.

GitHub grants stay with GitHub's installation picker. The local selection is a second restriction, never an added grant.
"""
import base64
import hashlib
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx

from .cloud import GoogleToken
from .github import AppInstallations, headers

CALLBACK = "/settings/connections/google/callback"
GOOGLE_SECRET_ENV = "LAB_GOOGLE_CLIENT_SECRET"  # noqa: S105 - environment variable name, never the secret
GOOGLE_SCOPE = "openid email https://www.googleapis.com/auth/cloud-platform.read-only https://www.googleapis.com/auth/cloud-billing.readonly"
TTL = 600
MAX_PAGES = 50


def json_response(response):
    response.raise_for_status()
    return response.json()


def github_catalog(github):
    """Fresh discovery independent of the local allowlist; only administrators can reach this through the UI."""
    credential = github.credentials()
    result = {"kind": credential.kind, "repositories": [], "installations": [], "install_url": None}

    def pages(get, path, key=None):
        items = []
        for page in range(1, MAX_PAGES + 1):
            data = json_response(get(path, params={"per_page": 100, "page": page}))
            batch = data[key] if key else data
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError("Too many repositories to display together. Limit the GitHub installation to the repositories you need, then refresh.")

    if isinstance(credential, AppInstallations):
        app = json_response(credential.signed("GET", "/app"))
        slug = app["slug"]
        if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
            raise ValueError("GitHub returned an invalid App name.")
        result["install_url"] = f"https://github.com/apps/{slug}/installations/new"
        result["app_name"] = slug
        installations = pages(lambda path, **kw: credential.signed("GET", path, **kw), "/app/installations")
        for installation in installations:
            if credential.installation_id and int(installation["id"]) != int(credential.installation_id):
                continue
            if installation.get("suspended_at"):
                continue
            installation_id = int(installation["id"])
            account = installation["account"]["login"]
            org = installation["account"].get("type") == "Organization"
            if not re.fullmatch(r"[A-Za-z0-9-]+", account):
                continue
            manage = (f"https://github.com/organizations/{account}/settings/installations/{installation_id}" if org
                      else f"https://github.com/settings/installations/{installation_id}")
            result["installations"].append({"account": account, "url": manage, "selection": installation["repository_selection"]})
            # This token can discover names, but cannot change a repository. Never reuse it for a deployment.
            token = json_response(credential.signed("POST", f"/app/installations/{installation_id}/access_tokens",
                                                   json={"permissions": {"metadata": "read"}}))["token"]
            auth = headers(token)
            try:
                repos = pages(lambda path, auth=auth, **kw: github.client.get(path, headers=auth, **kw), "/installation/repositories", "repositories")
            finally:
                try:
                    github.client.delete("/installation/token", headers=auth)
                except httpx.HTTPError:
                    pass  # expires in one hour even if revocation cannot reach GitHub
            result["repositories"].extend({"name": repo["full_name"], "private": repo["private"], "account": account} for repo in repos)
        # Changes on GitHub take effect on the next operation, including a newly installed repository.
        with credential.lock:
            credential.installations.clear()
            credential.tokens.clear()
            credential.granted.clear()
    else:
        auth = headers(credential.token())
        repos = pages(lambda path, **kw: github.client.get(path, headers=auth, **kw), "/user/repos")
        result["repositories"] = [{"name": repo["full_name"], "private": repo["private"], "account": repo["owner"]["login"]} for repo in repos]
    result["repositories"] = sorted({repo["name"].lower(): repo for repo in result["repositories"]}.values(), key=lambda repo: repo["name"].lower())
    return result


class BrowserConnections:
    """Short-lived state owned by an authenticated settings session. No user token is passed to a release workflow."""

    def __init__(self, config, github, client=None, clock=time.time, foundation_token=None):
        self.config, self.github = config, github
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.clock, self.lock = clock, threading.RLock()
        self.foundation_token = foundation_token or GoogleToken()
        self.states, self.accounts, self.catalogs, self.reviews = {}, {}, {}, {}

    def cleanup(self):
        now = self.clock()
        with self.lock:
            for items in (self.states, self.accounts, self.catalogs, self.reviews):
                for key in list(items):
                    if items[key]["until"] <= now:
                        del items[key]

    def forget(self, session):
        with self.lock:
            for items in (self.states, self.accounts, self.catalogs, self.reviews):
                for key in list(items):
                    if items[key]["session"] == session:
                        del items[key]

    def google_ready(self):
        return bool(self.config.oauth and self.config.connections.google_client_id and os.environ.get(GOOGLE_SECRET_ENV))

    def close(self):
        with self.lock:
            for items in (self.states, self.accounts, self.catalogs, self.reviews):
                items.clear()
        self.client.close()
        if isinstance(self.foundation_token, GoogleToken):
            self.foundation_token.client.close()

    def start_google(self, session):
        if not self.google_ready():
            raise ValueError("Google sign-in needs its one-time administrator setup. Open Connection setup below.")
        self.cleanup()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect = self.config.oauth.public_url + CALLBACK
        client_id = self.config.connections.google_client_id
        with self.lock:
            self.states[state] = {"session": session, "until": self.clock() + TTL, "verifier": verifier, "redirect": redirect, "client_id": client_id}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": GOOGLE_SCOPE,
            "access_type": "online", "prompt": "select_account", "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def finish_google(self, session, state, code, error=None):
        self.cleanup()
        with self.lock:
            pending = self.states.get(state)
            if pending is None or pending["session"] != session:
                raise ValueError("This Google sign-in expired or belongs to another browser session. Connect again.")
            del self.states[state]
        if error:
            raise ValueError("Google sign-in was not completed. You can connect again whenever you are ready.")
        if not code:
            raise ValueError("Google did not return a sign-in code. Connect again.")
        token = json_response(self.client.post("https://oauth2.googleapis.com/token", data={
            "client_id": pending["client_id"], "client_secret": os.environ.get(GOOGLE_SECRET_ENV, ""), "code": code,
            "redirect_uri": pending["redirect"], "grant_type": "authorization_code", "code_verifier": pending["verifier"],
        }))
        if not set(GOOGLE_SCOPE.split()) - {"openid", "email"} <= set(token.get("scope", "").split()):
            raise ValueError("Google project access was not granted. Connect again and allow read-only project access.")
        auth = {"Authorization": "Bearer " + token["access_token"]}
        user = json_response(self.client.get("https://openidconnect.googleapis.com/v1/userinfo", headers=auth))
        if not user.get("email_verified") or not user.get("email") or not user.get("sub"):
            raise ValueError("Google did not confirm this account's email. Connect another account.")
        key = secrets.token_urlsafe(24)
        account = {"session": session, "until": self.clock() + max(0, min(int(token["expires_in"]), 3600) - 60),
                   "email": user["email"], "subject": user["sub"], "token": token["access_token"], "projects": None}
        with self.lock:
            for previous in list(self.accounts):
                if self.accounts[previous]["session"] == session and self.accounts[previous]["subject"] == user["sub"]:
                    del self.accounts[previous]
            self.accounts[key] = account
        return key

    def account(self, session, key):
        self.cleanup()
        with self.lock:
            account = self.accounts.get(key)
            if account is None or account["session"] != session:
                raise ValueError("This Google connection expired. Connect the account again.")
            return dict(account)

    def projects(self, session, key):
        account = self.account(session, key)
        auth = {"Authorization": "Bearer " + account["token"]}
        projects, params = [], {"pageSize": 100}
        for _ in range(MAX_PAGES):
            data = json_response(self.client.get("https://cloudresourcemanager.googleapis.com/v3/projects:search", headers=auth, params=params))
            for project in data.get("projects", []):
                if project.get("state") == "ACTIVE":
                    projects.append({"id": project["projectId"], "number": project["name"].split("/")[-1], "name": project.get("displayName", project["projectId"])})
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        else:
            raise ValueError("Google returned too many projects. Use an account with access to fewer projects.")
        with self.lock:
            if key in self.accounts:
                self.accounts[key]["projects"] = sorted(projects, key=lambda project: project["name"].lower())
        return projects

    def destination(self, session, key, project_id):
        account = self.account(session, key)
        # Re-query before saving: the submitted project and number are not trusted.
        project = next((p for p in self.projects(session, key) if p["id"] == project_id), None)
        if project is None:
            raise ValueError("Choose a project this Google account can access. Refresh its projects first.")
        auth = {"Authorization": "Bearer " + account["token"]}
        billing = json_response(self.client.get(f"https://cloudbilling.googleapis.com/v1/projects/{project_id}/billingInfo", headers=auth))
        if not billing.get("billingEnabled"):
            raise ValueError("Enable billing for this project in Google Cloud, then select it again.")
        # Verify the foundation's existence. This does not assert permission to deploy: workflow/IAM checks still run.
        # IAM's get endpoint does not accept cloud-platform.read-only. Use the server's existing identity instead of
        # asking a person for the full cloud-platform scope just to choose a project.
        try:
            auth = {"Authorization": "Bearer " + self.foundation_token()}
        except ValueError:
            raise ValueError("The server cannot verify this project’s deployment setup. Ask its administrator to connect the server’s Google identity.") from None
        for url in (f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts/lab-deploy-infra-apply@{project_id}.iam.gserviceaccount.com",
                    f"https://iam.googleapis.com/v1/projects/{project_id}/serviceAccounts/lab-deploy-infra-plan@{project_id}.iam.gserviceaccount.com"):
            data = json_response(self.client.get(url, headers=auth))
            if data.get("disabled"):
                raise ValueError("This project's deployment identity is disabled. Ask its administrator to restore the Fast Deploy foundation.")
        bucket = self.config.onboarding.state_bucket if self.config.onboarding and self.config.onboarding.project_id == project_id else None
        bucket = bucket or f"{project_id}-tfstate"
        json_response(self.client.get(f"https://storage.googleapis.com/storage/v1/b/{bucket}", headers=auth))
        return {**project, "email": account["email"]}

    def refresh_github(self, session):
        self.cleanup()
        result = github_catalog(self.github)
        with self.lock:
            self.catalogs[session] = {"session": session, "until": self.clock() + TTL, "data": result}
        return result

    def snapshot(self, session):
        self.cleanup()
        with self.lock:
            catalog = self.catalogs.get(session)
            accounts = [{"key": key, "email": account["email"], "projects": account["projects"]} for key, account in self.accounts.items()
                        if account["session"] == session]
            review = self.reviews.get(session)
        return {"google_ready": self.google_ready(), "accounts": accounts, "github": catalog["data"] if catalog else None, "review": review}

    def repository_allowed(self, repository):
        selected = self.config.connections.repositories
        platform = self.config.platform.repository.lower() if self.config.platform else None
        if selected is not None and repository.lower() != platform and repository.lower() not in selected:
            raise ValueError("Select and save this repository in GitHub access before continuing.")
        return repository

    def review_repository(self, controller, principal, session, form, config_sha):
        from .onboarding import application_name, normalize_path, normalize_repository
        # A failed new check must never leave the previous request's Start button active.
        with self.lock:
            self.reviews.pop(session, None)
        repository = self.repository_allowed(normalize_repository(str(form.get("repository", ""))))
        path = normalize_path(str(form.get("path", "")))
        app_id = str(form.get("application", "")).strip()
        mode = str(form.get("mode", "as-is"))
        if mode not in ("as-is", "prepared"):
            raise ValueError("Choose one of the repository setup options.")
        app_id = application_name(repository, path, app_id, mode == "as-is")
        previous = controller.store.onboarding(app_id)
        if previous and (previous["repository"].lower() != repository.lower() or previous.get("path", "") != path):
            raise ValueError("This application name already belongs to another repository or folder. Choose a different name.")
        if previous and previous["state"] != "complete" and bool(previous.get("as_is")) != (mode == "as-is"):
            raise ValueError("This application already has a setup in progress. Choose its original setup option or a different application name.")
        result = controller.check_repository(principal, repository, path or None)
        ready = result.get("as_it_is", {}).get("ready") if mode == "as-is" else result.get("ready")
        review = {"session": session, "until": self.clock() + TTL, "id": secrets.token_urlsafe(24), "config_sha": config_sha,
                  "repository": repository, "path": path, "application": app_id, "as_is": mode == "as-is",
                  "auto_deploy": bool(previous.get("auto_deploy")) if previous else form.get("auto_deploy") == "on",
                  "previous_state": previous["state"] if previous else None, "check": result, "ready": bool(ready)}
        with self.lock:
            self.reviews[session] = review
        return review

    def start_application(self, controller, principal, session, review_id, config_sha):
        self.cleanup()
        with self.lock:
            review = self.reviews.get(session)
            if review is None or not secrets.compare_digest(review["id"].encode(), review_id.encode()):
                raise ValueError("This repository review expired or was already used. Check the repository again.")
            if review["config_sha"] != config_sha:
                raise ValueError("Settings changed after this review. Check the repository again to review the new destination and permissions.")
            if not review["ready"]:
                raise ValueError("Resolve the repository checks before starting.")
            self.repository_allowed(review["repository"])
            if not self.config.live_dispatch or self.config.paused:
                raise ValueError("Releases are disabled or paused. Enable them in Platform before starting an application.")
            if self.config.onboarding is None:
                raise ValueError("Choose the Google Cloud destination before starting an application.")
            del self.reviews[session]  # consume before dispatch, including failures; a double click cannot start twice
        return controller.onboard(principal, review["repository"], review["application"], review["auto_deploy"], review["path"] or None,
                                  as_is=review["as_is"], until="contract" if review["as_is"] else "infrastructure")
