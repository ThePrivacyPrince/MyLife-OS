"""INT-* : seed, multi-day simulation, CLI, dashboard smoke test."""
import datetime as dt

from sqlalchemy import select

from lifeos.cli import main
from lifeos.enums import TaskStatus as S
from lifeos.models import Goal, RecurringTemplate, Task
from lifeos.services import tasks as T
from lifeos.services.planner import plan_day
from lifeos.services.seed import seed


def test_int01_seed_is_idempotent_and_contains_no_money(s, today):
    first = seed(s, today)
    assert first == {"goals": 9, "tasks": 11, "recurring": 7}
    assert seed(s, today) == {"goals": 0, "tasks": 0, "recurring": 0}
    from lifeos.services.seed import DEFAULT_SEED
    assert "$" not in DEFAULT_SEED.read_text(), "financial figures must not live in Git"


def test_int02_multi_day_simulation(s, today):
    seed(s, today)
    day1 = plan_day(s, today)
    assert [p.task.title for p in day1.queue[1]] == ["Finish RXO onboarding requirements"]
    assert all(not p.gate.allowed for p in day1.queue[2]), "P2 gated while onboarding is open"
    assert [p.task.title for p in day1.protected] == ["Sleep (protected)"]

    onboarding = day1.queue[1][0].task
    T.transition(s, onboarding.id, S.COMPLETED, today=today)
    reset = next(p.task for p in plan_day(s, today).queue[2] if p.task.title == "Bedroom reset")
    T.transition(s, reset.id, S.COMPLETED, today=today)

    day2 = plan_day(s, today + dt.timedelta(days=1))
    missed_titles = {t.title for t in day2.auto_missed}
    assert "Review tomorrow's schedule" in missed_titles and "Bedroom reset" not in missed_titles
    # Day 1's completed reset is untouched; day 2 has a fresh, independent occurrence.
    resets = list(s.scalars(select(Task).where(Task.title == "Bedroom reset").order_by(Task.occurrence_date)))
    assert [r.status for r in resets] == ["completed", "pending"]
    # Two P1s due on day 2 now gate the P2 work.
    assert {p.task.title for p in day2.queue[1]} >= {"Check phone bill due date and amount"}


def test_int03_goal_links(s, today):
    seed(s, today)
    goal = s.scalar(select(Goal).where(Goal.code == "FIN-G003"))
    assert {t.title for t in goal.tasks} == {
        "Create/log in to IRS online account and pull account transcripts",
        "List exactly which tax years are unfiled",
    }
    assert s.scalar(select(RecurringTemplate).where(RecurringTemplate.protected.is_(True))).title.startswith("Sleep")


def test_int04_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LIFEOS_DB", str(tmp_path / "cli.db"))
    assert main(["seed"]) == 0
    assert main(["today", "--date", "2026-11-18"]) == 0
    assert main(["review", "--date", "2026-11-18"]) == 0
    out = capsys.readouterr().out
    assert "Finish RXO onboarding requirements" in out and "Review 2026-11-18" in out
