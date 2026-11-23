"""PLR-* : planner - events, class series, agenda, planning tasks onto days, .ics import, weekly review."""
import datetime as dt

import pytest
from sqlalchemy import select

from lifeos.enums import TaskStatus as S
from lifeos.errors import ValidationError
from lifeos.models import CalendarEvent, TaskEvent
from lifeos.services import planner_cal as P
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services import weekly as W
from lifeos.services.ics_import import guess_kind, import_ics, parse_ics
from lifeos.services.planner import detect_patterns

MON = dt.date(2026, 11, 23)
t = dt.time


def test_plr01_week_and_month_helpers():
    assert P.week_start(dt.date(2026, 11, 29)) == MON
    grid = P.month_weeks(2026, 11)
    assert grid[0][0].weekday() == 0 and all(len(w) == 7 for w in grid)
    assert any(dt.date(2026, 11, 30) in w for w in grid)
    assert P.shift_month(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert P.shift_month(dt.date(2026, 12, 15), 1) == dt.date(2027, 1, 15)


@pytest.mark.parametrize("kw", [dict(title=" "), dict(kind="party"), dict(start_time=t(10), end_time=t(10)),
                                dict(start_time=t(9)), dict(pillar="NOPE")])
def test_plr02_event_validation(s, kw):
    base = dict(title="Contracts exam", day=MON, kind="exam")
    base.update(kw)
    with pytest.raises(ValidationError):
        P.add_event(s, **base)


def test_plr03_class_series_expands_on_weekdays_with_skips(s):
    ser = P.add_series(s, title="Contracts", weekdays=["MO", "WE"], start_date=MON, end_date=MON + dt.timedelta(days=13),
                       start_time=t(9), end_time=t(10, 30), location="WCC 1015")
    P.skip_series_day(s, ser.id, MON + dt.timedelta(days=2))  # no class Wed (Thanksgiving week)
    blocks = P.blocks_between(s, MON, MON + dt.timedelta(days=13))
    assert [b.day.strftime("%a %d") for b in blocks] == ["Mon 23", "Mon 30", "Wed 02"]
    assert blocks[0].minutes == 90 and blocks[0].time_label == "9 am-10:30 am"
    with pytest.raises(ValidationError):
        P.add_series(s, title="x", weekdays=[], start_date=MON, end_date=MON, start_time=t(9), end_time=t(10))


def test_plr04_agenda_gathers_everything_for_a_day(s, mk):
    P.add_event(s, title="Study group", day=MON, start_time=t(18), end_time=t(20), kind="study")
    due = mk("Read Hadley v Baxendale", pillar="EDU", due_date=MON + dt.timedelta(days=1))
    late = mk("Overdue memo", due_date=MON - dt.timedelta(days=2))
    R.create_template(s, title="Brief cases", pillar="EDU", priority=2, frequency="daily", start_date=MON)
    days = P.agenda(s, MON, 3, today=MON)
    assert [b.title for b in days[0].blocks] == ["Study group"] and days[0].booked_minutes == 120
    assert late in days[0].tasks, "overdue work surfaces on today"
    assert due in days[1].tasks
    assert days[1].routines == ["Brief cases"] and days[0].routines == []  # today's routine is a real task row
    assert not days[2].empty and days[2].tasks == []


def test_plr05_planning_is_not_a_slip_but_moving_todays_work_later_is(s, mk):
    a = mk("Outline torts")
    P.plan_task_on(s, a.id, MON + dt.timedelta(days=2), today=MON)
    assert a.scheduled_date == MON + dt.timedelta(days=2)
    assert [e.event_type for e in a.events][-1] == "planned"
    b = mk("Draft memo", scheduled_date=MON)
    with pytest.raises(Exception, match="reason"):
        P.plan_task_on(s, b.id, MON + dt.timedelta(days=1), today=MON)
    P.plan_task_on(s, b.id, MON + dt.timedelta(days=1), today=MON, reason="moot court ran late")
    assert [e.event_type for e in b.events][-1] == "rescheduled"
    for d in (3, 4, 5):
        P.plan_task_on(s, a.id, MON + dt.timedelta(days=d), today=MON)
    assert not any(f.kind == "reschedule_repeat" for f in detect_patterns(s, MON)), "planning ahead isn't slipping"


def test_plr06_unscheduled_pile(s, mk):
    a = mk("A", priority=1)
    mk("B", scheduled_date=MON)
    assert [x.title for x in P.unscheduled_tasks(s)] == ["A"]
    P.plan_task_on(s, a.id, MON, today=MON)
    assert P.unscheduled_tasks(s) == []


ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:assign-1@canvas\r
SUMMARY:Legal Research & Writing Memo 1 due\r
DTSTART:20261202T045900Z\r
DTEND:20261202T045900Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:civpro@google\r
SUMMARY:Civil Procedure\\, Section 3\r
LOCATION:Austin Hall\r
DTSTART;TZID=America/New_York:20261123T130000\r
DTEND;TZID=America/New_York:20261123T143000\r
RRULE:FREQ=WEEKLY;BYDAY=MO,TH;COUNT=4\r
EXDATE;TZID=America/New_York:20261126T130000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:finals\r
SUMMARY:Torts Final Exam\r
DTSTART;VALUE=DATE:20261214\r
DTEND;VALUE=DATE:20261215\r
DESCRIPTION:Bring laptop\\nOpen book\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:gone\r
SUMMARY:Cancelled thing\r
STATUS:CANCELLED\r
DTSTART;VALUE=DATE:20261201\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_plr07_ics_parsing_times_repeats_exdates_and_kinds():
    items = parse_ics(ICS, MON)
    titles = [(i["title"], i["day"].isoformat(), i["start"]) for i in items]
    # 04:59Z on Dec 2 = 11:59 PM Dec 1 in New York; zero-length -> all-day marker with the due time in notes
    memo = next(i for i in items if i["title"].startswith("Legal"))
    assert memo["day"] == dt.date(2026, 12, 1) and memo["start"] is None and "Due 11:59 PM" in memo["notes"]
    assert memo["kind"] == "deadline"
    civ = [i for i in items if i["title"] == "Civil Procedure, Section 3"]
    assert [c["day"].isoformat() for c in civ] == ["2026-11-23", "2026-11-30", "2026-12-03"]  # Thanksgiving skipped
    assert civ[0]["start"] == t(13) and civ[0]["end"] == t(14, 30) and civ[0]["location"] == "Austin Hall"
    final = next(i for i in items if i["title"] == "Torts Final Exam")
    assert final["kind"] == "exam" and final["notes"] == "Bring laptop\nOpen book"
    assert not any(x[0] == "Cancelled thing" for x in titles)


def test_plr08_ics_import_is_idempotent_and_items_become_tasks(s):
    assert import_ics(s, ICS, MON) == {"added": 5, "updated": 0}
    assert import_ics(s, ICS, MON) == {"added": 0, "updated": 5}
    memo = s.scalar(select(CalendarEvent).where(CalendarEvent.title.like("Legal%")))
    task = P.event_to_task(s, memo.id, priority=1)
    assert task.due_date == dt.date(2026, 12, 1) and task.pillar_code == "EDU"
    with pytest.raises(ValidationError):
        P.event_to_task(s, memo.id)


@pytest.mark.parametrize("bad", ["not a calendar", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT\nEND:VCALENDAR"])
def test_plr09_hostile_or_empty_ics(s, bad):
    if "VCALENDAR" not in bad:
        with pytest.raises(ValidationError):
            parse_ics(bad, MON)
    else:
        assert parse_ics(bad, MON) == []  # no DTSTART -> ignored, nothing crashes


def test_plr10_guess_kind():
    assert guess_kind("Midterm - Torts") == "exam"
    assert guess_kind("Brief due") == "deadline"
    assert guess_kind("Constitutional Law lecture") == "class"


# ---------------------------------------------------------------- weekly review
def test_plr11_week_summary_scores_from_records(s, mk):
    from lifeos.config import get_settings
    today = get_settings().today()
    start = P.week_start(today)
    p1 = mk("File memo", priority=1, due_date=start)
    mk("Outline", priority=1, due_date=start)
    mk("Sleep", priority=1, due_date=start, protected=True)
    T.transition(s, p1.id, S.COMPLETED, today=today)
    if today == start:  # on Monday nothing has slipped yet; score as of the end of the week instead
        today = start + dt.timedelta(days=6)
    P.add_series(s, title="Contracts", weekdays=["MO", "TU", "WE", "TH", "FR", "SA", "SU"], start_date=start,
                 end_date=start + dt.timedelta(days=6), start_time=t(9), end_time=t(10), kind="class")
    w = W.week_summary(s, today, today)
    assert w.start == start and (w.p1_done, w.p1_total) == (1, 2) and w.p1_rate == 0.5
    assert [x.title for x in w.slipped] == ["Outline"], "protected items never count as slipped"
    assert w.metrics()["class_hours"] == 7.0


def test_plr12_review_saves_snapshot_and_top3(s, mk):
    from lifeos.config import get_settings
    today = get_settings().today()
    r = W.save_weekly_review(s, today, today, wins="Finished memo", energy=4,
                             focus=["Outline Torts", "", "Moot court prep", "extra", "dropped"])
    assert r.focus == ["Outline Torts", "Moot court prep", "extra"]
    assert "p1_total" in r.summary
    again = W.save_weekly_review(s, today, today, wins="Finished memo + outline")
    assert again.id == r.id
    with pytest.raises(ValidationError):
        W.save_weekly_review(s, today, today, energy=9)
    with pytest.raises(ValidationError):
        W.save_weekly_review(s, today + dt.timedelta(days=14), today)


def test_plr13_review_due_on_sunday_or_the_monday_after(s, mk):
    sun = dt.date(2026, 11, 29)
    assert W.review_due(s, sun) is None, "no nagging before LIFE OS was in use"
    mk("first task").created_at = dt.datetime(2026, 11, 20, 12)
    assert W.review_due(s, sun) == MON
    assert W.review_due(s, sun + dt.timedelta(days=1)) == MON
    assert W.review_due(s, MON + dt.timedelta(days=2)) is None
    W.save_weekly_review(s, MON, sun, focus=["A", "B", "C"])
    assert W.review_due(s, sun) is None
    assert W.current_focus(s, sun + dt.timedelta(days=1)) == ["A", "B", "C"]


def test_plr14_unfinished_today_is_not_slipped_yet(s, mk):
    wed = MON + dt.timedelta(days=2)
    mk("Due today", priority=1, due_date=wed)
    mk("Due Monday", priority=1, due_date=MON)
    w = W.week_summary(s, MON, wed)
    assert [x.title for x in w.slipped] == ["Due Monday"] and (w.p1_done, w.p1_total) == (0, 1)
