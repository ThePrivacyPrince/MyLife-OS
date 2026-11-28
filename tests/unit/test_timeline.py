"""TML-* : countdowns, timelines and deadline discipline ("so I'm not here starting over")."""
import datetime as dt

import pytest
from sqlalchemy import select

from lifeos.engines.timeline import countdown, humanize_days, quarter_slots, vision_deadline
from lifeos.errors import ReasonRequired, ValidationError
from lifeos.models import PlanEvent
from lifeos.services import cascade as C
from lifeos.services.seed import seed

D = dt.date


# ---------------------------------------------------------------- pure
def test_tml01_countdown_states():
    start, end = D(2026, 11, 22), D(2027, 5, 31)
    c = countdown(start, end, D(2026, 11, 22))
    assert c.state == "on_clock" and c.days_left == 190 and c.elapsed_fraction == 0
    assert "190 days" in c.label
    assert countdown(start, end, D(2027, 5, 20)).state == "due_soon"
    assert countdown(start, end, end).state == "due_today"
    late = countdown(start, end, D(2027, 6, 3))
    assert late.state == "overdue" and late.elapsed_fraction == 1.0 and "past deadline" in late.label
    assert countdown(start, None, start).state == "no_deadline"
    assert countdown(D(2027, 1, 1), D(2027, 3, 31), D(2026, 12, 1)).state == "not_started"


def test_tml02_elapsed_fraction_halfway():
    c = countdown(D(2026, 1, 1), D(2026, 1, 11), D(2026, 1, 6))
    assert c.elapsed_fraction == 0.5


@pytest.mark.parametrize("days,text", [(1, "1 day"), (45, "45 days"), (190, "6 months"), (61, "2 months, 1 day"),
                                       (900, "2 years, 5 months"), (1500, "4 years, 1 month")])
def test_tml03_humanize(days, text):
    assert humanize_days(days) == text


def test_tml04_quarters_cover_the_runway_without_stubs():
    slots = quarter_slots(D(2026, 11, 22), D(2027, 11, 30))
    assert slots[0][0] == D(2026, 11, 22) and slots[-1][1] == D(2027, 11, 30)
    for (_, a_end), (b_start, _) in zip(slots, slots[1:]):
        assert b_start == a_end + dt.timedelta(days=1)
    assert all((hi - lo).days >= 20 for lo, hi in slots)
    assert len(slots) == 4
    assert vision_deadline(2030) == D(2030, 12, 31)


# ---------------------------------------------------------------- deadlines are required and sticky
def test_tml05_every_tier_needs_a_deadline(s, today):
    with pytest.raises(ValidationError, match="target year"):
        C.create_vision(s, pillar="FIN", title="Rich", target_year=None, today=today)
    with pytest.raises(ValidationError, match="deadline"):
        C.create_goal(s, pillar="FIN", title="Taxes", target_date=None, today=today)


@pytest.fixture
def goal(s, today):
    g, _ = C.create_goal(s, pillar="BUS", title="Four skis in the fleet", target_date=D(2027, 5, 31), today=today)
    g.created_at = dt.datetime.combine(today, dt.time(12))  # pin to the test clock, not the real one
    return g


def test_tml06_moving_a_deadline_needs_a_reason_and_is_counted(s, goal):
    with pytest.raises(ReasonRequired):
        C.change_deadline(s, "goal", goal.code, D(2027, 6, 30))
    C.change_deadline(s, "goal", goal.code, D(2027, 6, 30), "Stipend arrives later")
    C.change_deadline(s, "goal", goal.code, D(2027, 7, 31), "Repair costs")
    assert C.deadline_moves(s, "goal", goal.code) == 2
    ev = s.scalars(select(PlanEvent).where(PlanEvent.event_type == "deadline_moved")).all()
    assert [(e.from_value, e.to_value) for e in ev] == [("2027-05-31", "2027-06-30"), ("2027-06-30", "2027-07-31")]
    gaps = " ".join(g.message for g in C.cascade_gaps(s, D(2026, 11, 22)))
    assert "moved 2 times" in gaps


