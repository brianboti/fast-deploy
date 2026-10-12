from app import app
from fastapi.testclient import TestClient


def test_health_and_version():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/").json()["version"] == "0.1.0"

