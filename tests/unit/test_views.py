"""VIW-* : tasks grouped by pillar, then by when they need to happen."""
import datetime as dt

from lifeos.enums import TaskStatus as S
from lifeos.services import tasks as T
from lifeos.services.views import bucket_of, group_by_pillar

PILLARS = ["BUS", "REL", "ENV", "FIN", "EDU"]


def test_viw01_buckets(s, mk, today):
    wk_end = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=6)
    late = mk("Late", due_date=today - dt.timedelta(days=1))
    now = mk("Now", scheduled_date=today)
    week = mk("This week", due_date=wk_end)
    later = mk("Later", due_date=wk_end + dt.timedelta(days=1))
    nodate = mk("Someday")
    wait = mk("Waiting")
    T.transition(s, wait.id, S.WAITING, reason="HR reply", today=today)
    done = mk("Done")
    T.transition(s, done.id, S.COMPLETED, today=today)
    assert [bucket_of(x, today) for x in (late, now, week, later, nodate, wait, done)] == \
        ["overdue", "this_week", "this_week", "later", "no_date", "waiting", None]


def test_viw02_grouped_by_pillar_sorted_and_counted(s, mk, today):
    mk("Call payroll", pillar="FIN", priority=2, due_date=today)
    mk("Pay phone bill", pillar="FIN", priority=1, due_date=today)
    mk("Sleep", pillar="ENV", priority=1, protected=True)
    mk("Text Amari's coach", pillar="REL", priority=2)
    groups = {g.code: g for g in group_by_pillar(s.query(T.Task).all(), PILLARS, today)}
    assert [g for g in PILLARS] == list(groups)  # every pillar shown, in pillar order
    fin = groups["FIN"]
    assert [x.title for x in fin.buckets["this_week"]] == ["Pay phone bill", "Call payroll"], "P1 first"
    assert (fin.open_count, fin.p1_count, fin.urgent) == (2, 1, True)
    assert groups["ENV"].open_count == 0, "protected commitments aren't to-dos"
    assert groups["REL"].buckets["no_date"][0].title == "Text Amari's coach" and not groups["REL"].urgent


def test_viw03_timeline_by_pillar_and_undated_goals(s, today):
    from lifeos.services import cascade as C
    from lifeos.services.seed import seed
    from lifeos.services.views import quarter_label, quarter_start, timeline_by_pillar
    seed(s, today)
    C.create_vision(s, pillar="FIN", title="Debt-free", target_year=2030, today=today)
    C.change_deadline(s, "goal", "FIN-G003", today + dt.timedelta(days=20))
    groups = {g.code: g for g in timeline_by_pillar(s, PILLARS, today)}
    fin = groups["FIN"]
    assert [d.code for d in fin.deadlines] == ["FIN-G003", "FIN-V1"], "soonest first"
    assert fin.next_up(today).code == "FIN-G003" and fin.due_within(today, 30)
    assert "FIN-G003" not in [g.code for g in fin.undated_goals] and len(fin.undated_goals) == 4
    assert groups["BUS"].deadlines[0].code == "BUS-G001"
    assert quarter_label(dt.date(2027, 5, 31)) == "Q2 2027" and quarter_start(dt.date(2027, 5, 31)) == dt.date(2027, 4, 1)
