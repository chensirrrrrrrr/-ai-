"""Dify 接线校验：**在把 DIFY_MODE 切成 live 之前先跑这个**。

为什么需要它
------------
live 模式下 Dify 调用失败会被降级到 mock（这是「不出现无响应」的设计），
但这个兜底有个副作用：**Key 写错 / 地址写错 / 应用没建**，用户拿到的是
mock 回答，却以为「已经接上 Dify 了」——从答复里根本看不出来。

本脚本按应用逐个探，把结论摊开说清楚：
- Key 有效 & 应用类型对        → OK
- Key 无效（401）              → 要重新复制 Key
- 连不上（ConnectionError）    → DIFY_BASE_URL / 网络 / 容器没起
- 路径不对（普遍 404）         → 多半是 base_url 少了 `/v1`
- Key 没配（.env 里缺）        → 直接指出来缺哪几个

用法
----
    python scripts/check_dify.py                # 检查全部 7 个应用
    python scripts/check_dify.py --app router   # 只查一个
    python scripts/check_dify.py --json         # 机器可读（给 CI / 启动器用）
    python scripts/check_dify.py --timeout 8

退出码：0 = 全部通过；1 = 有问题；2 = 用法/配置错误（例如还没切 live）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx                                                    # noqa: E402

from app.config import DIFY_APP_NAMES, settings                  # noqa: E402

OK = "ok"
KEY_INVALID = "key_invalid"
MISSING = "missing"
UNREACHABLE = "unreachable"
PATH_WRONG = "path_wrong"
UNKNOWN = "unknown"

VERDICT_TEXT = {
    OK: "通过",
    KEY_INVALID: "Key 无效（401）",
    MISSING: ".env 里没配 Key",
    UNREACHABLE: "连不上",
    PATH_WRONG: "路径不对（404，检查 base_url 是否以 /v1 结尾）",
    UNKNOWN: "状态未知",
}


def _probe_chat(client: httpx.Client, key: str) -> tuple[str, str]:
    """Chat / Chatflow / Agent 类应用：GET /parameters 是最省的一次鉴权探针。"""
    resp = client.get("/parameters", headers={"Authorization": f"Bearer {key}"})
    if resp.status_code == 200:
        return OK, "GET /parameters 200"
    if resp.status_code in (401, 403):
        return KEY_INVALID, f"GET /parameters {resp.status_code}"
    if resp.status_code == 404:
        return "", ""                                  # 交给 workflow 探测再判
    return UNKNOWN, f"GET /parameters {resp.status_code}"


def _probe_workflow(client: httpx.Client, key: str) -> tuple[str, str]:
    """Workflow 类应用没有 /parameters，改用一个**故意不合法**的请求体探。

    只发 `{"response_mode":"blocking"}`：Dify 会因为缺 `user` 直接 400，
    说明「路由存在 + 鉴权通过」，且**不会真的跑一遍工作流**（没有副作用/不烧 token）。
    """
    resp = client.post("/workflows/run",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json={"response_mode": "blocking"})
    if resp.status_code in (400, 422):
        return OK, f"POST /workflows/run {resp.status_code}（鉴权通过，请求体故意不合法）"
    if resp.status_code in (401, 403):
        return KEY_INVALID, f"POST /workflows/run {resp.status_code}"
    if resp.status_code == 404:
        return PATH_WRONG, "POST /workflows/run 404"
    return UNKNOWN, f"POST /workflows/run {resp.status_code}"


def check_one(app: str, timeout: float,
              transport: Optional[httpx.BaseTransport] = None) -> Dict[str, Any]:
    """探一个应用。`transport` 只给测试注入用（生产走真实网络）。"""
    key = settings.dify_key(app)
    if not key:
        return {"app": app, "verdict": MISSING, "detail": "DIFY_APP_KEYS 里没有这个应用",
                "masked_key": ""}

    masked = f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "…"
    with httpx.Client(base_url=settings.dify_base_url.rstrip("/"), timeout=timeout,
                      transport=transport,
                      trust_env=settings.dify_use_proxy) as client:
        try:
            verdict, detail = _probe_chat(client, key)
            if not verdict:                            # 404 → 可能不是 chat 类应用
                verdict, detail = _probe_workflow(client, key)
        except httpx.ConnectError as exc:
            return {"app": app, "verdict": UNREACHABLE, "masked_key": masked,
                    "detail": f"连不上 {settings.dify_base_url}（{exc.__class__.__name__}）"}
        except httpx.TimeoutException:
            return {"app": app, "verdict": UNREACHABLE, "masked_key": masked,
                    "detail": f"超时（>{timeout:g}s）"}
        except Exception as exc:                       # noqa: BLE001
            return {"app": app, "verdict": UNKNOWN, "masked_key": masked,
                    "detail": f"{exc.__class__.__name__}: {exc}"}

    return {"app": app, "verdict": verdict, "masked_key": masked, "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Dify 接线校验")
    parser.add_argument("--app", default=None, help="只检查某个应用名")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    apps: List[str] = list(DIFY_APP_NAMES)
    if args.app:
        if args.app not in DIFY_APP_NAMES:
            print(f"未知应用名 {args.app!r}；可选：{', '.join(DIFY_APP_NAMES)}")
            return 2
        apps = [args.app]

    header = {
        "mode": settings.dify_mode,
        "base_url": settings.dify_base_url,
        "target_is_local": settings.dify_target_is_local,
        "use_proxy": settings.dify_use_proxy,
        "apps": apps,
    }

    if not settings.is_dify_live:
        header["hint"] = ("当前是 mock 模式，不会真连 Dify。"
                          "确认下面这些信息没问题后，把 .env 的 DIFY_MODE 改成 live 再跑一次。")

    results = [check_one(app, args.timeout) for app in apps]

    # 判定口径：
    # - 「没配 Key」在 mock 模式下是**正常状态**（本来就不需要），不算失败；
    #   但一旦切到 live，它就是必须修的问题。
    # - 其余几种（Key 无效 / 连不上 / 路径不对 / 状态未知）任何模式下都算失败。
    hard_fail = [r for r in results
                 if r["verdict"] in (KEY_INVALID, UNREACHABLE, PATH_WRONG, UNKNOWN)]
    failed = hard_fail + ([r for r in results if r["verdict"] == MISSING]
                          if settings.is_dify_live else [])
    ok_all = not failed

    if args.json:
        print(json.dumps({**header, "results": results, "ok": ok_all},
                         ensure_ascii=False, indent=2))
        return 0 if ok_all else 1

    print("=" * 72)
    print(f"  Dify 接线校验   mode={header['mode']}   base_url={header['base_url']}")
    print(f"  目标类型：{'本机/内网' if header['target_is_local'] else '公网'}"
          f"   走系统代理：{'是' if header['use_proxy'] else '否'}"
          f"（httpx trust_env）")
    print("=" * 72)
    if header.get("hint"):
        print(f"\n  [提示] {header['hint']}\n")

    width = max(len(r["app"]) for r in results)
    for r in results:
        good = r["verdict"] == OK
        note = ""
        if r["verdict"] == MISSING and not settings.is_dify_live:
            note = "（mock 模式下正常）"
        mark = "[OK]  " if good else ("[--]  " if note else "[FAIL]")
        print(f"  {mark} {r['app'].ljust(width)}  {VERDICT_TEXT[r['verdict']]}{note}"
              f"   {r['masked_key']}")
        if not good and not note:
            print(f"         └─ {r['detail']}")

    print()
    print("=" * 72)
    if failed:
        verdicts = {r["verdict"] for r in failed}
        print(f"  结论：{len(failed)}/{len(results)} 个应用没通过")
        if verdicts == {PATH_WRONG}:
            print("  提示：全部 404，几乎可以肯定是 DIFY_BASE_URL 写错了 ——")
            print("        Dify 的服务 API 需要带版本前缀，例如 http://<host>/v1")
        elif verdicts == {UNREACHABLE}:
            print("  提示：全部连不上。自查顺序：容器/服务起没起 → 端口通不通")
            print("        → DIFY_BASE_URL 的 host 对不对（容器内互访别写 127.0.0.1）。")
        elif UNKNOWN in verdicts and header["use_proxy"]:
            print("  提示：出现了非预期状态码，且当前**走系统代理**。")
            print("        如果 Dify 在内网/本机，代理会把请求接走（常见 502/403）：")
            print("        在 .env 里加 DIFY_TRUST_ENV_PROXY=false 再试。")
        if MISSING in verdicts:
            print("  缺 Key 的应用："
                  f"{', '.join(r['app'] for r in failed if r['verdict'] == MISSING)}")
            print("        （live 模式必须 7 个都配齐，配一半会静默降级成 mock 回答）")
    else:
        if all(r["verdict"] == MISSING for r in results):
            # 一个 Key 都没配：脚本能证明的只是「现在没有东西可校验」，
            # 不能因此说「可以切 live」。
            print("  结论：还没有配 Key —— 先在 Dify 控制台为 7 个应用各建一个 API Key，")
            print("        填进 .env 的 DIFY_APP_KEYS 后重跑本脚本；")
            print("        等这里全部 [OK]，再把 DIFY_MODE 改成 live。")
        else:
            print("  结论：全部通过 ✓ 可以放心把 DIFY_MODE 切到 live")
    print("=" * 72)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
