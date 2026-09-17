"""路由聚合：对外 API（/api/v1）与内部工具回调（/internal）分开挂载。"""
from __future__ import annotations

from fastapi import APIRouter

from . import chat, crm, data, notice, ops, org, student, system, todo, tools

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(chat.router)
api_router.include_router(crm.router)
api_router.include_router(student.router)
api_router.include_router(org.router)
api_router.include_router(ops.router)
api_router.include_router(todo.router)
api_router.include_router(data.router)
api_router.include_router(notice.router)

internal_router = APIRouter()
internal_router.include_router(tools.router)

__all__ = ["api_router", "internal_router"]
