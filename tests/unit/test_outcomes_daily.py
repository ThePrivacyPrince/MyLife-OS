"""OUT-* / DCK-* / CAP-* : weekly outcomes, the evening daily check, and calendar-aware capacity."""
import datetime as dt

import pytest
from sqlalchemy import select

from lifeos.errors import ReasonRequired, ValidationError
from lifeos.models import PlanEvent, Task
from lifeos.services import daily_check as D
from lifeos.services import outcomes as O
from lifeos.services import planner_cal as P
from lifeos.services import weekly as W
from lifeos.services.planner import plan_day

MON = dt.date(2026, 11, 23)
t = dt.time


# ---------------------------------------------------------------- outcomes
def test_out01_max_three_and_finish_line_required(s):
    for i in range(3):
        O.add_outcome(s, any_day=MON + dt.timedelta(days=2), title=f"Outcome {i}")
    assert all(o.week_start == MON for o in O.outcomes_for_week(s, MON))
    with pytest.raises(ValidationError, match="at most 3"):
        O.add_outcome(s, any_day=MON, title="Fourth")
    with pytest.raises(ValidationError):
        O.add_outcome(s, any_day=MON + dt.timedelta(days=7), title="  ")


def test_out02_close_rules(s):
    o = O.add_outcome(s, any_day=MON, title="First pay date confirmed in writing")
    with pytest.raises(ReasonRequired, match="evidence"):
        O.close_outcome(s, o.id, "achieved")
    O.update_outcome(s, o.id, evidence="HR email: first pay Fri Dec 4")
    O.close_outcome(s, o.id, "achieved")
    assert o.status == "achieved"
    p = O.add_outcome(s, any_day=MON, title="Winter stipend amount in writing")
    with pytest.raises(ReasonRequired):
        O.close_outcome(s, p.id, "missed")
    O.close_outcome(s, p.id, "missed", "Athletic director out until Monday")
    with pytest.raises(ValidationError):
        O.close_outcome(s, p.id, "achieved", "x")
    assert s.scalars(select(PlanEvent).where(PlanEvent.entity_type == "outcome")).all()


def test_out03_waiting_needs_a_follow_up_and_can_be_unparked(s):
    o = O.add_outcome(s, any_day=MON, title="Stipend confirmed")
    O.update_outcome(s, o.id, waiting_until=MON + dt.timedelta(days=3), today=MON)
    assert (o.status, o.follow_up_date) == ("waiting", MON + dt.timedelta(days=3))
    with pytest.raises(ValidationError):
        O.update_outcome(s, o.id, waiting_until=MON - dt.timedelta(days=1), today=MON)
    O.update_outcome(s, o.id, waiting_until=None)
    assert o.status == "open"


def test_out04_carry_is_explicit_and_streaks_show(s):
    o = O.add_outcome(s, any_day=MON, title="Fleet list for six slots")
    n1 = O.carry_outcome(s, o.id, "Two listings still unanswered")
    n2 = O.carry_outcome(s, n1.id)
    assert o.status == "carried" and n2.week_start == MON + dt.timedelta(days=14)
    assert O.carry_streak(s, n2) == 2
    assert O.outcomes_for_week(s, MON) == [] and len(O.outcomes_for_week(s, MON, include_carried=True)) == 1


def test_out05_weekly_review_top3_become_next_weeks_outcomes(s):
    W.save_weekly_review(s, MON, MON + dt.timedelta(days=6), focus=["Pay date in writing", "Stipend clarity", ""])
    nxt = O.outcomes_for_week(s, MON + dt.timedelta(days=7))
    assert [o.title for o in nxt] == ["Pay date in writing", "Stipend clarity"]
    W.save_weekly_review(s, MON, MON + dt.timedelta(days=6), focus=["Pay date in writing", "Stipend clarity"])
    assert len(O.outcomes_for_week(s, MON + dt.timedelta(days=7))) == 2, "re-saving doesn't duplicate"
    assert W.current_focus(s, MON + dt.timedelta(days=8)) == ["Pay date in writing", "Stipend clarity"]


# ---------------------------------------------------------------- daily check
@pytest.fixture
def outcome(s):
    return O.add_outcome(s, any_day=MON, title="Know when money is coming in", pillar="FIN")


def test_dck01_progress_creates_or_plans_a_real_task(s, mk, outcome):
    tue = MON + dt.timedelta(days=1)
    c = D.save_check(s, for_date=tue, today=MON, outcome_id=outcome.id, new_task_title="Call RXO payroll")
    task = s.get(Task, c.progress_task_id)
    assert (task.title, task.scheduled_date, task.pillar_code) == ("Call RXO payroll", tue, "FIN")
    assert outcome.next_task_id == task.id
    existing = mk("Check phone bill", pillar="FIN")
    c2 = D.save_check(s, for_date=tue, today=MON, outcome_id=outcome.id, task_id=existing.id)
    assert c2.id == c.id, "one check per day - saving again updates it"
    assert existing.scheduled_date == tue and existing.events[-1].event_type == "planned"
    with pytest.raises(ValidationError):
        D.save_check(s, for_date=tue, today=MON, outcome_id=outcome.id)


