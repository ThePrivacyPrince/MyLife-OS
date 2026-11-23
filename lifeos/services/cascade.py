"""Planning cascade service: Vision <- Goals <- Objectives <- Key Results <- tasks/routines.

Every change is written to PlanEvent (or TaskEvent for task links). Progress flows up the arrows:
completed tasks -> key result periods met -> objective hit-rate -> goal -> vision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.engines.cascade import (
    CADENCES, MEASURES, KRStatus, goal_horizon_warnings, kr_status, level_status, parse_amount, period_bounds,
    periods_in,
    validate_objective_window, vision_horizon_warnings,
)
from lifeos.engines.timeline import Countdown, countdown, quarter_slots, vision_deadline
from lifeos.enums import Actor, EventType, TaskStatus as S
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import (
    Goal, KeyResult, KeyResultCheckIn, Objective, Pillar, PlanEvent, RecurringTemplate, Task, Vision,
)
from lifeos.services.tasks import get_task, log

STATUSES = {
    "vision": ("active", "achieved", "revised", "dropped"),
    "goal": ("active", "achieved", "paused", "missed", "dropped"),
    "objective": ("active", "achieved", "missed", "dropped"),
    "key_result": ("active", "retired"),
}
MODELS = {"vision": Vision, "goal": Goal, "objective": Objective, "key_result": KeyResult}


# ---------------------------------------------------------------- helpers
def _plan_log(s: Session, entity: str, code: str, event: str, *, frm=None, to=None, reason=None,
              actor: Actor = Actor.USER) -> None:
    s.add(PlanEvent(entity_type=entity, entity_code=code, event_type=event,
                    from_value=None if frm is None else str(frm), to_value=None if to is None else str(to),
                    reason=reason, actor=str(actor)))


def _clean(title: str) -> str:
    if not title or not title.strip():
        raise ValidationError("title cannot be blank")
    return title.strip()


def get_by_code(s: Session, entity: str, code: str):
    model = MODELS[entity]
    row = s.scalar(select(model).where(model.code == code))
    if row is None:
        raise NotFound(f"{entity.replace('_', ' ').title()} '{code}' not found.")
    return row


def _next_code(s: Session, model, prefix: str) -> str:
    n = s.scalar(select(func.count()).select_from(model).where(model.code.like(f"{prefix}%"))) or 0
    while s.scalar(select(model).where(model.code == f"{prefix}{n + 1}")) is not None:
        n += 1
    return f"{prefix}{n + 1}"


def _next_goal_code(s: Session, pillar: str) -> str:
    """Goals keep the original FIN-G001 style numbering."""
    nums = [int(c.rsplit("-G", 1)[1]) for c in s.scalars(select(Goal.code).where(Goal.code.like(f"{pillar}-G%")))
            if c.rsplit("-G", 1)[1].isdigit()]
    return f"{pillar}-G{max(nums, default=0) + 1:03d}"


# ---------------------------------------------------------------- tier 1: vision
def create_vision(s: Session, *, pillar: str, title: str, description: str = "", target_year: int | None = None,
                  today: dt.date | None = None) -> tuple[Vision, list[str]]:
    if s.get(Pillar, pillar) is None:
        raise ValidationError(f"Unknown pillar '{pillar}'.")
    if target_year is None:
        raise ValidationError("A vision needs a target year - when do you want to be there?")
    today = today or get_settings().today()
    c = get_settings().cascade
    v = Vision(code=_next_code(s, Vision, f"{pillar}-V"), pillar_code=pillar, title=_clean(title),
               description=description, target_year=target_year)
    s.add(v)
    s.flush()
    _plan_log(s, "vision", v.code, "created", to=v.title)
    return v, vision_horizon_warnings(today, target_year, c.vision_min_years, c.vision_max_years)


# ---------------------------------------------------------------- tier 2: goal
def create_goal(s: Session, *, pillar: str, title: str, description: str = "", target_date: dt.date | None = None,
                vision_code: str | None = None, progress_evidence: str = "", secondary: list[str] | None = None,
                today: dt.date | None = None) -> tuple[Goal, list[str]]:
    if s.get(Pillar, pillar) is None:
        raise ValidationError(f"Unknown pillar '{pillar}'.")
    if target_date is None:
        raise ValidationError("A goal needs a deadline - when should you reach it?")
    today = today or get_settings().today()
    c = get_settings().cascade
    vision = get_by_code(s, "vision", vision_code) if vision_code else None
    g = Goal(code=_next_goal_code(s, pillar), pillar_code=pillar, title=_clean(title),
             description=description, target_date=target_date, progress_evidence=progress_evidence,
             secondary_pillars=secondary or [], vision_id=vision.id if vision else None)
    s.add(g)
    s.flush()
    _plan_log(s, "goal", g.code, "created", to=g.title)
    warnings = goal_horizon_warnings(today, target_date, c.goal_min_months, c.goal_max_months)
    if vision is None:
        warnings.append("Not connected to a vision yet - which 3-5 year vision does this serve?")
    return g, warnings


def link_goal_to_vision(s: Session, goal_code: str, vision_code: str | None, reason: str = "") -> Goal:
    goal = get_by_code(s, "goal", goal_code)
    vision = get_by_code(s, "vision", vision_code) if vision_code else None
    old = goal.vision.code if goal.vision else None
    goal.vision_id = vision.id if vision else None
    s.flush()
    s.refresh(goal)
    _plan_log(s, "goal", goal.code, "linked_vision", frm=old, to=vision_code, reason=reason or None)
    return goal


# ---------------------------------------------------------------- tier 3: objective
def create_objective(s: Session, *, goal_code: str, title: str, start_date: dt.date,
                     end_date: dt.date | None = None, description: str = "") -> Objective:
    goal = get_by_code(s, "goal", goal_code)
    c = get_settings().cascade
    end_date = end_date or start_date + dt.timedelta(days=c.objective_default_days - 1)  # 91 days inclusive
    validate_objective_window(start_date, end_date, c.objective_max_days)
    if goal.target_date and start_date > goal.target_date:
        raise ValidationError("Objective starts after its goal's target date.")
    o = Objective(code=_next_code(s, Objective, f"{goal.code}-O"), goal_id=goal.id, title=_clean(title),
                  description=description, start_date=start_date, end_date=end_date)
    s.add(o)
    s.flush()
    s.expire(goal, ["objectives"])  # keep goal.objectives in sync for timelines and gap checks
    _plan_log(s, "objective", o.code, "created", to=f"{o.title} ({start_date} to {end_date})")
    return o


# ---------------------------------------------------------------- tier 4: key result
def create_key_result(s: Session, *, objective_code: str, title: str, cadence: str, target,
                      measure: str = "completions", unit: str = "") -> KeyResult:
    obj = get_by_code(s, "objective", objective_code)
    if cadence not in CADENCES:
        raise ValidationError(f"Key results are daily or weekly, not '{cadence}'.")
    if measure not in MEASURES:
        raise ValidationError(f"Unknown measure '{measure}'. Use {MEASURES}.")
    # No target given = "track only": the number is recorded and shown, never judged against an invented goal.
    target = Decimal(0) if target is None or str(target).strip() == "" else parse_amount(target)
    if target < 0:
        raise ValidationError("Target can't be negative.")
    kr = KeyResult(code=_next_code(s, KeyResult, f"{obj.code}-KR"), objective_id=obj.id, title=_clean(title),
                   cadence=cadence, measure=measure, target=target, unit=unit.strip())
    s.add(kr)
    s.flush()
    _plan_log(s, "key_result", kr.code, "created", to=f"{kr.title}: {target} {unit} per {'day' if cadence == 'daily' else 'week'}")
    return kr


def check_in(s: Session, kr_code: str, day: dt.date, value, note: str = "") -> KeyResultCheckIn:
    kr = get_by_code(s, "key_result", kr_code)
    if kr.measure == "completions":
        raise ValidationError("This key result is measured by completing its linked tasks - just complete them.")
    ci = KeyResultCheckIn(key_result_id=kr.id, day=day, value=parse_amount(value), note=note.strip())
    s.add(ci)
    s.flush()
    s.expire(kr, ["checkins"])  # keep kr.checkins in sync for progress reads in the same session
    _plan_log(s, "key_result", kr.code, "check_in", to=f"{ci.value} on {day}", reason=note or None)
    return ci


def set_status(s: Session, entity: str, code: str, status: str, reason: str) -> None:
    if entity not in STATUSES or status not in STATUSES[entity]:
        raise ValidationError(f"Invalid status '{status}' for {entity}.")
    if not (reason and reason.strip()):
        raise ReasonRequired("Closing or changing a plan item needs a reason (what happened?).")
    row = get_by_code(s, entity, code)
    old = row.status
    row.status = status
    _plan_log(s, entity, code, "status_change", frm=old, to=status, reason=reason.strip())
    s.flush()


# ---------------------------------------------------------------- linking tasks upward
def link_task(s: Session, task_id: int, kr_code: str | None) -> Task:
    task = get_task(s, task_id)
    kr = get_by_code(s, "key_result", kr_code) if kr_code else None
    old = task.key_result.code if task.key_result else None
    task.key_result_id = kr.id if kr else None
    s.flush()
    s.refresh(task)
    log(s, task, EventType.EDITED, frm=f"key_result={old}", to=f"key_result={kr_code}")
    return task


def link_template(s: Session, template_id: int, kr_code: str | None) -> RecurringTemplate:
    tpl = s.get(RecurringTemplate, template_id)
    if tpl is None:
        raise NotFound(f"Recurring template {template_id} not found.")
    kr = get_by_code(s, "key_result", kr_code) if kr_code else None
    tpl.key_result_id = kr.id if kr else None
    # Open occurrences follow the routine; completed history keeps the link it was completed under.
    for occ in tpl.occurrences:
        if occ.status in (S.PENDING, S.IN_PROGRESS, S.BLOCKED, S.WAITING, S.DEFERRED, S.MISSED):
            occ.key_result_id = tpl.key_result_id
    s.flush()
    _plan_log(s, "key_result", kr_code or "-", "linked_routine", to=f"template #{tpl.id} {tpl.title}")
    return tpl


@dataclass(frozen=True)
class Link:
    tier: str
    code: str
    title: str


def lineage(task: Task) -> list[Link]:
    """The upward chain a task serves: key result -> objective -> goal -> vision."""
    chain: list[Link] = []
    kr = task.key_result
    if kr:
        chain.append(Link("key_result", kr.code, kr.title))
        obj = kr.objective
        chain.append(Link("objective", obj.code, obj.title))
        goal = obj.goal
    else:
        goal = task.goal
    if goal:
        chain.append(Link("goal", goal.code, goal.title))
        if goal.vision:
            chain.append(Link("vision", goal.vision.code, goal.vision.title))
    return chain


# ---------------------------------------------------------------- measuring (the arrows)
def kr_actual(s: Session, kr: KeyResult, lo: dt.date, hi: dt.date) -> Decimal:
    settings = get_settings()
    if kr.measure == "manual":
        return sum((c.value for c in kr.checkins if lo <= c.day <= hi), Decimal(0))
    done = s.scalars(select(Task).where(Task.key_result_id == kr.id, Task.status == S.COMPLETED))
    return Decimal(sum(1 for t in done if t.completed_at and lo <= settings.local_date(t.completed_at) <= hi))


def kr_latest(kr: KeyResult, as_of: dt.date) -> tuple[Decimal | None, dt.date | None]:
    """Most recent reading on or before `as_of` (for level key results)."""
    readings = [c for c in kr.checkins if c.day <= as_of]
    if not readings:
        return None, None
    last = max(readings, key=lambda c: (c.day, c.id))
    return last.value, last.day


def kr_progress(s: Session, kr: KeyResult, day: dt.date) -> tuple[tuple[dt.date, dt.date], KRStatus]:
    period = period_bounds(kr.cadence, day)
    if day < kr.objective.start_date:
        # A phase that hasn't started can't be behind.
        return period, KRStatus(Decimal(0), kr.target, "not_started")
    if kr.measure == "level":
        return period, level_status(kr_latest(kr, day)[0], kr.target)
    return period, kr_status(kr_actual(s, kr, *period), kr.target, period, day)


@dataclass(frozen=True)
class Health:
    met: int
    total: int  # completed periods so far

    @property
    def rate(self) -> float | None:
        return None if self.total == 0 else self.met / self.total


def objective_health(s: Session, obj: Objective, day: dt.date) -> Health:
    """Share of finished key-result periods (inside the objective window) whose target was met."""
    met = total = 0
    last = min(day - dt.timedelta(days=1), obj.end_date)
    for kr in obj.key_results:
        # Only weekly/daily flows with a real target have periods to hit; levels and track-only numbers don't.
        if kr.status != "active" or kr.measure == "level" or kr.target <= 0:
            continue
        for lo, hi in periods_in(kr.cadence, obj.start_date, last) if last >= obj.start_date else []:
            if hi > last:  # period not finished yet
                continue
            total += 1
            met += kr_actual(s, kr, lo, hi) >= kr.target
    return Health(met, total)


def active_objectives(goal: Goal, day: dt.date) -> list[Objective]:
    return [o for o in goal.objectives if o.status == "active" and o.start_date <= day <= o.end_date]


def active_key_results(s: Session, day: dt.date) -> list[KeyResult]:
    rows = s.scalars(select(KeyResult).join(Objective).where(
        KeyResult.status == "active", Objective.status == "active",
        Objective.start_date <= day, Objective.end_date >= day).order_by(KeyResult.code))
    return list(rows)


# ---------------------------------------------------------------- gaps in the cascade
@dataclass(frozen=True)
class Gap:
    tier: str
    code: str | None
    message: str


def cascade_gaps(s: Session, day: dt.date) -> list[Gap]:
    gaps: list[Gap] = []
    visions = list(s.scalars(select(Vision).where(Vision.status == "active")))
    goals = list(s.scalars(select(Goal).where(Goal.status == "active")))
    for p in s.scalars(select(Pillar).order_by(Pillar.sort_order)):
        if not any(v.pillar_code == p.code for v in visions):
            gaps.append(Gap("vision", None, f"{p.name}: no 3-5 year vision yet. Where do you want to be?"))
    lead = get_settings().cascade.next_objective_lead_days
    for v in visions:
        if v.target_year is None:
            gaps.append(Gap("vision", v.code, f"Vision '{v.title}' has no target year - no countdown."))
        elif vision_deadline(v.target_year) < day:
            gaps.append(Gap("vision", v.code, f"Vision '{v.title}' reached its target year - close it out or "
                                              "set the next horizon."))
        if not any(g.vision_id == v.id for g in goals):
            gaps.append(Gap("vision", v.code, f"Vision '{v.title}' has no 6-12 month goal moving it forward."))
    for g in goals:
        if g.vision_id is None:
            gaps.append(Gap("goal", g.code, f"Goal {g.code} '{g.title}' isn't connected to a vision."))
        if g.target_date is None:
            gaps.append(Gap("goal", g.code, f"Goal {g.code} '{g.title}' has no deadline - set one so it counts down."))
        elif g.target_date < day:
            gaps.append(Gap("goal", g.code, f"Goal {g.code} '{g.title}' passed its deadline ({g.target_date}) - "
                                            "close it out: achieved, missed, or move the deadline with a reason."))
        moves = deadline_moves(s, "goal", g.code)
        if moves >= 2:
            gaps.append(Gap("goal", g.code, f"Goal {g.code}'s deadline has moved {moves} times. Is it realistic, "
                                            "or does it need smaller objectives?"))
        upcoming = [o for o in g.objectives if o.status == "active" and o.start_date > day]
        if not active_objectives(g, day) and not upcoming:
            gaps.append(Gap("goal", g.code, f"Goal {g.code} '{g.title}' has no objective planned - "
                                            "nothing is moving it this quarter."))
    for o in s.scalars(select(Objective).where(Objective.status == "active")):
        if o.end_date < day:
            gaps.append(Gap("objective", o.code, f"Objective {o.code} ended {o.end_date} - close it out: "
                                                 "achieved, missed, or dropped?"))
        elif not any(kr.status == "active" for kr in o.key_results):
            gaps.append(Gap("objective", o.code, f"Objective '{o.title}' has no daily/weekly key results."))
        if day <= o.end_date and (o.end_date - day).days <= lead and o.goal.status == "active" \
                and (o.goal.target_date is None or o.end_date < o.goal.target_date) \
                and not any(n.start_date > o.end_date - dt.timedelta(days=7) and n.id != o.id
                            and n.status != "dropped" for n in o.goal.objectives):
            gaps.append(Gap("objective", o.code, f"'{o.title}' ends in {(o.end_date - day).days} days - plan "
                                                 f"next quarter's objective for {o.goal.code} so the chain keeps going."))
    for kr in active_key_results(s, day):
        if kr.measure == "completions" and not kr.tasks and not kr.templates:
            gaps.append(Gap("key_result", kr.code, f"Key result '{kr.title}' has no task or routine driving it."))
    return gaps


# ---------------------------------------------------------------- deadlines, countdowns, timelines
def _created(row) -> dt.date:
    return get_settings().local_date(row.created_at) if row.created_at else get_settings().today()


def deadline_of(entity: str, row) -> dt.date | None:
    return {"vision": lambda r: vision_deadline(r.target_year), "goal": lambda r: r.target_date,
            "objective": lambda r: r.end_date}[entity](row)


def start_of(entity: str, row) -> dt.date:
    if entity == "objective":
        return row.start_date
    if entity == "goal" and row.objectives:
        return min(_created(row), min(o.start_date for o in row.objectives))
    return _created(row)


def countdown_for(entity: str, row, today: dt.date) -> Countdown:
    return countdown(start_of(entity, row), deadline_of(entity, row), today, get_settings().cascade.due_soon_days)


def change_deadline(s: Session, entity: str, code: str, new, reason: str = "") -> None:
    """Set or move a deadline. Setting the first one is free; MOVING one needs a reason and is counted."""
    row = get_by_code(s, entity, code)
    old = deadline_of(entity, row)
    if old is not None and not (reason and reason.strip()):
        raise ReasonRequired("Moving a deadline needs a reason - it's how you avoid quietly starting over.")
    if entity == "vision":
        year = int(new)
        row.target_year = year
        new_date = vision_deadline(year)
    elif entity == "goal":
        if not isinstance(new, dt.date):
            raise ValidationError("Goal deadline must be a date.")
        row.target_date = new_date = new
    elif entity == "objective":
        if not isinstance(new, dt.date):
            raise ValidationError("Objective end must be a date.")
        validate_objective_window(row.start_date, new, get_settings().cascade.objective_max_days)
        row.end_date = new_date = new
    else:
        raise ValidationError(f"{entity} has no deadline.")
    if old == new_date:
        raise ValidationError("That's the same deadline.")
    _plan_log(s, entity, code, "deadline_set" if old is None else "deadline_moved", frm=old, to=new_date,
              reason=(reason or "").strip() or None)
    s.flush()


def deadline_moves(s: Session, entity: str, code: str) -> int:
    return s.scalar(select(func.count()).select_from(PlanEvent).where(
        PlanEvent.entity_type == entity, PlanEvent.entity_code == code,
        PlanEvent.event_type == "deadline_moved")) or 0


@dataclass(frozen=True)
class Slot:
    start: dt.date
    end: dt.date
    objectives: tuple[Objective, ...]
    state: str  # achieved | missed | active | planned | unplanned | gap (past, nothing planned) | lead
    current: bool


def goal_timeline(goal: Goal, today: dt.date) -> list[Slot]:
    """The goal's runway cut into quarters, each showing what (if anything) was planned for it."""
    if goal.target_date is None:
        return []
    live = [o for o in goal.objectives if o.status != "dropped"]
    first_start = min((o.start_date for o in live), default=None)
    slots = []
    for lo, hi in quarter_slots(start_of("goal", goal), goal.target_date):
        objs = tuple(o for o in live if o.start_date <= hi and o.end_date >= lo)
        current = lo <= today <= hi
        if not objs and first_start and hi < first_start:
            state = "lead"  # sequenced goal: its first phase is planned to start later
        elif not objs:
            state = "gap" if hi < today else "unplanned"
        elif all(o.status == "achieved" for o in objs):
            state = "achieved"
        elif any(o.status == "missed" for o in objs) and hi < today:
            state = "missed"
        elif current:
            state = "active"
        else:
            state = "planned"
        slots.append(Slot(lo, hi, objs, state, current))
    return slots


@dataclass(frozen=True)
class Deadline:
    date: dt.date
    tier: str
    code: str
    title: str
    countdown: Countdown
    moves: int


def upcoming_deadlines(s: Session, today: dt.date, include_overdue: bool = True) -> list[Deadline]:
    """Every open commitment with a date, soonest first - the roadmap."""
    out: list[Deadline] = []
    for entity, model in (("vision", Vision), ("goal", Goal), ("objective", Objective)):
        for row in s.scalars(select(model).where(model.status == "active")):
            d = deadline_of(entity, row)
            if d is None or (d < today and not include_overdue):
                continue
            out.append(Deadline(d, entity, row.code, row.title, countdown_for(entity, row, today),
                                deadline_moves(s, entity, row.code)))
    return sorted(out, key=lambda x: (x.date, ("objective", "goal", "vision").index(x.tier)))


def unlinked_open_tasks(s: Session) -> list[Task]:
    rows = s.scalars(select(Task).where(Task.key_result_id.is_(None), Task.protected.is_(False),
                                        Task.status.in_([S.PENDING, S.IN_PROGRESS])))
    return list(rows)
