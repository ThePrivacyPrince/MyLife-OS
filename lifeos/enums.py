"""Core enumerations shared across engines and services."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Priority(IntEnum):
    P1 = 1  # Critical
    P2 = 2  # Important
    P3 = 3  # Optional

    @property
    def label(self) -> str:
        return {1: "P1 Critical", 2: "P2 Important", 3: "P3 Optional"}[self.value]


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    WAITING = "waiting"
    DEFERRED = "deferred"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MISSED = "missed"  # recurring occurrence whose day passed (policy: mark_missed)


class TaskKind(StrEnum):
    ONE_TIME = "one_time"
    OCCURRENCE = "occurrence"  # generated from a RecurringTemplate


class MissedPolicy(StrEnum):
    MARK_MISSED = "mark_missed"  # e.g. daily bedroom reset: yesterday's is gone, record it as missed
    CARRY_OVER = "carry_over"    # e.g. weekly laundry: still needs doing, stays open and overdue


class Actor(StrEnum):
    USER = "user"
    SYSTEM = "system"


class EventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGE = "status_change"
    PRIORITY_CHANGE = "priority_change"
    GATE_OVERRIDE = "gate_override"
    RESCHEDULED = "rescheduled"
    PLANNED = "planned"  # put on a day in the planner (not a slip)
    DUE_DATE_CHANGE = "due_date_change"
    EVIDENCE_ADDED = "evidence_added"
    DEPENDENCY_ADDED = "dependency_added"
    EDITED = "edited"
