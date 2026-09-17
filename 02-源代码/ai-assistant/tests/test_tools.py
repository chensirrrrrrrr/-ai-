"""内部工具回调（Dify -> FastAPI）：签名校验与工具行为。"""
from __future__ import annotations

import json
import time

from app.config import settings
from app.core import sign_tool_payload
from tests.conftest import data


def _post(client, name: str, params: dict, *, ts=None, signature=None, send_headers=True):
    body = json.dumps({"params": params}, ensure_ascii=False).encode("utf-8")
    timestamp = ts if ts is not None else str(time.time())
    sig = signature if signature is not None else sign_tool_payload(body, timestamp)
    headers = {"content-type": "application/json"}
    if send_headers:
        headers["x-tool-timestamp"] = timestamp
        headers["x-tool-signature"] = sig
    return client.post(f"/internal/tools/{name}", content=body, headers=headers)


def _post_as(client, name: str, params: dict, role: str):
    """带 `role` 的签名调用 —— 用来验证「通道过了但业务权限没过」。"""
    body = json.dumps({"params": params, "role": role}, ensure_ascii=False).encode("utf-8")
    timestamp = str(time.time())
    return client.post(f"/internal/tools/{name}", content=body,
                       headers={"content-type": "application/json",
                                "x-tool-timestamp": timestamp,
                                "x-tool-signature": sign_tool_payload(body, timestamp)})


def test_tool_catalog(client):
    body = data(client.get("/internal/tools"))
    names = {item["name"] for item in body["items"]}
    assert {"lead_lookup", "student_scores", "pending_requests", "nl2sql"} <= names


def test_tool_rejects_missing_signature(client):
    resp = _post(client, "lead_lookup", {"name": "赵六"}, send_headers=False)
    assert resp.status_code == 403


def test_tool_rejects_tampered_signature(client):
    resp = _post(client, "lead_lookup", {"name": "赵六"}, signature="0" * 64)
    assert resp.status_code == 403


def test_tool_rejects_stale_timestamp(client):
    stale = str(time.time() - 3600)
    resp = _post(client, "lead_lookup", {"name": "赵六"}, ts=stale,
                 signature=sign_tool_payload(
                     json.dumps({"params": {"name": "赵六"}}, ensure_ascii=False).encode(), stale))
    assert resp.status_code == 403
    assert "过期" in resp.json()["message"]


def test_tool_lead_lookup(client):
    body = data(_post(client, "lead_lookup", {"name": "赵六"}))
    assert body["count"] >= 1
    assert body["leads"][0]["name"] == "赵六"
    assert body["leads"][0]["recent_followups"]


def test_tool_student_scores(client):
    body = data(_post(client, "student_scores", {"student_id": 1}))
    assert body["count"] >= 1
    assert body["scores"][0]["subject"] == "IELTS"


def test_tool_pending_requests(client):
    body = data(_post(client, "pending_requests", {}))
    assert body["count"] >= 1
    assert all(r["type"] for r in body["requests"])


def test_tool_my_requests(client):
    """学生查自己的行政申请：与 pending_requests 不同，要含**全部状态**。"""
    body = data(_post(client, "my_requests", {"student_id": 1}))
    assert body["student_id"] == 1
    assert body["count"] >= 1
    assert all(r["status"] for r in body["requests"])
    assert all("created_at" in r for r in body["requests"])


def test_tool_my_requests_can_filter_status(client):
    body = data(_post(client, "my_requests", {"student_id": 1, "status": "pending"}))
    assert all(r["status"] == "PENDING" for r in body["requests"])


def test_tool_my_requests_is_scoped_to_one_student(client):
    """必须按 student_id 收口 —— 不能退化成「把所有人的记录都吐出来」。"""
    one = data(_post(client, "my_requests", {"student_id": 1}))["requests"]
    two = data(_post(client, "my_requests", {"student_id": 2}))["requests"]
    assert {r["id"] for r in one} & {r["id"] for r in two} == set()


