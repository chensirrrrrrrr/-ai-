"""语音录入（ASR）。

链路：前端录音 → `POST /api/v1/chat/asr`(multipart) → ASR Provider 转写
      → 结构化抽取（日期/天数/事由等）→ 返回文本 + slots → 前端回填表单或直接提交。

Provider 可插拔：
- `mock`：离线确定性转写，供本地开发与单测使用；
- `dify`：调用 Dify 应用的 `/audio-to-text`（Dify 侧再接 Whisper 等）；
- 其他厂商（阿里云/腾讯云）实现同一个 `AsrProvider` 协议即可接入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from ..config import settings
from ..core import AppError, UpstreamError
from .dify import APP_ENTERPRISE, APP_STUDENT_HELPER, dify_client


@dataclass
class AsrResult:
    transcript: str
    provider: str
    duration_ms: Optional[int] = None
    structured: Optional[Dict[str, Any]] = None


class AsrProvider(Protocol):
    name: str

    async def transcribe(self, filename: str, content: bytes,
                         purpose: str, user: str) -> AsrResult: ...


class MockAsr:
    """离线转写：按 purpose 返回固定文本，保证测试结果可断言。"""

    name = "mock"

    _CANNED = {
        "daily_report": (
            "今天上午陪张三做了澳国立和墨尔本的材料清单核对，"
            "下午跟进李四的雅思成绩，总分六点五，写作差零点五，"
            "约了明天上午十点复盘选校方案。"
        ),
        "leave_apply": "我要请假 2026 年 9 月 15 日 2 天 原因看病",
        "chat": "你们机构在成都的校区在哪",
    }

    async def transcribe(self, filename: str, content: bytes,
                         purpose: str, user: str) -> AsrResult:
        text = self._CANNED.get(purpose, self._CANNED["chat"])
        return AsrResult(
            transcript=text,
            provider=self.name,
            duration_ms=max(1, len(content) // 32),   # 粗略估算，便于返回非空
            structured=extract_slots(text),
        )


class DifyAsr:
    """通过 Dify 应用的音频转写能力（Dify 侧挂 Whisper / 第三方 ASR）。"""

    name = "dify"

    async def transcribe(self, filename: str, content: bytes,
                         purpose: str, user: str) -> AsrResult:
        app = APP_STUDENT_HELPER if purpose == "leave_apply" else APP_ENTERPRISE
        try:
            text = await dify_client.audio_to_text(app, filename, content, user)
        except UpstreamError:
            raise
        except Exception as exc:                        # noqa: BLE001
            raise UpstreamError(f"语音转写失败: {exc}") from exc
        return AsrResult(transcript=text, provider=self.name, structured=extract_slots(text))


def get_asr_provider() -> AsrProvider:
    provider = (settings.asr_provider or "mock").lower()
    if provider == "dify":
        return DifyAsr()
    if provider != "mock":
        raise AppError(f"不支持的 ASR_PROVIDER: {provider}")
    return MockAsr()


# --------------------------------------------------------------------------- #
# 轻量结构化抽取：把口述文本变成可用于填表的槽位
# 一期用规则（可解释、可控）；二期可换成 Dify 的「信息抽取」工作流
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DAY_RE = re.compile(r"(\d+)\s*天")
_REASON_HINTS = ("看病", "家里有事", "考试", "实习", "面试", "身体", "回家", "旅行")
_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")


def extract_slots(text: str) -> Dict[str, Any]:
    slots: Dict[str, Any] = {}
    if m := _DATE_RE.search(text):
        slots["start_date"] = "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := _DAY_RE.search(text):
        slots["days"] = int(m.group(1))
    if reason := next((h for h in _REASON_HINTS if h in text), None):
        slots["reason"] = reason
    if m := _SCORE_RE.search(text):
        slots["score"] = float(m.group(1))
    if "雅思" in text:
        slots["exam"] = "IELTS"
    if "托福" in text:
        slots["exam"] = "TOEFL"
    return slots
