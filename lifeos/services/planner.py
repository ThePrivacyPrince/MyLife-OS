"""Daily Command Center planning (spec 8.1) and end-of-day review (spec 8.2)."""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.priority import GateDecision, evaluate_gate, is_actionable
from lifeos.engines.state_machine import OPEN_STATES
from lifeos.enums import EventType, TaskKind, TaskStatus as S
from lifeos.models import DayReview, RecurringTemplate, Task, TaskEvent
from lifeos.services.recurring import apply_missed_policy, generate_occurrences, release_deferrals
from lifeos.services.tasks import unmet_prerequisites


@dataclass
class PlannedTask:
    task: Task
    gate: GateDecision
    overdue: bool = False
    unmet: list[Task] = field(default_factory=list)


@dataclass
class Flag:
    kind: str
    message: str
    task_ids: tuple[int, ...] = ()


@dataclass
class DayPlan:
    date: dt.date
    protected: list[PlannedTask]
    queue: dict[int, list[PlannedTask]]  # priority -> actionable tasks in order
    blocked: list[Task]
    waiting: list[Task]
    dependency_locked: list[PlannedTask]
    overdue: list[Task]
    upcoming: list[Task]
    follow_ups_due: list[Task]
    auto_missed: list[Task]
    capacity_minutes: int
    committed_minutes: int
    unestimated_count: int
    warnings: list[str]
    flags: list[Flag]

    @property
    def overcommitted(self) -> bool:
        return self.committed_minutes > self.capacity_minutes

    @property
    def actionable(self) -> list[PlannedTask]:
        return [p for prio in (1, 2, 3) for p in self.queue[prio]]

    @property
    def recurring_today(self) -> list[PlannedTask]:
        return [p for p in self.actionable + self.protected if p.task.is_recurring]

    @property
    def one_time_today(self) -> list[PlannedTask]:
        return [p for p in self.actionable if not p.task.is_recurring]


def refresh(s: Session, today: dt.date) -> list[Task]:
    """Bring stored state up to date: generate occurrences, apply missed policy, release deferrals."""
    generate_occurrences(s, through=today)
    missed = apply_missed_policy(s, today)
    release_deferrals(s, today)
    s.flush()
    return missed


def _sort_key(today: dt.date):
    def key(p: PlannedTask):
        t = p.task
        return (not p.overdue, t.due_date or dt.date.max, t.scheduled_date or dt.date.max, t.id)
    return key


def plan_day(s: Session, today: dt.date | None = None) -> DayPlan:
    settings = get_settings()
    today = today or settings.today()
    auto_missed = refresh(s, today)

    open_tasks = list(s.scalars(select(Task).where(Task.status.in_([str(x) for x in OPEN_STATES]))))
    unmet = {t.id: unmet_prerequisites(t) for t in open_tasks}
    actionable = [t for t in open_tasks if is_actionable(t, today, deps_met=not unmet[t.id])]

    def planned(t: Task) -> PlannedTask:
        overdue = bool(t.due_date and t.due_date < today)
        return PlannedTask(t, evaluate_gate(t, actionable), overdue, unmet[t.id])

    protected = sorted((planned(t) for t in actionable if t.protected), key=_sort_key(today))
    queue = {p: sorted((planned(t) for t in actionable if not t.protected and t.priority == p),
                       key=_sort_key(today)) for p in (1, 2, 3)}

    workable = {S.PENDING, S.IN_PROGRESS}
    dependency_locked = [planned(t) for t in open_tasks if t.status in workable and unmet[t.id]
                         and (t.effective_date is None or t.effective_date <= today)]
    blocked = [t for t in open_tasks if t.status == S.BLOCKED]
    waiting = [t for t in open_tasks if t.status == S.WAITING]
    overdue = sorted((t for t in open_tasks if t.due_date and t.due_date < today), key=lambda t: t.due_date)
    horizon = today + dt.timedelta(days=settings.planning.upcoming_window_days)
    upcoming = sorted((t for t in open_tasks if t.effective_date and today < t.effective_date <= horizon),
                      key=lambda t: (t.effective_date, t.priority))
    follow_ups = [t for t in blocked if t.follow_up_date and t.follow_up_date <= today]

    default_est = settings.planning.default_task_estimate_minutes
    committed_tasks = queue[1] + queue[2]
    unestimated = sum(1 for p in committed_tasks if not p.task.estimated_minutes)
    committed = sum(p.task.estimated_minutes or default_est for p in committed_tasks)
    capacity = settings.planning.discretionary_minutes_per_day

    warnings: list[str] = []
    if committed > capacity:
        warnings.append(
            f"Overcommitted: P1+P2 work is about {committed} min against {capacity} min of realistic capacity. "
            "Defer some P2 items (with a reason) rather than silently letting them slip."
        )
    if unestimated:
        warnings.append(f"Assumption: {unestimated} task(s) have no estimate; counted as {default_est} min each.")
    for t in follow_ups:
        warnings.append(f"Follow-up due on blocked task #{t.id} '{t.title}' ({t.status_reason}).")
    if auto_missed:
        warnings.append(f"{len(auto_missed)} recurring occurrence(s) from earlier days were recorded as missed.")

    return DayPlan(
        date=today, protected=protected, queue=queue, blocked=blocked, waiting=waiting,
        dependency_locked=dependency_locked, overdue=overdue, upcoming=upcoming, follow_ups_due=follow_ups,
        auto_missed=auto_missed, capacity_minutes=capacity, committed_minutes=committed,
        unestimated_count=unestimated, warnings=warnings, flags=detect_patterns(s, today),
    )


