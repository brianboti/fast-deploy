"""Durable Cloud Storage checkpoint with generation-checked writes."""
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

WRITE_ATTEMPTS = 3  # one transient Cloud Storage error must not stop a release that is otherwise healthy


class JournalConflict(RuntimeError):
    """Another writer changed the checkpoint: the release must stop touching the cloud."""


class GCSJournal:
    def __init__(self, bucket, service, token, client=None, sleep=time.sleep):
        self.bucket, self.key = bucket, f"releases/{service}.json"
        self.token = token
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.generation = None
        self.lock = threading.RLock()
        self.payload = None
        self.heartbeat_error = None
        self.sleep = sleep

    def request(self, method, url, **kwargs):
        response = self.client.request(method, url, headers={"Authorization": "Bearer " + self.token()}, **kwargs)
        return response

    def read(self):
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self.key, safe='')}"
        metadata = self.request("GET", url)
        if metadata.status_code == 404:
            self.generation = "0"
            self.payload = None
            return None
        metadata.raise_for_status()
        generation = metadata.json()["generation"]
        body = self.request("GET", url, params={"alt": "media", "generation": generation})
        body.raise_for_status()
        self.generation = generation
        self.payload = body.json()
        return self.payload

    def write(self, payload):
        with self.lock:
            try:
                return self._write(payload)
            except Exception as error:
                self.heartbeat_error = type(error).__name__
                raise

    def _write(self, payload):
        if self.heartbeat_error:
            raise RuntimeError("Release heartbeat failed; stop and reconcile") from None
        if self.generation is None:
            raise ValueError("Read the journal before writing")
        payload = {**payload, "schema_version": 2, "heartbeat_at": datetime.now(UTC).isoformat()}
        body = json.dumps(payload).encode()
        for attempt in range(WRITE_ATTEMPTS):
            try:
                response = self.request("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                                        params={"uploadType": "media", "name": self.key, "ifGenerationMatch": self.generation}, content=body)
            except httpx.TransportError:
                response = None
            if response is not None and response.status_code == 412:
                # After a lost response the first try may have landed; only someone else's write is a conflict.
                if attempt and self._landed(body):
                    self.payload = payload
                    return
                raise JournalConflict("Recovery journal changed concurrently; stop and reconcile")
            if response is not None and response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                self.generation = response.json()["generation"]
                self.payload = payload
                return
            if attempt + 1 < WRITE_ATTEMPTS:
                self.sleep(2 * (attempt + 1))
        raise RuntimeError("Recovery journal unavailable after retries; stop and reconcile")

    def _landed(self, body):
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self.key, safe='')}"
        metadata = self.request("GET", url)
        if metadata.status_code != 200:
            return False
        generation = metadata.json()["generation"]
        found = self.request("GET", url, params={"alt": "media", "generation": generation})
        if found.status_code != 200 or found.content != body:
            return False
        self.generation = generation
        return True

    def claim(self, identifier, binding, attempt=1):
        """One runner per release attempt; the same attempt can never mutate twice."""
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid release identity")
        service = self.key.split("/")[-1].removesuffix(".json")
        name = f"claims/{service}/{identifier}.json" if int(attempt) <= 1 else f"claims/{service}/{identifier}-a{int(attempt)}.json"
        response = self.request("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                                params={"uploadType": "media", "name": name, "ifGenerationMatch": "0"},
                                content=json.dumps(binding, sort_keys=True).encode())
        if response.status_code == 412:
            raise ValueError("This release operation was already claimed; reconcile it or create a new reviewed plan")
        response.raise_for_status()

    @contextmanager
    def keepalive(self, interval=30):
        stop = threading.Event()
        def pulse():
            while not stop.wait(interval):
                try:
                    with self.lock:
                        if self.payload is not None:
                            self._write(self.payload)
                except Exception as error:  # noqa: BLE001 - main execution observes the failure at its next checkpoint
                    self.heartbeat_error = type(error).__name__
                    return
        worker = threading.Thread(target=pulse, name="release-heartbeat", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=35)

