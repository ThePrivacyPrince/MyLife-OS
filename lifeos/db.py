"""Engine/session setup and schema initialization."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from lifeos.config import get_settings, load_pillars
from lifeos.models import Base, Pillar


def make_engine(url: str | None = None) -> Engine:
    engine = create_engine(url or get_settings().database_url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # enforce foreign keys in SQLite
            if url is None or (":memory:" not in url and url != "sqlite://"):
                dbapi_conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
    return engine


# Columns added after Milestone 1 shipped. Existing databases get them added in place (nullable, so no
# existing row changes). Full Alembic migrations arrive with Milestone 2.
ADDITIVE_COLUMNS = [
    ("goals", "vision_id", "INTEGER REFERENCES visions(id)"),
    ("tasks", "key_result_id", "INTEGER REFERENCES key_results(id)"),
    ("recurring_templates", "key_result_id", "INTEGER REFERENCES key_results(id)"),
    ("acquisition_candidates", "market_value", "VARCHAR(40)"),
    ("acquisition_candidates", "cash_ready", "BOOLEAN DEFAULT 0"),
    ("funds", "apr", "VARCHAR(40)"),
    ("funds", "min_payment", "VARCHAR(40)"),
    ("funds", "due_day", "INTEGER"),
    ("funds", "autopay", "BOOLEAN DEFAULT 0"),
    ("funds", "debt_status", "VARCHAR(14) DEFAULT 'active'"),
    ("funds", "owner", "VARCHAR(10) DEFAULT 'personal'"),
    ("funds", "holder", "VARCHAR(120) DEFAULT ''"),
    ("funds", "first_delinquency", "DATE"),
    ("funds", "validated", "BOOLEAN DEFAULT 0"),
    ("funds", "settle_open", "VARCHAR(40)"),
    ("funds", "settle_target", "VARCHAR(40)"),
    ("funds", "settle_walkaway", "VARCHAR(40)"),
]


def _add_missing_columns(engine: Engine) -> list[str]:
    added = []
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in ADDITIVE_COLUMNS:
            if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added.append(f"{table}.{column}")
    return added


def init_db(engine: Engine) -> None:
    """Create tables (idempotent), upgrade older databases, and sync the Five Pillars from config."""
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    with Session(engine) as s:
        for i, p in enumerate(load_pillars()):
            row = s.get(Pillar, p["code"]) or Pillar(code=p["code"])
            row.name, row.purpose = p["name"], p.get("purpose", "")
            row.subdomains, row.sort_order = p.get("subdomains", []), i
            s.add(row)
        s.commit()


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def unit_of_work(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any error."""
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
