"""通知中心：我的通知 / 已读 / 通道状态。

统一收口「系统主动告知」这件事（见 `services/notify.py` 的说明）。
收件人**从 Token 推导**，前端不传 recipient_id —— 与本项目「角色一律以 Token 为准」
的约定一致，避免前端伪造别人的收件箱。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core import Forbidden, NotFound, ok
from ...db import get_db
from ...services import notify
from ..deps import Principal, get_principal

router = APIRouter(tags=["通知"])

STAFF_ROLES = ("employee", "manager", "admin")


def recipient_of(principal: Principal) -> tuple[str, int]:
    """把 Token 里的身份翻译成「收件人」二元组。

    访客没有收件箱（也没有账号落库），显式拒绝而不是返回空列表 ——
    返回空列表会让前端以为「登录了但没消息」，排查成本更高。
    """
    if principal.role == "student" and principal.ref_id is not None:
        return "student", int(principal.ref_id)
    if principal.role in STAFF_ROLES and principal.ref_id is not None:
        return "employee", int(principal.ref_id)
    raise Forbidden("当前身份没有通知收件箱（访客账号不落库）")


@router.get("/notifications", summary="我的通知")
def list_mine(category: Optional[str] = None,
              unread_only: bool = False,
              page: int = Query(1, ge=1),
              page_size: int = Query(20, ge=1, le=100),
              principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)) -> dict:
    rtype, rid = recipient_of(principal)
    data = notify.list_for(db, recipient_type=rtype, recipient_id=rid,
                           category=category, unread_only=unread_only,
                           limit=page_size, offset=(page - 1) * page_size)
    return ok({"page": page, "page_size": page_size, **data})


@router.get("/notifications/unread-count", summary="未读通知数")
def unread(principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)) -> dict:
    rtype, rid = recipient_of(principal)
    return ok({"unread": notify.unread_count(db, recipient_type=rtype, recipient_id=rid)})


@router.get("/notifications/channels", summary="通知通道就绪情况")
def channels(principal: Principal = Depends(get_principal)) -> dict:
    """哪个通道现在真能发出去。

    未配置凭证的通道会**降级为站内**，这里把真实情况摊开，
    免得把「只落了站内」误当成「企业微信已通知」。
    """
    _ = principal
    return ok(notify.channel_status())


@router.post("/notifications/read-all", summary="全部标记已读")
def read_all(principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)) -> dict:
    rtype, rid = recipient_of(principal)
    count = notify.mark_all_read(db, recipient_type=rtype, recipient_id=rid)
    db.commit()
    return ok({"marked": count})


@router.post("/notifications/{notif_id}/read", summary="标记单条已读（幂等）")
def read_one(notif_id: int, read: bool = Query(True),
             principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)) -> dict:
    rtype, rid = recipient_of(principal)
    row = notify.mark_read(db, notif_id, recipient_type=rtype, recipient_id=rid, read=read)
    if row is None:
        # 别人的通知一律按「不存在」处理，不回显归属信息
        raise NotFound(f"通知 {notif_id} 不存在")
    db.commit()
    return ok({"id": row.id, "read": row.read_at is not None})
