import datetime as dt

import pytest
from sqlalchemy.orm import Session

from lifeos.db import init_db, make_engine
from lifeos.services.tasks import create_task

TODAY = dt.date(2026, 11, 18)  # fixed clock for deterministic tests


@pytest.fixture
def engine():
    eng = make_engine("sqlite://")
    init_db(eng)
    return eng


@pytest.fixture
def s(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def today():
    return TODAY


@pytest.fixture
def mk(s):
    def _mk(title="Task", pillar="FIN", priority=2, **kw):
        return create_task(s, title=title, pillar=pillar, priority=priority, **kw)
    return _mk
