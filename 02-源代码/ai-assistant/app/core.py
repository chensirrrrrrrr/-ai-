"""横切关注点：统一响应信封、trace_id、领域异常、密码哈希、JWT。

统一响应结构（与需求文档第 7.1 节一致）：
    {"code": 0, "message": "success", "data": {...}, "trace_id": "a1b2c3d4"}
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings

# --------------------------------------------------------------------------- #
# trace_id：用 contextvar 串起「中间件生成 → 业务读 → 响应回写」
# --------------------------------------------------------------------------- #
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:8]


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def get_trace_id() -> str:
    value = _trace_id.get()
    if not value:
        value = new_trace_id()
        _trace_id.set(value)
    return value


# --------------------------------------------------------------------------- #
# 统一响应信封
# --------------------------------------------------------------------------- #
def ok(data: Any = None, message: str = "success") -> dict:
    return {"code": 0, "message": message, "data": data, "trace_id": get_trace_id()}


def fail(message: str, code: int = 40000, http_status: int = 400, data: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "data": data, "trace_id": get_trace_id()},
    )


# 业务错误码（与文档一致：0 成功，其余为业务码）
CODE_INVALID_PARAM = 40000
CODE_UNAUTHORIZED = 40100
CODE_FORBIDDEN = 40300
CODE_NOT_FOUND = 40400
CODE_CONFLICT = 40900
CODE_UPSTREAM = 50200
CODE_INTERNAL = 50000
CODE_TOO_MANY = 42900


class AppError(Exception):
    """领域异常：由业务代码抛出，统一被 handler 转成响应信封。"""

    def __init__(self, message: str, code: int = CODE_INVALID_PARAM,
                 http_status: int = 400, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.data = data


class NotFound(AppError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(message, CODE_NOT_FOUND, 404)


class Unauthorized(AppError):
    def __init__(self, message: str = "未认证或凭证已失效") -> None:
        super().__init__(message, CODE_UNAUTHORIZED, 401)


class Forbidden(AppError):
    def __init__(self, message: str = "无权访问该资源") -> None:
        super().__init__(message, CODE_FORBIDDEN, 403)


class Conflict(AppError):
    def __init__(self, message: str = "状态冲突") -> None:
        super().__init__(message, CODE_CONFLICT, 409)


class UpstreamError(AppError):
    def __init__(self, message: str = "上游服务异常") -> None:
        super().__init__(message, CODE_UPSTREAM, 502)


class TooManyRequests(AppError):
    """限流命中（429）。data.retry_after 告诉调用方多久后再来。"""

    def __init__(self, message: str = "请求过于频繁", retry_after: int = 60) -> None:
        super().__init__(message, CODE_TOO_MANY, 429, data={"retry_after": retry_after})
        self.retry_after = retry_after


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return fail(exc.message, exc.code, exc.http_status, exc.data)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {401: CODE_UNAUTHORIZED, 403: CODE_FORBIDDEN, 404: CODE_NOT_FOUND}
        code = mapping.get(exc.status_code, CODE_INVALID_PARAM)
        return fail(str(exc.detail), code, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return fail("参数校验失败", CODE_INVALID_PARAM, 422, data=exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        return fail(f"服务内部错误: {exc}" if settings.debug else "服务内部错误",
                    CODE_INTERNAL, 500)


# --------------------------------------------------------------------------- #
# 密码哈希：PBKDF2-HMAC-SHA256，纯标准库，避免 bcrypt 的编译/版本坑
# --------------------------------------------------------------------------- #
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        algo, rounds, salt, want = hashed.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_access_token(subject: str, role: str,
                        extra: Optional[dict] = None,
                        expires_minutes: Optional[int] = None) -> str:
    now = datetime.now(timezone.utc)
    minutes = expires_minutes or settings.access_token_expire_minutes
    payload = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        "jti": uuid.uuid4().hex[:12],
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("凭证已过期") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("凭证无效") from exc


# --------------------------------------------------------------------------- #
# Dify 工具回调签名（FastAPI 作为被调用方时校验来源）
# --------------------------------------------------------------------------- #
def sign_tool_payload(body: bytes, timestamp: str) -> str:
    msg = timestamp.encode() + b"." + body
    return hmac.new(settings.tool_shared_secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_tool_signature(body: bytes, timestamp: str, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_tool_payload(body, timestamp), signature)
