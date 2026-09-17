"""内部工具回调：供 Dify 的 Agent / Workflow 以 Tool 节点反向调用。

安全模型
--------
- 路径前缀 `/internal`，不对外暴露（生产在网关层只放行 Dify 的容器网段）；
- **两道门，满足其一即可**：
  1. *HMAC 签名（原有，首选）*：`X-Tool-Timestamp` + `X-Tool-Signature`
     （HMAC-SHA256，签名内容 = `timestamp + "." + raw_body`），
     再校验时间窗 ±300s 防重放；
  2. *Dify 静态 Key（新增）*：`Authorization: Bearer <DIFY_TOOL_KEY>`。
     存在的理由：Dify 的「自定义工具 / HTTP 请求节点」只支持 none / api_key /
     bearer，**算不出 HMAC**。该通道默认关闭（`DIFY_TOOL_KEY` 为空即关），
     且**没有防重放**，等价于共享密码 —— 所以生产必须靠网关限制来源网段。
     ⚠️ 带了 `X-Tool-Signature` 就只走 HMAC，**不会**再用静态 Key 兜底，
     否则伪造签名的请求可以借静态 Key 绕过防重放。
- 工具自身仍做角色判断（`ToolSpec.min_role`），调用方在请求体里带 `role`
  （Dify 会带上会话角色），**不因为通道受信就跳过检查**。

本模块只做传输层的事：验签 → 取参 → 派发 → 包信封。
工具实现与「写操作两阶段确认（含撤回）」都在 `services/agent_tools.py`。

写工具的两次调用
----------------
1. `{"params": {...}}` → 返回 `{needs_confirm: true, preview: "人话回显", token: "..."}`，
   **不写任何业务数据**；
2. `{"params": {...}, "confirm": "<token>"}` → 校验后真正执行，返回执行结果；
3. `{"cancel": "<token>"}` → 撤回这条待确认写操作（置 CANCELLED，不执行），
   只有发起人本人可撤。用于「用户看过预览后反悔」，避免 token 一直悬挂到过期。

读工具直接执行，`data` 就是工具自己的返回值（与改造前一致，已配置的 Tool 节点不受影响）。
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ...config import settings
from ...core import AppError, Forbidden, ok, verify_tool_signature
from ...db import SessionLocal
from ...services import agent_tools
from ...services.agent_tools import TOOL_REGISTRY, ToolContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal-tools"])

SIGNATURE_TTL_SECONDS = 300


def _check_signature(request: Request, body: bytes) -> None:
    """两道门，满足其一即可：HMAC 签名（首选）或 Dify 静态 Key。

    顺序很重要：**只要带了 `X-Tool-Signature` 就只按 HMAC 校验**，验不过直接 403，
    不会退回静态 Key。否则攻击者只要伪造一个签名就能借静态 Key 通道绕过防重放。
    """
    ts = request.headers.get("x-tool-timestamp", "")
    sig = request.headers.get("x-tool-signature", "")

    if sig:
        if not verify_tool_signature(body, ts, sig):
            raise Forbidden("工具调用签名校验失败")
        try:
            delta = abs(time.time() - float(ts))
        except ValueError as exc:
            raise Forbidden("时间戳格式错误") from exc
        if delta > SIGNATURE_TTL_SECONDS:
            raise Forbidden(f"请求已过期（偏差 {int(delta)}s）")
        return

    if _static_key_ok(request):
        return

    raise Forbidden("工具调用签名校验失败")


def _static_key_ok(request: Request) -> bool:
    """Dify 静态 Key 通道。

    `DIFY_TOOL_KEY` 留空 ⇒ 通道整体关闭（默认）。
    ⚠️ 这条通道**不做时间窗校验**，所以它防的是「不知道 Key 的人」，
    防不了「抓包重放」。生产靠网关限制来源网段兜底。
    """
    expected = (settings.dify_tool_key or "").strip()
    if not expected:
        return False
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return False
    return hmac.compare_digest(token.strip(), expected)


@router.get("/tools", summary="工具清单（给 Dify 配置 Tool 节点用）")
def list_tools(include_legacy: bool = True) -> dict:
    """TDD 4.5 的 19 个工具 + JSON Schema + 读写分类。

    `coverage` 直接给出「TDD 声明了 19 个、实现了几个」，便于验收时核对。
    """
    return ok({
        "items": agent_tools.catalog(include_legacy=include_legacy),
        "counts": {
            "total": len(TOOL_REGISTRY),
            "read": sum(1 for s in TOOL_REGISTRY.values() if not s.is_write),
            "write": sum(1 for s in TOOL_REGISTRY.values() if s.is_write),
        },
        "coverage": agent_tools.tdd_coverage(),
        "write_flow": "写工具第一次调用只回显确认（needs_confirm=true + token），"
                      "带 confirm=<token> 再调一次才执行；带 cancel=<token> 则撤回该待确认操作",
    })


@router.post("/tools/{tool_name}", summary="工具调用入口（HMAC 签名校验；写工具需二次确认）")
async def invoke_tool(tool_name: str, request: Request):
    raw = await request.body()
    try:
        _check_signature(request, raw)
    except Forbidden as exc:
        return JSONResponse(status_code=403, content={"code": 40300, "message": exc.message})

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"code": 40000, "message": "请求体不是合法 JSON"})

    params = payload.get("params") or payload.get("arguments") or {}
    if not isinstance(params, dict):
        return JSONResponse(status_code=400,
                            content={"code": 40000, "message": "params 必须是对象"})

    ctx = ToolContext(
        actor_subject=str(payload.get("actor") or payload.get("user") or "dify"),
        actor_role=str(payload.get("role") or "employee").lower(),
    )
    confirm_token: Optional[str] = payload.get("confirm") or payload.get("confirm_token")
    cancel_token: Optional[str] = payload.get("cancel") or payload.get("cancel_token")
    dry_run = bool(payload.get("dry_run"))

    db = SessionLocal()
    try:
        result = agent_tools.invoke(db, tool_name, params, ctx,
                                    confirm_token=confirm_token, dry_run=dry_run,
                                    cancel_token=cancel_token)
        db.commit()
    except Forbidden as exc:
        db.rollback()
        return JSONResponse(status_code=403, content={"code": 40300, "message": exc.message})
    except AppError as exc:
        db.rollback()
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message})
    except Exception as exc:                          # noqa: BLE001
        db.rollback()
        logger.warning("工具 %s 执行失败: %s", tool_name, exc)
        return JSONResponse(status_code=500, content={"code": 50000, "message": str(exc)})
    finally:
        db.close()

    if result.get("kind") == "read":
        # 读工具保持「data 即工具返回值」的历史契约，已配置的 Dify Tool 节点不用改
        return ok(result.get("data"))

    envelope: Dict[str, Any] = {
        "kind": "write",
        "needs_confirm": result.get("needs_confirm", False),
        "executed": result.get("executed", False),
        "cancelled": result.get("cancelled"),
        "status": result.get("status"),
        "preview": result.get("preview"),
        "token": result.get("token"),
        "expires_at": result.get("expires_at"),
        "confirm_hint": result.get("confirm_hint"),
        "note": result.get("note"),
        "result": result.get("result"),
    }
    return ok({k: v for k, v in envelope.items() if v is not None})
