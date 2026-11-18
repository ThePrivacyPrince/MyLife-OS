"""REC-* : recurring tasks."""
import datetime as dt

import pytest
from hypothesis import given, strategies as st
from sqlalchemy import select

from lifeos.engines.recurrence import build_rule, occurrence_dates
from lifeos.enums import TaskStatus as S
from lifeos.errors import ValidationError
from lifeos.models import Task
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services.planner import plan_day

MON = dt.date(2026, 11, 16)


def test_rec01_rule_builders():
    assert build_rule("daily") == "FREQ=DAILY"
    assert build_rule("weekdays", weekdays=["mo", "WE"]) == "FREQ=WEEKLY;BYDAY=MO,WE"
    assert build_rule("biweekly", weekdays=["FR"]) == "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"
    assert build_rule("monthly", month_day=1) == "FREQ=MONTHLY;BYMONTHDAY=1"
    for bad in [("hourly", {}), ("weekdays", {}), ("weekly", {"weekdays": ["XX"]}), ("custom", {})]:
        with pytest.raises(ValidationError):
            build_rule(bad[0], **bad[1])
    with pytest.raises(ValidationError):
        build_rule("custom", custom="FREQ=NOPE")


def test_rec02_weekday_and_biweekly_dates():
    mwf = occurrence_dates(build_rule("weekdays", weekdays=["MO", "WE", "FR"]), MON, MON, MON + dt.timedelta(days=13))
    assert [d.strftime("%a") for d in mwf] == ["Mon", "Wed", "Fri"] * 2
    bi = occurrence_dates(build_rule("biweekly", weekdays=["MO"]), MON, MON, MON + dt.timedelta(days=35))
    assert [(d - MON).days for d in bi] == [0, 14, 28]


def test_rec03_dst_transition_produces_each_day_once():
    start = dt.date(2026, 10, 30)  # DST ends Nov 1, 2026 in the US
    days = occurrence_dates("FREQ=DAILY", start, start, dt.date(2026, 11, 4))
    assert days == [start + dt.timedelta(days=i) for i in range(6)]


@given(st.integers(0, 60), st.integers(0, 60))
def test_rec04_dates_sorted_unique_and_in_window(a, b):
    lo, hi = MON + dt.timedelta(days=min(a, b)), MON + dt.timedelta(days=max(a, b))
    days = occurrence_dates("FREQ=WEEKLY;BYDAY=TU,SA", MON, lo, hi)
    assert days == sorted(set(days)) and all(lo <= d <= hi for d in days)


def _tpl(s, **kw):
    base = dict(title="Bedroom reset", pillar="ENV", priority=2, frequency="daily", start_date=MON)
    base.update(kw)
    return R.create_template(s, **base)


def test_rec05_completing_today_does_not_complete_future(s):
    tpl = _tpl(s)
    R.generate_occurrences(s, MON)
    today_occ = s.scalar(select(Task).where(Task.template_id == tpl.id))
    T.transition(s, today_occ.id, S.COMPLETED, today=MON)
    R.generate_occurrences(s, MON + dt.timedelta(days=2))
    occs = list(s.scalars(select(Task).where(Task.template_id == tpl.id).order_by(Task.occurrence_date)))
    assert [o.status for o in occs] == ["completed", "pending", "pending"]
    assert len({o.id for o in occs}) == 3


def test_rec06_generation_is_idempotent(s):
    tpl = _tpl(s)
    R.generate_occurrences(s, MON + dt.timedelta(days=3))
    R.generate_occurrences(s, MON + dt.timedelta(days=3))
    tpl.generated_through = None  # even after losing the watermark, no duplicates
    R.generate_occurrences(s, MON + dt.timedelta(days=3))
    assert len(list(s.scalars(select(Task).where(Task.template_id == tpl.id)))) == 4


def test_rec07_exceptions_and_end_date(s):
    tpl = _tpl(s, end_date=MON + dt.timedelta(days=4))
    R.add_exception(s, tpl.id, MON + dt.timedelta(days=1))
    R.generate_occurrences(s, MON + dt.timedelta(days=6))  # within the 7-day backfill window
    dates = sorted(s.scalars(select(Task.occurrence_date).where(Task.template_id == tpl.id)))
    assert dates == [MON, MON + dt.timedelta(days=2), MON + dt.timedelta(days=3), MON + dt.timedelta(days=4)]
    with pytest.raises(ValidationError):
        _tpl(s, end_date=MON - dt.timedelta(days=1))


def test_rec08_mark_missed_records_never_deletes(s):
    tpl = _tpl(s, missed_policy="mark_missed")
    R.generate_occurrences(s, MON)
    missed = R.apply_missed_policy(s, MON + dt.timedelta(days=1))
    assert len(missed) == 1
    occ = missed[0]
    assert occ.status == S.MISSED and occ.template_id == tpl.id
    assert occ.events[-1].actor == "system" and "mark_missed" in occ.events[-1].reason


def test_rec09_carry_over_stays_open_and_overdue(s):
    _tpl(s, title="Laundry", frequency="weekly", weekdays=["MO"], missed_policy="carry_over")
    plan = plan_day(s, MON)
    plan = plan_day(s, MON + dt.timedelta(days=2))
    assert [t.title for t in plan.overdue] == ["Laundry"]
    assert plan.queue[2][0].overdue


def test_rec10_backfill_is_capped(s):
    _tpl(s, start_date=MON - dt.timedelta(days=365))
    created = R.generate_occurrences(s, MON)
    assert len(created) == 8  # max_backfill_days (7) + today


def test_rec11_paused_template_generates_nothing(s):
    tpl = _tpl(s)
    R.set_active(s, tpl.id, False)
    assert R.generate_occurrences(s, MON + dt.timedelta(days=5)) == []
