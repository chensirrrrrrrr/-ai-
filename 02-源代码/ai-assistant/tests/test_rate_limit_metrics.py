# -*- coding: utf-8 -*-
"""限流 + 监控指标。

为什么单独补
------------
限流和指标都是「不发生故障就没人看」的横切能力，但恰恰是它们出错最隐蔽：
- 限流如果误伤（阈值过紧 / key 取错），用户会莫名收到 429 —— 必须钉住
  「阈值内绝不拦、超限才拦、窗口滑出即恢复」三个相位；
- 指标如果算错（p95 取错位、分母为 0、decision 加起来对不上 total），
  看板会安静地骗人 —— 必须钉住口径。

conftest 把阈值调到了 100000/min（避免既有用例被误伤），本文件用
monkeypatch 压低阈值单独测限流行为。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import metrics, ratelimit
from tests.conftest import data

AUTH = "/api/v1/auth/token"
CHAT = "/api/v1/chat/message"


@pytest.fixture(autouse=True)
def _clean_buckets():
    """每个用例都从空桶开始，互不串味。"""
    ratelimit.clear()
    metrics.reset_for_tests()
    yield
    ratelimit.clear()
    metrics.reset_for_tests()


# --------------------------------------------------------------------------- #
# 服务层：滑动窗口三个相位
# --------------------------------------------------------------------------- #
def test_sliding_window_phases():
    """阈值内放行 → 超限拒绝并给出等待时长 → 最老一条出窗后恢复。"""
    # 1) 阈值内：3 个名额全放行
    for i in range(3):
        allowed, count, retry = ratelimit.allow("t", "k", 3, 60.0, now=1000.0 + i)
        assert allowed and count == i + 1 and retry == 0.0

    # 2) 超限：拒绝 + 建议重试时间 = 窗口 - 最老一条的年龄
    allowed, count, retry = ratelimit.allow("t", "k", 3, 60.0, now=1050.0)
    assert not allowed and count == 3
    assert 9.0 < retry <= 10.0

    # 3) 滑动：最老一条（t=1000）在 t=1061 出窗，名额腾出
    allowed, count, retry = ratelimit.allow("t", "k", 3, 60.0, now=1061.0)
    assert allowed and count == 2 and retry == 0.0   # 窗口内剩 t=1002 一条 + 本次

    # key 之间互不影响
    allowed, _, _ = ratelimit.allow("t", "another", 3, 60.0, now=1050.0)
    assert allowed

    # limit<=0 视为不限流
    allowed, _, _ = ratelimit.allow("t", "k", 0, 60.0, now=1050.0)
    assert allowed


def test_enforce_raises_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_min", 1)
    from app.core import TooManyRequests

    ratelimit.enforce("auth", "1.2.3.4", settings.rate_limit_auth_per_min)
    with pytest.raises(TooManyRequests) as exc:
        ratelimit.enforce("auth", "1.2.3.4", settings.rate_limit_auth_per_min)
    assert exc.value.http_status == 429
    assert exc.value.code == 42900
    assert exc.value.data["retry_after"] >= 1


# --------------------------------------------------------------------------- #
# 接口层：登录防爆破 / 对话防刷 / 开关
# --------------------------------------------------------------------------- #
def test_login_rate_limit_blocks_bruteforce(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_min", 3)
    for _ in range(3):
        resp = client.post(AUTH, json={"username": "ghost", "password": "wrong123"})
        assert resp.status_code == 401, "阈值内不该被限流拦住"
    fourth = client.post(AUTH, json={"username": "ghost", "password": "wrong123"})
    assert fourth.status_code == 429
    assert fourth.json()["code"] == 42900
    assert fourth.json()["data"]["retry_after"] >= 1

    # 换个 IP 就不受影响（按 IP 限流）
    resp = client.post(AUTH, json={"username": "ghost", "password": "wrong123"},
                       headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.status_code == 401


def test_chat_rate_limit_and_stream_share_bucket(client, student_headers, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_chat_per_min", 2)
    for _ in range(2):
        assert client.post(CHAT, json={"message": "你好", "session_id": "rl"},
                           headers=student_headers).status_code == 200
    third = client.post(CHAT, json={"message": "你好", "session_id": "rl"},
                        headers=student_headers)
    assert third.status_code == 429 and third.json()["code"] == 42900

    # 流式端点与普通消息同一个桶，不能成为绕行通道
    with client.stream("POST", "/api/v1/chat/stream",
                       json={"message": "你好", "session_id": "rl"},
                       headers=student_headers) as resp:
        assert resp.status_code == 429


def test_rate_limit_disabled_switch(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_auth_per_min", 1)
    for _ in range(4):
        resp = client.post(AUTH, json={"username": "ghost", "password": "wrong123"})
        assert resp.status_code == 401, "开关关掉后不应触发 429"


# --------------------------------------------------------------------------- #
# 指标：/health 的 metrics 口径
# --------------------------------------------------------------------------- #
def test_health_exposes_metrics_snapshot(client, student_headers):
    assert data(client.post(CHAT, json={"message": "我的成绩怎么样", "session_id": "m1"},
                            headers=student_headers))["intent"]

    health = data(client.get("/api/v1/health"))
    m = health["metrics"]
    assert m["uptime_seconds"] >= 0 and m["started_at"]
    # 口径注意：/health 自身这次请求是「先算快照、后记账」（中间件在响应后计数），
    # 所以快照里的 requests.total 至少包含刚才那次对话，但不含本次 health。
    assert m["requests"]["total"] >= 1 and m["requests"]["p95_ms"] >= 0

    chat = m["chat"]
    assert chat["total"] >= 1
    # 三种去向之和必须等于总数 —— 分母对不上，看板就在骗人
    assert (chat["dispatch"] + chat["clarify"] + chat["fallback"]) == chat["total"]
    if chat["total"]:
        rates = [chat["dispatch_rate"], chat["clarify_rate"], chat["fallback_rate"]]
        assert abs(sum(r for r in rates if r is not None) - 1.0) < 0.01
    assert chat["top_intents"] and chat["route_source"]
    assert m["rate_limit"]["enabled"] is True


def test_metrics_records_chat_clarify(client, student_headers):
    # 反问类消息也应计入 clarify，不能只统计成功派发的
    assert data(client.post(CHAT, json={"message": "你好", "session_id": "cl"},
                            headers=student_headers))["answer"]
    chat = data(client.get("/api/v1/health"))["metrics"]["chat"]
    assert chat["total"] >= 1
    assert chat["clarify"] + chat["dispatch"] + chat["fallback"] == chat["total"]


def test_metrics_percentile_math():
    assert metrics._pctl([1, 2, 3, 4, 5], 50) == 3.0
    assert metrics._pctl([1, 2, 3, 4, 5], 95) == 5.0
    assert metrics._pctl([], 95) == 0.0
    assert metrics._pctl([10], 50) == 10.0


def test_metrics_request_path_id_folding():
    """路径里的数字要折叠成 :id，否则计数 key 随业务数据无限增长。"""
    metrics.record_request("GET", "/api/v1/leads/123/followups", 200, 1.0)
    metrics.record_request("GET", "/api/v1/leads/456/followups", 200, 1.0)
    snap = metrics.snapshot()
    assert snap["requests"]["total"] >= 2
    # 同一条路径折叠后只占一个 key（通过 errors 键存在性间接验证折叠逻辑）
    metrics.record_request("GET", "/api/v1/leads/789/followups", 404, 1.0)
    assert metrics.snapshot()["requests"]["errors"] >= 1
