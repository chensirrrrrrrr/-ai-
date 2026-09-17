"""对话链路：意图路由 / 权限隔离 / 流式 / 语音录入。"""
from __future__ import annotations

import json

from tests.conftest import data


def test_visitor_knowledge_question(client, visitor_headers):
    """知识类问题：访客 -> 客服 Agent -> mock Dify 返回带引用的答案。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-t1", "message": "你们机构在成都的校区在哪？"},
                       headers=visitor_headers)
    body = data(resp)
    assert body["agent"] == "customer_service"
    assert body["intent"] == "company_info"
    assert body["confidence"] >= 0.75
    assert body["references"], "知识类回答必须带引用来源"
    assert body["degraded"] is False
    assert body["conversation_id"]


def test_staff_nl2sql_intent(client, advisor_headers):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-t2",
                             "message": "帮我查一下赵六的跟进记录并统计一下"},
                       headers=advisor_headers)
    body = data(resp)
    assert body["agent"] == "enterprise_assistant"
    assert body["intent"] == "nl2sql_query"
    assert any("nl2sql" in a for a in body["suggest_actions"])


def test_knowledge_question_not_misrouted_to_nl2sql(client, admin_headers):
    """回归：疑问词「哪些」曾让 mock 路由器把知识类问题判成取数请求。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-kb", "message": "留学需要准备哪些材料？"},
                       headers=admin_headers)
    body = data(resp)
    assert body["agent"] == "customer_service"
    assert body["intent"] == "company_info"
    assert body["references"], "材料清单类问题必须带引用来源"


