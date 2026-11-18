"""TSK-* and DEP-* : task commands, validation, evidence, dependencies, history."""
import datetime as dt

import pytest

from lifeos.enums import TaskStatus as S
from lifeos.errors import DependencyError, NotFound, ReasonRequired, ValidationError
from lifeos.services import tasks as T
from lifeos.services.recurring import release_deferrals


@pytest.mark.parametrize("kw", [
    dict(title="   "), dict(pillar="XXX"), dict(priority=4), dict(priority=0),
    dict(scheduled_date=dt.date(2026, 12, 2), due_date=dt.date(2026, 12, 1)),
    dict(due_date="not-a-date"), dict(estimated_minutes=0), dict(goal_code="NOPE-G1"),
])
def test_tsk01_create_rejects_invalid_input(mk, kw):
    with pytest.raises(ValidationError):
        mk(**kw)


def test_tsk02_evidence_required_before_completion(s, mk, today):
    t = mk("Confirm pay date", evidence_required=True)
    with pytest.raises(ValidationError):
        T.transition(s, t.id, S.COMPLETED, today=today)
    T.add_evidence(s, t.id, "HR email 11/18: weekly pay on Fridays")
    T.transition(s, t.id, S.COMPLETED, today=today)
    assert t.completed_at is not None


def test_tsk03_block_needs_follow_up_and_defer_needs_future_date(s, mk, today):
    t = mk()
    with pytest.raises(ReasonRequired):
        T.transition(s, t.id, S.BLOCKED, reason="need doc", today=today)
    with pytest.raises(ReasonRequired):
        T.transition(s, t.id, S.DEFERRED, reason="later", today=today)
    with pytest.raises(ValidationError):
        T.transition(s, t.id, S.DEFERRED, reason="later", defer_until=today, today=today)
    T.transition(s, t.id, S.DEFERRED, reason="later", defer_until=today + dt.timedelta(days=2), today=today)
    assert t.scheduled_date == today + dt.timedelta(days=2)
    assert release_deferrals(s, today + dt.timedelta(days=1)) == []
    assert release_deferrals(s, today + dt.timedelta(days=2)) == [t] and t.status == S.PENDING


def test_tsk04_reschedule_rules(s, mk, today):
    t = mk(due_date=today + dt.timedelta(days=1))
    with pytest.raises(ReasonRequired):
        T.reschedule(s, t.id, today + dt.timedelta(days=1), "", today=today)
    with pytest.raises(ValidationError):
        T.reschedule(s, t.id, today - dt.timedelta(days=1), "oops", today=today)
    T.reschedule(s, t.id, today + dt.timedelta(days=3), "shift ran long", today=today)
    assert t.due_date == today + dt.timedelta(days=1), "due date must never move silently"
    assert any("past its due date" in (e.reason or "") for e in t.events)


def test_tsk05_edit_cannot_bypass_audited_commands(s, mk):
    t = mk()
    for field in ("status", "priority", "due_date"):
        with pytest.raises(ValidationError):
            T.edit_task(s, t.id, **{field: "x"})
    T.edit_task(s, t.id, title="Renamed")
    assert t.title == "Renamed"


def test_tsk06_history_is_complete(s, mk, today):
    t = mk(priority=1)
    T.transition(s, t.id, S.IN_PROGRESS, today=today)
    T.change_priority(s, t.id, 2, "less urgent")
    T.add_evidence(s, t.id, "screenshot")
    T.transition(s, t.id, S.COMPLETED, today=today)
    T.transition(s, t.id, S.PENDING, reason="wasn't actually done", today=today)
    kinds = [e.event_type for e in T.history(s, t.id)]
    assert kinds == ["created", "status_change", "priority_change", "evidence_added", "status_change", "status_change"]
    assert t.completed_at is None


def test_tsk07_not_found(s):
    with pytest.raises(NotFound):
        T.get_task(s, 999)


def test_dep01_self_dependency_rejected(s, mk):
    t = mk()
    with pytest.raises(DependencyError):
        T.add_dependency(s, t.id, t.id)


def test_dep02_cycles_rejected(s, mk):
    a, b, c = mk("A"), mk("B"), mk("C")
    T.add_dependency(s, b.id, a.id)
    T.add_dependency(s, c.id, b.id)
    with pytest.raises(DependencyError, match="Circular"):
        T.add_dependency(s, a.id, c.id)


def test_dep03_cannot_start_or_complete_with_unmet_prerequisite(s, mk, today):
    irs = mk("Pull IRS transcripts")
    years = mk("List unfiled years", depends_on=[irs.id])
    for target in (S.IN_PROGRESS, S.COMPLETED):
        with pytest.raises(DependencyError, match="waiting on"):
            T.transition(s, years.id, target, today=today)
    T.transition(s, irs.id, S.COMPLETED, today=today)
    T.transition(s, years.id, S.COMPLETED, today=today)


def test_dep04_cancelled_prerequisite_keeps_dependent_locked(s, mk, today):
    pre = mk("Pre")
    dep = mk("Dep", depends_on=[pre.id])
    T.transition(s, pre.id, S.CANCELLED, reason="dropped", today=today)
    assert T.unmet_prerequisites(dep) == [pre]
