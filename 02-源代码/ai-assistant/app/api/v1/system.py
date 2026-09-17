"""系统与认证：健康检查 / Token 签发。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import DIFY_APP_NAMES, settings
from ...core import (AppError, Forbidden, Unauthorized, create_access_token,
                     hash_password, ok, verify_password)
from ...db import get_db
from ...models import SysAccount
from ...schemas import TokenRequest
from ...services import audit
from ..deps import Principal, client_ip, get_principal

router = APIRouter(tags=["system"])


@router.get("/health", summary="健康检查（存活探针）")
def health(db: Session = Depends(get_db)) -> dict:
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:                      # noqa: BLE001
        db_ok = False
    return ok({
        "status": "ok" if db_ok else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "database": "up" if db_ok else "down",
        "dialect": "sqlite" if settings.is_sqlite else "mysql",
        "dify_mode": settings.dify_mode,
        # live 模式下把「Key 配齐没」直接摊开：配了一半会静默降级到 mock，
        # 光看 dify_mode=live 是看不出问题的。
        # ⚠️ dify_ready 在 mock 模式下也是 True —— mock 有完整的本地编排层，
        # 是受支持的运行形态，不是「降级」。
        "dify_live": settings.is_dify_live,
        "dify_ready": settings.dify_ready,
        "dify_key_count": len([n for n in DIFY_APP_NAMES if settings.dify_key(n)]),
        "dify_missing_keys": settings.dify_missing_keys,
        "asr_provider": settings.asr_provider,
    })


@router.get("/ready", summary="就绪探针（数据库连通 + 关键配置齐备）")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return ok({
        "ready": settings.dify_ready,
        "dify_live": settings.is_dify_live,
        "dify_missing_keys": settings.dify_missing_keys,
    })


@router.post("/auth/token", summary="账号密码换取 JWT")
def issue_token(body: TokenRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    account = db.query(SysAccount).filter(SysAccount.username == body.username).first()
    if account is None or not verify_password(body.password, account.password_hash):
        raise Unauthorized("用户名或密码错误")
    if not account.is_active:
        raise Forbidden("账号已停用")

    account.last_login_at = datetime.now()
    audit.record(db, action="auth.login", actor_id=account.username, actor_role=account.role,
                 resource="sys_account", resource_id=account.id,
                 ip=client_ip(request))
    db.commit()

    token = create_access_token(account.username, account.role, {"ref_id": account.ref_id})
    return ok({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
        "role": account.role,
        "display_name": account.display_name,
    })


@router.post("/auth/visitor", summary="签发访客 Token（官网/H5 客服窗口用，匿名）")
def visitor_token(request: Request, db: Session = Depends(get_db)) -> dict:
    """访客无需注册即可咨询；Token 只携带 visitor 角色，能触达的能力被 ROLE_AGENTS 限制。"""
    import uuid

    username = f"visitor_{uuid.uuid4().hex[:8]}"
    token = create_access_token(username, "visitor", {"anonymous": True})
    audit.record(db, action="auth.visitor", actor_id=username, actor_role="visitor",
                 resource="sys_account", ip=client_ip(request))
    db.commit()
    return ok({"access_token": token, "token_type": "bearer", "role": "visitor",
               "expires_in": settings.access_token_expire_minutes * 60})


@router.get("/auth/me", summary="查看当前登录者")
def me(principal: Principal = Depends(get_principal)) -> dict:
    return ok({"username": principal.subject, "role": principal.role,
               "ref_id": principal.ref_id, "display_name": principal.display_name})


@router.post("/auth/password", summary="修改密码（需已登录）")
def change_password(body: dict, request: Request,
                    principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> dict:
    old = body.get("old_password", "")
    new = body.get("new_password", "")
    if len(new) < 6:
        raise AppError("新密码至少 6 位")
    account = db.query(SysAccount).filter(SysAccount.username == principal.subject).first()
    if account is None or not verify_password(old, account.password_hash):
        raise Unauthorized("原密码不正确")
    account.password_hash = hash_password(new)
    audit.record(db, action="auth.change_password", actor_id=principal.subject,
                 actor_role=principal.role, resource="sys_account", resource_id=account.id,
                 ip=client_ip(request))
    db.commit()
    return ok({"changed": True})
