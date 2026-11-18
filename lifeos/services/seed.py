"""Load starter goals, tasks and routines from YAML. Idempotent: skips if goals already exist."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import REPO_ROOT
from lifeos.models import Goal
from lifeos.services.recurring import create_template
from lifeos.services.tasks import create_task

DEFAULT_SEED = REPO_ROOT / "seed" / "starter.yaml"


def seed(s: Session, today: dt.date, path: Path = DEFAULT_SEED) -> dict[str, int]:
    if s.scalar(select(Goal).limit(1)) is not None:
        return {"goals": 0, "tasks": 0, "recurring": 0}
    data = yaml.safe_load(Path(path).read_text())

    for g in data.get("goals", []):
        s.add(Goal(code=g["code"], title=g["title"], description=g.get("description", ""), pillar_code=g["pillar"],
                   secondary_pillars=g.get("secondary", []), progress_evidence=g.get("progress_evidence", ""),
                   target_date=g.get("target_date")))
    s.flush()

    keys: dict[str, int] = {}
    for t in data.get("tasks", []):
        due = today + dt.timedelta(days=t["due_in_days"]) if "due_in_days" in t else None
        task = create_task(
            s, title=t["title"], pillar=t["pillar"], priority=t["priority"], due_date=due,
            estimated_minutes=t.get("estimated_minutes"), goal_code=t.get("goal"),
            evidence_required=t.get("evidence_required", False),
            depends_on=[keys[k] for k in t.get("depends_on", [])],
        )
        keys[t.get("key", t["title"])] = task.id

    for r in data.get("recurring", []):
        create_template(
            s, title=r["title"], pillar=r["pillar"], priority=r["priority"], frequency=r["frequency"],
            weekdays=r.get("weekdays"), start_date=today, estimated_minutes=r.get("estimated_minutes"),
            missed_policy=r.get("missed_policy", "mark_missed"), protected=r.get("protected", False),
            goal_code=r.get("goal"),
        )
    return {"goals": len(data.get("goals", [])), "tasks": len(keys), "recurring": len(data.get("recurring", []))}
