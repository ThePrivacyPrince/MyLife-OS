"""Engine/session setup and schema initialization."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
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
            if url is None or ":memory:" not in url and url != "sqlite://":
                dbapi_conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
    return engine


def init_db(engine: Engine) -> None:
    """Create tables (idempotent) and sync the Five Pillars from config."""
    Base.metadata.create_all(engine)
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
