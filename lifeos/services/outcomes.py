"""Weekly outcomes: up to three finish lines per week, each with a next task, evidence, and an honest close-out.

Nothing rolls over silently: an unfinished outcome is closed (partial / missed / dropped) or explicitly carried,
and a long carry streak raises the "too big?" question.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import Goal, Pillar, PlanEvent, Task, WeeklyOutcome, WeeklyReview
from lifeos.services.planner_cal import week_start

MAX_OUTCOMES = 3
OPEN = ("open", "waiting")
CLOSED = ("achieved", "partial", "missed", "dropped", "carried")


def _log(s: Session, o: WeeklyOutcome, event: str, frm=None, to=None, reason=None) -> None:
    s.add(PlanEvent(entity_type="outcome", entity_code=f"W{o.week_start:%Y%m%d}-{o.id}", event_type=event,
                    from_value=None if frm is None else str(frm)[:200], to_value=None if to is None else str(to)[:200],
                    reason=reason))


def get_outcome(s: Session, outcome_id: int) -> WeeklyOutcome:
    o = s.get(WeeklyOutcome, outcome_id)
    if o is None:
        raise NotFound(f"Outcome {outcome_id} not found.")
    return o


def outcomes_for_week(s: Session, any_day: dt.date, include_carried: bool = False) -> list[WeeklyOutcome]:
    start = week_start(any_day)
    _migrate_focus(s, start)
    rows = s.scalars(select(WeeklyOutcome).where(WeeklyOutcome.week_start == start).order_by(WeeklyOutcome.id))
    return [o for o in rows if include_carried or o.status != "carried"]


def _migrate_focus(s: Session, start: dt.date) -> None:
    """Older weekly reviews stored next week's top 3 as plain text; turn them into outcomes once."""
    prev = s.scalar(select(WeeklyReview).where(WeeklyReview.week_start == start - dt.timedelta(days=7)))
    if not prev or not prev.focus:
        return
    if s.scalar(select(WeeklyOutcome).where(WeeklyOutcome.week_start == start).limit(1)):
        return
    for title in prev.focus[:MAX_OUTCOMES]:
        o = WeeklyOutcome(week_start=start, title=title)
        s.add(o)
        s.flush()
        _log(s, o, "created", to=title, reason="Converted from last week's top 3")


def add_outcome(s: Session, *, any_day: dt.date, title: str, pillar: str | None = None, goal_code: str | None = None,
                next_task_id: int | None = None) -> WeeklyOutcome:
    start = week_start(any_day)
    if not title or not title.strip():
        raise ValidationError("Describe the finish line - what will be visibly different by the end of the week?")
    live = [o for o in outcomes_for_week(s, start) if o.status in OPEN]
    if len(live) >= MAX_OUTCOMES:
        raise ValidationError(f"A week has at most {MAX_OUTCOMES} outcomes. Close or drop one first.")
    if pillar and s.get(Pillar, pillar) is None:
        raise ValidationError(f"Unknown pillar '{pillar}'.")
    goal = None
    if goal_code:
        goal = s.scalar(select(Goal).where(Goal.code == goal_code))
        if goal is None:
            raise ValidationError(f"Unknown goal '{goal_code}'.")
    if next_task_id is not None and s.get(Task, next_task_id) is None:
        raise NotFound(f"Task {next_task_id} not found.")
    o = WeeklyOutcome(week_start=start, title=title.strip(), pillar_code=pillar or (goal.pillar_code if goal else None),
                      goal_id=goal.id if goal else None, next_task_id=next_task_id)
    s.add(o)
    s.flush()
    _log(s, o, "created", to=o.title)
    return o


