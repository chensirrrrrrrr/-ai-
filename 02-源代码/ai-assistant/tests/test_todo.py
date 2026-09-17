# -*- coding: utf-8 -*-
"""主动待办推送 + 心理预警触达。

覆盖两条客户需求：
- 企业助手「主动待办推送：检测到待处理事项时主动询问员工，并直接反馈查询结果」；
- 学生助手「一旦识别出高危风险，立即触发预警并记录原因，辅助老师介入」。

⚠️ 这里的用例**自己造来源数据**，不依赖 seed 留下的状态：
`test_todo.py` 按文件名排在 `test_student.py` 之后，而那边会审批申请、关闭工单，
靠 seed 数据断言会变成「用例顺序一变就红」的脆脆测试。
"""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import MentalAlert, TodoPush
from app.services import intent as intent_service
from app.services import scheduler as scheduler_module
from app.services import todo
from tests.conftest import data


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_alert(db, *, risk="HIGH", student_id=1, reason="单元测试：连续低落",
                notified_at=None) -> MentalAlert:
    row = MentalAlert(student_id=student_id, risk_level=risk, reason=reason,
                      status="OPEN", notified_at=notified_at)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_all_sources(client, advisor_headers, student_headers) -> None:
    """造齐四类待办的来源，让「待办清单」的断言不依赖运气。"""
    lead_id = data(client.post("/api/v1/leads", headers=advisor_headers,
                               json={"name": "待办测试客户",
                                     "phone": "137" + uuid.uuid4().hex[:8],
                                     "intention_country": "英国"}))["id"]
    data(client.post(f"/api/v1/leads/{lead_id}/followups", headers=advisor_headers,
                     json={"content": "已约定下次沟通", "next_plan": "确认预算区间",
                           "next_follow_at": "2026-09-01T09:00:00"}))
    data(client.post("/api/v1/tickets", headers=student_headers,
                     json={"content": "待办测试：签证材料清单回复较慢", "category": "签证"}))
    data(client.post("/api/v1/leave/apply", headers=student_headers,
                     json={"student_id": 1, "request_type": "LEAVE",
                           "start_date": "2026-10-20", "days": 1, "reason": "待办测试",
                           "idempotency_key": "todo-approval-" + uuid.uuid4().hex}))
    data(client.post("/api/v1/screening/analyze", headers=advisor_headers,
                     json={"source_type": "TEXT", "text": "想出国读书，还没想好去哪"}))


def _chat(client, headers, message, session="todo-test"):
    return data(client.post("/api/v1/chat/message", headers=headers,
                            json={"session_id": session, "message": message}))


# --------------------------------------------------------------------------- #
# 一、待办清单
# --------------------------------------------------------------------------- #
def test_pending_covers_every_source_for_staff(client, advisor_headers, student_headers):
    _seed_all_sources(client, advisor_headers, student_headers)

    body = data(client.get("/api/v1/todo/pending", headers=advisor_headers))
    by_key = {t["category"]: t for t in body["items"]}

    assert {"approval", "ticket", "followup", "screening_review"} <= set(by_key)
    assert "mental_alert" not in by_key, "心理预警是敏感数据，普通员工不该看到"
    assert body["category_count"] == len(body["items"])
    assert body["total"] == sum(t["count"] for t in body["items"])

    assert by_key["ticket"]["question"] == "有没有投诉反馈需要跟进？"
    assert by_key["approval"]["question"] == "有没有请假 / 考务申请需要审批？"
    assert by_key["followup"]["question"] == "有没有客户到了约定的跟进时间？"
    for item in body["items"]:
        assert item["items"] and item["action"]
        assert item["severity"] in ("high", "normal")
        assert all("oldest_hours" in row for row in item["items"])


def test_pending_is_sorted_high_first(client, db, manager_headers):
    _make_alert(db, risk="HIGH", student_id=1, reason="排序测试：高危未触达")
    body = data(client.get("/api/v1/todo/pending", headers=manager_headers))
    severities = [t["severity"] for t in body["items"]]
    assert severities == sorted(severities, key=lambda s: 0 if s == "high" else 1)
    assert severities[0] == "high", "高危待办必须排在最前面"
    assert body["highest_severity"] == "high"
    assert "有没有" in body["digest"], "话术必须是需求原文的「主动询问」句式"


def test_mental_alert_category_visible_only_to_manager(client, db, manager_headers,
                                                       advisor_headers):
    _make_alert(db, student_id=1, reason="权限测试：员工不该看到这条")

    mgr = data(client.get("/api/v1/todo/pending", headers=manager_headers))
    emp = data(client.get("/api/v1/todo/pending", headers=advisor_headers))
    assert "mental_alert" in {t["category"] for t in mgr["items"]}
    assert "mental_alert" not in {t["category"] for t in emp["items"]}
    # 不变量：管理层看到的类别必须是员工的超集（多的那几类只可能是敏感类）
    assert ({t["category"] for t in emp["items"]}
            <= {t["category"] for t in mgr["items"]})


