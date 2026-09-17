"""五类业务报告（需求 M5）：**数据聚合 → 统一快照 → 多出口渲染**。

需求原文的 5 类：
1. `customer_ops` 全域客户经营分析报告（意向 / 成交 / 流失三段 + 同环比 + 共性画像 + 流失预警）
2. `daily_digest` 员工日报汇总报告（日）
3. `weekly_digest` 员工日报汇总报告（周）
4. `mental_weekly` 学生心理健康周报
5. `complaint_weekly` 投诉处理周报

为啥先做「快照」再做「渲染」
--------------------------
如果让「页面查阅」「Excel 导出」「PDF 导出」各自去查一遍库，就会出现
**同一天导出的两份数字不一样**（中途有人提交了日报），这种口径漂移最难排查。
所以：`build()` 只跑一次聚合，把结果存进 `ReportRecord.snapshot`，
之后所有出口（markdown / pdf / xlsx / print-html）都只读这一份快照。

口径诚实原则
------------
本项目没有「状态变更历史表」，所以「期内成交 / 期内流失」只能用
`updated_at`（最后一次状态变更时间）落在报告期内来近似。这类近似必须写进
`notes` 让看报告的人知道，而不是假装精确 —— 需求里甲方最在意的就是口径统一。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

from ..models import (AfterSalesTicket, CustomerLead, Employee, EmployeeReport,
                      MentalAlert, ReportRecord, Student, StudentMentalProfile,
                      StudentRequest)
from . import pdf_writer, xlsx_writer

DATE_FMT = "%Y-%m-%d"

# --------------------------------------------------------------------------- #
# 报告类型注册表
# --------------------------------------------------------------------------- #
REPORT_TYPES: Dict[str, Dict[str, Any]] = {
    "customer_ops": {
        "title": "全域客户经营分析报告",
        "grain": "weekly",
        "default_days": 7,
        "scope": "意向 / 成交 / 流失三大客群 + 同环比 + 共性画像 + 流失预警",
    },
    "daily_digest": {
        "title": "员工日报汇总报告（日）",
        "grain": "daily",
        "default_days": 1,
        "scope": "当日全员工日报的核心进展 / 关键产出 / 潜在风险",
    },
    "weekly_digest": {
        "title": "员工日报汇总报告（周）",
        "grain": "weekly",
        "default_days": 7,
        "scope": "近一周日报按人聚合：产出、覆盖度、风险集中度",
    },
    "mental_weekly": {
        "title": "学生心理健康周报",
        "grain": "weekly",
        "default_days": 7,
        "scope": "情绪态势 / 高危名单 / 周期节点 / 疏导建议",
    },
    "complaint_weekly": {
        "title": "投诉处理周报",
        "grain": "weekly",
        "default_days": 7,
        "scope": "投诉量同环比 / 分类 / 处理时效 / 满意度 / 长期未决",
    },
}

# 历史值与它的新归属：老接口调 `weekly` 时不能直接 400，否则等于把旧调用方打断
LEGACY_TYPES: Dict[str, str] = {
    "weekly": "weekly_digest",
    "monthly": "customer_ops",
    "custom": "customer_ops",
}

ALL_TYPE_KEYS = tuple(REPORT_TYPES)

# 报告推送对象（AC-08 的「定时推送」）。
#
# 角色名对齐 `sys_account.role`。SRS 4.5.1 里点名的「部门负责人 / 心理老师 / 教务 /
# 售后」在当前账号模型里**没有独立角色**，所以先统一落到管理层与管理员 ——
# 这比凭空造四个不存在的角色名要诚实。等甲方把账号角色细化（SRS 待确认事项 10
# 「报告的推送对象、推送渠道与推送时间」），改这张表即可，推送逻辑不用动。
REPORT_TARGET_ROLES: Dict[str, Tuple[str, ...]] = {
    "customer_ops": ("manager", "admin"),
    "daily_digest": ("manager", "admin"),
    "weekly_digest": ("manager", "admin"),
    "mental_weekly": ("manager", "admin"),
    "complaint_weekly": ("manager", "admin"),
}

# 导出格式。`print` 是「打印就绪 HTML → 浏览器另存 PDF」的通道，
# 中文渲染一定正确，所以在界面上是推荐路径；`pdf` 是服务端直出（见 render_print_html 的说明）。
EXPORT_FORMATS = ("xlsx", "print", "pdf", "md")


def normalize_type(raw: Optional[str]) -> str:
    key = (raw or "").strip().lower()
    key = LEGACY_TYPES.get(key, key)
    if key not in REPORT_TYPES:
        raise ValueError(
            f"未知报告类型 {raw!r}；可选：{list(REPORT_TYPES)}"
            f"（兼容旧值 {list(LEGACY_TYPES)}）")
    return key


def type_catalog() -> List[Dict[str, Any]]:
    return [{"key": key, **meta, "grain_days": meta["default_days"]}
            for key, meta in REPORT_TYPES.items()]


# --------------------------------------------------------------------------- #
# 时间窗
# --------------------------------------------------------------------------- #
def resolve_period(report_type: str, *, anchor: Optional[date] = None,
                   start: Optional[str] = None, end: Optional[str] = None
                   ) -> Tuple[date, date]:
    """报告期：显式给 start/end 就用它，否则按锚点日往前推 N 天（含当天）。"""
    if start and end:
        begin = date.fromisoformat(start)
        finish = date.fromisoformat(end)
        if begin > finish:
            raise ValueError(f"报告期起止颠倒：{begin} > {finish}")
        return begin, finish

    today = anchor or date.today()
    if report_type == "daily_digest":
        return today, today
    days = REPORT_TYPES[report_type]["default_days"]
    return today - timedelta(days=days - 1), today


def _bounds(start: date, end: date) -> Tuple[datetime, datetime]:
    return datetime.combine(start, time.min), datetime.combine(end, time.max)


def _prev_window(start: date, end: date) -> Tuple[date, date]:
    span = (end - start).days + 1
    return start - timedelta(days=span), start - timedelta(days=1)


def _delta_text(current: int, previous: int) -> Tuple[str, str]:
    """返回 (环比文案, 方向)。previous=0 时说「新增」而不是「+∞%」。"""
    if previous == 0:
        return ("—（上期无数据）" if current == 0 else "新增"), \
               ("flat" if current == 0 else "up")
    diff = current - previous
    if diff == 0:
        return "0（持平）", "flat"
    pct = abs(diff) / previous * 100
    return f"{'+' if diff > 0 else '-'}{abs(diff)}（环比 {pct:.0f}%）", \
           ("up" if diff > 0 else "down")


def _metric(label: str, value: Any, delta: Optional[Tuple[str, str]] = None,
            hint: str = "") -> Dict[str, Any]:
    item: Dict[str, Any] = {"label": label, "value": value, "hint": hint}
    if delta:
        item["delta"], item["direction"] = delta
    return item


# --------------------------------------------------------------------------- #
# 各类型的聚合
# --------------------------------------------------------------------------- #
def _leads_in(db: Session, start_dt: datetime, end_dt: datetime) -> List[CustomerLead]:
    return (db.query(CustomerLead)
            .filter(CustomerLead.created_at >= start_dt,
                    CustomerLead.created_at <= end_dt).all())


def _status_changed_in(db: Session, status: str, start_dt: datetime,
                       end_dt: datetime) -> List[CustomerLead]:
    return (db.query(CustomerLead)
            .filter(CustomerLead.status == status,
                    CustomerLead.updated_at >= start_dt,
                    CustomerLead.updated_at <= end_dt).all())


def _customer_ops(db: Session, start: date, end: date) -> Dict[str, Any]:
    start_dt, end_dt = _bounds(start, end)
    ps, pe = _prev_window(start, end)
    ps_dt, pe_dt = _bounds(ps, pe)

    new_now = _leads_in(db, start_dt, end_dt)
    new_prev = _leads_in(db, ps_dt, pe_dt)
    signed_now = _status_changed_in(db, "SIGNED", start_dt, end_dt)
    signed_prev = _status_changed_in(db, "SIGNED", ps_dt, pe_dt)
    lost_now = _status_changed_in(db, "LOST", start_dt, end_dt)
    lost_prev = _status_changed_in(db, "LOST", ps_dt, pe_dt)

    total_leads = db.query(CustomerLead).count()
    status_counter = Counter(row.status for row in db.query(CustomerLead).all())

    conversion = (len(signed_now) / len(new_now) * 100) if new_now else 0.0
    cycle_days = [max((row.updated_at - row.created_at).days, 0)
                  for row in signed_now if row.updated_at and row.created_at]
    avg_cycle = (sum(cycle_days) / len(cycle_days)) if cycle_days else 0.0

    # 共性画像：意向客群按 意向国家 / 来源 聚类
    country_counter = Counter((row.intention_country or "未填写") for row in new_now)
    source_counter = Counter((row.source or "未填写") for row in new_now)

    now = datetime.now()
    stale = [row for row in db.query(CustomerLead).all()
             if row.status == "FOLLOWING"
             and row.updated_at and (now - row.updated_at).days >= 7]

    metrics = [
        _metric("新增意向客户", len(new_now), _delta_text(len(new_now), len(new_prev))),
        _metric("成交客户", len(signed_now), _delta_text(len(signed_now), len(signed_prev))),
        _metric("流失客户", len(lost_now), _delta_text(len(lost_now), len(lost_prev))),
        _metric("线索转化率", f"{conversion:.0f}%", hint="期内成交 / 期内新增"),
        _metric("平均转化周期", f"{avg_cycle:.1f} 天", hint="成交客户 createdAt→updatedAt"),
        _metric("在库线索总量", total_leads,
                hint=" ".join(f"{k}×{v}" for k, v in status_counter.items())),
    ]

    sections = [
        {
            "heading": "意向客群",
            "lines": [
                f"报告期内新增意向客户 {len(new_now)} 位，"
                f"主要方向：{'、'.join(f'{k} {v}' for k, v in country_counter.most_common(3)) or '暂无'}。",
                f"来源结构：{'、'.join(f'{k} {v}' for k, v in source_counter.most_common(3)) or '暂无'}。",
            ],
            "bullets": [
                f"共性画像：{_persona(country_counter, source_counter)}",
                f"在库线索状态分布：{'、'.join(f'{k} {v}' for k, v in status_counter.items())}。",
            ],
        },
        {
            "heading": "成交客群",
            "lines": [
                f"报告期内成交 {len(signed_now)} 单，平均转化周期 {avg_cycle:.1f} 天，"
                f"线索转化率 {conversion:.0f}%。",
            ],
            "bullets": [f"{row.name}（{row.intention_country or '方向未填'}·"
                        f"{row.source or '来源未填'}）" for row in signed_now[:5]]
            or ["本期没有新的成交记录。"],
        },
        {
            "heading": "流失客群与归因",
            "lines": [
                f"报告期内流失 {len(lost_now)} 位，"
                f"当前在库流失 {status_counter.get('LOST', 0)} 位。",
                "归因线索来自跟进备注与状态停留时长；状态长期停在「跟进中」的客户"
                "是最主要的流失前兆。",
            ],
            "bullets": [f"{row.name}：{row.remark or '无备注'}" for row in lost_now[:5]]
            or ["本期没有新增流失记录。"],
        },
    ]

    alerts: List[Dict[str, str]] = []
    if stale:
        alerts.append({
            "level": "high",
            "text": f"{len(stale)} 位跟进中客户已 ≥7 天没有任何状态变更，流失风险高："
                    + "、".join(row.name for row in stale[:5]),
        })
    if new_now and conversion < 20:
        alerts.append({"level": "normal",
                       "text": f"期内转化率仅 {conversion:.0f}%，建议复盘环节卡点。"})
    if not new_now:
        alerts.append({"level": "normal", "text": "报告期内没有新增线索，获客渠道需要检查。"})

    tables = []
    if country_counter or source_counter:
        rows = []
        for country, count in country_counter.most_common(5):
            share = f"{count / len(new_now) * 100:.0f}%" if new_now else "—"
            rows.append([country, count, share,
                         "、".join(src for src, _ in source_counter.most_common(2))])
        tables.append({"title": "意向客群共性画像", "columns": ["意向国家", "线索数", "占比", "主要来源"],
                       "rows": rows})
    if stale:
        tables.append({
            "title": "流失风险预警清单",
            "columns": ["客户", "意向国家", "状态停留", "备注"],
            "rows": [[row.name, row.intention_country or "—",
                      f"{(now - row.updated_at).days} 天", (row.remark or "—")[:30]]
                     for row in stale[:10]],
        })

    summary = (
        f"报告期内新增线索 {len(new_now)} 位、成交 {len(signed_now)} 单、流失 {len(lost_now)} 位；"
        + (f"有 {len(stale)} 位跟进中客户已超过 7 天未动，需优先挽回。"
           if stale else "存量客户跟进节奏正常。"))
    if not new_now and not signed_now:
        summary = "报告期内没有新增线索与成交，获客与转化链路都需要检查。"

    return {"summary": summary, "metrics": metrics, "sections": sections,
            "tables": tables, "alerts": alerts,
            "notes": [
                "「期内成交 / 期内流失」以客户最后一次状态变更时间（updated_at）落在报告期内判定 —— "
                "本库没有独立的状态变更历史表，这是近似口径。",
                "「平均转化周期」= 成交客户的 createdAt → updatedAt 天数均值。",
                "报告期 = 锚点日往前 N 天（含当天），非自然周。",
            ]}


def _persona(country: Counter, source: Counter) -> str:
    if not country:
        return "报告期内样本不足，暂不输出画像。"
    top_country = country.most_common(1)[0]
    top_source = source.most_common(1)[0] if source else ("未填写", 0)
    return (f"意向集中在「{top_country[0]}」（{top_country[1]} 位），"
            f"主要来源是「{top_source[0]}」；建议按该方向准备院校与案例话术。")


# --------------------------------------------------------------------------- #
# 日报汇总（日 / 周共用一个聚合器）
# --------------------------------------------------------------------------- #
_RISK_WORDS = ("风险", "阻塞", "延期", "延误", "困难", "投诉", "不满", "流失",
               "不够", "卡住", "卡在", "来不及", "出问题", "异常")
_PROGRESS_WORDS = ("完成", "跟进", "签约", "上线", "交付", "推进", "落实", "核对",
                   "排课", "回访", "沟通", "整理", "复盘", "对接", "录入")
_OUTPUT_RE = re.compile(r"(\d+)\s*(单|人|位|组|条|份|次|名|场|篇)")
_SENTENCE_SPLIT_RE = re.compile(r"[。；;\n\r]+")


def _sentences(text: str) -> List[str]:
    return [chunk.strip() for chunk in _SENTENCE_SPLIT_RE.split(text or "") if chunk.strip()]


def _digest(db: Session, start: date, end: date, *, weekly: bool) -> Dict[str, Any]:
    start_dt, end_dt = _bounds(start, end)
    ps, pe = _prev_window(start, end)
    ps_dt, pe_dt = _bounds(ps, pe)

    rows = (db.query(EmployeeReport)
            .filter(EmployeeReport.report_date >= start,
                    EmployeeReport.report_date <= end)
            .order_by(EmployeeReport.report_date.asc(), EmployeeReport.employee_id.asc())
            .all())
    prev_rows = (db.query(EmployeeReport)
                 .filter(EmployeeReport.report_date >= ps, EmployeeReport.report_date <= pe)
                 .all())

    staff = db.query(Employee).filter(Employee.status == "ACTIVE").all()
    names = {emp.id: emp.name for emp in staff}
    authors = {row.employee_id for row in rows}
    coverage = (len(authors) / len(staff) * 100) if staff else 0.0
    voice = sum(1 for row in rows if row.source == "voice")
    span_days = (end - start).days + 1

    progress: List[str] = []
    outputs: List[str] = []
    risks: List[Tuple[str, str]] = []
    for row in rows:
        who = names.get(row.employee_id, f"员工{row.employee_id}")
        for sentence in _sentences(row.content):
            if any(word in sentence for word in _RISK_WORDS):
                risks.append((who, sentence))
            if any(word in sentence for word in _PROGRESS_WORDS):
                progress.append(f"{who}：{sentence}")
            if _OUTPUT_RE.search(sentence):
                outputs.append(f"{who}：{sentence}")

    per_person = Counter(names.get(row.employee_id, f"员工{row.employee_id}") for row in rows)
    quantity_total = sum(int(match.group(1)) for row in rows
                         for match in _OUTPUT_RE.finditer(row.content or ""))

    metrics = [
        _metric("日报篇数", len(rows), _delta_text(len(rows), len(prev_rows))),
        _metric("提交人数", len(authors), hint=f"在职 {len(staff)} 人"),
        _metric("提交覆盖率", f"{coverage:.0f}%",
                hint=f"{'期' if weekly else '当日'}内至少提交 1 篇的比例"),
        _metric("语音录入占比",
                f"{(voice / len(rows) * 100) if rows else 0:.0f}%", hint=f"{voice} 篇来自语音"),
        _metric("可量化产出合计", quantity_total, hint="日报里「数字+量词」的合计"),
        _metric("风险提示条数", len(risks),
                hint="含「风险/阻塞/延期/投诉」等关键词的句子"),
    ]

    sections = [
        {"heading": "核心进展",
         "lines": [f"报告期共 {len(rows)} 篇日报、{len(authors)} 人提交"
                   + (f"，覆盖 {span_days} 天。" if weekly else "。")],
         "bullets": _dedupe(progress)[:8] or ["没有识别到明确的进展描述。"]},
        {"heading": "关键产出",
         "lines": ["从日报里提取到带数量的产出描述："] if outputs else ["日报里没有出现可量化的产出。"],
         "bullets": _dedupe(outputs)[:8]},
        {"heading": "潜在风险",
         "lines": ["以下句子命中了风险关键词，建议在周会上逐条确认："]
                  if risks else ["没有识别到风险信号。"],
         "bullets": [f"{who}：{sentence}" for who, sentence in risks[:8]]},
    ]

    alerts = [{"level": "high" if len(risks) >= 3 else "normal",
               "text": f"识别到 {len(risks)} 条风险描述，"
                       f"集中在：{'、'.join(sorted({who for who, _ in risks})[:5]) or '—'}"}]
    if coverage < 80:
        alerts.append({"level": "normal",
                       "text": f"日报覆盖率只有 {coverage:.0f}%，"
                               f"{len(staff) - len(authors)} 位在职员工本期没有提交。"})

    tables = [
        {"title": "员工维度汇总",
         "columns": ["员工", "篇数", "占比"],
         "rows": [[name, count, f"{count / len(rows) * 100:.0f}%" if rows else "—"]
                  for name, count in per_person.most_common()]},
    ]
    detail = rows[:12] if weekly else rows
    tables.append({
        "title": "日报明细",
        "columns": ["日期", "员工", "来源", "内容摘要", "风险"],
        "rows": [[row.report_date.isoformat(), names.get(row.employee_id, row.employee_id),
                  "语音" if row.source == "voice" else "手动",
                  (row.summary or row.content or "")[:34],
                  "是" if any(word in (row.content or "") for word in _RISK_WORDS) else ""]
                 for row in detail],
    })

    return {"summary": f"报告期 {len(authors)} 人提交 {len(rows)} 篇日报"
                       f"（覆盖率 {coverage:.0f}%），识别关键产出 {len(outputs)} 条、"
                       f"风险 {len(risks)} 条。",
            "metrics": metrics, "sections": sections, "tables": tables, "alerts": alerts,
            "notes": ["「关键产出」用「数字 + 量词」的正则匹配，属于启发式提取，"
                      "不等于财务口径的产出金额。",
                      "「风险提示」命中「风险/阻塞/延期/投诉」等关键词即计入，"
                      "用于提醒人工复核，不是自动定责。"]}


def _dedupe(items: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = item.split("：", 1)[-1][:18]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 心理健康周报
# --------------------------------------------------------------------------- #
def _mental_weekly(db: Session, start: date, end: date) -> Dict[str, Any]:
    start_dt, end_dt = _bounds(start, end)
    ps, pe = _prev_window(start, end)
    ps_dt, pe_dt = _bounds(ps, pe)

    alerts_now = (db.query(MentalAlert)
                  .filter(MentalAlert.created_at >= start_dt,
                          MentalAlert.created_at <= end_dt).all())
    alerts_prev = (db.query(MentalAlert)
                   .filter(MentalAlert.created_at >= ps_dt,
                           MentalAlert.created_at <= pe_dt).all())
    open_alerts = (db.query(MentalAlert)
                   .filter(MentalAlert.status.in_(("OPEN", "FOLLOWING"))).all())
    profiles = db.query(StudentMentalProfile).all()
    students = {stu.id: stu for stu in db.query(Student).all()}

    scores = [float(prof.emotion_score) for prof in profiles
              if prof.emotion_score is not None]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    level_counter = Counter(prof.risk_level for prof in profiles)
    tag_counter = Counter(tag for prof in profiles for tag in (prof.tags or []))
    undelivered = [row for row in open_alerts if row.notified_at is None]

    trend: Counter = Counter()
    for row in alerts_now:
        trend[row.created_at.strftime("%m-%d")] += 1

    # 留学周期节点：未来 14 天内的考试/考务安排
    upcoming: List[Tuple[str, str]] = []
    for request in db.query(StudentRequest).filter(
            StudentRequest.status.in_(("PENDING", "APPROVED"))).all():
        content = request.content or {}
        raw_date = content.get("date") or content.get("start_date")
        if not raw_date:
            continue
        try:
            when = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        if 0 <= (when - end).days <= 14:
            who = students.get(request.student_id)
            upcoming.append((who.name if who else f"学生{request.student_id}",
                             f"{raw_date} {content.get('exam') or request.type}"))

    metrics = [
        _metric("本期新增预警", len(alerts_now), _delta_text(len(alerts_now), len(alerts_prev))),
        _metric("高危（HIGH）", sum(1 for row in open_alerts if row.risk_level == "HIGH"),
                hint="未关闭口径"),
        _metric("未触达预警", len(undelivered), hint="notified_at 为空"),
        _metric("情绪均分", f"{avg_score:.1f}",
                hint=f"{len(scores)} 份画像（满分 100）"),
        _metric("心理画像覆盖", f"{len(profiles)} 人", hint=f"全部学生 {len(students)} 人"),
        _metric("风险等级分布",
                "、".join(f"{k}×{v}" for k, v in level_counter.items()) or "—"),
    ]

    high_list = [row for row in open_alerts if row.risk_level == "HIGH"]
    sections = [
        {"heading": "整体心理态势",
         "lines": [f"本期新增预警 {len(alerts_now)} 条（上期 {len(alerts_prev)} 条），"
                   f"当前未关闭 {len(open_alerts)} 条，情绪均分 {avg_score:.1f}。",
                   f"情绪波动按日分布：{'、'.join(f'{k} {v} 条' for k, v in sorted(trend.items())) or '本期无新增预警'}。"],
         "bullets": [f"{prof.summary}" for prof in profiles if prof.summary][:5]},
        {"heading": "风险学生识别",
         "lines": [f"高危 {len(high_list)} 人、未触达 {len(undelivered)} 条，"
                   "建议本周内完成首次介入并回填跟进记录。"],
         "bullets": _tag_bullets(tag_counter)},
        {"heading": "留学周期节点",
         "lines": ["结合留学周期，未来 14 天内的关键节点如下（考试周前后情绪波动最明显）："],
         "bullets": [f"{who}：{what}" for who, what in upcoming[:6]]
                    or ["未来 14 天没有考试 / 考务节点。"]},
    ]

    advice = [
        "孤独感偏高的学生：优先安排 1 对 1 线上交流，并邀请进入同城学长学姐社群。",
        "学业焦虑偏高的学生：把大目标拆成本周可完成的小任务，考试周前减少额外排课。",
        "文化冲突适应困难的学生：配对一名同校在读学长，提供生活类问题即时答疑。",
        f"高危名单 {len(high_list)} 人必须在 24 小时内接触；出现自伤表述立即升级心理老师与家长。",
    ]
    sections.append({"heading": "疏导与社群支持建议", "lines": [], "bullets": advice})

    alerts = []
    if high_list:
        alerts.append({"level": "high",
                       "text": f"{len(high_list)} 位学生处于高危状态，"
                               "需 24 小时内介入：" + "、".join(
                                   students.get(row.student_id).name
                                   if students.get(row.student_id) else f"学生{row.student_id}"
                                   for row in high_list[:5])})
    if undelivered:
        alerts.append({"level": "high",
                       "text": f"{len(undelivered)} 条预警尚未触达处理人，"
                               "可到「运营中心 → 触达未提醒预警」一键通知。"})
    if profiles and avg_score < 60:
        alerts.append({"level": "normal",
                       "text": f"整体情绪均分 {avg_score:.1f} 低于 60，建议提高关怀触达频率。"})

    tables = [{
        "title": "高危 / 未关闭预警清单",
        "columns": ["预警编号", "学生", "等级", "触发原因", "状态", "是否已触达"],
        "rows": [[row.id,
                  (students.get(row.student_id).name
                   if students.get(row.student_id) else f"学生{row.student_id}"),
                  row.risk_level, row.reason[:40], row.status,
                  "是" if row.notified_at else "否"] for row in open_alerts[:12]],
    }]

    return {"summary": f"本期新增预警 {len(alerts_now)} 条，未关闭 {len(open_alerts)} 条"
                       f"（高危 {len(high_list)} 条、未触达 {len(undelivered)} 条），"
                       f"整体情绪均分 {avg_score:.1f}。",
            "metrics": metrics, "sections": sections, "tables": tables, "alerts": alerts,
            "notes": ["学生心理数据属敏感信息，报告只对管理层开放。",
                      "情绪均分来自 student_mental_profile.emotion_score（满分 100），"
                      "仅统计有画像的学生。",
                      "触发原因是模型/规则识别结果的落库文本，用于辅助人工判断。"]}


def _tag_bullets(tag_counter: Counter) -> List[str]:
    if not tag_counter:
        return ["暂无情绪标签沉淀。"]
    return [f"「{tag}」出现 {count} 次" for tag, count in tag_counter.most_common(5)]


# --------------------------------------------------------------------------- #
# 投诉处理周报
# --------------------------------------------------------------------------- #
def _complaint_weekly(db: Session, start: date, end: date) -> Dict[str, Any]:
    start_dt, end_dt = _bounds(start, end)
    ps, pe = _prev_window(start, end)
    ps_dt, pe_dt = _bounds(ps, pe)

    rows = (db.query(AfterSalesTicket)
            .filter(AfterSalesTicket.created_at >= start_dt,
                    AfterSalesTicket.created_at <= end_dt).all())
    prev_rows = (db.query(AfterSalesTicket)
                 .filter(AfterSalesTicket.created_at >= ps_dt,
                         AfterSalesTicket.created_at <= pe_dt).all())
    open_rows = db.query(AfterSalesTicket).all()

    status_counter = Counter(row.status for row in open_rows)
    category_counter = Counter((row.category or "未分类") for row in rows)

    resolved = [row for row in open_rows
                if row.status in ("RESOLVED", "CLOSED") and row.resolved_at]
    durations = [max((row.resolved_at - row.created_at).total_seconds() / 3600, 0)
                 for row in resolved]
    avg_hours = sum(durations) / len(durations) if durations else 0.0
    max_hours = max(durations) if durations else 0.0

    scores = [row.satisfaction for row in open_rows if row.satisfaction]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    score_counter = Counter(scores)

    now = datetime.now()
    stale = [row for row in open_rows
             if row.status in ("OPEN", "PROCESSING") and row.created_at
             and (now - row.created_at).days >= 7]
    in_window_resolved = [row for row in rows if row.status in ("RESOLVED", "CLOSED")]

    metrics = [
        _metric("本期投诉量", len(rows), _delta_text(len(rows), len(prev_rows))),
        _metric("期内解决", len(in_window_resolved),
                hint=f"占本期 {(len(in_window_resolved) / len(rows) * 100) if rows else 0:.0f}%"),
        _metric("平均处理时效", f"{avg_hours:.1f} 小时",
                hint=f"最长 {max_hours:.1f} 小时（{len(durations)} 条已解决）"),
        _metric("平均满意度", f"{avg_score:.1f} / 5" if scores else "—",
                hint=f"{len(scores)} 条有评分"),
        _metric("未关闭工单", status_counter.get("OPEN", 0) + status_counter.get("PROCESSING", 0),
                hint=" ".join(f"{k}×{v}" for k, v in status_counter.items())),
        _metric("长期未决（≥7 天）", len(stale), hint="需重点跟进"),
    ]

    sections = [
        {"heading": "投诉分布与归类",
         "lines": [f"本期新增投诉 {len(rows)} 条"
                   f"（上期 {len(prev_rows)} 条），"
                   f"分类集中在：{'、'.join(f'{k} {v}' for k, v in category_counter.most_common(3)) or '—'}。"],
         "bullets": [f"{cat}：{count} 条" for cat, count in category_counter.most_common()]},
        {"heading": "处理时效与满意度",
         "lines": [f"已解决工单平均耗时 {avg_hours:.1f} 小时，最长 {max_hours:.1f} 小时；"
                   f"满意度均值 {'—' if not scores else f'{avg_score:.1f} / 5'}。"],
         "bullets": [f"{score} 分：{count} 条" for score, count in sorted(score_counter.items())]},
        {"heading": "长期未决与风险",
         "lines": ["以下工单超过 7 天仍未关闭，属于投诉处理周报必须点名的事项："],
         "bullets": [f"#{row.id} {row.category or '未分类'}：{(row.content or '')[:30]}"
                     for row in stale[:6]] or ["没有长期未决工单。"]},
    ]

    alerts = []
    if stale:
        alerts.append({"level": "high",
                       "text": f"{len(stale)} 条投诉超过 7 天未关闭，"
                               "建议指定专人限时闭环。"})
    if avg_hours > 48:
        alerts.append({"level": "normal",
                       "text": f"平均处理时效 {avg_hours:.1f} 小时超过 48 小时，"
                               "服务流程存在瓶颈。"})
    if scores and avg_score < 3.5:
        alerts.append({"level": "normal",
                       "text": f"平均满意度 {avg_score:.1f} 偏低，建议回访低分客户。"})

    tables = [{
        "title": "投诉明细",
        "columns": ["编号", "分类", "状态", "内容摘要", "处理时效(小时)", "满意度"],
        "rows": [[row.id, row.category or "未分类", row.status,
                  (row.summary or row.content or "")[:30],
                  (f"{(row.resolved_at - row.created_at).total_seconds() / 3600:.1f}"
                   if row.resolved_at else "未解决"),
                  row.satisfaction or "—"] for row in rows[:12]],
    }]
    if stale:
        tables.append({
            "title": "长期未决工单",
            "columns": ["编号", "分类", "状态", "已挂天数"],
            "rows": [[row.id, row.category or "未分类", row.status,
                      (now - row.created_at).days] for row in stale],
        })

    return {"summary": f"本期投诉 {len(rows)} 条（环比上期 {len(prev_rows)} 条），"
                       f"平均处理时效 {avg_hours:.1f} 小时，"
                       f"长期未决 {len(stale)} 条。",
            "metrics": metrics, "sections": sections, "tables": tables, "alerts": alerts,
            "notes": ["「平均处理时效」只统计有 resolved_at 的工单。",
                      "「长期未决」= 仍为 OPEN/PROCESSING 且创建时间超过 7 天。",
                      "满意度来自学生提交的 1-5 分评价。"]}


# --------------------------------------------------------------------------- #
# 快照构建入口
# --------------------------------------------------------------------------- #
_BUILDERS = {
    "customer_ops": _customer_ops,
    "daily_digest": lambda db, s, e: _digest(db, s, e, weekly=False),
    "weekly_digest": lambda db, s, e: _digest(db, s, e, weekly=True),
    "mental_weekly": _mental_weekly,
    "complaint_weekly": _complaint_weekly,
}


def build(db: Session, report_type: str, start: date, end: date) -> Dict[str, Any]:
    key = normalize_type(report_type)
    body = _BUILDERS[key](db, start, end)
    snapshot = {
        "report_type": key,
        "title": REPORT_TYPES[key]["title"],
        "scope": REPORT_TYPES[key]["scope"],
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": (end - start).days + 1,
                   "label": f"{start.isoformat()} ~ {end.isoformat()}"},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **body,
    }
    return snapshot


def generate(db: Session, *, report_type: str, params: Optional[dict] = None,
             period: Optional[Tuple[date, date]] = None,
             title: Optional[str] = None,
             generated_by: Optional[int] = None,
             from_schedule: bool = False,
             dify_runner: Optional[Dict[str, Any]] = None) -> ReportRecord:
    """生成一条报告记录（不 commit）。

    `period` 显式给报告期时不再重新推导 —— 调用方（API / 定时任务）算好的窗口
    必须原样用，否则「刚算出来的报告期」和「落库的报告期」可能差一天。

    `dify_runner` 可选：接上真实 Dify 时用它补一段「AI 叙述」，
    mock 模式下用本地模板渲染 —— 两条路的**统计数据完全一样**，
    差别只在叙述文字，这点在报告的口径说明里也写明了。
    """
    params = dict(params or {})
    key = normalize_type(report_type)
    if period is not None:
        start, end = period
    else:
        start, end = resolve_period(key,
                                    start=params.get("period_start") or params.get("start"),
                                    end=params.get("period_end") or params.get("end"))
    snapshot = build(db, key, start, end)

    title = title or params.get("title") \
        or f"{snapshot['title']}（{snapshot['period']['label']}）"
    # 快照里的标题跟着「用户实际看到的标题」走：markdown / PDF / Excel / 打印版
    # 四个出口都读快照，标题不一致会显得很业余。
    snapshot["type_title"] = REPORT_TYPES[key]["title"]
    snapshot["title"] = title
    row = ReportRecord(
        report_type=key, title=title, params=params,
        period_start=start, period_end=end, snapshot=snapshot,
        content=render_markdown(snapshot), status="DONE",
        generated_by=generated_by, from_schedule=from_schedule,
        dify_run_id=(dify_runner or {}).get("id") if dify_runner else None,
    )
    if dify_runner:
        narrative = str(dify_runner.get("content") or "").strip()
        if narrative:
            row.content = f"{row.content}\n\n## AI 叙述\n\n{narrative}\n"
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# 渲染出口
# --------------------------------------------------------------------------- #
def render_markdown(snapshot: Dict[str, Any]) -> str:
    lines = [f"# {snapshot['title']}", "",
             f"- 报告期：{snapshot['period']['label']}（{snapshot['period']['days']} 天）",
             f"- 覆盖范围：{snapshot['scope']}",
             f"- 生成时间：{snapshot['generated_at']}", "",
             f"**结论**：{snapshot['summary']}", "", "## 一、核心指标", "",
             "| 指标 | 数值 | 环比 / 说明 |", "| --- | --- | --- |"]
    for metric in snapshot["metrics"]:
        extra = metric.get("delta") or metric.get("hint") or "—"
        lines.append(f"| {metric['label']} | {metric['value']} | {extra} |")

    for index, section in enumerate(snapshot["sections"], start=2):
        lines += ["", f"## {_cn_number(index)}、{section['heading']}", ""]
        lines += [line for line in section.get("lines", [])]
        lines += [f"- {item}" for item in section.get("bullets", [])]

    for table in snapshot.get("tables", []):
        lines += ["", f"### {table['title']}", "",
                  "| " + " | ".join(table["columns"]) + " |",
                  "| " + " | ".join("---" for _ in table["columns"]) + " |"]
        for row in table["rows"]:
            lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
        if not table["rows"]:
            lines.append("| " + " | ".join("—" for _ in table["columns"]) + " |")

    if snapshot.get("alerts"):
        lines += ["", "## 风险提示", ""]
        for alert in snapshot["alerts"]:
            mark = "🔴" if alert["level"] == "high" else "⚠️"
            lines.append(f"- {mark} {alert['text']}")

    if snapshot.get("notes"):
        lines += ["", "## 口径说明", ""]
        lines += [f"- {note}" for note in snapshot["notes"]]
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (datetime, date)):
        return value.strftime(DATE_FMT)
    return str(value).replace("|", "／")


_CN = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")


def _cn_number(value: int) -> str:
    if value <= 10:
        return _CN[value]
    if value < 20:
        return "十" + _CN[value - 10]
    return str(value)


def pdf_blocks(snapshot: Dict[str, Any]) -> List[Tuple[str, Any]]:
    blocks: List[Tuple[str, Any]] = [
        ("keyvalues", [("报告期", snapshot["period"]["label"]),
                       ("覆盖范围", snapshot["scope"]),
                       ("生成时间", snapshot["generated_at"]),
                       ("结论", snapshot["summary"])]),
        ("spacer", 4.0),
        ("heading", "一、核心指标"),
        ("keyvalues", [(metric["label"],
                        f"{metric['value']}　{metric.get('delta') or metric.get('hint') or ''}")
                       for metric in snapshot["metrics"]]),
    ]
    for index, section in enumerate(snapshot["sections"], start=2):
        blocks.append(("heading", f"{_cn_number(index)}、{section['heading']}"))
        for line in section.get("lines", []):
            blocks.append(("paragraph", line))
        for bullet in section.get("bullets", []):
            blocks.append(("bullet", bullet))

    for table in snapshot.get("tables", []):
        blocks.append(("spacer", 4.0))
        blocks.append(("heading", table["title"]))
        blocks.append(("table", (table["columns"], [[_cell(v) for v in row]
                                                    for row in table["rows"]])))

    if snapshot.get("alerts"):
        blocks.append(("heading", "风险提示"))
        for alert in snapshot["alerts"]:
            blocks.append(("bullet", ("【高】" if alert["level"] == "high" else "【中】")
                                      + alert["text"]))
    if snapshot.get("notes"):
        blocks.append(("heading", "口径说明"))
        for note in snapshot["notes"]:
            blocks.append(("paragraph", "· " + note))
    return blocks


def render_pdf(snapshot: Dict[str, Any]) -> bytes:
    return pdf_writer.write_pdf(
        title=f"{snapshot['title']}（{snapshot['period']['label']}）",
        blocks=pdf_blocks(snapshot))


def excel_sheets(snapshot: Dict[str, Any]) -> List[xlsx_writer.Sheet]:
    overview: List[List[Any]] = [
        ["报告", snapshot["title"]],
        ["报告期", snapshot["period"]["label"]],
        ["覆盖范围", snapshot["scope"]],
        ["生成时间", snapshot["generated_at"]],
        [],
        ["结论", snapshot["summary"]],
        [],
        ["指标", "数值", "环比 / 说明"],
    ]
    for metric in snapshot["metrics"]:
        overview.append([metric["label"], metric["value"],
                         metric.get("delta") or metric.get("hint") or ""])

    sheets: List[xlsx_writer.Sheet] = [("概览", overview)]

    narrative: List[List[Any]] = [["章节", "内容"]]
    for section in snapshot["sections"]:
        for line in section.get("lines", []):
            narrative.append([section["heading"], line])
        for bullet in section.get("bullets", []):
            narrative.append([section["heading"], "· " + bullet])
    sheets.append(("分析结论", narrative))

    for table in snapshot.get("tables", []):
        rows: List[List[Any]] = [list(table["columns"])]
        rows += [[_cell(value) for value in row] for row in table["rows"]]
        sheets.append((table["title"][:31], rows))

    notes: List[List[Any]] = [["风险提示", "等级"]]
    notes += [[alert["text"], "高" if alert["level"] == "high" else "中"]
              for alert in snapshot.get("alerts", [])]
    notes += [[], ["口径说明", ""]] + [[note, ""] for note in snapshot.get("notes", [])]
    sheets.append(("风险与口径", notes))
    return sheets


def render_excel(snapshot: Dict[str, Any]) -> bytes:
    return xlsx_writer.write_workbook(excel_sheets(snapshot))


def render_print_html(snapshot: Dict[str, Any]) -> str:
    """打印就绪 HTML：由浏览器「另存为 PDF」。

    为什么这是 PDF 的**主通道**：服务端要直出「任何阅读器都正确显示中文」的 PDF，
    必须嵌入中文字体（字体文件几 MB 或引入字体子集化依赖）。浏览器打印走系统字体，
    中文渲染一定正确、零依赖 —— 这也是很多生产系统的做法。
    """
    def block(title: str, inner: str) -> str:
        return f'<section><h2>{escape(title)}</h2>{inner}</section>'

    cards = "".join(
        f'<div class="card"><div class="k">{escape(metric["label"])}</div>'
        f'<div class="v">{escape(str(metric["value"]))}</div>'
        f'<div class="h">{escape(str(metric.get("delta") or metric.get("hint") or ""))}</div></div>'
        for metric in snapshot["metrics"])

    body: List[str] = []
    for section in snapshot["sections"]:
        inner = "".join(f"<p>{escape(line)}</p>" for line in section.get("lines", []))
        inner += "<ul>" + "".join(f"<li>{escape(b)}</li>"
                                  for b in section.get("bullets", [])) + "</ul>"
        body.append(block(section["heading"], inner))

    for table in snapshot.get("tables", []):
        head = "".join(f"<th>{escape(str(c))}</th>" for c in table["columns"])
        rows = "".join("<tr>" + "".join(f"<td>{escape(_cell(v))}</td>" for v in row) + "</tr>"
                       for row in table["rows"])
        if not rows:
            rows = f'<tr><td colspan="{len(table["columns"])}">暂无数据</td></tr>'
        body.append(block(table["title"],
                          f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"))

    if snapshot.get("alerts"):
        body.append(block("风险提示", "<ul>" + "".join(
            f'<li class="{"high" if a["level"] == "high" else ""}">{escape(a["text"])}</li>'
            for a in snapshot["alerts"]) + "</ul>"))
    if snapshot.get("notes"):
        body.append(block("口径说明", "<ul>" + "".join(
            f"<li>{escape(note)}</li>" for note in snapshot["notes"]) + "</ul>"))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{escape(snapshot['title'])}</title>
<style>
  @page {{ size: A4; margin: 16mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px 28px; background: #fff; color: #111;
         font: 14px/1.7 "Microsoft YaHei","PingFang SC",-apple-system,sans-serif; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  .meta {{ color: #555; font-size: 12.5px; margin-bottom: 6px; }}
  .summary {{ background: #f4f7fb; border-left: 4px solid #3b6ea5;
              padding: 10px 12px; margin: 12px 0 18px; border-radius: 6px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 6px; }}
  .card {{ flex: 1 1 150px; border: 1px solid #e2e6ec; border-radius: 8px; padding: 9px 11px; }}
  .card .k {{ font-size: 12px; color: #666; }}
  .card .v {{ font-size: 18px; font-weight: 700; margin: 2px 0; }}
  .card .h {{ font-size: 11.5px; color: #777; }}
  h2 {{ font-size: 15px; margin: 20px 0 8px; padding-bottom: 5px;
        border-bottom: 1px solid #e2e6ec; }}
  ul {{ padding-left: 20px; margin: 6px 0; }}
  li {{ margin: 3px 0; }}
  li.high {{ color: #a11; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; }}
  th, td {{ border: 1px solid #dfe3e9; padding: 5px 7px; text-align: left; }}
  th {{ background: #eef3f8; }}
  .toolbar {{ position: fixed; right: 18px; top: 14px; }}
  .toolbar button {{ padding: 7px 14px; border-radius: 6px; border: 1px solid #3b6ea5;
                     background: #3b6ea5; color: #fff; cursor: pointer; font-size: 13px; }}
  @media print {{ .toolbar {{ display: none; }} body {{ padding: 0; }} }}
</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">打印 / 另存为 PDF</button></div>
<h1>{escape(snapshot['title'])}</h1>
<div class="meta">报告期：{escape(snapshot['period']['label'])}
  （{snapshot['period']['days']} 天）　覆盖范围：{escape(snapshot['scope'])}
  　生成时间：{escape(snapshot['generated_at'])}</div>
<div class="summary"><strong>结论：</strong>{escape(snapshot['summary'])}</div>
<div class="cards">{cards}</div>
{''.join(body)}
<script>window.addEventListener('load', function () {{ setTimeout(function () {{
  try {{ window.print(); }} catch (e) {{}} }}, 350); }});</script>
</body></html>
"""


