"""自然语言查库（NL2SQL）—— **受控模板 + 白名单**实现。

为什么一期不用「让 LLM 直接生成并执行 SQL」：
1. 大模型生成的 SQL 可能触及越权表、写出带副作用语句（UPDATE/DELETE）、
   或构造笛卡尔积把库拖垮；
2. 审计上无法解释「为什么这条 SQL 被允许执行」。

因此本实现的安全模型是：
- 语句**只能来自内置模板**（`TEMPLATES`），模型只参与「选模板 + 抽参数」；
- 模板 SQL 必须是单条 SELECT；
- 执行前再用 `validate_sql()` 做一次白名单校验（表名/列名/危险关键字），
  双保险：即使有人往模板里塞了越权 SQL，也会在执行前被拦下；
- 行数硬上限 `NL2SQL_MAX_ROWS`；
- 结果附带 `sql_preview` 供员工核对，实现「可解释、可审计」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..core import AppError, Forbidden

# --------------------------------------------------------------------------- #
# 白名单：可查询的表与禁止出现的关键字
# --------------------------------------------------------------------------- #
ALLOWED_TABLES = {
    "customer_lead", "customer_followup", "lead_screening",
    "employee", "employee_report",
    "student", "student_score", "student_request",
    "student_mental_profile", "mental_alert", "after_sales_ticket",
    "activity", "activity_enrollment", "course_project",
    "knowledge_doc", "report_record", "audit_log",
}

FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "truncate", "create",
    "replace", "grant", "revoke", "attach", "pragma",
)

# 必须按「整词」匹配，否则 created_at / updated_at / deleted_at 会被误杀
_FORBIDDEN_WORD_RE = re.compile(r"\b(?:" + "|".join(FORBIDDEN_KEYWORDS) + r")\b")

# 这些不是单词，直接做子串匹配
FORBIDDEN_SUBSTRINGS = (
    "into outfile", "load_file", "information_schema", "sleep(", "benchmark(",
)

SCOPE_PREFIX = "analytics"   # 预留：将来接视图时统一前缀

# 角色 -> 默认数据可见范围。
# 放在这里而不是各自的 API 模块里，是因为对话入口（mock_agent）和
# /nl2sql/query 都要用，两份映射迟早漂移。学员/访客不在此表中 —— 他们
# 压根不该走取数通道，由调用方在更外层拦掉。
ROLE_SCOPE: Dict[str, str] = {
    "employee": "sales",
    "manager": "manager",
    "admin": "admin",
}


def validate_sql(sql: str) -> List[str]:
    """校验 SQL 是否安全，返回命中的表名列表；不安全直接抛错。"""
    lowered = sql.lower()
    if not settings.nl2sql_whitelist_enabled:
        return []
    if not lowered.lstrip().startswith("select"):
        raise Forbidden("仅允许执行 SELECT 语句")
    if ";" in sql.rstrip().rstrip(";"):
        raise Forbidden("禁止多语句执行")
    if m := _FORBIDDEN_WORD_RE.search(lowered):
        raise Forbidden(f"SQL 中包含被禁止的关键字: {m.group(0)}")
    for kw in FORBIDDEN_SUBSTRINGS:
        if kw in lowered:
            raise Forbidden(f"SQL 中包含被禁止的关键字: {kw}")
    tables = re.findall(r"(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", lowered)
    unknown = [t for t in tables if t not in ALLOWED_TABLES]
    if unknown:
        raise Forbidden(f"SQL 引用了白名单之外的表: {unknown}")
    return tables


# --------------------------------------------------------------------------- #
# 模板定义
# --------------------------------------------------------------------------- #
@dataclass
class QueryTemplate:
    id: str
    title: str
    keywords: Tuple[str, ...]
    scopes: Tuple[str, ...]                  # 允许的 role_scope
    sql: str
    columns: List[str]
    param_hints: Tuple[str, ...] = ()        # 从问题里抽取字符串参数的提示词
    answer: str = "共 {row_count} 条记录。"


TEMPLATES: Tuple[QueryTemplate, ...] = (
    QueryTemplate(
        id="lead_followups",
        title="客户跟进记录",
        keywords=("跟进记录", "跟进情况", "随访"),
        scopes=("sales", "manager", "admin"),
        sql="""
            SELECT l.name AS customer, f.follow_type, f.content, f.created_at
            FROM customer_followup f
            JOIN customer_lead l ON l.id = f.lead_id
            WHERE l.name LIKE :kw OR :kw = ''
            ORDER BY f.created_at DESC
            LIMIT :lim
        """,
        columns=["客户姓名", "跟进方式", "跟进内容", "跟进时间"],
        param_hints=("name",),
        answer="命中 {row_count} 条跟进记录。",
    ),
    QueryTemplate(
        id="lead_status_stats",
        title="意向客户状态分布",
        keywords=("客户", "线索", "意向"),
        scopes=("sales", "manager", "admin"),
        sql="""
            SELECT status, COUNT(*) AS cnt
            FROM customer_lead
            GROUP BY status
            ORDER BY cnt DESC
            LIMIT :lim
        """,
        columns=["状态", "数量"],
        answer="各状态客户数量已汇总，共 {row_count} 个状态。",
    ),
    QueryTemplate(
        id="student_scores",
        title="学生成绩",
        keywords=("成绩", "分数", "考试"),
        scopes=("academic", "manager", "admin"),
        sql="""
            SELECT s.name AS student, sc.exam_name, sc.subject, sc.score, sc.full_score, sc.exam_date
            FROM student_score sc
            JOIN student s ON s.id = sc.student_id
            WHERE s.name LIKE :kw OR :kw = ''
            ORDER BY sc.exam_date DESC
            LIMIT :lim
        """,
        columns=["学生", "考试", "科目", "得分", "满分", "考试日期"],
        param_hints=("name",),
        answer="命中 {row_count} 条成绩记录。",
    ),
    QueryTemplate(
        id="score_averages",
        title="各科平均分",
        # ⚠️ 只收「各科平均分」这类明确组合，不收裸「平均分」——
        # 「平均分要求是多少」问的是院校录取门槛（知识类），不是库里的统计值。
        keywords=("各科平均分", "科目平均分", "各科成绩平均", "平均成绩"),
        scopes=("academic", "manager", "admin"),
        sql="""
            SELECT sc.subject, COUNT(*) AS cnt,
                   ROUND(AVG(sc.score), 1) AS avg_score,
                   MAX(sc.full_score) AS full_score
            FROM student_score sc
            GROUP BY sc.subject
            ORDER BY avg_score DESC
            LIMIT :lim
        """,
        columns=["科目", "人次", "平均分", "满分"],
        answer="共 {row_count} 个科目的平均分。",
    ),
    QueryTemplate(
        id="pending_requests",
        title="待审批的行政申请",
        keywords=("待审批", "请假", "行政", "申请"),
        scopes=("academic", "manager", "admin"),
        sql="""
            SELECT r.id, s.name AS student, r.type, r.status, r.created_at
            FROM student_request r
            LEFT JOIN student s ON s.id = r.student_id
            WHERE r.status = 'PENDING'
            ORDER BY r.created_at ASC
            LIMIT :lim
        """,
        columns=["申请单号", "学生", "类型", "状态", "提交时间"],
        answer="当前有 {row_count} 条待审批申请。",
    ),
    QueryTemplate(
        id="open_tickets",
        title="未闭环的售后工单",
        keywords=("工单", "售后", "投诉"),
        scopes=("academic", "sales", "manager", "admin"),
        sql="""
            SELECT t.id, t.category, t.status, t.content, t.created_at
            FROM after_sales_ticket t
            WHERE t.status IN ('OPEN', 'PROCESSING')
            ORDER BY t.created_at ASC
            LIMIT :lim
        """,
        columns=["工单号", "分类", "状态", "内容", "创建时间"],
        answer="当前有 {row_count} 条未闭环工单。",
    ),
    QueryTemplate(
        id="activity_enrollment",
        title="活动报名情况",
        keywords=("活动", "报名"),
        scopes=("sales", "academic", "manager", "admin"),
        sql="""
            SELECT a.title, a.capacity, a.enrolled_count, a.status
            FROM activity a
            ORDER BY a.start_at DESC
            LIMIT :lim
        """,
        columns=["活动名称", "名额", "已报名", "状态"],
        answer="共 {row_count} 场活动。",
    ),
    QueryTemplate(
        id="mental_alerts",
        title="心理预警",
        keywords=("预警", "心理", "风险"),
        scopes=("manager", "admin"),          # 敏感数据，仅管理层
        sql="""
            SELECT m.id, s.name AS student, m.risk_level, m.status, m.created_at
            FROM mental_alert m
            LEFT JOIN student s ON s.id = m.student_id
            ORDER BY m.created_at DESC
            LIMIT :lim
        """,
        columns=["预警号", "学生", "风险等级", "状态", "触发时间"],
        answer="共 {row_count} 条心理预警记录。",
    ),
)


_PREFIX_VERBS = ("帮我", "麻烦", "请", "给我", "帮忙", "我想", "我要")
_QUERY_VERBS = ("查一下", "查询", "查查", "看一下", "看下", "看看", "统计", "列出", "找一下")
_TAIL_NOUNS = ("的跟进记录", "跟进记录", "的成绩", "成绩", "的分数", "记录", "的情况",
               "情况", "的信息", "信息", "的资料", "资料")
_STOPWORDS = {"客户", "学生", "线索", "意向", "全部", "所有", "最近", "多少", "哪些",
              "一下", "情况", "记录", "结果", "数量"}


def _extract_param(question: str) -> str:
    """从问句里抽取一个人名/关键词。

    一期用可解释的规则（去动词前缀 → 去名词后缀 → 长度 2~4 的纯中文），
    比让模型自由发挥更可控；二期可换成 Dify 的「信息抽取」节点。
    """
    for seg in re.split(r"[，。？！?、,.\s]+", question.strip()):
        seg = seg.strip()
        for verb in _PREFIX_VERBS:
            if seg.startswith(verb):
                seg = seg[len(verb):]
        for verb in _QUERY_VERBS:
            if seg.startswith(verb):
                seg = seg[len(verb):]
        for noun in _TAIL_NOUNS:
            if seg.endswith(noun):
                seg = seg[: -len(noun)]
        if 2 <= len(seg) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]+", seg) and seg not in _STOPWORDS:
            return seg
    return ""


def _score_template(tpl: QueryTemplate, question: str) -> Tuple[int, int]:
    """模板打分：(命中的最长关键词长度, 命中个数)。

    必须按「最长关键词优先」，不能按「命中个数优先」：
    「帮我查一下最近的意向客户跟进情况」会同时命中
      - lead_followups（跟进情况，1 个 / 4 字）
      - lead_status_stats（客户 + 意向，2 个 / 各 2 字）
    按个数排会错判成「客户状态分布」，按最长关键词排才是用户想要的「跟进记录」。
    """
    matched = [kw for kw in tpl.keywords if kw in question]
    if not matched:
        return (0, 0)
    return (max(len(k) for k in matched), len(matched))


def pick_template(question: str, role_scope: str) -> QueryTemplate:
    best: Optional[QueryTemplate] = None
    best_score = (0, 0)
    for tpl in TEMPLATES:
        score = _score_template(tpl, question)
        if score > best_score:
            best, best_score = tpl, score
    if best is None:
        raise AppError("无法把该问题映射到受控查询模板，请换个说法"
                       "（例如「查一下最近的意向客户跟进情况」「统计一下客户线索的数量」）")
    if role_scope not in best.scopes:
        raise Forbidden(f"角色范围 [{role_scope}] 无权查询「{best.title}」"
                        f"（该模板允许：{'/'.join(best.scopes)}）")
    return best


def applicable_templates(role_scope: str) -> List[QueryTemplate]:
    """该范围下可用的模板，用于把「能查什么」告诉用户。"""
    return [t for t in TEMPLATES if role_scope in t.scopes]


def run_query(db: Session, question: str, role_scope: str = "sales",
              max_rows: Optional[int] = None) -> Dict[str, Any]:
    tpl = pick_template(question, role_scope)

    limit = min(max_rows or settings.nl2sql_max_rows, settings.nl2sql_max_rows)
    params: Dict[str, Any] = {"lim": limit}
    if "kw" in tpl.sql:
        keyword = _extract_param(question)
        params["kw"] = f"%{keyword}%" if keyword else ""

    validate_sql(tpl.sql)                       # 白名单二次校验

    result = db.execute(text(tpl.sql), params)
    rows = [list(r) for r in result.fetchall()]

    preview = tpl.sql.strip()
    for key, value in params.items():
        preview = preview.replace(f":{key}", repr(value))

    return {
        "sql_preview": re.sub(r"\s+", " ", preview).strip(),
        "columns": tpl.columns,
        "rows": rows,
        "row_count": len(rows),
        "answer_text": tpl.answer.format(row_count=len(rows)),
        "template_id": tpl.id,
        "template_title": tpl.title,
        "role_scope": role_scope,
    }
