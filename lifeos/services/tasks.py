"""Task operations. Every change goes through here so rules and audit history are always applied."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Iterable

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings, utcnow
from lifeos.engines.dependencies import check_new_dependency
from lifeos.engines.priority import GateDecision, evaluate_gate, is_actionable
from lifeos.engines.state_machine import validate_transition
from lifeos.enums import Actor, EventType, Priority, TaskKind, TaskStatus as S
from lifeos.errors import DependencyError, GateViolation, NotFound, ReasonRequired, ValidationError
from lifeos.models import Evidence, Goal, Pillar, Task, TaskDependency, TaskEvent


# ---------------------------------------------------------------- input models
class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    pillar: str
    priority: Priority
    description: str = ""
    scheduled_date: dt.date | None = None
    due_date: dt.date | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    goal_code: str | None = None
    evidence_required: bool = False
    protected: bool = False
    depends_on: list[int] = Field(default_factory=list)
    key_result_code: str | None = None

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title cannot be blank")
        return v.strip()

    @model_validator(mode="after")
    def _dates(self):
        if self.scheduled_date and self.due_date and self.scheduled_date > self.due_date:
            raise ValueError("scheduled date is after the due date")
        return self


def _validated(model_cls, **kwargs):
    try:
        return model_cls(**kwargs)
    except Exception as exc:  # pydantic.ValidationError -> domain error
        raise ValidationError(str(exc)) from exc


# ---------------------------------------------------------------- helpers
def get_task(s: Session, task_id: int) -> Task:
    task = s.get(Task, task_id)
    if task is None:
        raise NotFound(f"Task {task_id} not found.")
    return task


def log(s: Session, task: Task, event: EventType, *, frm=None, to=None, reason=None, actor=Actor.USER) -> None:
    s.add(TaskEvent(task=task, event_type=str(event), from_value=None if frm is None else str(frm),
                    to_value=None if to is None else str(to), reason=reason, actor=str(actor)))


def _require_pillar(s: Session, code: str) -> None:
    if s.get(Pillar, code) is None:
        raise ValidationError(f"Unknown pillar '{code}'.")


def _key_result_id(s: Session, code: str | None) -> int | None:
    if not code:
        return None
    from lifeos.models import KeyResult
    kr = s.scalar(select(KeyResult).where(KeyResult.code == code))
    if kr is None:
        raise ValidationError(f"Unknown key result '{code}'.")
    return kr.id


def _goal_id(s: Session, code: str | None) -> int | None:
    if not code:
        return None
    goal = s.scalar(select(Goal).where(Goal.code == code))
    if goal is None:
        raise ValidationError(f"Unknown goal '{code}'.")
    return goal.id


def dependency_graph(s: Session) -> dict[int, set[int]]:
    graph: dict[int, set[int]] = defaultdict(set)
    for dep in s.scalars(select(TaskDependency)):
        graph[dep.task_id].add(dep.depends_on_id)
    return graph


def unmet_prerequisites(task: Task) -> list[Task]:
    return [d.depends_on for d in task.prerequisites if d.depends_on.status != S.COMPLETED]


def actionable_tasks(s: Session, today: dt.date) -> list[Task]:
    rows = s.scalars(select(Task).where(Task.status.in_([S.PENDING, S.IN_PROGRESS])))
    return [t for t in rows if is_actionable(t, today, deps_met=not unmet_prerequisites(t))]


def gate_for(s: Session, task: Task, today: dt.date | None = None) -> GateDecision:
    today = today or get_settings().today()
    return evaluate_gate(task, actionable_tasks(s, today))


# ---------------------------------------------------------------- commands
def create_task(s: Session, *, actor: Actor = Actor.USER, **fields) -> Task:
    data = _validated(TaskCreate, **fields)
    _require_pillar(s, data.pillar)
    task = Task(
        title=data.title, description=data.description, pillar_code=data.pillar, priority=int(data.priority),
        kind=TaskKind.ONE_TIME, scheduled_date=data.scheduled_date, due_date=data.due_date,
        estimated_minutes=data.estimated_minutes, goal_id=_goal_id(s, data.goal_code),
        evidence_required=data.evidence_required, protected=data.protected, status=S.PENDING,
        key_result_id=_key_result_id(s, data.key_result_code),
    )
    s.add(task)
    s.flush()
    log(s, task, EventType.CREATED, to=f"P{task.priority}", actor=actor)
    for prereq in data.depends_on:
        add_dependency(s, task.id, prereq)
    return task


def add_dependency(s: Session, task_id: int, depends_on_id: int) -> TaskDependency:
    task, prereq = get_task(s, task_id), get_task(s, depends_on_id)
    s.flush()
    check_new_dependency(dependency_graph(s), task.id, prereq.id)
    dep = TaskDependency(task_id=task.id, depends_on_id=prereq.id)
    s.add(dep)
    s.flush()
    s.refresh(task)
    log(s, task, EventType.DEPENDENCY_ADDED, to=f"#{prereq.id} {prereq.title}")
    return dep


def transition(
    s: Session,
    task_id: int,
    target: S | str,
    *,
    reason: str | None = None,
    follow_up_date: dt.date | None = None,
    defer_until: dt.date | None = None,
    override_reason: str | None = None,
    actor: Actor = Actor.USER,
    today: dt.date | None = None,
) -> Task:
    task = get_task(s, task_id)
    target = S(target)
    today = today or get_settings().today()
    validate_transition(task.status, target, reason)

    if target == S.BLOCKED and not follow_up_date:
        raise ReasonRequired("Blocking a task requires a follow-up date so it is not forgotten.")
    if target == S.DEFERRED:
        if not defer_until:
            raise ReasonRequired("Deferring a task requires the date it comes back.")
        if defer_until <= today:
            raise ValidationError("Defer-until date must be in the future.")

    if target in (S.IN_PROGRESS, S.COMPLETED):
        unmet = unmet_prerequisites(task)
        if unmet:
            names = ", ".join(f"#{t.id} '{t.title}'" for t in unmet)
            raise DependencyError(f"Cannot {'start' if target == S.IN_PROGRESS else 'complete'} - waiting on: {names}.")
        if target == S.COMPLETED and task.evidence_required and not task.evidence:
            raise ValidationError("This task requires evidence before it can be marked complete.")
        # PAF gate: starting or completing out of priority order is never silent.
        if task.status != S.MISSED:
            decision = evaluate_gate(task, actionable_tasks(s, today))
            if not decision.allowed:
                if not (override_reason and override_reason.strip()):
                    raise GateViolation(decision)
                log(s, task, EventType.GATE_OVERRIDE, frm=",".join(map(str, decision.blocker_ids)),
                    to=str(target), reason=override_reason.strip(), actor=actor)

    previous = task.status
    task.status = target
    task.status_reason = reason.strip() if reason else None
    task.follow_up_date = follow_up_date if target == S.BLOCKED else None
    if target == S.DEFERRED:
        log(s, task, EventType.RESCHEDULED, frm=task.scheduled_date, to=defer_until, reason=reason, actor=actor)
        task.scheduled_date = defer_until
    task.completed_at = utcnow() if target == S.COMPLETED else None
    log(s, task, EventType.STATUS_CHANGE, frm=previous, to=target, reason=reason, actor=actor)
    s.flush()
    return task


def change_priority(s: Session, task_id: int, new: Priority | int, reason: str, actor: Actor = Actor.USER) -> Task:
    task = get_task(s, task_id)
    new = Priority(new)
    if not (reason and reason.strip()):
        raise ReasonRequired("Every priority change needs a reason.")
    if new == task.priority:
        raise ValidationError(f"Task is already P{new}.")
    old = task.priority
    task.priority = int(new)
    log(s, task, EventType.PRIORITY_CHANGE, frm=old, to=int(new), reason=reason.strip(), actor=actor)
    s.flush()
    return task


def reschedule(s: Session, task_id: int, new_date: dt.date, reason: str, actor: Actor = Actor.USER,
               today: dt.date | None = None, planned: bool = False) -> Task:
    """Move a task's working date. `planned=True` is putting it on a day in the planner (not a slip)."""
    task = get_task(s, task_id)
    if planned and not (reason and reason.strip()):
        reason = "Planned in the planner"
    if not (reason and reason.strip()):
        raise ReasonRequired("Rescheduling needs a reason (it feeds the end-of-day pattern review).")
    if task.status not in (S.PENDING, S.IN_PROGRESS, S.BLOCKED, S.WAITING, S.DEFERRED):
        raise ValidationError(f"Cannot reschedule a {task.status} task.")
    today = today or get_settings().today()
    if new_date < today:
        raise ValidationError("Cannot reschedule into the past.")
    old = task.scheduled_date
    task.scheduled_date = new_date
    log(s, task, EventType.PLANNED if planned else EventType.RESCHEDULED, frm=old, to=new_date,
        reason=reason.strip(), actor=actor)
    if task.due_date and new_date > task.due_date:
        log(s, task, EventType.EDITED, frm="on time", to="scheduled after due date",
            reason="Warning: rescheduled past its due date - the due date was NOT moved.", actor=Actor.SYSTEM)
    s.flush()
    return task


