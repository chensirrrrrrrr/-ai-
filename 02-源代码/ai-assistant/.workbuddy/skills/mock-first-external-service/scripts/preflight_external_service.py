"""外部服务接线预检（通用版）——**在把 MODE 从 mock 切成 live 之前先跑这个**。

为什么需要它
------------
live 模式下外部调用失败会被降级到 mock（这是「不出现无响应」的设计），
但这个兜底有个副作用：**Key 写错 / 地址写错 / 应用没建**，用户拿到的是 mock 回答，
却以为「已经接上了」—— 从答复里根本看不出来。所以切 live 前必须能**被证明**接上了。

它按应用逐个探，把结论摊开：ok / key_invalid / missing / unreachable / path_wrong / unknown。

用法（把下面 CONFIG 段改成你的项目，或直接改这个文件里的常量）
----------------------------------------------------------------
    python preflight_external_service.py                  # 检查全部应用
    python preflight_external_service.py --app router     # 只查一个
    python preflight_external_service.py --json           # 机器可读（CI / 启动器）
    python preflight_external_service.py --timeout 8

退出码：0 = 全部通过；1 = 有问题；2 = 用法/配置错误。

不联网自测
----------
`check_one(..., transport=...)` 支持注入 `httpx.MockTransport`，
生产传 `None` 走真实网络 —— 这样每条判定分支都能被单测覆盖，不依赖外部服务是否活着。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

# --------------------------------------------------------------------------- #
# CONFIG：改成你的项目
# --------------------------------------------------------------------------- #
APP_NAMES: Tuple[str, ...] = ("router", "customer_service", "student_helper")

# 想省事就直接填；正规做法是从 app.config.settings 读（见下方 load_config()）
BASE_URL = "http://127.0.0.1/v1"
MODE = "mock"                     # mock | live
APP_KEYS: Dict[str, str] = {}     # {"router": "app-xxxx"}
TRUST_ENV_PROXY: Optional[bool] = None   # None = 自动（内网绕开）
TIMEOUT = 10.0


def load_config() -> None:
    """从项目配置里取（推荐）。把这段替换成你的 import 即可。"""
    global BASE_URL, MODE, APP_KEYS, TRUST_ENV_PROXY, TIMEOUT, APP_NAMES
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.config import DIFY_APP_NAMES, settings            # noqa: F401
    except Exception:                                              # noqa: BLE001
        return
    APP_NAMES = tuple(DIFY_APP_NAMES)
    BASE_URL = settings.dify_base_url
    MODE = settings.dify_mode
    APP_KEYS = dict(settings.dify_app_keys)
    TRUST_ENV_PROXY = settings.dify_trust_env_proxy
    TIMEOUT = settings.dify_timeout


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
OK = "ok"
KEY_INVALID = "key_invalid"
MISSING = "missing"
UNREACHABLE = "unreachable"
PATH_WRONG = "path_wrong"
UNKNOWN = "unknown"

VERDICT_TEXT = {
    OK: "通过",
    KEY_INVALID: "Key 无效（401/403）",
    MISSING: "env 里没配 Key",
    UNREACHABLE: "连不上",
    PATH_WRONG: "路径不对（404，检查 base_url 是否少了版本前缀，如 /v1）",
    UNKNOWN: "状态未知",
}


def _probe_chat(client: httpx.Client, key: str) -> Tuple[str, str]:
    """Chat / Chatflow / Agent 类：GET /parameters 是最省的一次鉴权探针。"""
    resp = client.get("/parameters", headers={"Authorization": f"Bearer {key}"})
    if resp.status_code == 200:
        return OK, "GET /parameters 200"
    if resp.status_code in (401, 403):
        return KEY_INVALID, f"GET /parameters {resp.status_code}"
    if resp.status_code == 404:
        return "", ""            # 交给 workflow 探测再判
    return UNKNOWN, f"GET /parameters {resp.status_code}"


def _probe_workflow(client: httpx.Client, key: str) -> Tuple[str, str]:
    """Workflow 类没有 /parameters，改用一个**故意不合法**的请求体探。

    只发 `{"response_mode":"blocking"}`：服务端会因为缺 `user` 直接 400/422，
    说明「路由存在 + 鉴权通过」，且**不会真的跑一遍工作流**（没有副作用、不烧 token）。
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


def _is_private(url: str) -> bool:
    import re
    return bool(re.match(r"^https?://(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|"
                         r"10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
                         r"172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|host\.docker\.internal)(?::\d+)?",
                         (url or "").strip(), re.IGNORECASE))


def check_one(app: str, timeout: float,
              transport: Optional[httpx.BaseTransport] = None) -> Dict[str, Any]:
    """探一个应用。`transport` 只给测试注入用（生产走真实网络）。"""
    key = (APP_KEYS.get(app) or "").strip()
    if not key:
        return {"app": app, "verdict": MISSING, "detail": "env 里没有这个应用的 Key",
                "masked_key": ""}

    masked = f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "…"
    trust = TRUST_ENV_PROXY if TRUST_ENV_PROXY is not None else (not _is_private(BASE_URL))
    with httpx.Client(base_url=BASE_URL.rstrip("/"), timeout=timeout, transport=transport,
                      trust_env=trust) as client:
        try:
            verdict, detail = _probe_chat(client, key)
            if not verdict:                                # 404 → 可能不是 chat 类应用
                verdict, detail = _probe_workflow(client, key)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return {"app": app, "verdict": UNREACHABLE, "masked_key": masked,
                    "detail": f"连不上 {BASE_URL}（{exc.__class__.__name__}）"}
        except httpx.TimeoutException:
            return {"app": app, "verdict": UNREACHABLE, "masked_key": masked,
                    "detail": f"超时（>{timeout:g}s）"}
        except Exception as exc:                           # noqa: BLE001
            return {"app": app, "verdict": UNKNOWN, "masked_key": masked,
                    "detail": f"{exc.__class__.__name__}: {exc}"}

    return {"app": app, "verdict": verdict, "masked_key": masked, "detail": detail}


