"""LIFE OS - Daily Command Center (Streamlit).

Run:  lifeos dashboard   (or: streamlit run lifeos/dashboard/app.py)

All writes happen in button callbacks through the service layer, so every rule and audit entry
applies exactly as it does in tests. The UI holds no business logic.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st
from sqlalchemy import select

from lifeos.config import get_settings
from lifeos.db import init_db, make_engine, session_factory, unit_of_work
from lifeos.engines.state_machine import OPEN_STATES, TRANSITIONS
from lifeos.enums import MissedPolicy, Priority, TaskStatus as S
from lifeos.errors import GateViolation, LifeOSError
from lifeos.models import Goal, Pillar, RecurringTemplate, Task
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services.planner import detect_patterns, plan_day, recurring_adherence, review_day, save_review

st.set_page_config(page_title="LIFE OS", page_icon="◎", layout="wide")
SS = st.session_state
ICON = {"BUS": "💼", "REL": "🤝", "ENV": "🏠", "FIN": "💵", "EDU": "🎓"}
PRIORITY_COLOR = {1: "red", 2: "orange", 3: "gray"}


@st.cache_resource
def _factory():
    engine = make_engine()
    init_db(engine)
    return session_factory(engine)


def flash(kind: str, msg: str) -> None:
    SS["flash"] = (kind, msg)


def act(fn, ok: str) -> None:
    """Run a write through the service layer; surface rule violations instead of hiding them."""
    try:
        with unit_of_work(_factory()) as s:
            fn(s)
        flash("success", ok)
    except GateViolation as e:
        flash("warning", f"🔒 {e.decision.reason}  \nOpen **More → Override** to proceed with a recorded reason.")
    except LifeOSError as e:
        flash("error", str(e))
    except ValueError as e:
        flash("error", f"Invalid input: {e}")


def val(key: str, default=None):
    return SS.get(key, default)


# ------------------------------------------------------------------ sidebar
st.sidebar.title("◎ LIFE OS")
page = st.sidebar.radio("View", ["Today", "Add", "Recurring", "All tasks", "Goals", "Review"],
                        label_visibility="collapsed")
day: dt.date = st.sidebar.date_input("Planning date", value=get_settings().today())
pillar_filter = st.sidebar.multiselect("Pillars", list(ICON), format_func=lambda c: f"{ICON[c]} {c}")
kind_filter = st.sidebar.radio("Task type", ["All", "One-time", "Recurring"], horizontal=True)
st.sidebar.caption("Automate the repetitive. Systematize the important. Measure the meaningful.")


def visible(t: Task) -> bool:
    if pillar_filter and t.pillar_code not in pillar_filter:
        return False
    if kind_filter == "One-time" and t.is_recurring:
        return False
    return not (kind_filter == "Recurring" and not t.is_recurring)


if "flash" in SS:
    kind, msg = SS.pop("flash")
    getattr(st, kind)(msg)

s = _factory()()


# ------------------------------------------------------------------ task card
def meta_line(t: Task, overdue: bool = False) -> str:
    bits = [f"{ICON.get(t.pillar_code, '')} {t.pillar_code}", "↻ recurring" if t.is_recurring else "one-time"]
    if overdue:
        bits.append(f":red[**overdue** since {t.due_date:%b %d}]")
    elif t.due_date:
        bits.append(f"due {t.due_date:%a %b %d}")
    if t.estimated_minutes:
        bits.append(f"~{t.estimated_minutes} min")
    if t.status != S.PENDING:
        bits.append(f"**{t.status.replace('_', ' ')}**")
    if t.evidence_required:
        bits.append("📎 evidence required")
    if t.goal:
        bits.append(t.goal.code)
    return " · ".join(bits)


def task_card(t: Task, gate=None, overdue: bool = False, prefix: str = "") -> None:
    k = f"{prefix}{t.id}"
    gated = gate is not None and not gate.allowed
    with st.container(border=True):
        left, b1, b2 = st.columns([7, 1.3, 1.3])
        left.markdown(f"{'🔒 ' if gated else ''}**{t.title}**")
        left.caption(meta_line(t, overdue))
        if gated:
            left.caption(gate.reason)
        open_workable = t.status in (S.PENDING, S.IN_PROGRESS)
        if t.status == S.PENDING:
            b1.button("Start", key=f"start{k}", disabled=gated, use_container_width=True,
                      on_click=act, args=(lambda s, i=t.id: T.transition(s, i, S.IN_PROGRESS, today=day), "Started"))
        if open_workable or t.status in (S.WAITING, S.MISSED):
            b2.button("Done ✓", key=f"done{k}", type="primary", disabled=gated, use_container_width=True,
                      on_click=act, args=(lambda s, i=t.id: T.transition(s, i, S.COMPLETED, today=day), "Completed ✓"))
        if t.status in (S.BLOCKED, S.WAITING, S.DEFERRED):
            b1.button("Unblock", key=f"unb{k}", use_container_width=True,
                      on_click=act, args=(lambda s, i=t.id: T.transition(s, i, S.PENDING, today=day), "Back to pending"))
        with st.expander("More"):
            task_actions(t, k, gated)


def task_actions(t: Task, k: str, gated: bool) -> None:
    allowed = TRANSITIONS[S(t.status)]
    tabs = st.tabs(["Block / wait", "Defer / cancel", "Priority", "Reschedule", "Evidence", "History"]
                   + (["Override"] if gated else []))
    with tabs[0]:
        st.text_input("Reason", key=f"br{k}", placeholder="What is it waiting on?")
        st.date_input("Follow up on", key=f"bf{k}", value=day + dt.timedelta(days=2))
        c1, c2 = st.columns(2)
        c1.button("Block", key=f"blk{k}", disabled=S.BLOCKED not in allowed, on_click=act, args=(
            lambda s, i=t.id: T.transition(s, i, S.BLOCKED, reason=val(f"br{k}"), follow_up_date=val(f"bf{k}"),
                                           today=day), "Blocked - follow-up scheduled"))
        c2.button("Waiting on someone", key=f"wt{k}", disabled=S.WAITING not in allowed, on_click=act, args=(
            lambda s, i=t.id: T.transition(s, i, S.WAITING, reason=val(f"br{k}"), today=day), "Marked waiting"))
    with tabs[1]:
        st.text_input("Reason (required)", key=f"dr{k}")
        st.date_input("Bring back on", key=f"du{k}", value=day + dt.timedelta(days=7))
        c1, c2 = st.columns(2)
        c1.button("Defer", key=f"def{k}", disabled=S.DEFERRED not in allowed, on_click=act, args=(
            lambda s, i=t.id: T.transition(s, i, S.DEFERRED, reason=val(f"dr{k}"), defer_until=val(f"du{k}"),
                                           today=day), "Deferred"))
        c2.button("Cancel task", key=f"can{k}", disabled=S.CANCELLED not in allowed, on_click=act, args=(
            lambda s, i=t.id: T.transition(s, i, S.CANCELLED, reason=val(f"dr{k}"), today=day), "Cancelled"))
        if t.status in (S.COMPLETED, S.CANCELLED, S.MISSED):
            st.button("Reopen", key=f"reo{k}", on_click=act, args=(
                lambda s, i=t.id: T.transition(s, i, S.PENDING, reason=val(f"dr{k}"), today=day), "Reopened"))
    with tabs[2]:
        st.radio("New priority", [1, 2, 3], index=t.priority - 1, key=f"np{k}", horizontal=True,
                 format_func=lambda p: Priority(p).label)
        st.text_input("Why the change?", key=f"pr{k}")
        st.button("Change priority", key=f"chp{k}", on_click=act, args=(
            lambda s, i=t.id: T.change_priority(s, i, val(f"np{k}"), val(f"pr{k}")), "Priority changed"))
    with tabs[3]:
        st.date_input("Work on it on", key=f"rd{k}", value=day + dt.timedelta(days=1))
        st.text_input("Reason", key=f"rr{k}")
        st.button("Reschedule", key=f"rs{k}", on_click=act, args=(
            lambda s, i=t.id: T.reschedule(s, i, val(f"rd{k}"), val(f"rr{k}"), today=day), "Rescheduled"))
        st.caption("Due dates never move silently - rescheduling past the due date is flagged.")
    with tabs[4]:
        for ev in t.evidence:
            st.markdown(f"- {ev.description}" + (f" ({ev.uri})" if ev.uri else ""))
        st.text_input("Evidence (what proves it?)", key=f"ed{k}")
        st.text_input("Link / file path (optional)", key=f"eu{k}")
        st.button("Attach evidence", key=f"ea{k}", on_click=act, args=(
            lambda s, i=t.id: T.add_evidence(s, i, val(f"ed{k}"), val(f"eu{k}")), "Evidence attached"))
    with tabs[5]:
        st.dataframe([{"when (UTC)": e.at.strftime("%Y-%m-%d %H:%M"), "event": e.event_type, "from": e.from_value,
                       "to": e.to_value, "reason": e.reason, "by": e.actor} for e in t.events],
                     hide_index=True, use_container_width=True)
    if gated:
        with tabs[6]:
            st.caption("Doing this ahead of higher-priority work is allowed, but it is recorded, never silent.")
            st.text_input("Override reason", key=f"or{k}")
            c1, c2 = st.columns(2)
            if t.status == S.PENDING:
                c1.button("Start anyway", key=f"os{k}", on_click=act, args=(
                    lambda s, i=t.id: T.transition(s, i, S.IN_PROGRESS, override_reason=val(f"or{k}"), today=day),
                    "Started (override recorded)"))
            c2.button("Done anyway", key=f"od{k}", on_click=act, args=(
                lambda s, i=t.id: T.transition(s, i, S.COMPLETED, override_reason=val(f"or{k}"), today=day),
                "Completed (override recorded)"))


# ------------------------------------------------------------------ pages
def page_today() -> None:
    plan = plan_day(s, day)
    s.commit()
    review = review_day(s, day)
    st.title(f"{day:%A, %B %d}")
    c = st.columns(4)
    c[0].metric("P1 open", len(plan.queue[1]))
    c[1].metric("Done today", len(review.completed))
    c[2].metric("Overdue", len(plan.overdue))
    c[3].metric("Capacity (P1+P2)", f"{plan.committed_minutes}/{plan.capacity_minutes} min",
                delta="over" if plan.overcommitted else None, delta_color="inverse")
    for w in plan.warnings:
        st.warning(w, icon="⚠️")
    for f in plan.flags:
        st.info(f.message, icon="🔍")
    if plan.protected:
        st.caption("🛡️ Protected today: " + " · ".join(p.task.title for p in plan.protected))

    for prio in (1, 2, 3):
        items = [p for p in plan.queue[prio] if visible(p.task)]
        st.subheader(f":{PRIORITY_COLOR[prio]}[{Priority(prio).label}]  ·  {len(items)}")
        if not items:
            st.caption("Clear." if prio == 1 else "Nothing here.")
        for p in items:
            task_card(p.task, p.gate, p.overdue, prefix=f"q{prio}-")

    left, right = st.columns(2)
    with left:
        st.subheader("Blocked & waiting")
        stuck = [t for t in plan.blocked + plan.waiting if visible(t)]
        if not stuck:
            st.caption("Nothing stuck.")
        for t in stuck:
            fu = f" · follow up {t.follow_up_date:%b %d}" if t.follow_up_date else ""
            st.markdown(f"- **{t.title}** - {t.status}: _{t.status_reason}_{fu}")
        for t in plan.blocked + plan.waiting:
            if visible(t):
                with st.expander(f"Manage: {t.title}"):
                    task_card(t, prefix="bw-")
        if plan.dependency_locked:
            st.subheader("Waiting on prerequisites")
            for p in plan.dependency_locked:
                st.markdown(f"- **{p.task.title}** ← " + ", ".join(f"_{u.title}_" for u in p.unmet))
    with right:
        st.subheader("Coming up")
        if not plan.upcoming:
            st.caption("Nothing in the next week.")
        for t in plan.upcoming:
            if visible(t):
                st.markdown(f"- {t.effective_date:%a %b %d} · :{PRIORITY_COLOR[t.priority]}[P{t.priority}] "
                            f"{ICON.get(t.pillar_code, '')} {t.title}")
        st.caption("Want to get ahead? Open a task in **All tasks** and start it - the priority gate still applies.")


def _pillar_options() -> list[str]:
    return [p.code for p in s.scalars(select(Pillar).order_by(Pillar.sort_order))]


def _goal_options() -> list[str | None]:
    return [None] + [g.code for g in s.scalars(select(Goal).order_by(Goal.code))]


def page_add() -> None:
    st.title("Add")
    one, rec = st.tabs(["One-time task", "Recurring task"])
    with one, st.form("add_task", clear_on_submit=True):
        st.text_input("What needs to happen?", key="nt_title")
        c = st.columns(3)
        c[0].selectbox("Pillar", _pillar_options(), key="nt_pillar", format_func=lambda x: f"{ICON[x]} {x}")
        c[1].radio("Priority", [1, 2, 3], index=1, key="nt_prio", horizontal=True, format_func=lambda p: f"P{p}")
        c[2].number_input("Estimate (min)", min_value=0, value=30, step=5, key="nt_est")
        c = st.columns(3)
        c[0].date_input("Due date", value=None, key="nt_due")
        c[1].date_input("Don't start before", value=None, key="nt_sched")
        c[2].selectbox("Goal", _goal_options(), key="nt_goal")
        open_tasks = list(s.scalars(select(Task).where(Task.status.in_([str(x) for x in OPEN_STATES]))))
        st.multiselect("Can't start until these are done", [t.id for t in open_tasks], key="nt_deps",
                       format_func=lambda i: next(f"#{t.id} {t.title}" for t in open_tasks if t.id == i))
        c = st.columns(2)
        c[0].checkbox("Requires evidence to complete", key="nt_ev")
        c[1].checkbox("Protected (never gated: sleep, shifts, caregiving)", key="nt_prot")
        st.text_area("Notes", key="nt_desc")
        st.form_submit_button("Add task", type="primary", on_click=act, args=(lambda s: T.create_task(
            s, title=val("nt_title", ""), pillar=val("nt_pillar"), priority=val("nt_prio"),
            description=val("nt_desc", ""), due_date=val("nt_due"), scheduled_date=val("nt_sched"),
            estimated_minutes=val("nt_est") or None, goal_code=val("nt_goal"), evidence_required=val("nt_ev"),
            protected=val("nt_prot"), depends_on=val("nt_deps", [])), "Task added"))
    with rec, st.form("add_tpl", clear_on_submit=True):
        st.text_input("Routine", key="rt_title")
        c = st.columns(3)
        c[0].selectbox("Pillar", _pillar_options(), key="rt_pillar", format_func=lambda x: f"{ICON[x]} {x}")
        c[1].radio("Priority", [1, 2, 3], index=1, key="rt_prio", horizontal=True, format_func=lambda p: f"P{p}")
        c[2].number_input("Estimate (min)", min_value=0, value=15, step=5, key="rt_est")
        c = st.columns(3)
        c[0].selectbox("Repeats", ["daily", "weekdays", "weekly", "biweekly", "monthly", "custom"], key="rt_freq")
        c[1].multiselect("On days", ["MO", "TU", "WE", "TH", "FR", "SA", "SU"], key="rt_days",
                         help="For 'weekdays', 'weekly', 'biweekly'.")
        c[2].number_input("Day of month", min_value=0, max_value=31, value=0, key="rt_mday",
                          help="For 'monthly'. 0 = same day as start.")
        st.text_input("Custom RRULE (advanced)", key="rt_custom", placeholder="FREQ=WEEKLY;BYDAY=TU,TH")
        c = st.columns(3)
        c[0].date_input("Starts", value=day, key="rt_start")
        c[1].date_input("Ends (optional)", value=None, key="rt_end")
        c[2].selectbox("If a day is missed", [str(m) for m in MissedPolicy], key="rt_policy",
                       format_func=lambda m: {"mark_missed": "Record as missed, move on",
                                              "carry_over": "Carry over until done"}[m])
        st.checkbox("Protected (never gated)", key="rt_prot")
        st.form_submit_button("Create routine", type="primary", on_click=act, args=(lambda s: R.create_template(
            s, title=val("rt_title", ""), pillar=val("rt_pillar"), priority=val("rt_prio"),
            frequency=val("rt_freq"), weekdays=val("rt_days") or None, month_day=val("rt_mday") or None,
            custom_rule=val("rt_custom") or None, start_date=val("rt_start"), end_date=val("rt_end"),
            estimated_minutes=val("rt_est") or None, missed_policy=val("rt_policy"), protected=val("rt_prot")),
            "Routine created"))


def page_recurring() -> None:
    st.title("Recurring tasks")
    st.caption("Each day's occurrence is its own record - finishing today never touches tomorrow.")
    for tpl in s.scalars(select(RecurringTemplate).order_by(RecurringTemplate.pillar_code, RecurringTemplate.id)):
        if pillar_filter and tpl.pillar_code not in pillar_filter:
            continue
        done, total = recurring_adherence(s, tpl.id, day)
        with st.container(border=True):
            c1, c2, c3 = st.columns([5, 2, 1.4])
            c1.markdown(f"**{tpl.title}** {'🛡️' if tpl.protected else ''}")
            c1.caption(f"{ICON.get(tpl.pillar_code)} {tpl.pillar_code} · P{tpl.priority} · {tpl.frequency_label} · "
                       f"{'missed → recorded' if tpl.missed_policy == 'mark_missed' else 'carries over'}"
                       f"{'' if tpl.active else ' · **paused**'}")
            c2.metric("Last 14 days", f"{done}/{total}" if total else "-")
            c3.button("Resume" if not tpl.active else "Pause", key=f"tp{tpl.id}", on_click=act, args=(
                lambda s, i=tpl.id, a=not tpl.active: R.set_active(s, i, a), "Updated"))
            nxt = R.upcoming_dates(tpl, day + dt.timedelta(days=1), 14)[:5]
            st.caption("Next: " + (", ".join(d.strftime("%a %b %d") for d in nxt) or "none"))
            with st.expander("Skip a date"):
                st.date_input("Date to skip", key=f"sk{tpl.id}", value=day + dt.timedelta(days=1))
                st.button("Skip it", key=f"skb{tpl.id}", on_click=act, args=(
                    lambda s, i=tpl.id: R.add_exception(s, i, val(f"sk{i}")), "Date skipped"))


def page_all() -> None:
    st.title("All tasks")
    c = st.columns(2)
    statuses = c[0].multiselect("Status", [str(x) for x in S], default=[str(x) for x in OPEN_STATES])
    prios = c[1].multiselect("Priority", [1, 2, 3], default=[1, 2, 3], format_func=lambda p: f"P{p}")
    rows = [t for t in s.scalars(select(Task).order_by(Task.priority, Task.due_date, Task.id))
            if t.status in statuses and t.priority in prios and visible(t)]
    st.dataframe([{"#": t.id, "P": t.priority, "title": t.title, "pillar": t.pillar_code, "status": t.status,
                   "type": "recurring" if t.is_recurring else "one-time", "scheduled": t.scheduled_date,
                   "due": t.due_date, "goal": t.goal.code if t.goal else None} for t in rows],
                 hide_index=True, use_container_width=True)
    pick = st.selectbox("Open a task", [None] + [t.id for t in rows],
                        format_func=lambda i: "-" if i is None else next(f"#{t.id} {t.title}" for t in rows if t.id == i))
    if pick:
        t = s.get(Task, pick)
        task_card(t, T.gate_for(s, t, day) if t.status in (S.PENDING, S.IN_PROGRESS) else None,
                  bool(t.due_date and t.due_date < day and t.status in OPEN_STATES), prefix="all-")
        if t.description:
            st.markdown(t.description)


def page_goals() -> None:
    st.title("Goals")
    st.caption("Linked-task counts show **activity**, not achievement. Progress is judged by the evidence "
               "each goal defines.")
    for pillar in s.scalars(select(Pillar).order_by(Pillar.sort_order)):
        if pillar_filter and pillar.code not in pillar_filter:
            continue
        goals = list(s.scalars(select(Goal).where(Goal.pillar_code == pillar.code).order_by(Goal.code)))
        st.subheader(f"{ICON[pillar.code]} {pillar.name}")
        st.caption(pillar.purpose)
        for g in goals:
            linked = [t for t in g.tasks if t.status != S.CANCELLED]
            done = sum(t.status == S.COMPLETED for t in linked)
            with st.container(border=True):
                st.markdown(f"**{g.code} · {g.title}**" + (f" · target {g.target_date:%b %Y}" if g.target_date else ""))
                st.caption(g.description)
                st.caption(f"Evidence of progress: {g.progress_evidence or ':red[not defined]'}  \n"
                           f"Current value: {g.current_value or '_unknown_'} · Linked tasks done: {done}/{len(linked)}")


def page_review() -> None:
    st.title(f"End-of-day review · {day:%a %b %d}")
    r = review_day(s, day)
    c = st.columns(4)
    c[0].metric("Completed", len(r.completed))
    c[1].metric("P1 completion", "n/a" if r.p1_rate is None else f"{r.p1_rate:.0%}",
                help=f"{r.p1_completed} of {r.p1_total} P1 commitments due")
    c[2].metric("Missed routines", len(r.missed))
    c[3].metric("Gate overrides", r.gate_overrides)
    left, right = st.columns(2)
    with left:
        st.subheader("Done")
        for t in r.completed:
            st.markdown(f"- ✓ {t.title}")
        if not r.completed:
            st.caption("Nothing recorded yet.")
    with right:
        st.subheader("Still open")
        for t in r.incomplete:
            st.markdown(f"- P{t.priority} {t.title}")
            cc = st.columns([3, 1])
            cc[0].text_input("Why / reason", key=f"rv{t.id}", label_visibility="collapsed",
                             placeholder="Why not done? (feeds pattern review)")
            cc[1].button("→ Tomorrow", key=f"rvb{t.id}", on_click=act, args=(
                lambda s, i=t.id: T.reschedule(s, i, day + dt.timedelta(days=1), val(f"rv{i}"), today=day),
                "Moved to tomorrow"))
    if r.missed:
        st.subheader("Missed routines")
        st.markdown("\n".join(f"- {t.title}" for t in r.missed))
    flags = detect_patterns(s, day)
    if flags:
        st.subheader("Patterns worth a look")
        for f in flags:
            st.info(f.message, icon="🔍")
    st.subheader("Note")
    st.text_area("How did today go? What got in the way?", key="rv_note", label_visibility="collapsed")
    st.button("Save review", type="primary", on_click=act, args=(
        lambda s: save_review(s, day, val("rv_note", "")), "Review saved"))


{"Today": page_today, "Add": page_add, "Recurring": page_recurring, "All tasks": page_all,
 "Goals": page_goals, "Review": page_review}[page]()
s.close()
