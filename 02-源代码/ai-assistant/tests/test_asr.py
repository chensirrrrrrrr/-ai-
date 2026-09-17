"""语音录入（ASR）：Provider 选择、Dify 转写的三条分支、槽位抽取。

为什么补这一组：`asr.py` 的分支几乎全在「配置异常 / 上游异常」上 ——
- `ASR_PROVIDER` 写成不认识的厂商（`.env` 手滑）必须**显式拒绝**，不能静默退回 mock，
  否则线上会以为在用厂商 ASR，实际一直在跑确定性假文本；
- Dify 转写失败必须**包成 `UpstreamError`**（502），而 `UpstreamError` 本身要原样透传，
  不能被再包一层 —— 前端靠错误码区分「上游挂了」和「没想到的异常」。

不联网：`dify_client.audio_to_text` 全部被替掉。
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.core import AppError, UpstreamError
from app.services import asr


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Provider 选择
# --------------------------------------------------------------------------- #
def test_mock_is_the_default_provider(monkeypatch):
    monkeypatch.setattr(settings, "asr_provider", "mock", raising=False)
    provider = asr.get_asr_provider()
    assert isinstance(provider, asr.MockAsr)

    result = _run(provider.transcribe("a.wav", b"\x00" * 64, "chat", "u"))
    assert result.provider == "mock"
    assert result.transcript
    assert result.duration_ms and result.duration_ms >= 1


def test_mock_provider_falls_back_to_chat_canned_text():
    """未知 purpose 用 chat 的固定文本，保证结果可断言。"""
    result = _run(asr.MockAsr().transcribe("a.wav", b"\x00" * 32, "no_such_purpose", "u"))
    assert result.transcript == asr.MockAsr._CANNED["chat"]


def test_dify_provider_is_selected_case_insensitively(monkeypatch):
    monkeypatch.setattr(settings, "asr_provider", "Dify", raising=False)
    assert isinstance(asr.get_asr_provider(), asr.DifyAsr)


def test_unknown_provider_is_rejected_not_silently_degraded(monkeypatch):
    monkeypatch.setattr(settings, "asr_provider", "whisper", raising=False)
    with pytest.raises(AppError) as exc:
        asr.get_asr_provider()
    assert "whisper" in str(exc.value)


# --------------------------------------------------------------------------- #
# DifyAsr.transcribe：成功 / UpstreamError 透传 / 未知异常包装
# --------------------------------------------------------------------------- #
def test_dify_transcribe_returns_text_and_slots(monkeypatch):
    async def fake_audio(app, filename, content, user):
        assert filename == "a.wav"
        assert content == b"x" * 4
        return "托福考了 105 分"

    monkeypatch.setattr(asr.dify_client, "audio_to_text", fake_audio, raising=False)

    out = _run(asr.DifyAsr().transcribe("a.wav", b"x" * 4, "chat", "u"))
    assert out.provider == "dify"
    assert out.transcript == "托福考了 105 分"
    assert out.structured == {"exam": "TOEFL", "score": 105.0}


def test_leave_apply_routes_to_student_helper_app(monkeypatch):
    seen: dict = {}

    async def fake_audio(app, filename, content, user):
        seen["app"] = app
        return "我要请假"

    monkeypatch.setattr(asr.dify_client, "audio_to_text", fake_audio, raising=False)
    _run(asr.DifyAsr().transcribe("a.wav", b"x", "leave_apply", "u"))
    assert seen["app"] == asr.APP_STUDENT_HELPER


def test_other_purposes_route_to_enterprise_app(monkeypatch):
    seen: dict = {}

    async def fake_audio(app, filename, content, user):
        seen["app"] = app
        return "随便说点什么"

    monkeypatch.setattr(asr.dify_client, "audio_to_text", fake_audio, raising=False)
    _run(asr.DifyAsr().transcribe("a.wav", b"x", "daily_report", "u"))
    assert seen["app"] == asr.APP_ENTERPRISE


def test_upstream_error_is_passed_through_unchanged(monkeypatch):
    async def boom(app, filename, content, user):
        raise UpstreamError("上游 500")

    monkeypatch.setattr(asr.dify_client, "audio_to_text", boom, raising=False)
    with pytest.raises(UpstreamError) as exc:
        _run(asr.DifyAsr().transcribe("a.wav", b"x", "chat", "u"))
    assert "上游 500" in str(exc.value)


def test_unexpected_error_is_wrapped_into_upstream_error(monkeypatch):
    async def boom(app, filename, content, user):
        raise ValueError("boom")

    monkeypatch.setattr(asr.dify_client, "audio_to_text", boom, raising=False)
    with pytest.raises(UpstreamError) as exc:
        _run(asr.DifyAsr().transcribe("a.wav", b"x", "chat", "u"))
    assert "语音转写失败" in str(exc.value)
    assert "boom" in str(exc.value)


# --------------------------------------------------------------------------- #
# 槽位抽取（规则版）
# --------------------------------------------------------------------------- #
def test_extract_slots_pulls_every_known_field():
    slots = asr.extract_slots("我要请假 2026 年 9 月 15 日 2 天 原因看病，雅思 6.5 分")
    assert slots["start_date"] == "2026-09-15"
    assert slots["days"] == 2
    assert slots["reason"] == "看病"
    assert slots["score"] == 6.5
    assert slots["exam"] == "IELTS"


def test_extract_slots_toefl_branch():
    assert asr.extract_slots("托福考了 105 分") == {"score": 105.0, "exam": "TOEFL"}


def test_extract_slots_on_empty_text():
    assert asr.extract_slots("") == {}
