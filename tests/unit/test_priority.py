"""PAF-* : Priority Assurance Framework."""
import datetime as dt

import pytest

from lifeos.engines.priority import evaluate_gate
from lifeos.enums import TaskStatus as S
from lifeos.errors import GateViolation, ReasonRequired, ValidationError
from lifeos.models import TaskEvent
from lifeos.services import tasks as T
from lifeos.services.planner import detect_patterns


def test_paf01_p2_gated_until_p1_resolved(s, mk, today):
    p1 = mk("Pay bill", priority=1, due_date=today)
    p2 = mk("Study", priority=2)
    gate = T.gate_for(s, p2, today)
    assert not gate.allowed and p1.id in gate.blocker_ids
    assert "Pay bill" in gate.reason and "Resolve" in gate.reason
    with pytest.raises(GateViolation):
        T.transition(s, p2.id, S.IN_PROGRESS, today=today)
    T.transition(s, p1.id, S.COMPLETED, today=today)
    assert T.gate_for(s, p2, today).allowed
    T.transition(s, p2.id, S.IN_PROGRESS, today=today)


def test_paf02_p3_gated_by_p2(s, mk, today):
    mk("Important", priority=2)
    p3 = mk("Optional", priority=3)
    assert not T.gate_for(s, p3, today).allowed


def test_paf03_protected_never_gated_and_never_gates(s, mk, today):
    sleep = mk("Sleep", pillar="ENV", priority=1, protected=True)
    shift = mk("RXO shift", pillar="BUS", priority=2, protected=True)
    mk("Critical", priority=1)
    p2 = mk("Important", priority=2)
    assert T.gate_for(s, shift, today).allowed
    # only the unprotected P1 blocks p2, not 'Sleep'
    assert T.gate_for(s, p2, today).blocker_ids == (3,)
    assert sleep.id not in T.gate_for(s, p2, today).blocker_ids


@pytest.mark.parametrize("how", ["blocked", "deferred", "cancelled", "waiting"])
def test_paf04_legitimate_resolution_releases_gate(s, mk, today, how):
    p1 = mk("Critical", priority=1)
    p2 = mk("Important", priority=2)
    if how == "blocked":
        T.transition(s, p1.id, S.BLOCKED, reason="Need W-2", follow_up_date=today + dt.timedelta(days=2), today=today)
    elif how == "deferred":
        T.transition(s, p1.id, S.DEFERRED, reason="Reprioritized", defer_until=today + dt.timedelta(days=3), today=today)
    elif how == "cancelled":
        T.transition(s, p1.id, S.CANCELLED, reason="No longer needed", today=today)
    else:
        T.transition(s, p1.id, S.WAITING, reason="Waiting on HR", today=today)
    assert T.gate_for(s, p2, today).allowed


def test_paf05_future_p1_does_not_gate_today(s, mk, today):
    mk("Next week", priority=1, scheduled_date=today + dt.timedelta(days=7))
    assert T.gate_for(s, mk("Now", priority=2), today).allowed


def test_paf06_p1_waiting_on_prerequisite_does_not_gate(s, mk, today):
    pre = mk("Get document", priority=2, scheduled_date=today + dt.timedelta(days=5))
    mk("File return", priority=1, depends_on=[pre.id])
    assert T.gate_for(s, mk("Read", priority=2), today).allowed


def test_paf07_override_requires_reason_and_is_audited(s, mk, today):
    mk("Critical", priority=1)
    p3 = mk("Optional", priority=3)
    for bad in (None, "", "   "):
        with pytest.raises(GateViolation):
            T.transition(s, p3.id, S.COMPLETED, override_reason=bad, today=today)
    T.transition(s, p3.id, S.COMPLETED, override_reason="Already did it on break", today=today)
    overrides = [e for e in p3.events if e.event_type == "gate_override"]
    assert len(overrides) == 1 and overrides[0].reason == "Already did it on break"


def test_paf08_priority_change_requires_reason_and_logs(s, mk):
    t = mk("Thing", priority=1)
    with pytest.raises(ReasonRequired):
        T.change_priority(s, t.id, 2, "")
    with pytest.raises(ValidationError):
        T.change_priority(s, t.id, 1, "same")
    T.change_priority(s, t.id, 2, "Deadline moved")
    ev = [e for e in t.events if e.event_type == "priority_change"][0]
    assert (ev.from_value, ev.to_value, ev.reason, ev.actor) == ("1", "2", "Deadline moved", "user")
    assert ev.at is not None


def test_paf09_repeated_p1_downgrade_is_flagged(s, mk, today):
    t = mk("Taxes", priority=1)
    T.change_priority(s, t.id, 2, "busy")
    T.change_priority(s, t.id, 1, "back")
    T.change_priority(s, t.id, 3, "busy again")
    kinds = {f.kind for f in detect_patterns(s, dt.date.today())}
    assert "p1_downgrade_repeat" in kinds


def test_paf10_pure_gate_ordering_lists_highest_priority_first():
    class X:  # minimal duck type
        def __init__(self, i, p, prot=False):
            self.id, self.title, self.priority, self.status, self.protected = i, f"t{i}", p, "pending", prot
    target = X(9, 3)
    d = evaluate_gate(target, [X(1, 2), X(2, 1), X(3, 3), X(4, 1, prot=True)])
    assert d.blocker_ids == (2, 1)
    assert "P1 first" in d.reason


def test_paf11_late_completion_of_missed_occurrence_not_gated(s, mk, today):
    """Recording a fact that already happened is not 'starting new work'."""
    mk("Critical", priority=1)
    t = mk("Routine", priority=2)
    T.transition(s, t.id, S.MISSED, reason="day passed", today=today)
    T.transition(s, t.id, S.COMPLETED, today=today)
