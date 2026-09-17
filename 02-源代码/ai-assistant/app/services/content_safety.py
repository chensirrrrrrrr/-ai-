# -*- coding: utf-8 -*-
"""内容安全过滤：LLM 入口的最后一道闸。

两件事
------
1. **PII 打码（放行）**：用户消息里常见的身份证 / 银行卡 / 手机号，
   打码后才喂给 Dify / 落审计日志 —— 大模型与日志系统都不该拿到可还原的 PII；
2. **违禁内容（拦截）**：命中黑名单的消息不进模型、不进工具，直接礼貌拒绝
   （返回固定话术 + 审计留痕），而不是把问题转述给 LLM 再指望它自己拒答。

口径
----
- 打码只影响**模型输入与日志**，不影响业务库 —— 业务数据该是啥还是啥；
- 黑名单刻意保持极小（演示口径）：真实生产应接内容安全服务（如腾讯云 TMS），
  本模块的定位是「没有外部服务时也有兜底」，接口留好了（`screen()` 返回 findings）。
"""
from __future__ import annotations

import re
from typing import List, Tuple

# 顺序有讲究：先长号段（身份证 18 位），再银行卡（16-19 位），最后手机号（11 位）
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 违禁黑名单（演示口径，刻意收得很窄，避免误伤正常业务咨询）
BLOCKED_WORDS: Tuple[str, ...] = ("枪支", "毒品", "洗钱", "代开发票", "爆炸物", "淫秽")


def _mask(value: str, keep_head: int, keep_tail: int) -> str:
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return f"{value[:keep_head]}{'*' * (len(value) - keep_head - keep_tail)}{value[-keep_tail:] if keep_tail else ''}"


def sanitize(text: str) -> Tuple[str, List[str]]:
    """打码 PII。返回 `(打码后文本, 命中的处理类型列表)`。"""
    findings: List[str] = []
    out = _ID_RE.sub(lambda m: (_mask(m.group(0), 6, 4), findings.append("id_card"))[0], text)
    out = _BANK_RE.sub(lambda m: (_mask(m.group(0), 0, 4), findings.append("bank_card"))[0], out)
    out = _PHONE_RE.sub(lambda m: (_mask(m.group(0), 3, 4), findings.append("phone"))[0], out)
    return out, findings


def is_blocked(text: str) -> bool:
    """是否命中违禁黑名单。"""
    return any(word in text for word in BLOCKED_WORDS)


def screen(text: str) -> Tuple[str, List[str], bool]:
    """一步到位：`(打码后文本, findings, 是否拦截)`。"""
    if is_blocked(text):
        return text, ["blocked"], True
    clean, findings = sanitize(text)
    return clean, findings, False


GUARD_REPLY = ("这条消息包含敏感内容，我无法处理。如果是业务咨询（申请进度、请假、"
               "费用、投诉等），换个说法告诉我，我马上帮你查。")
