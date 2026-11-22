"""CAS-* : the planning cascade (3-5 yr Vision <- 6-12 mo Goals <- 3 mo Objectives <- daily/weekly Key Results)."""
import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, strategies as st
from sqlalchemy import select

from lifeos.engines.cascade import goal_horizon_warnings, kr_status, period_bounds, periods_in, vision_horizon_warnings
from lifeos.enums import TaskStatus as S
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import PlanEvent, Task
from lifeos.services import cascade as C
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services.seed import seed

SUN = dt.date(2026, 11, 22)
MON = dt.date(2026, 11, 16)


# ---------------------------------------------------------------- pure rules
def test_cas01_vision_horizon_is_3_to_5_years(today):
    assert vision_horizon_warnings(today, 2030, 3, 5) == []
    assert "closer to a goal" in vision_horizon_warnings(today, 2027, 3, 5)[0]
    assert "beyond" in vision_horizon_warnings(today, 2040, 3, 5)[0]
    assert vision_horizon_warnings(today, None, 3, 5)


def test_cas02_goal_horizon_is_6_to_12_months(today):
    assert goal_horizon_warnings(today, today + dt.timedelta(days=270), 6, 12) == []
    assert "objective-sized" in goal_horizon_warnings(today, today + dt.timedelta(days=60), 6, 12)[0]
    assert "beyond" in goal_horizon_warnings(today, today + dt.timedelta(days=700), 6, 12)[0]


def test_cas03_periods_daily_and_monday_to_sunday_weeks():
    assert period_bounds("daily", SUN) == (SUN, SUN)
    assert period_bounds("weekly", SUN) == (MON, SUN)
    assert period_bounds("weekly", MON) == (MON, SUN)
    with pytest.raises(ValidationError):
        period_bounds("monthly", MON)


@given(st.integers(0, 120), st.integers(0, 120), st.sampled_from(["daily", "weekly"]))
def test_cas04_periods_tile_the_range_without_gaps(a, b, cadence):
    lo, hi = MON + dt.timedelta(days=min(a, b)), MON + dt.timedelta(days=max(a, b))
    ps = periods_in(cadence, lo, hi)
    assert ps[0][0] <= lo and ps[-1][1] >= hi
    for (s1, e1), (s2, _) in zip(ps, ps[1:]):
        assert s2 == e1 + dt.timedelta(days=1)


def test_cas05_kr_status_paces_against_elapsed_time():
    week = (MON, SUN)
    assert kr_status(Decimal(4), Decimal(4), week, MON).status == "met"
    assert kr_status(Decimal(0), Decimal(4), week, MON).status == "on_track"      # day 1 of 7
    assert kr_status(Decimal(1), Decimal(4), week, dt.date(2026, 11, 20)).status == "behind"
    assert kr_status(Decimal(3), Decimal(4), week, dt.date(2026, 11, 23)).status == "missed"
    assert kr_status(Decimal(0), Decimal(1), (SUN, SUN), SUN).status == "on_track"  # daily: not behind until over


# ---------------------------------------------------------------- building the tiers
@pytest.fixture
def stack(s, today):
    """A full cascade: vision <- goal <- objective <- weekly KR."""
    vision, _ = C.create_vision(s, pillar="FIN", title="Debt-free with 6 months of reserves", target_year=2030,
                                today=today)
    goal, _ = C.create_goal(s, pillar="FIN", title="All tax returns filed", vision_code=vision.code,
                            target_date=today + dt.timedelta(days=240), today=today)
    obj = C.create_objective(s, goal_code=goal.code, title="Know exactly what I owe", start_date=MON)
    kr = C.create_key_result(s, objective_code=obj.code, title="Tax-paperwork sessions", cadence="weekly", target=3)
    return vision, goal, obj, kr


def test_cas06_codes_are_traceable(stack):
    vision, goal, obj, kr = stack
    assert vision.code == "FIN-V1"
    assert goal.code == "FIN-G001"
    assert obj.code == "FIN-G001-O1" and kr.code == "FIN-G001-O1-KR1"


def test_cas07_goal_numbering_continues_existing_codes(s, today):
    seed(s, today)
    g, warnings = C.create_goal(s, pillar="FIN", title="Emergency fund", target_date=today + dt.timedelta(days=300),
                                today=today)
    assert g.code == "FIN-G006"
    assert any("vision" in w for w in warnings)


def test_cas08_objective_window_is_about_three_months(s, stack):
    _, goal, obj, _ = stack
    assert (obj.end_date - obj.start_date).days == 91
    with pytest.raises(ValidationError, match="goal level"):
        C.create_objective(s, goal_code=goal.code, title="Too long", start_date=MON,
                           end_date=MON + dt.timedelta(days=200))
    with pytest.raises(ValidationError):
        C.create_objective(s, goal_code=goal.code, title="Backwards", start_date=MON,
                           end_date=MON - dt.timedelta(days=1))


@pytest.mark.parametrize("kw", [dict(cadence="monthly"), dict(target=0), dict(target="abc"),
                                dict(target="NaN"), dict(measure="vibes"), dict(title=" ")])
def test_cas09_key_result_validation(s, stack, kw):
    obj = stack[2]
    base = dict(objective_code=obj.code, title="x", cadence="weekly", target=1)
    base.update(kw)
    with pytest.raises(ValidationError):
        C.create_key_result(s, **base)