def test_mental_alert_todo_disappears_once_delivered(client, db, manager_headers):
    """「待触达心理预警」这一类是现场算的，触达完就该消失。

    这条锁的是冒烟测试踩过的坑：把断言写成「管理层必然多出 mental_alert 一类」，
    等于把前提绑在种子数据的初始状态上 —— 上一轮的触达动作会把它消耗掉，
    同一份数据第二次跑必然红。真正该断言的是「类别是否出现」与
    「是否还有未触达预警」的对应关系。
    """
    from app.models import MentalAlert
    from app.services import todo

    _make_alert(db, student_id=1, risk="HIGH", reason="触达前后联动测试")

    def mgr_categories() -> set:
        return {t["category"] for t in
                data(client.get("/api/v1/todo/pending", headers=manager_headers))["items"]}

    assert "mental_alert" in mgr_categories()

    # 触达全部未触达预警
    left = db.query(MentalAlert).filter(MentalAlert.notified_at.is_(None)).count()
    assert left >= 1
    todo.deliver_alerts(db, actor_subject="manager", actor_role="manager",
                        actor_ref_id=2, alert_ids=None)
    db.commit()
    assert db.query(MentalAlert).filter(MentalAlert.notified_at.is_(None)).count() == 0

    assert "mental_alert" not in mgr_categories(), \
        "预警已全部触达，待办里不该还挂着「待触达心理预警」"


def test_pending_hidden_from_non_staff(client, student_headers, visitor_headers):
    assert client.get("/api/v1/todo/pending", headers=student_headers).status_code == 403
    assert client.get("/api/v1/todo/pending", headers=visitor_headers).status_code == 403


def test_collect_reflects_business_changes(client, db, advisor_headers, student_headers):
    """待办是「现场算」的：把这条申请处理掉，待办里就该少一条。"""
    created = data(client.post("/api/v1/leave/apply", headers=student_headers,
                               json={"student_id": 1, "request_type": "LEAVE",
                                     "start_date": "2026-11-02", "days": 3,
                                     "reason": "待办联动测试",
                                     "idempotency_key": "todo-flip-" + uuid.uuid4().hex}))
    before = {t["category"]: t["count"] for t in data(
        client.get("/api/v1/todo/pending", headers=advisor_headers))["items"]}
    assert "approval" in before

    from app.models import StudentRequest
    row = db.get(StudentRequest, created["id"])
    row.status = "APPROVED"
    db.commit()

    after = {t["category"]: t["count"] for t in data(
        client.get("/api/v1/todo/pending", headers=advisor_headers))["items"]}
    assert after.get("approval", 0) == before["approval"] - 1


# --------------------------------------------------------------------------- #
# 二、推送（幂等 / 频控 / 留痕）
# --------------------------------------------------------------------------- #
def test_push_writes_records_then_dedupes(client, advisor_headers):
    first = data(client.post("/api/v1/todo/push", headers=advisor_headers,
                             json={"categories": ["ticket"]}))
    assert first["mode"] == "self"
    assert len(first["records"]) == 1
    ticket = first["records"][0]
    assert ticket["category"] == "ticket" and ticket["count"] >= 1

    second = data(client.post("/api/v1/todo/push", headers=advisor_headers,
                              json={"categories": ["ticket"]}))
    assert second["pushed"] == 0, "同一时间窗内不该重复提醒同一个人"
    assert second["records"][0]["duplicated"] is True
    assert second["records"][0]["id"] == ticket["id"], "幂等要命中同一条记录"


def test_push_respects_category_filter(client, advisor_headers):
    body = data(client.post("/api/v1/todo/push", headers=advisor_headers,
                            json={"categories": ["followup"]}))
    assert [r["category"] for r in body["records"]] == ["followup"]


def test_push_rejects_unknown_category(client, advisor_headers):
    resp = client.post("/api/v1/todo/push", headers=advisor_headers,
                       json={"categories": ["not-a-category"]})
    assert resp.status_code == 400
    assert "未知的待办分类" in resp.json()["message"]


def test_push_all_staff_requires_manager(client, advisor_headers, manager_headers):
    denied = client.post("/api/v1/todo/push", headers=advisor_headers,
                         json={"all_staff": True})
    assert denied.status_code == 403

    body = data(client.post("/api/v1/todo/push", headers=manager_headers,
                            json={"all_staff": True}))
    assert body["mode"] == "all_staff"
    assert body["staff_count"] >= 3


