"""统一对话入口：意图路由 → Agent → 统一响应。含语音录入。

两种执行路径：
- `DIFY_MODE=live`：交给 Dify 的 Agent 应用（它自己去调工具、检索知识库）。
- `DIFY_MODE=mock`：交给 `services.mock_agent` 本地编排 —— 它认路由结果，
  需要取数的走受控模板真查库，需要办理的落业务记录，知识类检索内置语料。
  这一层是离线可运行的替代品，不联网也不编造。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...config import settings
from ...core import AppError, ok
from ...db import get_db
from ...schemas import ChatRequest
from ...services import audit, content_safety, metrics, mock_agent, ratelimit
from ...services.asr import get_asr_provider
from ...services.dify import chunk_text, dify_client
from ...services.intent import route
from ..deps import Principal, client_ip, get_principal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

CLARIFY_TEXT = ("为了给你更准确的答复，请补充一点信息：你是想问机构/服务政策，"
                "还是想办理具体事项（请假、报名、投诉）？")

NL2SQL_HINT = "调用 /api/v1/nl2sql/query 获取数据结果"


def _dify_inputs(body: ChatRequest, principal: Principal) -> dict:
    """拼给 Dify 应用的 `inputs`。

    前端**故意不传** inputs（见 `frontend/js/views/chat.js` 的注释：角色一律以
    Token 为准），但新的「学生助手 Chatflow」把 `role` / `student_id` 声明成了
    **起始变量**，不注入就是空串 —— 分类器照样跑，分支里的工具调用却会因为
    角色缺失被 403。所以这两个值必须由服务端按 Token 补：

    - `role`：**始终**以 `principal.role` 覆盖，客户端传什么都不作数（防访客伪造
      角色触达员工 / 管理侧能力，和 intent 路由的取 `role` 口径保持一致）；
    - `student_id`：只有学生账号才有「自己的学生档案」（`ref_id` = student.id）。
      其他角色的 `ref_id` 指向 employee，塞进 student_id 是错的，所以不注入。
    """
    inputs = dict(body.inputs or {})
    inputs["role"] = principal.role
    if principal.role == "student" and principal.ref_id is not None:
        inputs["student_id"] = str(principal.ref_id)
    return inputs


@router.post("/chat/message", summary="统一对话入口（含意图路由）")
async def chat_message(body: ChatRequest, request: Request,
                       principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)) -> dict:
    """安全说明：`role` 一律以 Token 里的角色为准，请求体里的 role 只作参考，
    防止访客伪造角色越权触达员工/管理侧能力。"""
    role = principal.role
    user = f"{settings.dify_user_prefix}-{principal.subject}"

    # 对话防刷：每账号每分钟 N 次（阈值见 RATE_LIMIT_CHAT_PER_MIN）。LLM 调用是
    # 全链路最贵的资源，这道闸放在意图路由之前 —— 被限的请求一次模型调用都不花。
    if settings.rate_limit_enabled:
        ratelimit.enforce("chat", principal.subject, settings.rate_limit_chat_per_min)

    # 内容安全：违禁内容不进模型、不进工具（直接礼貌拒绝 + 审计留痕）；
    # PII（身份证/银行卡/手机号）打码后才喂给 Dify、才落审计 —— 模型与日志
    # 都不该拿到可还原的 PII。打码不影响业务库。
    sanitized_message, _findings, blocked = content_safety.screen(body.message)
    if blocked:
        audit.record(db, action="chat.blocked", actor_id=principal.subject, actor_role=role,
                     resource="chat", detail={"reason": "blocked_word"},
                     ip=client_ip(request))
        db.commit()
        return ok({"answer": content_safety.GUARD_REPLY, "agent": "guard",
                   "intent": "content_blocked", "confidence": 1.0, "references": [],
                   "suggest_actions": [], "conversation_id": None, "degraded": False})
    body.message = sanitized_message

    intent = await route(body.message, role, user)
    metrics.record_chat(intent.action, intent.intent, intent.confidence,
                        intent.route_source)

    if intent.action == "clarify":
        audit.record(db, action="chat.clarify", actor_id=principal.subject, actor_role=role,
                     resource="chat", detail={"message": body.message, "intent": intent.intent},
                     ip=client_ip(request))
        db.commit()
        return ok({
            "answer": CLARIFY_TEXT, "agent": intent.agent, "intent": intent.intent,
            "confidence": intent.confidence, "references": [], "suggest_actions": [],
            "conversation_id": None, "degraded": False,
        })

    if settings.is_mock:
        # 离线编排：认 intent，能查库的查库、能落库的落库
        result = mock_agent.respond(intent, body.message, role, db,
                                    subject=principal.subject, ref_id=principal.ref_id)
    else:
        result = await dify_client.chat(
            intent.agent, body.message, user,
            conversation_id=body.conversation_id,
            inputs=_dify_inputs(body, principal),
        )

    suggest = list(result.suggest_actions)
    # 取数类问题统一提示 NL2SQL 入口（mock 编排通常已带上，这里只兜底、不重复加）
    if (intent.agent == "enterprise_assistant" and intent.intent == "nl2sql_query"
            and not any("nl2sql" in item for item in suggest)):
        suggest.append(NL2SQL_HINT)

    audit.record(db, action="chat.message", actor_id=principal.subject, actor_role=role,
                 resource="chat",
                 detail={"session_id": body.session_id, "agent": intent.agent,
                         "intent": intent.intent, "confidence": intent.confidence,
                         "degraded": result.degraded},
                 ip=client_ip(request))
    db.commit()                                   # 顺带提交 mock 编排写入的工单/申请单

    return ok({
        "answer": result.answer,
        "agent": intent.agent,
        "intent": intent.intent,
        "confidence": intent.confidence,
        "references": result.references,
        "suggest_actions": suggest,
        "conversation_id": result.conversation_id,
        "degraded": result.degraded,
    })


@router.post("/chat/stream", summary="流式对话（SSE）")
async def chat_stream(body: ChatRequest, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)) -> StreamingResponse:
    role = principal.role
    user = f"{settings.dify_user_prefix}-{principal.subject}"
    # 流式与普通消息同一个限流桶（按账号计），防绕过 /chat/message 直接刷 /chat/stream
    if settings.rate_limit_enabled:
        ratelimit.enforce("chat", principal.subject, settings.rate_limit_chat_per_min)
    intent = await route(body.message, role, user)
    metrics.record_chat(intent.action, intent.intent, intent.confidence,
                        intent.route_source)

    async def event_stream():
        # 先把路由结果推给前端，便于边展示边渲染
        yield "data: %s\n\n" % json.dumps(
            {"event": "route", "agent": intent.agent, "intent": intent.intent,
             "confidence": intent.confidence}, ensure_ascii=False)
        try:
            if settings.is_mock:
                # 离线模式：先算出完整答复（可能含查库/落库），再切片模拟流式
                result = mock_agent.respond(intent, body.message, role, db,
                                            subject=principal.subject,
                                            ref_id=principal.ref_id)
                for piece in chunk_text(result.answer):
                    yield "data: %s\n\n" % json.dumps({"event": "message", "answer": piece},
                                                      ensure_ascii=False)
                yield "data: %s\n\n" % json.dumps(
                    {"event": "message_end", "conversation_id": result.conversation_id,
                     "references": result.references}, ensure_ascii=False)
                db.commit()                        # 提交 mock 编排写入的业务记录
            else:
                async for chunk in dify_client.stream_chat(
                    intent.agent, body.message, user,
                    conversation_id=body.conversation_id,
                    inputs=_dify_inputs(body, principal),
                ):
                    yield "data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("流式对话异常: %s", exc)
            yield "data: %s\n\n" % json.dumps({"event": "error", "message": "生成中断，请重试"},
                                              ensure_ascii=False)
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/chat/asr", summary="语音录入：音频转写 + 槽位抽取")
async def asr_transcribe(
    request: Request,
    file: UploadFile = File(..., description="音频文件（wav/mp3/m4a）"),
    purpose: str = Form("daily_report", description="daily_report | leave_apply | chat"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    content = await file.read()
    if not content:
        raise AppError("上传的音频为空")
    if len(content) > settings.asr_max_bytes:
        raise AppError(f"音频超过上限 {settings.asr_max_bytes // 1024 // 1024} MB")

    provider = get_asr_provider()
    result = await provider.transcribe(file.filename or "audio.wav", content, purpose,
                                       f"{settings.dify_user_prefix}-{principal.subject}")

    audit.record(db, action="chat.asr", actor_id=principal.subject, actor_role=principal.role,
                 resource="asr", detail={"purpose": purpose, "provider": result.provider,
                                         "bytes": len(content)},
                 ip=client_ip(request))
    db.commit()

    return ok({"transcript": result.transcript, "provider": result.provider,
               "duration_ms": result.duration_ms, "structured": result.structured})
