"""Weekly review: the end-of-week loop. Score the week from the records, reflect, set next week's top 3.

Week = Monday to Sunday (the same weeks key results use). Metrics come from what actually happened -
nothing is typed in to make the numbers look better.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.state_machine import OPEN_STATES
from lifeos.enums import EventType, TaskKind, TaskStatus as S
from lifeos.errors import ValidationError
from lifeos.models import KeyResult, Objective, Task, TaskEvent, WeeklyReview
from lifeos.services import cascade as C
from lifeos.services.planner_cal import blocks_between, week_start


@dataclass
class WeekSummary:
    start: dt.date
    end: dt.date
    completed: list[Task] = field(default_factory=list)
    slipped: list[Task] = field(default_factory=list)        # planned/due this week, not done
    missed_routines: list[Task] = field(default_factory=list)
    p1_done: int = 0
    p1_total: int = 0
    routine_done: int = 0
    routine_total: int = 0
    rescheduled: int = 0
    overrides: int = 0
    key_results: list = field(default_factory=list)          # (KeyResult, KRStatus)
    minutes_by_kind: dict = field(default_factory=dict)
    deadlines_ahead: list = field(default_factory=list)       # next 14 days after the week

    @staticmethod
    def _rate(a: int, b: int) -> float | None:
        return None if b == 0 else a / b

    @property
    def p1_rate(self) -> float | None:
        return self._rate(self.p1_done, self.p1_total)

    @property
    def routine_rate(self) -> float | None:
        return self._rate(self.routine_done, self.routine_total)

    @property
    def kr_met(self) -> int:
        return sum(1 for _, st in self.key_results if st.status == "met")

    @property
    def kr_scored(self) -> int:
        return sum(1 for _, st in self.key_results if st.status in ("met", "missed", "behind", "on_track", "below"))

    outcomes: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)

    def metrics(self) -> dict:
        return {
            **self.outcomes,
            **{k: v for k, v in self.checks.items() if k != "gains"},
            "completed": len(self.completed), "slipped": len(self.slipped),
            "p1_done": self.p1_done, "p1_total": self.p1_total,
            "routine_done": self.routine_done, "routine_total": self.routine_total,
            "kr_met": self.kr_met, "kr_scored": self.kr_scored,
            "rescheduled": self.rescheduled, "overrides": self.overrides,
            "class_hours": round(self.minutes_by_kind.get("class", 0) / 60, 1),
            "study_hours": round(self.minutes_by_kind.get("study", 0) / 60, 1),
        }


def week_summary(s: Session, start: dt.date, today: dt.date) -> WeekSummary:
    start = week_start(start)
    end = start + dt.timedelta(days=6)
    settings = get_settings()
    w = WeekSummary(start, end)
    in_week = lambda d: d is not None and start <= d <= end  # noqa: E731
    # Only days that are over can be judged: today's unfinished work hasn't slipped yet.
    cutoff = min(end, today - dt.timedelta(days=1))
    for t in s.scalars(select(Task)):
        if t.protected:
            continue  # sleep, shifts - commitments, not scorecard items
        done_day = settings.local_date(t.completed_at) if t.completed_at else None
        done_here = t.status == S.COMPLETED and in_week(done_day)
        if done_here:
            w.completed.append(t)
        planned_here = in_week(t.effective_date) and t.effective_date <= cutoff
        if t.kind == TaskKind.OCCURRENCE and in_week(t.occurrence_date) and t.status != S.CANCELLED \
                and (t.occurrence_date <= cutoff or done_here):
            w.routine_total += 1
            w.routine_done += t.status == S.COMPLETED
            if t.status == S.MISSED:
                w.missed_routines.append(t)
        elif t.kind != TaskKind.OCCURRENCE and planned_here and t.status in OPEN_STATES:
            w.slipped.append(t)
        if t.priority == 1 and t.status != S.CANCELLED and \
                (done_here or (planned_here and t.status != S.COMPLETED)):
            w.p1_total += 1
            w.p1_done += t.status == S.COMPLETED
    lo, hi = dt.datetime.combine(start - dt.timedelta(days=1), dt.time.min), \
        dt.datetime.combine(end + dt.timedelta(days=2), dt.time.min)
    for e in s.scalars(select(TaskEvent).where(TaskEvent.at >= lo, TaskEvent.at < hi)):
        if in_week(settings.local_date(e.at)):
            w.rescheduled += e.event_type == str(EventType.RESCHEDULED) and e.actor == "user"
            w.overrides += e.event_type == str(EventType.GATE_OVERRIDE)
    as_of = min(end, today)
    for kr in s.scalars(select(KeyResult).join(Objective).where(
            KeyResult.status == "active", Objective.start_date <= end, Objective.end_date >= start)):
        w.key_results.append((kr, C.kr_progress(s, kr, as_of)[1]))
    for b in blocks_between(s, start, end):
        w.minutes_by_kind[b.kind] = w.minutes_by_kind.get(b.kind, 0) + b.minutes
    from lifeos.services.daily_check import week_check_stats
    from lifeos.services.outcomes import outcome_stats
    w.outcomes = outcome_stats(s, start)
    w.checks = week_check_stats(s, start, today)
    horizon = end + dt.timedelta(days=14)
    w.deadlines_ahead = [d for d in C.upcoming_deadlines(s, today, include_overdue=False) if end < d.date <= horizon]
    return w


def tasks_due_between(s: Session, lo: dt.date, hi: dt.date) -> list[Task]:
    rows = s.scalars(select(Task).where(Task.due_date >= lo, Task.due_date <= hi,
                                        Task.status.in_([str(x) for x in OPEN_STATES])))
    return sorted(rows, key=lambda t: (t.due_date, t.priority))


def save_weekly_review(s: Session, start: dt.date, today: dt.date, *, wins: str = "", slipped: str = "",
                       lessons: str = "", energy: int | None = None, focus: list[str] | None = None) -> WeeklyReview:
    start = week_start(start)
    if start > today:
        raise ValidationError("You can't review a week that hasn't started.")
    if energy is not None and not 1 <= int(energy) <= 5:
        raise ValidationError("Energy is 1 to 5.")
    focus = [f.strip() for f in (focus or []) if f and f.strip()][:3]
    row = s.scalar(select(WeeklyReview).where(WeeklyReview.week_start == start)) or WeeklyReview(week_start=start)
    row.wins, row.slipped, row.lessons = wins.strip(), slipped.strip(), lessons.strip()
    row.energy = int(energy) if energy is not None else None
    row.focus = focus
    row.summary = week_summary(s, start, today).metrics()
    s.add(row)
    s.flush()
    # Next week's top 3 are real outcomes (finish lines), not just text.
    from lifeos.services.outcomes import add_outcome, outcomes_for_week
    nxt = start + dt.timedelta(days=7)
    existing = {o.title.lower() for o in outcomes_for_week(s, nxt)}
    for title in focus:
        if title.lower() not in existing and len([o for o in outcomes_for_week(s, nxt)
                                                  if o.status in ("open", "waiting")]) < 3:
            add_outcome(s, any_day=nxt, title=title)
    return row


def get_review(s: Session, start: dt.date) -> WeeklyReview | None:
    return s.scalar(select(WeeklyReview).where(WeeklyReview.week_start == week_start(start)))


def review_due(s: Session, today: dt.date) -> dt.date | None:
    """The week that needs reviewing now, if any: this week on review day, or last week on the Monday after."""
    day = get_settings().planner.weekly_review_day
    if today.weekday() == day:
        target = week_start(today)
    elif today.weekday() == 0 and day == 6:
        target = week_start(today) - dt.timedelta(days=7)
    else:
        return None
    if get_review(s, target):
        return None
    first = s.scalar(select(Task.created_at).order_by(Task.created_at).limit(1))
    if first is None or get_settings().local_date(first) > target + dt.timedelta(days=6):
        return None  # LIFE OS wasn't in use that week - nothing to review
    return target


def current_focus(s: Session, today: dt.date) -> list[str]:
    """This week's outcomes (finish lines) - set at last week's review or added during the week."""
    from lifeos.services.outcomes import outcomes_for_week
    return [o.title for o in outcomes_for_week(s, today) if o.status != "dropped"]


def history(s: Session, limit: int = 8) -> list[WeeklyReview]:
    return list(s.scalars(select(WeeklyReview).order_by(WeeklyReview.week_start.desc()).limit(limit)))
