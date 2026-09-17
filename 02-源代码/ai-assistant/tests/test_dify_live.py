"""live 模式（真连 Dify）的行为契约。

这一组的核心不是「调通」，而是**调不通时不许装作调通了**：

live 模式有两条容易骗过运维的路径，都必须锁死：
1. Key 没配 → 报错信息要直接说缺哪个应用、怎么补，而不是一句「未配置 API Key」；
2. Key/地址配错 → 请求失败会降级到 mock（这是「不出现无响应」的设计），
   但**必须把降级原因带出来**（`degraded` + `metadata.degraded_reason`），
   否则用户拿到 mock 回答会以为「已经接上 Dify 了」。

另外锁住 `/health`、`/ready` 能把「配了一半」如实暴露出来。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

import httpx
import pytest

from app.config import DIFY_APP_NAMES, settings
from app.core import UpstreamError
from app.services.dify import DifyClient


def _run(coro):
    return asyncio.run(coro)


def _client(handler: Callable[[httpx.Request], httpx.Response],
            mode: str = "live") -> DifyClient:
    return DifyClient(mode=mode, base_url="http://dify.test/v1",
                      transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 1. 请求成功：走真实 Dify 响应，不降级
# --------------------------------------------------------------------------- #
def test_live_chat_uses_dify_answer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-real"}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer app-real"
        assert request.url.path.endswith("/chat-messages")
        payload = json.loads(request.content)
        assert payload["query"] == "你们有几个校区"
        return httpx.Response(200, json={
            "answer": "来自 Dify 的真实回答",
            "conversation_id": "conv-real",
            "message_id": "msg-real",
            "metadata": {"retriever_resources": [{"doc": "校区分布"}]},
        })

    result = _run(_client(handler).chat("router", "你们有几个校区", "uas-admin"))

    assert result.answer == "来自 Dify 的真实回答"
    assert result.conversation_id == "conv-real"
    assert result.degraded is False
    assert result.references == [{"doc": "校区分布"}]


# --------------------------------------------------------------------------- #
# 2. Key 没配：报错要说清「缺哪个、怎么补」
# --------------------------------------------------------------------------- #
def test_missing_key_error_is_actionable(monkeypatch) -> None:
    """`_key()` 是错误信息的出口：要说清缺哪个应用、去哪个配置项补、怎么验证。"""
    monkeypatch.setattr(settings, "dify_mode", "live", raising=False)
    monkeypatch.setattr(settings, "dify_app_keys", {}, raising=False)

    client = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(UpstreamError) as exc:
        client._key("screener")

    message = exc.value.message
    assert "screener" in message
    assert "DIFY_APP_KEYS" in message, "要告诉运维去哪个配置项补"
    assert "check_dify.py" in message, "要给出验证手段"
    assert len(settings.dify_missing_keys) == len(DIFY_APP_NAMES)


def test_missing_key_does_not_blow_up_the_request(monkeypatch) -> None:
    """Key 没配也不能变成 500：降级给 mock 回答，并把原因带到 metadata 里。"""
    monkeypatch.setattr(settings, "dify_app_keys", {}, raising=False)

    result = _run(_client(lambda r: httpx.Response(200, json={}))
                  .chat("customer_service", "你们在成都有几个校区？", "uas-visitor"))

    assert result.degraded is True
    reason = result.metadata.get("degraded_reason", "")
    assert "UpstreamError" in reason
    assert "customer_service" in reason, "降级原因里要能看出是哪个应用没配好"
    assert result.answer, "降级后仍必须有回答"


# --------------------------------------------------------------------------- #
# 3. 连不上 / Key 错：降级必须可见
# --------------------------------------------------------------------------- #
def test_connect_failure_degrades_with_visible_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-x"}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _run(_client(handler).chat("router", "转人工", "uas-admin"))

    assert result.degraded is True
    assert "ConnectError" in result.metadata["degraded_reason"]
    assert result.metadata.get("mode") == "mock", "降级后走的是 mock 语料"


def test_unauthorized_is_reported_as_degraded(monkeypatch) -> None:
    """401 是最常见的「Key 复制错」，同样要被标出来而不是静默吞掉。"""
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-wrong"}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthorized", "message": "Access token invalid"})

    result = _run(_client(handler).chat("router", "转人工", "uas-admin"))

    assert result.degraded is True
    assert "401" in result.metadata["degraded_reason"]


# --------------------------------------------------------------------------- #
# 4. 流式路径：以前会让异常直接冒出去，现在也要降级
# --------------------------------------------------------------------------- #
def test_stream_degrades_when_nothing_emitted(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-x"}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def collect():
        return [ev async for ev in _client(handler).stream_chat("router", "转人工", "u")]

    events = _run(collect())

    assert events, "不能一个事件都没有"
    assert events[-1]["event"] == "message_end"
    assert events[-1]["degraded"] is True
    assert any(ev["event"] == "message" and ev.get("answer") for ev in events), \
        "一条都没吐出来时，应该用 mock 回答补齐"


def test_stream_passes_through_live_events(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-x"}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"event":"message","answer":"你"}\n\n'
            'data: {"event":"message","answer":"好"}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body,
                              headers={"Content-Type": "text/event-stream"})

    async def collect():
        return [ev async for ev in _client(handler).stream_chat("router", "hi", "u")]

    events = _run(collect())
    joined = "".join(ev.get("answer", "") for ev in events)
    assert joined == "你好"
    assert not any(ev.get("degraded") for ev in events)


def test_live_client_bypasses_proxy_for_local_target(monkeypatch) -> None:
    """本机 Dify 必须绕开系统代理，否则请求会被代理接走（实测 502）。"""
    monkeypatch.setattr(settings, "dify_trust_env_proxy", None, raising=False)
    client = DifyClient(mode="live", base_url="http://127.0.0.1/v1")
    async_client = client._client("app-x")
    try:
        assert async_client.trust_env is False
    finally:
        _run(async_client.aclose())


def test_live_client_keeps_proxy_for_public_target(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_trust_env_proxy", None, raising=False)
    client = DifyClient(mode="live", base_url="https://api.dify.ai/v1")
    async_client = client._client("app-x")
    try:
        assert async_client.trust_env is True
    finally:
        _run(async_client.aclose())


# --------------------------------------------------------------------------- #
# 5. 探针要如实反映 live 配置状态
# --------------------------------------------------------------------------- #
def test_health_exposes_dify_wiring(monkeypatch, client) -> None:
    # 显式清空 Key，避免测试结果被开发机 .env 里已填的真实 Key 干扰。
    # 契约本身与「Key 有没有配」无关：mock 下 Key 不是必需品。
    monkeypatch.setattr(settings, "dify_app_keys", {}, raising=False)

    body = client.get("/api/v1/health").json()["data"]
    assert body["dify_mode"] == "mock"           # 测试环境是 mock（conftest 设定）
    assert body["dify_live"] is False
    assert body["dify_missing_keys"] == []
    assert body["dify_key_count"] == 0          # mock 下不要求 Key
    # mock 是有完整本地编排层的受支持形态，不是降级状态 → 仍然是 ready
    assert body["dify_ready"] is True


def test_health_key_count_reflects_config_in_mock(monkeypatch, client) -> None:
    """mock 模式下若有 Key 残留，health 要如实报出个数。

    `dify_key_count` 是**配置态**诊断字段（数有多少个 Key 落到了配置里），
    不随模式归零；只有 `dify_missing_keys` 才受模式影响。
    """
    monkeypatch.setattr(
        settings, "dify_app_keys", {"router": "app-x", "screener": "app-y"}, raising=False
    )

    body = client.get("/api/v1/health").json()["data"]
    assert body["dify_mode"] == "mock"
    assert body["dify_key_count"] == 2
    assert body["dify_missing_keys"] == []       # mock 下不判定缺失
    assert body["dify_ready"] is True


def test_ready_reports_missing_dify_keys(monkeypatch, client) -> None:
    """live 但只配了一个 Key → ready 必须是 False，并列出还缺谁。"""
    monkeypatch.setattr(settings, "dify_mode", "live", raising=False)
    monkeypatch.setattr(settings, "dify_app_keys", {"router": "app-x"}, raising=False)

    body = client.get("/api/v1/ready").json()["data"]
    assert body["ready"] is False
    assert body["dify_live"] is True
    assert len(body["dify_missing_keys"]) == len(DIFY_APP_NAMES) - 1
    assert "router" not in body["dify_missing_keys"]

    health = client.get("/api/v1/health").json()["data"]
    assert health["dify_live"] is True
    assert health["dify_ready"] is False
    assert health["dify_key_count"] == 1


def test_ready_is_true_when_all_keys_present(monkeypatch, client) -> None:
    monkeypatch.setattr(settings, "dify_mode", "live", raising=False)
    monkeypatch.setattr(settings, "dify_app_keys",
                        {name: f"app-{i}" for i, name in enumerate(DIFY_APP_NAMES)},
                        raising=False)

    body = client.get("/api/v1/ready").json()["data"]
    assert body["ready"] is True
    assert body["dify_missing_keys"] == []


# --------------------------------------------------------------------------- #
# 6. 会话 ID 不属于本应用：丢掉重开会话，**不许降级**
#
# Dify 的 conversation_id 按应用隔离，而前端只存一个全局会话 ID。换一类问题
# （客服 → 学员助手）时会把上一个应用的会话递进来，Dify 回 404
# `Conversation Not Exists`。旧行为是把这个异常吞掉降级成 mock —— 用户拿到的
# 是一段与问题无关的套话，还以为是接上了 Dify。
# --------------------------------------------------------------------------- #
def test_conversation_missing_detector() -> None:
    """只认「会话不存在」这一种，别把普通 4xx/5xx 也当成要重开。"""
    from app.services.dify import _conversation_is_stale

    assert _conversation_is_stale(httpx.Response(404, json={
        "code": "not_found", "message": "Conversation Not Exists. ...", "status": 404}))
    assert _conversation_is_stale(httpx.Response(400, json={
        "errors": {"conversation_id": "Existing conversation ID x is not a valid uuid."}}))
    assert not _conversation_is_stale(httpx.Response(500, json={"message": "boom"}))
    assert not _conversation_is_stale(httpx.Response(404, json={"message": "not found"}))
    assert not _conversation_is_stale(httpx.Response(401, text="<html>401</html>"))


def _foreign_conv_handler(monkeypatch, calls, *, status: int, body: dict):
    """第一次带 conversation_id 就报「会话不存在」，不带则正常返回。"""
    monkeypatch.setattr(settings, "dify_app_keys", {"student_helper": "app-s"},
                        raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload.get("conversation_id"))
        if payload.get("conversation_id"):
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={
            "answer": "真实成绩", "conversation_id": "conv-new",
            "message_id": "m", "metadata": {}})

    return handler


def test_stale_conversation_id_is_retried_not_degraded(monkeypatch) -> None:
    calls: list = []
    handler = _foreign_conv_handler(monkeypatch, calls, status=404, body={
        "code": "not_found",
        "message": "Conversation Not Exists. You have requested this URI [...]",
        "status": 404})

    result = _run(_client(handler).chat("student_helper", "我雅思考了多少分", "u",
                                        conversation_id="conv-from-other-app"))

    assert calls == ["conv-from-other-app", None], "应当丢掉会话 ID 重开一次"
    assert result.degraded is False, "跨应用会话不该被降级成 mock"
    assert result.answer == "真实成绩"
    assert result.conversation_id == "conv-new"


def test_garbage_conversation_id_is_also_retried(monkeypatch) -> None:
    """畸形 id（400 not a valid uuid）同样重开，而不是降级。"""
    calls: list = []
    handler = _foreign_conv_handler(monkeypatch, calls, status=400, body={
        "errors": {"conversation_id": "Existing conversation ID x is not a valid uuid."},
        "message": "Input payload validation failed"})

    result = _run(_client(handler).chat("student_helper", "hi", "u",
                                        conversation_id="not-a-uuid"))

    assert calls == ["not-a-uuid", None]
    assert result.degraded is False


def test_other_errors_with_conversation_id_still_degrade(monkeypatch) -> None:
    """别把重试写成「任何错误都重试」：500 依旧降级，且只调一次。"""
    calls: list = []
    monkeypatch.setattr(settings, "dify_app_keys", {"student_helper": "app-s"},
                        raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content).get("conversation_id"))
        return httpx.Response(500, json={"message": "boom"})

    result = _run(_client(handler).chat("student_helper", "hi", "u",
                                        conversation_id="conv-1"))

    assert calls == ["conv-1"], "非会话类错误不该重试"
    assert result.degraded is True
    assert result.metadata.get("degraded") is True


def test_stream_stale_conversation_id_is_retried_not_degraded(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(settings, "dify_app_keys", {"student_helper": "app-s"},
                        raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload.get("conversation_id"))
        if payload.get("conversation_id"):
            return httpx.Response(404, json={"code": "not_found",
                                             "message": "Conversation Not Exists.",
                                             "status": 404})
        stream = ('data: {"event":"message","answer":"真实"}\n\n'
                  'data: {"event":"message_end","conversation_id":"conv-new",'
                  '"references":[]}\n\n'
                  "data: [DONE]\n\n")
        return httpx.Response(200, text=stream,
                              headers={"Content-Type": "text/event-stream"})

    async def collect():
        return [ev async for ev in _client(handler).stream_chat(
            "student_helper", "我雅思考了多少分", "u",
            conversation_id="conv-from-other-app")]

    events = _run(collect())

    assert calls == ["conv-from-other-app", None]
    assert not any(ev.get("degraded") for ev in events), "跨应用会话不该降级"
    assert "".join(ev.get("answer", "") for ev in events) == "真实"

