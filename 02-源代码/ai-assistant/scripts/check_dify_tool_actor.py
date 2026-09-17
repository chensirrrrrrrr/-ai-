# -*- coding: utf-8 -*-
"""检查 Dify 应用草稿里「调内部客户工具」的节点是否带了 `actor`（操作人身份）。

为什么需要它
------------
`lead_query` / `lead_lookup` 做了行级收敛以后（employee 只查自己名下），工具调用
**必须带上操作人的登录账号名** —— Dify 侧通常就是 `sys.user_id`（形如 `uas-advisor`，
后端会自动剥掉 `dify_user_prefix` 前缀）。不带的话员工角色查不到任何客户（刻意 fail closed），
表现是「助手说查不到」而不是报错，很难定位。

本脚本守的就是这条接线规范：把所有调用 `/internal/tools/*` 的节点摊开看，
凡是调 `lead_query` / `lead_lookup` 却没带 `actor` 的，**直接判红**。

凭据（不写进代码库）
--------------------
    DIFY_CONSOLE_EMAIL=... DIFY_CONSOLE_PASSWORD=... python scripts/check_dify_tool_actor.py

未设置凭据 → 打印跳过说明并以退出码 **2** 结束（不是失败）。
`--base` 可覆盖控制台地址（默认 http://127.0.0.1）。

退出码：0 = 无违规；1 = 发现未带 actor 的节点；2 = 跳过 / 环境不可用。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List

# 需要「操作人身份」才能安全执行的内部工具（行级收敛的那一批）
NEEDS_ACTOR = ("lead_query", "lead_lookup")


class Console:
    def __init__(self, base: str, email: str, password: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),                    # 本机代理会劫持 127.0.0.1
            urllib.request.HTTPCookieProcessor(self.cj))
        self.email, self.password = email, password

    def _req(self, method: str, path: str, payload: Any = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        # 1.x 的 console API 读写都要求 X-CSRF-Token（值 = 登录后种下的 csrf_token cookie）
        for cookie in self.cj:
            if cookie.name == "csrf_token":
                request.add_header("X-CSRF-Token", cookie.value)
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw[:200]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:300]
        except urllib.error.URLError as exc:
            return 0, f"连接失败：{exc.reason}"

    def login(self) -> bool:
        status, body = self._req("POST", "/console/api/login",
                                 {"email": self.email, "password": self.password,
                                  "language": "zh-Hans", "remember_me": True})
        return status == 200 and isinstance(body, dict) and body.get("result") == "success"

    def apps(self) -> List[Dict[str, Any]]:
        status, body = self._req("GET", "/console/api/apps?page=1&limit=100")
        return body.get("data", []) if status == 200 and isinstance(body, dict) else []

    def draft(self, app_id: str):
        return self._req("GET", f"/console/api/apps/{app_id}/workflows/draft")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Dify 工具节点是否带 actor")
    parser.add_argument("--base", default=os.environ.get("DIFY_CONSOLE_BASE", "http://127.0.0.1"))
    args = parser.parse_args()

    email = os.environ.get("DIFY_CONSOLE_EMAIL", "").strip()
    password = os.environ.get("DIFY_CONSOLE_PASSWORD", "")
    if not email or not password:
        print("跳过：未设置 DIFY_CONSOLE_EMAIL / DIFY_CONSOLE_PASSWORD（凭据不写进代码库）。")
        return 2

    console = Console(args.base, email, password)
    if not console.login():
        print("登录控制台失败（账号/密码，或控制台没起）。")
        return 2

    apps = console.apps()
    print(f"控制台 {args.base}：应用 {len(apps)} 个\n")

    violations: List[str] = []
    tool_nodes = 0

    for app in apps:
        app_id, name, mode = app["id"], app["name"], app.get("mode")
        if mode not in ("workflow", "advanced-chat"):
            print(f"- {name}（{mode}）：非工作流应用，跳过")
            continue
        status, body = console.draft(app_id)
        if status != 200:
            print(f"- {name}（{mode}）：取草稿失败 {status}")
            continue
        nodes = body.get("graph", {}).get("nodes", [])
        hits = []
        for node in nodes:
            blob = json.dumps(node, ensure_ascii=False)
            if "/internal/tools/" not in blob:
                continue
            data = node.get("data", {})
            url = str(data.get("url") or "")
            tool = url.rstrip("/").rsplit("/", 1)[-1]
            has_actor = '"actor"' in blob or "'actor'" in blob
            hits.append((tool, data.get("title"), has_actor))
            tool_nodes += 1
            if tool in NEEDS_ACTOR and not has_actor:
                violations.append(f"{name}（{mode}）节点「{data.get('title')}」调 {tool} 未带 actor")

        print(f"- {name}（{mode}）：{len(nodes)} 节点，工具调用 {len(hits)} 个")
        for tool, title, has_actor in hits:
            need = tool in NEEDS_ACTOR
            flag = ("✅" if has_actor else "❌") if need else "—"
            print(f"    {flag} {title} → {tool}"
                  f"{'' if need else '（无需 actor）'}")

    print()
    if violations:
        print(f"发现 {len(violations)} 处未带 actor 的调用：")
        for line in violations:
            print(f"  ❌ {line}")
        print("\n修法：HTTP 请求节点的 body 里补 \"actor\": \"{{#sys.user_id#}}\"（值形如 uas-advisor）。")
        return 1
    print(f"✅ 无违规：共检查 {tool_nodes} 个工具调用节点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
