"""静态校验前端 ES Module 的 import/export 一致性。

不引依赖、不跑浏览器：用正则在几个 js 文件里对一遍「具名导入」是否真的存在。
抓的是最容易致命的崩点 —— 导入了不存在的符号，浏览器里表现为整页白屏，
而且错误只在控制台可见，很容易漏。

用法：
    python scripts/check_frontend_imports.py                 # 默认 ../frontend
    python scripts/check_frontend_imports.py --dir src/web   # 指定目录
退出码 0 = 全部一致，1 = 有问题。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "frontend"

EXPORT_FN = re.compile(r"export\s+(?:async\s+)?function\s+(\w+)")
EXPORT_CLASS = re.compile(r"export\s+class\s+(\w+)")
EXPORT_DECL = re.compile(r"export\s+(?:const|let|var)\s+(\w+)")
EXPORT_LIST = re.compile(r"export\s*\{([^}]*)\}")
EXPORT_DEFAULT = re.compile(r"export\s+default\b")
IMPORT_STMT = re.compile(r"import\s+([^'\"]+?)\s+from\s+['\"]([^'\"]+)['\"]", re.S)


def exports_of(path: Path) -> tuple[set[str], bool]:
    src = path.read_text(encoding="utf-8")
    names: set[str] = set()
    names |= set(EXPORT_FN.findall(src))
    names |= set(EXPORT_CLASS.findall(src))
    names |= set(EXPORT_DECL.findall(src))
    for group in EXPORT_LIST.findall(src):
        for part in group.split(","):
            part = part.strip()
            if not part:
                continue
            name = part.split(" as ")[-1].strip()
            if name:
                names.add(name)
    return names, bool(EXPORT_DEFAULT.search(src))


def _check_named(clause_inner: str, names: set[str], where: str, spec: str,
                 problems: list[str]) -> None:
    for part in clause_inner.split(","):
        part = part.strip()
        if not part:
            continue
        imported = part.split(" as ")[0].strip()
        if imported not in names:
            problems.append(f"{where}  ->  {spec} 未导出 `{imported}`")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验前端 ES Module 的 import/export 一致性")
    parser.add_argument("--dir", default=str(DEFAULT_ROOT),
                        help=f"前端源码目录（默认 {DEFAULT_ROOT}）")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if not root.exists():
        print(f"[x] 找不到前端目录：{root}")
        return 1

    files = sorted(root.rglob("*.js"))
    table = {f: exports_of(f) for f in files}
    problems: list[str] = []
    total_imports = 0

    for f in files:
        where = f.relative_to(root)
        src = f.read_text(encoding="utf-8")
        for clause, spec in IMPORT_STMT.findall(src):
            if not spec.startswith("."):
                continue                                  # 裸模块名，跳过
            total_imports += 1
            target = (f.parent / spec).resolve()
            if target not in table:
                problems.append(f"{where}  ->  找不到模块 {spec}")
                continue

            names, has_default = table[target]
            clause = clause.strip()

            if clause.startswith("{"):
                _check_named(clause.strip("{} "), names, where, spec, problems)
            elif clause.startswith("*"):
                continue                                  # 命名空间导入
            else:
                default_name = clause.split(",")[0].strip()
                if default_name and not has_default:
                    problems.append(
                        f"{where}  ->  {spec} 没有 default 导出（导入名 {default_name}）")
                if "," in clause and "{" in clause:
                    inner = clause[clause.index("{") + 1: clause.rindex("}")]
                    _check_named(inner, names, where, spec, problems)

    print(f"扫描文件 {len(files)} 个，跨模块 import {total_imports} 处")
    if problems:
        print(f"\n[FAIL] 发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n[OK] 所有具名导入都能在被导入模块中找到对应导出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
