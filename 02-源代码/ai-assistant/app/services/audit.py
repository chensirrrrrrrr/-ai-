"""审计日志：所有写操作调用 `record()` 落一条，含 trace_id 便于全链路回放。"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from ..core import get_trace_id
from ..models import AuditLog


def record(db: Session, *, action: str,
           actor_id: Optional[str] = None,
           actor_role: Optional[str] = None,
           resource: Optional[str] = None,
           resource_id: Optional[Any] = None,
           detail: Optional[dict] = None,
           ip: Optional[str] = None) -> AuditLog:
    """把审计记录加入当前事务（不 commit，由调用方统一提交，保证与业务同生共死）。"""
    row = AuditLog(
        actor_id=str(actor_id) if actor_id is not None else None,
        actor_role=actor_role,
        action=action,
        resource=resource,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail=detail or {},
        ip=ip,
        trace_id=get_trace_id(),
    )
    db.add(row)
    return row
