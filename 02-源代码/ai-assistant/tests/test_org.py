"""组织域：组织架构查询（M3-06）与新人入职指引（M3-07）。"""
from __future__ import annotations

from app.services import onboarding

from tests.conftest import data


# --------------------------------------------------------------------------- #
# M3-06 组织架构
# --------------------------------------------------------------------------- #
def test_org_tree_builds_hierarchy(client, admin_headers):
    body = data(client.get("/api/v1/org/tree", headers=admin_headers))
    assert body["total"] >= 3
    # 种子里 陈总(E003) 是 王敏(E001) 的上级 → 树里应当是「陈总 → 王敏」
    root_names = [n["name"] for n in body["tree"]]
    assert "陈总" in root_names
    chen = next(n for n in body["tree"] if n["name"] == "陈总")
    assert any(c["name"] == "王敏" for c in chen["children"])
    assert {d["department"] for d in body["departments"]} >= {"顾问部", "教务部", "管理层"}


def test_org_tree_department_filter(client, advisor_headers):
    body = data(client.get("/api/v1/org/tree", params={"department": "顾问部"},
                           headers=advisor_headers))
    assert body["filtered_by_department"] == "顾问部"
    assert body["total"] == 1
    assert body["tree"][0]["name"] == "王敏"


def test_org_departments_headcount_and_head(client, manager_headers):
    body = data(client.get("/api/v1/org/departments", headers=manager_headers))
    depts = {d["department"]: d for d in body["items"]}
    assert depts["管理层"]["head"]["name"] == "陈总"
    assert depts["管理层"]["headcount"] >= 1
    assert depts["顾问部"]["members"][0]["name"] == "王敏"


def test_org_employees_filters(client, manager_headers):
    all_rows = data(client.get("/api/v1/org/employees", headers=manager_headers))
    assert all_rows["total"] >= 3
    assert all_rows["items"][0]["emp_no"].startswith("E")

    by_kw = data(client.get("/api/v1/org/employees", params={"keyword": "王"},
                            headers=manager_headers))
    assert by_kw["total"] == 1 and by_kw["items"][0]["name"] == "王敏"

    by_dept = data(client.get("/api/v1/org/employees", params={"department": "教务部"},
                              headers=manager_headers))
    assert [i["name"] for i in by_dept["items"]] == ["李强"]

    by_role = data(client.get("/api/v1/org/employees", params={"biz_role": "teacher"},
                              headers=manager_headers))
    assert by_role["total"] == 1


def test_org_employee_detail_relations(client, manager_headers):
    detail = data(client.get("/api/v1/org/employees/1", headers=manager_headers))
    assert detail["name"] == "王敏"
    assert detail["manager"]["name"] == "陈总"
    assert detail["id_card_masked"] if False else True          # 员工表无敏感字段，不应带出
    assert "id_card_masked" not in detail
    # 员工 1 没有下属，同部门也没有其他人
    assert detail["subordinates"] == []
    assert detail["colleagues"] == []

    supervisor = data(client.get("/api/v1/org/employees/1", headers=manager_headers))
    assert supervisor["phone"] and supervisor["email"]


def test_org_employee_not_found(client, manager_headers):
    assert client.get("/api/v1/org/employees/987654",
                      headers=manager_headers).status_code == 404


def test_org_restricted_to_staff(client, student_headers, visitor_headers):
    for path in ("/api/v1/org/tree", "/api/v1/org/departments", "/api/v1/org/employees",
                 "/api/v1/org/onboarding/guide"):
        assert client.get(path, headers=student_headers).status_code == 403
        assert client.get(path, headers=visitor_headers).status_code == 403


def test_org_requires_auth(client):
    assert client.get("/api/v1/org/tree").status_code == 401


# --------------------------------------------------------------------------- #
# M3-07 新人入职指引
# --------------------------------------------------------------------------- #
def test_onboarding_guide_payload(client, advisor_headers):
    g = data(client.get("/api/v1/org/onboarding/guide", headers=advisor_headers))
    assert g["version"] == onboarding.GUIDE_VERSION
    assert g["title"] == onboarding.GUIDE_TITLE
    assert g["owner"] and g["updated_at"] and g["tags"]
    # 五个阶段：D0 / D1 / W1 / M1 / M3
    assert [s["key"] for s in g["stages"]] == ["D0", "D1", "W1", "M1", "M3"]
    assert g["stats"]["item_count"] == sum(len(s["items"]) for s in g["stages"])
    assert g["stats"]["faq_count"] == len(onboarding.FAQ) >= 10
    # 每条步骤都要有负责人与办理入口（否则新人看完不知道找谁）
    for s in g["stages"]:
        for item in s["items"]:
            assert item["title"] and item["detail"] and item["owner"] and item["channel"]
    # 明确写清「不覆盖什么」
    assert g["limits"] and any("薪酬" in x for x in g["limits"])