def test_cas10_unknown_codes_rejected(s, mk):
    with pytest.raises(NotFound):
        C.create_objective(s, goal_code="NOPE", title="x", start_date=MON)
    with pytest.raises(ValidationError):
        mk(key_result_code="NOPE-KR1")


# ---------------------------------------------------------------- the arrows: progress flows up
def test_cas11_lineage_walks_all_the_way_up(s, mk, stack):
    vision, goal, obj, kr = stack
    t = mk("Find 2023 W-2", key_result_code=kr.code)
    assert [(l.tier, l.code) for l in C.lineage(t)] == [
        ("key_result", kr.code), ("objective", obj.code), ("goal", goal.code), ("vision", vision.code)]


def test_cas12_completions_count_toward_this_weeks_key_result(s, mk, stack):
    kr = stack[3]
    from lifeos.config import get_settings
    today = get_settings().today()
    for i in range(3):
        t = mk(f"Session {i}", key_result_code=kr.code)
        T.transition(s, t.id, S.COMPLETED, today=today)
    _, status = C.kr_progress(s, kr, today)
    assert status.actual == 3 and status.status == "met"


def test_cas13_routine_link_flows_to_open_and_future_occurrences_only(s, stack, today):
    kr = stack[3]
    tpl = R.create_template(s, title="Paperwork", pillar="FIN", priority=2, frequency="daily", start_date=today)
    R.generate_occurrences(s, today)
    first = tpl.occurrences[0]
    T.transition(s, first.id, S.COMPLETED, today=today)
    C.link_template(s, tpl.id, kr.code)
    assert first.key_result_id is None, "completed history keeps the link it was completed under"
    R.generate_occurrences(s, today + dt.timedelta(days=1))
    new = s.scalar(select(Task).where(Task.template_id == tpl.id, Task.occurrence_date == today + dt.timedelta(days=1)))
    assert new.key_result_id == kr.id


def test_cas14_manual_check_ins_use_exact_decimals(s, stack):
    obj = stack[2]
    kr = C.create_key_result(s, objective_code=obj.code, title="Saved", cadence="weekly", target="50.00",
                             measure="manual", unit="USD")
    for v in ("0.10", "0.20"):
        C.check_in(s, kr.code, MON, v)
    assert C.kr_actual(s, kr, MON, SUN) == Decimal("0.30")  # floats would give 0.30000000000000004
    with pytest.raises(ValidationError):
        C.check_in(s, stack[3].code, MON, 1)  # completion-measured KRs can't be hand-entered


def test_cas15_objective_health_counts_finished_weeks(s, mk, stack):
    obj, kr = stack[2], stack[3]
    for i in range(3):  # 3 sessions in week 1 (target 3), none in week 2
        t = mk(f"s{i}", key_result_code=kr.code)
        T.transition(s, t.id, S.COMPLETED, today=MON)
        t.completed_at = dt.datetime(2026, 11, 18, 17, 0)  # Wed of week 1 (UTC, still Wed locally)
    health = C.objective_health(s, obj, dt.date(2026, 12, 1))  # weeks of Nov 16 and Nov 23 are finished
    assert (health.met, health.total) == (1, 2) and health.rate == 0.5


def test_cas16_status_changes_need_reasons_and_are_audited(s, stack):
    obj = stack[2]
    with pytest.raises(ReasonRequired):
        C.set_status(s, "objective", obj.code, "achieved", "")
    with pytest.raises(ValidationError):
        C.set_status(s, "objective", obj.code, "crushed-it", "yay")
    C.set_status(s, "objective", obj.code, "achieved", "IRS transcript shows balance")
    ev = s.scalars(select(PlanEvent).where(PlanEvent.entity_code == obj.code)).all()[-1]
    assert (ev.from_value, ev.to_value, ev.reason) == ("active", "achieved", "IRS transcript shows balance")


# ---------------------------------------------------------------- gaps
def test_cas17_gaps_point_at_broken_arrows(s, today):
    seed(s, today)
    gaps = C.cascade_gaps(s, today)
    msgs = " | ".join(g.message for g in gaps)
    assert msgs.count("no 3-5 year vision") == 5
    assert "isn't connected to a vision" in msgs and "no objective for this quarter" in msgs


def test_cas18_complete_chain_has_no_gaps_for_it(s, mk, stack, today):
    vision, goal, obj, kr = stack
    mk("Session", key_result_code=kr.code)
    codes = {g.code for g in C.cascade_gaps(s, MON)}
    assert not codes & {vision.code, goal.code, obj.code, kr.code}


def test_cas19_finished_objective_must_be_closed_out(s, stack):
    obj = stack[2]
    gaps = C.cascade_gaps(s, obj.end_date + dt.timedelta(days=1))
    assert any(g.code == obj.code and "close it out" in g.message for g in gaps)


def test_cas20_goal_can_be_relinked_with_audit(s, stack, today):
    _, goal, _, _ = stack
    v2, _ = C.create_vision(s, pillar="FIN", title="Own my home", target_year=2031, today=today)
    C.link_goal_to_vision(s, goal.code, v2.code, "taxes are a step to a mortgage")
    assert goal.vision.code == v2.code
