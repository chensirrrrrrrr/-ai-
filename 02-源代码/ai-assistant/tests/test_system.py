"""系统与认证。"""
from __future__ import annotations

from tests.conftest import data


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = data(resp)
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["dialect"] == "sqlite"
    assert body["dify_mode"] == "mock"


def test_ready(client):
    body = data(client.get("/api/v1/ready"))
    assert body["ready"] is True


def test_root(client):
    body = data(client.get("/"))
    assert body["docs"] == "/docs"


def test_issue_token_success(client):
    resp = client.post("/api/v1/auth/token",
                       json={"username": "advisor", "password": "advisor123"})
    body = data(resp)
    assert body["role"] == "employee"
    assert body["access_token"]
    assert body["expires_in"] > 0


def test_issue_token_wrong_password(client):
    resp = client.post("/api/v1/auth/token",
                       json={"username": "advisor", "password": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == 40100


def test_me(client, manager_headers):
    body = data(client.get("/api/v1/auth/me", headers=manager_headers))
    assert body["role"] == "manager"
    assert body["display_name"]


def test_missing_token_rejected(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40100


def test_invalid_token_rejected(client):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_visitor_token_is_anonymous(client, visitor_headers):
    body = data(client.get("/api/v1/auth/me", headers=visitor_headers))
    assert body["role"] == "visitor"


def test_trace_id_echoed(client):
    resp = client.get("/api/v1/health", headers={"x-trace-id": "deadbeef"})
    assert resp.headers["x-trace-id"] == "deadbeef"
    assert resp.json()["trace_id"] == "deadbeef"


def test_trace_id_generated_when_absent(client):
    resp = client.get("/api/v1/health")
    assert len(resp.headers["x-trace-id"]) == 8


def test_process_time_header(client):
    resp = client.get("/api/v1/health")
    assert float(resp.headers["x-process-time-ms"]) >= 0


def test_openapi_available(client):
    payload = client.get("/openapi.json").json()
    assert payload["info"]["title"]
    assert "/api/v1/chat/message" in payload["paths"]
    assert len(payload["paths"]) >= 25


# --------------------------------------------------------------------------- #
# 健康探针的降级口径：数据库连不上要**如实说**，不能一律回 ok
# --------------------------------------------------------------------------- #
def test_health_reports_degraded_when_database_is_down(client):
    from app.db import get_db
    from app.main import app

    class _BrokenSession:
        def execute(self, *args, **kwargs):
            raise RuntimeError("database is gone")

    def _broken_db():
        yield _BrokenSession()

    app.dependency_overrides[get_db] = _broken_db
    try:
        body = data(client.get("/api/v1/health"))
        assert body["status"] == "degraded"
        assert body["database"] == "down"
    finally:
        app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# 账号状态与改密
#
# ⚠️ 用**临时账号**而不是改种子账号的密码 —— 种子账号是 session 级夹具登录的，
#    改掉会让「按文件字母序执行才通过」的假绿重现。
# --------------------------------------------------------------------------- #
def _make_account(username: str, password: str, *, role: str = "manager",
                  active: bool = True) -> None:
    from app.core import hash_password
    from app.db import SessionLocal
    from app.models import SysAccount

    session = SessionLocal()
    try:
        session.add(SysAccount(username=username, password_hash=hash_password(password),
                               role=role, display_name="临时验证账号", is_active=active))
        session.commit()
    finally:
        session.close()


def _drop_account(username: str) -> None:
    from app.db import SessionLocal
    from app.models import SysAccount

    session = SessionLocal()
    try:
        session.query(SysAccount).filter(SysAccount.username == username).delete()
        session.commit()
    finally:
        session.close()


def test_inactive_account_is_forbidden_not_unauthorized(client):
    """停用账号密码对了也不能进 —— 报 403 而不是 401，前端才好给「找管理员」的提示。"""
    _make_account("probe_inactive", "probe123", active=False)
    try:
        resp = client.post("/api/v1/auth/token",
                           json={"username": "probe_inactive", "password": "probe123"})
        assert resp.status_code == 403
        assert resp.json()["code"] == 40300
    finally:
        _drop_account("probe_inactive")


def test_change_password_requires_login(client):
    resp = client.post("/api/v1/auth/password",
                       json={"old_password": "whatever", "new_password": "abcdef"})
    assert resp.status_code == 401


def test_change_password_full_flow(client):
    _make_account("probe_pw", "initial123")
    try:
        token = client.post("/api/v1/auth/token",
                            json={"username": "probe_pw", "password": "initial123"}
                            ).json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 新密码太短 → 参数错误（不是 500）
        too_short = client.post("/api/v1/auth/password",
                                json={"old_password": "initial123", "new_password": "123"},
                                headers=headers)
        assert too_short.status_code == 400
        assert too_short.json()["code"] == 40000

        # 原密码不对 → 401
        wrong_old = client.post("/api/v1/auth/password",
                                json={"old_password": "not-my-password",
                                      "new_password": "brandnew123"},
                                headers=headers)
        assert wrong_old.status_code == 401
        assert wrong_old.json()["code"] == 40100

        # 正确改密 → 立即生效：新密码能登录、旧密码不能
        changed = data(client.post("/api/v1/auth/password",
                                   json={"old_password": "initial123",
                                         "new_password": "brandnew123"},
                                   headers=headers))
        assert changed["changed"] is True
        assert client.post("/api/v1/auth/token",
                           json={"username": "probe_pw", "password": "brandnew123"}
                           ).status_code == 200
        assert client.post("/api/v1/auth/token",
                           json={"username": "probe_pw", "password": "initial123"}
                           ).status_code == 401
    finally:
        _drop_account("probe_pw")


def test_change_password_writes_audit_log(client):
    """改密必须留审计（谁、什么时候、对哪个账号）—— 这是等保口径里最常被查的一条。"""
    from app.db import SessionLocal
    from app.models import AuditLog

    _make_account("probe_pw_audit", "initial123")
    session = SessionLocal()
    try:
        token = client.post("/api/v1/auth/token",
                            json={"username": "probe_pw_audit", "password": "initial123"}
                            ).json()["data"]["access_token"]
        data(client.post("/api/v1/auth/password",
                         json={"old_password": "initial123", "new_password": "brandnew123"},
                         headers={"Authorization": f"Bearer {token}"}))

        row = (session.query(AuditLog)
               .filter(AuditLog.action == "auth.change_password",
                       AuditLog.actor_id == "probe_pw_audit")
               .order_by(AuditLog.id.desc()).first())
        assert row is not None
    finally:
        session.close()
        _drop_account("probe_pw_audit")
