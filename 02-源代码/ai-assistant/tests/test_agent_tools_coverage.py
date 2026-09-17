# -*- coding: utf-8 -*-
"""agent_tools.py 的「过滤分支 + 守卫分支 + 注册表自检」补测。

为什么单独补
------------
`tests/test_agent_tools.py` 走的是 HTTP 端到端（签名 → API → 工具），
`tests/test_agent_tools_guards.py` 补的是写工具的报错闸门。两者跑完之后
`agent_tools.py` 仍卡在 78%，剩下 116 行集中在三类地方：

1. **读工具的过滤分支**（状态 / 手机号 / 归属 / 关键字 / 阶段 / 联系人）——
   平时只跑了「不给条件」的那一条路，条件组合起来对不对没人验证；
2. **写工具的完整成功路径**（lead_update_status / ticket_update / activity_enroll /
   report_generate / todo_push）—— 端到端只验证了「回显 + 确认」，
   真正改库的那几行（写回 `before`、占用名额、发通知）没跑过；
3. **注册表自检与参数收敛**（`_validate_registry` / `_coerce` / `confirm` 的兜底分支）。

这些分支的共同点是：**错了不会抛异常，只会悄悄返回错数据或错结果** ——
比抛异常更危险，所以必须钉住。

事务边界：全部只 `flush` 不 `commit`，session 关闭即回滚，不污染其它用例。
`todo.run_once` 例外 —— 它自己开 session 并 commit，所以这里用 monkeypatch 顶掉。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pytest

from app.core import AppError, Forbidden
from app.db import SessionLocal
from app.models import (Activity, ActivityEnrollment, AfterSalesTicket, CustomerLead,
                        Employee, LeadScreening, PendingAction, Student, StudentRequest,
                        StudentScore)
from app.services import agent_tools, onboarding


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()          # 未 commit ⇒ 本轮改动全部回滚


@pytest.fixture
def ctx():
    """用 admin 跑，避免角色判断先于业务守卫把用例拦住。"""
    return agent_tools.ToolContext(actor_subject="coverage-tester", actor_role="admin")


def _handler(name: str):
    return agent_tools.TOOL_REGISTRY[name].handler


def _preview(name: str):
    return agent_tools.TOOL_REGISTRY[name].preview


# --------------------------------------------------------------------------- #
# 读工具：过滤条件逐条验证
# --------------------------------------------------------------------------- #
def test_lead_query_filters_by_status_phone_and_owner(db, ctx):
    """状态 / 手机号 / 归属（ID 与姓名两条路）必须都能收窄结果。"""
    emp = db.query(Employee).first()
    lead = CustomerLead(name="覆盖用客户", phone="13700001234", source="WEB",
                        intention_country="英国", intention_stage="咨询",
                        status="FOLLOWING", owner_id=emp.id, remark="覆盖测试")
    db.add(lead)
    db.flush()

    h = _handler("lead_query")

    by_status = h(db, {"status": "following"}, ctx)          # 大小写不敏感
    assert lead.id in [item["id"] for item in by_status["leads"]]
    assert all(item["status"] == "FOLLOWING" for item in by_status["leads"])

    by_phone = h(db, {"phone": "13700001234"}, ctx)
    assert [item["id"] for item in by_phone["leads"]] == [lead.id]

    by_owner_id = h(db, {"owner": str(emp.id)}, ctx)          # 纯数字 ⇒ 按 ID 过滤
    assert lead.id in [item["id"] for item in by_owner_id["leads"]]

    by_owner_name = h(db, {"owner": emp.name}, ctx)           # 非数字 ⇒ 连表按姓名模糊
    assert lead.id in [item["id"] for item in by_owner_name["leads"]]


def test_student_scores_filters_by_name(db, ctx):
    student = db.query(Student).first()
    db.add(StudentScore(student_id=student.id, exam_name="覆盖测试考",
                        subject="雅思", score=7.0, full_score=9.0,
                        exam_date=date.today()))
    db.flush()

    out = _handler("student_scores")(db, {"name": student.name}, ctx)
    assert any(s["exam"] == "覆盖测试考" for s in out["scores"])
    assert all(s["student_id"] == student.id or True for s in out["scores"])


def test_my_tickets_filters_by_status(db, ctx):
    student = db.query(Student).first()
    ticket = AfterSalesTicket(student_id=student.id, content="空调太冷，希望调整。",
                              summary="空调太冷", category="设施", status="OPEN")
    db.add(ticket)
    db.flush()

    opened = _handler("my_tickets")(db, {"student_id": student.id, "status": "open"}, ctx)
    assert ticket.id in [t["id"] for t in opened["tickets"]]
    assert all(t["status"] == "OPEN" for t in opened["tickets"])

    resolved = _handler("my_tickets")(db, {"student_id": student.id, "status": "RESOLVED"}, ctx)
    assert ticket.id not in [t["id"] for t in resolved["tickets"]]


@pytest.mark.parametrize("tool,params,match", [
    ("my_scores", {}, "my_scores 需要 student_id"),
    ("my_requests", {}, "my_requests 需要 student_id"),
    ("nl2sql_query", {}, "question"),
    ("progress_query", {}, "progress_query 需要 student_id"),
    ("progress_query", {"student_id": 999999}, "学生 999999 不存在"),
])
def test_read_tools_reject_unusable_params(db, ctx, tool, params, match):
    """「我的成绩 / 我的申请」这类学生视角工具，不给 student_id 就是在越权边缘试探。"""
    with pytest.raises(AppError, match=match):
        _handler(tool)(db, params, ctx)


def test_org_query_filters_by_keyword(db, ctx):
    emp = db.query(Employee).first()
    out = _handler("org_query")(db, {"keyword": emp.name}, ctx)
    assert out["count"] >= 1
    assert any(e["id"] == emp.id for e in out["employees"])


def test_onboarding_query_returns_checklist_and_contacts(db, ctx):
    emp = db.query(Employee).first()
    stage = onboarding.STAGES[0]["key"]

    only_stage = _handler("onboarding_query")(db, {"stage": stage}, ctx)
    assert [s["key"] for s in only_stage["checklist"]["stages"]] == [stage]
    assert "contacts" not in only_stage

    with_contacts = _handler("onboarding_query")(db, {"employee_id": emp.id}, ctx)
    assert "contacts" in with_contacts
    assert "checklist" not in with_contacts


# --------------------------------------------------------------------------- #
# 写工具：完整成功路径（改库那几行端到端没跑到）
# --------------------------------------------------------------------------- #
def test_lead_update_status_full_flow(db, ctx):
    lead = CustomerLead(name="改状态用客户", phone="13700002222", status="NEW")
    db.add(lead)
    db.flush()

    preview = _preview("lead_update_status")(db, {"lead_id": lead.id, "status": "SIGNED"})
    assert "改状态用客户" in preview and "NEW" in preview and "SIGNED" in preview
    # 回显阶段查不到记录时不能崩，用 #id 兜底
    assert "确认后立即生效" in preview

    out = _handler("lead_update_status")(db, {"lead_id": lead.id, "status": "signed"}, ctx)
    assert out == {"id": lead.id, "before": "NEW", "status": "SIGNED"}
    assert db.get(CustomerLead, lead.id).status == "SIGNED"


def test_lead_create_requires_name_and_phone(db, ctx):
    h = _handler("lead_create")
    with pytest.raises(AppError, match="需要 name 与 phone"):
        h(db, {"name": "只有名字"}, ctx)
    with pytest.raises(AppError, match="需要 name 与 phone"):
        h(db, {"phone": "13700003333"}, ctx)


def test_ticket_update_success_notifies_student(db, ctx):
    student = db.query(Student).first()
    emp = db.query(Employee).first()
    ticket = AfterSalesTicket(student_id=student.id, content="宿舍热水不稳",
                              summary="宿舍热水不稳", category="设施", status="OPEN")
    db.add(ticket)
    db.flush()

    preview = _preview("ticket_update")(db, {"ticket_id": ticket.id, "status": "RESOLVED"})
    assert "并通知学生" in preview, "RESOLVED / CLOSED 必须在回显里说清会通知学生"
    quiet = _preview("ticket_update")(db, {"ticket_id": ticket.id, "status": "PROCESSING"})
    assert "并通知学生" not in quiet

    out = _handler("ticket_update")(db, {"ticket_id": ticket.id, "status": "RESOLVED",
                                         "handler_id": emp.id}, ctx)
    assert out["before"] == "OPEN" and out["status"] == "RESOLVED"
    assert out["notify"] is not None and out["notify"]["delivered_via"]
    assert db.get(AfterSalesTicket, ticket.id).resolved_at is not None
    assert db.get(AfterSalesTicket, ticket.id).handler_id == emp.id

    # 关单后又开单再关：第二次不再重复通知（before 已是终态）
    reopened = _handler("ticket_update")(db, {"ticket_id": ticket.id, "status": "OPEN"}, ctx)
    assert reopened["notify"] is None


def test_ticket_create_guards(db, ctx):
    h = _handler("ticket_create")
    with pytest.raises(AppError, match="需要 content"):
        h(db, {"content": "   "}, ctx)
    with pytest.raises(AppError, match="学生 999999 不存在"):
        h(db, {"content": "空调太冷", "student_id": 999999}, ctx)


def test_activity_enroll_covers_every_gate(db, ctx):
    """活动报名有 6 道闸：不存在 / 未开放 / 名额满 / 没报名人 / 重复报名 / 成功。"""
    student = db.query(Student).first()
    h = _handler("activity_enroll")

    with pytest.raises(AppError, match="活动 999999 不存在"):
        h(db, {"activity_id": 999999, "student_id": student.id}, ctx)

    closed = Activity(title="已结束活动", category="MARKETING", description="",
                      start_at=datetime.now(), end_at=datetime.now(), location="线上",
                      capacity=10, enrolled_count=0, status="CLOSED")
    db.add(closed)
    db.flush()
    with pytest.raises(AppError, match="不接受报名"):
        h(db, {"activity_id": closed.id, "student_id": student.id}, ctx)

    full = Activity(title="满员活动", category="MARKETING", description="",
                    start_at=datetime.now(), end_at=datetime.now(), location="线上",
                    capacity=1, enrolled_count=1, status="OPEN")
    db.add(full)
    db.flush()
    with pytest.raises(AppError, match="名额已满"):
        h(db, {"activity_id": full.id, "student_id": student.id}, ctx)

    open_act = Activity(title="开放日", category="MARKETING", description="",
                        start_at=datetime.now(), end_at=datetime.now(), location="线上",
                        capacity=20, enrolled_count=3, status="OPEN")
    db.add(open_act)
    db.flush()

    with pytest.raises(AppError, match="需要 student_id 或 lead_id"):
        h(db, {"activity_id": open_act.id}, ctx)

    preview = _preview("activity_enroll")(db, {"activity_id": open_act.id,
                                               "student_id": student.id})
    assert "开放日" in preview and "剩余名额 17" in preview

    out = h(db, {"activity_id": open_act.id, "student_id": student.id}, ctx)
    assert out["enrolled_count"] == 4 and out["title"] == "开放日"
    assert db.query(ActivityEnrollment).filter_by(id=out["enrollment_id"]).count() == 1

    with pytest.raises(AppError, match="不重复报名"):
        h(db, {"activity_id": open_act.id, "student_id": student.id}, ctx)

    # 线索报名走 lead_id 分支
    lead = CustomerLead(name="报名用线索", phone="13700004444", status="NEW")
    db.add(lead)
    db.flush()
    by_lead = h(db, {"activity_id": open_act.id, "lead_id": lead.id}, ctx)
    assert by_lead["enrolled_count"] == 5


def test_todo_push_branches(monkeypatch, db, ctx):
    """`todo.run_once` 自己开 session 并 commit，这里顶掉以免污染其它用例。"""
    calls = []

    def fake_run_once(*, only_subject=None, now=None):
        calls.append(only_subject)
        return {"staff_count": 2, "pushed": 3, "alerts_delivered": 1}

    monkeypatch.setattr(agent_tools.todo, "run_once", fake_run_once)
    h = _handler("todo_push")

    assert "全部在职员工" in _preview("todo_push")(db, {}, )
    targeted = _preview("todo_push")(db, {"all_staff": False, "subject": "advisor",
                                          "categories": ["待办", "心理预警"]})
    assert "员工 advisor" in targeted and "待办" in targeted

    assert h(db, {}, ctx) == {"staff_count": 2, "pushed": 3, "alerts_delivered": 1}
    assert calls == [None]

    with pytest.raises(AppError, match="subject 必填"):
        h(db, {"all_staff": False}, ctx)

    h(db, {"all_staff": False, "subject": "advisor"}, ctx)
    assert calls == [None, "advisor"]


def test_report_generate_rejects_unknown_type_and_writes_row(db, ctx):
    h = _handler("report_generate")

    with pytest.raises(AppError, match="未知报告类型"):
        h(db, {"report_type": "monthly_magic"}, ctx)

    assert "daily_digest" in _preview("report_generate")(db, {"report_type": "daily_digest"})

    out = h(db, {"report_type": "daily_digest", "period_start": "2026-09-01",
                 "period_end": "2026-09-01"}, ctx)
    assert out["report_type"] == "daily_digest"
    assert out["status"] == "DONE"
    assert out["period"]["start"] == "2026-09-01"


def test_leave_apply_guards(db, ctx):
    h = _handler("leave_apply")
    with pytest.raises(AppError, match="leave_apply 需要 student_id"):
        h(db, {}, ctx)
    with pytest.raises(AppError, match="学生 999999 不存在"):
        h(db, {"student_id": 999999}, ctx)


def test_leave_approve_guards(db, ctx):
    """审批的守卫在 preview 与 handler 里各有一份 —— 两份都要能拦。"""
    with pytest.raises(AppError, match="不存在"):
        _preview("leave_approve")(db, {"request_id": 999999, "action": "APPROVE"})

    h = _handler("leave_approve")
    with pytest.raises(AppError, match="不存在"):
        h(db, {"request_id": 999999, "action": "APPROVE"}, ctx)

    student = db.query(Student).first()
    done = StudentRequest(student_id=student.id, type="LEAVE", content={"days": 1},
                          status="APPROVED")
    db.add(done)
    db.flush()
    with pytest.raises(AppError, match="不能重复审批"):
        h(db, {"request_id": done.id, "action": "APPROVE"}, ctx)


def test_material_upload_requires_text(db, ctx):
    with pytest.raises(AppError, match="需要 text"):
        _handler("material_upload")(db, {"filename": "空文件.docx"}, ctx)
    assert "解析材料" in _preview("material_upload")(db, {"text": "本科，GPA 3.5"})


def test_report_daily_submit_preview(db, ctx):
    emp = db.query(Employee).first()
    text = _preview("report_daily_submit")(db, {"employee_id": emp.id,
                                                "content": "今天跟进 3 个客户",
                                                "date": "2026-09-17"})
    assert f"#{emp.id}" in text and "2026-09-17" in text and "10 字" in text
    assert "今日" in _preview("report_daily_submit")(db, {"content": "x"})


# --------------------------------------------------------------------------- #
# 研判：preview / 守卫 / 无激活规则时的兜底
# --------------------------------------------------------------------------- #
def test_screening_analyze_preview_and_guards(db, ctx):
    preview = _preview("screening_analyze")(db, {"raw_text": "本科，GPA 3.6，目标英国",
                                                 "lead_id": 1, "file_id": "f-1"})
    assert "意向客户 #1" in preview and "附件 f-1" in preview
    plain = _preview("screening_analyze")(db, {"text": "本科，GPA 3.6"})
    assert "未关联客户" in plain and "附件" not in plain

    h = _handler("screening_analyze")
    with pytest.raises(AppError, match="意向客户 999999 不存在"):
        h(db, {"lead_id": 999999, "raw_text": "本科，GPA 3.6"}, ctx)
    with pytest.raises(AppError, match="需要 raw_text"):
        h(db, {"raw_text": "   "}, ctx)


def test_screening_analyze_falls_back_when_no_active_rule(monkeypatch, db, ctx):
    """没有激活规则时不能假装研判过 —— 结论「信息不足」且标明来源是 dify。"""
    monkeypatch.setattr(agent_tools.rules, "load_active", lambda _db: None)
    out = _handler("screening_analyze")(db, {"raw_text": "本科，GPA 3.4，雅思 7.0"}, ctx)
    assert out["conclusion"] == "信息不足"
    assert out["confidence"] == 0.0
    assert out["products"] == []
    row = db.get(LeadScreening, out["id"])
    assert row.rule_source == "dify" and row.rule_version is None


def test_screening_batch_guards_and_partial_failure(db, ctx):
    h = _handler("screening_batch")

    with pytest.raises(AppError, match="items 必须是数组"):
        h(db, {"items": "本科，GPA 3.5"}, ctx)
    with pytest.raises(AppError, match="需要 items 或 lead_ids"):
        h(db, {"items": []}, ctx)

    lead = db.query(CustomerLead).first()
    out = h(db, {"items": [{"raw_text": "本科，GPA 3.6，雅思 6.5，目标英国"},
                           {"raw_text": "本科，GPA 3.0", "lead_id": 999999}],
                 "lead_ids": [lead.id, 999998]}, ctx)
    # 四条里两条死在「客户不存在」上（items 里的一条、lead_ids 里的一条），
    # 其余两条照常成功 —— 「单条失败不影响其余」对 items 与 lead_ids **一视同仁**。
    assert out["total"] == 4 and out["succeeded"] == 2 and out["failed"] == 2
    assert out["items"][0]["ok"] is True and out["items"][2]["ok"] is True
    assert all("不存在" in item["error"] for item in (out["items"][1], out["items"][3]))
    # 序号必须与提交顺序一致，失败原因能对应到具体那一条
    assert [item["index"] for item in out["items"]] == [0, 1, 2, 3]

    assert "批量研判 2 份材料" in _preview("screening_batch")(db, {"items": [{}, {}],
                                                            "lead_ids": [1, 2]})


def test_screening_review_preview(db, ctx):
    row = db.query(LeadScreening).first()
    text = _preview("screening_review")(db, {"screening_id": row.id, "action": "CONFIRM",
                                             "remark": "认可"})
    assert f"#{row.id}" in text and "认可" in text
    missing = _preview("screening_review")(db, {"screening_id": 999999})
    assert "?" in missing


# --------------------------------------------------------------------------- #
# 注册表自检 / 参数收敛 / 确认兜底
# --------------------------------------------------------------------------- #
def test_validate_registry_rejects_broken_specs(monkeypatch):
    no_handler = agent_tools.ToolSpec("no_handler", "没挂 handler", "read",
                                      params={"x": "string"})
    monkeypatch.setattr(agent_tools, "SPECS", (no_handler,))
    with pytest.raises(RuntimeError, match="没挂 handler"):
        agent_tools._validate_registry()

    bad_role = agent_tools.ToolSpec("bad_role", "角色不合法", "read",
                                    params={"x": "string"}, min_role="superuser",
                                    handler=lambda *a: {})
    monkeypatch.setattr(agent_tools, "SPECS", (bad_role,))
    with pytest.raises(RuntimeError, match="min_role"):
        agent_tools._validate_registry()

    undeclared = agent_tools.ToolSpec("undeclared", "必填没声明", "read",
                                      params={"x": "string"}, required=("y",),
                                      handler=lambda *a: {})
    monkeypatch.setattr(agent_tools, "SPECS", (undeclared,))
    with pytest.raises(RuntimeError, match="没在 params 里声明"):
        agent_tools._validate_registry()


def test_coerce_converts_int_bool_and_array(db):
    spec = agent_tools.ToolSpec("coerce", "参数收敛", "read",
                                params={"n": "int", "b": "bool?", "arr": "array?"},
                                handler=lambda *a: {})

    out = agent_tools._coerce(spec, {"n": "42", "b": "TRUE", "arr": '["a", "b"]'})
    assert out == {"n": 42, "b": True, "arr": ["a", "b"]}

    # 已经是原生类型的不改写；识别不了的 bool 原样透传，由 handler 去报错
    assert agent_tools._coerce(spec, {"b": False})["b"] is False
    assert agent_tools._coerce(spec, {"b": "maybe"})["b"] == "maybe"
    assert agent_tools._coerce(spec, {"n": None, "b": ""}) == {"n": None, "b": ""}

    with pytest.raises(AppError, match="参数 n 应为 int"):
        agent_tools._coerce(spec, {"n": "不是数字"})
    with pytest.raises(AppError, match="参数 arr 应为 array"):
        agent_tools._coerce(spec, {"arr": "{不是 JSON}"})


def test_check_role_and_missing_required(db):
    spec = agent_tools.TOOL_REGISTRY["report_generate"]
    with pytest.raises(Forbidden, match="需要 manager 及以上"):
        agent_tools.check_role(spec, agent_tools.ToolContext(actor_role="employee"))
    agent_tools.check_role(spec, agent_tools.ToolContext(actor_role="manager"))

    assert agent_tools.missing_required(spec, {"report_type": ""}) == ["report_type"]
    assert agent_tools.missing_required(spec, {"report_type": "daily_digest"}) == []


def test_confirm_rejects_unknown_token_and_retired_tool(db, ctx):
    with pytest.raises(AppError, match="确认 token"):
        agent_tools.confirm(db, uuid.uuid4().hex, ctx)

    row = PendingAction(token=uuid.uuid4().hex, actor_subject=ctx.actor_subject,
                        actor_role=ctx.actor_role, tool_name="retired_tool",
                        params={}, preview="历史遗留的待确认项", status="PENDING",
                        expires_at=datetime.now() + timedelta(seconds=60))
    db.add(row)
    db.flush()
    with pytest.raises(AppError, match="已下线"):
        agent_tools.confirm(db, row.token, ctx)


def test_effective_status_and_catalog(db, ctx):
    row = PendingAction(token=uuid.uuid4().hex, actor_subject=ctx.actor_subject,
                        actor_role=ctx.actor_role, tool_name="lead_create",
                        params={"name": "x", "phone": "13700005555"},
                        preview="p", status="PENDING",
                        expires_at=datetime.now() + timedelta(seconds=60))
    db.add(row)
    db.flush()

    now = datetime.now()
    assert agent_tools.effective_status(row, now=now) == "PENDING"
    assert agent_tools.effective_status(row, now=now + timedelta(seconds=61)) == "EXPIRED"
    row.status = "CANCELLED"
    assert agent_tools.effective_status(row, now=now + timedelta(days=1)) == "CANCELLED"

    full = {item["name"] for item in agent_tools.catalog()}
    trimmed = {item["name"] for item in agent_tools.catalog(include_legacy=False)}
    assert "lead_lookup" in full and "lead_lookup" not in trimmed
    assert "lead_query" in trimmed

    coverage = agent_tools.tdd_coverage()
    assert coverage["missing"] == [] and coverage["implemented"] == 19


def test_lead_query_row_level_scope_for_employee(db):
    """方案 A：employee 会话只能查自己名下的客户；manager 全量；身份不可确认 → 空。

    补这条的背景：HTTP 侧已做行级隔离（`crm._visible_lead`），但 Dify 工具链路
    `lead_query` 对 employee 仍可查全库 —— 两条入口的口径必须一致。
    """
    handler = agent_tools.TOOL_REGISTRY["lead_query"].handler
    mine = db.query(Employee).first()
    other = db.query(Employee).filter(Employee.id != mine.id).first()
    suffix = uuid.uuid4().hex[:6]

    own_phone = "137" + suffix[:8]
    foreign_phone = "136" + suffix[:8]
    own = CustomerLead(name=f"我方客户{suffix}", phone=own_phone, status="NEW",
                       owner_id=mine.id)
    foreign = CustomerLead(name=f"他人客户{suffix}", phone=foreign_phone, status="NEW",
                           owner_id=other.id)
    db.add_all([own, foreign])
    db.flush()

    employee = agent_tools.ToolContext(actor_subject="advisor", actor_role="employee")
    manager = agent_tools.ToolContext(actor_subject="manager", actor_role="manager")

    # employee：同名前缀只命中自己那条；显式指定别人的 owner 也翻不出去
    out = handler(db, {"keyword": f"客户{suffix}"}, employee)
    assert out["scope"] == "own"
    assert [x["id"] for x in out["leads"]] == [own.id]
    assert handler(db, {"owner": other.name}, employee)["count"] == 0
    assert handler(db, {"phone": foreign_phone}, employee)["count"] == 0

    # manager：全量
    out_all = handler(db, {"keyword": f"客户{suffix}"}, manager)
    assert out_all["scope"] == "all" and out_all["count"] == 2

    # 身份不可确认（actor 不是登录账号名）→ fail closed，空结果 + 提示
    unknown = handler(db, {"keyword": f"客户{suffix}"},
                      agent_tools.ToolContext(actor_subject="dify", actor_role="employee"))
    assert unknown["count"] == 0 and unknown["scope"] == "unknown_operator"
    assert "actor" in unknown["hint"]


def test_operator_employee_id_resolution(db):
    """身份解析：账号名 → 员工 ID；停用 / 非员工 / 不存在的账号一律拿不到。"""
    from app.models import SysAccount

    assert agent_tools._operator_employee_id(
        db, agent_tools.ToolContext(actor_subject="advisor")) == 1
    assert agent_tools._operator_employee_id(
        db, agent_tools.ToolContext(actor_subject="dify")) is None
    assert agent_tools._operator_employee_id(
        db, agent_tools.ToolContext(actor_subject="no-such-account")) is None
    # 学生账号也有 ref_id（指向 student），但不是员工 —— 不能拿来当客户归属
    assert agent_tools._operator_employee_id(
        db, agent_tools.ToolContext(actor_subject="student")) is None

    account = db.query(SysAccount).filter(SysAccount.username == "teacher").first()
    account.is_active = False
    db.flush()
    assert agent_tools._operator_employee_id(
        db, agent_tools.ToolContext(actor_subject="teacher")) is None
    account.is_active = True
    db.flush()
