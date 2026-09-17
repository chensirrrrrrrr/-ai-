"""数据库引擎与会话。

- 开发：SQLite（`sqlite:///./data/ai_assistant.db`），零外部依赖，文件即库。
- 生产：MySQL 8（`mysql+pymysql://...?charset=utf8mb4`），仅需改 DATABASE_URL。
两者共用同一套 SQLAlchemy 2.0 模型，类型差异通过 `with_variant` 抹平。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator, List, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 相对路径的父目录不存在时自动创建，否则连接直接失败。"""
    if not url.startswith("sqlite"):
        return
    raw = url.split("sqlite:///", 1)[-1]
    if raw in ("", ":memory:"):
        return
    Path(raw).resolve().parent.mkdir(parents=True, exist_ok=True)


def _build_engine() -> Engine:
    _ensure_sqlite_dir(settings.database_url)
    kwargs: dict = {"echo": settings.db_echo, "future": True, "pool_pre_ping": True}
    if settings.is_sqlite:
        # SQLite 默认禁止跨线程复用连接，FastAPI 线程池下必须放开
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=1800,   # 小于 MySQL 默认 wait_timeout
        )
    return create_engine(settings.database_url, **kwargs)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            expire_on_commit=False)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _record):  # pragma: no cover - 驱动回调
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _literal_default(column) -> Optional[str]:
    """把模型上的标量默认值渲染成 SQL 字面量。

    只认 bool/int/float/str；callable（如 `default=datetime.now`）和
    JSON 的 dict/list 一律返回 None —— 它们没法写成简单的 DEFAULT 子句，
    而那些列都是可空列，不需要默认值。
    """
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _sync_missing_columns() -> List[str]:
    """给**已存在**的表补上模型里新增的列（只做 ADD COLUMN，幂等，只加不改不删）。

    为什么需要这一步：`create_all` 对已经存在的表是空操作，不会补列。
    演示库 `data/ai_assistant.db` 是上一版建好的，模型加了字段以后如果不
    `--reseed` 就会直接 `no such column`。生产环境仍建议上 Alembic
    （见 `init_db` 注释），这里只是让「不重建库也能跑起来」这件事成立。

    安全边界：只处理模型里声明、库里缺失的列；失败只告警不抛出
    （补列失败不该把服务启动流程带崩）。返回补上的列清单，便于脚本/测试观察。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    dialect = engine.dialect
    preparer = dialect.identifier_preparer
    added: List[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present or column.primary_key:
                continue
            ddl = (
                f"ALTER TABLE {preparer.quote(table.name)} "
                f"ADD COLUMN {preparer.quote(column.name)} "
                f"{column.type.compile(dialect=dialect)}"
            )
            if not column.nullable:
                # ⚠️ 顺序不能反：SQLite 不允许「NOT NULL 且无默认值」的 ADD COLUMN
                # （Cannot add a NOT NULL column with default value NULL）。
                ddl += " NOT NULL"
            literal = _literal_default(column)
            if literal:
                ddl += f" DEFAULT {literal}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
            except Exception as exc:                      # noqa: BLE001 - 补列失败不阻断启动
                logger.warning("补列失败 %s.%s：%s", table.name, column.name, exc)

    if added:
        logger.info("已为存量库补上新列：%s", "、".join(added))
    return added


def init_db() -> List[str]:
    """建表 + 补列（幂等）。返回本次**新补上的列**清单。

    生产环境如需版本化演进，可在此基础上接 Alembic；这里的边界是
    「只加不改不删」——新表由 `create_all` 建，缺列由 `_sync_missing_columns` 补。

    返回值不是装饰：启动器与 `scripts/sync_schema.py` 会把它打出来，
    让「到底补了什么」变成可观察的事实，而不是一句「已同步」。
    """
    from . import models  # noqa: F401  导入以完成模型注册

    Base.metadata.create_all(bind=engine)
    return _sync_missing_columns()


def drop_all() -> None:
    from . import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
