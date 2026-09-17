# -*- coding: utf-8 -*-
"""进程内滑动窗口限流（纯标准库，无 slowapi / redis 依赖）。

为什么不用 slowapi
------------------
slowapi 的默认后端是进程内内存，单进程部署下和这里自建的滑窗没有本质区别，
却多引入一个依赖和一套装饰器语法。本项目对限流的诉求只有两条：
**登录防爆破**、**对话防刷 LLM** —— 60 行标准库就能覆盖，且阈值/开关都进配置。

局限（写清楚，别装看不见）
--------------------------
- **单进程语义**：多 worker / 多机部署时要换成 Redis 计数（接口不变，换实现即可）；
- 计数在内存里，进程重启即清零 —— 对「防爆破」来说宁可偏松也不误伤。
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

from ..core import TooManyRequests

_LOCK = threading.Lock()
_HITS: Dict[Tuple[str, str], List[float]] = {}


def allow(scope: str, key: str, limit: int, window_seconds: float = 60.0,
          now: Optional[float] = None) -> Tuple[bool, int, float]:
    """滑动窗口判定。

    返回 `(是否放行, 窗口内已计数, 建议重试秒数)`。
    `now` 参数供测试注入时间（真实调用传 None，用 monotonic 时钟）。
    """
    if limit <= 0:                              # 0 / 负数 = 不限流
        return True, 0, 0.0
    ts = time.monotonic() if now is None else now
    bucket_key = (scope, key)
    with _LOCK:
        window = [t for t in _HITS.get(bucket_key, []) if ts - t < window_seconds]
        if len(window) >= limit:
            # 最老的一条出窗后才腾出名额 —— 别让客户端空转，告诉它等多久
            retry_after = window_seconds - (ts - window[0])
            _HITS[bucket_key] = window
            return False, len(window), max(retry_after, 0.0)
        window.append(ts)
        _HITS[bucket_key] = window
        return True, len(window), 0.0


def enforce(scope: str, key: str, limit: int, window_seconds: float = 60.0) -> None:
    """限流 + 超限直接抛 429（带 data.retry_after），供接口层一行调用。"""
    allowed, _, retry_after = allow(scope, key, limit, window_seconds)
    if not allowed:
        from . import metrics                   # 局部导入避免循环依赖
        metrics.record_rejected(scope)
        raise TooManyRequests(f"请求过于频繁，请 {max(retry_after, 0.5):.0f} 秒后再试",
                              retry_after=max(int(max(retry_after, 0.5)), 1))


def clear() -> None:
    """清空计数（仅测试用）。"""
    with _LOCK:
        _HITS.clear()
