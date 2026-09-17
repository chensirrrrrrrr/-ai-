# -*- coding: utf-8 -*-
"""M5 五类业务报告：聚合口径 / 导出格式 / 定时生成。

覆盖两条需求：
- 智能报告「1 全域客户经营分析 2 日报汇总（日） 3 日报汇总（周） 4 心理健康周报 5 投诉处理周报」；
- 「报告须支持在线查阅、导出（PDF/Excel）与定时推送」。

导出这块刻意验到「字节级」：
- xlsx 用项目自己的读端（`services.material._xlsx_builtin`）**往返读回**，
  因为本机没有 openpyxl —— 能读回来说明 XML 结构、inlineStr、单元格引用都对；
- pdf 用 pypdf 解析并提取中文，能提取出来说明 ToUnicode CMap 与 UTF-16BE 编码都对。
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.services import material, reports
from tests.conftest import data

ALL_TYPES = list(reports.ALL_TYPE_KEYS)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _generate(client, headers, report_type, **extra):
    payload = {"report_type": report_type, **extra}
    return data(client.post("/api/v1/reports/generate", json=payload, headers=headers))


# --------------------------------------------------------------------------- #
# 一、口径清单与类型归一
# --------------------------------------------------------------------------- #
def test_type_catalog_exposes_all_five(client, manager_headers):
    body = data(client.get("/api/v1/reports/types", headers=manager_headers))
    keys = [item["key"] for item in body["items"]]
    assert keys == ["customer_ops", "daily_digest", "weekly_digest",
                    "mental_weekly", "complaint_weekly"]
    assert body["total"] == 5
    assert body["legacy"]["weekly"] == "weekly_digest"
    for item in body["items"]:
        assert item["scope"] and item["title"] and item["default_days"] >= 1


def test_type_catalog_requires_manager(client, advisor_headers, student_headers):
    assert client.get("/api/v1/reports/types", headers=advisor_headers).status_code == 403
    assert client.get("/api/v1/reports/types", headers=student_headers).status_code == 403


def test_normalize_type_accepts_legacy_and_rejects_unknown():
    assert reports.normalize_type("weekly") == "weekly_digest"
    assert reports.normalize_type("monthly") == "customer_ops"
    assert reports.normalize_type("CUSTOM") == "customer_ops"
    assert reports.normalize_type("complaint_weekly") == "complaint_weekly"
    with pytest.raises(ValueError) as err:
        reports.normalize_type("whatever")
    assert "可选" in str(err.value)


def test_unknown_type_returns_400_with_options(client, manager_headers):
    resp = client.post("/api/v1/reports/generate", headers=manager_headers,
                       json={"report_type": "not-a-type"})
    assert resp.status_code == 400
    assert "可选" in resp.json()["message"]


def test_resolve_period_honours_explicit_range_and_grains():
    start, end = reports.resolve_period("customer_ops", start="2026-08-01", end="2026-08-10")
    assert (start.isoformat(), end.isoformat()) == ("2026-08-01", "2026-08-10")

    daily = reports.resolve_period("daily_digest", anchor=date(2026, 9, 15))
    assert daily[0] == daily[1] == date(2026, 9, 15)

    weekly = reports.resolve_period("mental_weekly", anchor=date(2026, 9, 15))
    assert (weekly[1] - weekly[0]).days == 6, "周报默认取最近 7 天（含当天）"

    with pytest.raises(ValueError):
        reports.resolve_period("customer_ops", start="2026-09-10", end="2026-09-01")


# --------------------------------------------------------------------------- #
# 二、聚合口径（直接用服务层，便于断言数字）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("report_type", ALL_TYPES)
def test_every_type_builds_a_complete_snapshot(db, report_type):
    start, end = reports.resolve_period(report_type)
    snapshot = reports.build(db, report_type, start, end)

    assert snapshot["report_type"] == report_type
    assert snapshot["title"] and snapshot["scope"]
    assert snapshot["summary"]
    assert snapshot["period"]["days"] >= 1
    assert snapshot["metrics"] and all("label" in m and "value" in m
                                       for m in snapshot["metrics"])
    assert snapshot["sections"], "每类报告都要有分析章节"
    assert snapshot["notes"], "必须写明口径，否则数字不可解释"
    for section in snapshot["sections"]:
        assert section["heading"]
        assert section.get("lines") or section.get("bullets")


def test_customer_ops_counts_funnel_and_reports_deltas(db):
    start, end = reports.resolve_period("customer_ops")
    snapshot = reports.build(db, "customer_ops", start, end)
    metrics = {m["label"]: m for m in snapshot["metrics"]}

    assert int(metrics["新增意向客户"]["value"]) >= 1
    assert "delta" in metrics["新增意向客户"], "经营分析必须给同环比"
    assert metrics["线索转化率"]["value"].endswith("%")
    assert float(metrics["平均转化周期"]["value"].split()[0]) > 0, \
        "种子数据里的成交客户有 created→updated 间隔，不该是 0 天"
    assert snapshot["tables"], "要有共性画像之类的表格"
    # 口径说明必须点出「用 updated_at 近似」这件事
    assert any("updated_at" in note for note in snapshot["notes"])


def test_daily_and_weekly_digest_differ_in_window(db):
    daily_start, daily_end = reports.resolve_period("daily_digest")
    weekly_start, weekly_end = reports.resolve_period("weekly_digest")
    assert (daily_end - daily_start).days == 0
    assert (weekly_end - weekly_start).days == 6

    daily = reports.build(db, "daily_digest", daily_start, daily_end)
    weekly = reports.build(db, "weekly_digest", weekly_start, weekly_end)
    daily_rows = int({m["label"]: m["value"] for m in daily["metrics"]}["日报篇数"])
    weekly_rows = int({m["label"]: m["value"] for m in weekly["metrics"]}["日报篇数"])
    assert weekly_rows >= daily_rows, "周报的日报篇数不可能少于日报"


def test_digest_extracts_progress_output_and_risk(db):
    end = date.today()
    snapshot = reports.build(db, "weekly_digest", end - timedelta(days=6), end)
    headings = [section["heading"] for section in snapshot["sections"]]
    assert headings[:3] == ["核心进展", "关键产出", "潜在风险"]

    risk = next(s for s in snapshot["sections"] if s["heading"] == "潜在风险")
    joined = "\n".join(risk["bullets"])
    assert "风险" in joined or "不满" in joined, "种子里有「客户不满，存在流失风险」的日报"
    assert any("heuristic" in note.lower() or "启发式" in note for note in snapshot["notes"])


def test_mental_weekly_flags_high_risk_and_advice(db):
    start, end = reports.resolve_period("mental_weekly")
    snapshot = reports.build(db, "mental_weekly", start, end)
    metrics = {m["label"]: m for m in snapshot["metrics"]}
    assert int(metrics["高危（HIGH）"]["value"]) >= 1
    assert "情绪均分" in metrics

    advice = next(s for s in snapshot["sections"] if s["heading"] == "疏导与社群支持建议")
    assert len(advice["bullets"]) >= 3
    assert any("24 小时" in bullet for bullet in advice["bullets"])
    assert any("敏感" in note for note in snapshot["notes"])


def test_complaint_weekly_covers_category_timeliness_and_sla(db):
    start, end = reports.resolve_period("complaint_weekly")
    snapshot = reports.build(db, "complaint_weekly", start, end)
    metrics = {m["label"]: m for m in snapshot["metrics"]}
    assert int(metrics["本期投诉量"]["value"]) >= 1
    assert "平均处理时效" in metrics and "平均满意度" in metrics
    assert int(metrics["长期未决（≥7 天）"]["value"]) >= 1, "种子里有一条挂了 9 天的工单"
    assert any(alert["level"] == "high" for alert in snapshot["alerts"])


# --------------------------------------------------------------------------- #
# 三、在线查阅
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("report_type", ALL_TYPES)
def test_generate_then_read_online(client, manager_headers, report_type):
    created = _generate(client, manager_headers, report_type)
    assert created["status"] == "DONE"

    detail = data(client.get(f"/api/v1/reports/{created['id']}", headers=manager_headers))
    assert detail["report_type"] == report_type
    assert detail["snapshot"]["metrics"]
    assert detail["content"].startswith("# ")
    assert "口径说明" in detail["content"]
    assert detail["from_schedule"] is False


def test_report_list_carries_period_summary_and_exports(client, manager_headers):
    created = _generate(client, manager_headers, "complaint_weekly")
    listing = data(client.get("/api/v1/reports", headers=manager_headers,
                              params={"report_type": "complaint_weekly"}))
    assert listing["total"] >= 1
    entry = next(item for item in listing["items"] if item["id"] == created["id"])
    assert entry["period"]["label"] and entry["summary"]
    assert len(listing["types"]) == 5


def test_report_list_accepts_legacy_type_filter(client, manager_headers):
    created = _generate(client, manager_headers, "weekly_digest")
    listing = data(client.get("/api/v1/reports", headers=manager_headers,
                              params={"report_type": "weekly"}))
    assert any(item["id"] == created["id"] for item in listing["items"]), \
        "按旧值 weekly 过滤也要能查到 weekly_digest"


def test_report_detail_404(client, manager_headers):
    assert client.get("/api/v1/reports/99999999", headers=manager_headers).status_code == 404


# --------------------------------------------------------------------------- #
# 四、导出（xlsx / pdf / print / md）
# --------------------------------------------------------------------------- #
def test_export_xlsx_round_trips_through_the_builtin_reader(client, manager_headers):
    created = _generate(client, manager_headers, "customer_ops")
    resp = client.get(f"/api/v1/reports/{created['id']}/export",
                      params={"format": "xlsx"}, headers=manager_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert ".xlsx" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:      # 结构合法
        names = zf.namelist()
        assert "xl/workbook.xml" in names and "xl/styles.xml" in names
        assert sum(1 for n in names if n.startswith("xl/worksheets/sheet")) >= 3

    parsed = material.parse_material("report.xlsx", resp.content)
    assert created["title"] in parsed.text
    assert "客户经营" in parsed.text
    assert "口径说明" in parsed.text


def test_export_pdf_is_a_readable_pdf_with_chinese(client, manager_headers):
    created = _generate(client, manager_headers, "mental_weekly")
    resp = client.get(f"/api/v1/reports/{created['id']}/export",
                      params={"format": "pdf"}, headers=manager_headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-1.4")
    assert resp.headers["content-type"] == "application/pdf"

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(resp.content))
    assert len(reader.pages) >= 1
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    assert "学生心理健康周报" in text or "心理健康" in text
    assert "第 1 页" in text, "页脚页码要写在每页上"
    assert "核心指标" in text


def test_export_print_html_is_self_contained(client, manager_headers):
    created = _generate(client, manager_headers, "complaint_weekly")
    resp = client.get(f"/api/v1/reports/{created['id']}/export",
                      params={"format": "print"}, headers=manager_headers)
    assert resp.status_code == 200
    html = resp.text
    assert html.startswith("<!doctype html>")
    assert "@page" in html and "window.print()" in html
    assert "投诉处理周报" in html
    assert "<table>" in html


def test_export_markdown_and_format_validation(client, manager_headers):
    created = _generate(client, manager_headers, "daily_digest")

    md = client.get(f"/api/v1/reports/{created['id']}/export",
                    params={"format": "md"}, headers=manager_headers)
    assert md.status_code == 200
    assert md.text.startswith("# ")

    bad = client.get(f"/api/v1/reports/{created['id']}/export",
                     params={"format": "docx"}, headers=manager_headers)
    assert bad.status_code == 400
    assert "可选" in bad.json()["message"]


def test_export_records_which_formats_were_used(client, manager_headers):
    created = _generate(client, manager_headers, "weekly_digest")
    for fmt in ("xlsx", "pdf"):
        client.get(f"/api/v1/reports/{created['id']}/export",
                   params={"format": fmt}, headers=manager_headers)
    detail = data(client.get(f"/api/v1/reports/{created['id']}", headers=manager_headers))
    assert detail["exports"] == ["xlsx", "pdf"], "导出要留痕，且不重复记录"


def test_export_requires_manager_and_existing_report(client, manager_headers, advisor_headers):
    created = _generate(client, manager_headers, "customer_ops")
    assert client.get(f"/api/v1/reports/{created['id']}/export",
                      headers=advisor_headers).status_code == 403
    assert client.get("/api/v1/reports/99999999/export",
                      headers=manager_headers).status_code == 404


# --------------------------------------------------------------------------- #
# 五、定时生成
# --------------------------------------------------------------------------- #
def test_due_types_are_daily_plus_weekly_on_monday():
    monday = datetime(2026, 9, 14, 9, 0)          # 周一
    tuesday = datetime(2026, 9, 15, 9, 0)
    assert reports.due_types(monday) == ["daily_digest", "weekly_digest",
                                        "customer_ops", "mental_weekly",
                                        "complaint_weekly"]
    assert reports.due_types(tuesday) == ["daily_digest"]


def test_run_scheduled_creates_then_skips(db):
    monday = datetime(2026, 9, 14, 9, 0)
    key = ("customer_ops", date(2026, 9, 8), date(2026, 9, 14))

    from app.models import ReportRecord

    first = reports.run_scheduled(db, now=monday)
    db.commit()
    assert first["created_count"] == 5 and first["skipped_count"] == 0

    second = reports.run_scheduled(db, now=monday)
    db.commit()
    assert second["created_count"] == 0 and second["skipped_count"] == 5, \
        "同一 (类型, 报告期) 的定时报告不能重复生成"

    row = (db.query(ReportRecord)
           .filter(ReportRecord.report_type == key[0],
                   ReportRecord.period_start == key[1],
                   ReportRecord.period_end == key[2],
                   ReportRecord.from_schedule.is_(True)).first())
    assert row is not None and row.status == "DONE"
    assert row.snapshot["metrics"]


def test_scheduled_run_api_and_force(client, manager_headers):
    body = data(client.post("/api/v1/reports/scheduled/run", headers=manager_headers,
                            params={"force": "true"}))
    assert body["created_count"] == 5
    assert "daily_digest" in body["due"]
    assert all(item["id"] for item in body["created"])

    # AC-08：定时生成必须**顺手推送**，否则「报告生成了但没人收到」这个缺口还在。
    # 推送默认走站内（未配凭证的企业微信/短信会降级，见 services/notify.py）。
    assert len(body["pushed"]) == body["created_count"]
    assert body["pushed_count"] == sum(d["sent"] for d in body["pushed"])
    assert body["pushed_count"] >= 1
    report_id = body["created"][0]["id"]
    deliveries = data(client.get(f"/api/v1/reports/{report_id}/deliveries",
                                 headers=manager_headers))
    assert deliveries["total"] >= 1
    assert all(d["delivered_via"] for d in deliveries["items"])
    inbox = data(client.get("/api/v1/notifications",
                            params={"category": "report"}, headers=manager_headers))
    assert inbox["total"] >= 1

    again = data(client.post("/api/v1/reports/scheduled/run", headers=manager_headers,
                             params={"force": "true"}))
    assert again["created_count"] == 0 and again["skipped_count"] == 5

    assert client.post("/api/v1/reports/scheduled/run",
                       headers=manager_headers).status_code == 200


def test_scheduled_run_requires_manager(client, advisor_headers):
    assert client.post("/api/v1/reports/scheduled/run",
                       headers=advisor_headers).status_code == 403


def test_scheduler_tick_also_runs_reports():
    """定时器的 tick 里三个任务都要跑到：待办推送 + 考前提醒 + 报告生成推送。

    ⚠️ 这个用例会**真的按今天的报告期**生成定时报告，所以必须留在
       `test_scheduled_run_api_and_force` 之后（它已经把那 5 份造好了，
       这里再跑只会 skip，不会影响别的文件的断言）。
    """
    from app.services.scheduler import BackgroundScheduler

    result = BackgroundScheduler(60).tick()
    assert "staff_count" in result
    assert "reminders" in result, "REQ-M4-04 的考前提醒必须挂进定时任务"
    assert "reports" in result
    assert set(result["reports"]) >= {"created", "skipped", "due", "pushed"}


# --------------------------------------------------------------------------- #
# 六、导出层的纯单元测试（不经过 HTTP）
# --------------------------------------------------------------------------- #
def test_xlsx_writer_escapes_and_keeps_numbers(db):
    from app.services import xlsx_writer

    payload = xlsx_writer.write_workbook([
        ("特殊字符", [["标题", "值"], ["含 <尖括号> & 与号", 12], ["单引号'与双引号\"", -3.5]]),
    ])
    parsed = material.parse_material("t.xlsx", payload)
    assert "含 <尖括号> & 与号" in parsed.text
    assert "12" in parsed.text and "-3.5" in parsed.text


def test_pdf_writer_wraps_long_text_across_pages():
    from app.services import pdf_writer

    blocks = [("paragraph", "很长的中文段落。" * 400)]
    payload = pdf_writer.write_pdf(title="分页测试", blocks=blocks)

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(payload))
    assert len(reader.pages) > 1, "超长内容必须自动分页"
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    assert "分页测试" in text
    assert "共" in text and "页" in text


def test_pdf_writer_rejects_unknown_block():
    from app.services import pdf_writer

    with pytest.raises(ValueError):
        pdf_writer.write_pdf(title="x", blocks=[("video", "不支持")])
