# -*- coding: utf-8 -*-
"""Dify 知识库嵌入模型原地切换（看现状 → PATCH → 等重建 → 命中验证）。

只依赖标准库。凭据从命令行或环境变量读，**不落盘**。

用法：
    export DIFY_CONSOLE_BASE=http://127.0.0.1
    export DIFY_CONSOLE_EMAIL=you@example.com
    export DIFY_CONSOLE_PASSWORD=******

    # 预演（只看现状，不写）
    python kb_embed_switch.py --dataset <id> --provider langgenius/zhipuai/zhipuai --model embedding-3

    # 执行
    python kb_embed_switch.py --dataset <id> --provider langgenius/zhipuai/zhipuai --model embedding-3 --apply

    # 多个知识库 + 自定义命中测试词
    python kb_embed_switch.py --dataset A --dataset B --provider ... --model ... --apply \
        --query 签证 --query 申请服务

⚠️ Windows 上访问本机 Dify 建议先设 NO_PROXY=127.0.0.1,localhost，否则系统代理会把本机请求也接管。
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_QUERIES = ["介绍", "服务", "流程"]


class Console:
    """Dify 控制面最小客户端（cookie + X-CSRF-Token）。"""

    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.cj = http.cookiejar.CookieJar()
        # 显式空 ProxyHandler：确保本机请求不走系统代理
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cj),
        )
        self.login()

    def _csrf(self) -> str | None:
        for c in self.cj:
            if c.name == "csrf_token":
                return c.value
        return None

    def req(self, method: str, path: str, body=None, timeout: int = 60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        csrf = self._csrf()
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

    def login(self):
        st, r = self.req("POST", "/console/api/login", {
            "email": self.email, "password": self.password,
            "language": "zh-Hans", "remember_me": True,
        })
        if st != 200:
            sys.exit(f"[FATAL] 登录失败 HTTP {st}: {json.dumps(r, ensure_ascii=False)[:300]}")
        return r


def show(d: Console, ds: str, tag: str = "") -> dict:
    st, r = d.req("GET", f"/console/api/datasets/{ds}")
    if st != 200:
        sys.exit(f"[FATAL] 读数据集失败 HTTP {st}: {json.dumps(r, ensure_ascii=False)[:300]}")
    print(f"  [{tag or ds}] embedding_model={r.get('embedding_model')} "
          f"provider={r.get('embedding_model_provider')} "
          f"technique={r.get('indexing_technique')} docs={r.get('document_count')} "
          f"文档状态={doc_stats(d, ds)}")
    return r


def switch(d: Console, ds: str, provider: str, model: str, technique: str):
    st, r = d.req("PATCH", f"/console/api/datasets/{ds}", {
        # ⚠️ 三个字段一个都不能少：漏 indexing_technique 直接 500
        "indexing_technique": technique,
        "embedding_model": model,
        "embedding_model_provider": provider,
    }, timeout=300)
    print(f"  PATCH HTTP {st}: {json.dumps(r, ensure_ascii=False)[:200]}")
    return st == 200


def doc_stats(d: Console, ds: str) -> dict:
    st, r = d.req("GET", f"/console/api/datasets/{ds}/documents?page=1&limit=100")
    counts: dict[str, int] = {}
    for it in (r or {}).get("data", []):
        counts[it.get("indexing_status")] = counts.get(it.get("indexing_status"), 0) + 1
    return counts


# Dify 文档在各阶段的状态；这些表示「还在重建中」
_IN_PROGRESS = {"waiting", "parsing", "cleaning", "splitting", "indexing"}


def wait_reindex(d: Console, ds: str, rounds: int = 40, gap: int = 3) -> bool:
    """⚠️ 数据集级的 indexing_status 在稳定态是 None，不能拿它当判据；
    必须看**文档级**状态：没有进行中的、且至少有一条 completed，才算重建完成。"""
    for i in range(rounds):
        counts = doc_stats(d, ds)
        pending = sum(v for k, v in counts.items() if k in _IN_PROGRESS)
        print(f"    [wait {i}] docs={counts} pending={pending}")
        if counts.get("error"):
            print("    ⚠️ 出现 error 状态的文档，重建失败")
            return False
        if pending == 0 and counts.get("completed"):
            return True
        time.sleep(gap)
    return False


def hit_test(d: Console, ds: str, queries: list[str]) -> bool:
    ok = False
    for q in queries:
        st, r = d.req("POST", f"/console/api/datasets/{ds}/hit-testing", {"query": q})
        recs = (r or {}).get("records", []) if isinstance(r, dict) else []
        print(f"    query={q!r} HTTP {st} 命中 {len(recs)} 条")
        for rec in recs[:2]:
            seg = rec.get("segment", {})
            print(f"       score={rec.get('score'):.3f}  {(seg.get('content') or '')[:50]}")
        ok = ok or bool(recs)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Dify 知识库嵌入模型原地切换")
    ap.add_argument("--base", default=os.environ.get("DIFY_CONSOLE_BASE", "http://127.0.0.1"))
    ap.add_argument("--email", default=os.environ.get("DIFY_CONSOLE_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("DIFY_CONSOLE_PASSWORD"))
    ap.add_argument("--dataset", action="append", required=True, help="dataset id，可重复")
    ap.add_argument("--provider", help="如 langgenius/zhipuai/zhipuai")
    ap.add_argument("--model", help="如 embedding-3")
    ap.add_argument("--technique", default="high_quality")
    ap.add_argument("--query", action="append", default=[], help="命中测试用 query，可重复")
    ap.add_argument("--apply", action="store_true", help="不加则只预演")
    args = ap.parse_args()

    if not args.email or not args.password:
        sys.exit("[FATAL] 请提供 --email/--password 或 DIFY_CONSOLE_EMAIL/DIFY_CONSOLE_PASSWORD")

    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    d = Console(args.base, args.email, args.password)
    print(f"登录成功：{args.base}")

    original = {}
    print("\n=== 变更前（记下来，回滚要用）===")
    for ds in args.dataset:
        original[ds] = show(d, ds)

    if not args.apply:
        print("\n[DRY-RUN] 未加 --apply，不执行变更。")
        if args.provider and args.model:
            print(f"将要改为：provider={args.provider} model={args.model} technique={args.technique}")
        return 0

    if not args.provider or not args.model:
        sys.exit("[FATAL] --apply 需要同时给出 --provider 与 --model")

    print("\n=== 执行 PATCH ===")
    for ds in args.dataset:
        if not switch(d, ds, args.provider, args.model, args.technique):
            print(f"  ⚠️ {ds} PATCH 失败（检查：插件是否已装 / 凭据是否已写 / 是否漏 indexing_technique）")

    print("\n=== 等待重建 ===")
    all_ok = True
    for ds in args.dataset:
        ok = wait_reindex(d, ds)
        print(f"  文档索引状态：{doc_stats(d, ds)}")
        all_ok = all_ok and ok

    print("\n=== 命中验证（能召回才算真的换成功）===")
    queries = args.query or DEFAULT_QUERIES
    hit_ok = False
    for ds in args.dataset:
        hit_ok = hit_test(d, ds, queries) or hit_ok

    print("\n=== 结论 ===")
    print(f"  重建完成: {all_ok}    命中验证: {hit_ok}")
    if not (all_ok and hit_ok):
        print("  ⚠️ 未全部通过 —— 别只看配置回显，先确认重建完成与凭据可用。")
        return 1
    print("  ✅ 切换成功。原配置（回滚用）：")
    for ds, r in original.items():
        print(f"     {ds}: {r.get('embedding_model_provider')} / {r.get('embedding_model')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
