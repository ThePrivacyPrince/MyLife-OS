"""PLN-* and REV-* : daily planning and end-of-day review."""
import datetime as dt

from lifeos.enums import TaskStatus as S
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services.planner import detect_patterns, plan_day, review_day, save_review


def test_pln01_sections_and_ordering(s, mk, today):
    late = mk("Late bill", priority=1, due_date=today - dt.timedelta(days=2))
    now = mk("Today bill", priority=1, due_date=today)
    blocked = mk("Blocked", priority=2)
    T.transition(s, blocked.id, S.BLOCKED, reason="need form", follow_up_date=today, today=today)
    waiting = mk("Waiting", priority=2)
    T.transition(s, waiting.id, S.WAITING, reason="HR reply", today=today)
    future = mk("Future", priority=2, due_date=today + dt.timedelta(days=3))
    plan = plan_day(s, today)
    assert [p.task.id for p in plan.queue[1]] == [late.id, now.id], "overdue first"
    assert plan.queue[1][0].overdue
    assert plan.overdue == [late], "overdue tasks are never silently dropped"
    assert plan.blocked == [blocked] and plan.waiting == [waiting]
    assert plan.upcoming == [future]
    assert plan.follow_ups_due == [blocked]
    assert any("Follow-up due" in w for w in plan.warnings)


def test_pln02_overcommitment_and_assumptions(s, mk, today):
    mk("Big", priority=1, estimated_minutes=200)
    mk("No estimate", priority=2)
    plan = plan_day(s, today)
    assert plan.overcommitted and plan.committed_minutes == 230
    assert any("Overcommitted" in w for w in plan.warnings)
    assert any("no estimate" in w for w in plan.warnings)


def test_pln03_dependency_locked_visible(s, mk, today):
    pre = mk("Get W-2")
    dep = mk("File", priority=1, depends_on=[pre.id])
    plan = plan_day(s, today)
    assert [p.task for p in plan.dependency_locked] == [dep]
    assert dep not in [p.task for p in plan.actionable]


def test_pln04_recurring_and_one_time_views_share_one_queue(s, mk, today):
    mk("One-off", priority=2)
    R.create_template(s, title="Reset", pillar="ENV", priority=2, frequency="daily", start_date=today)
    plan = plan_day(s, today)
    assert {p.task.title for p in plan.queue[2]} == {"One-off", "Reset"}
    assert [p.task.title for p in plan.recurring_today] == ["Reset"]
    assert [p.task.title for p in plan.one_time_today] == ["One-off"]


def test_pln05_plan_is_idempotent(s, mk, today):
    R.create_template(s, title="Reset", pillar="ENV", priority=2, frequency="daily", start_date=today)
    a = plan_day(s, today)
    b = plan_day(s, today)
    assert [p.task.id for p in a.actionable] == [p.task.id for p in b.actionable]


def test_rev01_review_summary_and_upsert(s, mk):
    today = dt.date.today()  # completion timestamps use the real clock
    from lifeos.config import get_settings
    today = get_settings().today()
    done = mk("Done", priority=1, due_date=today)
    mk("Not done", priority=1, due_date=today)
    T.transition(s, done.id, S.COMPLETED, today=today)
    r = review_day(s, today)
    assert [t.title for t in r.completed] == ["Done"]
    assert "Not done" in [t.title for t in r.incomplete]
    assert (r.p1_completed, r.p1_total) == (1, 2) and r.p1_rate == 0.5
    save_review(s, today, "long shift")
    row = save_review(s, today, "long shift, slept 5h")
    assert row.note == "long shift, slept 5h" and row.summary["p1_total"] == 2


def test_rev02_patterns_are_constructive(s, mk, today):
    t = mk("Organize tax docs")
    for i in range(3):
        T.reschedule(s, t.id, today + dt.timedelta(days=i + 1), "too tired", today=today)
    tpl = R.create_template(s, title="Gym", pillar="ENV", priority=3, frequency="daily",
                            start_date=today - dt.timedelta(days=5))
    plan_day(s, today)  # generates the past occurrences and marks them missed
    flags = detect_patterns(s, today)
    kinds = {f.kind for f in flags}
    assert {"reschedule_repeat", "recurring_adherence"} <= kinds
    text = " ".join(f.message for f in flags).lower()
    assert "lazy" not in text and "discipline" not in text and "?" in text