def test_onboarding_checklist_by_stage(client, teacher_headers):
    d1 = data(client.get("/api/v1/org/onboarding/checklist", params={"stage": "D1"},
                         headers=teacher_headers))
    assert len(d1["stages"]) == 1 and d1["stages"][0]["key"] == "D1"
    assert d1["total"] == len(d1["stages"][0]["items"]) >= 4

    allstage = data(client.get("/api/v1/org/onboarding/checklist", headers=teacher_headers))
    assert allstage["total"] > d1["total"]

    unknown = data(client.get("/api/v1/org/onboarding/checklist", params={"stage": "X9"},
                              headers=teacher_headers))
    assert unknown["total"] == 0


def test_onboarding_faq_search(client, manager_headers):
    hit = data(client.get("/api/v1/org/onboarding/faq", params={"keyword": "日报"},
                          headers=manager_headers))
    assert hit["total"] >= 1
    assert any("日报" in i["q"] or "日报" in i["a"] for i in hit["items"])

    # 标签也能命中
    tag_hit = data(client.get("/api/v1/org/onboarding/faq", params={"keyword": "合规"},
                              headers=manager_headers))
    assert tag_hit["total"] >= 1

    miss = data(client.get("/api/v1/org/onboarding/faq", params={"keyword": "量子计算机"},
                           headers=manager_headers))
    assert miss["total"] == 0

    full = data(client.get("/api/v1/org/onboarding/faq", headers=manager_headers))
    assert full["total"] == len(onboarding.FAQ)


def test_onboarding_contacts_resolved_from_org(client, advisor_headers):
    c = data(client.get("/api/v1/org/onboarding/contacts",
                        params={"employee_id": 1}, headers=advisor_headers))
    person = {i["key"]: i for i in c["items"]}
    assert person["manager"]["contact"]["name"] == "陈总"     # 来自 employee.manager_id
    assert "王敏" in c["scope"]
    # 顾问部没有带教老师 / 部门负责人 → 如实说明，不编造姓名
    assert person["buddy"]["contact"] is None and person["buddy"]["note"]
    assert person["dept_head"]["contact"] is None and person["dept_head"]["note"]
    # 人力行政与 IT 在种子里没有对应岗位 → 也要给出说明
    assert person["hr"]["contact"] is None and person["hr"]["note"]
    assert person["it"]["contact"] is None and person["it"]["note"]


def test_onboarding_contacts_default_to_self(client, advisor_headers):
    """不传 employee_id 时按当前登录人解析（顾问 = 员工 1）。"""
    c = data(client.get("/api/v1/org/onboarding/contacts", headers=advisor_headers))
    person = {i["key"]: i for i in c["items"]}
    assert person["manager"]["contact"]["name"] == "陈总"


def test_onboarding_contacts_unknown_employee(client, manager_headers):
    assert client.get("/api/v1/org/onboarding/contacts", params={"employee_id": 987654},
                      headers=manager_headers).status_code == 404


def test_onboarding_guide_registered_as_knowledge_doc(client, manager_headers):
    docs = data(client.get("/api/v1/kb/documents", params={"category": "NEO"},
                           headers=manager_headers))
    assert docs["total"] >= 1
    neo = docs["items"][0]
    assert neo["title"] == onboarding.GUIDE_TITLE
    assert neo["version"] == onboarding.GUIDE_VERSION
    assert neo["status"] == "INDEXED"


# --------------------------------------------------------------------------- #
# 对话入口：入职指引意图
# --------------------------------------------------------------------------- #
def test_chat_onboarding_intent_for_staff(client, advisor_headers):
    body = data(client.post("/api/v1/chat/message",
                            json={"session_id": "t-neo-1", "message": "新人入职指引"},
                            headers=advisor_headers))
    assert body["intent"] == "onboarding"
    assert body["agent"] == "enterprise_assistant"
    assert "入职" in body["answer"]
    assert "报到前" in body["answer"]                      # 未命中 FAQ 时给阶段目录
    assert body["references"] and body["references"][0]["doc"] == onboarding.GUIDE_TITLE


def test_chat_onboarding_faq_hit(client, teacher_headers):
    body = data(client.post("/api/v1/chat/message",
                            json={"session_id": "t-neo-2", "message": "入职指引：报到要带什么？"},
                            headers=teacher_headers))
    assert body["intent"] == "onboarding"
    assert "身份证" in body["answer"]
    assert onboarding.GUIDE_VERSION in body["answer"]      # 答复要带版本，便于判断是否过期


def test_chat_onboarding_denied_for_student(client, student_headers):
    body = data(client.post("/api/v1/chat/message",
                            json={"session_id": "t-neo-3", "message": "新人入职指引"},
                            headers=student_headers))
    assert body["intent"] == "onboarding"
    assert "内部文档" in body["answer"]
    assert body["agent"] != "enterprise_assistant"        # 越权已被降级
    assert "转人工" in body["suggest_actions"]
