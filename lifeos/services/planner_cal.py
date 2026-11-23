"""Planner: calendar events, repeating class schedules, day/week/month views, and planning tasks onto days.

Everything a day holds is gathered in one place: timed events, tasks scheduled or due, routines,
and goal/objective deadlines from the cascade.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.recurrence import build_rule, occurrence_dates
from lifeos.engines.state_machine import OPEN_STATES
from lifeos.enums import TaskKind, TaskStatus as S
from lifeos.errors import NotFound, ValidationError
from lifeos.models import CalendarEvent, EventSeries, Pillar, RecurringTemplate, Task
from lifeos.services import tasks as T

KINDS = {"class": "🎓", "study": "📚", "work": "💼", "appointment": "📍", "exam": "📝", "deadline": "⏰",
         "personal": "🙂", "other": "•"}


# ---------------------------------------------------------------- date helpers (pure)
def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def month_weeks(year: int, month: int) -> list[list[dt.date]]:
    """Calendar grid for a month, Monday-first, padded with neighbouring days."""
    return calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)


def shift_month(day: dt.date, months: int) -> dt.date:
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


# ---------------------------------------------------------------- events
def _check(title: str, kind: str, pillar: str | None, s: Session, start, end) -> str:
    if not title or not title.strip():
        raise ValidationError("title cannot be blank")
    if kind not in KINDS:
        raise ValidationError(f"Unknown kind '{kind}'. Use one of {list(KINDS)}.")
    if pillar and s.get(Pillar, pillar) is None:
        raise ValidationError(f"Unknown pillar '{pillar}'.")
    if (start is None) != (end is None):
        raise ValidationError("Give both a start and end time, or neither (all day).")
    if start and end and end <= start:
        raise ValidationError("End time must be after start time.")
    return title.strip()


def add_event(s: Session, *, title: str, day: dt.date, start_time: dt.time | None = None,
              end_time: dt.time | None = None, kind: str = "other", pillar: str | None = None,
              location: str = "", notes: str = "") -> CalendarEvent:
    ev = CalendarEvent(title=_check(title, kind, pillar, s, start_time, end_time), day=day, start_time=start_time,
                       end_time=end_time, kind=kind, pillar_code=pillar, location=location, notes=notes)
    s.add(ev)
    s.flush()
    return ev


def add_series(s: Session, *, title: str, weekdays: list[str], start_date: dt.date, end_date: dt.date,
               start_time: dt.time, end_time: dt.time, kind: str = "class", pillar: str | None = None,
               location: str = "", biweekly: bool = False) -> EventSeries:
    if not weekdays:
        raise ValidationError("Pick at least one weekday.")
    if end_date < start_date:
        raise ValidationError("Series ends before it starts.")
    if (end_date - start_date).days > 400:
        raise ValidationError("A series can run at most about a year - add another for the next term.")
    rule = build_rule("biweekly" if biweekly else "weekdays", weekdays=weekdays)
    ser = EventSeries(title=_check(title, kind, pillar, s, start_time, end_time), kind=kind, pillar_code=pillar,
                      rule=rule, frequency_label=("every other " if biweekly else "") + "/".join(weekdays),
                      start_date=start_date, end_date=end_date, start_time=start_time, end_time=end_time,
                      location=location)
    s.add(ser)
    s.flush()
    return ser


def skip_series_day(s: Session, series_id: int, day: dt.date) -> None:
    ser = s.get(EventSeries, series_id)
    if ser is None:
        raise NotFound(f"Series {series_id} not found.")
    ser.exceptions = sorted(set(ser.exceptions or []) | {day.isoformat()})
    s.flush()


def delete_event(s: Session, event_id: int) -> None:
    ev = s.get(CalendarEvent, event_id)
    if ev is None:
        raise NotFound(f"Event {event_id} not found.")
    s.delete(ev)
    s.flush()


def end_series(s: Session, series_id: int) -> None:
    ser = s.get(EventSeries, series_id)
    if ser is None:
        raise NotFound(f"Series {series_id} not found.")
    ser.active = False
    s.flush()


def event_to_task(s: Session, event_id: int, priority: int = 2) -> Task:
    """Turn a calendar item (e.g. an imported assignment due date) into a task due that day."""
    ev = s.get(CalendarEvent, event_id)
    if ev is None:
        raise NotFound(f"Event {event_id} not found.")
    if ev.task_id:
        raise ValidationError("Already a task.")
    task = T.create_task(s, title=ev.title, pillar=ev.pillar_code or "EDU", priority=priority, due_date=ev.day,
                         description=(ev.notes or "") + (f"\nFrom calendar: {ev.location}" if ev.location else ""))
    ev.task_id = task.id
    s.flush()
    return task


# ---------------------------------------------------------------- reading a day
@dataclass(frozen=True)
class Block:
    title: str
    day: dt.date
    start: dt.time | None
    end: dt.time | None
    kind: str
    location: str = ""
    event_id: int | None = None
    series_id: int | None = None
    task_id: int | None = None

    @property
    def minutes(self) -> int:
        if not (self.start and self.end):
            return 0
        return (dt.datetime.combine(self.day, self.end) - dt.datetime.combine(self.day, self.start)).seconds // 60

    @property
    def time_label(self) -> str:
        if not self.start:
            return "All day"
        f = lambda t: t.strftime("%-I:%M %p").replace(":00 ", " ").lower()  # noqa: E731
        return f"{f(self.start)}-{f(self.end)}"


def blocks_between(s: Session, lo: dt.date, hi: dt.date) -> list[Block]:
    out = [Block(e.title, e.day, e.start_time, e.end_time, e.kind, e.location, event_id=e.id, task_id=e.task_id)
           for e in s.scalars(select(CalendarEvent).where(CalendarEvent.day >= lo, CalendarEvent.day <= hi))]
    for ser in s.scalars(select(EventSeries).where(EventSeries.active.is_(True))):
        skips = [dt.date.fromisoformat(d) for d in ser.exceptions or []]
        for d in occurrence_dates(ser.rule, ser.start_date, lo, hi, end_date=ser.end_date, exceptions=skips):
            out.append(Block(ser.title, d, ser.start_time, ser.end_time, ser.kind, ser.location, series_id=ser.id))
    return sorted(out, key=lambda b: (b.day, b.start or dt.time.min, b.title))


@dataclass
class DayAgenda:
    day: dt.date
    blocks: list[Block] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)          # scheduled/due that day (and overdue, on today)
    routines: list[str] = field(default_factory=list)        # future routine occurrences (preview)
    deadlines: list = field(default_factory=list)            # cascade Deadline objects

    @property
    def empty(self) -> bool:
        return not (self.blocks or self.tasks or self.routines or self.deadlines)

    @property
    def booked_minutes(self) -> int:
        return sum(b.minutes for b in self.blocks)


def agenda(s: Session, start: dt.date, days: int, today: dt.date) -> list[DayAgenda]:
    from lifeos.services.cascade import upcoming_deadlines
    end = start + dt.timedelta(days=days - 1)
    settings = get_settings()
    out = {start + dt.timedelta(days=i): DayAgenda(start + dt.timedelta(days=i)) for i in range(days)}
    for b in blocks_between(s, start, end):
        out[b.day].blocks.append(b)
    for t in s.scalars(select(Task)):
        if t.status == S.COMPLETED and t.completed_at:
            d = settings.local_date(t.completed_at)
            if d in out and d <= today:
                out[d].tasks.append(t)
            continue
        if t.status not in OPEN_STATES and t.status != S.MISSED:
            continue
        d = t.effective_date
        if d is None:
            continue
        if t.status in OPEN_STATES and d < today and today in out:
            out[today].tasks.append(t)  # overdue work surfaces on today, never disappears
        elif d in out:
            out[d].tasks.append(t)
    for tpl in s.scalars(select(RecurringTemplate).where(RecurringTemplate.active.is_(True))):
        lo = max(start, today + dt.timedelta(days=1))
        if lo > end:
            continue
        skips = [dt.date.fromisoformat(x) for x in tpl.exceptions or []]
        for d in occurrence_dates(tpl.rule, tpl.start_date, lo, end, end_date=tpl.end_date, exceptions=skips):
            out[d].routines.append(tpl.title)
    for dl in upcoming_deadlines(s, today):
        if dl.date in out:
            out[dl.date].deadlines.append(dl)
    for a in out.values():
        a.tasks.sort(key=lambda t: (t.status == S.COMPLETED, t.priority, t.id))
    return [out[k] for k in sorted(out)]


def unscheduled_tasks(s: Session) -> list[Task]:
    """Open one-time tasks with no working day - the pile the weekly plan draws from."""
    rows = s.scalars(select(Task).where(Task.kind == TaskKind.ONE_TIME, Task.scheduled_date.is_(None),
                                        Task.status.in_([S.PENDING, S.IN_PROGRESS])))
    return sorted(rows, key=lambda t: (t.priority, t.due_date or dt.date.max, t.id))


def plan_task_on(s: Session, task_id: int, day: dt.date, today: dt.date, reason: str = "") -> Task:
    """Put a task on a day. Moving one that was already due to be worked on (today or earlier) to later is a
    slip, and needs a reason like any reschedule."""
    task = T.get_task(s, task_id)
    old = task.scheduled_date
    slipping = old is not None and old <= today and day > old
    return T.reschedule(s, task_id, day, reason, today=today, planned=not slipping)
