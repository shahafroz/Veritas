import os
import tempfile
from pathlib import Path

os.environ["VERITAS_DB"] = str(Path(tempfile.mkdtemp(prefix="veritas-test-")) / "test.db")
os.environ["SEED_DEMO"] = "true"
os.environ["LLM_ENABLED"] = "false"
os.environ["JWT_SECRET"] = "test-secret-at-least-32-bytes-long-xx"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.seed import DEMO_PASSWORD  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth(client):
    cache: dict[str, dict] = {}

    def headers(who: str) -> dict:
        if who not in cache:
            r = client.post("/api/auth/login", json={"email": f"{who}@helix.demo", "password": DEMO_PASSWORD})
            assert r.status_code == 200, r.text
            cache[who] = {"Authorization": f"Bearer {r.json()['token']}"}
        return cache[who]

    return headers
