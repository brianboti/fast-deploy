"""A fake Cloud Build: builds.create and builds.get as the API answers them, with the statuses a build goes through
scripted per build, and every request it was sent kept for the tests to read."""
import itertools

DIGEST_BUILT = "sha256:" + "b" * 64


class FakeCloudBuild:
    """Each build reports the statuses in `script` one get at a time, then stays on the last. A SUCCESS carries the digest
    of the image the build asked to push, unless `no_digest`."""

    def __init__(self, script=("QUEUED", "WORKING", "SUCCESS"), digest=DIGEST_BUILT, no_digest=False, fail_create=None, fail_get=0):
        self.script, self.digest, self.no_digest = list(script), digest, no_digest
        self.fail_create, self.fail_get = fail_create, fail_get
        self.created: list[dict] = []
        self.gets: list[tuple] = []
        self.builds: dict[str, dict] = {}
        self.ids = (f"cb-{number:04d}" for number in itertools.count(1))

    def __call__(self, environment):
        self.environment = environment
        return self

    def create(self, project, region, body):
        if self.fail_create is not None:
            raise self.fail_create
        build_id = next(self.ids)
        self.created.append({"project": project, "region": region, "body": body, "environment": self.environment})
        self.builds[build_id] = {"body": body, "step": 0}
        return {"id": build_id, "status": "QUEUED", "logUrl": f"https://console.cloud.google.com/cloud-build/builds;region={region}/{build_id}"}

    def get(self, project, region, cloud_build_id):
        self.gets.append((project, region, cloud_build_id))
        if self.fail_get:
            self.fail_get -= 1
            from appdelivery.errors import DeliveryError, ErrorCode
            raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, "Cloud Build could not be reached (ConnectError)")
        build = self.builds[cloud_build_id]
        status = self.script[min(build["step"], len(self.script) - 1)]
        build["step"] += 1
        found = {"id": cloud_build_id, "status": status, "logUrl": f"https://console.cloud.google.com/cloud-build/builds;region={region}/{cloud_build_id}"}
        if status == "SUCCESS" and not self.no_digest:
            found["results"] = {"images": [{"name": build["body"]["images"][0], "digest": self.digest}]}
        if status == "FAILURE":
            found["statusDetail"] = "Build step 0 \"gcr.io/cloud-builders/docker\" failed: step exited with non-zero status: 1"
        return found