# --------------------------------------------------------------------------- #
# 定时生成
# --------------------------------------------------------------------------- #
def due_types(now: Optional[datetime] = None) -> List[str]:
    """今天该生成哪几类：日报类每天，其余按周（默认周一）。

    用一个纯函数决定「该不该跑」，好处是定时线程 / 手动触发 / 测试
    都能拿同一天试出同一结果，不用等真实的钟表走到周一。
    """
    moment = now or datetime.now()
    due = ["daily_digest"]
    if moment.weekday() == 0:                     # 周一
        due += ["weekly_digest", "customer_ops", "mental_weekly", "complaint_weekly"]
    return due


def run_scheduled(db: Session, *, now: Optional[datetime] = None,
                  force: bool = False) -> Dict[str, Any]:
    """按 due_types 生成报告；同一 (类型, 报告期) 已存在则跳过（幂等）。"""
    moment = now or datetime.now()
    kinds = ALL_TYPE_KEYS if force else due_types(moment)
    created, skipped = [], []
    for key in kinds:
        start, end = resolve_period(key, anchor=moment.date())
        exists = (db.query(ReportRecord)
                  .filter(ReportRecord.report_type == key,
                          ReportRecord.period_start == start,
                          ReportRecord.period_end == end,
                          ReportRecord.from_schedule.is_(True)).first())
        if exists is not None:
            skipped.append({"report_type": key, "period_end": end.isoformat(),
                            "existing_id": exists.id})
            continue
        row = generate(db, report_type=key, period=(start, end), from_schedule=True)
        created.append({"report_type": key, "id": row.id, "title": row.title,
                        "period": row.snapshot["period"]["label"],
                        "summary": row.snapshot["summary"]})
    db.flush()
    return {"ran_at": moment.isoformat(timespec="seconds"), "due": kinds,
            "created": created, "skipped": skipped,
            "created_count": len(created), "skipped_count": len(skipped)}