# ---------------------------------------------------------------- end-of-day review
@dataclass
class DayReviewSummary:
    date: dt.date
    completed: list[Task]
    incomplete: list[Task]
    blocked: list[Task]
    missed: list[Task]
    rescheduled: list[Task]
    overdue: list[Task]
    gate_overrides: int
    minutes_completed: int
    p1_completed: int
    p1_total: int

    @property
    def p1_rate(self) -> float | None:
        return None if self.p1_total == 0 else self.p1_completed / self.p1_total

    def as_dict(self) -> dict:
        ids = lambda ts: [t.id for t in ts]  # noqa: E731
        return {
            "completed": ids(self.completed), "incomplete": ids(self.incomplete), "blocked": ids(self.blocked),
            "missed": ids(self.missed), "rescheduled": ids(self.rescheduled), "overdue": ids(self.overdue),
            "gate_overrides": self.gate_overrides, "minutes_completed": self.minutes_completed,
            "p1_completed": self.p1_completed, "p1_total": self.p1_total,
        }


def _events_on(s: Session, day: dt.date, event_type: EventType) -> list[TaskEvent]:
    settings = get_settings()
    lo = dt.datetime.combine(day - dt.timedelta(days=1), dt.time.min)
    hi = dt.datetime.combine(day + dt.timedelta(days=2), dt.time.min)
    rows = s.scalars(select(TaskEvent).where(TaskEvent.event_type == str(event_type),
                                             TaskEvent.at >= lo, TaskEvent.at < hi))
    return [e for e in rows if settings.local_date(e.at) == day]


def review_day(s: Session, day: dt.date | None = None) -> DayReviewSummary:
    settings = get_settings()
    day = day or settings.today()
    all_tasks = list(s.scalars(select(Task)))
    completed = [t for t in all_tasks if t.status == S.COMPLETED and t.completed_at
                 and settings.local_date(t.completed_at) == day]
    due_today = [t for t in all_tasks if t.effective_date == day or (t.effective_date and t.effective_date < day
                                                                     and t.status in OPEN_STATES)]
    incomplete = [t for t in all_tasks if t.status in (S.PENDING, S.IN_PROGRESS)
                  and (t.effective_date is None or t.effective_date <= day)]
    missed = [t for t in all_tasks if t.kind == TaskKind.OCCURRENCE and t.occurrence_date == day
              and t.status in (S.MISSED, S.PENDING, S.IN_PROGRESS)]
    resched_ids = {e.task_id for e in _events_on(s, day, EventType.RESCHEDULED)}
    p1_scope = {t.id: t for t in due_today + completed if t.priority == 1 and not t.protected
                and t.status != S.CANCELLED}
    return DayReviewSummary(
        date=day,
        completed=completed,
        incomplete=incomplete,
        blocked=[t for t in all_tasks if t.status == S.BLOCKED],
        missed=missed,
        rescheduled=[t for t in all_tasks if t.id in resched_ids],
        overdue=[t for t in all_tasks if t.status in OPEN_STATES and t.due_date and t.due_date < day],
        gate_overrides=len(_events_on(s, day, EventType.GATE_OVERRIDE)),
        minutes_completed=sum(t.estimated_minutes or 0 for t in completed),
        p1_completed=sum(1 for t in p1_scope.values() if t.status == S.COMPLETED),
        p1_total=len(p1_scope),
    )


