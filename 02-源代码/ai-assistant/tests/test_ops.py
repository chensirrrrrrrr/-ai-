"""运营域：活动报名 / 报告 / 员工日报（含语音来源）/ 知识库。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.conftest import data


def test_activities_list(client, student_headers):
    body = data(client.get("/api/v1/activities", headers=student_headers))
    assert body["total"] >= 2
    assert body["items"][0]["title"]


def test_activity_enroll_duplicate_student(client, manager_headers):
    created = data(client.post("/api/v1/activities",
                               json={"title": "单元测试活动-去重", "capacity": 5,
                                     "start_at": (datetime.now() + timedelta(days=3)).isoformat()},
                               headers=manager_headers))
    activity_id = created["id"]

    first = data(client.post(f"/api/v1/activities/{activity_id}/enroll",
                             json={"student_id": 1}, headers=manager_headers))
    assert first["status"] == "ENROLLED"
    assert first["enrolled_count"] == 1

    duplicate = client.post(f"/api/v1/activities/{activity_id}/enroll",
                            json={"student_id": 1}, headers=manager_headers)
    assert duplicate.status_code == 409

    roster = data(client.get(f"/api/v1/activities/{activity_id}/enrollments",
                             headers=manager_headers))
    assert roster["total"] == 1


def test_activity_enroll_capacity_full(client, manager_headers):
    created = data(client.post("/api/v1/activities",
                               json={"title": "单元测试活动-满额", "capacity": 1,
                                     "start_at": (datetime.now() + timedelta(days=4)).isoformat()},
                               headers=manager_headers))
    activity_id = created["id"]

    data(client.post(f"/api/v1/activities/{activity_id}/enroll",
                     json={"student_id": 1}, headers=manager_headers))
    full = client.post(f"/api/v1/activities/{activity_id}/enroll",
                       json={"student_id": 2}, headers=manager_headers)
    assert full.status_code == 409
    assert "名额已满" in full.json()["message"]


def test_student_enroll_uses_own_identity(client, student_headers, manager_headers):
    """学生报名时，服务端强制用自己的身份，忽略请求体里伪造的 student_id。"""
    created = data(client.post("/api/v1/activities",
                               json={"title": "单元测试活动-身份", "capacity": 5,
                                     "start_at": (datetime.now() + timedelta(days=5)).isoformat()},
                               headers=manager_headers))
    enrolled = data(client.post(f"/api/v1/activities/{created['id']}/enroll",
                                json={"student_id": 2}, headers=student_headers))
    roster = data(client.get(f"/api/v1/activities/{created['id']}/enrollments",
                             headers=manager_headers))
    assert roster["items"][0]["student_id"] == 1        # 而不是 2
    assert enrolled["status"] == "ENROLLED"


def test_activity_enroll_closed(client, manager_headers, student_headers):
    created = data(client.post("/api/v1/activities",
                               json={"title": "已关闭活动", "status": "CLOSED"},
                               headers=manager_headers))
    resp = client.post(f"/api/v1/activities/{created['id']}/enroll",
                       json={"student_id": 1}, headers=student_headers)
    assert resp.status_code == 409


def test_report_generate_and_download(client, manager_headers):
    """报告改成「真实聚合 + 结构化快照」之后，下载的正文不再是 mock 文本。

    老断言（`"# 周报" in download.text`）锁的是 mock 工作流那段假内容，
    现在正文由 `services.reports.render_markdown()` 渲染真实统计，所以改成断言结构。
    """
    created = data(client.post("/api/v1/reports/generate",
                               json={"report_type": "weekly", "title": "第 37 周经营周报",
                                     "params": {"week": "2026-W37"}},
                               headers=manager_headers))
    assert created["status"] == "DONE"
    assert created["preview"]
    assert created["report_type"] == "weekly_digest", "旧值 weekly 应归一到新类型"
    assert created["period"]["days"] >= 1
    assert created["metrics"], "报告必须带真实聚合出来的指标"

    detail = data(client.get(f"/api/v1/reports/{created['id']}", headers=manager_headers))
    assert "第 37 周经营周报" in detail["content"]
    assert "核心指标" in detail["content"]
    assert detail["snapshot"]["summary"]
    assert detail["export_formats"] == ["xlsx", "print", "pdf", "md"]

    download = client.get(f"/api/v1/reports/{created['id']}/download",
                          headers=manager_headers)
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    assert download.text.startswith("# ")
    assert "第 37 周经营周报" in download.text


def test_reports_restricted_to_manager(client, teacher_headers, student_headers):
    assert client.get("/api/v1/reports", headers=teacher_headers).status_code == 403
    assert client.get("/api/v1/reports", headers=student_headers).status_code == 403


def test_daily_report_submit_voice_and_summary(client, advisor_headers):
    submitted = data(client.post("/api/v1/reports/daily",
                                 json={"employee_id": 1, "content": "今天跟进 3 位客户，"
                                                                   "预约 1 次到访。",
                                       "source": "voice",
                                       "raw_audio_url": "oss://demo/voice/1.m4a"},
                                 headers=advisor_headers))
    assert submitted["source"] == "voice"
    assert submitted["updated"] is True      # 种子里已有今日日报 -> 覆盖

    summary = data(client.get("/api/v1/reports/daily/summary",
                              params={"report_date": date.today().isoformat()},
                              headers=advisor_headers))
    assert summary["total_employees"] >= 3
    assert summary["submitted"] >= 2
    assert summary["voice_sourced"] >= 1
    assert summary["items"]


def test_daily_report_unknown_employee(client, advisor_headers):
    resp = client.post("/api/v1/reports/daily",
                       json={"employee_id": 987654, "content": "x"},
                       headers=advisor_headers)
    assert resp.status_code == 404


def test_daily_trend(client, teacher_headers):
    body = data(client.get("/api/v1/reports/daily/trend", params={"days": 7},
                           headers=teacher_headers))
    assert len(body["series"]) == 7
    assert sum(item["count"] for item in body["series"]) >= 1


def test_kb_documents(client, advisor_headers):
    created = data(client.post("/api/v1/kb/documents",
                               json={"title": "单元测试知识文档", "category": "测试",
                                     "dify_dataset_id": "ds-test-001", "chunk_count": 12},
                               headers=advisor_headers))
    assert created["status"] == "INDEXED"

    draft = data(client.post("/api/v1/kb/documents",
                             json={"title": "未入库文档", "category": "测试"},
                             headers=advisor_headers))
    assert draft["status"] == "DRAFT"

    listed = data(client.get("/api/v1/kb/documents", params={"category": "测试"},
                             headers=advisor_headers))
    assert listed["total"] >= 2
