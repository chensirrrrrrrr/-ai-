"""API 层：依赖注入与鉴权。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core import Forbidden, Unauthorized, decode_access_token
from ..db import get_db
from ..models import SysAccount

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    """当前调用者。subject 是 sys_account.username。"""

    subject: str
    role: str
    ref_id: Optional[int] = None
    display_name: Optional[str] = None

    @property
    def is_manager(self) -> bool:
        return self.role in ("manager", "admin")

    @property
    def is_staff(self) -> bool:
        return self.role in ("employee", "manager", "admin")


def get_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Principal:
    """解析 Bearer Token 并校验账号仍然有效。"""
    if credentials is None or not credentials.credentials:
        raise Unauthorized("缺少 Authorization: Bearer <token>")
    payload = decode_access_token(credentials.credentials)

    username = payload.get("sub")
    role = payload.get("role", "visitor")

    # 匿名访客没有 sys_account 记录，直接由 Token 声明构造（权限仍受 ROLE_AGENTS 限制）
    if payload.get("anonymous"):
        return Principal(subject=username, role=role, ref_id=None, display_name="访客")

    account = db.query(SysAccount).filter(SysAccount.username == username).first()
    if account is None:
        raise Unauthorized("账号不存在")
    if not account.is_active:
        raise Forbidden("账号已被停用")

    return Principal(subject=account.username, role=account.role,
                     ref_id=account.ref_id, display_name=account.display_name)


def require_roles(*roles: Sequence[str]):
    """生成一个「限定角色」的依赖。"""

    allowed = set(roles)

    def _checker(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in allowed:
            raise Forbidden(f"该操作仅限 {sorted(allowed)}，当前角色 {principal.role}")
        return principal

    return _checker


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
