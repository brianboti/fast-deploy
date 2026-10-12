"""Durable Cloud Storage checkpoint with generation-checked writes."""
import json
from urllib.parse import quote

import httpx


class GCSJournal:
    def __init__(self, bucket, service, token, client=None):
        self.bucket, self.key = bucket, f"releases/{service}.json"
        self.token = token
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.generation = None

    def request(self, method, url, **kwargs):
        response = self.client.request(method, url, headers={"Authorization": "Bearer " + self.token()}, **kwargs)
        return response

    def read(self):
        url = f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self.key, safe='')}"
        metadata = self.request("GET", url)
        if metadata.status_code == 404:
            self.generation = "0"
            return None
        metadata.raise_for_status()
        generation = metadata.json()["generation"]
        body = self.request("GET", url, params={"alt": "media", "generation": generation})
        body.raise_for_status()
        self.generation = generation
        return body.json()

    def write(self, payload):
        if self.generation is None:
            raise ValueError("Read the journal before writing")
        response = self.request("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
                                params={"uploadType": "media", "name": self.key, "ifGenerationMatch": self.generation},
                                content=json.dumps(payload).encode())
        if response.status_code == 412:
            raise RuntimeError("Recovery journal changed concurrently; stop and reconcile")
        response.raise_for_status()
        self.generation = response.json()["generation"]