def run_scheduled_with_push(db: Session, *, now: Optional[datetime] = None,
                            force: bool = False,
                            channel: Optional[str] = None) -> Dict[str, Any]:
    """定时生成 + 顺手推送（AC-08 的完整闭环）。

    生成与推送分开成两个函数（`run_scheduled` / `push`），但定时入口用这一个 ——
    否则「报告按时生成了，但没人收到」这个缺口会一直留着（这正是查验发现的
    SRS 4.5.3 / AC-08 缺口）。
    """
    result = run_scheduled(db, now=now, force=force)
    delivered = []
    for item in result["created"]:
        row = db.get(ReportRecord, item["id"])
        if row is None:
            continue
        outcome = push(db, row, channel=channel)
        delivered.append({"report_id": row.id, "report_type": row.report_type,
                          "sent": outcome["sent"], "skipped": outcome["skipped"],
                          "recipients": outcome["recipients"]})
    result["pushed"] = delivered
    result["pushed_count"] = sum(d["sent"] for d in delivered)
    db.flush()
    return result


# --------------------------------------------------------------------------- #
# 报告推送（AC-08 / SRS 4.5.3「支持在线查阅、导出与定时推送」）
# --------------------------------------------------------------------------- #
def _already_delivered(db: Session, report_id: int, subject: str) -> bool:
    from ..models import Notification

    return db.query(Notification).filter(
        Notification.category == "report",
        Notification.biz_type == "report_record",
        Notification.biz_id == str(report_id),
        Notification.recipient_subject == subject,
    ).first() is not None


