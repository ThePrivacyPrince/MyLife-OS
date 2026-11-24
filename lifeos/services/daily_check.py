"""The evening check - three questions that plan tomorrow:

1. Progress:    What's one action tomorrow that moves a weekly outcome?
2. Negotiation: What's one thing I can ask for tomorrow that would improve the price, timing, terms or scope?
                Who will I ask, and what exactly will I say?
3. Capacity:    What do I need to protect tomorrow, and what moves if the day is full?

It runs alongside the "Review tomorrow's schedule" routine (not a clock time), so it works with overnights.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import utcnow
from lifeos.engines.state_machine import OPEN_STATES
from lifeos.enums import TaskStatus as S
from lifeos.errors import ReasonRequired, ValidationError
from lifeos.models import DailyCheck, Task
from lifeos.services import tasks as T
from lifeos.services.outcomes import get_outcome, update_outcome
from lifeos.services.planner_cal import plan_task_on

ASK_AREAS = ("price", "timing", "terms", "scope")
ASK_RESULTS = ("got", "partial", "no", "not_asked")


def get_check(s: Session, for_date: dt.date) -> DailyCheck | None:
    return s.scalar(select(DailyCheck).where(DailyCheck.for_date == for_date))


def tasks_on(s: Session, day: dt.date) -> list[Task]:
    """Open, unprotected work landing on `day` - the candidates for 'what moves if the day is full'."""
    rows = s.scalars(select(Task).where(Task.status.in_([str(x) for x in OPEN_STATES]), Task.protected.is_(False)))
    return sorted((t for t in rows if t.effective_date == day), key=lambda t: (-t.priority, t.id))


def save_check(s: Session, *, for_date: dt.date, today: dt.date, outcome_id: int | None = None,
               task_id: int | None = None, new_task_title: str = "", new_task_pillar: str = "BUS",
               no_ask: bool = False, ask_who: str = "", ask_area: str = "", ask_what: str = "", ask_script: str = "",
               protect_note: str = "", awake_hours: float | None = None, bump_task_ids: list[int] | None = None
               ) -> DailyCheck:
    if for_date < today:
        raise ValidationError("You can only plan today or a future day.")
    check = get_check(s, for_date) or DailyCheck(for_date=for_date)

    # 1. Progress - one action that moves an outcome; it becomes (or is planned as) a real task on that day.
    if outcome_id is not None:
        outcome = get_outcome(s, outcome_id)
        if new_task_title.strip():
            task = T.create_task(s, title=new_task_title.strip(), pillar=outcome.pillar_code or new_task_pillar,
                                 priority=2, scheduled_date=for_date,
                                 goal_code=outcome.goal.code if outcome.goal else None)
        elif task_id is not None:
            task = T.get_task(s, task_id)
            if task.status not in (S.PENDING, S.IN_PROGRESS, S.WAITING, S.BLOCKED):
                raise ValidationError(f"'{task.title}' isn't open.")
            if task.scheduled_date != for_date and task.status in (S.PENDING, S.IN_PROGRESS):
                plan_task_on(s, task.id, for_date, today=today,
                             reason=f"Chosen in the daily check to move '{outcome.title}'")
        else:
            raise ValidationError("Pick the task that moves the outcome, or type a new one.")
        update_outcome(s, outcome.id, next_task_id=task.id)
        check.progress_outcome_id, check.progress_task_id = outcome.id, task.id

    # 2. Negotiation - one concrete ask, or an honest "nothing worth asking".
    check.no_ask = bool(no_ask)
    if no_ask:
        check.ask_who = check.ask_area = check.ask_what = check.ask_script = ""
    elif any(x.strip() for x in (ask_who, ask_what, ask_script)) or ask_area:
        if ask_area not in ASK_AREAS:
            raise ValidationError(f"What does the ask improve? One of {ASK_AREAS}.")
        if not (ask_who.strip() and ask_what.strip() and ask_script.strip()):
            raise ValidationError("An ask needs who, what you want, and the exact words you'll say.")
        check.ask_who, check.ask_area = ask_who.strip(), ask_area
        check.ask_what, check.ask_script = ask_what.strip(), ask_script.strip()

    # 3. Capacity - what's protected, how long you're awake, and what moves first if the day overflows.
    check.protect_note = protect_note.strip()
    if awake_hours is not None:
        if not 0 <= float(awake_hours) <= 24:
            raise ValidationError("Awake hours must be between 0 and 24.")
        check.awake_minutes = int(round(float(awake_hours) * 60))
    candidates = {t.id for t in tasks_on(s, for_date)}
    bump = [i for i in (bump_task_ids or []) if i != check.progress_task_id]
    unknown = [i for i in bump if i not in candidates]
    if unknown:
        raise ValidationError("Only work planned for that day can be on the 'moves first' list.")
    check.bump_task_ids = bump
    s.add(check)
    s.flush()
    return check


def record_ask(s: Session, for_date: dt.date, result: str, gain: str = "") -> DailyCheck:
    check = get_check(s, for_date)
    if check is None or check.no_ask or not check.ask_what:
        raise ValidationError("No ask was planned for that day.")
    if result not in ASK_RESULTS:
        raise ValidationError(f"Result is one of {ASK_RESULTS}.")
    if result in ("got", "partial") and not gain.strip():
        raise ReasonRequired("What did you get? (e.g. '$300 off', 'pay date moved to Friday', 'free trailer').")
    check.ask_result, check.ask_gain, check.ask_result_at = result, gain.strip(), utcnow()
    s.flush()
    return check


def move_bumped(s: Session, day: dt.date, today: dt.date) -> list[Task]:
    """The day is full: move the pre-decided tasks to the next day."""
    check = get_check(s, day)
    if not check or not check.bump_task_ids:
        raise ValidationError("Nothing was pre-decided to move for that day.")
    moved = []
    for tid in check.bump_task_ids:
        t = s.get(Task, tid)
        if t and t.status in (S.PENDING, S.IN_PROGRESS) and t.effective_date == day:
            plan_task_on(s, t.id, day + dt.timedelta(days=1), today=today,
                         reason="Day full - moved as pre-decided in the daily check")
            moved.append(t)
    return moved


def check_due(s: Session, today: dt.date) -> bool:
    """Prompt the evening check while tomorrow isn't planned yet."""
    return get_check(s, today + dt.timedelta(days=1)) is None