def test_tool_my_requests_requires_student_id(client):
    """缺 student_id 是**参数问题（400）**，不能被当成「不传就查全部」。"""
    resp = _post(client, "my_requests", {})
    assert resp.status_code == 400
    assert "student_id" in resp.json()["message"]


def test_tool_my_tickets(client):
    body = data(_post(client, "my_tickets", {"student_id": 1}))
    assert body["student_id"] == 1
    assert body["count"] >= 1
    assert all(t["status"] for t in body["tickets"])
    assert all(t["summary"] for t in body["tickets"])


def test_tool_my_tickets_requires_student_id(client):
    resp = _post(client, "my_tickets", {})
    assert resp.status_code == 400
    assert "student_id" in resp.json()["message"]


def test_tool_my_scores(client):
    """学生查自己成绩：含考试名 / 科目 / 分数 / 满分 / 日期。"""
    body = data(_post(client, "my_scores", {"student_id": 1}))
    assert body["student_id"] == 1
    assert body["count"] >= 2
    assert all(s["exam"] for s in body["scores"])
    assert all(s["subject"] for s in body["scores"])
    assert all(s["full_score"] > 0 for s in body["scores"])


def test_tool_my_scores_is_scoped_to_one_student(client):
    """必须按 student_id 收口 —— 不能退化成「把所有人的成绩都吐出来」。"""
    one = data(_post(client, "my_scores", {"student_id": 1}))["scores"]
    two = data(_post(client, "my_scores", {"student_id": 2}))["scores"]
    assert one and two
    assert {r["id"] for r in one} & {r["id"] for r in two} == set()


def test_tool_my_scores_requires_student_id(client):
    """缺 student_id 是**参数问题（400）**，不是静默查全库。"""
    resp = _post(client, "my_scores", {})
    assert resp.status_code == 400
    assert "student_id" in resp.json()["message"]


def test_my_scores_allows_student_but_legacy_student_scores_does_not(client):
    """`my_scores` 存在的理由，写成回归测试免得以后被「合并」掉。

    遗留的 `student_scores` 的 student_id 可选、min_role 是默认的 employee，
    学生调用会被 403；学生端只能走 `my_scores`。
    """
    allowed = _post_as(client, "my_scores", {"student_id": 1}, "student")
    assert allowed.status_code == 200

    blocked = _post_as(client, "student_scores", {"student_id": 1}, "student")
    assert blocked.status_code == 403
    assert "角色" in blocked.json()["message"]


def test_tool_my_records_denied_for_visitor(client):
    """min_role=student：访客即使过了通道校验，也不能查学生记录。"""
    for name in ("my_requests", "my_tickets", "my_scores"):
        resp = _post_as(client, name, {"student_id": 1}, "visitor")
        assert resp.status_code == 403, name
        assert "角色" in resp.json()["message"]


def test_tool_nl2sql(client):
    body = data(_post(client, "nl2sql", {"question": "统计一下客户线索的数量"}))
    assert body["row_count"] >= 1
    assert body["sql_preview"].lower().startswith("select")


def test_tool_nl2sql_requires_question(client):
    """缺必填参数返回 400（参数问题），不是 403（权限问题）—— 语义要分清楚。"""
    resp = _post(client, "nl2sql", {})
    assert resp.status_code == 400
    assert "question" in resp.json()["message"]


def test_unknown_tool_returns_404(client):
    resp = _post(client, "not_a_tool", {})
    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