def test_pushes_are_scoped_to_self_but_manager_sees_all(client, db, advisor_headers,
                                                        manager_headers):
    data(client.post("/api/v1/todo/push", headers=advisor_headers, json={}))

    mine = data(client.get("/api/v1/todo/pushes", headers=advisor_headers))
    assert mine["total"] >= 1
    assert all(r["subject"] == "advisor" for r in mine["items"])

    denied = client.get("/api/v1/todo/pushes", headers=advisor_headers, params={"all": True})
    assert denied.status_code == 403

    everyone = data(client.get("/api/v1/todo/pushes", headers=manager_headers,
                               params={"all": True, "limit": 100}))
    assert len({r["subject"] for r in everyone["items"]}) >= 2


def test_ack_is_idempotent_and_permission_checked(client, advisor_headers, manager_headers):
    pushed = data(client.post("/api/v1/todo/push", headers=advisor_headers,
                              json={"categories": ["ticket"]}))
    push_id = pushed["records"][0]["id"]

    got = data(client.patch(f"/api/v1/todo/pushes/{push_id}/ack", headers=advisor_headers,
                            json={"remark": "已联系学生"}))
    assert got["already"] is False and got["acknowledged_at"]

    again = data(client.patch(f"/api/v1/todo/pushes/{push_id}/ack", headers=advisor_headers,
                              json={}))
    assert again["already"] is True
    assert again["acknowledged_at"] == got["acknowledged_at"], "重复标记不能刷新时间"

    assert client.patch(f"/api/v1/todo/pushes/{push_id}/ack", headers=manager_headers,
                        json={}).status_code == 200, "管理层可代标记"
    assert client.patch("/api/v1/todo/pushes/99999999/ack", headers=advisor_headers,
                        json={}).status_code == 404


def test_ack_rejects_other_peoples_push(client, db, advisor_headers, manager_headers):
    data(client.post("/api/v1/todo/push", headers=manager_headers, json={}))
    others = (db.query(TodoPush)
              .filter(TodoPush.subject == "manager")
              .order_by(TodoPush.id.desc()).first())
    assert others is not None
    resp = client.patch(f"/api/v1/todo/pushes/{others.id}/ack", headers=advisor_headers,
                        json={})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# 三、心理预警触达
# --------------------------------------------------------------------------- #
def test_alert_notify_writes_delivery_and_advice(client, db, manager_headers):
    alert = _make_alert(db, risk="HIGH", student_id=1, reason="单元测试：连续 4 天低分")

    body = data(client.post("/api/v1/alerts/notify", headers=manager_headers,
                            json={"alert_ids": [alert.id], "channel": "chat"}))
    entry = next(i for i in body["items"] if i["id"] == alert.id)
    assert entry["notified_to"] == 1, "学生 1 的带教顾问是员工 1（王敏）"
    assert entry["notified_to_name"] == "王敏"
    assert entry["channel"] == "chat"
    assert len(entry["advice"]) >= 3
    assert any("12356" in line for line in entry["advice"])

    db.expire_all()
    row = db.get(MentalAlert, alert.id)
    assert row.notified_at is not None
    assert row.notified_to == 1 and row.notify_channel == "chat"
    assert row.handler_id == 1
    assert "24 小时" in row.intervention and "1." in row.intervention

    # 统一通知表也要落一条：`notified_at` 只回答「触达过没有」，
    # 回答不了「谁收到了、读没读、内容是什么」。
    from app.models import Notification

    notices = (db.query(Notification)
               .filter(Notification.category == "alert",
                       Notification.biz_type == "mental_alert",
                       Notification.biz_id == str(alert.id)).all())
    assert len(notices) == 1, "指派必须落到收件人的「我的通知」里才算闭环"
    assert notices[0].recipient_type == "employee"
    assert notices[0].recipient_id == 1
    assert "心理预警" in notices[0].title
    assert "12356" in notices[0].body        # 危机热线等干预建议要带上


def test_alert_notify_all_clears_the_backlog(client, db, manager_headers):
    _make_alert(db, student_id=2, reason="单元测试：未触达 A")
    body = data(client.post("/api/v1/alerts/notify", headers=manager_headers, json={}))
    assert body["total"] >= 1

    db.expire_all()
    remaining = (db.query(MentalAlert)
                 .filter(MentalAlert.notified_at.is_(None),
                         MentalAlert.status.in_(("OPEN", "FOLLOWING"))).count())
    assert remaining == 0, "一轮触达之后不该还有「未触达」的预警挂着"

    again = data(client.post("/api/v1/alerts/notify", headers=manager_headers, json={}))
    assert again["total"] == 0 and "没有需要触达" in again["message"]


def test_alert_notify_requires_manager(client, advisor_headers, student_headers):
    for headers in (advisor_headers, student_headers):
        resp = client.post("/api/v1/alerts/notify", headers=headers, json={})
        assert resp.status_code == 403
        assert resp.json()["code"] == 40300


