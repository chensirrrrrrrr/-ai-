"""数据能力：NL2SQL 受控模板 + 权限 + 审计日志。"""
from __future__ import annotations

from tests.conftest import data


def test_nl2sql_lead_status_stats(client, advisor_headers):
    body = data(client.post("/api/v1/nl2sql/query",
                            json={"question": "统计一下客户线索的数量"},
                            headers=advisor_headers))
    assert body["template_id"] == "lead_status_stats"
    assert body["row_count"] >= 1
    assert body["sql_preview"].lower().startswith("select")
    assert "customer_lead" in body["sql_preview"]
    assert len(body["columns"]) == 2
    assert body["answer_text"]


def test_nl2sql_followups_extracts_name(client, advisor_headers):
    body = data(client.post("/api/v1/nl2sql/query",
                            json={"question": "帮我查一下赵六的跟进记录"},
                            headers=advisor_headers))
    assert body["template_id"] == "lead_followups"
    assert body["row_count"] >= 1
    assert "%赵六%" in body["sql_preview"], "人名参数必须被正确抽取并落进预览 SQL"
    assert body["rows"]


def test_nl2sql_respects_max_rows(client, advisor_headers):
    body = data(client.post("/api/v1/nl2sql/query",
                            json={"question": "统计一下客户线索的数量", "max_rows": 1},
                            headers=advisor_headers))
    assert body["row_count"] <= 1


def test_nl2sql_scope_denies_sensitive_template(client, advisor_headers):
    """销售范围不能查心理预警（敏感数据）。"""
    resp = client.post("/api/v1/nl2sql/query",
                       json={"question": "查一下心理预警情况"}, headers=advisor_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


def test_nl2sql_manager_can_read_alerts(client, manager_headers):
    body = data(client.post("/api/v1/nl2sql/query",
                            json={"question": "查一下心理预警情况"},
                            headers=manager_headers))
    assert body["template_id"] == "mental_alerts"
    assert body["row_count"] >= 1


def test_nl2sql_explicit_scope_allows_academic(client, advisor_headers):
    body = data(client.post("/api/v1/nl2sql/query",
                            json={"question": "查一下李四的成绩", "role_scope": "academic"},
                            headers=advisor_headers))
    assert body["template_id"] == "student_scores"
    assert body["row_count"] >= 1


def test_nl2sql_unknown_question_is_rejected(client, advisor_headers):
    resp = client.post("/api/v1/nl2sql/query",
                       json={"question": "帮我订一张电影票吧"}, headers=advisor_headers)
    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


def test_nl2sql_requires_staff(client, visitor_headers):
    resp = client.post("/api/v1/nl2sql/query",
                       json={"question": "统计一下客户线索的数量"}, headers=visitor_headers)
    assert resp.status_code == 403


def test_nl2sql_templates_endpoint(client, advisor_headers):
    body = data(client.get("/api/v1/nl2sql/templates", headers=advisor_headers))
    assert len(body["items"]) >= 5
    assert any(t["id"] == "mental_alerts" for t in body["items"])


def test_audit_logs_written_and_queryable(client, manager_headers):
    body = data(client.get("/api/v1/audit/logs", params={"limit": 100},
                           headers=manager_headers))
    assert body["total"] >= 1
    actions = {item["action"] for item in body["items"]}
    assert "nl2sql.query" in actions
    assert all(item["trace_id"] for item in body["items"][:5])


def test_audit_logs_filter_by_action(client, manager_headers):
    body = data(client.get("/api/v1/audit/logs", params={"action": "nl2sql.query"},
                           headers=manager_headers))
    assert body["total"] >= 1
    assert all(item["action"] == "nl2sql.query" for item in body["items"])


def test_audit_logs_forbidden_for_staff(client, teacher_headers):
    assert client.get("/api/v1/audit/logs", headers=teacher_headers).status_code == 403