def week_check_stats(s: Session, start: dt.date, today: dt.date) -> dict:
    """Daily checks done for days in the week, and how the asks went."""
    days = [start + dt.timedelta(days=i) for i in range(7)]
    rows = [c for c in s.scalars(select(DailyCheck).where(DailyCheck.for_date >= start,
                                                          DailyCheck.for_date <= days[-1]))]
    asks = [c for c in rows if not c.no_ask and c.ask_what]
    return {"checks_done": len(rows), "checks_possible": sum(1 for d in days if d <= today + dt.timedelta(days=1)),
            "asks_planned": len(asks), "asks_got": sum(c.ask_result == "got" for c in asks),
            "asks_partial": sum(c.ask_result == "partial" for c in asks),
            "asks_no": sum(c.ask_result == "no" for c in asks),
            "asks_skipped": sum(c.ask_result == "not_asked" for c in asks),
            "gains": [f"{c.for_date:%a}: {c.ask_gain}" for c in asks if c.ask_gain]}


def missed_twice(s: Session, today: dt.date) -> bool:
    """'Never miss twice': no check for yesterday's plan of today AND none for today's plan of tomorrow."""
    return get_check(s, today) is None and get_check(s, today - dt.timedelta(days=1)) is None and \
        s.scalar(select(DailyCheck).limit(1)) is not None


def set_ask(s: Session, *, for_date: dt.date, today: dt.date, who: str, area: str, what: str, script: str) -> DailyCheck:
    """Fill just the 'ask' part of a day's evening check (keeps progress and capacity answers untouched)."""
    if for_date < today:
        raise ValidationError("You can only plan today or a future day.")
    if area not in ASK_AREAS:
        raise ValidationError(f"Area is one of {ASK_AREAS}.")
    if not (who.strip() and what.strip() and script.strip()):
        raise ValidationError("An ask needs who, what, and the words you'll say.")
    check = get_check(s, for_date) or DailyCheck(for_date=for_date, bump_task_ids=[])
    check.no_ask = False
    check.ask_who, check.ask_area, check.ask_what, check.ask_script = who.strip(), area, what.strip(), script.strip()
    check.ask_result = None
    s.add(check)
    s.flush()
    return check
