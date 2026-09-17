"""TDD 4.5 工具集（19 个）+ 写操作二次确认（P0-2）。

对应的查验缺口：TDD 声明 19 个工具，实际只暴露了 4 个只读接口；
且「写类工具执行前回显确认」这条执行保障完全没有落地。

这里要证明的几件事：
1. 19 个工具逐个都在注册表里（不是「写了个空壳」，handler 与 schema 都要有）；
2. 读工具直接执行，写工具**第一次绝不落业务数据**，只回显 + 发 token；
3. 带 token 确认后才真执行，且**只能由发起人确认**、过期/取消后不能执行；
4. 角色不够时在回显阶段就被拦掉（不能等到确认了才发现越权）。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta

import pytest

from app.core import sign_tool_payload
from app.db import SessionLocal
from app.models import AfterSalesTicket, PendingAction, StudentRequest
from app.services import agent_tools
from tests.conftest import data

BASE = "/internal/tools"


def call(client, name: str, params: dict, **extra):
    """带 HMAC 签名的工具调用；`extra` 里的 role / actor / confirm 放在顶层。"""
    payload = {"params": params, **extra}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timestamp = str(time.time())
    headers = {
        "content-type": "application/json",
        "x-tool-timestamp": timestamp,
        "x-tool-signature": sign_tool_payload(body, timestamp),
    }
    return client.post(f"{BASE}/{name}", content=body, headers=headers)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 注册表：19 个工具都在，且都真的可用
# --------------------------------------------------------------------------- #
def test_catalog_covers_all_tdd_tools(client):
    body = data(client.get(BASE))
    names = {item["name"] for item in body["items"]}
    assert set(agent_tools.TDD_TOOL_NAMES) <= names
    assert body["coverage"] == {"declared": 19, "implemented": 19, "missing": [],
                                "total_registered": len(agent_tools.TOOL_REGISTRY)}
    assert body["counts"]["read"] + body["counts"]["write"] == body["counts"]["total"]
    assert body["counts"]["write"] >= 14, "TDD 里的写类工具占多数，不该只剩只读"


def test_every_tool_declares_schema_and_kind(client):
    items = data(client.get(BASE))["items"]
    for item in items:
        assert item["x-kind"] in ("read", "write")
        assert item["x-min-role"] in agent_tools.ROLE_RANK
        params = item["parameters"]
        assert params["type"] == "object"
        # 必填参数必须在 properties 里声明，否则调用方无从知道要传什么
        for key in params["required"]:
            assert key in params["properties"], f"{item['name']} 的必填 {key} 没声明"
        assert item["description"]


def test_write_tools_always_have_preview():
    """写工具给不出人话回显 → 注册期就该炸，而不是上线后让用户确认一坨 JSON。"""
    for spec in agent_tools.TOOL_REGISTRY.values():
        if spec.is_write:
            assert spec.preview is not None, f"写工具 {spec.name} 缺 preview"
    assert agent_tools.TOOL_REGISTRY["leave_approve"].is_write


def test_registry_rejects_write_without_preview(monkeypatch):
    bad = agent_tools.ToolSpec("bad_write", "缺少回显的写工具", "write",
                               params={"x": "string"}, handler=lambda *a: {})
    monkeypatch.setattr(agent_tools, "SPECS", (bad,))
    with pytest.raises(RuntimeError, match="preview"):
        agent_tools._validate_registry()


# --------------------------------------------------------------------------- #
# 读工具：直接执行
# --------------------------------------------------------------------------- #
def test_read_tool_executes_directly(client):
    body = data(call(client, "progress_query", {"student_id": 1}))
    assert body["student_id"] == 1
    assert body["stage"]
    assert len(body["timeline"]) == 5
    assert "board" in body and "deadlines" in body


def test_org_and_onboarding_tools(client):
    org = data(call(client, "org_query", {"department": "顾问部"}))
    assert org["count"] >= 1
    assert all("顾问部" in e["department"] for e in org["employees"])
    by_role = data(call(client, "org_query", {"biz_role": "manager"}))
    assert by_role["count"] >= 1
    assert all(e["biz_role"] == "manager" for e in by_role["employees"])
    ob = data(call(client, "onboarding_query", {}))
    assert "guide" in ob and "contacts" in ob
    faq = data(call(client, "onboarding_query", {"keyword": "报销"}))
    assert "faq" in faq


# --------------------------------------------------------------------------- #
# 写工具：第一次只回显，确认后才执行
# --------------------------------------------------------------------------- #
def test_write_tool_does_not_write_until_confirmed(client, db):
    before = db.query(AfterSalesTicket).count()
    preview = data(call(client, "ticket_create",
                        {"content": "图书馆空调太冷，希望调整。", "category": "设施"}))

    assert preview["kind"] == "write"
    assert preview["needs_confirm"] is True and preview["executed"] is False
    assert preview["token"] and preview["expires_at"]
    assert "售后工单" in preview["preview"] and "图书馆空调" in preview["preview"]

    db.expire_all()
    assert db.query(AfterSalesTicket).count() == before, "回显阶段绝不能落业务数据"

    done = data(call(client, "ticket_create",
                     {"content": "图书馆空调太冷，希望调整。", "category": "设施"},
                     confirm=preview["token"]))
    assert done["executed"] is True and done["needs_confirm"] is False
    assert done["result"]["id"]

    db.expire_all()
    assert db.query(AfterSalesTicket).count() == before + 1


def test_confirm_token_cannot_be_reused(client):
    preview = data(call(client, "ticket_create", {"content": "重复确认验证"}))
    token = preview["token"]
    data(call(client, "ticket_create", {"content": "重复确认验证"}, confirm=token))

    again = call(client, "ticket_create", {"content": "重复确认验证"}, confirm=token)
    assert again.status_code == 400
    assert "CONFIRMED" in again.json()["message"]


def test_expired_token_is_rejected(client, db):
    preview = data(call(client, "ticket_create", {"content": "过期验证"}))
    token = preview["token"]

    row = agent_tools.pending_of(db, token)
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()

    resp = call(client, "ticket_create", {"content": "过期验证"}, confirm=token)
    assert resp.status_code == 400
    assert "超时" in resp.json()["message"]

    # 「超时」由时间推导，不落库 —— 落库字段仍只有用户真正的动作
    db.expire_all()
    stored = db.get(PendingAction, row.id)
    assert stored.status == "PENDING"
    assert agent_tools.effective_status(stored) == "EXPIRED"


def test_only_initiator_can_confirm(client):
    preview = data(call(client, "ticket_create", {"content": "越权确认验证"},
                        actor="advisor-a"))
    resp = call(client, "ticket_create", {"content": "越权确认验证"},
                confirm=preview["token"], actor="advisor-b")
    assert resp.status_code == 403
    assert "只能确认自己发起的操作" in resp.json()["message"]

    # 本人确认仍然可以
    mine = data(call(client, "ticket_create", {"content": "越权确认验证"},
                     confirm=preview["token"], actor="advisor-a"))
    assert mine["executed"] is True


def test_cancel_token_blocks_later_confirmation(client, db):
    """用户看过回显后反悔：撤掉 token，之后不能再拿它执行（否则 token 一直悬挂到过期）。"""
    preview = data(call(client, "ticket_create", {"content": "撤回验证"}))
    token = preview["token"]

    cancelled = data(call(client, "ticket_create", {}, cancel=token))
    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["executed"] is False

    db.expire_all()
    row = agent_tools.pending_of(db, token)
    assert row.status == "CANCELLED"
    assert row.confirmed_at is None, "撤回不该写 confirmed_at"

    again = call(client, "ticket_create", {"content": "撤回验证"}, confirm=token)
    assert again.status_code == 400
    assert "CANCELLED" in again.json()["message"]


def test_only_initiator_can_cancel(client):
    preview = data(call(client, "ticket_create", {"content": "撤回归属验证"},
                        actor="advisor-a"))
    token = preview["token"]

    denied = call(client, "ticket_create", {}, cancel=token, actor="advisor-b")
    assert denied.status_code == 403
    assert "只能取消自己发起的操作" in denied.json()["message"]

    mine = data(call(client, "ticket_create", {}, cancel=token, actor="advisor-a"))
    assert mine["status"] == "CANCELLED"


def test_cancel_unknown_token_is_rejected(client):
    resp = call(client, "ticket_create", {}, cancel=uuid.uuid4().hex)
    assert resp.status_code == 400
    assert "不存在" in resp.json()["message"]


def test_cancel_confirmed_token_is_rejected(client):
    preview = data(call(client, "ticket_create", {"content": "已确认后撤回验证"}))
    token = preview["token"]
    data(call(client, "ticket_create", {"content": "已确认后撤回验证"}, confirm=token))

    resp = call(client, "ticket_create", {}, cancel=token)
    assert resp.status_code == 400
    assert "CONFIRMED" in resp.json()["message"]


def test_dry_run_previews_without_pending_record(client, db):
    before = db.query(PendingAction).count()
    body = data(call(client, "lead_create",
                     {"name": "干跑客户", "phone": "1380000" + uuid.uuid4().hex[:4]},
                     dry_run=True))
    assert body["needs_confirm"] is True and body.get("token") is None
    assert "干跑客户" in body["preview"]
    db.expire_all()
    assert db.query(PendingAction).count() == before, "dry_run 不该留下待确认记录"


def test_missing_required_params_returns_400(client):
    resp = call(client, "lead_create", {"name": "只有名字"})
    assert resp.status_code == 400
    assert "phone" in resp.json()["message"]


# --------------------------------------------------------------------------- #
# 角色越权：在回显阶段就拦掉
# --------------------------------------------------------------------------- #
def test_min_role_is_enforced_before_confirmation(client):
    denied = call(client, "report_generate", {"report_type": "daily_digest"}, role="employee")
    assert denied.status_code == 403
    assert "manager" in denied.json()["message"]

    allowed = call(client, "report_generate", {"report_type": "daily_digest"}, role="manager")
    assert allowed.status_code == 200
    assert data(allowed)["needs_confirm"] is True


# --------------------------------------------------------------------------- #
# 端到端：请假申请 → 审批 → 通知申请学生
# --------------------------------------------------------------------------- #
def test_leave_apply_then_approve_notifies_student(client, db):
    applied = data(call(client, "leave_apply",
                        {"student_id": 1, "start_date": "2026-11-02", "days": 2,
                         "reason": "家中临时有事"}, actor="advisor"),
                   )
    assert applied["needs_confirm"] is True
    assert "请假" in applied["preview"] and "2 天" in applied["preview"]

    created = data(call(client, "leave_apply",
                        {"student_id": 1, "start_date": "2026-11-02", "days": 2,
                         "reason": "家中临时有事"}, confirm=applied["token"], actor="advisor"))
    request_id = created["result"]["id"]

    db.expire_all()
    row = db.get(StudentRequest, request_id)
    assert row.status == "PENDING" and row.type == "LEAVE"

    approve_preview = data(call(client, "leave_approve",
                                {"request_id": request_id, "action": "APPROVE",
                                 "remark": "已核实"}, actor="manager", role="manager"))
    assert "不可撤销" in approve_preview["preview"]

    approved = data(call(client, "leave_approve",
                         {"request_id": request_id, "action": "APPROVE", "remark": "已核实"},
                         confirm=approve_preview["token"], actor="manager", role="manager"))
    assert approved["result"]["status"] == "APPROVED"
    assert approved["result"]["notify"]["delivered_via"] == "internal"

    # 重复审批被拦
    again = call(client, "leave_approve", {"request_id": request_id, "action": "APPROVE"},
                 actor="manager", role="manager")
    assert again.status_code == 400
    assert "重复审批" in again.json()["message"]


def test_material_upload_extracts_fields_without_writing(client, db):
    preview = data(call(client, "material_upload",
                        {"text": "姓名 钱九，本科 211，GPA 3.4/4.0，雅思 7.0，目标英国",
                         "filename": "钱九.docx"}))
    assert "解析材料" in preview["preview"]
    body = data(call(client, "material_upload",
                     {"text": "姓名 钱九，本科 211，GPA 3.4/4.0，雅思 7.0，目标英国",
                      "filename": "钱九.docx"}, confirm=preview["token"]))
    fields = body["result"]["fields"]
    assert fields["degree"] == "本科"
    assert "gpa" in fields and "language" in fields


def test_batch_screening_can_target_lead_ids(client, db):
    from app.models import LeadScreening

    before = db.query(LeadScreening).count()
    preview = data(call(client, "screening_batch",
                        {"items": [{"raw_text": "本科，GPA 3.6/4.0，雅思 6.5，目标英国"},
                                   {"raw_text": "想出国读书"}],
                         "lead_ids": [1]}))
    body = data(call(client, "screening_batch",
                     {"items": [{"raw_text": "本科，GPA 3.6/4.0，雅思 6.5，目标英国"},
                                {"raw_text": "想出国读书"}],
                      "lead_ids": [1]}, confirm=preview["token"]))
    result = body["result"]
    assert result["succeeded"] == 3 and result["failed"] == 0
    assert all(item["ok"] for item in result["items"])

    db.expire_all()
    assert db.query(LeadScreening).count() == before + 3


def test_duplicate_lead_phone_is_rejected_on_confirm(client):
    phone = "139" + uuid.uuid4().hex[:8]
    first = data(call(client, "lead_create", {"name": "重复验证甲", "phone": phone}))
    data(call(client, "lead_create", {"name": "重复验证甲", "phone": phone},
              confirm=first["token"]))

    second = data(call(client, "lead_create", {"name": "重复验证乙", "phone": phone}))
    resp = call(client, "lead_create", {"name": "重复验证乙", "phone": phone},
                confirm=second["token"])
    assert resp.status_code == 400
    assert "已存在" in resp.json()["message"]
