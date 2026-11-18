"""Priority Assurance Framework (PAF) gate.

Rule: every actionable P1 must be resolved before P2 work starts, and every actionable P2 before P3.
"Resolved" = completed, blocked (reason + follow-up), deferred (reason), cancelled (reason) or missed.
Protected commitments (sleep, work shifts, caregiving, meals) are never gated and never gate others.

Pure functions: no database access, fully unit-testable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

from lifeos.engines.state_machine import WORKABLE_STATES


class GateSubject(Protocol):
    id: int
    title: str
    priority: int
    status: str
    protected: bool


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    blocker_ids: tuple[int, ...] = field(default_factory=tuple)


def effective_date(task) -> dt.date | None:
    return task.scheduled_date or task.due_date


def is_actionable(task, today: dt.date, deps_met: bool) -> bool:
    """Workable status, dated today-or-earlier (or undated), with all prerequisites complete."""
    if task.status not in WORKABLE_STATES or not deps_met:
        return False
    when = effective_date(task)
    return when is None or when <= today


def evaluate_gate(task: GateSubject, actionable: Iterable[GateSubject]) -> GateDecision:
    if task.protected:
        return GateDecision(True, "Protected commitment - never gated.")
    blockers: Sequence[GateSubject] = sorted(
        (t for t in actionable if t.id != task.id and not t.protected and t.priority < task.priority),
        key=lambda t: (t.priority, t.id),
    )
    if not blockers:
        return GateDecision(True, "No higher-priority work is open.")
    top = blockers[0].priority
    names = ", ".join(f"'{b.title}'" for b in blockers[:3])
    more = f" and {len(blockers) - 3} more" if len(blockers) > 3 else ""
    reason = (
        f"Gated by {len(blockers)} open higher-priority task(s) (P{top} first): {names}{more}. "
        "Resolve them first: complete, block with a follow-up date, defer with a reason, or cancel "
        "with a justification. Or record an override reason."
    )
    return GateDecision(False, reason, tuple(b.id for b in blockers))