def test_tool_bad_json_body(client):
    timestamp = str(time.time())
    raw = b"{not json"
    headers = {
        "content-type": "application/json",
        "x-tool-timestamp": timestamp,
        "x-tool-signature": sign_tool_payload(raw, timestamp),
    }
    resp = client.post("/internal/tools/lead_lookup", content=raw, headers=headers)
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# 第二道门：Dify 静态 Key（`Authorization: Bearer <DIFY_TOOL_KEY>`）
#
# 存在的理由：Dify 的「自定义工具 / HTTP 请求节点」只支持 none / api_key /
# bearer，**算不出 HMAC**（签名依赖原始 body 与 timestamp）。所以给 Dify 单独
# 开一条静态通道。这里既测新通道能用，也测**它没有削弱原来的 HMAC 防线**。
# --------------------------------------------------------------------------- #
STATIC_KEY = "test-dify-tool-key-0123456789"
_TOOL_BODY = json.dumps({"params": {"name": "赵六"}}, ensure_ascii=False).encode("utf-8")


def _post_bearer(client, name: str, token: str | None, role: str | None = None):
    payload = {"params": {"name": "赵六"}}
    if role:
        payload["role"] = role
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return client.post(f"/internal/tools/{name}", content=body, headers=headers)


def test_static_key_channel_off_when_not_configured(client, monkeypatch):
    """DIFY_TOOL_KEY 为空时通道整体关闭 —— 不给未配置的环境留后门。

    注意：这里**显式**把值打成空串，而不是断言「开发机 .env 里没配」——
    真实部署的 .env 会带上它，测试不能跟着开发机的配置漂。
    """
    monkeypatch.setattr(settings, "dify_tool_key", "")
    assert _post_bearer(client, "lead_lookup", STATIC_KEY).status_code == 403
    assert _post_bearer(client, "lead_lookup", None).status_code == 403


def test_static_key_accepts_correct_token(client, monkeypatch):
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    body = data(_post_bearer(client, "lead_lookup", STATIC_KEY))
    assert body["count"] >= 1
    assert body["leads"][0]["name"] == "赵六"


def test_static_key_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    assert _post_bearer(client, "lead_lookup", "wrong-key").status_code == 403


def test_static_key_rejects_non_bearer_scheme(client, monkeypatch):
    """裸 Key（不带 `Bearer `）不算数，避免「看着配了其实没配」。"""
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    resp = client.post("/internal/tools/lead_lookup", content=_TOOL_BODY,
                       headers={"content-type": "application/json",
                                "authorization": STATIC_KEY})
    assert resp.status_code == 403


def test_static_key_does_not_bypass_tampered_signature(client, monkeypatch):
    """⚠️ 核心防线：带了签名头就只认 HMAC。伪造签名 + 正确静态 Key 仍须 403。"""
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    resp = _post(client, "lead_lookup", {"name": "赵六"}, signature="0" * 64)
    assert resp.status_code == 403


def test_static_key_does_not_bypass_stale_timestamp(client, monkeypatch):
    """同理：签名正确但时间戳过期，不能靠静态 Key 兜底。"""
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    stale = str(time.time() - 3600)
    resp = client.post(
        "/internal/tools/lead_lookup", content=_TOOL_BODY,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {STATIC_KEY}",
                 "x-tool-timestamp": stale,
                 "x-tool-signature": sign_tool_payload(_TOOL_BODY, stale)},
    )
    assert resp.status_code == 403
    assert "过期" in resp.json()["message"]


def test_static_key_still_enforces_role(client, monkeypatch):
    """通道受信 ≠ 越权放行：静态 Key 调高权限工具仍被 min_role 拦下。"""
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    body = json.dumps({"params": {"request_id": 1, "action": "approve"},
                       "role": "visitor"}, ensure_ascii=False).encode("utf-8")
    resp = client.post("/internal/tools/leave_approve", content=body,
                       headers={"content-type": "application/json",
                                "authorization": f"Bearer {STATIC_KEY}"})
    assert resp.status_code == 403
    assert "角色" in resp.json()["message"]


def test_hmac_still_works_alongside_static_key(client, monkeypatch):
    """两条路并存：静态 Key 开着也不影响原有 HMAC 调用。"""
    monkeypatch.setattr(settings, "dify_tool_key", STATIC_KEY)
    body = data(_post(client, "lead_lookup", {"name": "赵六"}))
    assert body["count"] >= 1
