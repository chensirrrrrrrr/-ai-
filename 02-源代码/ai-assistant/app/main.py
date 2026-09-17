"""FastAPI 应用入口。

启动：
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.v1.router import api_router, internal_router
from .config import settings
from .core import new_trace_id, ok, register_exception_handlers, set_trace_id
from .services import scheduler as scheduler_service

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
logger = logging.getLogger("app")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "留学机构 AI 智能助手系统后端。\n\n"
            "- 技术栈：Python + FastAPI + Dify(Agent) + MySQL/SQLite\n"
            "- 对话走 Dify，业务写操作留在这里（可审计、可事务）\n"
            f"- 当前模式：Dify={settings.dify_mode} / ASR={settings.asr_provider}"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # 跨域：前后端分离部署（用户端 / 管理后台与 API 不同域）时必须放开；
    # 生产用 CORS_ORIGINS 白名单登记，debug=True 时全放通便于本地联调。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-trace-id", "x-process-time-ms", "content-disposition"],
    )

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        set_trace_id(trace_id)
        started = time.perf_counter()
        response = await call_next(request)
        cost_ms = (time.perf_counter() - started) * 1000
        response.headers["x-trace-id"] = trace_id
        response.headers["x-process-time-ms"] = f"{cost_ms:.1f}"
        if request.url.path not in ("/health", "/api/v1/health"):
            logger.info("%s %s -> %s (%.1fms) trace=%s",
                        request.method, request.url.path,
                        response.status_code, cost_ms, trace_id)
        return response

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.include_router(internal_router, prefix="/internal")

    @app.get("/", include_in_schema=False)
    def index() -> dict:
        return ok({
            "app": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": f"{settings.api_v1_prefix}/health",
            "dify_mode": settings.dify_mode,
        })

    @app.on_event("startup")
    def _startup() -> None:
        logger.info("启动完成：db=%s dify=%s asr=%s scheduler=%s",
                    "sqlite" if settings.is_sqlite else "mysql",
                    settings.dify_mode, settings.asr_provider,
                    "on" if settings.enable_scheduler else "off")
        # 主动待办推送的定时器：默认关（见 services/scheduler 的说明），
        # 要用 POST /api/v1/todo/push 手动触发也行 —— 两条路走同一个 run_once()。
        scheduler_service.autostart()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler_service.scheduler.stop()

    return app


app = create_app()