def update_outcome(s: Session, outcome_id: int, *, title: str | None = None, next_task_id=..., evidence: str | None = None,
                   waiting_until: dt.date | None = ..., today: dt.date | None = None) -> WeeklyOutcome:
    """Edit an open outcome. `waiting_until=<date>` parks it as waiting with a follow-up; `None` un-parks it."""
    o = get_outcome(s, outcome_id)
    if o.status not in OPEN:
        raise ValidationError("That outcome is closed. Reopen it at the weekly review if needed.")
    if title is not None and title.strip() and title.strip() != o.title:
        _log(s, o, "edited", frm=o.title, to=title.strip())
        o.title = title.strip()
    if next_task_id is not ...:
        if next_task_id is not None and s.get(Task, next_task_id) is None:
            raise NotFound(f"Task {next_task_id} not found.")
        _log(s, o, "next_task", frm=o.next_task_id, to=next_task_id)
        o.next_task_id = next_task_id
    if evidence is not None and evidence.strip() != (o.evidence or ""):
        _log(s, o, "evidence", to=evidence.strip())
        o.evidence = evidence.strip()
    if waiting_until is not ...:
        if waiting_until is None:
            o.status, o.follow_up_date = "open", None
            _log(s, o, "status", frm="waiting", to="open")
        else:
            if today and waiting_until < today:
                raise ValidationError("Follow-up date is in the past.")
            o.status, o.follow_up_date = "waiting", waiting_until
            _log(s, o, "status", frm="open", to=f"waiting until {waiting_until}")
    s.flush()
    return o


def close_outcome(s: Session, outcome_id: int, status: str, note: str = "") -> WeeklyOutcome:
    """achieved needs evidence; partial / missed / dropped need a short note on what happened."""
    o = get_outcome(s, outcome_id)
    if status not in ("achieved", "partial", "missed", "dropped"):
        raise ValidationError(f"Close an outcome as achieved, partial, missed or dropped - not '{status}'.")
    if o.status not in OPEN:
        raise ValidationError("Already closed.")
    note = (note or "").strip()
    if status == "achieved" and not (o.evidence or note):
        raise ReasonRequired("Achieved needs evidence - what shows the finish line was crossed?")
    if status != "achieved" and not note:
        raise ReasonRequired("Say briefly what happened (design, not discipline: too big? blocked? wrong week?).")
    if status == "achieved" and not o.evidence:
        o.evidence = note
    _log(s, o, "closed", frm=o.status, to=status, reason=note or None)
    o.status, o.close_note = status, note
    s.flush()
    return o


def carry_outcome(s: Session, outcome_id: int, note: str = "") -> WeeklyOutcome:
    """Explicitly move an unfinished outcome into next week (keeps the chain so carry streaks are visible)."""
    o = get_outcome(s, outcome_id)
    if o.status not in OPEN:
        raise ValidationError("Only open outcomes can be carried.")
    nxt_start = o.week_start + dt.timedelta(days=7)
    live_next = [x for x in outcomes_for_week(s, nxt_start) if x.status in OPEN]
    if len(live_next) >= MAX_OUTCOMES:
        raise ValidationError("Next week already has three outcomes.")
    new = WeeklyOutcome(week_start=nxt_start, title=o.title, pillar_code=o.pillar_code, goal_id=o.goal_id,
                        next_task_id=o.next_task_id, evidence=o.evidence, carried_from_id=o.id)
    s.add(new)
    o.status, o.close_note = "carried", (note or "").strip()
    s.flush()
    _log(s, o, "carried", to=f"week of {nxt_start}", reason=note or None)
    return new


def carry_streak(s: Session, o: WeeklyOutcome) -> int:
    """How many weeks in a row this finish line has been carried into."""
    n, cur = 0, o
    while cur.carried_from_id:
        n += 1
        cur = s.get(WeeklyOutcome, cur.carried_from_id)
    return n


def outcome_stats(s: Session, any_day: dt.date) -> dict:
    rows = outcomes_for_week(s, any_day, include_carried=True)
    return {"outcomes_total": len(rows), "outcomes_achieved": sum(o.status == "achieved" for o in rows),
            "outcomes_partial": sum(o.status == "partial" for o in rows),
            "outcomes_carried": sum(o.status == "carried" for o in rows)}
