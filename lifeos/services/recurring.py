"""Recurring templates and occurrence generation."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.recurrence import build_rule, occurrence_dates, validate_rule
from lifeos.enums import Actor, EventType, MissedPolicy, Priority, TaskKind, TaskStatus as S
from lifeos.errors import NotFound, ValidationError
from lifeos.models import Pillar, RecurringTemplate, Task
from lifeos.services.tasks import _goal_id, _key_result_id, log, transition


def create_template(
    s: Session,
    *,
    title: str,
    pillar: str,
    priority: Priority | int,
    frequency: str,
    start_date: dt.date,
    weekdays: list[str] | None = None,
    month_day: int | None = None,
    custom_rule: str | None = None,
    end_date: dt.date | None = None,
    estimated_minutes: int | None = None,
    missed_policy: MissedPolicy | str = MissedPolicy.MARK_MISSED,
    protected: bool = False,
    evidence_required: bool = False,
    description: str = "",
    goal_code: str | None = None,
    key_result_code: str | None = None,
) -> RecurringTemplate:
    if not title or not title.strip():
        raise ValidationError("title cannot be blank")
    if s.get(Pillar, pillar) is None:
        raise ValidationError(f"Unknown pillar '{pillar}'.")
    if end_date and end_date < start_date:
        raise ValidationError("End date is before start date.")
    rule = build_rule(frequency, weekdays=weekdays, month_day=month_day, custom=custom_rule)
    validate_rule(rule, start_date)
    label = frequency + (f" ({','.join(weekdays)})" if weekdays else "")
    tpl = RecurringTemplate(
        title=title.strip(), description=description, pillar_code=pillar, priority=int(Priority(priority)),
        rule=rule, frequency_label=label, start_date=start_date, end_date=end_date,
        estimated_minutes=estimated_minutes, missed_policy=str(MissedPolicy(missed_policy)),
        protected=protected, evidence_required=evidence_required, goal_id=_goal_id(s, goal_code),
        key_result_id=_key_result_id(s, key_result_code),
    )
    s.add(tpl)
    s.flush()
    return tpl


def get_template(s: Session, template_id: int) -> RecurringTemplate:
    tpl = s.get(RecurringTemplate, template_id)
    if tpl is None:
        raise NotFound(f"Recurring template {template_id} not found.")
    return tpl


def add_exception(s: Session, template_id: int, day: dt.date) -> None:
    """Skip one date (e.g. a holiday). Applies to not-yet-generated occurrences."""
    tpl = get_template(s, template_id)
    tpl.exceptions = sorted(set(tpl.exceptions or []) | {day.isoformat()})
    s.flush()


def set_active(s: Session, template_id: int, active: bool) -> None:
    get_template(s, template_id).active = active
    s.flush()


def generate_occurrences(s: Session, through: dt.date) -> list[Task]:
    """Create occurrence rows up to `through` (inclusive). Idempotent."""
    backfill = get_settings().planning.max_backfill_days
    created: list[Task] = []
    for tpl in s.scalars(select(RecurringTemplate).where(RecurringTemplate.active.is_(True))):
        if tpl.generated_through:
            window_start = tpl.generated_through + dt.timedelta(days=1)
        else:
            window_start = max(tpl.start_date, through - dt.timedelta(days=backfill))
        if window_start > through:
            continue
        exceptions = [dt.date.fromisoformat(d) for d in (tpl.exceptions or [])]
        existing = set(s.scalars(select(Task.occurrence_date).where(Task.template_id == tpl.id)))
        for day in occurrence_dates(tpl.rule, tpl.start_date, window_start, through,
                                    end_date=tpl.end_date, exceptions=exceptions):
            if day in existing:
                continue
            occ = Task(
                title=tpl.title, description=tpl.description, pillar_code=tpl.pillar_code,
                priority=tpl.priority, kind=TaskKind.OCCURRENCE, template_id=tpl.id, occurrence_date=day,
                scheduled_date=day, due_date=day, estimated_minutes=tpl.estimated_minutes,
                goal_id=tpl.goal_id, evidence_required=tpl.evidence_required, protected=tpl.protected,
                status=S.PENDING, key_result_id=tpl.key_result_id,
            )
            s.add(occ)
            s.flush()
            log(s, occ, EventType.CREATED, to=f"P{occ.priority}", reason=f"Generated from template #{tpl.id}",
                actor=Actor.SYSTEM)
            created.append(occ)
        tpl.generated_through = through
    s.flush()
    return created


def apply_missed_policy(s: Session, today: dt.date) -> list[Task]:
    """Occurrences whose day has passed: mark_missed -> 'missed' (recorded, never deleted);
    carry_over -> left open, shown as overdue."""
    marked: list[Task] = []
    stale = s.scalars(
        select(Task).join(RecurringTemplate, Task.template_id == RecurringTemplate.id).where(
            Task.kind == TaskKind.OCCURRENCE,
            Task.status.in_([S.PENDING, S.IN_PROGRESS]),
            Task.scheduled_date < today,
            RecurringTemplate.missed_policy == MissedPolicy.MARK_MISSED,
        )
    )
    for occ in list(stale):
        transition(s, occ.id, S.MISSED, reason=f"Not completed on {occ.scheduled_date} (policy: mark_missed).",
                   actor=Actor.SYSTEM, today=today)
        marked.append(occ)
    return marked


def release_deferrals(s: Session, today: dt.date) -> list[Task]:
    """Deferred tasks whose return date has arrived go back to pending."""
    released: list[Task] = []
    for task in list(s.scalars(select(Task).where(Task.status == S.DEFERRED, Task.scheduled_date <= today))):
        transition(s, task.id, S.PENDING, reason="Deferral period ended.", actor=Actor.SYSTEM, today=today)
        released.append(task)
    return released


def upcoming_dates(tpl: RecurringTemplate, start: dt.date, days: int) -> list[dt.date]:
    """Preview future dates without creating rows."""
    exceptions = [dt.date.fromisoformat(d) for d in (tpl.exceptions or [])]
    return occurrence_dates(tpl.rule, tpl.start_date, start, start + dt.timedelta(days=days),
                            end_date=tpl.end_date, exceptions=exceptions)
