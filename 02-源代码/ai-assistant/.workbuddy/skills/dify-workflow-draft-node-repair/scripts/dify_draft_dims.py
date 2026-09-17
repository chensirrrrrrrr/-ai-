"""Dify 工作流节点在网页编辑器里显示成「细白条」—— 检测与修复（自包含，仅用标准库）。

## 真正的根因（2026-09-16 定位，推翻此前「前端测量」的旧结论）

Dify 画布给 ReactFlow 只注册了**一个**节点组件，key 是 `"custom"`；
真正的节点种类放在 `data.type` 里（start / llm / answer / ...）。

Dify 前端源码（1.10.0，`web/app/components/workflow/utils/flow-to-canvas` 编译产物）：

    return a.map(e => { e.type || (e.type = l.hB); ... })        // l.hB === "custom"

即**只有顶层 `type` 缺失时才补成 `"custom"`**。如果用 API 建图时把顶层 `type`
写成了 `"answer"` / `"llm"` / `"start"`，前端就保留这个值，ReactFlow 在 nodeTypes
里查不到 → 退化成内置 default 节点。

ReactFlow v11 的 default 节点 CSS 是：

    .react-flow__node-default{padding:10px;width:150px;background-color:#fff;...}

空节点量出来正好 **150x21 的白条**，与前端的自动保存值完全吻合。

次生症状：Dify 的「检查清单」只统计 `type === "custom"` 的节点

    let i = e.filter(e => e.type === c.hB)          // c.hB === "custom"
    Object.keys(p).filter(e => p[e].metaData.isRequired).forEach(e => {
      i.find(t => t.data.type === e) || r.push({ ... needAdd ... })
    })

于是误报「**必须添加直接回复节点**」——哪怕图里明明有 6 个 answer 节点。

**后端执行只认 `data.type`**，所以已发布版本一直跑得好好的 ⇒ 纯前端观感 + 编辑器不可用。

## 用法

    python dify_draft_dims.py --base http://127.0.0.1 \
        --email you@example.com --password 'xxx'                 # 只检测（dry-run）
    python dify_draft_dims.py ... --apply --yes                  # 写回全部工作流应用
    python dify_draft_dims.py ... --app <app_id|应用名> --apply --yes

写回时：顶层 `type` 统一改成 `"custom"`，`data`（prompt / model / variables / 连线）
一个字段都不碰；尺寸按 `data.type` 归一化；并带上刚读到的 draft hash 做乐观锁。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

# 画布节点唯一的 ReactFlow 组件 key
REACTFLOW_NODE_TYPE = "custom"

# 尺寸按 data.type 归一化（start/answer/end 矮一点，其余略高）
SIZE_BY_BLOCK = {
    "start": (244, 90),
    "end": (244, 90),
    "answer": (244, 90),
}
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
        self.email = email
        self.password = password
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
            raise SystemExit(f"登录失败 HTTP {st}: {str(res)[:300]}")

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
    """返回 (bad_type_nodes, bad_size_nodes)。

    bad_type  : 顶层 type != "custom" —— 这就是白条的根因
    bad_size  : 尺寸与 data.type 对应值不符（含 150x21 的塌缩值）
    """
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
        block = (n.get("data") or {}).get("type")
        tw, th = SIZE_BY_BLOCK.get(block, DEFAULT_SIZE)
        n["width"], n["height"] = tw, th
    return graph


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--app", action="append", default=[],
                    help="只处理指定 app_id 或应用名，可重复；默认处理全部 workflow 应用")
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过写回前的确认")
    args = ap.parse_args()

    d = Dify(args.base, args.email, args.password)
    apps = d.list_apps()

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
        print(f"将要修正 draft 的节点顶层 type（-> custom）与尺寸：{names}")
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
            print(f"\n[{name}] {app_id}  不是工作流应用（无节点），跳过")
            continue

        bad_type, bad_size = inspect(graph)
        print(f"\n[{name}] {app_id}  nodes={len(nodes)}  "
              f"顶层type错误={len(bad_type)}  尺寸不符={len(bad_size)}")
        for nid, cur, block, title in bad_type:
            print(f"    type  {nid:<12} {str(cur):<20} -> custom   (data.type={block}, {title})")
        for nid, block, title, w, h, tw, th in bad_size:
            print(f"    size  {nid:<12} {w}x{h} -> {tw}x{th}   (data.type={block}, {title})")

        if not bad_type and not bad_size:
            print("  ✓ 已是 custom 且尺寸正常，无需修改")
            continue

        if not args.apply:
            print("  [dry-run] 未提交")
            continue

        apply_fix(graph)
        st, res = d.put_draft(app_id, graph, features=body.get("features"),
                              env_vars=body.get("environment_variables"),
                              conv_vars=body.get("conversation_variables"),
                              hash_=body.get("hash"))
        print(f"  写回 -> HTTP {st} {str(res)[:200]}")
        if st != 200:
            rc = 1
        else:
            print("  提示：立刻重新读一次确认；若用户编辑器页开着，让他 Ctrl+Shift+R 强制刷新")

    if not args.apply:
        print("\n（dry-run 结束；加 --apply 才会写入）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