def push(db: Session, row: ReportRecord, *, roles: Optional[Sequence[str]] = None,
         channel: Optional[str] = None) -> Dict[str, Any]:
    """把一份报告推给目标角色下的**全部在用账号**（不 commit）。

    幂等口径：同一 (报告, 收件账号) 只推一次 —— 重复点「推送」不会刷屏，
    这跟待办推送的 `dedupe_key` 是同一个思路。
    """
    from ..models import Notification, SysAccount
    from . import notify

    key = normalize_type(row.report_type)
    target_roles = tuple(roles) if roles else REPORT_TARGET_ROLES.get(key, ("manager", "admin"))
    accounts = (db.query(SysAccount)
                .filter(SysAccount.role.in_(target_roles), SysAccount.is_active.is_(True))
                .order_by(SysAccount.id.asc()).all())

    snapshot = row.snapshot or {}
    period = (snapshot.get("period") or {}).get("label") or ""
    summary = snapshot.get("summary") or ""
    body_parts = [f"报告期：{period}" if period else "", summary,
                  "可在「运营中心 → 报告」在线查阅与导出（Excel / 打印版 / PDF / Markdown）。"]
    body = "\n".join(p for p in body_parts if p)

    sent, skipped, rows_out = 0, 0, []
    for acc in accounts:
        if _already_delivered(db, row.id, acc.username):
            skipped += 1
            continue
        notice = notify.send(
            db, recipient_type="employee", recipient_id=acc.ref_id or acc.id,
            recipient_subject=acc.username, category="report",
            title=f"【报告】{row.title}", body=body,
            biz_type="report_record", biz_id=row.id, channel=channel,
        )
        sent += 1
        rows_out.append({"subject": acc.username, "role": acc.role,
                         "channel": notice.channel, "delivered_via": notice.delivered_via})
    return {"report_id": row.id, "roles": list(target_roles), "sent": sent,
            "skipped": skipped, "recipients": rows_out,
            "account_count": len(accounts)}


def deliveries(db: Session, report_id: int) -> List[Dict[str, Any]]:
    """某份报告的推送记录（界面上的「推送情况」）。"""
    from ..models import Notification
    from . import notify

    rows = (db.query(Notification)
            .filter(Notification.category == "report",
                    Notification.biz_type == "report_record",
                    Notification.biz_id == str(report_id))
            .order_by(Notification.id.desc()).all())
    return [notify.dump(r) for r in rows]
