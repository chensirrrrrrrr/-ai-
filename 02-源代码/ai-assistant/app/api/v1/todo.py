"""主动待办推送：待办清单 / 手动推送 / 推送留痕 / 标记已处理。

这一组接口同时是「定时任务」的手动入口 —— 需求要的是「配置定时任务或触发器」，
`POST /todo/push` 就是那个触发器；后台调度器打开的只是「自动按时触发」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core import AppError, Forbidden, NotFound, ok
from ...db import get_db
from ...models import TodoPush
from ...schemas import TodoPushRequest
from ...services import audit, todo
from ..deps import Principal, client_ip, require_roles

router = APIRouter(tags=["todo"])

staff_only = require_roles("employee", "manager", "admin")

MANAGER_ROLES = ("manager", "admin")


def _push_out(row: TodoPush) -> dict:
    return {
        "id": row.id,
        "subject": row.subject,
        "role": row.role,
        "employee_id": row.employee_id,
        "category": row.category,
        "title": row.title,
        "question": row.question,
        "severity": row.severity,
        "item_count": row.item_count,
        "summary": row.summary,
        "channel": row.channel,
        "delivered_at": row.delivered_at.isoformat(),
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_by": row.acknowledged_by,
        "ack_remark": row.ack_remark,
    }


@router.get("/todo/pending", summary="我的待办（主动提醒清单）")
def list_pending(principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    """待办是**现场算**的：处理完立刻消失，不需要手动关掉。

    响应里的 `question` 就是「主动询问」的那句话（需求原文句式），
    `digest` 是渲染好的整段话术，对话侧与前端横幅共用同一份口径。
    """
    todos = todo.collect(db, principal.role)
    return ok({
        "total": sum(t["count"] for t in todos),
        "category_count": len(todos),
        "highest_severity": todos[0]["severity"] if todos else "none",
        "categories": [t["category"] for t in todos],
        "digest": todo.digest_text(todos),
        "items": todos,
        "generated_at": datetime.now().isoformat(),
    })


@router.post("/todo/push", summary="立刻推送一轮待办（定时任务的同一入口）")
def push_now(body: TodoPushRequest, request: Request,
             principal: Principal = Depends(staff_only),
             db: Session = Depends(get_db)) -> dict:
    """默认只推自己；`all_staff=true` 需要管理层权限。

    同一时间窗内重复触发会命中幂等键（`duplicated=true`），不会重复提醒 ——
    否则定时任务 + 手动触发并存时，员工会被同一条待办反复轰炸。
    """
    categories = body.categories
    if categories:
        unknown = [c for c in categories if c not in todo.CATEGORIES]
        if unknown:
            raise AppError(f"未知的待办分类：{unknown}；可选 {list(todo.CATEGORIES)}")

    if body.all_staff:
        if principal.role not in MANAGER_ROLES:
            raise Forbidden(f"全员推送仅限 {list(MANAGER_ROLES)}，当前角色 {principal.role}")
        result = todo.run_once()
        # 全员模式下明细按人分组在 details 里，而不是一条条 records ——
        # 一次可能推几十条，平铺回来对调用方没有意义。
        payload = {
            "mode": "all_staff",
            "pushed": result["pushed"],
            "staff_count": result["staff_count"],
            "alerts_delivered": result["alerts_delivered"],
            "records": [],
            "delivered_alerts": [],
            "details": result["details"],
        }
    else:
        result = todo.push_due(db, subject=principal.subject, role=principal.role,
                               ref_id=principal.ref_id, categories=categories)
        audit.record(db, action="todo.push", actor_id=principal.subject,
                     actor_role=principal.role, resource="todo_push", resource_id=None,
                     detail={"mode": "self", "categories": categories or list(todo.CATEGORIES),
                             "pushed": result["pushed"],
                             "alerts_delivered": len(result["delivered_alerts"])},
                     ip=client_ip(request))
        db.commit()
        payload = {
            "mode": "self",
            "pushed": result["pushed"],
            "staff_count": None,
            "alerts_delivered": len(result["delivered_alerts"]),
            "records": result["records"],
            "delivered_alerts": result["delivered_alerts"],
            "details": [],
        }

    return ok(payload)


@router.get("/todo/pushes", summary="推送记录（默认只看自己）")
def list_pushes(all: bool = Query(False, description="管理层可传 true 看全员"),
                category: Optional[str] = None,
                include_acknowledged: bool = Query(True),
                limit: int = Query(50, ge=1, le=200),
                principal: Principal = Depends(staff_only),
                db: Session = Depends(get_db)) -> dict:
    query = db.query(TodoPush)
    if all:
        if principal.role not in MANAGER_ROLES:
            raise Forbidden(f"查看全员推送记录仅限 {list(MANAGER_ROLES)}，"
                            f"当前角色 {principal.role}")
    else:
        query = query.filter(TodoPush.subject == principal.subject)
    if category:
        query = query.filter(TodoPush.category == category)
    if not include_acknowledged:
        query = query.filter(TodoPush.acknowledged_at.is_(None))

    rows = query.order_by(TodoPush.id.desc()).limit(limit).all()
    return ok({
        "total": len(rows),
        "unacknowledged": sum(1 for r in rows if r.acknowledged_at is None),
        "items": [_push_out(r) for r in rows],
    })


@router.patch("/todo/pushes/{push_id}/ack", summary="标记待办已处理")
def acknowledge(push_id: int, body: dict, request: Request,
                principal: Principal = Depends(staff_only),
                db: Session = Depends(get_db)) -> dict:
    """标记「这条提醒我处理完了」。

    刻意做成**幂等**：重复标记不会报错，返回 `already=True`。
    「已处理」这个状态跟调用几次无关，做成 409 只会让前端多点一下就弹错。
    """
    row = db.get(TodoPush, push_id)
    if row is None:
        raise NotFound(f"推送记录 {push_id} 不存在")
    if row.subject != principal.subject and principal.role not in MANAGER_ROLES:
        raise Forbidden("只能标记自己的待办提醒")

    already = row.acknowledged_at is not None
    if not already:
        row.acknowledged_at = datetime.now()
        row.acknowledged_by = principal.subject
        row.ack_remark = (body or {}).get("remark")
        audit.record(db, action="todo.ack", actor_id=principal.subject,
                     actor_role=principal.role, resource="todo_push", resource_id=push_id,
                     detail={"category": row.category, "remark": row.ack_remark},
                     ip=client_ip(request))
        db.commit()

    return ok({"id": row.id, "acknowledged_at": row.acknowledged_at.isoformat(),
               "already": already})