def test_role_isolation_blocks_staff_agent_for_visitor(client, visitor_headers):
    """访客即使说「我要请假」（预筛命中学生助手），也会被权限层降级到客服 Agent。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-t3", "message": "我要请假 2 天"},
                       headers=visitor_headers)
    body = data(resp)
    assert body["agent"] == "customer_service"
    assert body["confidence"] <= 0.5


def test_prefilter_hits_high_confidence(client, student_headers):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-t4", "message": "我要请假 2 天"},
                       headers=student_headers)
    body = data(resp)
    assert body["agent"] == "student_helper"
    assert body["intent"] == "leave_apply"
    assert body["confidence"] >= 0.9


def test_no_recall_answer_is_graceful(client, visitor_headers):
    """知识库无召回时必须给出可解释的兜底话术，而不是编造。

    回归点：旧版只回一句「暂未查询到相关信息」，用户看不出下一步该干什么。
    现在要求：明说没有信息 + 讲清能处理什么 + 给出转人工的出口。
    """
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-t5", "message": "帮我订一张电影票吧"},
                       headers=visitor_headers)
    body = data(resp)
    assert "暂未查询到" not in body["answer"], "不该再退回无信息量的死胡同话术"
    assert "校区" in body["answer"], "兜底应说明自己能处理哪些事"
    assert "转人工" in body["answer"], "兜底必须给出下一步出口"
    assert body["references"] == []


def test_stream_sse(client, visitor_headers):
    resp = client.post("/api/v1/chat/stream",
                       json={"session_id": "s-t6", "message": "你们机构在成都的校区在哪"},
                       headers=visitor_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    chunks = [line[6:] for line in resp.text.splitlines() if line.startswith("data: ")]
    payloads = [json.loads(c) for c in chunks if c != "[DONE]"]
    assert payloads[0]["event"] == "route"
    assert payloads[0]["agent"] == "customer_service"
    assert any(p.get("event") == "message" for p in payloads)
    assert chunks[-1] == "[DONE]"


def test_asr_leave_apply_extracts_slots(client, student_headers):
    files = {"file": ("leave.wav", b"RIFF" + b"\x00" * 2048, "audio/wav")}
    resp = client.post("/api/v1/chat/asr", files=files,
                       data={"purpose": "leave_apply"}, headers=student_headers)
    body = data(resp)
    assert body["provider"] == "mock"
    assert "请假" in body["transcript"]
    assert body["structured"]["days"] == 2
    assert body["structured"]["start_date"] == "2026-09-15"
    assert body["structured"]["reason"] == "看病"


def test_asr_daily_report(client, advisor_headers):
    files = {"file": ("daily.m4a", b"ID3" + b"\x01" * 4096, "audio/m4a")}
    resp = client.post("/api/v1/chat/asr", files=files,
                       data={"purpose": "daily_report"}, headers=advisor_headers)
    body = data(resp)
    assert "雅思" in body["transcript"]
    assert body["structured"]["exam"] == "IELTS"
    assert body["duration_ms"] > 0


def test_asr_rejects_empty_file(client, student_headers):
    files = {"file": ("empty.wav", b"", "audio/wav")}
    resp = client.post("/api/v1/chat/asr", files=files,
                       data={"purpose": "chat"}, headers=student_headers)
    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


def test_chat_requires_auth(client):
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-anon", "message": "你好"})
    assert resp.status_code == 401


def test_every_role_can_chat(client, admin_headers, manager_headers,
                             advisor_headers, student_headers, visitor_headers):
    """五种角色都必须能进统一对话入口。

    回归点：`ChatRequest.role` 的 Literal 曾漏掉 `admin`，导致管理员一发言就 422。
    前端曾把登录角色原样塞进请求体，正好把这个坑踩出来。
    """
    for name, headers in [("admin", admin_headers), ("manager", manager_headers),
                          ("employee", advisor_headers), ("student", student_headers),
                          ("visitor", visitor_headers)]:
        resp = client.post("/api/v1/chat/message",
                           json={"session_id": f"s-role-{name}", "message": "你们机构的服务流程是怎样的？"},
                           headers=headers)
        assert resp.status_code == 200, f"{name} 角色对话失败：{resp.status_code} {resp.text[:200]}"
        assert data(resp)["answer"]


def test_chat_ignores_spoofed_body_role(client, visitor_headers):
    """请求体里伪造 role=admin 也必须被忽略，实际仍按 Token 里的 visitor 判定。"""
    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-spoof", "message": "我要请假 2 天",
                             "role": "admin"},
                       headers=visitor_headers)
    body = data(resp)
    assert body["agent"] == "customer_service", "伪造角色越权未被拦截"


# --------------------------------------------------------------------------- #
# 发给 Dify 的 inputs（学生助手 Chatflow 的起始变量）
#
# 背景：Chatflow 版学生助手把 `role` / `student_id` 声明成了**起始变量**，
# 但前端故意不传 inputs（角色一律以 Token 为准）。不注入的话，分类器照跑，
# 分支里的工具调用却会因为 role 为空被后端 403。
# --------------------------------------------------------------------------- #
class _FakeBody:
    def __init__(self, inputs=None):
        self.inputs = inputs


def _principal(role, ref_id=None, subject="u"):
    from app.api.deps import Principal
    return Principal(subject=subject, role=role, ref_id=ref_id)


def test_dify_inputs_injects_role_and_student_id_for_student():
    from app.api.v1.chat import _dify_inputs
    got = _dify_inputs(_FakeBody(None), _principal("student", 1))
    assert got == {"role": "student", "student_id": "1"}


def test_dify_inputs_overrides_spoofed_role_and_student_id():
    """客户端传的 role / student_id 一律不作数 —— 防伪造越权。"""
    from app.api.v1.chat import _dify_inputs
    got = _dify_inputs(_FakeBody({"role": "admin", "student_id": "999"}),
                       _principal("student", 1))
    assert got["role"] == "student"
    assert got["student_id"] == "1"


def test_dify_inputs_skips_student_id_for_staff():
    """员工的 ref_id 指向 employee，塞进 student_id 是错的，不能注入。"""
    from app.api.v1.chat import _dify_inputs
    got = _dify_inputs(_FakeBody(None), _principal("employee", 1))
    assert got["role"] == "employee"
    assert "student_id" not in got


def test_dify_inputs_keeps_other_client_inputs():
    """其他 Dify 应用可能依赖自定义 inputs，不能被整体清空。"""
    from app.api.v1.chat import _dify_inputs
    got = _dify_inputs(_FakeBody({"foo": "bar"}), _principal("visitor"))
    assert got == {"foo": "bar", "role": "visitor"}


def test_chat_really_passes_injected_inputs_to_dify(client, student_headers, monkeypatch):
    """端到端：注入的 inputs 真的落到了 `dify_client.chat` 的入参上。"""
    from app.api.v1 import chat as chat_api
    from app.services import dify as dify_mod
    from app.services.intent import IntentResult

    seen = {}

    async def fake_route(message, role, user):
        return IntentResult(agent="student_helper", intent="score_query",
                            confidence=0.9, route_source="prefilter")

    async def fake_chat(app, query, user, conversation_id=None, inputs=None):
        seen["app"], seen["inputs"] = app, inputs
        return dify_mod.DifyChatResult(answer="ok", conversation_id="c1")

    monkeypatch.setattr(chat_api, "route", fake_route)
    monkeypatch.setattr(chat_api.dify_client, "chat", fake_chat)
    monkeypatch.setattr(chat_api.settings, "dify_mode", "live")

    resp = client.post("/api/v1/chat/message",
                       json={"session_id": "s-inputs", "message": "我雅思考了多少分"},
                       headers=student_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert seen["app"] == "student_helper"
    assert seen["inputs"]["role"] == "student"
    assert seen["inputs"]["student_id"] == "1"

