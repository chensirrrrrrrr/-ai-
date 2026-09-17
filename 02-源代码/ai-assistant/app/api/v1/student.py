"""学员域：学生档案 / 成绩 / 行政申请（请假·考务）/ 售后工单 / 心理预警。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core import Conflict, Forbidden, NotFound, ok
from ...db import get_db
from ...models import (AfterSalesTicket, MentalAlert, Student, StudentRequest,
                       StudentScore)
from ...schemas import (AlertNotifyRequest, ApproveBody, DeadlineImport,
                        DeadlineStatusBody, LeaveApply, ProgressImport,
                        ReminderRunBody, ScoreCreate, TicketCreate, TicketUpdate)
from ...services import audit, deadline, notify, todo
from ..deps import Principal, client_ip, get_principal, require_roles

router = APIRouter(tags=["student"])

staff_only = require_roles("employee", "manager", "admin")
manager_only = require_roles("manager", "admin")


def _ensure_student_access(principal: Principal, student_id: int) -> None:
    """学生只能看自己的数据；员工及以上不受限。"""
    if principal.role == "student" and principal.ref_id != student_id:
        raise Forbidden("学生只能访问自己的数据")


# 行政申请类型的中文名（通知文案与列表复用，避免各写一份）
REQUEST_TYPE_LABELS = {"LEAVE": "请假申请", "EXAM": "考务申请", "OTHER": "其他申请"}


def _request_type_label(raw: Optional[str]) -> str:
    return REQUEST_TYPE_LABELS.get((raw or "OTHER").upper(), "行政申请")


@router.get("/students", summary="学生列表")
def list_students(stage: Optional[str] = None,
                  keyword: Optional[str] = None,
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    query = db.query(Student)
    if stage:
        query = query.filter(Student.stage == stage)
    if keyword:
        query = query.filter(Student.name.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(Student.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"total": total, "page": page,
               "items": [{"id": s.id, "student_no": s.student_no, "name": s.name,
                          "stage": s.stage, "country_target": s.country_target,
                          "advisor_id": s.advisor_id} for s in rows]})


@router.get("/students/{student_id}", summary="学生详情")
def get_student(student_id: int, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict:
    _ensure_student_access(principal, student_id)
    student = db.get(Student, student_id)
    if student is None:
        raise NotFound(f"学生 {student_id} 不存在")
    return ok({
        "id": student.id, "student_no": student.student_no, "name": student.name,
        "country_target": student.country_target, "program_level": student.program_level,
        "stage": student.stage, "advisor_id": student.advisor_id,
        "enroll_date": student.enroll_date.isoformat() if student.enroll_date else None,
        # 敏感字段只返回脱敏值
        "id_card_masked": student.id_card_masked,
    })


@router.get("/students/{student_id}/progress", summary="查询申请进度")
def student_progress(student_id: int, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)) -> dict:
    _ensure_student_access(principal, student_id)
    student = db.get(Student, student_id)
    if student is None:
        raise NotFound(f"学生 {student_id} 不存在")

    order = ["PREPARING", "APPLYING", "OFFERED", "VISA", "ENROLLED"]
    current = order.index(student.stage) if student.stage in order else 0
    timeline = [
        {"stage": s, "done": i <= current, "current": i == current}
        for i, s in enumerate(order)
    ]
    scores = db.query(StudentScore).filter(StudentScore.student_id == student_id).count()
    pending = (db.query(StudentRequest)
               .filter(StudentRequest.student_id == student_id,
                       StudentRequest.status == "PENDING").count())
    return ok({"student_id": student_id, "stage": student.stage,
               "timeline": timeline, "score_records": scores, "pending_requests": pending})


# --------------------------------------------------------------------------- #
# REQ-M4-04 学业考务：节点导入 / 查询 / 考前提醒
#
# ⚠️ 路由顺序：`/deadlines/run-reminders` 与 `/students/{student_id}/deadlines`
# 段数不同，不会互相吃；但 `/deadlines/{deadline_id}` 必须排在
# `/deadlines/run-reminders` **之后**，否则「run-reminders」会被当成 id。
# --------------------------------------------------------------------------- #
@router.get("/students/{student_id}/deadlines", summary="学生考务节点（含紧急度分档）")
def list_student_deadlines(student_id: int,
                           kind: Optional[str] = None,
                           status: Optional[str] = None,
                           due_within_days: Optional[int] = Query(None, ge=1, le=365),
                           include_done: bool = True,
                           principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)) -> dict:
    """REQ-M4-04：论文 DDL / 考试时间等节点的查询。

    返回里带 `urgency`（overdue / today / soon / week / later）与 `days_left`，
    前端不用自己算倒计时 —— 阈值口径只在这里定义一份。
    """
    _ensure_student_access(principal, student_id)
    if db.get(Student, student_id) is None:
        raise NotFound(f"学生 {student_id} 不存在")
    rows = deadline.list_deadlines(db, student_id=student_id, kind=kind, status=status,
                                  due_within_days=due_within_days,
                                  include_done=include_done)
    now = datetime.now()
    return ok({
        "student_id": student_id,
        "total": len(rows),
        "summary": deadline.deadline_summary(db, student_id=student_id, now=now),
        "kinds": [{"key": k, "label": v} for k, v in deadline.DEADLINE_KINDS.items()],
        "items": [deadline.dump_deadline(r, now=now) for r in rows],
    })


@router.post("/students/{student_id}/deadlines", summary="导入考务节点（幂等，人工兜底/教务同步同一契约）")
def import_student_deadlines(student_id: int, body: DeadlineImport, request: Request,
                             principal: Principal = Depends(staff_only),
                             db: Session = Depends(get_db)) -> dict:
    """TDD 风险 2 的兜底方案：教务系统没接口时先人工导入。

    幂等键 `(source, external_id)`，没有 `external_id` 时回落自然键，
    所以同一份表导两遍不会翻倍；改期会重置提醒状态（见 services/deadline.py）。
    """
    if db.get(Student, student_id) is None:
        raise NotFound(f"学生 {student_id} 不存在")
    result = deadline.upsert_deadlines(
        db, student_id, [i.model_dump() for i in body.items], source=body.source)
    audit.record(db, action="deadline.import", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_deadline",
                 resource_id=student_id,
                 detail={"source": body.source, "created": result["created"],
                         "updated": result["updated"], "skipped": result["skipped"]},
                 ip=client_ip(request))
    db.commit()
    now = datetime.now()
    return ok({"created": result["created"], "updated": result["updated"],
               "skipped": result["skipped"],
               "items": [deadline.dump_deadline(r, now=now) for r in result["items"]],
               "summary": deadline.deadline_summary(db, student_id=student_id, now=now)})


@router.post("/deadlines/run-reminders", summary="跑一轮考前/截止前提醒（定时任务同一入口）")
def run_deadline_reminders(body: ReminderRunBody = ReminderRunBody(), request: Request = None,
                           principal: Principal = Depends(staff_only),
                           db: Session = Depends(get_db)) -> dict:
    """到提前量就通知**学生本人 + 其顾问**；`reminded_at` 保证只提醒一次。

    `dry_run=true` 只列影响面，不发通知也不写 `reminded_at`。
    """
    result = deadline.run_reminders(db, channel=body.channel, dry_run=body.dry_run)
    if not body.dry_run:
        audit.record(db, action="deadline.run_reminders", actor_id=principal.subject,
                     actor_role=principal.role, resource="student_deadline",
                     detail={"scanned": result["scanned"], "sent": result["sent"]},
                     ip=client_ip(request))
        db.commit()
    return ok(result)


@router.get("/deadlines/run-reminders/preview", summary="预演一轮提醒（只看会提醒到谁）")
def preview_deadline_reminders(principal: Principal = Depends(staff_only),
                               db: Session = Depends(get_db)) -> dict:
    return ok(deadline.run_reminders(db, dry_run=True))


@router.patch("/deadlines/{deadline_id}", summary="更新考务节点状态（完成 / 取消 / 重开）")
def update_deadline(deadline_id: int, body: DeadlineStatusBody, request: Request,
                    principal: Principal = Depends(staff_only),
                    db: Session = Depends(get_db)) -> dict:
    row = deadline.mark_deadline(db, deadline_id, body.status)
    if row is None:
        raise NotFound(f"考务节点 {deadline_id} 不存在，或状态值不合法")
    audit.record(db, action="deadline.update", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_deadline",
                 resource_id=row.id, detail={"status": row.status},
                 ip=client_ip(request))
    db.commit()
    return ok(deadline.dump_deadline(row))


# --------------------------------------------------------------------------- #
# REQ-M4-05 业务进度明细（文书审核 / 院校申请 / 签证办理）
# --------------------------------------------------------------------------- #
@router.get("/students/{student_id}/progress-board", summary="业务进度看板（按阶段分组 + 完成度）")
def student_progress_board(student_id: int,
                           principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db)) -> dict:
    """细项级进度 —— `student.stage` 只有一个粗粒度阶段，回答不了「文书到哪一步」。

    与 `/students/{student_id}/progress`（粗粒度时间线）并存，两者不冲突：
    那个答「整体到哪个阶段」，这个答「每个细项谁在跟、卡在哪」。
    """
    _ensure_student_access(principal, student_id)
    if db.get(Student, student_id) is None:
        raise NotFound(f"学生 {student_id} 不存在")
    board = deadline.progress_board(db, student_id=student_id)
    board["phases_catalog"] = [{"key": k, "label": v}
                               for k, v in deadline.PROGRESS_PHASES.items()]
    return ok(board)


@router.post("/students/{student_id}/progress", summary="导入业务进度明细（幂等）")
def import_student_progress(student_id: int, body: ProgressImport, request: Request,
                            principal: Principal = Depends(staff_only),
                            db: Session = Depends(get_db)) -> dict:
    if db.get(Student, student_id) is None:
        raise NotFound(f"学生 {student_id} 不存在")
    result = deadline.upsert_progress(
        db, student_id, [i.model_dump() for i in body.items], source=body.source)
    audit.record(db, action="progress.import", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_progress",
                 resource_id=student_id,
                 detail={"source": body.source, "created": result["created"],
                         "updated": result["updated"], "skipped": result["skipped"]},
                 ip=client_ip(request))
    db.commit()
    return ok({"created": result["created"], "updated": result["updated"],
               "skipped": result["skipped"],
               "items": [deadline.dump_progress(r) for r in result["items"]],
               "board": deadline.progress_board(db, student_id=student_id)})


@router.get("/students/{student_id}/deadline-notifications", summary="学生的考务提醒记录")
def student_deadline_notifications(student_id: int,
                                   principal: Principal = Depends(get_principal),
                                   db: Session = Depends(get_db)) -> dict:
    _ensure_student_access(principal, student_id)
    rows = deadline.notifications_of(db, student_id)
    from ...services import notify as notify_svc

    return ok({"student_id": student_id, "total": len(rows),
               "items": [notify_svc.dump(r) for r in rows]})


@router.post("/scores", summary="录入成绩")
def create_score(body: ScoreCreate, request: Request,
                 principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    if db.get(Student, body.student_id) is None:
        raise NotFound(f"学生 {body.student_id} 不存在")
    row = StudentScore(**body.model_dump())
    db.add(row)
    db.flush()
    audit.record(db, action="score.create", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_score", resource_id=row.id,
                 detail={"student_id": body.student_id, "subject": body.subject},
                 ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "student_id": row.student_id, "subject": row.subject,
               "score": float(row.score)})


@router.get("/scores", summary="查询成绩")
def list_scores(student_id: Optional[int] = None, subject: Optional[str] = None,
                principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict:
    if principal.role == "student":
        student_id = principal.ref_id
    query = db.query(StudentScore)
    if student_id:
        query = query.filter(StudentScore.student_id == student_id)
    if subject:
        query = query.filter(StudentScore.subject == subject)
    rows = query.order_by(StudentScore.exam_date.desc()).limit(100).all()
    return ok({"total": len(rows),
               "items": [{"id": r.id, "student_id": r.student_id, "exam_name": r.exam_name,
                          "subject": r.subject, "score": float(r.score),
                          "full_score": float(r.full_score),
                          "exam_date": r.exam_date.isoformat() if r.exam_date else None}
                         for r in rows]})


@router.post("/leave/apply", summary="提交行政申请（请假/考务），支持幂等键")
def apply_leave(body: LeaveApply, request: Request,
                principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict:
    _ensure_student_access(principal, body.student_id)
    if db.get(Student, body.student_id) is None:
        raise NotFound(f"学生 {body.student_id} 不存在")

    if body.idempotency_key:
        dup = (db.query(StudentRequest)
               .filter(StudentRequest.idempotency_key == body.idempotency_key).first())
        if dup:
            return ok({"id": dup.id, "status": dup.status, "duplicated": True})

    row = StudentRequest(
        student_id=body.student_id, type=body.request_type, status="PENDING",
        content={"start_date": body.start_date.isoformat() if body.start_date else None,
                 "days": body.days, "reason": body.reason},
        idempotency_key=body.idempotency_key,
    )
    db.add(row)
    db.flush()
    audit.record(db, action="request.apply", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_request", resource_id=row.id,
                 detail={"type": body.request_type}, ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "status": row.status, "duplicated": False,
               "content": row.content})


@router.get("/requests", summary="行政申请列表")
def list_requests(status: Optional[str] = None, student_id: Optional[int] = None,
                  principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict:
    query = db.query(StudentRequest)
    if principal.role == "student":
        student_id = principal.ref_id
    elif principal.role == "visitor":
        raise Forbidden("访客无权查看行政申请")
    if status:
        query = query.filter(StudentRequest.status == status)
    if student_id:
        query = query.filter(StudentRequest.student_id == student_id)
    rows = query.order_by(StudentRequest.id.desc()).limit(100).all()
    return ok({"total": len(rows),
               "items": [{"id": r.id, "student_id": r.student_id, "type": r.type,
                          "status": r.status, "content": r.content,
                          "approver_id": r.approver_id,
                          "created_at": r.created_at.isoformat()} for r in rows]})


@router.post("/leave/{request_id}/approve", summary="审批行政申请（幂等：重复审批返回冲突）")
def approve_leave(request_id: int, body: ApproveBody, request: Request,
                  principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    row = db.get(StudentRequest, request_id)
    if row is None:
        raise NotFound(f"申请单 {request_id} 不存在")
    if row.status != "PENDING":
        raise Conflict(f"申请单当前状态为 {row.status}，不可重复审批")

    row.status = "APPROVED" if body.approve else "REJECTED"
    row.approver_id = principal.ref_id
    row.approved_at = datetime.now()
    row.remark = body.remark
    audit.record(db, action="request.approve", actor_id=principal.subject,
                 actor_role=principal.role, resource="student_request", resource_id=request_id,
                 detail={"approve": body.approve, "remark": body.remark},
                 ip=client_ip(request))

    # 闭环通知：审批结果必须回到申请人手上。
    # 过去这一步只写库不告知，学生得反复刷新页面才知道结果 ——
    # 这正是查验报告里「行政申请闭环缺失」的那一条。
    verdict = "已通过" if body.approve else "已驳回"
    notice = notify.send(
        db, recipient_type="student", recipient_id=row.student_id,
        category="leave", title=f"【行政申请{verdict}】{_request_type_label(row.type)}",
        body="\n".join(p for p in [
            f"申请编号：{row.id}",
            f"结果：{verdict}",
            f"审批人：{principal.subject}",
            f"审批意见：{body.remark}" if body.remark else "",
        ] if p),
        biz_type="student_request", biz_id=row.id)
    db.commit()
    return ok({"id": request_id, "status": row.status,
               "approved_at": row.approved_at.isoformat(),
               "notify": {"channel": notice.channel, "delivered_via": notice.delivered_via}})


@router.post("/tickets", summary="创建售后反馈工单")
def create_ticket(body: TicketCreate, request: Request,
                  principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict:
    if principal.role == "student":
        body.student_id = principal.ref_id
    if body.student_id and db.get(Student, body.student_id) is None:
        raise NotFound(f"学生 {body.student_id} 不存在")

    summary = body.content[:60] + ("..." if len(body.content) > 60 else "")
    row = AfterSalesTicket(student_id=body.student_id, content=body.content,
                           summary=summary, category=body.category, status="OPEN")
    db.add(row)
    db.flush()
    audit.record(db, action="ticket.create", actor_id=principal.subject,
                 actor_role=principal.role, resource="after_sales_ticket", resource_id=row.id,
                 detail={"category": body.category}, ip=client_ip(request))
    db.commit()
    return ok({"id": row.id, "status": row.status, "summary": row.summary})


@router.get("/tickets", summary="工单列表")
def list_tickets(status: Optional[str] = None, category: Optional[str] = None,
                 principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    query = db.query(AfterSalesTicket)
    if status:
        query = query.filter(AfterSalesTicket.status == status)
    if category:
        query = query.filter(AfterSalesTicket.category == category)
    rows = query.order_by(AfterSalesTicket.id.desc()).limit(100).all()
    return ok({"total": len(rows),
               "items": [{"id": r.id, "student_id": r.student_id, "summary": r.summary,
                          "category": r.category, "status": r.status,
                          "handler_id": r.handler_id,
                          "created_at": r.created_at.isoformat()} for r in rows]})


@router.patch("/tickets/{ticket_id}", summary="更新工单状态/处理人")
def update_ticket(ticket_id: int, body: TicketUpdate, request: Request,
                  principal: Principal = Depends(staff_only),
                  db: Session = Depends(get_db)) -> dict:
    row = db.get(AfterSalesTicket, ticket_id)
    if row is None:
        raise NotFound(f"工单 {ticket_id} 不存在")
    before = row.status
    if body.status:
        row.status = body.status
        if body.status in ("RESOLVED", "CLOSED"):
            row.resolved_at = datetime.now()
    if body.handler_id:
        row.handler_id = body.handler_id
    if body.category:
        row.category = body.category
    if body.satisfaction is not None:
        row.satisfaction = body.satisfaction
    audit.record(db, action="ticket.update", actor_id=principal.subject,
                 actor_role=principal.role, resource="after_sales_ticket", resource_id=ticket_id,
                 detail={"before": before, "after": row.status}, ip=client_ip(request))

    # 闭环通知：工单被处理完必须告诉提问题的学生。
    # 只在**状态真的从「未完结」变成「已解决/已关闭」**时发，
    # 否则反复点「更新处理人」会把学生的收件箱刷屏。
    notice = None
    if (row.status in ("RESOLVED", "CLOSED") and before not in ("RESOLVED", "CLOSED")
            and row.student_id is not None):
        notice = notify.send(
            db, recipient_type="student", recipient_id=row.student_id,
            category="ticket", title=f"【售后工单{'已解决' if row.status == 'RESOLVED' else '已关闭'}】{row.summary or ''}".strip(),
            body="\n".join(p for p in [
                f"工单编号：{row.id}",
                f"处理结果：{row.status}",
                f"处理人：{principal.subject}",
                "如对处理结果不满意，可在「我的反馈」里重新提交。",
            ] if p),
            biz_type="after_sales_ticket", biz_id=row.id)
    db.commit()
    return ok({"id": ticket_id, "status": row.status, "before": before,
               "notify": ({"channel": notice.channel,
                           "delivered_via": notice.delivered_via} if notice else None)})


@router.get("/alerts", summary="心理预警列表（仅管理层，敏感数据）")
def list_alerts(status: Optional[str] = None, risk_level: Optional[str] = None,
                notified: Optional[bool] = Query(None, description="true=已触达 / false=未触达"),
                principal: Principal = Depends(manager_only),
                db: Session = Depends(get_db)) -> dict:
    query = db.query(MentalAlert)
    if status:
        query = query.filter(MentalAlert.status == status)
    if risk_level:
        query = query.filter(MentalAlert.risk_level == risk_level)
    if notified is True:
        query = query.filter(MentalAlert.notified_at.isnot(None))
    elif notified is False:
        query = query.filter(MentalAlert.notified_at.is_(None))
    rows = query.order_by(MentalAlert.id.desc()).limit(100).all()
    return ok({"total": len(rows),
               "undelivered": sum(1 for r in rows if r.notified_at is None),
               "items": [{"id": r.id, "student_id": r.student_id,
                          "risk_level": r.risk_level, "reason": r.reason,
                          "status": r.status,
                          "notified_at": r.notified_at.isoformat() if r.notified_at else None,
                          "notified_to": r.notified_to,
                          "notify_channel": r.notify_channel,
                          "handler_id": r.handler_id,
                          "created_at": r.created_at.isoformat()} for r in rows]})


@router.post("/alerts/notify", summary="触达心理预警（写 notified_at + 干预建议）")
def notify_alerts(body: AlertNotifyRequest, request: Request,
                  principal: Principal = Depends(manager_only),
                  db: Session = Depends(get_db)) -> dict:
    """REQ：一旦识别出高危风险，立即触发预警并记录原因，辅助老师快速介入。

    不传 `alert_ids` 就把「还没触达过」的全部触达一遍；已触达的不会被重复写，
    除非显式点名 ID（换处理人后重新通知的场景）。
    """
    delivered = todo.deliver_alerts(db, actor_subject=principal.subject,
                                    actor_role=principal.role,
                                    actor_ref_id=principal.ref_id,
                                    alert_ids=body.alert_ids,
                                    channel=body.channel)
    if not delivered:
        db.rollback()
        return ok({"total": 0, "items": [],
                   "message": "没有需要触达的预警（要么都已触达，要么没有未关闭的预警）"})
    audit.record(db, action="alert.notify.api", actor_id=principal.subject,
                 actor_role=principal.role, resource="mental_alert",
                 resource_id=body.channel,
                 detail={"count": len(delivered), "ids": [d["id"] for d in delivered]},
                 ip=client_ip(request))
    db.commit()
    return ok({"total": len(delivered), "channel": body.channel, "items": delivered})


@router.patch("/alerts/{alert_id}", summary="更新预警跟进状态（仅管理层）")
def update_alert(alert_id: int, body: dict, request: Request,
                 principal: Principal = Depends(manager_only),
                 db: Session = Depends(get_db)) -> dict:
    row = db.get(MentalAlert, alert_id)
    if row is None:
        raise NotFound(f"预警记录 {alert_id} 不存在")
    new_status = body.get("status")
    if new_status not in ("OPEN", "FOLLOWING", "CLOSED"):
        raise Conflict("status 只能是 OPEN / FOLLOWING / CLOSED")
    row.status = new_status
    if principal.ref_id:
        row.handler_id = principal.ref_id
    audit.record(db, action="alert.update", actor_id=principal.subject,
                 actor_role=principal.role, resource="mental_alert", resource_id=alert_id,
                 detail={"status": new_status}, ip=client_ip(request))
    db.commit()
    return ok({"id": alert_id, "status": row.status})
