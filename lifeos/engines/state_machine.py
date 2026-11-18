"""Task state machine.

Stored states: pending, in_progress, blocked, waiting, deferred, completed, cancelled, missed.
"Ready" and "Overdue" are DERIVED (from dependencies and dates), not stored, so they can never
drift out of sync with the facts they describe.
"""

from __future__ import annotations

from lifeos.enums import TaskStatus as S
from lifeos.errors import InvalidTransition, ReasonRequired

OPEN_STATES = frozenset({S.PENDING, S.IN_PROGRESS, S.BLOCKED, S.WAITING, S.DEFERRED})
CLOSED_STATES = frozenset({S.COMPLETED, S.CANCELLED, S.MISSED})
# States in which a task counts against the priority gate.
WORKABLE_STATES = frozenset({S.PENDING, S.IN_PROGRESS})

TRANSITIONS: dict[S, frozenset[S]] = {
    S.PENDING: frozenset({S.IN_PROGRESS, S.BLOCKED, S.WAITING, S.DEFERRED, S.COMPLETED, S.CANCELLED, S.MISSED}),
    S.IN_PROGRESS: frozenset({S.PENDING, S.BLOCKED, S.WAITING, S.DEFERRED, S.COMPLETED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.PENDING, S.IN_PROGRESS, S.WAITING, S.CANCELLED}),
    S.WAITING: frozenset({S.PENDING, S.IN_PROGRESS, S.BLOCKED, S.COMPLETED, S.CANCELLED}),
    S.DEFERRED: frozenset({S.PENDING, S.IN_PROGRESS, S.CANCELLED}),
    S.COMPLETED: frozenset({S.PENDING}),            # reopen
    S.CANCELLED: frozenset({S.PENDING}),            # reopen
    S.MISSED: frozenset({S.COMPLETED, S.PENDING}),  # late completion or reopen
}

# Entering these states requires a documented reason.
REASON_REQUIRED_TARGETS = frozenset({S.BLOCKED, S.WAITING, S.DEFERRED, S.CANCELLED, S.MISSED})


def requires_reason(current: S, target: S) -> bool:
    reopening = current in CLOSED_STATES and target == S.PENDING
    return target in REASON_REQUIRED_TARGETS or reopening


def validate_transition(current: S | str, target: S | str, reason: str | None) -> None:
    current, target = S(current), S(target)
    if target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(t.value for t in TRANSITIONS[current])) or "none"
        raise InvalidTransition(f"Cannot move a task from '{current}' to '{target}'. Allowed: {allowed}.")
    if requires_reason(current, target) and not (reason and reason.strip()):
        raise ReasonRequired(f"Moving a task from '{current}' to '{target}' requires a reason.")
