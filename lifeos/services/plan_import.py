"""Import a whole plan (vision -> goals -> objectives -> key results -> tasks) from a YAML file.

Plan files hold personal and financial details, so they live in your git-ignored `data/` folder, never in the repo.
The import is all-or-nothing: if any part fails validation, nothing is written. A plan can be imported once.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.errors import ValidationError
from lifeos.models import Goal, PlanEvent
from lifeos.services import cascade as C
from lifeos.services.tasks import create_task


def _req(d: dict, key: str, where: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ValidationError(f"{where}: '{key}' is required.")
    return d[key]


def _date(v, where: str) -> dt.date:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError as exc:
        raise ValidationError(f"{where}: '{v}' is not a date (use YYYY-MM-DD).") from exc


def import_plan(s: Session, data: dict, today: dt.date) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("Plan file must be a YAML mapping.")
    name = str(_req(data, "plan", "plan file")).strip()[:56]
    if s.scalar(select(PlanEvent).where(PlanEvent.entity_type == "plan", PlanEvent.entity_code == name)):
        raise ValidationError(f"Plan '{name}' was already imported. Edit it on the Plan page instead.")
    reason = f"Imported plan '{name}'"
    warnings: list[str] = []
    summary = {"vision": 0, "goals_created": 0, "goals_updated": 0, "objectives": 0, "key_results": 0, "tasks": 0}

    # Tier 1
    vision_code = None
    if v := data.get("vision"):
        vision, w = C.create_vision(s, pillar=_req(v, "pillar", "vision"), title=_req(v, "title", "vision"),
                                    description=v.get("description", ""), target_year=v.get("target_year"),
                                    today=today)
        vision_code, warnings = vision.code, warnings + w
        summary["vision"] = 1

    # Tier 2
    goal_codes: dict[str, str] = {}
    for i, g in enumerate(data.get("goals", []), 1):
        where = f"goal #{i}"
        key = _req(g, "key", where)
        deadline = _date(_req(g, "target_date", where), where)
        existing = s.scalar(select(Goal).where(Goal.code == g["code"])) if g.get("code") else None
        if existing:
            old_title = existing.title
            existing.title = _req(g, "title", where)
            existing.description = g.get("description", existing.description)
            existing.progress_evidence = g.get("evidence", existing.progress_evidence)
            C._plan_log(s, "goal", existing.code, "revised", frm=old_title, to=existing.title, reason=reason)
            if vision_code:
                C.link_goal_to_vision(s, existing.code, vision_code, reason)
            if existing.target_date != deadline:
                C.change_deadline(s, "goal", existing.code, deadline, reason)
            goal = existing
            summary["goals_updated"] += 1
        else:
            goal, w = C.create_goal(s, pillar=_req(g, "pillar", where), title=_req(g, "title", where),
                                    description=g.get("description", ""), target_date=deadline,
                                    vision_code=vision_code, progress_evidence=g.get("evidence", ""),
                                    secondary=g.get("secondary"), today=today)
            warnings += [f"{goal.code}: {x}" for x in w]
            summary["goals_created"] += 1
        goal.baseline = g.get("baseline", goal.baseline)
        goal.target_value = g.get("target_value", goal.target_value)
        goal_codes[key] = goal.code

    # Tiers 3 and 4
    kr_codes: dict[str, str] = {}
    for i, o in enumerate(data.get("objectives", []), 1):
        where = f"objective #{i}"
        gkey = _req(o, "goal", where)
        if gkey not in goal_codes:
            raise ValidationError(f"{where}: goal '{gkey}' isn't defined in this plan.")
        desc = o.get("description", "")
        if o.get("done_when"):
            desc = (desc + "\n\n" if desc else "") + f"Done when: {o['done_when']}"
        obj = C.create_objective(s, goal_code=goal_codes[gkey], title=_req(o, "title", where), description=desc,
                                 start_date=_date(_req(o, "start", where), where),
                                 end_date=_date(o["end"], where) if o.get("end") else None)
        summary["objectives"] += 1
        for j, k in enumerate(o.get("key_results", []), 1):
            kw = f"{where} key result #{j}"
            kr = C.create_key_result(s, objective_code=obj.code, title=_req(k, "title", kw),
                                     cadence=k.get("cadence", "weekly"), target=k.get("target"),
                                     measure=k.get("measure", "manual"), unit=str(k.get("unit", "")))
            if k.get("key"):
                kr_codes[k["key"]] = kr.code
            summary["key_results"] += 1

    # Tasks (checklist items with dates)
    for i, t in enumerate(data.get("tasks", []), 1):
        where = f"task #{i}"
        krk = t.get("key_result")
        if krk and krk not in kr_codes:
            raise ValidationError(f"{where}: key result '{krk}' isn't defined in this plan.")
        create_task(s, title=_req(t, "title", where), pillar=t.get("pillar", "BUS"), priority=t.get("priority", 2),
                    description=t.get("notes", ""),
                    scheduled_date=_date(t["scheduled"], where) if t.get("scheduled") else None,
                    due_date=_date(t["due"], where) if t.get("due") else None,
                    estimated_minutes=t.get("estimated_minutes"),
                    key_result_code=kr_codes.get(krk) if krk else None)
        summary["tasks"] += 1

    C._plan_log(s, "plan", name, "imported", to=str(summary), reason=reason)
    s.flush()
    return {"summary": summary, "warnings": warnings}
