# -*- coding: utf-8 -*-
"""`app/db.py` 的工具函数单测（补列 / 默认值渲染 / SQLite 目录）。

为什么值得补：`_sync_missing_columns` 这条补列逻辑的回归原来只走**子进程**
（tests/test_schema_migration.py），所以本进程覆盖率里它整段是空的（db.py 一度只有 65%）。
但它是「不重建库直接升级」的唯一保障，值得在本进程里也钉住：

- 空库跑一轮 → 所有表都「不存在」，直接跳过（不报错、不加工）；
- 缺列的老表 → 只 ADD COLUMN、NOT NULL 列带上模型声明的默认值、老数据不动；
- 幂等 → 再跑一次返回空列表。
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect

from app import db as dbmod


# --------------------------------------------------------------------------- #
# _ensure_sqlite_dir
# --------------------------------------------------------------------------- #
def test_ensure_sqlite_dir_skips_non_sqlite():
    """MySQL 的 URL 直接返回，不去 mkdir 一个叫 mysql+pymysql 的目录。"""
    dbmod._ensure_sqlite_dir("mysql+pymysql://user:pw@127.0.0.1:3306/ai")


def test_ensure_sqlite_dir_skips_memory_and_bare():
    """内存库与空路径没有父目录，跳过。"""
    dbmod._ensure_sqlite_dir("sqlite:///:memory:")
    dbmod._ensure_sqlite_dir("sqlite://")


def test_ensure_sqlite_dir_creates_parent(tmp_path):
    target = tmp_path / "deep" / "nested" / "ai.db"
    assert not target.parent.exists()
    dbmod._ensure_sqlite_dir(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir()


# --------------------------------------------------------------------------- #
# _literal_default
# --------------------------------------------------------------------------- #
def test_literal_default_returns_none_without_scalar_default():
    assert dbmod._literal_default(sa.Column("a", sa.String)) is None
    # callable（如 default=dict / datetime.now）没法写成简单 DEFAULT 子句
    assert dbmod._literal_default(sa.Column("b", sa.JSON, default=dict)) is None
    # 标量但不是 bool/int/float/str
    assert dbmod._literal_default(sa.Column("c", sa.LargeBinary, default=b"raw")) is None


def test_literal_default_renders_scalars():
    assert dbmod._literal_default(sa.Column("a", sa.Boolean, default=True)) == "1"
    assert dbmod._literal_default(sa.Column("b", sa.Boolean, default=False)) == "0"
    assert dbmod._literal_default(sa.Column("c", sa.Integer, default=5)) == "5"
    assert dbmod._literal_default(sa.Column("d", sa.Float, default=1.5)) == "1.5"
    assert dbmod._literal_default(sa.Column("e", sa.String, default="PENDING")) == "'PENDING'"


def test_literal_default_escapes_single_quote():
    """默认值里带单引号必须转义，否则拼出来的 DDL 直接语法错误。"""
    assert dbmod._literal_default(sa.Column("a", sa.String, default="O'Brien")) == "'O''Brien'"


# --------------------------------------------------------------------------- #
# _sync_missing_columns
# --------------------------------------------------------------------------- #
def test_sync_missing_columns_noop_when_no_model_table_exists(monkeypatch, tmp_path):
    """空库：所有模型表都「不存在」→ 全部跳过，返回空清单（不建表，建表是 create_all 的事）。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    monkeypatch.setattr(dbmod, "engine", eng)

    assert dbmod._sync_missing_columns() == []


def test_sync_missing_columns_backfills_and_is_idempotent(monkeypatch, tmp_path):
    """缺列的老表：补回缺失列、NOT NULL 列带默认值、可空列留 NULL、再跑一次为空。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    dbmod.Base.metadata.create_all(bind=eng)

    # 造一个「上一版建好的表」：删掉一个 NOT NULL 列与一个可空列。
    # ⚠️ review_status 带索引，SQLite 要求先 DROP INDEX 才能 DROP COLUMN。
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_lead_screening_review_status")
        conn.exec_driver_sql("ALTER TABLE lead_screening DROP COLUMN review_status")
        conn.exec_driver_sql("ALTER TABLE lead_screening DROP COLUMN source_name")

    before = {c["name"] for c in inspect(eng).get_columns("lead_screening")}
    assert "review_status" not in before and "source_name" not in before

    monkeypatch.setattr(dbmod, "engine", eng)
    added = dbmod._sync_missing_columns()

    assert "lead_screening.review_status" in added
    assert "lead_screening.source_name" in added

    after = {c["name"] for c in inspect(eng).get_columns("lead_screening")}
    assert {"review_status", "source_name"} <= after

    # 幂等：列都在了就不该再补
    assert dbmod._sync_missing_columns() == []


def test_sync_missing_columns_does_not_touch_existing_rows(monkeypatch, tmp_path):
    """补列不能改老数据：老行在新列上取默认值/NULL，原有列原样保留。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'rows.db').as_posix()}")
    dbmod.Base.metadata.create_all(bind=eng)

    # 先退化成「缺 review_status 的老表」，再塞一条老数据
    with eng.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_lead_screening_review_status")
        conn.exec_driver_sql("ALTER TABLE lead_screening DROP COLUMN review_status")
        conn.exec_driver_sql(
            "INSERT INTO lead_screening (id, source_type, conclusion, created_at) "
            "VALUES (1, 'TEXT', '符合', '2026-01-01 09:00:00')")

    monkeypatch.setattr(dbmod, "engine", eng)
    assert "lead_screening.review_status" in dbmod._sync_missing_columns()

    with eng.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT review_status, conclusion FROM lead_screening WHERE id = 1").fetchone()
    assert row[0] == "PENDING", f"NOT NULL 新列应落到模型声明的默认值，实际 {row[0]!r}"
    assert row[1] == "符合", "老数据不能被改动"