@pytest.mark.parametrize("kw", [dict(ask_who="Seller", ask_area="price", ask_what="$300 off"),  # no script
                                dict(ask_who="Seller", ask_area="cheaper", ask_what="x", ask_script="y")])
def test_dck02_an_ask_must_be_concrete(s, kw):
    with pytest.raises(ValidationError):
        D.save_check(s, for_date=MON + dt.timedelta(days=1), today=MON, **kw)


def test_dck03_ask_result_and_nothing_to_ask_is_valid(s):
    tue = MON + dt.timedelta(days=1)
    D.save_check(s, for_date=tue, today=MON, ask_who="VX seller (Craigslist)", ask_area="price",
                 ask_what="$4,200 instead of $4,800 with the trailer", ask_script="I can pick it up Saturday with cash...")
    with pytest.raises(ReasonRequired):
        D.record_ask(s, tue, "got")
    c = D.record_ask(s, tue, "partial", "$4,450 with trailer")
    assert c.ask_result == "partial"
    wed = tue + dt.timedelta(days=1)
    D.save_check(s, for_date=wed, today=MON, no_ask=True)
    with pytest.raises(ValidationError):
        D.record_ask(s, wed, "got", "x")
    stats = D.week_check_stats(s, MON, MON + dt.timedelta(days=6))
    assert (stats["checks_done"], stats["asks_planned"], stats["asks_partial"]) == (2, 1, 1)


def test_dck04_pre_decided_moves_when_the_day_is_full(s, mk):
    tue = MON + dt.timedelta(days=1)
    a = mk("Outline torts", scheduled_date=tue)
    b = mk("Organize receipts", scheduled_date=tue, priority=3)
    with pytest.raises(ValidationError):
        D.save_check(s, for_date=tue, today=MON, bump_task_ids=[999])
    D.save_check(s, for_date=tue, today=MON, bump_task_ids=[b.id], protect_note="Track practice 4-6, sleep after shift")
    moved = D.move_bumped(s, tue, today=tue)
    assert moved == [b] and b.scheduled_date == tue + dt.timedelta(days=1) and a.scheduled_date == tue
    assert "pre-decided" in b.events[-1].reason


def test_dck05_cannot_plan_the_past_and_never_miss_twice(s):
    with pytest.raises(ValidationError):
        D.save_check(s, for_date=MON - dt.timedelta(days=1), today=MON)
    assert D.missed_twice(s, MON) is False  # never used yet - no nagging
    D.save_check(s, for_date=MON - dt.timedelta(days=5), today=MON - dt.timedelta(days=6))
    assert D.missed_twice(s, MON) is True
    D.save_check(s, for_date=MON, today=MON)
    assert D.missed_twice(s, MON) is False


# ---------------------------------------------------------------- capacity
def test_cap01_overnight_shift_splits_across_midnight(s):
    P.add_event(s, title="RXO overnight", day=MON, start_time=t(23), end_time=t(7), kind="work")
    blocks = P.blocks_between(s, MON, MON)
    assert blocks[0].overnight and blocks[0].minutes == 480 and "(overnight)" in blocks[0].time_label
    assert P.capacity_for(s, MON).booked == 60
    assert P.capacity_for(s, MON + dt.timedelta(days=1)).booked == 420
    ag = P.agenda(s, MON, 2, today=MON)
    assert ag[1].booked_minutes == 420


def test_cap02_capacity_is_awake_minus_calendar_minus_buffer_capped(s):
    tue = MON + dt.timedelta(days=1)
    free = P.capacity_for(s, tue)
    assert (free.awake, free.available) == (960, 180), "a free day is capped at what you'd really do"
    P.add_event(s, title="Track practice", day=tue, start_time=t(15), end_time=t(18), kind="training")
    P.add_event(s, title="Overnight", day=tue, start_time=t(22), end_time=t(6), kind="work")
    D.save_check(s, for_date=tue, today=MON, awake_hours=8)
    cap = P.capacity_for(s, tue)
    assert (cap.awake, cap.booked, cap.awake_overridden) == (480, 300, True)
    assert cap.available == 60  # 480 - 300 - 120


def test_cap03_plan_day_uses_real_capacity(s, mk):
    P.add_event(s, title="Double shift", day=MON, start_time=t(6), end_time=t(20), kind="work")
    mk("Big task", priority=1, estimated_minutes=60)
    plan = plan_day(s, MON)
    assert plan.capacity_minutes == 0 and plan.overcommitted
    assert any("calendar 14.0h" in w for w in plan.warnings)
