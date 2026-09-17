# -*- coding: utf-8 -*-
"""写工具的「守卫分支」单测 —— 全是「宁可 400，也不写一条脏数据」的闸门。

为什么单独补：这些 handler 的**正常路径**已被 tests/test_agent_tools.py 的端到端用例覆盖，
但报错分支（记录不存在 / 枚举值非法）平时一条都没跑到，agent_tools.py 的覆盖率因此卡在 69%。
而这些分支恰恰最该钉住 —— 它们存在的唯一意义就是拦住坏输入。

实现方式：直接调 `ToolSpec.handler`（跳过 preview / confirm 的传输层），
只 `flush` 不 `commit`，退出时随 session 关闭自动回滚，不污染其它用例的数据。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.core import AppError
from app.db import SessionLocal
from app.models import AfterSalesTicket, CustomerLead, Employee, LeadScreening
from app.services import agent_tools


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
    return agent_tools.ToolContext(actor_subject="guard-tester", actor_role="admin")


def _handler(name: str):
    return agent_tools.TOOL_REGISTRY[name].handler


# --------------------------------------------------------------------------- #
# 研判复核：不存在 / action 非法 / OVERRIDE 缺结论
# --------------------------------------------------------------------------- #
def test_screening_review_guards(db, ctx):
    h = _handler("screening_review")

    with pytest.raises(AppError, match="不存在"):
        h(db, {"screening_id": 999999, "action": "CONFIRM"}, ctx)

    sid = db.query(LeadScreening).first().id

    with pytest.raises(AppError, match="action 只能是"):
        h(db, {"screening_id": sid, "action": "BOGUS"}, ctx)

    # 「推翻但不说改成什么」在业务上没意义，必须拦住
    with pytest.raises(AppError, match="OVERRIDE"):
        h(db, {"screening_id": sid, "action": "OVERRIDE", "conclusion": "瞎写"}, ctx)

    out = h(db, {"screening_id": sid, "action": "OVERRIDE",
                 "conclusion": "不符合", "remark": "人工复核"}, ctx)
    assert out["action"] == "OVERRIDE"
    assert out["review_status"] == "OVERRIDDEN"
    assert out["conclusion"] == "不符合"


# --------------------------------------------------------------------------- #
# 日报提交：缺人 / 人不存在 / 正文为空 → 都不落库
# --------------------------------------------------------------------------- #
def test_report_daily_submit_guards(db, ctx):
    h = _handler("report_daily_submit")

    with pytest.raises(AppError, match="employee_id"):
        h(db, {"employee_id": "", "content": "今天跟进 3 个客户"}, ctx)

    with pytest.raises(AppError, match="不存在"):
        h(db, {"employee_id": 999999, "content": "今天跟进 3 个客户"}, ctx)

    emp = db.query(Employee).first()
    with pytest.raises(AppError, match="content"):
        h(db, {"employee_id": emp.id, "content": "   "}, ctx)

    out = h(db, {"employee_id": emp.id, "content": "今天跟进 3 个客户", "date": "2026-09-17"}, ctx)
    assert out["employee_id"] == emp.id
    assert out["report_date"] == "2026-09-17"

    # 不传 date 时回落到今天
    out2 = h(db, {"employee_id": emp.id, "content": "未指定日期"}, ctx)
    assert out2["report_date"] == date.today().isoformat()


# --------------------------------------------------------------------------- #
# 线索改状态 / 工单改状态：不存在 + 枚举非法
# --------------------------------------------------------------------------- #
def test_lead_update_status_guards(db, ctx):
    h = _handler("lead_update_status")

    with pytest.raises(AppError, match="不存在"):
        h(db, {"lead_id": 999999, "status": "SIGNED"}, ctx)

    lead_id = db.query(CustomerLead).first().id
    with pytest.raises(AppError, match="status 只能是"):
        h(db, {"lead_id": lead_id, "status": "瞎写"}, ctx)


def test_ticket_update_guards(db, ctx):
    h = _handler("ticket_update")

    with pytest.raises(AppError, match="不存在"):
        h(db, {"ticket_id": 999999, "status": "CLOSED"}, ctx)

    tid = db.query(AfterSalesTicket).first().id
    with pytest.raises(AppError, match="status 只能是"):
        h(db, {"ticket_id": tid, "status": "瞎写"}, ctx)
