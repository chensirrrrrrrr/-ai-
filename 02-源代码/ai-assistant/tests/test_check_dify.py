"""`scripts/check_dify.py` 的判定逻辑测试。

这个脚本的价值全在「判定准不准」：它得能区分
    Key 无效（401） / 连不上 / base_url 少了 /v1（404）
    / workflow 类应用没有 /parameters（404 但其实是正常的）
/ 真的通过（200）。

探针用 httpx.MockTransport 注入，测逻辑不测网络。
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from app.config import DIFY_APP_NAMES, settings
from scripts.check_dify import (KEY_INVALID, MISSING, OK, PATH_WRONG, UNREACHABLE,
                                check_one)

Transport = httpx.BaseTransport


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> Transport:
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch):
    """默认给 7 个应用都配上假 Key；个别用例再覆盖。"""
    monkeypatch.setattr(settings, "dify_app_keys",
                        {name: f"app-{name}-0000000000" for name in DIFY_APP_NAMES},
                        raising=False)


def test_chat_app_returns_ok_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/parameters")
        return httpx.Response(200, json={"opening_statement": "hi"})

    result = check_one("customer_service", 5.0, transport=_transport(handler))
    assert result["verdict"] == OK


def test_invalid_key_is_detected() -> None:
    result = check_one("customer_service", 5.0,
                       transport=_transport(lambda r: httpx.Response(401, json={})))
    assert result["verdict"] == KEY_INVALID


def test_workflow_app_404_then_valid_400_is_ok() -> None:
    """/parameters 404 不代表配错：workflow 类应用本来就没有这个端点。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/parameters"):
            return httpx.Response(404, json={"code": "not_found"})
        assert request.url.path.endswith("/workflows/run")
        # 故意不合法（缺 user）→ Dify 给 400，说明路由存在 + 鉴权通过
        return httpx.Response(400, json={"code": "invalid_param"})

    result = check_one("screener", 5.0, transport=_transport(handler))
    assert result["verdict"] == OK
    assert "鉴权通过" in result["detail"]


def test_workflow_probe_does_not_run_the_workflow() -> None:
    """探针不能真跑工作流（会烧 token / 有副作用）：请求体必须是残缺的。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workflows/run"):
            seen.append(json.loads(request.content))
        return httpx.Response(404, json={})

    check_one("router", 5.0, transport=_transport(handler))
    assert seen, "应该发过一次 workflow 探测"
    assert "user" not in seen[0], "缺 user 才会被 Dify 判 400，从而不执行工作流"
    assert seen[0].get("inputs") is None, "不能传 inputs，避免工作流真的跑起来"


def test_wrong_base_url_shows_as_path_wrong() -> None:
    """DIFY_BASE_URL 少了 /v1 时全站 404，要给出这个结论。"""
    result = check_one("reporter", 5.0,
                       transport=_transport(lambda r: httpx.Response(404, json={})))
    assert result["verdict"] == PATH_WRONG


def test_connect_error_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = check_one("router", 5.0, transport=_transport(handler))
    assert result["verdict"] == UNREACHABLE
    assert "连不上" in result["detail"]


def test_missing_key_is_reported_before_any_network_call(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dify_app_keys", {}, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:      # pragma: no cover
        raise AssertionError("没配 Key 时不该发请求")

    result = check_one("mental_care", 5.0, transport=_transport(handler))
    assert result["verdict"] == MISSING


def test_key_is_masked_in_output(monkeypatch) -> None:
    """输出里不能出现完整 Key（脚本会被贴进群里/日志里）。"""
    secret = "app-abcdefghijklmnopqrstuvwxyz012345"
    monkeypatch.setattr(settings, "dify_app_keys", {"customer_service": secret},
                        raising=False)

    result = check_one("customer_service", 5.0,
                       transport=_transport(lambda r: httpx.Response(200, json={})))
    assert secret not in result["masked_key"]
    assert "…" in result["masked_key"]
    assert result["masked_key"].startswith("app-abc")
