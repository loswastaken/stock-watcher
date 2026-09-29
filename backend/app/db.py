"""SQLite engine/session plumbing + lightweight schema migrations."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("stockwatcher.db")


class Base(DeclarativeBase):
    pass


# One stable sessionmaker; init_engine() (re)binds it.
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)
_engine: Engine | None = None


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=10000")
    finally:
        cur.close()


def init_engine(url: str | None = None) -> Engine:
    global _engine
    if _engine is not None:
        _engine.dispose()
    if url is None:
        s = get_settings()
        s.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{s.db_path}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 15},
        pool_size=10,
        max_overflow=20,
        future=True,
    )
    event.listen(engine, "connect", _set_sqlite_pragmas)
    _engine = engine
    SessionLocal.configure(bind=engine)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def get_db() -> Iterator[Session]:
    if _engine is None:
        init_engine()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sql_default(col) -> str | None:
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return None
    v = d.arg
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return None


def add_missing_columns(engine: Engine) -> None:
    """Add columns that exist in the models but not in the DB (SQLite ALTER TABLE ADD COLUMN).

    New columns are added nullable (SQLite can't add NOT NULL without a default); scalar
    Python defaults are mirrored as SQL DEFAULTs so existing rows get sensible values.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = (
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                    f"{col.type.compile(dialect=engine.dialect)}"
                )
                default = _sql_default(col)
                if default is not None:
                    ddl += f" DEFAULT {default}"
                log.info("migrating: %s", ddl)
                conn.exec_driver_sql(ddl)
            # indexes declared on the model (e.g. index=True on a newly added column)
            existing_ix = {ix["name"] for ix in insp.get_indexes(table.name)}
            for ix in table.indexes:
                if ix.name and ix.name not in existing_ix:
                    log.info("migrating: CREATE INDEX %s", ix.name)
                    ix.create(conn, checkfirst=True)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    engine = get_engine()
    Base.metadata.create_all(engine)
    add_missing_columns(engine)
