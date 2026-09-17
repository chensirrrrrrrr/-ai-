"""pytest 全局夹具。

⚠️ 必须在 import app 之前设置环境变量：`app.config.settings` 是进程级单例，
   导入后再改 env 不会生效。
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "data" / "test_ai_assistant.db"
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
# 连 WAL/SHM 一起清掉，否则上一轮的残留数据会带进来
for suffix in ("", "-wal", "-shm"):
    leftover = pathlib.Path(str(DB_FILE) + suffix)
    if leftover.exists():
        leftover.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["DIFY_MODE"] = "mock"
os.environ["ASR_PROVIDER"] = "mock"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdefghijklmn"
os.environ["DEBUG"] = "true"
os.environ["NL2SQL_MAX_ROWS"] = "50"

import pytest                                                    # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

from app.db import drop_all, init_db                              # noqa: E402
from app.main import app                                          # noqa: E402
from scripts.seed import seed                                     # noqa: E402

CREDENTIALS = {
    "admin": ("admin", "admin123"),
    "manager": ("manager", "manager123"),
    "advisor": ("advisor", "advisor123"),
    "teacher": ("teacher", "teacher123"),
    "student": ("student", "student123"),
}


@pytest.fixture(scope="session", autouse=True)
def _database():
    drop_all()
    seed()
    yield
    drop_all()


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/token", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


@pytest.fixture(scope="session")
def admin_headers(client):
    return _login(client, *CREDENTIALS["admin"])


@pytest.fixture(scope="session")
def manager_headers(client):
    return _login(client, *CREDENTIALS["manager"])


@pytest.fixture(scope="session")
def advisor_headers(client):
    return _login(client, *CREDENTIALS["advisor"])


@pytest.fixture(scope="session")
def teacher_headers(client):
    return _login(client, *CREDENTIALS["teacher"])


@pytest.fixture(scope="session")
def student_headers(client):
    return _login(client, *CREDENTIALS["student"])


@pytest.fixture(scope="session")
def visitor_headers(client):
    resp = client.post("/api/v1/auth/visitor")
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


def data(resp):
    """取出统一响应信封里的 data，并断言 code==0。"""
    payload = resp.json()
    assert payload["code"] == 0, payload
    assert payload["trace_id"], "响应必须带 trace_id"
    return payload["data"]
