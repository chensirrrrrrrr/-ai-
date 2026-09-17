"""主动待办推送与心理预警触达。

对应两条客户需求：

* 企业智能助手 —— 「主动待办推送：配置定时任务或触发器，当系统检测到有待处理事项时，
  主动询问员工『有没有投诉反馈需要跟进？』『有没有请假需要审批？』，并直接反馈查询结果。」
* 学生智能助手·心理关怀 —— 「一旦识别出高危风险，立即触发预警并记录原因，
  辅助老师通过企业助手快速介入干预。」

三条设计原则
------------
1. **待办是「算」出来的，不是「存」出来的**。`collect()` 每次现场查业务表，
   不维护一张「待办表」—— 否则审批完了待办还在，状态不同步是迟早的事。
2. **推送记录才是「存」的**。`todo_push` 存「推过什么、推给谁、处理没有」，
   同时靠 `dedupe_key` 唯一约束承担**幂等 / 频控**，避免同一提醒反复轰炸。
3. **调度器只管「什么时候跑」**。业务逻辑全在 `push_due()` / `run_once()` 里，
   于是同一套逻辑既能被定时触发，也能被 `POST /todo/push` 手动触发、
   被 pytest 直接调用 —— 可测性不依赖「后台线程跑起来了没有」。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from ..config import settings
from . import notify
from ..models import (AfterSalesTicket, CustomerFollowup, CustomerLead, Employee,
                      LeadScreening, MentalAlert, Student, StudentRequest, TodoPush)

logger = logging.getLogger(__name__)

STAFF_ROLES = ("employee", "manager", "admin")
MANAGER_ROLES = ("manager", "admin")

# 待办分类（顺序即展示顺序，也是「最该先看哪个」的排序参考）
CATEGORIES = ("approval", "ticket", "followup", "screening_review", "mental_alert")

# 心理预警是敏感数据，只给管理层 —— 与 `/alerts` 接口的口径保持一致，
# 否则「对话里能问出来」就成了权限旁路。
MANAGER_ONLY_CATEGORIES = {"mental_alert"}

CATEGORY_META: Dict[str, Dict[str, str]] = {
    "approval": {
        "title": "待审批申请",
        "question": "有没有请假 / 考务申请需要审批？",
        "action": "去「学员中心」逐条审批",
    },
    "ticket": {
        "title": "待跟进工单",
        "question": "有没有投诉反馈需要跟进？",
        "action": "去「学员中心」更新工单状态",
    },
    "followup": {
        "title": "到期客户跟进",
        "question": "有没有客户到了约定的跟进时间？",
        "action": "去「客户管理」补跟进记录",
    },
    "screening_review": {
        "title": "待人工复核的研判",
        "question": "有没有研判结论等着人工复核？",
        "action": "去「客户管理」确认或推翻",
    },
    "mental_alert": {
        "title": "待触达心理预警",
        "question": "有没有心理预警还没联系到带教老师？",
        "action": "去「运营中心」触达预警",
    },
}

_SEVERITY_RANK = {"high": 0, "normal": 1}


# --------------------------------------------------------------------------- #
# 一、算待办
# --------------------------------------------------------------------------- #
def _student_names(db: Session, student_ids: Iterable[int]) -> Dict[int, str]:
    ids = {sid for sid in student_ids if sid}
    if not ids:
        return {}
    rows = db.query(Student.id, Student.name).filter(Student.id.in_(ids)).all()
    return {row[0]: row[1] for row in rows}


def _overdue_hours(moment: Optional[datetime], now: datetime) -> float:
    if moment is None:
        return 0.0
    return round((now - moment).total_seconds() / 3600, 1)


def _todo(category: str, items: List[Dict[str, Any]], overdue: float) -> Dict[str, Any]:
    meta = CATEGORY_META[category]
    return {
        "category": category,
        "title": meta["title"],
        "question": meta["question"],
        "action": meta["action"],
        "count": len(items),
        "severity": "high" if overdue >= settings.alert_overdue_hours else "normal",
        "oldest_hours": overdue,
        "items": items,
    }


def _pending_approvals(db: Session, now: datetime) -> Dict[str, Any]:
    rows = (db.query(StudentRequest)
            .filter(StudentRequest.status == "PENDING")
            .order_by(StudentRequest.created_at.asc()).all())
    names = _student_names(db, (r.student_id for r in rows))
    items = [{
        "id": r.id,
        "label": f"{names.get(r.student_id, f'学生{r.student_id}')} · "
                 f"{'请假' if r.type == 'LEAVE' else '考务' if r.type == 'EXAM' else r.type}",
        "detail": _describe_request(r),
        "oldest_hours": _overdue_hours(r.created_at, now),
    } for r in rows]
    overdue = max((i["oldest_hours"] for i in items), default=0.0)
    return _todo("approval", items, overdue)


def _describe_request(row: StudentRequest) -> str:
    content = row.content or {}
    if row.type == "LEAVE":
        return (f"{content.get('start_date', '未填日期')} 起 "
                f"{content.get('days', '?')} 天，原因：{content.get('reason', '未填')}")
    return str(content.get("exam") or content.get("date") or "详见申请内容")


def _open_tickets(db: Session, now: datetime) -> Dict[str, Any]:
    rows = (db.query(AfterSalesTicket)
            .filter(AfterSalesTicket.status.in_(("OPEN", "PROCESSING")))
            .order_by(AfterSalesTicket.created_at.asc()).all())
    names = _student_names(db, (r.student_id for r in rows))
    items = [{
        "id": r.id,
        "label": f"#{r.id} {names.get(r.student_id, '未关联学生')} · {r.status}",
        "detail": (r.summary or r.content or "")[:60],
        "oldest_hours": _overdue_hours(r.created_at, now),
    } for r in rows]
    overdue = max((i["oldest_hours"] for i in items), default=0.0)
    return _todo("ticket", items, overdue)


def _due_followups(db: Session, now: datetime) -> Dict[str, Any]:
    """到了约定跟进时间、且客户还没结束（已签约/已流失的不再催）。"""
    rows = (db.query(CustomerFollowup, CustomerLead)
            .join(CustomerLead, CustomerLead.id == CustomerFollowup.lead_id)
            .filter(CustomerFollowup.next_follow_at.isnot(None),
                    CustomerFollowup.next_follow_at <= now,
                    CustomerLead.status.notin_(("SIGNED", "LOST")))
            .order_by(CustomerFollowup.next_follow_at.asc()).all())

    latest: Dict[int, Any] = {}          # 同一客户只留最近一条约定
    for followup, lead in rows:
        latest[lead.id] = (followup, lead)

    items = [{
        "id": lead.id,
        "label": f"{lead.name}（{lead.intention_country or '意向未填'}）",
        "detail": f"计划：{followup.next_plan or '未填'} · "
                  f"约定 {followup.next_follow_at:%Y-%m-%d %H:%M}",
        "oldest_hours": _overdue_hours(followup.next_follow_at, now),
    } for followup, lead in latest.values()]
    items.sort(key=lambda i: i["oldest_hours"], reverse=True)
    overdue = max((i["oldest_hours"] for i in items), default=0.0)
    return _todo("followup", items, overdue)


def _pending_screenings(db: Session, now: datetime) -> Dict[str, Any]:
    rows = (db.query(LeadScreening)
            .filter(LeadScreening.review_status == "PENDING")
            .order_by(LeadScreening.created_at.asc()).all())
    items = [{
        "id": r.id,
        "label": f"研判 #{r.id} · {r.conclusion}",
        "detail": f"{r.source_type}"
                  + (f" · {r.source_name}" if r.source_name else "")
                  + f" · 缺 {len(r.missing_fields or [])} 个关键字段",
        "oldest_hours": _overdue_hours(r.created_at, now),
    } for r in rows]
    overdue = max((i["oldest_hours"] for i in items), default=0.0)
    return _todo("screening_review", items, overdue)


def _undelivered_alerts(db: Session, now: datetime) -> Dict[str, Any]:
    rows = (db.query(MentalAlert)
            .filter(MentalAlert.notified_at.is_(None),
                    MentalAlert.status.in_(("OPEN", "FOLLOWING")))
            .order_by(MentalAlert.created_at.asc()).all())
    names = _student_names(db, (r.student_id for r in rows))
    items = [{
        "id": r.id,
        "label": f"{names.get(r.student_id, f'学生{r.student_id}')} · {r.risk_level}",
        "detail": r.reason[:60],
        "oldest_hours": _overdue_hours(r.created_at, now),
    } for r in rows]
    # 心理预警天然是高优先级：直接按最高档算，不跟其他待办排队
    overdue = max([settings.alert_overdue_hours] +
                  [i["oldest_hours"] for i in items], default=settings.alert_overdue_hours)
    return _todo("mental_alert", items, overdue)


def collect(db: Session, role: str, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """按角色算出「当前该被提醒的待办」，空清单的类别不返回。

    待办是**现场算**的：审批完/跟进完/复核完，下一次调用它自然就消失了。
    """
    moment = now or datetime.now()
    todos: List[Dict[str, Any]] = []

    if role in STAFF_ROLES:
        todos += [_pending_approvals(db, moment), _open_tickets(db, moment),
                  _due_followups(db, moment), _pending_screenings(db, moment)]
    if role in MANAGER_ROLES:
        todos.append(_undelivered_alerts(db, moment))

    visible = [t for t in todos if t["count"] > 0]
    visible.sort(key=lambda t: (_SEVERITY_RANK.get(t["severity"], 9),
                                CATEGORIES.index(t["category"])))
    return visible


def digest_text(todos: Sequence[Dict[str, Any]]) -> str:
    """把待办渲染成「先问、再答」的对话话术（需求原文要的就是这个句式）。"""
    if not todos:
        return ("我看了一圈，现在没有需要你处理的事项：申请都审完了、工单都跟进了、"
                "研判也都复核过了。有新情况我会主动提醒你。")
    total = sum(t["count"] for t in todos)
    lines = [f"我扫了一遍后台，现在有 **{total}** 件事等着你处理：", ""]
    for todo in todos:
        flag = "🔴 " if todo["severity"] == "high" else "· "
        lines.append(f"{flag}{todo['question']}（{todo['count']} 条）")
        for item in todo["items"][:3]:
            lines.append(f"    - {item['label']}：{item['detail']}")
        if todo["count"] > 3:
            lines.append(f"    …… 还有 {todo['count'] - 3} 条")
        lines.append(f"    处理入口：{todo['action']}")
    lines += ["", "需要我打开哪一类的明细，直接说（例如「待审批申请」）。"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 二、推送（幂等 + 频控 + 留痕）
# --------------------------------------------------------------------------- #
def _window_start(now: datetime) -> datetime:
    minutes = max(1, settings.todo_push_window_minutes)
    return now.replace(second=0, microsecond=0) - timedelta(
        minutes=now.minute % minutes)


def dedupe_key(subject: str, category: str, now: datetime) -> str:
    """同一员工 + 同一待办类别 + 同一时间窗 = 同一条推送。"""
    return f"{category}:{subject}:{_window_start(now):%Y%m%d%H%M}"


def push_due(db: Session, *, subject: str, role: str,
             ref_id: Optional[int] = None,
             categories: Optional[Sequence[str]] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """把当前待办推给一个人。不 commit，由调用方决定事务边界。

    频控命中（同一时间窗内已推过）时返回 `duplicated=True` 且不新增记录 ——
    这是「别反复轰炸员工」的实现，也是定时任务 + 手动触发并存时不会重复写的原因。
    """
    moment = now or datetime.now()
    wanted = set(categories) if categories else None
    todos = [t for t in collect(db, role, now=moment)
             if wanted is None or t["category"] in wanted]

    records: List[Dict[str, Any]] = []
    for todo in todos:
        key = dedupe_key(subject, todo["category"], moment)
        existing = db.query(TodoPush).filter(TodoPush.dedupe_key == key).first()
        if existing is not None:
            records.append({"id": existing.id, "category": todo["category"],
                            "count": existing.item_count, "duplicated": True})
            continue

        row = TodoPush(
            employee_id=ref_id, subject=subject, role=role,
            category=todo["category"], title=todo["title"],
            question=todo["question"], severity=todo["severity"],
            item_count=todo["count"],
            summary={"items": todo["items"][:5], "oldest_hours": todo["oldest_hours"]},
            channel="chat", dedupe_key=key, delivered_at=moment,
        )
        db.add(row)
        db.flush()
        records.append({"id": row.id, "category": todo["category"],
                        "count": todo["count"], "duplicated": False,
                        "question": todo["question"], "severity": todo["severity"]})

    # 心理预警属于「立即触达」：推送这一类的同一时刻就把预警标记为已触达，
    # 否则「有没有预警还没联系老师」会永远挂在待办里。
    delivered_alerts: List[Dict[str, Any]] = []
    if wanted is None or "mental_alert" in wanted:
        if role in MANAGER_ROLES and any(t["category"] == "mental_alert" for t in todos):
            delivered_alerts = deliver_alerts(
                db, actor_subject=subject, actor_role=role,
                actor_ref_id=ref_id, now=moment)

    return {"subject": subject, "role": role, "records": records,
            "delivered_alerts": delivered_alerts,
            "pushed": sum(1 for r in records if not r["duplicated"])}


def run_once(*, only_subject: Optional[str] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """跑一轮：给全部在职员工算待办并推送。定时任务与手动触发共用的入口。"""
    from ..db import SessionLocal
    from ..models import SysAccount

    moment = now or datetime.now()
    db = SessionLocal()
    try:
        query = db.query(SysAccount).filter(SysAccount.is_active.is_(True),
                                            SysAccount.role.in_(STAFF_ROLES))
        if only_subject:
            query = query.filter(SysAccount.username == only_subject)
        accounts = query.all()

        details = []
        delivered = 0
        for account in accounts:
            result = push_due(db, subject=account.username, role=account.role,
                              ref_id=account.ref_id, now=moment)
            delivered += len(result["delivered_alerts"])
            details.append({"subject": account.username, "role": account.role,
                            "pushed": result["pushed"],
                            "categories": [r["category"] for r in result["records"]
                                           if not r["duplicated"]]})
        db.commit()
        return {"ran_at": moment.isoformat(), "staff_count": len(accounts),
                "pushed": sum(d["pushed"] for d in details),
                "alerts_delivered": delivered, "details": details}
    except Exception:
        db.rollback()
        logger.exception("主动待办推送失败")
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 三、心理预警触达
# --------------------------------------------------------------------------- #
def intervention_advice(alert: MentalAlert, student_name: str) -> List[str]:
    """生成可执行的介入步骤（留痕用，避免「通知了但没说该做什么」）。"""
    return [
        f"24 小时内单独联系 {student_name}（线上亦可），先听不评判，不在群里提及此事",
        f"围绕触发点展开：{alert.reason[:60]}",
        "观察是否出现自伤/极端言语；若有，立即升级到心理老师与家长，并同步管理层",
        "3 个工作日内回填一次跟进记录，风险等级未下降则维持 FOLLOWING",
        "紧急情况可拨打 12356 心理援助热线，或联系校方心理中心",
    ]


def resolve_assignee(db: Session, alert: MentalAlert) -> tuple:
    """决定这次触达给谁：学生的带教顾问 → 兜底第一个管理层。

    返回 `(employee, note)`；找不到任何人时 employee 为 None 并给出说明 ——
    宁可在留痕里写「没找到处理人」，也不要瞎编一个联系人。
    """
    student = db.get(Student, alert.student_id)
    advisor = db.get(Employee, student.advisor_id) if (
        student is not None and student.advisor_id) else None
    if advisor is not None:
        return advisor, f"学生带教顾问：{advisor.name}"

    fallback = (db.query(Employee).filter(Employee.biz_role == "manager")
                .order_by(Employee.id.asc()).first())
    if fallback is not None:
        reason = ("学生的 advisor_id 指向的员工不存在"
                  if student is not None and student.advisor_id
                  else "学生未配置带教顾问")
        return fallback, f"{reason}，兜底给管理层：{fallback.name}"
    return None, "没有可用处理人（既没配带教顾问，也没有管理层账号）"


def deliver_alerts(db: Session, *, actor_subject: str, actor_role: str,
                   actor_ref_id: Optional[int] = None,
                   alert_ids: Optional[Sequence[int]] = None,
                   now: Optional[datetime] = None,
                   channel: str = "chat") -> List[Dict[str, Any]]:
    """触达心理预警：写 `notified_at` / `notified_to` / `intervention`，不 commit。

    幂等：已经触达过的（`notified_at` 非空）不会重复写入，除非显式点名 `alert_ids`。
    """
    moment = now or datetime.now()
    query = db.query(MentalAlert).filter(MentalAlert.status.in_(("OPEN", "FOLLOWING")))
    if alert_ids:
        rows = query.filter(MentalAlert.id.in_(list(alert_ids))).all()
    else:
        rows = query.filter(MentalAlert.notified_at.is_(None)).all()

    names = _student_names(db, (r.student_id for r in rows))
    delivered: List[Dict[str, Any]] = []
    for alert in rows:
        if alert.notified_at is not None and not alert_ids:
            continue
        student_name = names.get(alert.student_id, f"学生{alert.student_id}")
        assignee, note = resolve_assignee(db, alert)
        advice = intervention_advice(alert, student_name)

        alert.notified_at = moment
        alert.notified_to = assignee.id if assignee else None
        alert.notify_channel = channel
        alert.intervention = "\n".join(f"{i}. {line}" for i, line in enumerate(advice, 1))
        if assignee is not None and alert.handler_id is None:
            alert.handler_id = assignee.id

        # 统一通知表也落一条：`mental_alert.notified_at` 只回答「触达过没有」，
        # 回答不了「谁收到了、读没读、内容是什么」。没有这一条，
        # 处理人在「我的通知」里看不到自己被指派的预警 —— 触达等于没闭环。
        if assignee is not None and assignee.id is not None:
            notify.send(
                db, recipient_type="employee", recipient_id=assignee.id,
                category="alert",
                title=f"【心理预警】{student_name} · {alert.risk_level}",
                body="\n".join([
                    f"学生：{student_name}（ID {alert.student_id}）",
                    f"风险等级：{alert.risk_level}",
                    f"判定依据：{alert.reason}",
                    "干预建议：",
                    *[f"{i}. {line}" for i, line in enumerate(advice, 1)],
                ]),
                biz_type="mental_alert", biz_id=alert.id)

        delivered.append({
            "id": alert.id, "student_id": alert.student_id, "student_name": student_name,
            "risk_level": alert.risk_level, "reason": alert.reason,
            "notified_to": assignee.id if assignee else None,
            "notified_to_name": assignee.name if assignee else None,
            "assignee_note": note, "channel": channel,
            "notified_at": moment.isoformat(), "advice": advice,
        })

    if delivered:
        # 触达动作本身也要留痕（谁触达的、触达了几条）
        from . import audit
        audit.record(db, action="alert.notify", actor_id=actor_subject,
                     actor_role=actor_role, resource="mental_alert",
                     resource_id=",".join(str(d["id"]) for d in delivered),
                     detail={"count": len(delivered),
                             "to": [d["notified_to"] for d in delivered]})
    return delivered


def alert_digest(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """心理预警汇总（对话侧与管理端共用）。"""
    moment = now or datetime.now()
    rows = (db.query(MentalAlert)
            .filter(MentalAlert.status.in_(("OPEN", "FOLLOWING")))
            .order_by(MentalAlert.risk_level.asc(), MentalAlert.created_at.asc()).all())
    names = _student_names(db, (r.student_id for r in rows))
    items = [{
        "id": r.id, "student_id": r.student_id,
        "student_name": names.get(r.student_id, f"学生{r.student_id}"),
        "risk_level": r.risk_level, "reason": r.reason, "status": r.status,
        "notified": r.notified_at is not None,
        "notified_at": r.notified_at.isoformat() if r.notified_at else None,
        "handler_id": r.handler_id,
        "created_at": r.created_at.isoformat(),
    } for r in rows]
    return {
        "total": len(items),
        "high_risk": sum(1 for i in items if i["risk_level"] == "HIGH"),
        "undelivered": sum(1 for i in items if not i["notified"]),
        "items": items,
    }
