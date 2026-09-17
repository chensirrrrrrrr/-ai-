"""一键全量回归：单元/集成 → 起服体检 → HTTP 冒烟 → 真实浏览器 E2E。

为什么要有这个脚本
------------------
四层验证此前是**四段手动命令**：先 `pytest`，再 `start.bat`，然后 `smoke_test.py`，
最后 `node frontend_e2e.mjs`。任何人漏跑一层都看不出来，于是「改完代码测一下」这个动作
的可靠性完全取决于记性。本脚本把它们串成一条命令，并把结果落成
`reports/regression_report.md`，**可归档、可比对**。

复用而非重写
------------
第 2~4 层直接调 `launcher.py --selfcheck --e2e` —— 起服/收服/端口冲突处理/E2E 的
「必须同进程」坑都已经在 launcher 里解决过了（见 `scripts/launcher.py` 的
`run_frontend_e2e` 注释：服务是子进程，Shell 一退服务就没了）。
这里只负责**编排**和**出报告**，不重复实现。

用法
----
    python scripts/regression.py              # 四层全跑
    python scripts/regression.py --no-e2e     # 跳过浏览器层（快约 2 分钟）
    python scripts/regression.py --no-cov     # pytest 不带覆盖率统计
    python scripts/regression.py --unit-only  # 只跑单元/集成层

退出码：0 全绿；1 有层失败；2 环境/参数问题。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPORT = PROJECT / "reports" / "regression_report.md"

# 调本机一律绕开系统代理（本机 127.0.0.1 被代理接走会 502，踩过）
_ENV = dict(os.environ)
_ENV["NO_PROXY"] = "127.0.0.1,localhost"
_ENV["no_proxy"] = "127.0.0.1,localhost"


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(PROJECT), env=_ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- #
# 第 1 层：单元 / 集成
# --------------------------------------------------------------------------- #
def run_unit(with_cov: bool) -> dict:
    cmd = [sys.executable, "-m", "pytest"]
    if with_cov:
        cmd += ["--cov=app", "--cov-report=term"]
    started = time.time()
    code, out = _run(cmd, timeout=900)
    passed = failed = 0
    if m := re.search(r"(\d+) passed", out):
        passed = int(m.group(1))
    if m := re.search(r"(\d+) failed", out):
        failed = int(m.group(1))
    cov = None
    if with_cov and (m := re.search(r"^TOTAL\s+\d+\s+\d+\s+(\d+)%", out, re.M)):
        cov = int(m.group(1))
    return {
        "name": "单元 / 集成（pytest" + (" + 覆盖率" if with_cov else "") + "）",
        "ok": code == 0 and failed == 0 and passed > 0,
        "detail": f"{passed} passed" + (f" / {failed} failed" if failed else "")
                  + (f" / 覆盖率 {cov}%" if cov is not None else ""),
        "seconds": round(time.time() - started, 1),
    }


# --------------------------------------------------------------------------- #
# 第 2~4 层：起服体检 + 冒烟 + 浏览器 E2E（全部委托给 launcher）
# --------------------------------------------------------------------------- #
def run_selfcheck(with_e2e: bool, backend_port: int, frontend_port: int) -> dict:
    cmd = [sys.executable, str(PROJECT / "scripts" / "launcher.py"), "--selfcheck",
           "--backend-port", str(backend_port), "--frontend-port", str(frontend_port)]
    if with_e2e:
        cmd.append("--e2e")
    started = time.time()
    code, out = _run(cmd, timeout=1200)

    passed = len(re.findall(r"\[OK\]", out))
    failed_items = [ln.strip() for ln in out.splitlines() if "[FAIL]" in ln]
    return {
        "name": "起服体检 + HTTP 冒烟" + (" + 浏览器 E2E" if with_e2e else ""),
        "ok": code == 0 and not failed_items,
        "detail": f"{passed} 项通过" + (f" / {len(failed_items)} 项失败" if failed_items else ""),
        "failed_lines": failed_items[:10],
        "seconds": round(time.time() - started, 1),
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def write_report(layers: list[dict], with_e2e: bool, with_cov: bool) -> Path:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_ok = all(layer["ok"] for layer in layers)
    lines = [
        "# 全量回归报告",
        "",
        f"- **执行时间**：{now}",
        f"- **执行方式**：`python scripts/regression.py`"
        + ("" if with_e2e else " --no-e2e")
        + ("" if with_cov else " --no-cov"),
        f"- **结论**：{'✅ 全部通过' if all_ok else '❌ 有层未通过'}",
        "",
        "| 层 | 结果 | 明细 | 耗时 |",
        "|---|---|---|---|",
    ]
    for layer in layers:
        lines.append(f"| {layer['name']} | {'✅' if layer['ok'] else '❌'} "
                     f"| {layer['detail']} | {layer['seconds']}s |")
    for layer in layers:
        if layer.get("failed_lines"):
            lines += ["", f"### 失败明细：{layer['name']}", ""]
            lines += [f"- {ln}" for ln in layer["failed_lines"]]
    lines += ["", "> 本文件由 `scripts/regression.py` 自动生成，每次全量回归会整体覆盖。", ""]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return REPORT


def main() -> int:
    parser = argparse.ArgumentParser(description="一键全量回归（单元 → 体检 → 冒烟 → E2E）")
    parser.add_argument("--no-e2e", action="store_true", help="跳过真实浏览器 E2E 层")
    parser.add_argument("--no-cov", action="store_true", help="pytest 不做覆盖率统计")
    parser.add_argument("--unit-only", action="store_true", help="只跑单元/集成层")
    parser.add_argument("--backend-port", type=int, default=8010)
    parser.add_argument("--frontend-port", type=int, default=8020)
    args = parser.parse_args()

    layers: list[dict] = []
    print("=" * 64)
    print("  全量回归：单元/集成 → 起服体检 → HTTP 冒烟 → 浏览器 E2E")
    print("=" * 64)

    with_cov = not args.no_cov
    print("\n[1/2] 单元 / 集成（pytest）…")
    unit = run_unit(with_cov)
    layers.append(unit)
    print(f"      {'✅' if unit['ok'] else '❌'} {unit['detail']}（{unit['seconds']}s）")

    if not args.unit_only:
        print("\n[2/2] 起服体检 + HTTP 冒烟" + ("" if args.no_e2e else " + 浏览器 E2E") + "…")
        layer = run_selfcheck(not args.no_e2e, args.backend_port, args.frontend_port)
        layers.append(layer)
        print(f"      {'✅' if layer['ok'] else '❌'} {layer['detail']}（{layer['seconds']}s）")
        for ln in layer.get("failed_lines", []):
            print(f"        - {ln}")

    report = write_report(layers, not args.no_e2e and not args.unit_only, with_cov)
    all_ok = all(layer["ok"] for layer in layers)

    print("\n" + "=" * 64)
    for layer in layers:
        print(f"  {'✅' if layer['ok'] else '❌'} {layer['name']}：{layer['detail']}")
    print("=" * 64)
    print(f"  结论：{'全部通过 ✓' if all_ok else '有层未通过 ✗'}")
    print(f"  报告：{report}")
    print("=" * 64)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