def save_review(s: Session, day: dt.date, note: str) -> DayReview:
    summary = review_day(s, day)
    row = s.scalar(select(DayReview).where(DayReview.date == day)) or DayReview(date=day)
    row.note, row.summary = note, summary.as_dict()
    s.add(row)
    s.flush()
    return row


# ---------------------------------------------------------------- pattern detection
def detect_patterns(s: Session, today: dt.date) -> list[Flag]:
    """Recurring-failure and control-weakness signals. Framed as design questions, not discipline judgments."""
    c = get_settings().controls
    flags: list[Flag] = []

    # 1. Repeated downgrading of P1 tasks (spec 6.2).
    since = dt.datetime.combine(today - dt.timedelta(days=c.downgrade_window_days), dt.time.min)
    downgrades = [e for e in s.scalars(select(TaskEvent).where(
        TaskEvent.event_type == str(EventType.PRIORITY_CHANGE), TaskEvent.at >= since))
        if e.from_value == "1" and e.to_value in ("2", "3")]
    per_task = Counter(e.task_id for e in downgrades)
    for task_id, n in per_task.items():
        if n >= c.downgrade_per_task_threshold:
            flags.append(Flag("p1_downgrade_repeat",
                              f"Task #{task_id} was downgraded from P1 {n} times in {c.downgrade_window_days} days. "
                              "Is it really critical, or is something making it hard to start?", (task_id,)))
    if len(downgrades) >= c.downgrade_total_threshold:
        flags.append(Flag("p1_downgrade_volume",
                          f"{len(downgrades)} P1 downgrades in {c.downgrade_window_days} days - possible control "
                          "weakness in how P1s are assigned.", tuple(per_task)))

    # 2. Repeated rescheduling.
    resched = Counter(e.task_id for e in s.scalars(select(TaskEvent).where(
        TaskEvent.event_type == str(EventType.RESCHEDULED), TaskEvent.actor == "user")))
    for task_id, n in resched.items():
        task = s.get(Task, task_id)
        if n >= c.reschedule_threshold and task and task.status in OPEN_STATES:
            flags.append(Flag("reschedule_repeat",
                              f"'{task.title}' has been rescheduled {n} times. It may be too big, unclear, or "
                              "blocked by something not yet written down. Consider breaking it into a first step.",
                              (task_id,)))

    # 3. Recurring adherence.
    window_start = today - dt.timedelta(days=c.adherence_window_days)
    stats: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for occ in s.scalars(select(Task).where(Task.kind == TaskKind.OCCURRENCE, Task.occurrence_date >= window_start,
                                            Task.occurrence_date < today, Task.status != S.CANCELLED)):
        stats[occ.template_id][1] += 1
        stats[occ.template_id][0] += occ.status == S.COMPLETED
    for tpl_id, (done, total) in stats.items():
        if total >= c.adherence_min_samples and done / total < c.adherence_min_rate:
            tpl = s.get(RecurringTemplate, tpl_id)
            flags.append(Flag("recurring_adherence",
                              f"'{tpl.title}' was done {done} of {total} times in the last "
                              f"{c.adherence_window_days} days. Is the schedule realistic, the time slot right, "
                              "or should the routine be smaller?", ()))
    return flags


def recurring_adherence(s: Session, template_id: int, today: dt.date, days: int = 14) -> tuple[int, int]:
    start = today - dt.timedelta(days=days)
    occs = list(s.scalars(select(Task).where(Task.template_id == template_id, Task.occurrence_date >= start,
                                             Task.occurrence_date < today, Task.status != S.CANCELLED)))
    return sum(1 for o in occs if o.status == S.COMPLETED), len(occs)
