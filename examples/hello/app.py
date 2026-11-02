"""A tiny web application for the runs on Google Cloud (docs/DELIVERY-SPIKE.md): built by the delivery service from this
repository, then released onto a machine, a cluster and Cloud Run. Standard library only.

/healthz answers 200. / answers the VERSION it was given, the names (never the values) of the other variables, and how
the container runs: its user and effective capabilities, so a run can show the bounds the service put on it.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHOWN = ("VERSION", "PORT")


def effective_capabilities() -> str:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("CapEff:"):
                    return line.split()[1]
    except OSError:
        pass
    return "unknown"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = b"ok\n"
        else:
            body = (json.dumps({"version": os.environ.get("VERSION", "unset"), "uid": os.getuid(),
                                "capabilities": effective_capabilities(),
                                "variables": sorted(name for name in os.environ if name not in SHOWN)}) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    # a container answers on its own address, where the host's port, the pod's probe and Cloud Run reach it
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()  # noqa: S104
