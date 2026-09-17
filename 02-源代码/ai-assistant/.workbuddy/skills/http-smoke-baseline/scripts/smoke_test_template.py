# -*- coding: utf-8 -*-
"""HTTP 冒烟基线模板 —— 对**真实运行中的服务**发请求，覆盖核心链路并产出报告。

用法：
    # 1) 先起服务（真实端口，不是进程内 TestClient）
    uvicorn app.main:app --port 8010
    # 2) 再跑冒烟
    python scripts/smoke_test.py [--base http://127.0.0.1:8010]

产出：reports/smoke_report.md（逐条结果 + 耗时）
退出码：0 = 全部通过；1 = 有失败（可直接被 CI / 一键启动器调用）

设计要点（详见 SKILL.md）：
  - HTTP 状态码 **和** 业务 code 双校验；非统一信封的端点用 expect_code=None
  - 异常兜底成一条 FAIL，绝不中断整轮
  - 每条用例记耗时，冷启动/性能回归一眼可见
  - 造数带 uuid，避免撞唯一约束或改到别人的数据
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, List, Optional, Tuple

import httpx

# 结果元组：(用例名, PASS/FAIL, 说明, 耗时ms)
RESULTS: List[Tuple[str, str, str, float]] = []


def record(name: str, ok: bool, detail: str, cost_ms: float) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail, cost_ms))
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<44} {cost_ms:7.1f}ms  {detail}")


def check(client: httpx.Client, name: str, method: str, path: str, *,
          expect: int = 200, expect_code: Optional[int] = 0, **kwargs) -> Optional[Any]:
    """发一次请求并记录结果。

    expect_code=None 用于**非统一响应信封**的端点（/docs、/openapi.json、静态资源），
    否则会为了绕过校验写一堆丑陋特例。
    任何异常都降级成一条 FAIL，不抛出 —— 否则脚本会在第一个红点上崩掉。
    """
    started = time.perf_counter()
    try:
        resp = client.request(method, path, **kwargs)
        cost = (time.perf_counter() - started) * 1000
        try:
            payload = resp.json()
        except Exception:                                       # noqa: BLE001
            payload = {"_raw": resp.text[:120]}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = resp.status_code == expect and (expect_code is None or code == expect_code)
        detail = f"HTTP {resp.status_code} code={code}"
        if not ok:
            detail += f" body={json.dumps(payload, ensure_ascii=False)[:160]}"
        record(name, ok, detail, cost)
        return payload if ok else None
    except Exception as exc:                                    # noqa: BLE001
        cost = (time.perf_counter() - started) * 1000
        record(name, False, f"EXCEPTION {exc}", cost)
        return None


def auth(payload: Optional[Any]) -> dict:
    """从登录响应里取 Bearer 头。登录失败时返回空字典，后续用例会自然 401。"""
    if not payload:
        return {}
    return {"Authorization": f"Bearer {payload['data']['access_token']}"}


def unwrap(payload: Optional[Any]) -> dict:
    """取出统一信封里的 data；失败时返回空字典。"""
    return (payload or {}).get("data") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    args = parser.parse_args()

    started_at = datetime.now()
    print(f"== 冒烟测试目标: {args.base}  开始 {started_at:%Y-%m-%d %H:%M:%S}\n")

    tag = uuid.uuid4().hex[:8]          # 造数后缀：反复跑不撞唯一约束，也便于事后清理

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        # ---------------- 1. 存活与就绪 ----------------
        check(client, "健康检查 /health", "GET", "/api/v1/health")
        check(client, "就绪探针 /ready", "GET", "/api/v1/ready")
        # OpenAPI 文档不是统一响应体（没有 code 字段），只校验 HTTP 200
        check(client, "OpenAPI 文档", "GET", "/openapi.json", expect_code=None)

        # ---------------- 2. 各角色登录 ----------------
        admin = check(client, "登录（管理员）", "POST", "/api/v1/auth/token",
                      json={"username": "admin", "password": "admin123"})
        staff = check(client, "登录（普通员工）", "POST", "/api/v1/auth/token",
                      json={"username": "advisor", "password": "advisor123"})
        limited = check(client, "登录（受限角色）", "POST", "/api/v1/auth/token",
                        json={"username": "student", "password": "student123"})
        visitor = check(client, "访客 Token", "POST", "/api/v1/auth/visitor")

        admin_h, staff_h = auth(admin), auth(staff)
        limited_h, visitor_h = auth(limited), auth(visitor)

        check(client, "当前身份 /auth/me", "GET", "/api/v1/auth/me", headers=staff_h)

        # ---------------- 3. 鉴权边界 ----------------
        check(client, "无 token 访问受保护端点应 401", "GET", "/api/v1/auth/me",
              expect=401, expect_code=None, headers={})
        check(client, "受限角色访问管理端点应 403", "GET", "/api/v1/leads",
              expect=403, headers=limited_h)
        check(client, "访客访问业务数据应 403", "GET", "/api/v1/leads",
              expect=403, headers=visitor_h)
        check(client, "管理端点（管理员）", "GET", "/api/v1/leads", headers=admin_h)

        # ---------------- 4. 主链路：列表 → 创建 → 详情 → 写后读 ----------------
        created = check(client, "创建线索", "POST", "/api/v1/leads",
                        json={"name": f"冒烟测试客户-{tag}", "phone": "13700000000",
                              "source": "冒烟"}, headers=staff_h)
        lead_id = unwrap(created).get("id")

        if lead_id:
            check(client, "线索详情", "GET", f"/api/v1/leads/{lead_id}", headers=staff_h)
            listed = check(client, "写后读：列表里能查到刚创建的线索", "GET",
                           "/api/v1/leads", headers=staff_h)
            names = [row.get("name") for row in (unwrap(listed).get("items") or [])]
            record("写后读一致性", f"冒烟测试客户-{tag}" in names,
                   f"列表 {len(names)} 条", 0.0)

        # ---------------- 5. 参数校验 ----------------
        check(client, "坏参数应 422", "POST", "/api/v1/leads",
              expect=422, expect_code=None, json={}, headers=staff_h)

        # ---------------- 6. 幂等性 ----------------
        key = f"smoke-{tag}"
        body = {"content": {"reason": "冒烟"}, "idempotency_key": key}
        first = check(client, "幂等写入（第 1 次）", "POST", "/api/v1/requests",
                      json=body, headers=limited_h)
        second = check(client, "幂等写入（第 2 次，应复用同一条）", "POST",
                       "/api/v1/requests", json=body, headers=limited_h)
        same = (unwrap(first).get("id") == unwrap(second).get("id")
                and unwrap(first).get("id") is not None)
        record("幂等键生效", same, f"id={unwrap(first).get('id')} vs {unwrap(second).get('id')}", 0.0)

    # ---------------- 报告 ----------------
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    failed = len(RESULTS) - passed
    total_ms = sum(r[3] for r in RESULTS)

    lines = [
        "# 冒烟测试报告",
        "",
        f"- 目标服务：`{args.base}`",
        f"- 执行时间：{started_at:%Y-%m-%d %H:%M:%S}",
        f"- 用例总数：**{len(RESULTS)}**，通过 **{passed}**，失败 **{failed}**",
        f"- 累计请求耗时：{total_ms:.0f} ms",
        "",
        "| # | 用例 | 结果 | 耗时(ms) | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, (name, flag, detail, cost) in enumerate(RESULTS, 1):
        mark = "✅" if flag == "PASS" else "❌"
        lines.append(f"| {i} | {name} | {mark} {flag} | {cost:.1f} | {detail} |")
    lines.append("")
    lines.append(f"结论：**{'全部通过' if failed == 0 else f'{failed} 条未通过，需排查'}**")
    lines.append("")

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "smoke_report.md")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n== 汇总：{passed}/{len(RESULTS)} 通过，{failed} 失败，累计 {total_ms:.0f} ms")
    print(f"== 报告已写入 {out_file}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
