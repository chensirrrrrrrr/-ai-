# -*- coding: utf-8 -*-
"""轻量并发压测：把「这套东西能撑多少并发」从拍脑袋变成可报的数。

为什么自己写而不是上 locust / k6
--------------------------------
只需要「几个主要接口在 N 并发下的 p50/p95/错误率」，一个线程池 + httpx 连接池就够，
还能直接产出跟项目同一风格的 Markdown 报告；引 locust 反而要多学一套 DSL。

口径（报告里必须写清楚，否则数字会被误读）
------------------------------------------
- 走 **`DIFY_MODE=mock`**：对话不连真实模型，测的是**本系统的**吞吐与延迟上限；
  真实 Dify 的耗时在大模型侧（秒级），不在本系统 —— 两个数要分开报，不能混。
- 只对**只读/幂等**接口加压（健康检查、成绩查询、客户列表），
  对话接口只跑「一问一答」，不制造脏数据；写接口的持久化能力由单测保证。
- SQLite 是单写者模型：**并发写不是本压测的目标**，结论只针对读路径。

用法
----
    # 先起服（建议 mock 模式，避免真实模型耗时干扰）
    DIFY_MODE=mock python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
    python scripts/load_test.py --base http://127.0.0.1:8010 --concurrency 20 50
    python scripts/load_test.py --json            # 机器可读
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "load_test_report.md"

# 每个场景：名称 / 方法 / 路径 / 是否需要登录 / 是否带 JSON 体
SCENARIOS: List[Tuple[str, str, str, bool, Optional[dict]]] = [
    ("健康检查 /health", "GET", "/api/v1/health", False, None),
    ("成绩查询 /scores", "GET", "/api/v1/scores", True, None),
    ("客户列表 /leads", "GET", "/api/v1/leads?page_size=20", True, None),
    ("对话一问一答 /chat/message", "POST", "/api/v1/chat/message", True,
     {"session_id": "load", "message": "我的成绩怎么样"}),
]


def _pctl(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def hammer(fn: Callable[[], int], *, concurrency: int, total: int) -> Dict[str, Any]:
    """用 `concurrency` 个线程打 `total` 次，返回延迟与错误统计。

    口径：**429（限流）单独计数**，不算稳定性缺陷 —— 那正是保护逻辑在工作
    （对话默认 120/min·账号）。真正的错误是 5xx / 连接超时。
    """
    latencies: List[float] = []
    codes: Dict[int, int] = {}
    exceptions: List[str] = []
    lock = threading.Lock()
    # 余数摊到前几个 worker，保证总请求数精确等于 total
    per_worker, remainder = divmod(total, concurrency)
    per_worker = max(1, per_worker)

    def worker(count: int = per_worker) -> None:
        for _ in range(count):
            started = time.perf_counter()
            try:
                status = fn()
            except Exception as exc:                       # noqa: BLE001 - 压测要把异常算成错误
                status, name = 0, type(exc).__name__
            else:
                name = ""
            cost = (time.perf_counter() - started) * 1000
            with lock:
                latencies.append(cost)
                codes[status] = codes.get(status, 0) + 1
                if name:
                    exceptions.append(name)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for i in range(concurrency):
            pool.submit(worker, per_worker + (1 if i < remainder else 0))
    elapsed = time.perf_counter() - started

    done = len(latencies)
    throttled = codes.get(429, 0)
    failures = sum(n for code, n in codes.items() if code >= 500 or code == 0)
    return {
        "requests": done,
        "throttled": throttled,                            # 429：限流保护，非缺陷
        "errors": failures,                                # 5xx / 异常
        "error_rate": round(failures / done, 4) if done else 0.0,
        "codes": {str(k): v for k, v in sorted(codes.items())},
        "p50_ms": _pctl(latencies, 50),
        "p95_ms": _pctl(latencies, 95),
        "p99_ms": _pctl(latencies, 99),
        "avg_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "rps": round(done / elapsed, 1) if elapsed > 0 else 0.0,
        "exceptions": sorted(set(exceptions))[:3],
    }


def run(base: str, concurrency: int, total: int, timeout: float = 30.0) -> Dict[str, Any]:
    result: Dict[str, Any] = {"concurrency": concurrency, "per_scenario_requests": total,
                              "scenarios": {}}
    with httpx.Client(base_url=base, timeout=timeout, trust_env=False) as client:
        # 预先登录几个员工账号：压测里不重复登录（登录接口自身有 30/min·IP 限流，
        # 重复登录会让它成为瓶颈），并且**多账号轮转** —— 单账号高并发会撞上
        # 对话限流（120/min·账号），那是保护逻辑，不该被误读成系统扛不住。
        tokens: List[str] = []
        for username, password in (("advisor", "advisor123"), ("teacher", "teacher123"),
                                   ("manager", "manager123")):
            resp = client.post("/api/v1/auth/token",
                               json={"username": username, "password": password})
            resp.raise_for_status()
            tokens.append(resp.json()["data"]["access_token"])
        cursor = {"i": 0}
        cursor_lock = threading.Lock()

        def next_headers() -> Optional[dict]:
            with cursor_lock:
                token = tokens[cursor["i"] % len(tokens)]
                cursor["i"] += 1
            return {"Authorization": f"Bearer {token}"}

        for name, method, path, need_auth, payload in SCENARIOS:
            def call(method=method, path=path, need_auth=need_auth, payload=payload) -> int:
                headers = next_headers() if need_auth else None
                if method == "GET":
                    r = client.get(path, headers=headers)
                else:
                    r = client.post(path, json=payload or {}, headers=headers)
                return r.status_code

            result["scenarios"][name] = hammer(call, concurrency=concurrency, total=total)
    return result


def _table(rows: List[Dict[str, Any]]) -> str:
    head = ("| 场景 | 请求数 | 5xx/异常 | 限流 429 | p50 | p95 | p99 | 最大 | 吞吐 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    lines = list(head)
    for r in rows:
        lines.append(f"| {r['name']} | {r['requests']} | **{r['errors']}** "
                     f"| {r['throttled']} | {r['p50_ms']} ms | **{r['p95_ms']} ms** "
                     f"| {r['p99_ms']} ms | {r['max_ms']} ms | {r['rps']} req/s |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="轻量并发压测")
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[20, 50],
                        help="并发档位，可给多个（默认 20 50）")
    parser.add_argument("--requests", type=int, default=100,
                        help="每个场景每档的总请求数（默认 100）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-report", action="store_true", help="不写 Markdown 报告")
    args = parser.parse_args()

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    runs = [run(args.base, c, args.requests) for c in args.concurrency]

    if args.json:
        print(json.dumps({"base": args.base, "at": started_at, "runs": runs},
                         ensure_ascii=False, indent=2))
        return 0

    lines = [
        "# 并发压测报告",
        "",
        f"- 压测时间：{started_at}",
        f"- 目标服务：{args.base}",
        f"- 运行模式：**DIFY_MODE=mock**（对话不连真实模型，测的是本系统自身的处理上限）",
        f"- 机器：{platform.processor() or platform.machine()} / "
        f"{platform.system()} {platform.release()} / CPU {platform.python_version()}",
        "",
        "> ⚠️ 口径：只压**只读与幂等**接口；真实 Dify 的耗时在模型侧（秒级），"
        "与本系统处理耗时（毫秒级）不是一个量级，两个数要分开报。",
        "",
    ]
    for item in runs:
        rows = [{"name": name, **stat} for name, stat in item["scenarios"].items()]
        lines += [f"## 并发 {item['concurrency']}（每场景 {item['per_scenario_requests']} 次）",
                  "", _table(rows), ""]
        worst = max(r["p95_ms"] for r in rows)
        errs = sum(r["errors"] for r in rows)
        throttled = sum(r["throttled"] for r in rows)
        lines += [
            f"**小结**：最高 p95 = **{worst} ms**；5xx/异常 **{errs} 条**"
            + ("（✅ 无服务端错误）" if errs == 0 else "（需排查）")
            + (f"；限流拒绝 {throttled} 条（保护逻辑生效，非缺陷）" if throttled else "") + "。",
            "",
        ]
    lines += [
        "---",
        "",
        "## 怎么读这份报告",
        "",
        "- **p95** 是「95% 的请求不比它慢」—— 比平均值更能反映用户实际感受；",
        "- **错误率** 必须是 0：并发下出现 5xx / 超时说明连接池或数据库配置需要调；",
        "- **吞吐（req/s）** 受限于最慢的那个接口；SQLite 单写者模型下，"
        "读路径可并发、写路径排队，上多人协作应换 MySQL；",
        "- **429 是保护不是故障**：登录 30/min·IP、对话 120/min·账号，窗口 60 秒。"
        "**连续跑多轮会累计消耗同一分钟的配额** —— 要对比数据就每轮之间隔 1 分钟。",
    ]
    text = "\n".join(lines) + "\n"

    if not args.no_report:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(text)
        print(f"报告已写入 {REPORT}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
