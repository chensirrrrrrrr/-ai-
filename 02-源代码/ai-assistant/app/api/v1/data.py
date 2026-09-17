"""数据能力：NL2SQL 查询 / 审计日志查询。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from ...core import ok
from ...db import get_db
from ...models import AuditLog
from ...schemas import Nl2SqlRequest
from ...services import audit
from ...services.nl2sql import ROLE_SCOPE, run_query
from ..deps import Principal, client_ip, require_roles

router = APIRouter(tags=["data"])

staff_only = require_roles("employee", "manager", "admin")
manager_only = require_roles("manager", "admin")


@router.post("/nl2sql/query", summary="自然语言查库（受控模板 + 白名单）")
def nl2sql_query(body: Nl2SqlRequest, request: Request,
                 principal: Principal = Depends(staff_only),
                 db: Session = Depends(get_db)) -> dict:
    """`role_scope` 默认由登录角色推导，也允许显式指定（服务端仍会二次鉴权）。"""
    scope = body.role_scope or ROLE_SCOPE.get(principal.role, "sales")
    if principal.role in ("manager", "admin"):
        scope = body.role_scope or "manager"

    result = run_query(db, body.question, scope, body.max_rows)

    audit.record(db, action="nl2sql.query", actor_id=principal.subject,
                 actor_role=principal.role, resource="nl2sql",
                 detail={"question": body.question, "scope": scope,
                         "template": result["template_id"], "row_count": result["row_count"],
                         "sql": result["sql_preview"]},
                 ip=client_ip(request))
    db.commit()
    return ok(result)


@router.get("/nl2sql/templates", summary="查看可用查询模板（便于前端提示）")
def list_templates(principal: Principal = Depends(staff_only)) -> dict:
    from ...services.nl2sql import TEMPLATES

    return ok({"items": [{"id": t.id, "title": t.title, "scopes": list(t.scopes),
                          "keywords": list(t.keywords)} for t in TEMPLATES]})


@router.get("/audit/logs", summary="审计日志查询")
def list_audit_logs(action: Optional[str] = None,
                    actor_id: Optional[str] = None,
                    trace_id: Optional[str] = None,
                    limit: int = Query(50, ge=1, le=200),
                    principal: Principal = Depends(manager_only),
                    db: Session = Depends(get_db)) -> dict:
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if actor_id:
        query = query.filter(AuditLog.actor_id == actor_id)
    if trace_id:
        query = query.filter(AuditLog.trace_id == trace_id)
    rows = query.order_by(AuditLog.id.desc()).limit(limit).all()
    return ok({"total": len(rows),
               "items": [{"id": r.id, "actor_id": r.actor_id, "actor_role": r.actor_role,
                          "action": r.action, "resource": r.resource,
                          "resource_id": r.resource_id, "detail": r.detail,
                          "trace_id": r.trace_id,
                          "created_at": r.created_at.isoformat()} for r in rows]})
