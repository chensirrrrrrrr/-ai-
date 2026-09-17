# -*- coding: utf-8 -*-
"""存量库的「补列」回归测试。

为什么值得单开一个文件：`create_all` 对已存在的表是空操作，不会补列。
演示库 `data/ai_assistant.db` 是上一版建好的，模型加了字段以后如果不
`--reseed` 就会直接 `no such column` —— 也就是说，这条补列逻辑一坏，
所有「不重建库直接升级」的场景全挂，而且报错点在启动后的第一个查询里，
很难一眼看出是 schema 的事。

这里用子进程 + 临时 sqlite 造一个**缺列的老表**，跑真正的 init_db()，
再验证：① 缺的列被补上；② 老数据没被动过；③ NOT NULL 的新列有默认值。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]

LEGACY_COLUMNS = {
    "review_status", "ai_conclusion", "reviewed_at", "review_remark",
    "missing_fields", "source_name", "batch_id",
}

SCRIPT = textwrap.dedent('''
    import os, sqlite3, sys
    os.environ["DATABASE_URL"] = "sqlite:///{db_posix}"
    os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdefghijklmn"
    sys.path.insert(0, {root!r})

    con = sqlite3.connect({db!r})
    con.execute("""
        CREATE TABLE lead_screening (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            source_type VARCHAR(16) NOT NULL,
            raw_file_url VARCHAR(512),
            extracted_fields JSON,
            hit_products JSON,
            conclusion VARCHAR(32) NOT NULL,
            evidence JSON,
            confidence NUMERIC(5,4),
            reviewed_by INTEGER,
            dify_run_id VARCHAR(64),
            created_at DATETIME NOT NULL
        )
    """)
    con.execute("INSERT INTO lead_screening "
                "(id, source_type, conclusion, created_at) "
                "VALUES (1, 'TEXT', '符合', '2026-01-01 09:00:00')")
    con.commit()
    con.close()

    from sqlalchemy import inspect
    from app.db import engine, init_db

    init_db()

    columns = {{c["name"] for c in inspect(engine).get_columns("lead_screening")}}
    missing = {need!r} - columns
    assert not missing, "init_db 没补上这些列: %s" % sorted(missing)

    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "select review_status, ai_conclusion, conclusion from lead_screening where id = 1"
        ).fetchone()
    assert row[0] == "PENDING", "新增 NOT NULL 列必须落到模型声明的默认值，实际 %r" % (row[0],)
    assert row[1] is None, "可空新列应为 NULL，实际 %r" % (row[1],)
    assert row[2] == "符合", "老数据不能被改动，实际 %r" % (row[2],)

    # 幂等：再跑一次不应该报错/重复加列
    init_db()
    print("LEGACY-OK")
''')


def test_init_db_backfills_columns_on_legacy_table(tmp_path):
    db_file = tmp_path / "legacy.db"
    script = SCRIPT.format(db=str(db_file), db_posix=db_file.as_posix(),
                           root=str(ROOT), need=LEGACY_COLUMNS)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "LEGACY-OK" in proc.stdout
