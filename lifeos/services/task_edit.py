"""Edit a task or a routine from one form. Every change still goes through its own audited command,
and the whole edit is one transaction: if any part is refused, nothing is saved."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.recurrence import build_rule, validate_rule
from lifeos.enums import EventType, MissedPolicy, Priority, TaskStatus as S
from lifeos.errors import ReasonRequired, ValidationError
from lifeos.models import Goal, KeyResult, Pillar, PlanEvent, RecurringTemplate, Task
from lifeos.services import tasks as T

_UNSET = object()


class TaskEdit(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    pillar: str
    priority: Priority
    scheduled_date: dt.date | None = None
    due_date: dt.date | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    goal_code: str | None = None
    key_result_code: str | None = None
    evidence_required: bool = False
    protected: bool = False


def task_values(task: Task) -> dict:
    """Current values in the shape update_task() takes - used to pre-fill the edit form."""
    return dict(title=task.title, description=task.description or "", pillar=task.pillar_code,
                priority=task.priority, scheduled_date=task.scheduled_date, due_date=task.due_date,
                estimated_minutes=task.estimated_minutes, goal_code=task.goal.code if task.goal else None,
                key_result_code=task.key_result.code if task.key_result else None,
                evidence_required=task.evidence_required, protected=task.protected)


def update_task(s: Session, task_id: int, *, reason: str = "", today: dt.date | None = None, **values) -> list[str]:
    """Apply only the fields that changed. Returns the names of the changed fields."""
    today = today or get_settings().today()
    task = T.get_task(s, task_id)
    current = task_values(task)
    merged = {**current, **{k: v for k, v in values.items() if k in current}}
    if isinstance(merged.get("title"), str):
        merged["title"] = merged["title"].strip()
    try:
        new = TaskEdit(**merged)
    except Exception as exc:
        raise T.ValidationError(str(exc)) from exc
    if new.scheduled_date and new.due_date and new.scheduled_date > new.due_date:
        raise ValidationError("The work day is after the due date. Move the due date too (with a reason), "
                              "or pick an earlier day.")
    changed = [k for k in current if getattr(new, k) != current[k]]
    if not changed:
        raise ValidationError("Nothing changed.")
    reason = (reason or "").strip()

    plain = {k: getattr(new, k) for k in ("title", "description", "estimated_minutes", "evidence_required",
                                          "protected") if k in changed}
    if plain:
        T.edit_task(s, task_id, **plain)
    if "pillar" in changed:
        if s.get(Pillar, new.pillar) is None:
            raise ValidationError(f"Unknown pillar '{new.pillar}'.")
        T.log(s, task, EventType.EDITED, frm=f"pillar={task.pillar_code}", to=f"pillar={new.pillar}")
        task.pillar_code = new.pillar
    if "goal_code" in changed:
        goal = s.scalar(select(Goal).where(Goal.code == new.goal_code)) if new.goal_code else None
        if new.goal_code and goal is None:
            raise ValidationError(f"Unknown goal '{new.goal_code}'.")
        T.log(s, task, EventType.EDITED, frm=f"goal={current['goal_code']}", to=f"goal={new.goal_code}")
        task.goal_id = goal.id if goal else None
    if "key_result_code" in changed:
        from lifeos.services.cascade import link_task
        link_task(s, task_id, new.key_result_code)
    if "priority" in changed:
        if not reason:
            raise ReasonRequired("Changing priority needs a reason (add it in the Reason box).")
        T.change_priority(s, task_id, new.priority, reason)
    if "due_date" in changed:
        T.change_due_date(s, task_id, new.due_date, reason)
    if "scheduled_date" in changed:
        old = current["scheduled_date"]
        if new.scheduled_date is None:
            T.log(s, task, EventType.PLANNED, frm=old, to=None, reason=reason or "Unscheduled")
            task.scheduled_date = None
        else:
            from lifeos.services.planner_cal import plan_task_on
            plan_task_on(s, task_id, new.scheduled_date, today=today, reason=reason)
    s.flush()
    return changed


# ---------------------------------------------------------------- routines
def update_template(s: Session, template_id: int, *, title: str | None = None, pillar: str | None = None,
                    priority: int | None = None, estimated_minutes=_UNSET, missed_policy: str | None = None,
                    protected: bool | None = None, end_date=_UNSET, frequency: str | None = None,
                    weekdays: list[str] | None = None, month_day: int | None = None, custom_rule: str | None = None,
                    reason: str = "", today: dt.date | None = None) -> list[str]:
    """Edit a routine. Changes apply to its open occurrences from today on; history is left as it happened."""
    today = today or get_settings().today()
    tpl = s.get(RecurringTemplate, template_id)
    if tpl is None:
        raise T.NotFound(f"Routine {template_id} not found.")
    changed: list[str] = []

    def note(field, old, new):
        changed.append(field)
        s.add(PlanEvent(entity_type="routine", entity_code=f"R{tpl.id}", event_type="edited",
                        from_value=f"{field}={old}"[:200], to_value=f"{field}={new}"[:200], reason=reason or None))

    if title is not None and title.strip() and title.strip() != tpl.title:
        note("title", tpl.title, title.strip())
        tpl.title = title.strip()
    if pillar is not None and pillar != tpl.pillar_code:
        if s.get(Pillar, pillar) is None:
            raise ValidationError(f"Unknown pillar '{pillar}'.")
        note("pillar", tpl.pillar_code, pillar)
        tpl.pillar_code = pillar
    if estimated_minutes is not _UNSET and estimated_minutes != tpl.estimated_minutes:
        if estimated_minutes is not None and not 1 <= int(estimated_minutes) <= 24 * 60:
            raise ValidationError("Estimate must be between 1 and 1440 minutes.")
        note("estimated_minutes", tpl.estimated_minutes, estimated_minutes)
        tpl.estimated_minutes = estimated_minutes
    if missed_policy is not None and missed_policy != tpl.missed_policy:
        note("missed_policy", tpl.missed_policy, str(MissedPolicy(missed_policy)))
        tpl.missed_policy = str(MissedPolicy(missed_policy))
    if protected is not None and protected != tpl.protected:
        note("protected", tpl.protected, protected)
        tpl.protected = protected
    if end_date is not _UNSET and end_date != tpl.end_date:
        if end_date is not None and end_date < tpl.start_date:
            raise ValidationError("End date is before the routine started.")
        note("end_date", tpl.end_date, end_date)
        tpl.end_date = end_date
    if frequency:
        rule = build_rule(frequency, weekdays=weekdays, month_day=month_day, custom=custom_rule)
        validate_rule(rule, tpl.start_date)
        if rule != tpl.rule:
            note("schedule", tpl.frequency_label, frequency)
            tpl.rule = rule
            tpl.frequency_label = frequency + (f" ({','.join(weekdays)})" if weekdays else "")
    new_priority = int(Priority(priority)) if priority is not None else tpl.priority
    if new_priority != tpl.priority:
        if not reason.strip():
            raise ReasonRequired("Changing a routine's priority needs a reason.")
        note("priority", tpl.priority, new_priority)
        tpl.priority = new_priority
    if not changed:
        raise ValidationError("Nothing changed.")

    # Carry the changes to today's and future open occurrences.
    for occ in s.scalars(select(Task).where(Task.template_id == tpl.id, Task.occurrence_date >= today,
                                            Task.status.in_([S.PENDING, S.IN_PROGRESS]))):
        for field, value in (("title", tpl.title), ("estimated_minutes", tpl.estimated_minutes),
                             ("protected", tpl.protected)):
            if getattr(occ, field) != value:
                T.edit_task(s, occ.id, **{field: value})
        if occ.pillar_code != tpl.pillar_code:
            T.log(s, occ, EventType.EDITED, frm=f"pillar={occ.pillar_code}", to=f"pillar={tpl.pillar_code}")
            occ.pillar_code = tpl.pillar_code
        if occ.priority != tpl.priority:
            T.change_priority(s, occ.id, tpl.priority, f"Routine edited: {reason.strip()}")
    s.flush()
    return changed
