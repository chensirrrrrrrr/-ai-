"""学员域：档案 / 成绩 / 行政申请闭环 / 工单 / 心理预警。"""
from __future__ import annotations

import uuid
from datetime import date

from tests.conftest import data


def test_students_list(client, advisor_headers):
    body = data(client.get("/api/v1/students", headers=advisor_headers))
    assert body["total"] >= 3
    assert {"id", "student_no", "name", "stage"} <= set(body["items"][0])


def test_students_list_forbidden_for_visitor(client, visitor_headers):
    assert client.get("/api/v1/students", headers=visitor_headers).status_code == 403


def test_student_can_only_read_self(client, student_headers):
    assert client.get("/api/v1/students/1", headers=student_headers).status_code == 200
    resp = client.get("/api/v1/students/2", headers=student_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


def test_student_detail_masks_id_card(client, advisor_headers):
    body = data(client.get("/api/v1/students/1", headers=advisor_headers))
    assert "*" in body["id_card_masked"], "身份证必须脱敏返回"


def test_progress_timeline(client, student_headers):
    body = data(client.get("/api/v1/students/1/progress", headers=student_headers))
    assert body["stage"] == "APPLYING"
    assert len(body["timeline"]) == 5
    assert sum(1 for t in body["timeline"] if t["done"]) == 2   # PREPARING + APPLYING
    assert body["pending_requests"] >= 1


def test_score_create_and_query(client, teacher_headers):
    created = data(client.post("/api/v1/scores",
                               json={"student_id": 2, "exam_name": "单元测试",
                                     "subject": "IELTS", "score": 6.0, "full_score": 9,
                                     "exam_date": date.today().isoformat()},
                               headers=teacher_headers))
    assert created["id"] > 0

    body = data(client.get("/api/v1/scores", params={"student_id": 2},
                           headers=teacher_headers))
    assert body["total"] >= 2
    assert all(item["student_id"] == 2 for item in body["items"])


def test_score_unknown_student(client, teacher_headers):
    resp = client.post("/api/v1/scores",
                       json={"student_id": 987654, "exam_name": "x", "subject": "y",
                             "score": 1},
                       headers=teacher_headers)
    assert resp.status_code == 404


def test_leave_apply_approve_closed_loop(client, student_headers, teacher_headers):
    key = "leave-" + uuid.uuid4().hex
    applied = data(client.post("/api/v1/leave/apply",
                               json={"student_id": 1, "request_type": "LEAVE",
                                     "start_date": "2026-10-01", "days": 3,
                                     "reason": "家里有事", "idempotency_key": key},
                               headers=student_headers))
    assert applied["status"] == "PENDING"
    request_id = applied["id"]

    again = data(client.post("/api/v1/leave/apply",
                             json={"student_id": 1, "request_type": "LEAVE", "days": 3,
                                   "idempotency_key": key},
                             headers=student_headers))
    assert again["duplicated"] is True
    assert again["id"] == request_id

    approved = data(client.post(f"/api/v1/leave/{request_id}/approve",
                                json={"approve": True, "remark": "已核实"},
                                headers=teacher_headers))
    assert approved["status"] == "APPROVED"
    # 闭环通知：审批结果必须回到申请人手里（未配凭证的通道降级为站内）
    assert approved["notify"]["delivered_via"] == "internal"
    inbox = data(client.get("/api/v1/notifications", params={"category": "leave"},
                            headers=student_headers))
    row = next(i for i in inbox["items"] if i["biz_id"] == str(request_id))
    assert "已通过" in row["title"] and "已核实" in row["body"]

    # 重复审批必须被拒（幂等保护）
    duplicate = client.post(f"/api/v1/leave/{request_id}/approve",
                            json={"approve": True}, headers=teacher_headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == 40900


def test_leave_reject_path(client, student_headers, teacher_headers):
    applied = data(client.post("/api/v1/leave/apply",
                               json={"student_id": 1, "request_type": "LEAVE", "days": 1,
                                     "reason": "旅行"},
                               headers=student_headers))
    rejected = data(client.post(f"/api/v1/leave/{applied['id']}/approve",
                                json={"approve": False, "remark": "非请假事由"},
                                headers=teacher_headers))
    assert rejected["status"] == "REJECTED"
    inbox = data(client.get("/api/v1/notifications", params={"category": "leave"},
                            headers=student_headers))
    row = next(i for i in inbox["items"] if i["biz_id"] == str(applied["id"]))
    assert "已驳回" in row["title"] and "非请假事由" in row["body"]


def test_student_cannot_approve_own_request(client, student_headers):
    applied = data(client.post("/api/v1/leave/apply",
                               json={"student_id": 1, "request_type": "LEAVE", "days": 1},
                               headers=student_headers))
    resp = client.post(f"/api/v1/leave/{applied['id']}/approve",
                       json={"approve": True}, headers=student_headers)
    assert resp.status_code == 403


def test_requests_list_scoped_for_student(client, student_headers):
    body = data(client.get("/api/v1/requests", headers=student_headers))
    assert all(item["student_id"] == 1 for item in body["items"])


def test_visitor_cannot_read_requests(client, visitor_headers):
    assert client.get("/api/v1/requests", headers=visitor_headers).status_code == 403


def test_ticket_lifecycle(client, student_headers, teacher_headers):
    created = data(client.post("/api/v1/tickets",
                               json={"content": "签证材料清单回复太慢，希望加快进度。",
                                     "category": "签证"},
                               headers=student_headers))
    assert created["status"] == "OPEN"
    assert created["summary"]

    listed = data(client.get("/api/v1/tickets", params={"status": "OPEN"},
                             headers=teacher_headers))
    assert listed["total"] >= 1

    updated = data(client.patch(f"/api/v1/tickets/{created['id']}",
                                json={"status": "RESOLVED", "satisfaction": 5},
                                headers=teacher_headers))
    assert updated["status"] == "RESOLVED"
    assert updated["before"] == "OPEN"
    # 闭环通知：工单解决必须告诉提问的学生
    assert updated["notify"]["delivered_via"] == "internal"
    inbox = data(client.get("/api/v1/notifications", params={"category": "ticket"},
                            headers=student_headers))
    assert any(i["biz_id"] == str(created["id"]) for i in inbox["items"])

    # 状态没变（只改处理人）不该再发一条 —— 否则反复更新会把学生收件箱刷屏
    before_count = data(client.get("/api/v1/notifications/unread-count",
                                   headers=student_headers))["unread"]
    again = data(client.patch(f"/api/v1/tickets/{created['id']}",
                              json={"status": "RESOLVED"}, headers=teacher_headers))
    assert again["notify"] is None
    after_count = data(client.get("/api/v1/notifications/unread-count",
                                  headers=student_headers))["unread"]
    assert after_count == before_count


def test_ticket_client_cannot_list(client, student_headers):
    assert client.get("/api/v1/tickets", headers=student_headers).status_code == 403


def test_alerts_restricted_to_manager(client, manager_headers, teacher_headers):
    ok_body = data(client.get("/api/v1/alerts", headers=manager_headers))
    assert ok_body["total"] >= 1

    denied = client.get("/api/v1/alerts", headers=teacher_headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == 40300


def test_alert_update_validation(client, manager_headers):
    alert_id = data(client.get("/api/v1/alerts", headers=manager_headers))["items"][0]["id"]
    bad = client.patch(f"/api/v1/alerts/{alert_id}", json={"status": "WHATEVER"},
                       headers=manager_headers)
    assert bad.status_code == 409

    good = data(client.patch(f"/api/v1/alerts/{alert_id}", json={"status": "FOLLOWING"},
                             headers=manager_headers))
    assert good["status"] == "FOLLOWING"
