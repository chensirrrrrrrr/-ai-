# -*- coding: utf-8 -*-
"""REQ-M4-04 学业考务与考前提醒 + REQ-M4-05 业务进度明细。

对应的查验缺口：`student.stage` 只有一个粗粒度阶段，既答不了「3 天后要考雅思」，
也答不了「文书审到哪一步、谁在跟」。

这里重点验三件容易做假的事：
1. **幂等**：同一份考务表导两遍不翻倍（教务系统会重复同步）；
2. **改期会重置提醒**：改期之后必须能按新时间再提醒一次，否则就是静默漏提醒；
3. **提醒对象是学生 + 顾问**：只提醒学生等于没提醒，推动落地的责任人在顾问。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import SessionLocal
from app.services import deadline as dl
from tests.conftest import data

ADVISOR_STUDENT = 1        # student123 对应 张三（advisor_id=1）


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _iso(delta_days: float) -> str:
    return (datetime.now() + timedelta(days=delta_days)).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 导入与幂等
# --------------------------------------------------------------------------- #
def test_import_deadlines_is_idempotent(client, advisor_headers):
    items = [{"title": "雅思考试（幂等验证）", "kind": "EXAM", "due_at": _iso(20),
              "subject": "IELTS", "remind_before_hours": 72}]
    first = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                             json={"items": items}, headers=advisor_headers))
    assert first["created"] == 1 and first["updated"] == 0

    second = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                              json={"items": items}, headers=advisor_headers))
    assert second["created"] == 0 and second["updated"] == 1, "同一份表导两遍不该翻倍"

    listing = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                              headers=advisor_headers))
    same = [i for i in listing["items"] if i["title"] == "雅思考试（幂等验证）"]
    assert len(same) == 1


def test_import_with_external_id_upserts(db):
    row = {"title": "论文终稿", "kind": "DDL", "due_at": _iso(10),
           "external_id": "ACAD-0001"}
    first = dl.upsert_deadlines(db, ADVISOR_STUDENT, [row], source="academic_system")
    db.commit()
    assert first["created"] == 1

    # 教务系统重复同步同一条（标题变了、时间也改了）→ 更新而不是新增
    again = dl.upsert_deadlines(db, ADVISOR_STUDENT,
                                [{**row, "title": "论文终稿（修订）", "due_at": _iso(12)}],
                                source="academic_system")
    db.commit()
    assert again["created"] == 0 and again["updated"] == 1
    rows = dl.list_deadlines(db, student_id=ADVISOR_STUDENT)
    assert len([r for r in rows if r.external_id == "ACAD-0001"]) == 1


def test_reschedule_resets_reminder_state(db):
    """改期必须让提醒「重新可发」，否则新时间点会被静默漏掉。"""
    scheduled = dl.upsert_deadlines(db, ADVISOR_STUDENT,
                                    [{"title": "面试（改期验证）", "kind": "INTERVIEW",
                                      "due_at": _iso(1), "remind_before_hours": 48}],
                                    source="manual")
    db.commit()
    row = scheduled["items"][0]

    reminded = dl.run_reminders(db, channel="internal")
    db.commit()
    assert any(r["deadline_id"] == row.id for r in reminded["recipients"])
    db.refresh(row)
    assert row.reminded_at is not None

    # 改期 → reminded_at 被清空，于是会按新时间再提醒一次
    dl.upsert_deadlines(db, ADVISOR_STUDENT,
                        [{"title": "面试（改期验证）", "kind": "INTERVIEW",
                          "due_at": _iso(9), "remind_before_hours": 24}],
                        source="manual")
    db.commit()
    db.refresh(row)
    assert row.reminded_at is None


def test_import_rejects_incomplete_rows_at_api(client, advisor_headers):
    """API 层：缺 title / due_at 直接 422（结构化校验先于服务层的「跳过」）。"""
    resp = client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                       json={"items": [{"title": "缺时间"}]}, headers=advisor_headers)
    assert resp.status_code == 422

    resp = client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                       json={"items": [{"due_at": _iso(3)}]}, headers=advisor_headers)
    assert resp.status_code == 422


def test_service_skips_incomplete_rows(db):
    """服务层：教务系统同步来的脏数据不炸整批，只跳过并计数。"""
    out = dl.upsert_deadlines(db, ADVISOR_STUDENT,
                              [{"title": "  "}, {"title": "没有时间"},
                               {"title": "正常节点", "due_at": _iso(4)}],
                              source="academic_system")
    db.commit()
    assert out["skipped"] == 2 and out["created"] == 1


# --------------------------------------------------------------------------- #
# 查询与紧急度
# --------------------------------------------------------------------------- #
def test_list_deadlines_with_urgency_buckets(client, advisor_headers):
    data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                     json={"items": [
                         {"title": "已过期事项", "kind": "DDL", "due_at": _iso(-2)},
                         {"title": "两天后事项", "kind": "DDL", "due_at": _iso(2)},
                         {"title": "很远的考试", "kind": "EXAM", "due_at": _iso(60)},
                     ]}, headers=advisor_headers))

    body = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                           headers=advisor_headers))
    by_title = {i["title"]: i for i in body["items"]}
    assert by_title["已过期事项"]["urgency"] == "overdue"
    assert by_title["两天后事项"]["urgency"] == "soon"
    assert by_title["很远的考试"]["urgency"] == "later"
    assert by_title["两天后事项"]["days_left"] > 0
    # 升序：最近的排最前
    assert body["items"][0]["title"] == "已过期事项"
    assert body["summary"]["buckets"]["overdue"] >= 1
    assert body["summary"]["next"]["title"] == "已过期事项"

    only_exam = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                                params={"kind": "EXAM"}, headers=advisor_headers))
    assert all(i["kind"] == "EXAM" for i in only_exam["items"])

    within = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                             params={"due_within_days": 7}, headers=advisor_headers))
    assert all(i["days_left"] <= 7 for i in within["items"])


def test_mark_deadline_done_and_exclude(client, advisor_headers):
    created = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                               json={"items": [{"title": "待完成事项", "kind": "OTHER",
                                                "due_at": _iso(5)}]},
                               headers=advisor_headers))
    did = created["items"][0]["id"]

    updated = data(client.patch(f"/api/v1/deadlines/{did}", json={"status": "DONE"},
                                headers=advisor_headers))
    assert updated["status"] == "DONE"

    only_open = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                                params={"include_done": False}, headers=advisor_headers))
    assert all(i["id"] != did for i in only_open["items"])

    missing = client.patch("/api/v1/deadlines/99999999", json={"status": "DONE"},
                           headers=advisor_headers)
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# 提醒：学生 + 顾问，且幂等
# --------------------------------------------------------------------------- #
def test_run_reminders_notifies_student_and_advisor(client, advisor_headers, student_headers):
    created = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                               json={"items": [{"title": "考前提醒验证", "kind": "EXAM",
                                                "subject": "IELTS",
                                                "due_at": _iso(0.5),
                                                "remind_before_hours": 48}]},
                               headers=advisor_headers))
    did = created["items"][0]["id"]

    result = data(client.post("/api/v1/deadlines/run-reminders", json={},
                              headers=advisor_headers))
    mine = [r for r in result["recipients"] if r["deadline_id"] == did]
    types = {r["recipient_type"] for r in mine}
    assert types == {"student", "employee"}, "必须同时提醒学生本人与其顾问"
    assert all(r["delivered_via"] == "internal" for r in mine)

    # 学生侧能看到这条提醒
    inbox = data(client.get("/api/v1/notifications", params={"category": "deadline"},
                            headers=student_headers))
    assert any(i["biz_id"] == str(did) for i in inbox["items"])

    # 幂等：再跑一轮不会重复轰炸
    again = data(client.post("/api/v1/deadlines/run-reminders", json={},
                             headers=advisor_headers))
    assert all(r["deadline_id"] != did for r in again["recipients"])

    # 提醒记录可查
    log = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadline-notifications",
                          headers=advisor_headers))
    assert any(i["biz_id"] == str(did) for i in log["items"])


def test_reminder_preview_does_not_send(client, advisor_headers):
    before = data(client.get("/api/v1/notifications/unread-count",
                             headers=advisor_headers))["unread"]
    preview = data(client.get("/api/v1/deadlines/run-reminders/preview",
                              headers=advisor_headers))
    assert preview["dry_run"] is True
    assert preview["sent"] == 0
    after = data(client.get("/api/v1/notifications/unread-count",
                            headers=advisor_headers))["unread"]
    assert after == before, "预演不能真的发通知"


# --------------------------------------------------------------------------- #
# REQ-M4-05 业务进度看板
# --------------------------------------------------------------------------- #
def test_progress_import_and_board(client, advisor_headers):
    items = [
        {"phase": "DOC", "item": "文书素材收集", "status": "DONE"},
        {"phase": "DOC", "item": "研究计划书", "status": "DOING"},
        {"phase": "VISA", "item": "签证材料", "status": "BLOCKED"},
    ]
    created = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/progress",
                               json={"items": items}, headers=advisor_headers))
    assert created["created"] == 3
    board = created["board"]
    assert board["total"] >= 3 and board["done"] >= 1
    assert 0 < board["progress"] <= 1
    assert board["blocked"], "卡住的细项必须能被一眼看到"
    doc = next(p for p in board["phases"] if p["phase"] == "DOC")
    assert doc["label"] == "文书审核" and doc["total"] >= 2

    # 幂等：同一批再导一遍不翻倍
    again = data(client.post(f"/api/v1/students/{ADVISOR_STUDENT}/progress",
                             json={"items": items}, headers=advisor_headers))
    assert again["created"] == 0 and again["updated"] == 3

    fetched = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/progress-board",
                              headers=advisor_headers))
    assert fetched["total"] == board["total"]


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
def test_import_requires_staff(client, student_headers):
    resp = client.post(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                       json={"items": [{"title": "x", "due_at": _iso(1)}]},
                       headers=student_headers)
    assert resp.status_code == 403


def test_student_cannot_read_other_students_deadlines(client, student_headers):
    resp = client.get("/api/v1/students/2/deadlines", headers=student_headers)
    assert resp.status_code == 403

    mine = data(client.get(f"/api/v1/students/{ADVISOR_STUDENT}/deadlines",
                           headers=student_headers))
    assert mine["student_id"] == ADVISOR_STUDENT