def change_due_date(s: Session, task_id: int, new_due: dt.date | None, reason: str = "") -> Task:
    """Setting a first due date is free; moving or removing one needs a reason."""
    task = get_task(s, task_id)
    old = task.due_date
    if old is not None and not (reason and reason.strip()):
        raise ReasonRequired("Moving or removing a due date needs a reason.")
    if old == new_due:
        raise ValidationError("That's the same due date.")
    task.due_date = new_due
    log(s, task, EventType.DUE_DATE_CHANGE, frm=old, to=new_due, reason=(reason or "").strip() or None)
    s.flush()
    return task


def add_evidence(s: Session, task_id: int, description: str, uri: str | None = None) -> Evidence:
    task = get_task(s, task_id)
    if not (description and description.strip()):
        raise ValidationError("Evidence needs a description.")
    ev = Evidence(task=task, description=description.strip(), uri=(uri or None))
    s.add(ev)
    log(s, task, EventType.EVIDENCE_ADDED, to=description.strip()[:200])
    s.flush()
    return ev


def edit_task(s: Session, task_id: int, **changes) -> Task:
    """Edit descriptive fields only. Status, priority and dates have their own audited commands."""
    allowed = {"title", "description", "estimated_minutes", "evidence_required", "protected"}
    bad = set(changes) - allowed
    if bad:
        raise ValidationError(f"Use the dedicated command to change: {sorted(bad)}.")
    task = get_task(s, task_id)
    for field, value in changes.items():
        if field == "title" and not str(value).strip():
            raise ValidationError("title cannot be blank")
        old = getattr(task, field)
        if old != value:
            setattr(task, field, value)
            log(s, task, EventType.EDITED, frm=f"{field}={old}", to=f"{field}={value}")
    s.flush()
    return task


def history(s: Session, task_id: int) -> list[TaskEvent]:
    return list(get_task(s, task_id).events)


def tasks_by_ids(s: Session, ids: Iterable[int]) -> list[Task]:
    ids = list(ids)
    return list(s.scalars(select(Task).where(Task.id.in_(ids)))) if ids else []
