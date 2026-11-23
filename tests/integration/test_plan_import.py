"""IMP-* : importing a whole plan file (generic sample data - real plans live in data/, never in Git)."""
import datetime as dt
from decimal import Decimal

import pytest
import yaml
from sqlalchemy import select

from lifeos.errors import ValidationError
from lifeos.models import Goal, KeyResult, Objective, Task, Vision
from lifeos.services import cascade as C
from lifeos.services.plan_import import import_plan
from lifeos.services.seed import seed

SAMPLE = """
plan: Sample business plan
vision: {pillar: BUS, target_year: 2030, title: A business that runs without me}
goals:
  - {key: open, code: BUS-G001, pillar: BUS, title: Open with six units, target_date: 2027-05-31,
     baseline: 2 units, evidence: Six units inspected}
  - {key: sales, pillar: BUS, title: Summer sales target, target_date: 2027-08-31}
objectives:
  - goal: open
    title: Buy units as cash comes in
    start: 2026-12-01
    end: 2027-02-28
    done_when: Each purchase passes inspection
    key_results:
      - {title: Units ready, measure: level, target: 6, unit: units}
      - {key: weekly, title: Cash added, measure: manual, unit: USD}
  - goal: sales
    title: Grow sales
    start: 2027-06-01
    end: 2027-08-31
    key_results:
      - {key: bk, title: Bookings, measure: manual, target: 19, unit: bookings}
tasks:
  - {title: Set up booking links, scheduled: 2027-03-01, due: 2027-03-31, key_result: bk}
  - {title: Decide on the truck, priority: 1, scheduled: 2027-09-25, due: 2027-10-01}
"""


@pytest.fixture
def plan():
    return yaml.safe_load(SAMPLE)


def test_imp01_full_chain_is_created_and_linked(s, today, plan):
    seed(s, today)
    out = import_plan(s, plan, today)
    assert out["summary"] == {"vision": 1, "goals_created": 1, "goals_updated": 1, "objectives": 2,
                              "key_results": 3, "tasks": 2}
    g1 = s.scalar(select(Goal).where(Goal.code == "BUS-G001"))
    assert g1.title == "Open with six units" and g1.vision.title == "A business that runs without me"
    assert g1.baseline == "2 units"
    o1 = s.scalar(select(Objective).where(Objective.code == "BUS-G001-O1"))
    assert "Done when: Each purchase passes inspection" in o1.description
    level = s.scalar(select(KeyResult).where(KeyResult.title == "Units ready"))
    assert (level.measure, level.target) == ("level", Decimal(6))
    assert s.scalar(select(KeyResult).where(KeyResult.title == "Cash added")).target == 0  # track only
    t = s.scalar(select(Task).where(Task.title == "Set up booking links"))
    assert [l.tier for l in C.lineage(t)] == ["key_result", "objective", "goal", "vision"]


def test_imp02_existing_goal_is_updated_not_duplicated(s, today, plan):
    seed(s, today)
    import_plan(s, plan, today)
    assert len(list(s.scalars(select(Goal).where(Goal.pillar_code == "BUS")))) == 2


def test_imp03_future_tasks_do_not_gate_today(s, today, plan):
    seed(s, today)
    import_plan(s, plan, today)
    from lifeos.services.planner import plan_day
    titles = {p.task.title for p in plan_day(s, today).queue[1]}
    assert "Decide on the truck" not in titles


def test_imp04_import_once(s, today, plan):
    import_plan(s, plan, today)
    with pytest.raises(ValidationError, match="already imported"):
        import_plan(s, plan, today)


@pytest.mark.parametrize("breakage", [
    lambda p: p["objectives"][0].update(goal="nope"),
    lambda p: p["tasks"][0].update(key_result="nope"),
    lambda p: p["goals"][1].pop("target_date"),
    lambda p: p["objectives"][0].update(end="2027-12-31"),          # longer than ~3 months
    lambda p: p["objectives"][0]["key_results"][0].update(target="lots"),
    lambda p: p.pop("plan"),
])
def test_imp05_any_error_means_nothing_is_written(engine, today, plan, breakage):
    from sqlalchemy.orm import Session
    breakage(plan)
    with Session(engine) as s:
        with pytest.raises(ValidationError):
            import_plan(s, plan, today)
        s.rollback()
        assert s.scalar(select(Vision)) is None and s.scalar(select(Objective)) is None


def test_imp06_cli_reports_errors_cleanly(tmp_path, monkeypatch, capsys):
    from lifeos.cli import main
    monkeypatch.setenv("LIFEOS_DB", str(tmp_path / "cli.db"))
    f = tmp_path / "p.yaml"
    f.write_text(SAMPLE)
    assert main(["import-plan", str(f)]) == 0
    assert main(["import-plan", str(f)]) == 1
    assert "already imported" in capsys.readouterr().err
