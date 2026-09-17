"""Agent 工具集（TDD 4.5 的 19 个工具）+ 写操作二次确认。

为什么单独一个服务层
--------------------
`app/api/v1/tools.py` 只负责「验签 → 取参 → 派发 → 包信封」这些传输层的事。
工具本身是**业务能力**，要和 HTTP 解耦（Dify 之外，编排器、定时任务、
前端确认按钮以后都可能直接调它），所以实现放在这里。

两类工具
--------
- `read`：直接执行，返回数据（查询类，无副作用）；
- `write`：**不直接执行**。第一次调用只落一条 `PendingAction` 并把「人话回显」
  返回给调用方；带 `confirm=<token>` 再调一次才真正执行
  （SRS 4.3.4 / TDD 4.5「写类工具执行前回显确认」，防的是审批、状态变更这类误操作）。

为什么 `preview` 是必填而不是可选
--------------------------------
一个写工具如果给不出人话回显，调用方就只能把 JSON 甩给用户 —— 那种「确认」
等于没确认。所以 `ToolSpec` 里 `write` 类**必须**提供 `preview`，
缺失直接在注册期就报错（见 `_validate_registry`）。

角色越权
--------
每个工具声明 `min_role`，调用方在请求体里带 `role`（Dify 会带上会话角色）。
缺失时按 `employee` 处理 —— 这条通道有 HMAC 签名保护且只对 Dify 容器网段放行，
但**不因此跳过检查**：审批类、报告类工具仍要求 manager 及以上。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..core import AppError, Forbidden, NotFound
from ..models import (Activity, ActivityEnrollment, AfterSalesTicket, CustomerFollowup,
                      CustomerLead, Employee, EmployeeReport, LeadScreening,
                      PendingAction, Student, StudentRequest)
from . import deadline, material, notify, onboarding, reports, rules, todo
from .nl2sql import run_query

logger = logging.getLogger(__name__)

Role = str
ROLE_RANK: Dict[str, int] = {"visitor": 0, "student": 1, "employee": 2, "manager": 3, "admin": 4}


@dataclass(frozen=True)
class ToolContext:
    """谁在调这个工具。写工具的确认留痕要用到。"""

    actor_subject: str = "dify"
    actor_role: str = "employee"

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.actor_role, 0)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    desc: str
    kind: str                                   # read | write
    params: Dict[str, Any] = field(default_factory=dict)
    required: Tuple[str, ...] = ()
    min_role: Role = "employee"
    handler: Callable[[Session, Dict[str, Any], ToolContext], Dict[str, Any]] = None  # type: ignore[assignment]
    preview: Optional[Callable[[Session, Dict[str, Any]], str]] = None
    legacy_of: Optional[str] = None             # 旧工具名（保留兼容）

    @property
    def is_write(self) -> bool:
        return self.kind == "write"

    def schema(self) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        for name, desc in self.params.items():
            optional = desc.endswith("?")
            properties[name] = {"type": "string", "description": desc.rstrip("?")}
            if optional:
                properties[name]["x-optional"] = True
        return {
            "name": self.name, "description": self.desc,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [r for r in self.required],
                "additionalProperties": False,
            },
            "x-kind": self.kind,
            "x-min-role": self.min_role,
        }


# --------------------------------------------------------------------------- #
# read 工具
# --------------------------------------------------------------------------- #
def _lead_query(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """按关键字 / 状态 / 归属查意向客户，带最近跟进。"""
    keyword = (params.get("keyword") or params.get("name") or "").strip()
    query = db.query(CustomerLead)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(CustomerLead.name.like(like) | CustomerLead.phone.like(like))
    if params.get("status"):
        query = query.filter(CustomerLead.status == str(params["status"]).upper())
    if params.get("phone"):
        query = query.filter(CustomerLead.phone == params["phone"])
    owner = params.get("owner") or params.get("owner_id")
    if owner not in (None, ""):
        if str(owner).isdigit():
            query = query.filter(CustomerLead.owner_id == int(owner))
        else:
            query = query.join(Employee, Employee.id == CustomerLead.owner_id).filter(
                Employee.name.like(f"%{owner}%"))
    leads = query.order_by(CustomerLead.id.desc()).limit(20).all()
    out = []
    for lead in leads:
        followups = (db.query(CustomerFollowup)
                     .filter(CustomerFollowup.lead_id == lead.id)
                     .order_by(CustomerFollowup.created_at.desc()).limit(3).all())
        out.append({
            "id": lead.id, "name": lead.name, "phone": lead.phone,
            "status": lead.status, "source": lead.source,
            "intention_country": lead.intention_country,
            "intention_stage": lead.intention_stage, "owner_id": lead.owner_id,
            "recent_followups": [{"content": f.content,
                                  "at": f.created_at.strftime("%Y-%m-%d %H:%M")}
                                 for f in followups],
        })
    return {"count": len(out), "leads": out}


def _student_scores(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    from ..models import StudentScore

    query = db.query(StudentScore).join(Student, Student.id == StudentScore.student_id)
    if params.get("student_id"):
        query = query.filter(StudentScore.student_id == int(params["student_id"]))
    if params.get("name"):
        query = query.filter(Student.name.like(f"%{params['name']}%"))
    rows = query.order_by(StudentScore.exam_date.desc()).limit(20).all()
    return {"count": len(rows),
            "scores": [{"student_id": r.student_id, "exam": r.exam_name,
                        "subject": r.subject, "score": float(r.score),
                        "full_score": float(r.full_score)} for r in rows]}


def _pending_requests(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    rows = (db.query(StudentRequest)
            .filter(StudentRequest.status == "PENDING")
            .order_by(StudentRequest.created_at.asc()).limit(50).all())
    return {"count": len(rows),
            "requests": [{"id": r.id, "student_id": r.student_id, "type": r.type,
                          "content": r.content} for r in rows]}


def _my_requests(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """学生查**自己**的行政申请记录（请假 / 考务），含审批状态与备注。

    与 `pending_requests` 的区别必须说清楚，否则很容易混用：
    - `pending_requests`：只看 `PENDING`、**不带学生维度**，是给审批人看的「待办池」；
    - `my_requests`：**必须带 student_id**，是学生视角的「我的记录」，含已批/已驳。

    调用方（Dify）负责传**当前会话用户自己的** student_id —— 本工具只认参数，
    认不出「这到底是不是他本人」，所以身份由上层保证。
    """
    rows = (_student_request_query(db, params)
            .order_by(StudentRequest.created_at.desc()).limit(20).all())
    return {
        "count": len(rows),
        "student_id": int(params["student_id"]),
        "requests": [{
            "id": r.id, "type": r.type, "status": r.status, "content": r.content,
            "remark": r.remark,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        } for r in rows],
    }


def _my_tickets(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """学生查**自己**提交过的售后工单与处理进度。

    同样必须带 student_id（见 `_my_requests` 的说明）。
    """
    student_id = int(params["student_id"])
    query = (db.query(AfterSalesTicket)
             .filter(AfterSalesTicket.student_id == student_id))
    status = str(params.get("status") or "").strip().upper()
    if status:
        query = query.filter(AfterSalesTicket.status == status)
    rows = query.order_by(AfterSalesTicket.created_at.desc()).limit(20).all()
    return {
        "count": len(rows),
        "student_id": student_id,
        "tickets": [{
            "id": r.id, "summary": r.summary, "category": r.category,
            "status": r.status, "satisfaction": r.satisfaction,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        } for r in rows],
    }


def _my_scores(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """学生查**自己**的成绩（历次考试 / 科目 / 分数 / 满分 / 考试日期）。

    和 `student_scores` 的区别，跟 `my_requests` / `pending_requests` 是同一个道理：

    - `student_scores`：早期只读接口，`student_id` **可选**，不传就跨学生返回
      （limit 20），`min_role` 走默认的 `employee` —— 是给顾问 / 管理层查库用的；
    - `my_scores`：**必须带 student_id**，只返回该学生的记录，`min_role=student`
      —— 是学生端「我的成绩」。

    身份同样由上层保证（Dify 传当前会话用户自己的 student_id），本工具只认参数。
    """
    student_id = params.get("student_id")
    if student_id in (None, ""):
        raise AppError("my_scores 需要 student_id（只允许查本人的成绩）")

    from ..models import StudentScore

    rows = (db.query(StudentScore)
            .filter(StudentScore.student_id == int(student_id))
            .order_by(StudentScore.exam_date.desc()).limit(20).all())
    return {
        "count": len(rows),
        "student_id": int(student_id),
        "scores": [{
            "id": r.id, "exam": r.exam_name, "subject": r.subject,
            "score": float(r.score), "full_score": float(r.full_score),
            "exam_date": r.exam_date.isoformat() if r.exam_date else None,
        } for r in rows],
    }


def _student_request_query(db: Session, params: Dict[str, Any]):
    """构造「我的行政申请」查询：student_id 必填，status 可选。"""
    student_id = params.get("student_id")
    if student_id in (None, ""):
        raise AppError("my_requests 需要 student_id（只允许查本人的记录）")
    query = db.query(StudentRequest).filter(StudentRequest.student_id == int(student_id))
    status = str(params.get("status") or "").strip().upper()
    if status:
        query = query.filter(StudentRequest.status == status)
    return query


def _nl2sql_query(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    question = params.get("question")
    if not question:
        raise Forbidden("nl2sql_query 工具需要 question 参数")
    scope = params.get("role_scope") or params.get("scope") or "sales"
    return run_query(db, question, scope, params.get("max_rows"))


def _progress_query(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """REQ-M4-05：申请进度（粗粒度阶段 + 细项看板 + 考务节点）。

    一次给全三样：学生问「我进度到哪了」时，只回答 `stage` 是不够的 ——
    真正想知道的是「哪些还没做、谁在跟、什么时候要交」。
    """
    if not params.get("student_id"):
        raise Forbidden("progress_query 需要 student_id")
    student_id = int(params["student_id"])
    student = db.get(Student, student_id)
    if student is None:
        raise AppError(f"学生 {student_id} 不存在")

    order = ["PREPARING", "APPLYING", "OFFERED", "VISA", "ENROLLED"]
    current = order.index(student.stage) if student.stage in order else 0
    return {
        "student_id": student_id, "name": student.name, "stage": student.stage,
        "timeline": [{"stage": s, "done": i <= current, "current": i == current}
                     for i, s in enumerate(order)],
        "board": deadline.progress_board(db, student_id=student_id),
        "deadlines": [deadline.dump_deadline(r)
                      for r in deadline.list_deadlines(db, student_id=student_id,
                                                       include_done=False)],
    }


def _org_query(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """组织架构 / 花名册查询（部门、关键字、岗位角色）。"""
    query = db.query(Employee).filter(Employee.status == "ACTIVE")
    if params.get("department"):
        query = query.filter(Employee.department.like(f"%{params['department']}%"))
    if params.get("keyword"):
        like = f"%{params['keyword']}%"
        query = query.filter(Employee.name.like(like) | Employee.title.like(like))
    if params.get("biz_role"):
        query = query.filter(Employee.biz_role == str(params["biz_role"]).strip())
    rows = query.order_by(Employee.id.asc()).limit(50).all()
    return {"count": len(rows),
            "employees": [{"id": e.id, "emp_no": e.emp_no, "name": e.name,
                           "department": e.department, "title": e.title,
                           "biz_role": e.biz_role, "manager_id": e.manager_id,
                           "phone": e.phone, "email": e.email} for e in rows]}


def _onboarding_query(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """入职指引 / 清单 / FAQ / 联系人。给了 keyword 且带到问句时走 FAQ 检索。"""
    keyword = (params.get("keyword") or "").strip()
    stage = params.get("stage")
    employee_id = params.get("employee_id")
    payload: Dict[str, Any] = {}
    if stage:
        payload["checklist"] = onboarding.checklist(stage)
    if keyword:
        payload["faq"] = onboarding.search_faq(keyword)
    if employee_id not in (None, ""):
        payload["contacts"] = onboarding.resolve_contacts(db, int(employee_id))
    if not payload:
        # 什么都没给 → 返回完整指引（含联系人卡片，新人第一次问就是这个场景）
        payload = {"guide": onboarding.guide(),
                   "contacts": onboarding.resolve_contacts(db)}
    return payload


# --------------------------------------------------------------------------- #
# write 工具（先回显确认，再执行）
# --------------------------------------------------------------------------- #
def _preview_lead_create(db: Session, params: Dict[str, Any]) -> str:
    return (f"即将新增意向客户：姓名 {params.get('name')}、手机号 {params.get('phone')}、"
            f"来源 {params.get('source') or '—'}、意向国家 {params.get('intention') or '—'}。"
            "确认后立即写入客户库。")


def _lead_create(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    name = (params.get("name") or "").strip()
    phone = (params.get("phone") or "").strip()
    if not name or not phone:
        raise AppError("lead_create 需要 name 与 phone")
    exists = db.query(CustomerLead).filter(CustomerLead.phone == phone).first()
    if exists is not None:
        raise AppError(f"手机号 {phone} 已存在（客户 #{exists.id} {exists.name}），不再重复创建")
    lead = CustomerLead(name=name, phone=phone, source=params.get("source"),
                        intention_country=params.get("intention") or params.get("intention_country"),
                        intention_stage=params.get("intention_stage"),
                        remark=params.get("remark"), status="NEW")
    db.add(lead)
    db.flush()
    return {"id": lead.id, "name": lead.name, "phone": lead.phone, "status": lead.status}


def _preview_lead_update_status(db: Session, params: Dict[str, Any]) -> str:
    lead = db.get(CustomerLead, int(params.get("lead_id") or 0))
    current = lead.status if lead else "?"
    who = f"{lead.name}" if lead else f"#{params.get('lead_id')}"
    return (f"即将把客户「{who}」的状态从 {current} 改为 {params.get('status')}。"
            "确认后立即生效。")


def _lead_update_status(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    lead = db.get(CustomerLead, int(params.get("lead_id") or 0))
    if lead is None:
        raise AppError(f"意向客户 {params.get('lead_id')} 不存在")
    target = str(params.get("status") or "").upper()
    allowed = {"NEW", "FOLLOWING", "SIGNED", "LOST"}
    if target not in allowed:
        raise AppError(f"status 只能是 {sorted(allowed)}，收到 {params.get('status')!r}")
    before = lead.status
    lead.status = target
    db.flush()
    return {"id": lead.id, "before": before, "status": lead.status}


def _preview_screening_analyze(db: Session, params: Dict[str, Any]) -> str:
    text = (params.get("raw_text") or params.get("text") or "")
    target = f"意向客户 #{params['lead_id']}" if params.get("lead_id") else "未关联客户"
    return (f"即将对{target}发起画像研判（材料 {len(text)} 字"
            f"{'、附件 ' + str(params.get('file_id')) if params.get('file_id') else ''}），"
            "结果会落库并写入研判记录。")


def _screening_analyze(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    """本地规则引擎研判（与 `/screening/analyze` 同一口径，见 services/rules.py）。"""
    text = params.get("raw_text") or params.get("text") or ""
    lead_id = params.get("lead_id")
    if lead_id not in (None, "") and db.get(CustomerLead, int(lead_id)) is None:
        raise AppError(f"意向客户 {lead_id} 不存在")
    if not text.strip():
        raise AppError("screening_analyze 需要 raw_text")

    fields = material.extract_fields(text)
    active = rules.load_active(db)
    if active:
        verdict = rules.evaluate(active, fields, text)
        conclusion = verdict["conclusion"]
        confidence = verdict["confidence"]
        hit_products = [{"key": p["product_key"], "name": p["product_name"],
                         "conclusion": p["conclusion"], "match": p["match"],
                         "confidence": p["confidence"], "rule_version": p["version"],
                         "reason": p["reason"]} for p in verdict["products"]]
        evidence = verdict["evidence"]
        rule_version = "、".join(f"{k}:{v}" for k, v in verdict["rule_versions"].items())
        rule_source = "local"
    else:
        conclusion, confidence = "信息不足", 0.0
        hit_products, evidence, rule_version, rule_source = None, [], None, "dify"

    row = LeadScreening(lead_id=int(lead_id) if lead_id not in (None, "") else None,
                        source_type="TEXT", source_name="工具调用",
                        extracted_fields=fields,
                        missing_fields=material.missing_fields(fields),
                        hit_products=hit_products, conclusion=conclusion,
                        ai_conclusion=conclusion, evidence=evidence,
                        confidence=confidence, review_status="PENDING",
                        rule_version=rule_version, rule_source=rule_source)
    db.add(row)
    db.flush()
    return {"id": row.id, "conclusion": conclusion, "confidence": float(confidence),
            "fields": fields, "rule_version": rule_version,
            "products": [{"name": p["name"], "conclusion": p["conclusion"],
                          "reason": p["reason"]} for p in (hit_products or [])]}


def _preview_screening_batch(db: Session, params: Dict[str, Any]) -> str:
    items = params.get("items") or []
    lead_ids = params.get("lead_ids") or []
    return (f"即将批量研判 {len(items)} 份材料"
            f"{'（另含客户 ' + '、'.join('#' + str(i) for i in lead_ids) + '）' if lead_ids else ''}，"
            "每条都会单独落库，单条失败不影响其余。")


def _screening_batch(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    items = params.get("items") or []
    if not isinstance(items, list):
        raise AppError("items 必须是数组")
    # `lead_ids` 是「按客户研判」：材料不在参数里，而是该客户的备注 + 最近跟进，
    # 否则这些条目会因为「没有 raw_text」而全部失败，批量入口等于废掉。
    by_lead = [{"lead_id": i, "raw_text": _lead_text(db, i)}
               for i in (params.get("lead_ids") or [])]
    items = list(items) + by_lead
    if not items:
        raise AppError("screening_batch 需要 items 或 lead_ids")
    succeeded, failed, results = 0, 0, []
    for index, item in enumerate(items):
        raw = item if isinstance(item, dict) else {"raw_text": str(item)}
        try:
            out = _screening_analyze(db, raw, ctx)
            succeeded += 1
            results.append({"index": index, "ok": True, **out})
        except Exception as exc:                       # noqa: BLE001 - 单条失败不牵连整批
            failed += 1
            results.append({"index": index, "ok": False, "error": str(exc)})
    return {"total": len(items), "succeeded": succeeded, "failed": failed, "items": results}


def _lead_text(db: Session, lead_id: Any) -> str:
    """把一个客户的现有信息拼成可研判的文本（备注 + 最近跟进）。"""
    lead = db.get(CustomerLead, int(lead_id or 0))
    if lead is None:
        raise AppError(f"意向客户 {lead_id} 不存在")
    parts = [f"姓名 {lead.name}", lead.remark or "",
             f"意向国家 {lead.intention_country}" if lead.intention_country else "",
             f"意向阶段 {lead.intention_stage}" if lead.intention_stage else ""]
    followups = (db.query(CustomerFollowup)
                 .filter(CustomerFollowup.lead_id == lead.id)
                 .order_by(CustomerFollowup.created_at.desc()).limit(5).all())
    parts += [f.content for f in followups]
    return "\n".join(p for p in parts if p)


def _preview_screening_review(db: Session, params: Dict[str, Any]) -> str:
    row = db.get(LeadScreening, int(params.get("screening_id") or 0))
    current = row.conclusion if row else "?"
    return (f"即将把研判 #{params.get('screening_id')} 的结论从 {current} 改为 "
            f"{params.get('conclusion') or '（按 ' + str(params.get('action')) + ' 处理）'}"
            f"{'，备注：' + str(params.get('remark')) if params.get('remark') else ''}。"
            "确认后写入人工复核结论。")


def _screening_review(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    row = db.get(LeadScreening, int(params.get("screening_id") or 0))
    if row is None:
        raise AppError(f"研判记录 {params.get('screening_id')} 不存在")
    action = str(params.get("action") or "CONFIRM").upper()
    if action not in ("CONFIRM", "OVERRIDE"):
        raise AppError("action 只能是 CONFIRM 或 OVERRIDE")
    before = row.conclusion
    if action == "OVERRIDE":
        target = params.get("conclusion")
        if target not in ("符合", "不符合", "信息不足"):
            raise AppError("OVERRIDE 时 conclusion 必须是 符合 / 不符合 / 信息不足")
        row.conclusion = target
    row.review_status = "CONFIRMED" if action == "CONFIRM" else "OVERRIDDEN"
    row.review_remark = params.get("remark")
    db.flush()
    return {"id": row.id, "action": action, "before": before,
            "conclusion": row.conclusion, "review_status": row.review_status}


def _preview_material_upload(db: Session, params: Dict[str, Any]) -> str:
    text = params.get("text") or params.get("raw_text") or ""
    filename = params.get("filename") or "文本材料"
    return (f"即将解析材料「{filename}」并抽取关键字段"
            f"（正文 {len(text)} 字{('，关联客户 #' + str(params['lead_id'])) if params.get('lead_id') else ''}）。"
            "确认后返回抽出的字段，**不落研判记录**。") 


def _material_upload(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    text = params.get("text") or params.get("raw_text") or ""
    if not text.strip():
        raise AppError("material_upload 需要 text（或由上传通道解析出的正文）")
    fields = material.extract_fields(text)
    return {"filename": params.get("filename") or "文本材料",
            "text_length": len(text), "fields": fields,
            "missing_fields": material.missing_fields(fields)}


def _preview_report_daily_submit(db: Session, params: Dict[str, Any]) -> str:
    content = params.get("content") or ""
    return (f"即将为员工 #{params.get('employee_id') or '（未指定）'} 提交 {params.get('date') or '今日'} 日报"
            f"（{len(content)} 字）。确认后写入日报并计入汇总。")


def _report_daily_submit(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    employee_id = params.get("employee_id")
    if employee_id in (None, ""):
        raise AppError("report_daily_submit 需要 employee_id")
    if db.get(Employee, int(employee_id)) is None:
        raise AppError(f"员工 {employee_id} 不存在")
    content = (params.get("content") or "").strip()
    if not content:
        raise AppError("report_daily_submit 需要 content")
    raw_date = params.get("date")
    day = date.fromisoformat(raw_date) if raw_date else date.today()
    summary = content[:60] + ("..." if len(content) > 60 else "")
    row = EmployeeReport(employee_id=int(employee_id), report_date=day,
                         content=content, summary=summary,
                         source=params.get("source") or "dify")
    db.add(row)
    db.flush()
    return {"id": row.id, "employee_id": row.employee_id,
            "report_date": row.report_date.isoformat(), "summary": row.summary}


def _preview_leave_apply(db: Session, params: Dict[str, Any]) -> str:
    student = db.get(Student, int(params.get("student_id") or 0))
    who = student.name if student else f"学生 #{params.get('student_id')}"
    return (f"即将为{who}提交请假申请：{params.get('start_date') or '（未填起始日）'} 起 "
            f"{params.get('days') or '?'} 天，事由：{params.get('reason') or '未填写'}。"
            "确认后提交待审批。")


def _leave_apply(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    student_id = params.get("student_id")
    if student_id in (None, ""):
        raise AppError("leave_apply 需要 student_id")
    if db.get(Student, int(student_id)) is None:
        raise AppError(f"学生 {student_id} 不存在")
    start = params.get("start_date")
    payload = {"start_date": start, "days": params.get("days"),
               "reason": params.get("reason")}
    row = StudentRequest(student_id=int(student_id),
                         type=str(params.get("request_type") or "LEAVE").upper(),
                         content=payload, status="PENDING")
    db.add(row)
    db.flush()
    return {"id": row.id, "student_id": row.student_id, "type": row.type,
            "status": row.status, "content": payload}


def _preview_leave_approve(db: Session, params: Dict[str, Any]) -> str:
    row = db.get(StudentRequest, int(params.get("request_id") or 0))
    if row is None:
        raise AppError(f"行政申请 {params.get('request_id')} 不存在")
    # 回显阶段就做**只读**前置校验：等用户点完「确认」再告诉他「这条已经批过了」
    # 是糟糕的体验，而且审批这种不可逆动作更不该走到那一步才发现问题。
    if row.status != "PENDING":
        raise AppError(f"申请 {row.id} 已是 {row.status}，不能重复审批")
    who = f"学生 #{row.student_id}"
    verdict = "通过" if str(params.get("action", "")).upper() in ("APPROVE", "APPROVED", "TRUE", "YES") else "驳回"
    return (f"即将把{who}的行政申请 #{row.id} 审批为「{verdict}」"
            f"{'（意见：' + str(params.get('remark')) + '）' if params.get('remark') else ''}，"
            "并通知申请人。审批不可撤销，请确认。")


def _leave_approve(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    row = db.get(StudentRequest, int(params.get("request_id") or 0))
    if row is None:
        raise AppError(f"行政申请 {params.get('request_id')} 不存在")
    if row.status != "PENDING":
        raise AppError(f"申请 {row.id} 已是 {row.status}，不能重复审批")
    action = str(params.get("action") or "").upper()
    approve = action in ("APPROVE", "APPROVED", "TRUE", "YES")
    row.status = "APPROVED" if approve else "REJECTED"
    row.approved_at = datetime.now()
    row.remark = params.get("remark")
    verdict = "已通过" if approve else "已驳回"
    labels = {"LEAVE": "请假申请", "EXAM": "考务申请", "OTHER": "其他申请"}
    notice = notify.send(db, recipient_type="student", recipient_id=row.student_id,
                         category="leave",
                         title=f"【行政申请{verdict}】{labels.get(row.type, '行政申请')}",
                         body="\n".join(p for p in [
                             f"申请编号：{row.id}", f"结果：{verdict}",
                             f"审批意见：{row.remark}" if row.remark else ""] if p),
                         biz_type="student_request", biz_id=row.id)
    db.flush()
    return {"id": row.id, "status": row.status, "approved_at": row.approved_at.isoformat(),
            "notify": {"channel": notice.channel, "delivered_via": notice.delivered_via}}


def _preview_ticket_create(db: Session, params: Dict[str, Any]) -> str:
    content = params.get("content") or ""
    return (f"即将创建售后工单（分类 {params.get('category') or '未分类'}，"
            f"学生 {params.get('student_id') or '未关联'}）：{content[:50]}"
            f"{'...' if len(content) > 50 else ''}。确认后进入处理队列。")


def _ticket_create(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    content = (params.get("content") or "").strip()
    if not content:
        raise AppError("ticket_create 需要 content")
    student_id = params.get("student_id")
    if student_id not in (None, "") and db.get(Student, int(student_id)) is None:
        raise AppError(f"学生 {student_id} 不存在")
    row = AfterSalesTicket(student_id=int(student_id) if student_id not in (None, "") else None,
                           content=content,
                           summary=content[:60] + ("..." if len(content) > 60 else ""),
                           category=params.get("category"), status="OPEN")
    db.add(row)
    db.flush()
    return {"id": row.id, "status": row.status, "summary": row.summary}


def _preview_ticket_update(db: Session, params: Dict[str, Any]) -> str:
    row = db.get(AfterSalesTicket, int(params.get("ticket_id") or 0))
    before = row.status if row else "?"
    return (f"即将把工单 #{params.get('ticket_id')} 的状态从 {before} 改为 {params.get('status')}"
            f"{'，并通知学生' if str(params.get('status', '')).upper() in ('RESOLVED', 'CLOSED') else ''}。")


def _ticket_update(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    row = db.get(AfterSalesTicket, int(params.get("ticket_id") or 0))
    if row is None:
        raise AppError(f"工单 {params.get('ticket_id')} 不存在")
    target = str(params.get("status") or "").upper()
    allowed = {"OPEN", "PROCESSING", "RESOLVED", "CLOSED"}
    if target not in allowed:
        raise AppError(f"status 只能是 {sorted(allowed)}，收到 {params.get('status')!r}")
    before = row.status
    row.status = target
    if target in ("RESOLVED", "CLOSED"):
        row.resolved_at = datetime.now()
    if params.get("handler_id") not in (None, ""):
        row.handler_id = int(params["handler_id"])
    notice = None
    if target in ("RESOLVED", "CLOSED") and before not in ("RESOLVED", "CLOSED") \
            and row.student_id is not None:
        notice = notify.send(db, recipient_type="student", recipient_id=row.student_id,
                             category="ticket",
                             title=f"【售后工单{'已解决' if target == 'RESOLVED' else '已关闭'}】"
                                   f"{row.summary or ''}".strip(),
                             body=f"工单编号：{row.id}\n处理结果：{target}",
                             biz_type="after_sales_ticket", biz_id=row.id)
    db.flush()
    return {"id": row.id, "before": before, "status": row.status,
            "notify": ({"delivered_via": notice.delivered_via} if notice else None)}


def _preview_activity_enroll(db: Session, params: Dict[str, Any]) -> str:
    act = db.get(Activity, int(params.get("activity_id") or 0))
    title = act.title if act else f"活动 #{params.get('activity_id')}"
    who = params.get("student_id") or params.get("lead_id") or "未指定报名人"
    return (f"即将把 {who} 报名到活动「{title}」"
            f"{'（剩余名额 ' + str((act.capacity or 0) - act.enrolled_count) + '）' if act and act.capacity else ''}。"
            "确认后占用一个名额。")


def _activity_enroll(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    act = db.get(Activity, int(params.get("activity_id") or 0))
    if act is None:
        raise AppError(f"活动 {params.get('activity_id')} 不存在")
    if act.status != "OPEN":
        raise AppError(f"活动「{act.title}」当前状态为 {act.status}，不接受报名")
    if act.capacity and act.enrolled_count >= act.capacity:
        raise AppError(f"活动「{act.title}」名额已满（{act.enrolled_count}/{act.capacity}）")
    student_id = params.get("student_id")
    lead_id = params.get("lead_id")
    if student_id in (None, "") and lead_id in (None, ""):
        raise AppError("activity_enroll 需要 student_id 或 lead_id")
    duplicate = (db.query(ActivityEnrollment)
                 .filter(ActivityEnrollment.activity_id == act.id,
                         ActivityEnrollment.student_id == (int(student_id) if student_id not in (None, "") else None),
                         ActivityEnrollment.lead_id == (int(lead_id) if lead_id not in (None, "") else None))
                 .first())
    if duplicate is not None:
        raise AppError("该报名人已在活动名单中，不重复报名")
    row = ActivityEnrollment(activity_id=act.id,
                             student_id=int(student_id) if student_id not in (None, "") else None,
                             lead_id=int(lead_id) if lead_id not in (None, "") else None,
                             status="ENROLLED")
    db.add(row)
    act.enrolled_count = (act.enrolled_count or 0) + 1
    db.flush()
    return {"enrollment_id": row.id, "activity_id": act.id, "title": act.title,
            "enrolled_count": act.enrolled_count, "capacity": act.capacity}


def _preview_todo_push(db: Session, params: Dict[str, Any]) -> str:
    who = "全部在职员工" if params.get("all_staff", True) else f"员工 {params.get('subject')}"
    cats = params.get("categories") or ["全部类别"]
    return (f"即将向{who}推送待办与心理预警触达（{'、'.join(cats)}）。"
            "确认后立即发送，收件人会收到通知。")


def _todo_push(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    if params.get("all_staff", True):
        result = todo.run_once()
    else:
        subject = params.get("subject")
        if not subject:
            raise AppError("非全员推送时 subject 必填")
        result = todo.run_once(only_subject=subject)
    return {"staff_count": result["staff_count"], "pushed": result["pushed"],
            "alerts_delivered": result["alerts_delivered"]}


def _preview_report_generate(db: Session, params: Dict[str, Any]) -> str:
    return (f"即将生成「{params.get('report_type') or 'weekly_digest'}」报告"
            f"（{params.get('period_start') or '按默认窗口'} ~ {params.get('period_end') or '今天'}）。"
            "确认后落库为可查阅/可导出的报告。")


def _report_generate(db: Session, params: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
    try:
        key = reports.normalize_type(params.get("report_type"))
    except ValueError as exc:
        raise AppError(str(exc)) from exc
    start, end = reports.resolve_period(key, start=params.get("period_start"),
                                        end=params.get("period_end"))
    row = reports.generate(db, report_type=key, period=(start, end))
    db.flush()
    return {"id": row.id, "report_type": row.report_type, "title": row.title,
            "status": row.status,
            "period": row.snapshot.get("period", {}) if row.snapshot else {}}


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
SPECS: Tuple[ToolSpec, ...] = (
    # ---- read ----
    ToolSpec("lead_query", "查询意向客户（关键字 / 手机号 / 状态 / 归属）", "read",
             params={"keyword": "string?", "phone": "string?", "status": "string?",
                     "owner": "string?"}, handler=_lead_query),
    ToolSpec("progress_query", "查询学生申请进度（阶段 + 细项看板 + 考务节点）", "read",
             params={"student_id": "int"}, required=("student_id",), handler=_progress_query),
    ToolSpec("nl2sql_query", "自然语言查库（受控模板，需 role_scope 限定范围）", "read",
             params={"question": "string", "role_scope": "string?", "max_rows": "int?"},
             required=("question",), handler=_nl2sql_query),
    ToolSpec("org_query", "组织架构 / 花名册查询（部门、关键字、岗位角色）", "read",
             params={"department": "string?", "keyword": "string?", "biz_role": "string?"},
             handler=_org_query),
    ToolSpec("onboarding_query", "入职指引 / 清单 / FAQ / 联系人", "read",
             params={"stage": "string?", "keyword": "string?", "employee_id": "int?"},
             handler=_onboarding_query),
    # 早期版本的三个只读工具（保留，避免已配置的 Dify Tool 节点失效）
    ToolSpec("student_scores", "查询学生成绩（早期只读接口，保留兼容）", "read",
             params={"student_id": "int?", "name": "string?"}, handler=_student_scores,
             legacy_of="—"),
    ToolSpec("pending_requests", "查询待审批的行政申请（早期只读接口，保留兼容）", "read",
             params={}, handler=_pending_requests, legacy_of="—"),
    # 学生视角的「我的记录」。与 pending_requests 的分工写在 `_my_requests` 的注释里：
    # 那两个是「审批人的待办池」，这两个是「学生查自己」，**必须带 student_id**。
    ToolSpec("my_requests", "查询某学生的行政申请记录（请假 / 考务，含审批状态与备注）", "read",
             params={"student_id": "int", "status": "string?"},
             required=("student_id",), min_role="student", handler=_my_requests),
    ToolSpec("my_tickets", "查询某学生提交过的售后工单与处理进度（含满意度）", "read",
             params={"student_id": "int", "status": "string?"},
             required=("student_id",), min_role="student", handler=_my_tickets),
    # 学生端的「我的成绩」。注意别和上面遗留的 `student_scores` 混用 ——
    # 那个 student_id 可选、且 min_role 是默认的 employee，学生调用会被 403。
    ToolSpec("my_scores", "查询某学生本人的成绩（历次考试 / 科目 / 分数 / 满分）", "read",
             params={"student_id": "int"},
             required=("student_id",), min_role="student", handler=_my_scores),
    ToolSpec("lead_lookup", "按姓名/手机号查意向客户（早期名称，等价 lead_query）", "read",
             params={"name": "string?", "phone": "string?"}, handler=_lead_query,
             legacy_of="lead_query"),
    ToolSpec("nl2sql", "自然语言查库（早期名称，等价 nl2sql_query）", "read",
             params={"question": "string", "scope": "string?", "max_rows": "int?"},
             required=("question",), handler=_nl2sql_query, legacy_of="nl2sql_query"),

    # ---- write（需二次确认）----
    ToolSpec("screening_analyze", "客户画像研判（本地规则引擎，落库并留痕）", "write",
             params={"raw_text": "string", "lead_id": "int?", "file_id": "string?"},
             required=("raw_text",), handler=_screening_analyze,
             preview=_preview_screening_analyze),
    ToolSpec("screening_batch", "批量研判（材料 / 客户），单条失败不牵连整批", "write",
             params={"items": "array", "lead_ids": "array?"}, handler=_screening_batch,
             preview=_preview_screening_batch),
    ToolSpec("screening_review", "人工复核并回写研判结论", "write",
             params={"screening_id": "int", "action": "string", "conclusion": "string?",
                     "remark": "string?"}, required=("screening_id", "action"),
             handler=_screening_review, preview=_preview_screening_review),
    ToolSpec("lead_create", "新增意向客户", "write",
             params={"name": "string", "phone": "string", "source": "string?",
                     "intention": "string?"}, required=("name", "phone"),
             handler=_lead_create, preview=_preview_lead_create),
    ToolSpec("lead_update_status", "更新客户状态", "write",
             params={"lead_id": "int", "status": "string"},
             required=("lead_id", "status"), handler=_lead_update_status,
             preview=_preview_lead_update_status),
    ToolSpec("material_upload", "上传材料并解析抽字段（不落研判记录）", "write",
             params={"text": "string", "filename": "string?", "lead_id": "int?"},
             required=("text",), handler=_material_upload, preview=_preview_material_upload),
    ToolSpec("report_daily_submit", "提交员工日报", "write",
             params={"employee_id": "int", "content": "string", "date": "string?"},
             required=("employee_id", "content"), handler=_report_daily_submit,
             preview=_preview_report_daily_submit),
    ToolSpec("leave_apply", "提交请假 / 考务申请", "write",
             params={"student_id": "int", "start_date": "string?", "days": "int?",
                     "reason": "string?", "request_type": "string?"},
             required=("student_id",), handler=_leave_apply, preview=_preview_leave_apply),
    ToolSpec("leave_approve", "审批请假 / 行政申请（不可撤销，强制二次确认）", "write",
             params={"request_id": "int", "action": "string", "remark": "string?"},
             required=("request_id", "action"), min_role="employee",
             handler=_leave_approve, preview=_preview_leave_approve),
    ToolSpec("ticket_create", "创建售后反馈工单", "write",
             params={"content": "string", "category": "string?", "student_id": "int?"},
             required=("content",), handler=_ticket_create, preview=_preview_ticket_create),
    ToolSpec("ticket_update", "更新工单状态（解决/关闭时自动通知学生）", "write",
             params={"ticket_id": "int", "status": "string", "handler_id": "int?"},
             required=("ticket_id", "status"), handler=_ticket_update,
             preview=_preview_ticket_update),
    ToolSpec("activity_enroll", "活动报名（占用名额，需确认）", "write",
             params={"activity_id": "int", "student_id": "int?", "lead_id": "int?"},
             required=("activity_id",), handler=_activity_enroll,
             preview=_preview_activity_enroll),
    ToolSpec("todo_push", "推送待办与心理预警触达", "write",
             params={"all_staff": "bool?", "subject": "string?", "categories": "array?"},
             min_role="manager", handler=_todo_push, preview=_preview_todo_push),
    ToolSpec("report_generate", "生成五类业务报告（可导出）", "write",
             params={"report_type": "string", "period_start": "string?",
                     "period_end": "string?"}, required=("report_type",),
             min_role="manager", handler=_report_generate,
             preview=_preview_report_generate),
)

TOOL_REGISTRY: Dict[str, ToolSpec] = {spec.name: spec for spec in SPECS}

# TDD 4.5 明确列出的 19 个工具（不含保留下来的 4 个早期只读接口）
TDD_TOOL_NAMES: Tuple[str, ...] = (
    "screening_analyze", "lead_create", "lead_query", "lead_update_status",
    "nl2sql_query", "report_daily_submit", "leave_apply", "leave_approve",
    "ticket_create", "ticket_update", "activity_enroll", "progress_query",
    "material_upload", "screening_batch", "screening_review", "org_query",
    "onboarding_query", "todo_push", "report_generate",
)


def _validate_registry() -> None:
    """注册期就把「写工具给不出人话回显」这种错误挡掉。"""
    for spec in SPECS:
        if spec.handler is None:
            raise RuntimeError(f"工具 {spec.name} 没挂 handler")
        if spec.is_write and spec.preview is None:
            raise RuntimeError(f"写工具 {spec.name} 必须提供 preview（回显确认文案）")
        if spec.min_role not in ROLE_RANK:
            raise RuntimeError(f"工具 {spec.name} 的 min_role={spec.min_role} 不合法")
        for name in spec.required:
            if name not in spec.params:
                raise RuntimeError(f"工具 {spec.name} 的必填参数 {name} 没在 params 里声明")


_validate_registry()


def catalog(*, include_legacy: bool = True) -> List[Dict[str, Any]]:
    specs = SPECS if include_legacy else tuple(s for s in SPECS if not s.legacy_of)
    return [spec.schema() for spec in specs]


def tdd_coverage() -> Dict[str, Any]:
    """TDD 4.5 的 19 个工具是否都实现（查验报告里的 P0-2 缺口）。"""
    missing = [n for n in TDD_TOOL_NAMES if n not in TOOL_REGISTRY]
    return {"declared": len(TDD_TOOL_NAMES), "implemented": len(TDD_TOOL_NAMES) - len(missing),
            "missing": missing, "total_registered": len(TOOL_REGISTRY)}


# --------------------------------------------------------------------------- #
# 参数校验 / 权限
# --------------------------------------------------------------------------- #
_BOOLS = {"true": True, "false": False, "1": True, "0": False}


def _coerce(spec: ToolSpec, params: Dict[str, Any]) -> Dict[str, Any]:
    """按声明把字符串化的入参收敛回类型（工具调用方常把一切都传成字符串）。"""
    out = dict(params)
    for name in spec.params:
        value = out.get(name)
        if value is None or value == "":
            continue
        declared = spec.params[name].rstrip("?")
        try:
            if declared == "int":
                out[name] = int(value)
            elif declared == "bool" and isinstance(value, str):
                out[name] = _BOOLS.get(value.strip().lower(), value)
            elif declared == "array" and isinstance(value, str):
                import json as _json
                out[name] = _json.loads(value)
        except (TypeError, ValueError):
            raise AppError(f"参数 {name} 应为 {declared}，收到 {value!r}") from None
    return out


def check_role(spec: ToolSpec, ctx: ToolContext) -> None:
    if ctx.rank < ROLE_RANK.get(spec.min_role, 0):
        raise Forbidden(f"工具 {spec.name} 需要 {spec.min_role} 及以上角色，"
                        f"当前 {ctx.actor_role}")


def missing_required(spec: ToolSpec, params: Dict[str, Any]) -> List[str]:
    return [name for name in spec.required if params.get(name) in (None, "", [], {})]


# --------------------------------------------------------------------------- #
# 二次确认
# --------------------------------------------------------------------------- #
def create_pending(db: Session, spec: ToolSpec, params: Dict[str, Any],
                   ctx: ToolContext) -> PendingAction:
    preview = spec.preview(db, params) if spec.preview else spec.desc
    row = PendingAction(
        token=uuid.uuid4().hex,
        actor_subject=ctx.actor_subject, actor_role=ctx.actor_role,
        tool_name=spec.name, params=params, preview=preview[:512], status="PENDING",
        expires_at=datetime.now() + timedelta(seconds=settings.pending_action_ttl_seconds))
    db.add(row)
    db.flush()
    return row


def pending_of(db: Session, token: str) -> Optional[PendingAction]:
    return db.query(PendingAction).filter(PendingAction.token == token).first()


def effective_status(row: PendingAction, *, now: Optional[datetime] = None) -> str:
    """待确认记录**当前**的状态。

    「超时」是一个由时间推导出来的事实，不是一次写操作 ——
    查一下就把它改成 EXPIRED 会让「谁在什么时候改的」变得含糊，
    而且读接口还得有写权限。所以状态在读取时算出来，落库的只有
    用户真的做了动作（CONFIRMED / CANCELLED）。
    """
    if row.status == "PENDING" and row.expires_at < (now or datetime.now()):
        return "EXPIRED"
    return row.status


def execute(db: Session, spec: ToolSpec, params: Dict[str, Any],
            ctx: ToolContext) -> Dict[str, Any]:
    """真正执行（read 直接用；write 只在确认后走到这里）。"""
    check_role(spec, ctx)
    return spec.handler(db, params, ctx)


def confirm(db: Session, token: str, ctx: ToolContext,
            override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """用 token 确认并执行一条待确认的写操作。

    过期 / 已确认 / 已取消 / 工具下架都会给出明确原因，不静默失败。
    """
    row = pending_of(db, token)
    if row is None:
        raise AppError(f"确认 token {token} 不存在")
    status = effective_status(row)
    if status == "EXPIRED":
        raise AppError("确认已超时，请重新发起操作")
    if status != "PENDING":
        raise AppError(f"该操作已是 {status}，不能重复确认")

    spec = TOOL_REGISTRY.get(row.tool_name)
    if spec is None:
        raise AppError(f"工具 {row.tool_name} 已下线")
    # 确认人必须是同一个操作人 —— 否则 A 发起、B 误点确认也能过
    if ctx.actor_subject != row.actor_subject:
        raise Forbidden("只能确认自己发起的操作")

    params = {**(row.params or {}), **(override or {})}
    result = execute(db, spec, params, ctx)
    row.status = "CONFIRMED"
    row.confirmed_at = datetime.now()
    row.result = result
    db.flush()
    return {"token": token, "tool": spec.name, "preview": row.preview, "result": result}


def cancel(db: Session, token: str, ctx: ToolContext) -> Dict[str, Any]:
    row = pending_of(db, token)
    if row is None:
        raise AppError(f"确认 token {token} 不存在")
    status = effective_status(row)
    if status != "PENDING":
        raise AppError(f"该操作已是 {status}，不能取消")
    if ctx.actor_subject != row.actor_subject:
        raise Forbidden("只能取消自己发起的操作")
    row.status = "CANCELLED"
    db.flush()
    return {"token": token, "status": row.status}


def invoke(db: Session, name: str, params: Dict[str, Any], ctx: ToolContext,
           confirm_token: Optional[str] = None,
           dry_run: bool = False,
           cancel_token: Optional[str] = None) -> Dict[str, Any]:
    """工具总入口：read 直接执行；write 无 token 则只回显，带 token 则执行。

    返回里一定有 `kind` 与 `executed`，调用方（Dify / 前端）据此决定
    是「把数据讲给人听」还是「把确认文案展示给人看」。

    `cancel_token` 优先于 `confirm_token`：带它表示撤回一条待确认写操作，
    只置为 CANCELLED、不执行（发起人已发出但反悔的 token 不再悬挂到过期）。
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise NotFound(f"未知工具 {name}，可用：{sorted(TOOL_REGISTRY)}")
    check_role(spec, ctx)

    if cancel_token:
        outcome = cancel(db, cancel_token, ctx)
        return {"kind": "write", "executed": False, "needs_confirm": False,
                "cancelled": True, "tool": name, **outcome}

    params = _coerce(spec, params)
    if confirm_token:
        outcome = confirm(db, confirm_token, ctx, override=params)
        return {"kind": "write", "executed": True, "needs_confirm": False,
                "tool": name, **outcome}

    miss = missing_required(spec, params)
    if miss:
        raise AppError(f"工具 {name} 缺少必填参数：{', '.join(miss)}")

    if not spec.is_write:
        return {"kind": "read", "executed": True, "needs_confirm": False,
                "tool": name, "data": spec.handler(db, params, ctx)}

    preview = spec.preview(db, params) if spec.preview else spec.desc
    if dry_run:
        return {"kind": "write", "executed": False, "needs_confirm": True,
                "tool": name, "preview": preview, "token": None,
                "note": "dry_run：只回显，未落库也未执行"}

    row = create_pending(db, spec, params, ctx)
    return {"kind": "write", "executed": False, "needs_confirm": True,
            "tool": name, "preview": preview, "token": row.token,
            "expires_at": row.expires_at.isoformat(timespec="seconds"),
            "confirm_hint": f"再次调用同一工具并带 confirm={row.token} 即执行"}