def test_alert_list_exposes_delivery_state_and_filter(client, db, manager_headers):
    _make_alert(db, student_id=3, reason="单元测试：列表字段")

    all_rows = data(client.get("/api/v1/alerts", headers=manager_headers))
    assert "undelivered" in all_rows
    assert all("notified_at" in row for row in all_rows["items"])

    undelivered = data(client.get("/api/v1/alerts", headers=manager_headers,
                                  params={"notified": False}))
    assert undelivered["items"]
    assert all(row["notified_at"] is None for row in undelivered["items"])


def test_resolve_assignee_falls_back_to_manager_with_note(client, db):
    from app.models import Student

    student = Student(student_no="S-TODO-1", name="无顾问学生", country_target="英国",
                      program_level="硕士", stage="PREPARING", status="ACTIVE")
    db.add(student)
    db.commit()
    db.refresh(student)
    alert = None
    try:
        alert = _make_alert(db, student_id=student.id, reason="单元测试：无带教顾问")
        assignee, note = todo.resolve_assignee(db, alert)
        assert assignee is not None and assignee.biz_role == "manager"
        assert "兜底给管理层" in note
    finally:
        # 先删预警再删学生：mental_alert.student_id 有外键，顺序反了会 IntegrityError
        if alert is not None:
            db.delete(alert)
            db.flush()
        db.delete(student)
        db.commit()


# --------------------------------------------------------------------------- #
# 四、调度器（与手动触发共用同一条逻辑）
# --------------------------------------------------------------------------- #
def test_run_once_pushes_for_every_staff_account(client, db):
    result = todo.run_once()
    assert result["staff_count"] >= 3
    assert "ran_at" in result and isinstance(result["details"], list)

    subjects = {row.subject for row in db.query(TodoPush).all()}
    assert {"advisor", "manager"} <= subjects


def test_scheduler_tick_matches_run_once_and_is_off_by_default():
    assert settings.enable_scheduler is False, "测试/CI 下不该默认开后台线程"
    assert scheduler_module.autostart() is False
    assert scheduler_module.scheduler.running is False

    instance = scheduler_module.TodoScheduler(60)
    assert instance.running is False
    tick_result = instance.tick()
    assert tick_result["staff_count"] >= 3
    assert instance.interval == 60


def test_scheduler_interval_has_a_floor():
    assert scheduler_module.TodoScheduler(0).interval == 5


# --------------------------------------------------------------------------- #
# 五、对话侧：先「问」再「答」
# --------------------------------------------------------------------------- #
def test_chat_todo_intent_for_staff(client, advisor_headers):
    body = _chat(client, advisor_headers, "今天有什么待办要处理？", "todo-chat-1")
    assert body["intent"] == "todo_push"
    assert "有没有" in body["answer"]
    assert body["suggest_actions"], "要给出下一步动作"


def test_chat_todo_intent_denied_for_student(client, student_headers):
    body = _chat(client, student_headers, "今天有什么待办要处理？", "todo-chat-2")
    assert body["intent"] == "todo_push"
    assert "员工用的功能" in body["answer"]


def test_chat_alert_digest_for_manager(client, db, manager_headers):
    _make_alert(db, risk="HIGH", student_id=1, reason="对话侧单元测试：连续低分")
    body = _chat(client, manager_headers, "现在有没有心理预警？", "todo-chat-3")
    assert body["intent"] == "alert_digest"
    assert "心理预警" in body["answer"]
    assert "建议动作" in body["answer"]


def test_chat_alert_digest_denied_for_employee(client, advisor_headers):
    body = _chat(client, advisor_headers, "现在有没有心理预警？", "todo-chat-4")
    assert body["intent"] == "alert_digest"
    assert "只有管理层" in body["answer"]
    assert "连续" not in body["answer"], "越权答复里不能漏出任何学生信息"


def test_prefilter_rules_do_not_shadow_onboarding():
    """「新人入职待办」两个规则都沾边，应该由更具体的 onboarding 赢。"""
    onboarding = intent_service._prefilter("新人入职待办清单")
    assert onboarding is not None and onboarding.intent == "onboarding"

    todo_hit = intent_service._prefilter("今天有什么待办")
    assert todo_hit is not None and todo_hit.intent == "todo_push"

    alert_hit = intent_service._prefilter("有没有心理预警需要处理")
    assert alert_hit is not None and alert_hit.intent == "alert_digest"


def test_alert_digest_service_reports_undelivered(client, db):
    _make_alert(db, student_id=3, reason="单元测试：digest 结构")
    digest = todo.alert_digest(db)
    assert digest["total"] >= 1
    assert digest["high_risk"] >= 1
    assert set(digest["items"][0]) >= {"id", "student_name", "risk_level", "notified"}
