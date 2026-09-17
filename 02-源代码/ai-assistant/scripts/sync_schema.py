# -*- coding: utf-8 -*-
"""给**存量库**补上新表 / 新列（只加不改不删），不重建、不灌数据。

为什么需要它
------------
`create_all` 对已存在的表是空操作，不会补列。所以「模型加了字段 → 老库
`no such column` → 必须 `--reset` 重建」曾经是唯一出路，而重建会**清空演示数据**。
对已经交付、已经有真实数据的库，这条出路不可接受。

于是把 dev 库的演进拆成两步：
- `scripts/seed.py`        —— 建表 + 灌演示数据（会动数据，慎重）
- `scripts/sync_schema.py` —— **只同步结构**，一行业务数据都不碰 ✅

用法：
    python scripts/sync_schema.py            # 补新表 + 补缺失列
    python scripts/sync_schema.py --check    # 只看差什么，不做任何改动（CI 用）

安全边界（与 `app/db._sync_missing_columns` 一致）：
- 只处理「模型里声明了、库里没有」的表与列；
- 只发 `CREATE TABLE` / `ALTER TABLE ... ADD COLUMN`，
  绝不 `DROP` / 绝不改列类型 / 绝不改可空性；
- 生产环境仍建议上 Alembic 做版本化演进，本脚本是「让不重建库也能跑起来」的兜底。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect                              # noqa: E402

from app.db import Base, engine                             # noqa: E402


def _diff() -> dict:
    """算出「模型有、库里没有」的表与列。"""
    from app import models                                  # noqa: F401  注册模型

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing_tables, missing_columns = [], []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            missing_tables.append(table.name)
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in present and not column.primary_key:
                missing_columns.append(f"{table.name}.{column.name}")
    return {"tables": missing_tables, "columns": missing_columns}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步存量库的结构（只加不改不删）")
    parser.add_argument("--check", action="store_true",
                        help="只检查差异，不做任何改动；有差异返回码 1")
    args = parser.parse_args()

    before = _diff()
    total = len(before["tables"]) + len(before["columns"])
    print(f"[sync] 目标库：{engine.url}")
    if not total:
        print("[sync] 结构已是最新，无需改动")
        return 0

    for name in before["tables"]:
        print(f"[sync] 缺表：{name}")
    for name in before["columns"]:
        print(f"[sync] 缺列：{name}")

    if args.check:
        print(f"[sync] --check：共 {total} 处差异（未做改动）")
        return 1

    from app.db import init_db

    added = init_db()
    after = _diff()
    print(f"[sync] 已建表 {len(before['tables'])} 张、补列 {len(added)} 个")
    if added:
        print("[sync] 补上的列：" + "、".join(added))
    if after["tables"] or after["columns"]:
        print(f"[sync] ⚠️ 仍有差异：表 {after['tables']}、列 {after['columns']}",
              file=sys.stderr)
        return 2
    print("[sync] 完成：结构与模型一致（未触碰任何业务数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
