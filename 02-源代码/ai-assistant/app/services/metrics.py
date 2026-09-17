# -*- coding: utf-8 -*-
"""进程内轻量监控指标（纯标准库，随进程存续，重启即清零）。

为什么自己做而不是接 Prometheus
--------------------------------
本项目是单进程 FastAPI（uvicorn 一发一收），监控诉求是「运维/答辩时一眼看出
系统活得好不好」：请求量、错误率、对话意图命中情况、p95 延迟。一个线程安全的
Counter + 环形缓冲就够了 —— 引入 Prometheus + grafana 是部署形态的事，
不该把可观测性的门槛抬到「必须先搭一套监控系统」。

口径说明（重要）
----------------
- **dispatch_rate（可执行率）**：意图路由给出的结果里，能直接派发 Agent 执行的
  占比。剩下的要么反问澄清（clarify），要么兜底（fallback）—— 这俩占比越低，
  说明「用户说的话」和「系统能力」对得越齐，是意图准确度的**代理指标**
  （没有人工标注，测不了真准确率，但两者强相关）。
- **p95 延迟**：环形缓冲只留最近 N 条，是「近期表现」不是「历史累计」。
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter, deque
from datetime import datetime
from typing import Any, Dict, Tuple

_LOCK = threading.Lock()
_STARTED_AT = time.time()

# 路径里的数字段折叠成 :id，否则 Counter 的 key 会随业务数据无限增长
_PATH_ID_RE = re.compile(r"/\d+(?=/|$)")

_REQUESTS_TOTAL: Counter = Counter()
_REQUEST_ERRORS: Counter = Counter()
_LATENCIES: deque = deque(maxlen=2000)          # (method, path, cost_ms)

_CHAT_TOTAL = 0
_CHAT_DECISIONS: Counter = Counter()            # dispatch / clarify / fallback
_CHAT_ROUTE_SOURCES: Counter = Counter()        # prefilter / dify / fallback
_CHAT_INTENTS: Counter = Counter()
_CHAT_CONFIDENCE_SUM = 0.0
_CHAT_LATENCIES: deque = deque(maxlen=1000)

_RATE_LIMIT_REJECTED: Counter = Counter()       # scope -> n


def record_request(method: str, path: str, status: int, cost_ms: float) -> None:
    """HTTP 中间件每请求调一次。"""
    key = f"{method} {_PATH_ID_RE.sub('/:id', path)}"
    with _LOCK:
        _REQUESTS_TOTAL[key] += 1
        if status >= 400:
            _REQUEST_ERRORS[key] += 1
        _LATENCIES.append((method, path, cost_ms))


def record_chat(decision: str, intent: str, confidence: float,
                route_source: str) -> None:
    """对话入口每次路由完成后调一次。"""
    global _CHAT_TOTAL, _CHAT_CONFIDENCE_SUM
    with _LOCK:
        _CHAT_TOTAL += 1
        _CHAT_CONFIDENCE_SUM += float(confidence or 0.0)
        _CHAT_DECISIONS[decision] += 1
        _CHAT_ROUTE_SOURCES[route_source] += 1
        _CHAT_INTENTS[intent] += 1


def record_chat_latency(cost_ms: float) -> None:
    with _LOCK:
        _CHAT_LATENCIES.append(cost_ms)


def record_rejected(scope: str) -> None:
    """限流拒绝一次（ scope：auth / chat …）。"""
    with _LOCK:
        _RATE_LIMIT_REJECTED[scope] += 1


def _pctl(sorted_values, p: int) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(p / 100 * (len(sorted_values) - 1))))
    return round(float(sorted_values[index]), 1)


def _rates(total: int, hit: int) -> Any:
    return round(hit / total, 3) if total else None


def snapshot() -> Dict[str, Any]:
    """当前指标快照（/health 的 metrics 字段）。"""
    from ..config import settings            # 局部导入避免循环依赖

    with _LOCK:
        lat = sorted(cost for _, _, cost in _LATENCIES)
        chat_lat = sorted(_CHAT_LATENCIES)
        decisions = dict(_CHAT_DECISIONS)
        total = _CHAT_TOTAL
        payload = {
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "started_at": datetime.fromtimestamp(_STARTED_AT).isoformat(timespec="seconds"),
            "window_sizes": {"requests": len(_LATENCIES), "chat": len(chat_lat)},
            "requests": {
                "total": sum(_REQUESTS_TOTAL.values()),
                "errors": sum(_REQUEST_ERRORS.values()),
                "p50_ms": _pctl(lat, 50),
                "p95_ms": _pctl(lat, 95),
            },
            "chat": {
                "total": total,
                "dispatch": decisions.get("dispatch", 0),
                "clarify": decisions.get("clarify", 0),
                "fallback": decisions.get("fallback", 0),
                "dispatch_rate": _rates(total, decisions.get("dispatch", 0)),
                "clarify_rate": _rates(total, decisions.get("clarify", 0)),
                "fallback_rate": _rates(total, decisions.get("fallback", 0)),
                "avg_confidence": round(_CHAT_CONFIDENCE_SUM / total, 3) if total else None,
                "p50_ms": _pctl(chat_lat, 50),
                "p95_ms": _pctl(chat_lat, 95),
                "route_source": dict(_CHAT_ROUTE_SOURCES),
                "top_intents": _CHAT_INTENTS.most_common(5),
            },
            "rate_limit": {
                "enabled": settings.rate_limit_enabled,
                "auth_per_min": settings.rate_limit_auth_per_min,
                "chat_per_min": settings.rate_limit_chat_per_min,
                "rejected_total": sum(_RATE_LIMIT_REJECTED.values()),
                "rejected_by_scope": dict(_RATE_LIMIT_REJECTED),
            },
        }
    return payload


def reset_for_tests() -> None:
    """清空全部计数（仅测试用）。"""
    global _STARTED_AT, _CHAT_TOTAL, _CHAT_CONFIDENCE_SUM
    with _LOCK:
        _STARTED_AT = time.time()
        _CHAT_TOTAL = 0
        _CHAT_CONFIDENCE_SUM = 0.0
        _REQUESTS_TOTAL.clear()
        _REQUEST_ERRORS.clear()
        _LATENCIES.clear()
        _CHAT_DECISIONS.clear()
        _CHAT_ROUTE_SOURCES.clear()
        _CHAT_INTENTS.clear()
        _CHAT_LATENCIES.clear()
        _RATE_LIMIT_REJECTED.clear()
