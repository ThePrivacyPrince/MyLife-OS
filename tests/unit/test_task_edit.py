"""EDT-* : editing tasks and routines from one form, with the audit rules intact."""
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.errors import ReasonRequired, ValidationError
from lifeos.models import PlanEvent, Task
from lifeos.services import cascade as C
from lifeos.services import recurring as R
from lifeos.services.task_edit import task_values, update_task, update_template
from lifeos.services.seed import seed


def test_edt01_plain_fields_change_without_a_reason(s, mk, today):
    t = mk("Raed cases", estimated_minutes=30)
    changed = update_task(s, t.id, title="Read cases", description="Ch. 4", estimated_minutes=45,
                          pillar="EDU", evidence_required=True, today=today)
    assert set(changed) == {"title", "description", "estimated_minutes", "pillar", "evidence_required"}
    assert (t.title, t.pillar_code, t.estimated_minutes) == ("Read cases", "EDU", 45)
    assert {e.event_type for e in t.events} >= {"edited"}


def test_edt02_priority_needs_a_reason(s, mk, today):
    t = mk(priority=2)
    with pytest.raises(ReasonRequired):
        update_task(s, t.id, priority=1, today=today)
    update_task(s, t.id, priority=1, reason="Deadline moved up", today=today)
    assert t.priority == 1 and t.events[-1].reason == "Deadline moved up"


def test_edt03_due_dates_first_set_free_then_moves_need_reason(s, mk, today):
    t = mk()
    update_task(s, t.id, due_date=today + dt.timedelta(days=5), today=today)
    with pytest.raises(ReasonRequired):
        update_task(s, t.id, due_date=today + dt.timedelta(days=9), today=today)
    update_task(s, t.id, due_date=today + dt.timedelta(days=9), reason="Prof extended it", today=today)
    assert t.due_date == today + dt.timedelta(days=9)


def test_edt04_work_day_planning_vs_slipping(s, mk, today):
    t = mk()
    update_task(s, t.id, scheduled_date=today + dt.timedelta(days=2), today=today)  # planning: free
    assert t.events[-1].event_type == "planned"
    u = mk(scheduled_date=today)
    with pytest.raises(ReasonRequired):
        update_task(s, u.id, scheduled_date=today + dt.timedelta(days=1), today=today)
    update_task(s, u.id, scheduled_date=None, today=today)  # back to the unscheduled pile
    assert u.scheduled_date is None


def test_edt05_goal_and_key_result_links(s, mk, today):
    seed(s, today)
    o = C.create_objective(s, goal_code="FIN-G003", title="Know what I owe", start_date=today)
    kr = C.create_key_result(s, objective_code=o.code, title="Sessions", cadence="weekly", target=3)
    t = mk()
    update_task(s, t.id, goal_code="FIN-G003", key_result_code=kr.code, today=today)
    assert t.goal.code == "FIN-G003" and t.key_result.code == kr.code
    with pytest.raises(ValidationError):
        update_task(s, t.id, goal_code="NOPE", today=today)


@pytest.mark.parametrize("kw", [dict(title="  "), dict(pillar="XXX"), dict(priority=7), dict(estimated_minutes=0)])
def test_edt06_invalid_values_rejected(s, mk, today, kw):
    t = mk()
    with pytest.raises(ValidationError):
        update_task(s, t.id, today=today, **kw)


def test_edt07_nothing_changed_and_conflicting_dates(s, mk, today):
    t = mk(due_date=today + dt.timedelta(days=1))
    with pytest.raises(ValidationError, match="Nothing changed"):
        update_task(s, t.id, **task_values(t), today=today)
    with pytest.raises(ValidationError, match="after the due date"):
        update_task(s, t.id, scheduled_date=today + dt.timedelta(days=3), today=today)


def test_edt08_all_or_nothing(engine, today):
    from lifeos.services.tasks import create_task
    with Session(engine) as s:
        t = create_task(s, title="Original", pillar="FIN", priority=2)
        s.commit()
        with pytest.raises(ReasonRequired):
            update_task(s, t.id, title="Renamed", priority=1, today=today)  # title ok, priority refused
        s.rollback()
        assert s.get(Task, t.id).title == "Original"


def test_edt09_routine_edit_flows_to_open_occurrences_only(s, today):
    tpl = R.create_template(s, title="Gym", pillar="ENV", priority=3, frequency="daily",
                            start_date=today - dt.timedelta(days=2), missed_policy="carry_over")
    R.generate_occurrences(s, today)
    with pytest.raises(ReasonRequired):
        update_template(s, tpl.id, priority=2, today=today)
    update_template(s, tpl.id, title="Gym - 30 min", priority=2, estimated_minutes=30, frequency="weekdays",
                    weekdays=["MO", "WE", "FR"], reason="Realistic with classes", today=today)
    occs = {o.occurrence_date: o for o in tpl.occurrences}
    assert occs[today].title == "Gym - 30 min" and occs[today].priority == 2
    assert occs[today - dt.timedelta(days=1)].title == "Gym", "history stays as it happened"
    assert tpl.rule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    assert s.scalars(select(PlanEvent).where(PlanEvent.entity_type == "routine")).all()
    with pytest.raises(ValidationError):
        update_template(s, tpl.id, end_date=tpl.start_date - dt.timedelta(days=1), today=today)
