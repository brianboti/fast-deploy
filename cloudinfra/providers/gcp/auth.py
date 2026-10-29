"""Provider authentication for Google Cloud, isolated from every caller of the service.

The service holds no key: on a Google Cloud machine it uses the attached service account through the metadata server; on a
laptop it asks gcloud for a short-lived token, impersonating the configured service account when one is named. The token is
cached in memory until a minute before it ends and never written anywhere. This class was labdeploy.cloud.GoogleToken; the
application layer imports it from here now, so the dependency points from that layer to this one.
"""
import subprocess
import time

import httpx

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


class GoogleToken:
    """An access token for Google APIs as this host itself: the attached service account through the metadata server on a
    Google Cloud machine, else the operator's own gcloud sign-in on a laptop. Kept until a minute before it ends, or five
    minutes for gcloud's, whose end is not reported; a caller Google refuses with 401 invalidates it and asks again."""

    def __init__(self, client=None, runner=None, clock=time.time, impersonate=None):
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.runner = runner or (lambda args: subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout.strip())
        self.clock = clock
        self.impersonate = impersonate
        self.cached, self.until = None, 0.0

    def __call__(self):
        if self.cached and self.clock() < self.until:
            return self.cached
        try:
            response = self.client.get(METADATA, headers={"Metadata-Flavor": "Google"}, timeout=0.5)
            if response.status_code == 200:
                data = response.json()
                self.cached, self.until = data["access_token"], self.clock() + max(60, int(data.get("expires_in", 300)) - 60)
                return self.cached
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        command = ["gcloud", "auth", "print-access-token"]
        if self.impersonate:
            command += ["--impersonate-service-account", self.impersonate]
        try:
            token = self.runner(command)
        except (subprocess.CalledProcessError, OSError, subprocess.SubprocessError):
            raise ValueError("Could not get a Google token on this host: on a laptop, sign in with gcloud auth login; on the machine, "
                             "check that it runs with its service account") from None
        # gcloud may hand back a token it already holds, with only minutes left; a 401 calls invalidate() and asks again
        self.cached, self.until = token, self.clock() + 300
        return token

    def invalidate(self) -> None:
        """Forget the token: Google refused it (it expired sooner than this class assumed)."""
        self.cached, self.until = None, 0.0
