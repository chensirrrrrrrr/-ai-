# -*- coding: utf-8 -*-
"""生产配置守卫：把「能跑起来」和「能安全上线」分开检查。

为什么单独一个脚本
------------------
`launcher --selfcheck` 回答的是「服务能不能跑起来」，它 **不会** 告诉你
「密钥还是仓库里的默认值」—— 那玩意儿跑得挺欢，只是谁都能伪造 JWT。
本脚本只做静态检查：读配置（含 .env），按生产口径逐条判定，不连库、不发请求。

判定口径
--------
- 🔴 **FAIL（必须改）**：留着会出事 —— 默认密钥、debug 开着对外、凭证缺失；
- 🟡 **WARN（按需改）**：视部署形态而定 —— 比如 SQLite 在小团队内可接受，
  但上多人/多进程就要换 MySQL；
- ✅ **PASS**。

用法
----
    python scripts/check_prod_config.py              # 人读
    python scripts/check_prod_config.py --json       # 机器读（CI / 启动器）
    python scripts/check_prod_config.py --strict     # WARN 也算失败

退出码：0 = 无 FAIL（--strict 时也无 WARN）；1 = 有 FAIL（或 --strict 下有 WARN）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DIFY_APP_NAMES, settings            # noqa: E402

FAIL, WARN, PASS = "FAIL", "WARN", "PASS"

# config.py 里写死的「开发用默认值」—— 上线前必须换掉
DEFAULT_SECRETS = {
    "secret_key": "dev-only-secret-change-me-0123456789abcdef",
    "tool_shared_secret": "dev-only-tool-secret-change-me-0123456789",
}


def _rules() -> List[Tuple[str, str, str, str]]:
    """返回 (级别, 项目, 结论, 建议)。"""
    items: List[Tuple[str, str, str, str]] = []

    # ---- 密钥 ----
    key = settings.secret_key
    if key in DEFAULT_SECRETS.values() or key == DEFAULT_SECRETS["secret_key"]:
        items.append((FAIL, "SECRET_KEY", "仍是仓库默认值",
                      "生成新值：python -c \"import secrets;print(secrets.token_urlsafe(48))\""))
    elif len(key) < 32:
        items.append((FAIL, "SECRET_KEY", f"长度不足（{len(key)} < 32）",
                      "JWT 签名密钥至少 32 字节，建议 48"))
    else:
        items.append((PASS, "SECRET_KEY", "已自定义且长度合规", ""))

    tool_secret = settings.tool_shared_secret
    if tool_secret == DEFAULT_SECRETS["tool_shared_secret"]:
        items.append((FAIL, "TOOL_SHARED_SECRET", "仍是仓库默认值",
                      "内部工具 HMAC 签名密钥必须换（等价于「谁能调我的写操作」）"))
    else:
        items.append((PASS, "TOOL_SHARED_SECRET", "已自定义", ""))

    # ---- Dify ----
    if settings.is_dify_live:
        missing = settings.dify_missing_keys
        if missing:
            items.append((FAIL, "DIFY_APP_KEYS", f"live 模式下缺 {len(missing)} 个：{'、'.join(missing)}",
                          "补齐 Key，或先切回 mock（缺 Key 会静默降级，答复看起来正常）"))
        else:
            items.append((PASS, "DIFY_APP_KEYS", f"live 模式，{len(DIFY_APP_NAMES)} 个 Key 齐备", ""))
        if not settings.dify_tool_key:
            items.append((WARN, "DIFY_TOOL_KEY", "未配置（自定义工具静态 Key 通道关闭）",
                          "若 Dify 侧走「自定义工具」而非 HMAC，需要它；走 HTTP 节点 + HMAC 则不必"))
        else:
            items.append((PASS, "DIFY_TOOL_KEY", "已配置", ""))
    else:
        items.append((WARN, "DIFY_MODE", "mock（本地编排，不接真实模型）",
                      "生产演示/上线需切 live 并配齐 Key；mock 是受支持的运行形态，不是错误"))

    # ---- 运行形态 ----
    if settings.debug:
        items.append((FAIL, "DEBUG", "开启（异常详情外泄 + CORS 全放通）", "生产必须 DEBUG=false"))
    else:
        items.append((PASS, "DEBUG", "关闭", ""))

    if not settings.debug and "*" in (settings.cors_origins or []):
        items.append((FAIL, "CORS_ORIGINS", '含 "*" 且 DEBUG=false',
                      "生产要登记前端域名白名单"))
    elif settings.debug:
        items.append((PASS, "CORS_ORIGINS", "DEBUG=true 下全放通（本地联调用）", ""))
    else:
        items.append((PASS, "CORS_ORIGINS", f"白名单 {len(settings.cors_origins)} 条", ""))

    if settings.is_sqlite:
        items.append((WARN, "DATABASE_URL", "SQLite（单进程，无并发写保护）",
                      "多人/多 worker 部署换 MySQL：DATABASE_URL=mysql+pymysql://..."))
    else:
        items.append((PASS, "DATABASE_URL", "MySQL", ""))

    # ---- 新增的安全项（与代码能力对应）----
    if not settings.rate_limit_enabled:
        items.append((FAIL, "RATE_LIMIT_ENABLED", "限流关闭",
                      "登录可被爆破、LLM 对话可被刷，生产必须开启"))
    else:
        items.append((PASS, "限流", f"开启（登录 {settings.rate_limit_auth_per_min}/min·IP，"
                                    f"对话 {settings.rate_limit_chat_per_min}/min·账号）", ""))

    if settings.field_encryption_key:
        items.append((PASS, "字段加密", "开启（客户手机号密文存储 + HMAC 检索）", ""))
    else:
        items.append((WARN, "FIELD_ENCRYPTION_KEY", "未配置 → 客户手机号明文落库",
                      "配置 Fernet key 后跑 scripts/encrypt_backfill.py 回填存量（先备份库）"))

    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="生产配置守卫")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--strict", action="store_true", help="WARN 也视为失败")
    args = parser.parse_args()

    items = _rules()
    fails = [i for i in items if i[0] == FAIL]
    warns = [i for i in items if i[0] == WARN]

    if args.json:
        payload: Dict[str, Any] = {
            "items": [{"level": lv, "name": name, "detail": detail, "advice": advice}
                      for lv, name, detail, advice in items],
            "fail": len(fails), "warn": len(warns),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        icons = {FAIL: "🔴", WARN: "🟡", PASS: "✅"}
        print("生产配置检查（静态，不连库不发请求）\n")
        for level, name, detail, advice in items:
            print(f"{icons[level]} [{level:4}] {name:<20} {detail}")
            if advice:
                print(f"              └ 建议：{advice}")
        print(f"\n合计：{len(fails)} 项必须改（FAIL），{len(warns)} 项按需改（WARN），"
              f"{len(items) - len(fails) - len(warns)} 项通过。")
        if fails:
            print("结论：🔴 不能直接上线，先处理 FAIL 项。")
        elif warns:
            print("结论：🟡 可以跑，但 WARN 项建议按部署形态确认。")
        else:
            print("结论：✅ 配置符合生产口径。")

    if fails:
        return 1
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
