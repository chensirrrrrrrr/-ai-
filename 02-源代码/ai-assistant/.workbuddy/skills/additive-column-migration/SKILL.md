---
name: additive-column-migration
description: 给「已经跑了、有真实数据」的库加字段而不重建——用只加不改的 ADD COLUMN 补列迁移保证升级后不再 no such column，并用「造一个缺列的老库 + 子进程跑 init_db」的方式写回归测试。当模型加了新列、或要把改动交付给已有数据库的环境时使用。
agent_created: true
---

# 不重建库地给表加字段

## 什么时候需要它

只要出现「模型加了列，但目标环境里的库是上一版建好的」，就会有这个问题：

- ORM 一般用 `Base.metadata.create_all()` 建表，而它**对已存在的表是空操作**，
  不会补列。于是启动后第一个查询就 `no such column: xxx`。
- 演示/内网环境的常规操作是 `--reseed`（drop_all + create_all）把数据全清掉重建，
  这在**演示库上没问题，在有真实数据的环境上不可接受**。
- 彻底方案是 Alembic，但为「加几个可空列」引入迁移框架通常是过度工程。

中间那条路就是：**只做加法的补列同步**，放在 `init_db()` 里，幂等、失败不阻断启动。

## 实现要点（踩过的坑都在这里）

```python
def _sync_missing_columns() -> list[str]:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    dialect, preparer = engine.dialect, engine.dialect.identifier_preparer
    added = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present or column.primary_key:
                continue
            ddl = (f"ALTER TABLE {preparer.quote(table.name)} "
                   f"ADD COLUMN {preparer.quote(column.name)} "
                   f"{column.type.compile(dialect=dialect)}")
            if not column.nullable:
                ddl += " NOT NULL"
            literal = _literal_default(column)      # 见下
            if literal:
                ddl += f" DEFAULT {literal}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
            except Exception as exc:                # 补列失败不该把启动搞挂
                logger.warning("补列失败 %s.%s：%s", table.name, column.name, exc)
    return added
```

**1. `NOT NULL` 的列必须有默认值，而且顺序不能反。**
SQLite 直接报 `Cannot add a NOT NULL column with default value NULL`。
把模型上的 **Python 侧默认值**渲染成 SQL 字面量即可：

```python
def _literal_default(column):
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None                     # callable(如 datetime.now) / dict / list 一律跳过
    value = default.arg
    if isinstance(value, bool):   return "1" if value else "0"
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, str):    return "'" + value.replace("'", "''") + "'"
    return None
```
👉 所以想加一个「非空且要有初值」的列，**必须写成 `mapped_column(String(16), default="PENDING", nullable=False)`**，
不能只靠 `server_default` 或干脆不写默认值。

**2. 只加不改不删。** 不处理改类型/改名/加索引/加约束 —— 那些确实该上 Alembic。
名字里带 `additive` 就是为了让下一个人知道边界在哪。

**3. 表名/列名要用 `identifier_preparer.quote()`**，别手写引号：SQLite 认双引号、
MySQL 默认只认反引号。

**4. 每个表的列清单只取一次**，不要在循环里反复 `get_columns`。

**5. 失败只告警不抛。** 补列失败时的正确行为是「服务照常起，日志里有告警」，
因为用户可能只是想先看看，而不是每次启动都崩。

## 回归测试：造一个缺列的老库

这段逻辑最要命的地方在于：**它坏了，测试全绿**（测试库每次 `drop_all` 都是新 schema），
线上却是启动后第一个查询就炸。所以要专门造一个「老 schema」来打它 ——
而且必须**在子进程里跑**，因为 `settings` / `engine` 是进程级单例。

```python
SCRIPT = textwrap.dedent('''
    import os, sqlite3, sys
    os.environ["DATABASE_URL"] = "sqlite:///{db_posix}"
    sys.path.insert(0, {root!r})

    con = sqlite3.connect({db!r})
    con.execute("""CREATE TABLE lead_screening (
        id INTEGER PRIMARY KEY, source_type VARCHAR(16) NOT NULL,
        conclusion VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL)""")
    con.execute("INSERT INTO lead_screening VALUES (1,'TEXT','符合','2026-01-01')")
    con.commit(); con.close()

    from sqlalchemy import inspect
    from app.db import engine, init_db
    init_db()

    columns = {{c["name"] for c in inspect(engine).get_columns("lead_screening")}}
    assert not {need!r} - columns, "没补上: %s" % sorted({need!r} - columns)
    with engine.connect() as conn:
        row = conn.exec_driver_sql("select review_status, conclusion from lead_screening").fetchone()
    assert row[0] == "PENDING"          # 非空新列落到默认值
    assert row[1] == "符合"             # 老数据没被动过
    init_db()                           # 幂等：再跑一次不报错
    print("LEGACY-OK")
''')

def test_init_db_backfills_columns_on_legacy_table(tmp_path):
    db_file = tmp_path / "legacy.db"
    proc = subprocess.run([sys.executable, "-c", SCRIPT.format(...)],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LEGACY-OK" in proc.stdout
```

三条断言缺一不可：① 列补上了 ② **老数据还在** ③ 非空新列拿到默认值；
再加一次重复调用验证幂等。

## 附：同一类思路——测试别往仓库塞二进制夹具

补列测试要造「老库」，解析类测试要造 PDF/XLSX，做法同源：**用标准库现场生成**。

- 合法 xlsx = 一个 zip：`[Content_Types].xml` + `xl/sharedStrings.xml` + `xl/worksheets/sheet1.xml`。
  单元格用 `t="s"` + 共享字符串下标，20 行代码就够。
- 合法 PDF 的关键是 **`/Length` 必须等于内容流真实字节数、xref 偏移要对**。
  偷懒写错会让 `pdfplumber` 越界读到下一个对象，正文变乱码，
  然后你会花半小时怀疑自己的解析器有 bug。
- 好处：① 不进二进制文件、不用 Git LFS；② 文本内容可控，断言好写；
  ③ 顺带把「没装 openpyxl 时的标准库兜底路径」也一并测了。

```python
# 造 xlsx（放在 tests/material_fixtures.py 这类非 test_ 前缀的模块里，避免被 pytest 收集）
def make_xlsx(rows): ...        # zipfile + 共享字符串
def make_pdf(lines): ...        # 未压缩内容流 + 正确 /Length + 正确 xref
```