def run(apps: Sequence[str], timeout: float = TIMEOUT) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
    is_live = MODE.lower() == "live"
    header = {
        "mode": MODE,
        "base_url": BASE_URL,
        "target_is_local": _is_private(BASE_URL),
        "use_proxy": TRUST_ENV_PROXY if TRUST_ENV_PROXY is not None else (not _is_private(BASE_URL)),
        "apps": list(apps),
    }
    if not is_live:
        header["hint"] = ("当前是 mock 模式，不会真连外部服务。"
                          "确认下面这些信息没问题后，把 MODE 改成 live 再跑一次。")

    results = [check_one(app, timeout) for app in apps]

    # 判定口径：
    # - 「没配 Key」在 mock 模式下是**正常状态**（本来就不需要），不算失败；
    #   一旦切到 live，它就是必须修的问题。
    # - 其余几种任何模式下都算失败。
    hard = [r for r in results
            if r["verdict"] in (KEY_INVALID, UNREACHABLE, PATH_WRONG, UNKNOWN)]
    failed = hard + ([r for r in results if r["verdict"] == MISSING] if is_live else [])
    return header, results, (not failed)


def main() -> int:
    load_config()
    parser = argparse.ArgumentParser(description="外部服务接线预检")
    parser.add_argument("--app", default=None, help="只检查某个应用名")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--timeout", type=float, default=TIMEOUT)
    args = parser.parse_args()

    apps: List[str] = list(APP_NAMES)
    if args.app:
        if args.app not in APP_NAMES:
            print(f"未知应用名 {args.app!r}；可选：{', '.join(APP_NAMES)}")
            return 2
        apps = [args.app]

    header, results, ok_all = run(apps, args.timeout)
    if args.json:
        print(json.dumps({**header, "results": results, "ok": ok_all},
                         ensure_ascii=False, indent=2))
        return 0 if ok_all else 1

    is_live = header["mode"].lower() == "live"
    print("=" * 72)
    print(f"  接线校验   mode={header['mode']}   base_url={header['base_url']}")
    print(f"  目标类型：{'本机/内网' if header['target_is_local'] else '公网'}"
          f"   走系统代理：{'是' if header['use_proxy'] else '否'}（httpx trust_env）")
    print("=" * 72)
    if header.get("hint"):
        print(f"\n  [提示] {header['hint']}\n")

    width = max(len(r["app"]) for r in results)
    for r in results:
        good = r["verdict"] == OK
        note = "（mock 模式下正常）" if (r["verdict"] == MISSING and not is_live) else ""
        mark = "[OK]  " if good else ("[--]  " if note else "[FAIL]")
        print(f"  {mark} {r['app'].ljust(width)}  {VERDICT_TEXT[r['verdict']]}{note}"
              f"   {r['masked_key']}")
        if not good and not note:
            print(f"         └─ {r['detail']}")

    failed = [r for r in results if r["verdict"] != OK
              and not (r["verdict"] == MISSING and not is_live)]
    print()
    print("=" * 72)
    if failed:
        verdicts = {r["verdict"] for r in failed}
        print(f"  结论：{len(failed)}/{len(results)} 个应用没通过")
        if verdicts == {PATH_WRONG}:
            print("  提示：全部 404，几乎可以肯定是 BASE_URL 写错了 ——")
            print("        服务 API 通常需要带版本前缀，例如 http://<host>/v1")
        elif verdicts == {UNREACHABLE}:
            print("  提示：全部连不上。自查顺序：容器/服务起没起 → 端口通不通")
            print("        → BASE_URL 的 host 对不对（容器内互访别写 127.0.0.1）。")
        elif UNKNOWN in verdicts and header["use_proxy"]:
            print("  提示：出现了非预期状态码，且当前**走系统代理**。")
            print("        如果目标是内网/本机，代理会把请求接走（常见 502/403）：")
            print("        显式关掉 trust_env（或设 NO_PROXY）再试。")
        if MISSING in verdicts:
            print("  缺 Key 的应用："
                  f"{', '.join(r['app'] for r in failed if r['verdict'] == MISSING)}")
            print("        （live 模式必须全部配齐，配一半会静默降级成 mock 回答）")
    elif all(r["verdict"] == MISSING for r in results):
        # 一个 Key 都没配：脚本能证明的只是「现在没有东西可校验」，不能说「可以切 live」
        print("  结论：还没有配 Key —— 先在控制台为每个应用建一个 API Key，")
        print("        填进 env 后重跑本脚本；等这里全部 [OK]，再把 MODE 改成 live。")
    else:
        print("  结论：全部通过 ✓ 可以放心把 MODE 切到 live")
    print("=" * 72)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
