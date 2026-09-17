"""客户域：意向客户 / 跟进记录 / 研判（材料上传解析 · 人工复核 · 批量）。"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import data
from tests.material_fixtures import make_pdf, make_xlsx

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

VERTICAL_ROWS = [
    ("姓名", "钱九"),
    ("年龄", "23"),
    ("学历", "本科"),
    ("毕业院校", "西南财经大学"),
    ("专业", "金融学"),
    ("GPA", "3.4/4.0"),
    ("雅思", "7.0"),
    ("意向国家", "英国"),
    ("意向阶段", "待签约"),
]


def test_lead_create_and_duplicate_phone(client, advisor_headers):
    phone = "137" + uuid.uuid4().hex[:8]
    payload = {"name": "测试客户A", "phone": phone, "intention_country": "英国",
               "intention_stage": "硕士", "source": "单元测试"}

    created = data(client.post("/api/v1/leads", json=payload, headers=advisor_headers))
    assert created["id"] > 0
    assert created["status"] == "NEW"
    assert created["owner_id"] == 1              # 未指定 owner 时落到当前登录顾问

    dup = client.post("/api/v1/leads", json=payload, headers=advisor_headers)
    assert dup.status_code == 409
    assert dup.json()["code"] == 40900


def test_lead_list_filter_and_paging(client, advisor_headers):
    body = data(client.get("/api/v1/leads", params={"page": 1, "page_size": 2},
                           headers=advisor_headers))
    assert body["total"] >= 3
    assert len(body["items"]) <= 2

    filtered = data(client.get("/api/v1/leads", params={"status": "FOLLOWING"},
                               headers=advisor_headers))
    assert all(item["status"] == "FOLLOWING" for item in filtered["items"])


def test_lead_status_change_and_audit(client, advisor_headers):
    lead_id = data(client.post("/api/v1/leads",
                               json={"name": "测试客户B", "phone": "138" + uuid.uuid4().hex[:8]},
                               headers=advisor_headers))["id"]
    body = data(client.patch(f"/api/v1/leads/{lead_id}/status",
                             json={"status": "SIGNED", "remark": "已签约"},
                             headers=advisor_headers))
    assert body["before"] == "NEW"
    assert body["status"] == "SIGNED"


def test_followup_idempotency(client, advisor_headers):
    lead_id = data(client.post("/api/v1/leads",
                               json={"name": "测试客户C", "phone": "139" + uuid.uuid4().hex[:8]},
                               headers=advisor_headers))["id"]
    key = "idem-" + uuid.uuid4().hex

    first = data(client.post(f"/api/v1/leads/{lead_id}/followups",
                             json={"content": "首次电话沟通", "idempotency_key": key},
                             headers=advisor_headers))
    assert first["duplicated"] is False

    second = data(client.post(f"/api/v1/leads/{lead_id}/followups",
                              json={"content": "首次电话沟通", "idempotency_key": key},
                              headers=advisor_headers))
    assert second["duplicated"] is True
    assert second["id"] == first["id"], "幂等键命中时必须返回同一条记录"

    detail = data(client.get(f"/api/v1/leads/{lead_id}", headers=advisor_headers))
    assert len(detail["followups"]) == 1
    assert detail["lead"]["status"] == "FOLLOWING"    # 首次跟进自动推进状态


def test_lead_not_found(client, advisor_headers):
    resp = client.get("/api/v1/leads/99999999", headers=advisor_headers)
    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


def test_visitor_cannot_touch_leads(client, visitor_headers):
    assert client.get("/api/v1/leads", headers=visitor_headers).status_code == 403
    resp = client.post("/api/v1/leads", json={"name": "x", "phone": "1"},
                       headers=visitor_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


def test_screening_analyze_qualified(client, advisor_headers):
    text = "学生本科 211，GPA 3.4/4.0，雅思 7.0，希望申请英国硕士"
    body = data(client.post("/api/v1/screening/analyze",
                            json={"source_type": "TEXT", "text": text},
                            headers=advisor_headers))
    assert body["conclusion"] == "符合"
    assert body["confidence"] > 0.5
    assert body["hit_products"]
    assert body["dify_run_id"].startswith("run-mock")

    detail = data(client.get(f"/api/v1/screening/{body['id']}", headers=advisor_headers))
    assert detail["extracted_fields"]["intention_country"] == "英国"
    assert detail["evidence"]


def test_screening_insufficient_info(client, advisor_headers):
    body = data(client.post("/api/v1/screening/analyze",
                            json={"source_type": "TEXT", "text": "想出国读书，还没想好去哪"},
                            headers=advisor_headers))
    assert body["conclusion"] == "信息不足"


def test_screening_requires_text_for_text_type(client, advisor_headers):
    resp = client.post("/api/v1/screening/analyze",
                       json={"source_type": "TEXT"}, headers=advisor_headers)
    assert resp.status_code == 400


def test_screening_list_has_aggregation(client, advisor_headers):
    body = data(client.get("/api/v1/screening", headers=advisor_headers))
    assert body["total"] >= 1
    assert "符合" in body["by_conclusion"]


# --------------------------------------------------------------------------- #
# REQ-M1-01 / M1-02：多格式上传 + 文档解析 + 字段抽取
# --------------------------------------------------------------------------- #
def _upload(client, headers, filename, content, mime, lead_id=None):
    form = {"lead_id": str(lead_id)} if lead_id is not None else None
    return client.post("/api/v1/screening/upload", headers=headers,
                       files={"file": (filename, content, mime)}, data=form)


def test_upload_xlsx_parses_and_extracts_key_fields(client, advisor_headers):
    content = make_xlsx([list(row) for row in VERTICAL_ROWS])
    body = data(_upload(client, advisor_headers, "客户登记表.xlsx", content, XLSX_MIME))

    assert body["source_type"] == "EXCEL"
    assert body["filename"] == "客户登记表.xlsx"
    assert body["size"] == len(content)
    assert body["parser"]                       # 用了哪条解析路径必须可见
    assert "西南财经大学" in body["text"]
    assert body["fields"]["name"] == "钱九"
    assert body["fields"]["school"] == "西南财经大学"
    assert body["fields"]["intention_country"] == "英国"
    assert body["missing_fields"] == []
    assert body["raw_file_url"].startswith("/api/v1/screening/files/")
    assert body["field_rows"][0]["label"] == "姓名"
    assert body["preview"]


def test_upload_pdf_keeps_source_type_and_original_link(client, advisor_headers):
    content = make_pdf(["Name: Zhao Liu", "IELTS 7.0  GPA 3.4/4.0", "Target Country: UK"])
    body = data(_upload(client, advisor_headers, "resume.pdf", content, "application/pdf"))

    assert body["source_type"] == "PDF"
    assert body["unit_name"] == "页"
    assert body["fields"]["intention_country"] == "英国"
    assert "gpa" in body["fields"] or "language" in body["fields"]

    rel_path = body["raw_file_url"].split("/api/v1/screening/files/")[1]
    downloaded = client.get(f"/api/v1/screening/files/{rel_path}", headers=advisor_headers)
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert "attachment" in downloaded.headers.get("content-disposition", "")


def test_upload_rejects_unsupported_and_empty(client, advisor_headers):
    old_xls = _upload(client, advisor_headers, "old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1", "application/vnd.ms-excel")
    assert old_xls.status_code == 400
    assert "暂不支持" in old_xls.json()["message"]

    empty = _upload(client, advisor_headers, "empty.txt", b"", "text/plain")
    assert empty.status_code == 400
    assert "空文件" in empty.json()["message"]


def test_upload_rejects_oversized_material(client, advisor_headers, monkeypatch):
    from app.api.v1 import crm as crm_api

    monkeypatch.setattr(crm_api.settings, "upload_max_bytes", 512)
    try:
        resp = _upload(client, advisor_headers, "big.txt",
                       ("意向国家：英国\n" * 200).encode("utf-8"), "text/plain")
        assert resp.status_code == 413
        assert resp.json()["code"] == 40000
    finally:
        monkeypatch.undo()


def test_upload_is_staff_only(client, visitor_headers):
    resp = _upload(client, visitor_headers, "a.txt", b"x", "text/plain")
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


def test_upload_unknown_lead_is_404(client, advisor_headers):
    resp = _upload(client, advisor_headers, "a.txt", b"x", "text/plain", lead_id=99999999)
    assert resp.status_code == 404


def test_upload_path_traversal_is_blocked():
    from app.api.v1 import crm as crm_api
    from app.core import NotFound

    for evil in ("../../../etc/passwd", "..\\..\\windows\\win.ini", "/etc/hosts"):
        with pytest.raises(NotFound):
            crm_api._resolve_upload_path(evil)


def test_analyze_can_reparse_from_uploaded_file(client, advisor_headers):
    """只给 raw_file_url、不给 text 也要能研判（第三方系统常见调法）。"""
    content = make_xlsx([["姓名", "意向国家", "GPA", "雅思"],
                         ["周九", "加拿大", "3.6/4.0", "7.5"]])
    uploaded = data(_upload(client, advisor_headers, "周九.xlsx", content, XLSX_MIME))

    body = data(client.post("/api/v1/screening/analyze", headers=advisor_headers,
                            json={"source_type": "EXCEL",
                                  "raw_file_url": uploaded["raw_file_url"]}))
    assert body["conclusion"] == "符合"                 # 正文里有数字，mock 研判判符合
    detail = data(client.get(f"/api/v1/screening/{body['id']}", headers=advisor_headers))
    assert detail["extracted_fields"]["intention_country"] == "加拿大"
    assert detail["source_name"] == "周九.xlsx"
    assert detail["review_status"] == "PENDING"
    assert detail["ai_conclusion"] == "符合"


# --------------------------------------------------------------------------- #
# REQ-M1-06：人工复核（确认 / 推翻 + 回写留痕）
# --------------------------------------------------------------------------- #
def _new_screening(client, headers, text=None):
    body = data(client.post("/api/v1/screening/analyze", headers=headers,
                            json={"source_type": "TEXT",
                                  "text": text or "本科 211，GPA 3.4/4.0，雅思 7.0，目标英国硕士"}))
    return body["id"]


def test_review_confirm_keeps_conclusion(client, advisor_headers):
    sid = _new_screening(client, advisor_headers)
    body = data(client.patch(f"/api/v1/screening/{sid}/review", headers=advisor_headers,
                             json={"action": "CONFIRM", "remark": "与材料一致"}))
    assert body["review_status"] == "CONFIRMED"
    assert body["conclusion"] == body["ai_conclusion"]
    assert body["revised"] is False
    assert body["reviewed_by"] == 1                     # advisor 的 employee.ref_id
    assert body["reviewed_at"]
    assert body["before"]["review_status"] == "PENDING"


def test_review_override_rewrites_conclusion_and_keeps_ai_value(client, advisor_headers):
    sid = _new_screening(client, advisor_headers)
    body = data(client.patch(f"/api/v1/screening/{sid}/review", headers=advisor_headers,
                             json={"action": "OVERRIDE", "conclusion": "不符合",
                                   "remark": "雅思 7.0 是目标不是现有成绩，材料不足"}))
    assert body["review_status"] == "OVERRIDDEN"
    assert body["conclusion"] == "不符合"
    assert body["ai_conclusion"] == "符合"               # AI 原结论必须留痕
    assert body["revised"] is True
    assert "雅思 7.0" in body["review_remark"]

    refetched = data(client.get(f"/api/v1/screening/{sid}", headers=advisor_headers))
    assert refetched["conclusion"] == "不符合"


def test_review_override_requires_conclusion(client, advisor_headers):
    sid = _new_screening(client, advisor_headers)
    resp = client.patch(f"/api/v1/screening/{sid}/review", headers=advisor_headers,
                        json={"action": "OVERRIDE"})
    assert resp.status_code == 400
    assert "OVERRIDE" in resp.json()["message"]


def test_review_override_same_conclusion_is_conflict(client, advisor_headers):
    sid = _new_screening(client, advisor_headers)
    current = data(client.get(f"/api/v1/screening/{sid}", headers=advisor_headers))["conclusion"]
    resp = client.patch(f"/api/v1/screening/{sid}/review", headers=advisor_headers,
                        json={"action": "OVERRIDE", "conclusion": current})
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


def test_review_not_found_and_role_guard(client, advisor_headers, visitor_headers):
    assert client.patch("/api/v1/screening/99999999/review", headers=advisor_headers,
                        json={"action": "CONFIRM"}).status_code == 404
    sid = _new_screening(client, advisor_headers)
    assert client.patch(f"/api/v1/screening/{sid}/review", headers=visitor_headers,
                        json={"action": "CONFIRM"}).status_code == 403


def test_corrections_list_collects_overridden_cases(client, advisor_headers):
    sid = _new_screening(client, advisor_headers, "本科 211，GPA 3.4/4.0，目标英国硕士")
    data(client.patch(f"/api/v1/screening/{sid}/review", headers=advisor_headers,
                      json={"action": "OVERRIDE", "conclusion": "信息不足",
                            "remark": "缺语言成绩"}))
    body = data(client.get("/api/v1/screening/corrections", headers=advisor_headers))

    assert body["total"] >= 1
    assert any(item["id"] == sid for item in body["items"])
    assert "符合 → 信息不足" in body["by_transition"]
    assert "frequent_missing_fields" in body
    entry = next(item for item in body["items"] if item["id"] == sid)
    assert entry["ai_conclusion"] == "符合" and entry["conclusion"] == "信息不足"


# --------------------------------------------------------------------------- #
# REQ-M1-07：批量研判
# --------------------------------------------------------------------------- #
def test_batch_by_items_returns_result_list(client, advisor_headers):
    items = [
        {"source_type": "TEXT", "text": "本科，GPA 3.5/4.0，雅思 7.0，目标英国", "source_name": "材料A"},
        {"source_type": "TEXT", "text": "想出国读书，还没想好去哪", "source_name": "材料B"},
        {"source_type": "TEXT", "text": "硕士，均分 85，目标澳洲", "source_name": "材料C"},
    ]
    body = data(client.post("/api/v1/screening/batch", headers=advisor_headers,
                            json={"items": items}))
    assert body["batch_id"].startswith("batch-")
    assert body["total"] == 3 and body["succeeded"] == 3 and body["failed"] == 0
    assert len(body["items"]) == 3
    assert body["by_conclusion"]["符合"] == 2
    assert all(item["id"] and item["ok"] for item in body["items"])

    # 结果清单可按批号回捞
    listing = data(client.get("/api/v1/screening", headers=advisor_headers,
                              params={"batch_id": body["batch_id"]}))
    assert listing["total"] == 3


def test_batch_isolates_single_failure(client, advisor_headers):
    items = [
        {"source_type": "TEXT", "text": "本科，GPA 3.5/4.0，雅思 7.0，目标英国"},
        {"source_type": "TEXT"},                       # 缺 text → 单条失败
        {"source_type": "TEXT", "text": "硕士，均分 85，目标澳洲"},
    ]
    body = data(client.post("/api/v1/screening/batch", headers=advisor_headers,
                            json={"items": items}))
    assert body["succeeded"] == 2 and body["failed"] == 1
    failed = [item for item in body["items"] if not item["ok"]]
    assert len(failed) == 1 and failed[0]["index"] == 1
    assert "必须提供 text" in failed[0]["error"]
    # 关键：一条失败不能把已成功的两条带走
    assert all(item["id"] for item in body["items"] if item["ok"])


def test_batch_stops_on_error_when_asked(client, advisor_headers):
    items = [
        {"source_type": "TEXT"},
        {"source_type": "TEXT", "text": "本科，GPA 3.5/4.0"},
    ]
    body = data(client.post("/api/v1/screening/batch", headers=advisor_headers,
                            json={"items": items, "stop_on_error": True}))
    assert body["processed"] == 1 and body["failed"] == 1


def test_batch_by_lead_ids_builds_material_from_profile(client, advisor_headers):
    lead_id = data(client.post("/api/v1/leads", headers=advisor_headers,
                               json={"name": "批量客户", "phone": "136" + uuid.uuid4().hex[:8],
                                     "intention_country": "英国",
                                     "intention_stage": "待签约"}))["id"]
    data(client.post(f"/api/v1/leads/{lead_id}/followups", headers=advisor_headers,
                     json={"content": "电话沟通，客户均分 85，雅思 7.0"}))

    body = data(client.post("/api/v1/screening/batch", headers=advisor_headers,
                            json={"lead_ids": [lead_id]}))
    assert body["succeeded"] == 1
    created_id = body["items"][0]["id"]
    detail = data(client.get(f"/api/v1/screening/{created_id}", headers=advisor_headers))
    assert detail["lead_id"] == lead_id
    assert detail["source_name"] == "客户档案-批量客户"
    assert detail["extracted_fields"]["intention_country"] == "英国"
    assert detail["extracted_fields"]["gpa"] == "85"


def test_batch_rejects_empty_and_over_limit(client, advisor_headers):
    empty = client.post("/api/v1/screening/batch", headers=advisor_headers, json={})
    assert empty.status_code == 400

    too_many = client.post("/api/v1/screening/batch", headers=advisor_headers,
                           json={"items": [{"source_type": "TEXT", "text": f"材料{i}"}
                                           for i in range(21)]})
    assert too_many.status_code == 400
    assert "最多" in too_many.json()["message"]


def test_batch_unknown_lead_is_404(client, advisor_headers):
    resp = client.post("/api/v1/screening/batch", headers=advisor_headers,
                       json={"lead_ids": [99999999]})
    assert resp.status_code == 404


def test_batch_is_staff_only(client, visitor_headers):
    resp = client.post("/api/v1/screening/batch", headers=visitor_headers,
                       json={"items": [{"source_type": "TEXT", "text": "x"}]})
    assert resp.status_code == 403


def test_screening_list_filters_and_aggregates_review_status(client, advisor_headers):
    body = data(client.get("/api/v1/screening", headers=advisor_headers,
                           params={"review_status": "OVERRIDDEN"}))
    assert body["total"] >= 1
    assert all(item["review_status"] == "OVERRIDDEN" for item in body["items"])
    assert body["by_review_status"]["OVERRIDDEN"] >= 1
    assert "pending_review" in body


def test_lead_row_level_isolation(client, advisor_headers, teacher_headers, manager_headers):
    """行级隔离：employee 只能碰自己名下的客户，manager / admin 全量。

    补这条的背景：此前鉴权只挡了「能不能进接口」（staff_only），
    拿到合法 Token 的普通员工仍能靠猜 ID 翻到别人的客户与跟进记录。
    """
    lead_id = data(client.post("/api/v1/leads", headers=advisor_headers,
                               json={"name": "越权隔离客户",
                                     "phone": "135" + uuid.uuid4().hex[:8]}))["id"]

    # 列表：advisor 只见自己名下；强行按别人的 owner_id 查也被收敛回自己
    mine = data(client.get("/api/v1/leads", headers=advisor_headers))
    assert mine["total"] >= 1
    assert all(item["owner_id"] == 1 for item in mine["items"])
    forced = data(client.get("/api/v1/leads", params={"owner_id": 2}, headers=advisor_headers))
    assert all(item["owner_id"] == 1 for item in forced["items"])

    # teacher（employee, ref_id=2）拿合法 Token 也翻不到 advisor 的客户
    assert client.get(f"/api/v1/leads/{lead_id}", headers=teacher_headers).status_code == 403
    assert client.get(f"/api/v1/leads/{lead_id}/followups",
                      headers=teacher_headers).status_code == 403
    assert client.patch(f"/api/v1/leads/{lead_id}/status", json={"status": "SIGNED"},
                        headers=teacher_headers).status_code == 403
    assert client.post(f"/api/v1/leads/{lead_id}/followups", headers=teacher_headers,
                       json={"content": "越权跟进"}).status_code == 403
    assert client.post("/api/v1/screening/analyze", headers=teacher_headers,
                       json={"source_type": "TEXT", "text": "本科，GPA 3.5",
                             "lead_id": lead_id}).status_code == 403
    assert client.post("/api/v1/screening/batch", headers=teacher_headers,
                       json={"lead_ids": [lead_id]}).status_code == 403
    t_list = data(client.get("/api/v1/leads", headers=teacher_headers))
    assert all(item["owner_id"] == 2 for item in t_list["items"])

    # manager / admin 全量可见
    m_list = data(client.get("/api/v1/leads", headers=manager_headers))
    assert m_list["total"] >= mine["total"]
    assert any(item["id"] == lead_id for item in m_list["items"])

    # 自己名下的读写不受影响
    assert data(client.get(f"/api/v1/leads/{lead_id}", headers=advisor_headers))["lead"]["id"] == lead_id
