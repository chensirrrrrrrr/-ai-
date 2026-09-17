"""Dify 画布节点自检/修复：检查工作流 draft 的节点类型与尺寸是否符合画布约定。

为什么需要它
------------
本项目用 API（而不是网页编辑器）创建 Dify 工作流。画布给 ReactFlow 只注册了
**一个** 节点组件，key 是 `custom`；真正的节点种类放在节点数据 `data.type` 里。
若在节点的**顶层 `type`** 上也写了业务种类（`answer` / `llm` / `start` …），
图形引擎找不到对应组件，会退化成内置 default 节点，表现为：

- 画布上一排 **150×21 的空白细条**，节点看不出内容、点不进去；
- 右侧检查清单**误报**「必须添加直接回复节点」（它只统计顶层 `type == custom` 的节点）。

**后端执行只读 `data.type`，所以这类问题不影响运行，只损坏编辑体验** ——
排查时极易被误判成程序缺陷。本脚本用来在改完图之后立刻自查。

用法
----
    # 只检查（默认），凭据优先取环境变量
    python scripts/check_dify_node_type.py
    python scripts/check_dify_node_type.py --base http://127.0.0.1 \\
        --email you@example.com --password '***'
    # 真正写回
    python scripts/check_dify_node_type.py --apply --yes
    # 只处理指定应用（app_id 或应用名，可重复）
    python scripts/check_dify_node_type.py --app router --apply --yes

环境变量：DIFY_CONSOLE_BASE（默认 http://127.0.0.1）、
          DIFY_CONSOLE_EMAIL、DIFY_CONSOLE_PASSWORD

退出码：0 = 无需修改或修复成功；1 = 读取/写回失败；2 = 缺凭据。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

# 画布节点唯一的 ReactFlow 组件 key
REACTFLOW_NODE_TYPE = "custom"

# 尺寸按 data.type 归一化（start / answer / end 矮一点，其余略高）
SIZE_BY_BLOCK = {"start": (244, 90), "end": (244, 90), "answer": (244, 90)}
DEFAULT_SIZE = (244, 98)

EMPTY_FEATURES = {
    "opening_statement": "",
    "suggested_questions": [],
    "suggested_questions_after_answer": {"enabled": False},
    "speech_to_text": {"enabled": False},
    "text_to_speech": {"enabled": False},
    "retriever_resource": {"enabled": False},
    "sensitive_word_avoidance": {"enabled": False},
}


class Dify:
    """极简 Dify 控制台客户端（cookie 登录 + CSRF）。"""

    def __init__(self, base: str, email: str, password: str):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),          # 内网目标必须绕开系统代理
            urllib.request.HTTPCookieProcessor(self.cj),
        )
        st, res = self.req("POST", "/console/api/login", {
            "email": email, "password": password,
            "language": "zh-Hans", "remember_me": True,
        })
        if st != 200:
            raise SystemExit(f"控制台登录失败 HTTP {st}: {str(res)[:300]}")

    def _ck(self, name):
        for c in self.cj:
            if c.name == name:
                return c.value

    def req(self, method: str, path: str, body=None, timeout: int = 120):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        csrf = self._ck("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, {"_raw": raw[:600]}
        except Exception as e:  # noqa: BLE001
            return -1, {"_exc": f"{type(e).__name__}: {e}"}

    def list_apps(self):
        st, d = self.req("GET", "/console/api/apps?page=1&limit=100")
        return d.get("data", []) if st == 200 else []

    def get_draft(self, app_id: str):
        return self.req("GET", f"/console/api/apps/{app_id}/workflows/draft")

    def put_draft(self, app_id: str, graph: dict, features=None, env_vars=None,
                  conv_vars=None, hash_=None):
        body = {
            "graph": graph,
            "features": features if features is not None else dict(EMPTY_FEATURES),
            "environment_variables": env_vars if env_vars is not None else [],
            "conversation_variables": conv_vars if conv_vars is not None else [],
        }
        if hash_:
            body["hash"] = hash_
        return self.req("POST", f"/console/api/apps/{app_id}/workflows/draft", body)


def inspect(graph: dict):
    """返回 (bad_type, bad_size)：顶层 type 不是 custom 的；尺寸与 data.type 不符的。"""
    bad_type, bad_size = [], []
    for n in graph.get("nodes") or []:
        nid = n.get("id")
        block = (n.get("data") or {}).get("type")
        title = (n.get("data") or {}).get("title")
        if n.get("type") != REACTFLOW_NODE_TYPE:
            bad_type.append((nid, n.get("type"), block, title))
        tw, th = SIZE_BY_BLOCK.get(block, DEFAULT_SIZE)
        if (n.get("width"), n.get("height")) != (tw, th):
            bad_size.append((nid, block, title, n.get("width"), n.get("height"), tw, th))
    return bad_type, bad_size


def apply_fix(graph: dict):
    for n in graph.get("nodes") or []:
        n["type"] = REACTFLOW_NODE_TYPE
        tw, th = SIZE_BY_BLOCK.get((n.get("data") or {}).get("type"), DEFAULT_SIZE)
        n["width"], n["height"] = tw, th
    return graph


def main() -> int:
    ap = argparse.ArgumentParser(description="检查/修复 Dify 工作流 draft 的节点类型与尺寸")
    ap.add_argument("--base", default=os.environ.get("DIFY_CONSOLE_BASE", "http://127.0.0.1"))
    ap.add_argument("--email", default=os.environ.get("DIFY_CONSOLE_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("DIFY_CONSOLE_PASSWORD"))
    ap.add_argument("--app", action="append", default=[],
                    help="只处理指定 app_id 或应用名，可重复；默认处理全部工作流应用")
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只检查）")
    ap.add_argument("--yes", action="store_true", help="跳过写回前的确认")
    args = ap.parse_args()

    if not args.email or not args.password:
        print("缺少控制台凭据：请用 --email/--password，或设置 "
              "DIFY_CONSOLE_EMAIL / DIFY_CONSOLE_PASSWORD。", file=sys.stderr)
        return 2

    d = Dify(args.base, args.email, args.password)
    apps = d.list_apps()
    if not apps:
        print("拉不到应用列表（检查 --base 与控制台凭据）。")
        return 1

    if args.app:
        wanted = set(args.app)
        targets = [(a.get("name"), a.get("id")) for a in apps
                   if a.get("id") in wanted or a.get("name") in wanted]
    else:
        targets = [(a.get("name"), a.get("id")) for a in apps
                   if a.get("mode") in ("workflow", "advanced-chat")]

    if not targets:
        print("没有匹配的工作流应用")
        return 1

    if args.apply and not args.yes:
        names = ", ".join(n or i for n, i in targets)
        print(f"将修正 draft 的节点顶层 type（-> custom）与尺寸：{names}")
        if input("确认？输入 yes 继续：").strip().lower() != "yes":
            print("已取消")
            return 1

    rc = 0
    for name, app_id in targets:
        st, body = d.get_draft(app_id)
        if st != 200:
            print(f"[{name}] 读 draft 失败 HTTP {st}: {str(body)[:200]}")
            rc = 1
            continue

        graph = body.get("graph") or {}
        nodes = graph.get("nodes") or []
        if not nodes:
            print(f"[{name}] {app_id}  不是工作流应用（无节点），跳过")
            continue

        bad_type, bad_size = inspect(graph)
        flag = "OK" if not bad_type and not bad_size else "需修正"
        print(f"\n[{name}] {app_id}  nodes={len(nodes)}  "
              f"顶层type错误={len(bad_type)}  尺寸不符={len(bad_size)}  -> {flag}")
        for nid, cur, block, title in bad_type:
            print(f"    type  {nid:<12} {str(cur):<20} -> custom   (data.type={block}, {title})")
        for nid, block, title, w, h, tw, th in bad_size:
            print(f"    size  {nid:<12} {w}x{h} -> {tw}x{th}   (data.type={block}, {title})")

        if not bad_type and not bad_size:
            continue
        if not args.apply:
            print("    [仅检查] 加 --apply 才会写回")
            continue

        apply_fix(graph)
        st, res = d.put_draft(app_id, graph, features=body.get("features"),
                              env_vars=body.get("environment_variables"),
                              conv_vars=body.get("conversation_variables"),
                              hash_=body.get("hash"))
        print(f"    写回 -> HTTP {st} {str(res)[:200]}")
        if st != 200:
            rc = 1
        else:
            print("    提示：若网页编辑器开着，请 Ctrl+Shift+R 强制刷新后再看")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