def test_tml07_first_deadline_on_old_goal_is_free(s, today):
    seed(s, today)  # seeded goals mostly have no deadline
    assert any("has no deadline" in g.message for g in C.cascade_gaps(s, today))
    C.change_deadline(s, "goal", "FIN-G003", D(2027, 4, 15))
    ev = s.scalars(select(PlanEvent).where(PlanEvent.entity_code == "FIN-G003")).all()[-1]
    assert ev.event_type == "deadline_set"
    with pytest.raises(ValidationError):
        C.change_deadline(s, "goal", "FIN-G003", D(2027, 4, 15), "same")


def test_tml08_objective_deadline_still_obeys_three_months(s, goal, today):
    o = C.create_objective(s, goal_code=goal.code, title="Buy ski #1", start_date=today)
    with pytest.raises(ValidationError):
        C.change_deadline(s, "objective", o.code, today + dt.timedelta(days=200), "need more time")


def test_tml09_passed_deadline_must_be_closed_out(s, goal):
    gaps = C.cascade_gaps(s, D(2027, 6, 1))
    assert any(g.code == goal.code and "passed its deadline" in g.message for g in gaps)


# ---------------------------------------------------------------- the timeline keeps the chain going
def test_tml10_goal_timeline_shows_planned_and_unplanned_quarters(s, goal, today):
    C.create_objective(s, goal_code=goal.code, title="Buy ski #1", start_date=today)
    slots = C.goal_timeline(goal, today)
    assert len(slots) == 2
    assert slots[0].state == "active" and slots[0].current
    assert slots[1].state == "unplanned"


def test_tml11_past_quarter_with_nothing_planned_is_a_visible_gap(s, goal, today):
    later = today + dt.timedelta(days=100)
    slots = C.goal_timeline(goal, later)
    assert slots[0].state == "gap"


def test_tml12_nudge_to_plan_next_quarter_before_objective_ends(s, goal, today):
    o = C.create_objective(s, goal_code=goal.code, title="Buy ski #1", start_date=today)
    near_end = o.end_date - dt.timedelta(days=10)
    assert any(g.code == o.code and "plan next quarter" in g.message for g in C.cascade_gaps(s, near_end))
    C.create_objective(s, goal_code=goal.code, title="Buy skis #2 and #3", start_date=o.end_date + dt.timedelta(days=1))
    assert not any("plan next quarter" in g.message for g in C.cascade_gaps(s, near_end))


def test_tml13_roadmap_is_soonest_first(s, goal, today):
    v, _ = C.create_vision(s, pillar="BUS", title="Jet Life runs itself", target_year=2030, today=today)
    o = C.create_objective(s, goal_code=goal.code, title="Buy ski #1", start_date=today)
    road = C.upcoming_deadlines(s, today)
    assert [d.code for d in road] == [o.code, goal.code, v.code]
    assert road[0].countdown.days_left == 90


def test_tml14_future_phase_is_not_behind(s, goal, today):
    o = C.create_objective(s, goal_code=goal.code, title="Summer", start_date=today + dt.timedelta(days=30))
    kr = C.create_key_result(s, objective_code=o.code, title="Bookings", cadence="weekly", target=19)
    assert C.kr_progress(s, kr, today)[1].status == "not_started"


def test_tml15_sequenced_goal_lead_time_is_not_unplanned(s, today):
    g, _ = C.create_goal(s, pillar="BUS", title="Summer sales", target_date=D(2027, 8, 31), today=today)
    C.create_objective(s, goal_code=g.code, title="Grow sales", start_date=D(2027, 6, 1), end_date=D(2027, 8, 31))
    states = [sl.state for sl in C.goal_timeline(g, today)]
    assert states[0] == "lead" and "unplanned" not in states[:-1]
