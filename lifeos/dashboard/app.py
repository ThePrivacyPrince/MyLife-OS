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
from lifeos.engines.cascade import TIER_LABELS
from lifeos.models import Goal, KeyResult, Objective, Pillar, RecurringTemplate, Task, Vision
from lifeos.models import UnitProgress as UnitProgressModel
from lifeos.services import cascade as C
from lifeos.services import planner_cal as P
from lifeos.services import task_edit as E
from lifeos.services import daily_check as D
from lifeos.services import outcomes as O
from lifeos.services import money as M
from lifeos.services import debt_plan as DP
from decimal import Decimal as D_
from lifeos.services import acquisitions as Q
from lifeos.services import university as UNI
from lifeos.services import notes as NT
from lifeos.services import jobs as JB
from lifeos.services import studio as ST
from lifeos.engines import jobprep as JP
from lifeos.engines import learning as LRN
from lifeos.engines import acquisition as AQ
from lifeos.services import weekly as W
from lifeos.services import recurring as R
from lifeos.services import tasks as T
from lifeos.services.planner import detect_patterns, plan_day, recurring_adherence, review_day, save_review

st.set_page_config(page_title="LIFE OS", page_icon="◎", layout="wide")
SS = st.session_state
ICON = {"BUS": "💼", "REL": "🤝", "ENV": "🏠", "FIN": "💵", "EDU": "🎓"}
PRIORITY_COLOR = {1: "red", 2: "orange", 3: "gray"}


@st.cache_resource
def _factory_for(url: str):
    engine = make_engine(url)
    init_db(engine)
    return session_factory(engine)


def _factory():
    """One engine per database location, so switching LIFEOS_DB never reads the wrong file."""
    return _factory_for(get_settings().database_url)


def flash(kind: str, msg: str) -> None:
    SS["flash"] = (kind, msg)


def act_warn(fn, ok: str) -> None:
    """Like act(), for service calls that return (row, warnings): show horizon warnings with the success."""
    def run(s):
        _, warnings = fn(s)
        SS["plan_warnings"] = warnings
    act(run, ok)


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
PAGES = ["Today", "Planner", "Plan", "Timeline", "Money", "Deals", "University", "Jobs", "Studio", "Add", "Recurring",
         "All tasks", "Review"]
page = st.sidebar.radio("View", PAGES, label_visibility="collapsed", key="nav")
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
    for w in SS.pop("plan_warnings", []) if kind == "success" else []:
        st.info(w, icon="🧭")

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
        chain = C.lineage(t)
        if chain:
            left.caption("↑ serves: " + " → ".join(f"{l.title}" for l in chain))
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


def edit_form(t: Task, k: str) -> None:
    """One form to edit everything about a task. Audit rules still apply; nothing saves if any part is refused."""
    v = E.task_values(t)
    with st.form(f"edit{k}"):
        st.text_input("Title", value=v["title"], key=f"e_t{k}")
        st.text_area("Notes", value=v["description"], key=f"e_n{k}", height=80)
        c = st.columns(3)
        pills = _pillar_options()
        c[0].selectbox("Pillar", pills, index=pills.index(v["pillar"]), key=f"e_p{k}",
                       format_func=lambda x: f"{ICON[x]} {x}")
        c[1].radio("Priority", [1, 2, 3], index=v["priority"] - 1, key=f"e_pr{k}", horizontal=True,
                   format_func=lambda p: f"P{p}")
        c[2].number_input("Estimate (min, 0 = none)", min_value=0, max_value=1440, step=5,
                          value=v["estimated_minutes"] or 0, key=f"e_m{k}")
        c = st.columns(2)
        c[0].date_input("Work on it on", value=v["scheduled_date"] or day, key=f"e_s{k}")
        c[0].checkbox("No work day yet", value=v["scheduled_date"] is None, key=f"e_sn{k}")
        c[1].date_input("Due date", value=v["due_date"] or day, key=f"e_d{k}")
        c[1].checkbox("No due date", value=v["due_date"] is None, key=f"e_dn{k}")
        c = st.columns(2)
        goals = _goal_options()
        c[0].selectbox("Goal", goals, index=goals.index(v["goal_code"]) if v["goal_code"] in goals else 0,
                       key=f"e_g{k}", format_func=_goal_label)
        krs = _kr_options()
        c[1].selectbox("Drives key result ↑", krs, key=f"e_k{k}", format_func=_kr_label,
                       index=krs.index(v["key_result_code"]) if v["key_result_code"] in krs else 0)
        c = st.columns(2)
        c[0].checkbox("Requires evidence to complete", value=v["evidence_required"], key=f"e_ev{k}")
        c[1].checkbox("Protected (never gated)", value=v["protected"], key=f"e_pt{k}")
        st.text_input("Reason (only needed to change priority, move a due date, or push back work that was due)",
                      key=f"e_r{k}")
        st.form_submit_button("Save changes", type="primary", on_click=act, args=(
            lambda s, i=t.id: E.update_task(
                s, i, title=val(f"e_t{k}", ""), description=val(f"e_n{k}", ""), pillar=val(f"e_p{k}"),
                priority=val(f"e_pr{k}"), estimated_minutes=val(f"e_m{k}") or None,
                scheduled_date=None if val(f"e_sn{k}") else val(f"e_s{k}"),
                due_date=None if val(f"e_dn{k}") else val(f"e_d{k}"),
                goal_code=val(f"e_g{k}"), key_result_code=val(f"e_k{k}"),
                evidence_required=val(f"e_ev{k}"), protected=val(f"e_pt{k}"),
                reason=val(f"e_r{k}", ""), today=day), "Task updated ✏️"))


def _rule_parts(rule: str) -> tuple[list[str], int]:
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    days = parts.get("BYDAY", "").split(",") if parts.get("BYDAY") else []
    return days, int(parts.get("BYMONTHDAY", 0) or 0)


def routine_edit_form(tpl: RecurringTemplate) -> None:
    k = f"r{tpl.id}"
    freq_now = tpl.frequency_label.split(" (")[0] or "daily"
    days_now, mday_now = _rule_parts(tpl.rule)
    freqs = ["daily", "weekdays", "weekly", "biweekly", "monthly", "custom"]
    with st.form(f"redit{k}"):
        st.text_input("Routine", value=tpl.title, key=f"re_t{k}")
        c = st.columns(3)
        pills = _pillar_options()
        c[0].selectbox("Pillar", pills, index=pills.index(tpl.pillar_code), key=f"re_p{k}",
                       format_func=lambda x: f"{ICON[x]} {x}")
        c[1].radio("Priority", [1, 2, 3], index=tpl.priority - 1, key=f"re_pr{k}", horizontal=True,
                   format_func=lambda p: f"P{p}")
        c[2].number_input("Estimate (min, 0 = none)", min_value=0, max_value=1440, step=5,
                          value=tpl.estimated_minutes or 0, key=f"re_m{k}")
        c = st.columns(3)
        c[0].selectbox("Repeats", freqs, index=freqs.index(freq_now) if freq_now in freqs else 0, key=f"re_f{k}")
        c[1].multiselect("On days", ["MO", "TU", "WE", "TH", "FR", "SA", "SU"], default=days_now, key=f"re_d{k}")
        c[2].number_input("Day of month (0 = start day)", min_value=0, max_value=31, value=mday_now, key=f"re_md{k}")
        st.text_input("Custom RRULE", value=tpl.rule if freq_now == "custom" else "", key=f"re_c{k}")
        c = st.columns(3)
        c[0].selectbox("If a day is missed", ["mark_missed", "carry_over"],
                       index=0 if tpl.missed_policy == "mark_missed" else 1, key=f"re_mp{k}",
                       format_func=lambda m: {"mark_missed": "Record as missed", "carry_over": "Carry over"}[m])
        c[1].date_input("Ends", value=tpl.end_date or day + dt.timedelta(days=90), key=f"re_e{k}")
        c[1].checkbox("No end date", value=tpl.end_date is None, key=f"re_en{k}")
        c[2].checkbox("Protected (never gated)", value=tpl.protected, key=f"re_pt{k}")
        st.text_input("Reason (needed to change priority)", key=f"re_r{k}")
        st.caption("Changes apply to today's open occurrence and every future one. Past days stay as they happened.")
        st.form_submit_button("Save routine", type="primary", on_click=act, args=(
            lambda s, i=tpl.id: E.update_template(
                s, i, title=val(f"re_t{k}"), pillar=val(f"re_p{k}"), priority=val(f"re_pr{k}"),
                estimated_minutes=val(f"re_m{k}") or None, missed_policy=val(f"re_mp{k}"),
                protected=val(f"re_pt{k}"), end_date=None if val(f"re_en{k}") else val(f"re_e{k}"),
                frequency=val(f"re_f{k}"), weekdays=val(f"re_d{k}") or None, month_day=val(f"re_md{k}") or None,
                custom_rule=val(f"re_c{k}") or None, reason=val(f"re_r{k}", ""), today=day), "Routine updated ✏️"))


def task_actions(t: Task, k: str, gated: bool) -> None:
    allowed = TRANSITIONS[S(t.status)]
    edit_tab, *tabs = st.tabs(["✏️ Edit", "Block / wait", "Defer / cancel", "Priority", "Reschedule", "Evidence",
                               "History", "Link ↑"] + (["Override"] if gated else []))
    with edit_tab:
        edit_form(t, k)
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
    with tabs[6]:
        krs = _kr_options()
        current = t.key_result.code if t.key_result else None
        st.selectbox("Which key result does this drive?", krs, index=krs.index(current) if current in krs else 0,
                     key=f"lk{k}", format_func=_kr_label)
        st.button("Save link", key=f"lkb{k}", on_click=act, args=(
            lambda s, i=t.id: C.link_task(s, i, val(f"lk{k}")), "Linked"))
    if gated:
        with tabs[7]:
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



# ================================================================== Outcomes + daily check
OUT_ICON = {"open": "🎯", "waiting": "⏳", "achieved": "✅", "partial": "◐", "missed": "✗", "dropped": "—",
            "carried": "↪️"}
AREA_TEXT = {"price": "💲 price", "timing": "🕒 timing", "terms": "📄 terms", "scope": "📐 scope"}


def _open_task_options(include: int | None = None) -> list[int]:
    rows = s.scalars(select(Task).where(Task.status.in_(["pending", "in_progress", "waiting", "blocked"]),
                                        Task.protected.is_(False)))
    ids = [x.id for x in sorted(rows, key=lambda x: (x.priority, x.due_date or dt.date.max, x.id))]
    if include and include not in ids:
        ids.insert(0, include)
    return ids


def _task_label(i: int | None) -> str:
    if i is None:
        return "-"
    x = s.get(Task, i)
    return f"#{x.id} {x.title} · P{x.priority}" if x else f"#{i}"


def outcome_line(o) -> str:
    nt = o.next_task
    nxt = ""
    if nt:
        nxt = f" · next: {'✅ ' if nt.status == S.COMPLETED else ''}{nt.title}"
        if nt.scheduled_date and nt.status != S.COMPLETED:
            nxt += f" ({nt.scheduled_date:%a})"
    wait = f" · ⏳ follow up {o.follow_up_date:%a %b} {o.follow_up_date.day}" if o.status == "waiting" else ""
    streak = O.carry_streak(s, o)
    carried = f" · :orange[carried {streak}×]" if streak else ""
    return f"{OUT_ICON[o.status]} **{o.title}**:gray[{nxt}{wait}]{carried}"


def outcomes_strip(where: str) -> None:
    outs = O.outcomes_for_week(s, day)
    if not outs:
        st.caption("🎯 No outcomes for this week yet - set up to three finish lines in **Planner → This week's outcomes**.")
        return
    st.markdown("**🎯 This week's outcomes**")
    for o in outs:
        st.markdown(outcome_line(o))
        if O.carry_streak(s, o) >= 2 and where == "today":
            st.caption("Carried 2+ weeks - is the finish line too big, or blocked by something not written down?")


def outcome_manager() -> None:
    week = P.week_start(_pl_anchor())
    outs = O.outcomes_for_week(s, week)
    live = [o for o in outs if o.status in O.OPEN]
    with st.expander(f"🎯 This week's outcomes ({len(live)}/{O.MAX_OUTCOMES})", expanded=not outs):
        st.caption("Finish lines for the week: something visibly different by Sunday. Habits belong in Recurring.")
        for o in outs:
            r = st.columns([0.8, 0.2])
            r[0].markdown(outcome_line(o))
            if o.status not in O.OPEN:
                if o.close_note or o.evidence:
                    r[0].caption(o.evidence or o.close_note)
                continue
            k = f"oc{o.id}"
            with r[1].popover("Manage"):
                with st.form(f"of{k}"):
                    st.text_input("Finish line", value=o.title, key=f"ot{k}")
                    opts = [None] + _open_task_options(o.next_task_id)
                    st.selectbox("Next task", opts, index=opts.index(o.next_task_id) if o.next_task_id in opts else 0,
                                 key=f"on{k}", format_func=_task_label)
                    st.text_area("Evidence (what proves it's done?)", value=o.evidence or "", key=f"oe{k}", height=70)
                    st.checkbox("Waiting on someone", value=o.status == "waiting", key=f"ow{k}")
                    st.date_input("Follow up on", value=o.follow_up_date or day + dt.timedelta(days=2), key=f"of{k}d")
                    st.form_submit_button("Save", type="primary", on_click=act, args=(
                        lambda s, i=o.id: O.update_outcome(
                            s, i, title=val(f"ot{k}"), next_task_id=val(f"on{k}"), evidence=val(f"oe{k}", ""),
                            waiting_until=val(f"of{k}d") if val(f"ow{k}") else None, today=day), "Outcome updated"))
                st.markdown("**Close it**")
                st.selectbox("As", ["achieved", "partial", "missed", "dropped"], key=f"os{k}")
                st.text_input("Note (evidence if achieved; what happened otherwise)", key=f"oz{k}")
                c = st.columns(2)
                c[0].button("Close", key=f"oc{k}b", on_click=act, args=(
                    lambda s, i=o.id: O.close_outcome(s, i, val(f"os{k}"), val(f"oz{k}", "")), "Outcome closed"))
                c[1].button("Carry to next week", key=f"ocr{k}", on_click=act, args=(
                    lambda s, i=o.id: O.carry_outcome(s, i, val(f"oz{k}", "")), "Carried to next week"))
        if len(live) < O.MAX_OUTCOMES:
            with st.form(f"oadd{week}", clear_on_submit=True):
                c = st.columns([3, 1, 2])
                c[0].text_input("New finish line", key="oa_t",
                                placeholder="e.g. First RXO pay date confirmed in writing")
                c[1].selectbox("Pillar", [None] + _pillar_options(), key="oa_p",
                               format_func=lambda x: "-" if x is None else f"{ICON[x]} {x}")
                opts = [None] + _open_task_options()
                c[2].selectbox("Next task", opts, key="oa_n", format_func=_task_label)
                st.form_submit_button("Add outcome", on_click=act, args=(lambda s: O.add_outcome(
                    s, any_day=week, title=val("oa_t", ""), pillar=val("oa_p"), next_task_id=val("oa_n")),
                    "Outcome added"))


def yesterday_card(plan) -> None:
    c = D.get_check(s, day)
    if c is None:
        if D.missed_twice(s, day):
            st.info("🌙 Two days without the evening check - never miss twice. It takes 3 minutes (below).")
        return
    with st.container(border=True):
        st.markdown("**🧭 Yesterday-you decided**")
        if c.progress_task:
            tk = c.progress_task
            done = "✅ " if tk.status == S.COMPLETED else ""
            out = f" · ↑ moves: _{c.progress_outcome.title}_" if c.progress_outcome else ""
            st.markdown(f"**1. Progress:** {done}{tk.title}{out}")
        else:
            st.markdown("**1. Progress:** :gray[no action picked]")
        if c.no_ask or not c.ask_what:
            st.markdown("**2. Ask:** :gray[nothing worth asking today]")
        else:
            st.markdown(f"**2. Ask {c.ask_who}** for better {AREA_TEXT.get(c.ask_area, c.ask_area)}: {c.ask_what}")
            with st.expander("What you'll say"):
                st.markdown(c.ask_script)
            if c.ask_result:
                res = {"got": "✅ got it", "partial": "◐ partly", "no": "✗ no", "not_asked": "— didn't ask"}[c.ask_result]
                st.caption(f"Result: {res}" + (f" · {c.ask_gain}" if c.ask_gain else ""))
            else:
                r = st.columns([2.2, 0.8, 0.8, 0.8, 1])
                r[0].text_input("What you got", key=f"ag{day}", label_visibility="collapsed",
                                placeholder="What did you get? e.g. $300 off")
                for col, (res, label) in zip(r[1:], [("got", "Got it"), ("partial", "Partly"), ("no", "No"),
                                                     ("not_asked", "Didn't ask")]):
                    col.button(label, key=f"ar{res}{day}", on_click=act, args=(
                        lambda s, rr=res: D.record_ask(s, day, rr, val(f"ag{day}", "")), "Ask recorded"))
        cap = P.capacity_for(s, day)
        prot = f" · protect: {c.protect_note}" if c.protect_note else ""
        st.markdown(f"**3. Capacity:** about {cap.available} min for P1/P2 "
                    f":gray[(awake {cap.awake / 60:.0f}h - calendar {cap.booked / 60:.1f}h - buffer {cap.buffer // 60}h)]{prot}")
        if c.bump_task_ids:
            names = [s.get(Task, i).title for i in c.bump_task_ids if s.get(Task, i)]
            st.caption("Moves first if the day is full: " + " · ".join(names))
            if plan.overcommitted:
                st.warning("The day is full - move the tasks you pre-decided?")
                st.button("Move them to tomorrow", key="bumpgo", on_click=act, args=(
                    lambda s: D.move_bumped(s, day, today=day), "Moved as pre-decided"))


def daily_check_form() -> None:
    choice = st.radio("Plan for", ["Tomorrow", "Today"], horizontal=True, key="dck_for",
                      help="With overnights, 'today' may be the right day if you're planning after waking up.")
    for_date = day + dt.timedelta(days=1) if choice == "Tomorrow" else day
    ex = D.get_check(s, for_date)
    outs = [o for o in O.outcomes_for_week(s, for_date) if o.status in O.OPEN]
    k = f"{for_date}"
    with st.form(f"dck{k}"):
        st.markdown(f"**1. Progress** - What's one action {choice.lower()} that moves a weekly outcome?")
        if outs:
            ids = [o.id for o in outs]
            cur_o = ex.progress_outcome_id if ex and ex.progress_outcome_id in ids else ids[0]
            c = st.columns(2)
            c[0].selectbox("Outcome", ids, index=ids.index(cur_o), key=f"d_o{k}",
                           format_func=lambda i: next(o.title for o in outs if o.id == i))
            topts = [None] + _open_task_options(ex.progress_task_id if ex else None)
            cur_t = ex.progress_task_id if ex and ex.progress_task_id in topts else None
            c[1].selectbox("Existing task", topts, index=topts.index(cur_t), key=f"d_t{k}", format_func=_task_label)
            st.text_input("…or a new task", key=f"d_n{k}", placeholder="e.g. Email Don Bosco AD about winter stipend")
        else:
            st.caption("No open outcomes this week - add them under **Planner → This week's outcomes**.")
        st.markdown("**2. Negotiation** - What's one thing I can ask for that would improve the price, timing, terms "
                    "or scope? Who will I ask, and what exactly will I say?")
        st.checkbox("Nothing worth asking", value=bool(ex and ex.no_ask), key=f"d_x{k}")
        c = st.columns([2, 1])
        c[0].text_input("Who", value=ex.ask_who if ex else "", key=f"d_w{k}", placeholder="e.g. VX seller on Marketplace")
        areas = list(D.ASK_AREAS)
        c[1].selectbox("Improves", areas, index=areas.index(ex.ask_area) if ex and ex.ask_area in areas else 0,
                       key=f"d_a{k}", format_func=lambda a: AREA_TEXT[a])
        st.text_input("What you're asking for", value=ex.ask_what if ex else "", key=f"d_q{k}",
                      placeholder="e.g. $4,200 with the trailer instead of $4,800")
        st.text_area("Exactly what you'll say", value=ex.ask_script if ex else "", key=f"d_s{k}", height=80,
                     placeholder="e.g. I can pick it up Saturday morning with cash. Would you take $4,200 with the trailer?")
        st.markdown("**3. Capacity** - What do I need to protect, and what moves if the day is full?")
        blocks = [b for b in P.blocks_between(s, for_date - dt.timedelta(days=1), for_date)
                  if b.day == for_date or (b.overnight and b.day == for_date - dt.timedelta(days=1))]
        if blocks:
            st.caption("On the calendar: " + " · ".join(
                f"{P.KINDS.get(b.kind, '•')} {b.time_label} {b.title}" + (" (from the night before)" if b.day != for_date else "")
                for b in blocks))
        cap = P.capacity_for(s, for_date)
        c = st.columns([1, 3])
        c[0].number_input("Awake hours", min_value=0.0, max_value=24.0, step=0.5, value=round(cap.awake / 60, 1),
                          key=f"d_h{k}", help="Overnights and practice days vary - set it for that day.")
        c[1].text_input("Protect", value=ex.protect_note if ex else "", key=f"d_p{k}",
                        placeholder="e.g. Sleep 8a-3p after the overnight · time with Amari 5-7 · track 3:30-5:30")
        cands = D.tasks_on(s, for_date)
        st.multiselect("Moves first if the day is full", [x.id for x in cands],
                       default=[i for i in (ex.bump_task_ids if ex else []) if i in {x.id for x in cands}],
                       key=f"d_b{k}", format_func=_task_label)
        st.form_submit_button("Save the plan", type="primary", on_click=act, args=(lambda s: D.save_check(
            s, for_date=for_date, today=day,
            outcome_id=val(f"d_o{k}") if outs and (val(f"d_t{k}") or (val(f"d_n{k}") or "").strip()) else None,
            task_id=val(f"d_t{k}"), new_task_title=val(f"d_n{k}", "") or "",
            no_ask=val(f"d_x{k}", False), ask_who=val(f"d_w{k}", ""),
            ask_area=val(f"d_a{k}") if (val(f"d_w{k}") or val(f"d_q{k}") or val(f"d_s{k}")) else "",
            ask_what=val(f"d_q{k}", ""), ask_script=val(f"d_s{k}", ""), protect_note=val(f"d_p{k}", ""),
            awake_hours=val(f"d_h{k}"), bump_task_ids=val(f"d_b{k}", [])), f"{choice}'s plan saved 🌙"))
    if ex:
        st.caption("Saved already - saving again updates it.")


# ================================================================== Money
PACE_TEXT = {"on_track": "🟢 on track", "behind": "🟠 behind pace", "done": "✅ done", "no_deadline": "",
             "over_budget": "🟠 over budget", "under_budget": "🟢 within budget", "no_budget": ""}


def fund_card(f) -> None:
    p = M.progress(f, day)
    k = f"fd{f.id}"
    with st.container(border=True):
        head = f"**{f.name}**"
        if f.kind == "debt" and f.counterparty:
            head += f" :gray[· to {f.counterparty}]"
        st.markdown(head)
        if f.kind == "save":
            text = f"{M.fmt(p.done)} of {M.fmt(p.goal)} saved · {p.fraction:.0%}"
        elif f.kind == "debt":
            text = f"Paid {M.fmt(p.done)} of {M.fmt(p.goal)} · {M.fmt(M.remaining(f))} left"
        else:
            text = (f"This month: {M.fmt(p.done)} of {M.fmt(p.goal)} budget" if p.goal
                    else f"This month: {M.fmt(p.done)} spent")
        st.progress(p.fraction, text=text)
        bits = []
        if f.deadline:
            bits.append(f"by {f.deadline:%b} {f.deadline.day}, {f.deadline.year}")
        if p.weekly_needed and p.pace != "done":
            bits.append(f"{M.fmt(p.weekly_needed)}/week gets you there")
        if p.pace == "done":
            bits.append("✅ paid off" if f.kind == "debt" else "✅ fully saved")
        elif PACE_TEXT.get(p.pace):
            bits.append(PACE_TEXT[p.pace])
        if p.eta and p.pace != "done":
            bits.append(f"at your recent pace: ~{p.eta:%b} {p.eta.day}")
        if f.kept_where:
            bits.append(f"kept in: {f.kept_where}")
        if bits:
            st.caption(" · ".join(bits))
        unlock = M.cooloff_ends(f)
        if f.space == "wants":
            if unlock is None:
                st.caption(f"🧊 Once it's fully saved, a {get_settings().money.cooloff_days}-day cool-off starts. "
                           "Still want it after? Then buy it.")
            elif day < unlock:
                st.caption(f"🧊 Fully saved. Cool-off ends {unlock:%a %b} {unlock.day} - "
                           f"{(unlock - day).days} day(s) to be sure.")
            else:
                st.caption("🔓 Cool-off done. Still want it? **More → Bought it**, or drop it and keep the money.")
        if f.kind == "spend" and f.entries:
            recent = sorted(f.entries, key=lambda e: (e.day, e.id))[-3:]
            st.caption("Recent: " + " · ".join(f"{e.day:%b} {e.day.day} {M.fmt(e.amount)} {e.note}".strip() for e in recent))
        r = st.columns([1.1, 2.2, 1, 1, 0.9])
        r[0].text_input("Amount", key=f"amt{k}", label_visibility="collapsed", placeholder="$")
        r[1].text_input("Note", key=f"note{k}", label_visibility="collapsed",
                        placeholder={"spend": "what it was for", "debt": "e.g. Zelle, Nov 30"}.get(f.kind, "note (optional)"))
        actions = {"save": [("add", "Set aside"), ("withdraw", "Take out")],
                   "debt": [("payment", "Paid"), ("charge", "New charge")],
                   "spend": [("spend", "Log spending")]}[f.kind]
        for col, (kind, label) in zip(r[2:4], actions):
            col.button(label, key=f"{kind}{k}", use_container_width=True, type="primary" if kind in ("add", "payment", "spend") else "secondary",
                       on_click=act, args=(lambda s, i=f.id, kk=kind: M.add_entry(
                           s, i, kk, val(f"amt{k}", ""), note=val(f"note{k}", ""), today=day), f"{label} ✓"))
        with r[4].popover("More", use_container_width=True):
            fund_more(f, k)


def fund_more(f, k: str) -> None:
    hist = sorted(f.entries, key=lambda e: (e.day, e.id), reverse=True)[:12]
    if hist:
        st.markdown("**History**")
        st.dataframe([{"day": e.day, "what": e.kind, "amount": M.fmt(e.amount), "note": e.note} for e in hist],
                     hide_index=True, use_container_width=True)
    with st.form(f"fe{k}"):
        st.markdown("**Edit**")
        st.text_input("Name", value=f.name, key=f"fn{k}")
        c = st.columns(2)
        c[0].text_input("Monthly budget" if f.kind == "spend" else ("Amount owed" if f.kind == "debt" else "Target"),
                        value=str(f.target or ""), key=f"ft{k}")
        c[1].date_input("Deadline", value=f.deadline or day + dt.timedelta(days=60), key=f"fdl{k}")
        c[1].checkbox("No deadline", value=f.deadline is None, key=f"fdn{k}")
        st.text_input("Kept where", value=f.kept_where, key=f"fk{k}")
        if f.kind == "debt":
            st.text_input("Owed to", value=f.counterparty, key=f"fc{k}")
        st.text_input("Reason (needed to move a deadline)", key=f"fr{k}")
        st.form_submit_button("Save", on_click=act, args=(lambda s, i=f.id: M.update_fund(
            s, i, name=val(f"fn{k}"), target=val(f"ft{k}", ""),
            deadline=None if val(f"fdn{k}") else val(f"fdl{k}"), kept_where=val(f"fk{k}", ""),
            counterparty=val(f"fc{k}") if f.kind == "debt" else None, reason=val(f"fr{k}", "")), "Updated"))
    if f.kind == "save":
        st.markdown("**Bought it**")
        st.text_input("What it actually cost", key=f"bp{k}", placeholder="$")
        unlock = M.cooloff_ends(f)
        early = f.space == "wants" and (unlock is None or day < unlock)
        if early:
            st.text_input("Buying before the cool-off ends? Why", key=f"bo{k}")
        st.button("Bought it ✓", key=f"bb{k}", on_click=act, args=(lambda s, i=f.id: M.mark_bought(
            s, i, val(f"bp{k}", ""), today=day, override_reason=val(f"bo{k}", "") if early else ""), "Recorded 🎉"))
    if f.kind != "debt":
        st.markdown("**Drop it**" + (" - what you'd saved counts as kept" if f.space == "wants" else ""))
        st.text_input("Why", key=f"dr{k}")
        st.button("Drop", key=f"dd{k}", on_click=act, args=(lambda s, i=f.id: M.drop_fund(
            s, i, val(f"dr{k}", ""), today=day), "Dropped"))


def add_fund_form(space: str) -> None:
    kinds = ["debt"] if space == "owed" else ["save", "spend"]
    label = {"save": "Saving up for something", "spend": "Tracking spending", "debt": "Money I owe"}
    with st.expander(f"➕ Add to {M.SPACES[space].split(' ', 1)[1]}"):
        with st.form(f"af{space}", clear_on_submit=True):
            if len(kinds) > 1:
                st.radio("Type", kinds, index=kinds.index(M.DEFAULT_KIND[space]), key=f"afk{space}", horizontal=True,
                         format_func=label.get)
            st.text_input("Name", key=f"afn{space}", placeholder={
                "family": "e.g. My son - clothes & school", "holiday": "e.g. Christmas 2026",
                "wants": "e.g. Meta glasses", "reserves": "e.g. Jet Life opening cushion",
                "business": "e.g. Ski purchase fund", "owed": "e.g. Parents"}[space])
            c = st.columns(3)
            c[0].text_input("Target / amount owed / monthly budget", key=f"aft{space}", placeholder="$")
            c[1].date_input("Deadline", value=day + dt.timedelta(days=30), key=f"afd{space}")
            c[1].checkbox("No deadline", value=space in ("family", "wants", "business"), key=f"afdn{space}")
            if space == "owed":
                c[2].text_input("Owed to", key=f"afc{space}")
            else:
                c[2].text_input("Kept where", key=f"afw{space}", placeholder="e.g. savings account")
            st.form_submit_button("Add", type="primary", on_click=act, args=(lambda s: M.create_fund(
                s, name=val(f"afn{space}", ""), space=space,
                kind=val(f"afk{space}") if len(kinds) > 1 else "debt", target=val(f"aft{space}", ""),
                deadline=None if val(f"afdn{space}") else val(f"afd{space}"),
                counterparty=val(f"afc{space}", "") if space == "owed" else "",
                kept_where=val(f"afw{space}", "") if space != "owed" else "", today=day), "Added"))


def before_you_buy() -> None:
    st.caption("Thinking of buying something? Check it first.")
    with st.form("byb", clear_on_submit=False):
        c = st.columns([2, 1])
        c[0].text_input("What", key="byb_i", placeholder="e.g. Meta glasses")
        c[1].text_input("Price", key="byb_p", placeholder="$")
        st.form_submit_button("Check it")
    item, price = val("byb_i", ""), val("byb_p", "")
    if item and price:
        try:
            amt = M.money(price)
        except Exception:
            st.error("Price isn't a number.")
            return
        hrs = M.hours_of_work(s, amt)
        if hrs is not None:
            st.markdown(f"**{M.fmt(amt)} = about {hrs} hours of work** at your take-home rate.")
        else:
            st.caption("Set your take-home hourly rate under ⚙️ Settings to see this in hours of work.")
        behind = M.behind_protected(s, day)
        if behind:
            st.warning("Behind pace first: " + ", ".join(f.name for f in behind))
        c = st.columns(3)
        c[0].button("Skip it - keep the money", key="byb_s", type="primary", on_click=act, args=(lambda s: M.impulse_check(
            s, item=item, price=amt, decision="skip", today=day), f"Kept {M.fmt(amt)} 💪"))
        c[1].button(f"Wish list ({get_settings().money.cooloff_days}-day cool-off)", key="byb_w", on_click=act,
                    args=(lambda s: M.impulse_check(s, item=item, price=amt, decision="wishlist", today=day),
                          "Added to Wants - save for it, then cool off"))
        c[2].button("Buying it", key="byb_b", on_click=act, args=(lambda s: M.impulse_check(
            s, item=item, price=amt, decision="buy", today=day), "Logged"))


def got_paid() -> None:
    st.caption("Split money as it comes in: what you owe first, then reserves, family, holidays, and wants last.")
    st.text_input("Amount that came in", key="gp_amt", placeholder="$")
    raw = val("gp_amt", "")
    if not raw:
        return
    try:
        splits, left = M.suggest_split(s, raw, day)
    except Exception as exc:
        st.error(str(exc))
        return
    if not splits:
        st.caption("Add a fund with a target first.")
        return
    with st.form("gp_form"):
        for x in splits:
            c = st.columns([3, 1.2])
            c[0].markdown(f"{M.SPACES[x.fund.space].split(' ')[0]} **{x.fund.name}**  \n:gray[{x.why}]")
            c[1].text_input("Amount", value=str(x.suggested) if x.suggested else "", key=f"gp{x.fund.id}",
                            label_visibility="collapsed")
        st.caption(f"Unassigned after suggestions: {M.fmt(left)} - leave it as a buffer or assign it above.")
        st.form_submit_button("Record this split", type="primary", on_click=act, args=(lambda s: M.apply_split(
            s, {x.fund.id: val(f"gp{x.fund.id}", "") for x in splits}, day), "Split recorded 💵"))


def money_settings() -> None:
    rate = M.hourly_rate(s)
    c = st.columns(2)
    c[0].text_input("Take-home pay per hour", value=str(rate or ""), key="ms_rate", placeholder="$ after taxes",
                    help="Stored only in your local database. Used to show prices as hours of work.")
    c[0].button("Save rate", key="ms_rate_b", on_click=act, args=(lambda s: M.set_hourly_rate(s, val("ms_rate", "")),
                                                                  "Saved"))
    c[1].text_input("Cash you actually have right now", key="ms_cash", placeholder="$",
                    help="Optional. Lets LIFE OS warn you if your funds add up to more than you really have.")
    c[1].button("Save cash", key="ms_cash_b", on_click=act, args=(lambda s: M.set_cash_on_hand(
        s, val("ms_cash", ""), day), "Saved"))


PLAY_ICON = {"done": "✅", "todo": "⬜", "warn": "⚠️"}


def debt_playbook() -> None:
    from lifeos.engines import debt as DE
    pv = DP.plan(s)
    fs = DP.debts(s)
    owed = sum((M.remaining(f) for f in fs), M.ZERO)
    chosen = pv.chosen
    paid_m, budget = DP.month_progress(s, day)
    c = st.columns(4)
    c[0].metric("Still owed", M.fmt(owed))
    free = chosen.debt_free(day) if chosen else None
    c[1].metric("Debt-free" + (" (planned debts)" if pv.incomplete else ""),
                f"{free:%b %Y}" if free else ("never at this pace" if chosen else "-"),
                help="On your chosen plan: every minimum, plus your extra, micro-payments rolled into one monthly budget."
                     + (f" Not counted until you add their terms: {', '.join(f.name for f in pv.incomplete)}."
                        if pv.incomplete else ""))
    saved = pv.interest_saved()
    c[2].metric("Interest saved vs minimums", M.fmt(saved) if saved is not None else "-")
    c[3].metric("Paid this month", f"{M.fmt(paid_m)} / {M.fmt(budget)}" if budget else M.fmt(paid_m),
                help="Payments logged this month vs your plan's monthly budget.")
    if DP.micro_due(s, day) and pv.target:
        st.button(f"☕ Micro-pay {M.fmt(D_(DP.setting(s, 'micro_amount')))} → {pv.target.name}", type="primary",
                  key="dp_micro", on_click=act, args=(lambda s: DP.micro_pay(s, day), "Micro-payment logged ☕"))
    for d in DP.due_status(s, day):
        if d.missed:
            st.error(f"**{d.fund.name}**: no payment logged for the last due date. Pay the minimum now and call about the late fee.")
        else:
            st.warning(f"**{d.fund.name}** minimum {M.fmt(d.fund.min_payment)} due "
                       f"{'today' if d.days == 0 else f'in {d.days} day(s)'} ({d.due:%a %b} {d.due.day}).")
    st.markdown("#### The playbook")
    for p in DP.plays(s, day):
        st.markdown(f"{PLAY_ICON[p.status]} **{p.title}** - {p.detail}")
    st.markdown("#### Your debts")
    if not fs:
        st.caption("Add debts on the 💳 Money I owe tab first.")
    for f in fs:
        k = f"dt{f.id}"
        with st.container(border=True):
            miss = DP.missing_terms(f)
            head = f"**{f.name}** · {M.fmt(M.remaining(f))} owed"
            if f.apr is not None:
                head += f" · {f.apr}% APR"
            if f.min_payment is not None:
                head += f" · min {M.fmt(f.min_payment)}"
            if f.due_day:
                head += f" · due the {f.due_day}{'th' if 10 <= f.due_day % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(f.due_day % 10, 'th')}"
            head += " · autopay ✅" if f.autopay else ""
            if pv.target and f.id == pv.target.id:
                head += " · 🎯 **target**"
            st.markdown(head + (f"  \n:orange[Missing: {', '.join(miss)}]" if miss else ""))
            cc = st.columns([1, 1, 1, 1, 1])
            cc[0].text_input("APR %", value="" if f.apr is None else str(f.apr), key=f"{k}a", placeholder="e.g. 24.99")
            cc[1].text_input("Minimum $", value="" if f.min_payment is None else str(f.min_payment), key=f"{k}m")
            cc[2].number_input("Due day", min_value=1, max_value=31, value=f.due_day, key=f"{k}d", placeholder="1-31")
            cc[3].checkbox("Autopay on", value=f.autopay, key=f"{k}p")
            cc[4].button("Save terms", key=f"{k}s", on_click=act, args=(lambda s, i=f.id, k=k: DP.set_terms(
                s, i, apr=val(f"{k}a"), min_payment=val(f"{k}m"), due_day=val(f"{k}d"), autopay=val(f"{k}p")),
                "Terms saved"))
            cc = st.columns([2, 1, 2])
            cc[0].text_input("Statement balance", key=f"{k}b", label_visibility="collapsed",
                             placeholder="Balance on your latest statement")
            cc[1].button("Match statement", key=f"{k}bs", on_click=act, args=(lambda s, i=f.id, k=k: DP.sync_statement(
                s, i, val(f"{k}b", ""), day), "Matched to the statement (interest & fees logged)"))
            if f.apr is not None and f.apr >= DP.HIGH_APR and not M.get_setting(s, f"debt.asked.{f.id}"):
                cc[2].button("📞 Plan the lower-APR call", key=f"{k}ask", on_click=act, args=(
                    lambda s, i=f.id: DP.ask_for_lower_apr(s, i, day), "Task added for the call - script is in the task"))
    st.markdown("#### The plan")
    cc = st.columns(4)
    cc[0].selectbox("Strategy", DE.ORDERS, index=DE.ORDERS.index(pv.strategy), key="dp_strat",
                    format_func=lambda o: "🏔️ Avalanche - highest APR first" if o == "avalanche" else "⛄ Snowball - smallest first")
    cc[1].text_input("Extra per month $", value=DP.setting(s, "extra"), key="dp_extra",
                     help="On top of every minimum - what you commit to, not what you hope for.")
    cc[2].text_input("☕ Micro-payment $", value=DP.setting(s, "micro_amount"), key="dp_micro_amt", placeholder="e.g. 8")
    cc[3].number_input("every N days", min_value=1, max_value=14, value=int(DP.setting(s, "micro_every")), key="dp_micro_every")
    cc = st.columns([1, 3])
    cc[0].text_input("💵 Payday rule %", value=DP.setting(s, "payday_pct"), key="dp_pct", placeholder="e.g. 25",
                     help="Share of every paycheck or windfall suggested for your target debt in 'Got paid? Split it'.")
    cc[1].markdown("&nbsp;")
    cc[1].button("Save plan", type="primary", key="dp_save", on_click=act, args=(lambda s: DP.configure(
        s, strategy=val("dp_strat"), extra=val("dp_extra"), micro_amount=val("dp_micro_amt", ""),
        micro_every=val("dp_micro_every"), payday_pct=val("dp_pct", ""), today=day), "Plan saved"))
    if pv.incomplete:
        st.info("Not in the plan yet (add APR and minimum): " + ", ".join(f.name for f in pv.incomplete))
    if pv.plans:
        names = {f.id: f.name for f in pv.complete}
        rows = []
        for label, p in (("🏔️ Avalanche", pv.plans["avalanche"]), ("⛄ Snowball", pv.plans["snowball"]),
                         ("Minimums only", pv.baseline)):
            d_ = p.debt_free(day)
            first = (names[p.sequence[0]] + f" (month {p.payoff_month[p.sequence[0]]})") if p.sequence else "-"
            rows.append({"Plan": label, "Debt-free": f"{d_:%b %Y}" if d_ else "never", "Months": p.months or "-",
                         "Total interest": M.fmt(p.total_interest), "First payoff": first})
        st.dataframe(rows, hide_index=True, use_container_width=True)
        if chosen and chosen.sequence:
            st.markdown("**Payoff order:** " + " → ".join(
                f"{names[k]} ({DE.add_months(day, chosen.payoff_month[k]):%b %Y})" for k in chosen.sequence))
        st.caption(f"Monthly budget {M.fmt(pv.budget)} = minimums + {M.fmt(pv.extra)} extra (micro-payments included). "
                   "Assumes no new charges and that you keep paying at least today's minimum. Interest monthly at APR/12.")
        micro = DP.setting(s, "micro_amount")
        if micro and pv.target and pv.target.apr is not None:
            r = DE.micro_compare(M.remaining(pv.target), pv.target.apr, pv.target.min_payment, D_(micro),
                                 int(DP.setting(s, "micro_every")))
            with st.expander(f"☕ Honest micro-payment math on {pv.target.name}"):
                st.markdown(
                    f"- Minimums only: **{r.minimum_only[0] or 'never'} months**, {M.fmt(r.minimum_only[1])} interest\n"
                    f"- Same {M.fmt(r.extra_per_month)}/month extra, paid once a month: **{r.monthly_lump[0]} months**, "
                    f"{M.fmt(r.monthly_lump[1])} interest\n"
                    f"- Paid as {M.fmt(D_(micro))} every {DP.setting(s, 'micro_every')} days: **{r.micro[0]} months**, "
                    f"{M.fmt(r.micro[1])} interest\n\n"
                    f"Paying **more** saves {M.fmt(r.saved_by_paying_more)}. Paying **more often** saves another "
                    f"{M.fmt(r.saved_by_paying_often)} (lower average daily balance). The habit is what makes the extra happen.")
        code = M.get_setting(s, "debt.goal_code")
        if code:
            st.caption(f"🎯 Goal **{code} Debt-free** is on your Plan and Timeline.")
        elif chosen and chosen.months:
            st.button(f"🎯 Make 'Debt-free by {chosen.debt_free(day):%b %Y}' a goal", key="dp_goal", on_click=act_warn,
                      args=(lambda s: DP.make_goal(s, day), "Goal created - it now counts down on Plan and Timeline"))
    with st.expander("📚 Why these plays"):
        st.markdown("- **Avalanche** pays the least interest. **Snowball** gives faster first wins, and research shows "
                    "many people stick with it better (Amar et al., 2011; Gathergood et al., 2019 - both in the "
                    "University library, OWN101 unit 2).\n"
                    "- **Autopay the minimum** so a busy week never becomes a late fee or a penalty APR.\n"
                    "- **Micro-payments** turn spare change into a habit; the extra money is what shortens the payoff.\n"
                    "- **Payday rule** decides before the money arrives, when willpower isn't needed.\n"
                    "- **Statement check** keeps the balance real - interest is included, not guessed.")


def page_money() -> None:
    st.title("💰 Money")
    st.caption("Track what you set aside, pay down and spend. LIFE OS tracks money - it never moves it, so record "
               "what you actually did. Each area has its own tab, so nothing gets mixed together.")
    from sqlalchemy import select as _sel
    from lifeos.models import Fund
    active = list(s.scalars(_sel(Fund).where(Fund.status == "active")))
    aside = sum((M.balance(f) for f in active if f.kind == "save"), M.ZERO)
    owed = sum((M.remaining(f) for f in active if f.kind == "debt"), M.ZERO)
    lo, hi = M.month_bounds(day)
    c = st.columns(3)
    c[0].metric("Set aside", M.fmt(aside))
    c[1].metric("Still owed", M.fmt(owed))
    c[2].metric("Kept by not buying", M.fmt(M.kept_total(s)), help=f"This month: {M.fmt(M.kept_total(s, lo, hi))}")
    cash, cash_day, set_aside = M.cash_check(s)
    if cash is not None and set_aside > cash:
        st.warning(f"Your funds say {M.fmt(set_aside)} is set aside, but you reported {M.fmt(cash)} on "
                   f"{cash_day:%b} {cash_day.day}. Some of that money may be counted twice or already spent.")
    t1, t2, t3 = st.columns(3)
    with t1.expander("🧠 Before you buy"):
        before_you_buy()
    with t2.expander("💵 Got paid? Split it"):
        got_paid()
    with t3.expander("⚙️ Settings"):
        money_settings()
    tabs = st.tabs(list(M.SPACES.values()) + ["🎯 Debt playbook"])
    with tabs[-1]:
        debt_playbook()
    for tab, space in zip(tabs, M.SPACES):
        with tab:
            funds = M.funds_in(s, space)
            if space == "wants":
                behind = M.behind_protected(s, day)
                if behind:
                    st.info("⏸️ Before wants: " + ", ".join(f"**{f.name}**" for f in behind) +
                            " " + ("is" if len(behind) == 1 else "are") + " behind pace. Fill those first?")
            if not funds:
                st.caption("Nothing here yet.")
            for f in funds:
                fund_card(f)
            add_fund_form(space)
            closed = [f for f in M.funds_in(s, space, include_closed=True) if f.status != "active"]
            if closed:
                with st.expander(f"Done & dropped ({len(closed)})"):
                    for f in closed:
                        extra = f" · paid {M.fmt(f.paid_price)}" if f.paid_price is not None else ""
                        st.markdown(f"{'✅' if f.status == 'done' else '—'} {f.name}{extra}")


# ================================================================== Deals (acquisitions)
FLAG_ICON = {"risk": "🔴", "caution": "🟠", "leverage": "💲"}
COND = list(AQ.CONDITIONS)


def _dollars(d) -> str:
    return "unknown" if d is None else f"${d:,.0f}"


def candidate_summary(ev, rank: int | None = None) -> None:
    c = ev.candidate
    head = f"**{'#' + str(rank) + ' · ' if rank else ''}{c.title}**"
    st.markdown(head + (f" :gray[· {c.source}]" if c.source else "") + (f" · [listing]({c.url})" if c.url else ""))
    facts = [f"asking {_dollars(c.asking_price)}"]
    if c.offer:
        facts.append(f"offer {_dollars(c.offer)}")
    facts += [f"{c.engine_hours} hrs" if c.engine_hours is not None else "hours unknown",
              f"title {c.title_status}", f"trailer {c.trailer_included}", c.location or "location unknown"]
    st.caption(" · ".join(facts))
    c1, c2, c3 = st.columns(3)
    c1.metric("Ready to rent", ev.cost.label)
    c2.metric("Gates passed", f"{ev.gates_passed}/7")
    c3.metric("Facts known", f"{ev.facts_known}/{ev.facts_total}")
    for f in ev.flags:
        st.caption(f"{FLAG_ICON[f.level]} {f.text}")
    plan = Q.offer_plan(s, c, day)
    if plan.dealbreakers:
        st.markdown("🚫 **Walk away - don't negotiate.** " + " ".join(plan.dealbreakers))
    elif plan.ready:
        pre = " :gray[(preliminary - before inspection)]" if plan.preliminary else ""
        st.markdown(f"💲 **Offers:** 🔥 great **${plan.great:,.0f}** · 🤝 fair **${plan.fair:,.0f}** · "
                    f"🚫 walk away above **${plan.walk_away:,.0f}**{pre}")
    st.info(f"**Next step:** {ev.next_step}", icon="👉")


def candidate_detail(ev) -> None:
    c = ev.candidate
    k = f"cd{c.id}"
    t1, t2, tn, t3, t4 = st.tabs(["Facts & costs", "🔧 Inspection", "💲 Negotiate", "Gates & decision", "History"])
    with t1, st.form(f"cf{k}"):
        r = st.columns(3)
        r[0].text_input("Listing name", value=c.title, key=f"c_t{k}")
        r[1].text_input("Source", value=c.source, key=f"c_src{k}")
        r[2].text_input("Link", value=c.url, key=f"c_url{k}")
        r = st.columns(4)
        r[0].text_input("Make", value=c.manufacturer, key=f"c_mk{k}")
        r[1].text_input("Model", value=c.model, key=f"c_md{k}")
        r[2].number_input("Year (0 = unknown)", 0, 2100, value=c.year or 0, key=f"c_y{k}")
        r[3].number_input("Engine hours (0 = unknown)", 0, 5000, value=c.engine_hours or 0, key=f"c_h{k}")
        r = st.columns(4)
        r[0].text_input("Asking price", value=str(c.asking_price or ""), key=f"c_p{k}")
        r[1].text_input("Your offer", value=str(c.offer or ""), key=f"c_o{k}")
        r[2].text_input("Agreed price", value=str(c.agreed_price or ""), key=f"c_ag{k}")
        r[3].text_input("Location", value=c.location, key=f"c_l{k}")
        r = st.columns(5)
        r[0].selectbox("Hull", COND, index=COND.index(c.hull_condition), key=f"c_hu{k}")
        r[1].selectbox("Mechanical (as stated)", COND, index=COND.index(c.mechanical_condition), key=f"c_me{k}")
        r[2].selectbox("Title", list(AQ.TITLE_STATUSES), index=AQ.TITLE_STATUSES.index(c.title_status), key=f"c_ti{k}")
        r[3].selectbox("Trailer", list(AQ.YES_NO), index=AQ.YES_NO.index(c.trailer_included), key=f"c_tr{k}")
        r[4].selectbox("Suits rentals", list(AQ.YES_NO), index=AQ.YES_NO.index(c.rental_suitable), key=f"c_rs{k}")
        st.checkbox("Title verified in person (name matches seller's ID, HIN matches hull)", value=c.title_verified,
                    key=f"c_tv{k}")
        r = st.columns(3)
        r[0].text_input("Estimated repairs", value=str(c.est_repairs if c.est_repairs is not None else ""), key=f"c_rp{k}",
                        help="Leave blank if unknown - enter 0 if you know there are none.")
        r[1].text_input("Transport", value=str(c.transport_cost if c.transport_cost is not None else ""), key=f"c_tc{k}")
        r[2].text_input("Prep (registration, gear, rental-ready)", value=str(c.prep_cost if c.prep_cost is not None else ""),
                        key=f"c_pc{k}")
        r = st.columns(2)
        r[0].text_input("What similar units really sell for (your research, optional)",
                        value=str(c.market_value if c.market_value is not None else ""), key=f"c_mv{k}")
        r[1].checkbox("I can pay cash and pick it up quickly", value=c.cash_ready, key=f"c_cash{k}")
        st.text_area("What the seller said (word for word helps)", value=c.seller_statements, key=f"c_ss{k}", height=70)
        st.text_input("Why it does / doesn't suit rentals", value=c.rental_note, key=f"c_rn{k}")
        st.form_submit_button("Save facts", type="primary", on_click=act, args=(lambda s, i=c.id: Q.update_candidate(
            s, i, today=day, title=val(f"c_t{k}"), source=val(f"c_src{k}", ""), url=val(f"c_url{k}", ""),
            manufacturer=val(f"c_mk{k}", ""), model=val(f"c_md{k}", ""), year=val(f"c_y{k}") or None,
            engine_hours=val(f"c_h{k}") or None, asking_price=val(f"c_p{k}", ""), offer=val(f"c_o{k}", ""),
            agreed_price=val(f"c_ag{k}", ""), location=val(f"c_l{k}", ""), hull_condition=val(f"c_hu{k}"),
            mechanical_condition=val(f"c_me{k}"), title_status=val(f"c_ti{k}"), trailer_included=val(f"c_tr{k}"),
            rental_suitable=val(f"c_rs{k}"), title_verified=val(f"c_tv{k}"), est_repairs=val(f"c_rp{k}", ""),
            transport_cost=val(f"c_tc{k}", ""), prep_cost=val(f"c_pc{k}", ""), seller_statements=val(f"c_ss{k}", ""),
            market_value=val(f"c_mv{k}", ""), cash_ready=val(f"c_cash{k}", False),
            rental_note=val(f"c_rn{k}", "")), "Facts saved"))
    with t2:
        st.caption(f"Pre-purchase protocol: **{AQ.inspection_state(c).replace('_', ' ')}**. ⭐ items must pass. "
                   "For anything that fails, add a fix-cost estimate - it feeds the offer. Mark 🚫 if it's a deal-breaker.")
        labels = {"": "—", "pass": "✅ pass", "fail": "🔧 fail - fixable", "dealbreaker": "🚫 walk away", "na": "n/a"}
        section = None
        for key, sec, text, crit in AQ.PROTOCOL:
            if sec != section:
                section = sec
                st.markdown(f"**{sec}**")
            cur = (c.inspection or {}).get(key, {})
            r = st.columns([3.6, 1.6, 1, 1.8])
            r[0].markdown(f"{'⭐ ' if crit else ''}{text}")
            opts = ["", "pass", "fail", "dealbreaker"] + ([] if crit else ["na"])
            r[1].selectbox("Result", opts, index=opts.index(cur.get("result", "")) if cur.get("result", "") in opts else 0,
                           key=f"in{k}{key}", label_visibility="collapsed", format_func=labels.get)
            r[2].text_input("Fix cost", value=cur.get("cost", ""), key=f"inc{k}{key}", label_visibility="collapsed",
                            placeholder="$ fix")
            r[3].text_input("Note", value=cur.get("note", ""), key=f"inn{k}{key}", label_visibility="collapsed",
                            placeholder="readings / notes")

        def _save_inspection(s, cid=c.id, prev=dict(c.inspection or {})):
            for key, _, _, _ in AQ.PROTOCOL:
                res, note, cost = val(f"in{k}{key}", ""), val(f"inn{k}{key}", ""), val(f"inc{k}{key}", "")
                old = prev.get(key, {})
                if (res, note, cost) != (old.get("result", ""), old.get("note", ""), old.get("cost", "")):
                    Q.set_inspection(s, cid, key, res, note, cost if res == "fail" else None)
        st.button("Save inspection", key=f"insv{k}", type="primary", on_click=act, args=(_save_inspection,
                                                                                         "Inspection saved"))
    with tn:
        negotiate_tab(c, k)
    with t3:
        for g in ev.gates:
            st.markdown(f"{'✅' if g.passed else '⬜'} **{g.name}** :gray[· {g.detail}]")
        funds = [f for f in M.funds_in(s, "business") + M.funds_in(s, "reserves") if f.kind == "save"]
        opts = [None] + [f.id for f in funds]
        r = st.columns([3, 1])
        r[0].selectbox("Paid from fund", opts, index=opts.index(c.funding_fund_id) if c.funding_fund_id in opts else 0,
                       key=f"cfund{k}", format_func=lambda i: "- choose -" if i is None else next(
                           f"{M.SPACES[f.space].split(' ')[0]} {f.name} ({M.fmt(M.balance(f))})" for f in funds if f.id == i))
        r[1].button("Set fund", key=f"cfb{k}", on_click=act, args=(lambda s, i=c.id: Q.set_funding(
            s, i, val(f"cfund{k}")), "Fund set"))
        if not M.funds_in(s, "business"):
            st.caption("Tip: add a **Ski purchases** fund on **💰 Money → 💼 Business** so buying never touches reserves.")
        if c.stage != "approved":
            st.text_input("Override reason (only if approving with gates still open)", key=f"cov{k}")
            st.button("Approve purchase", key=f"cap{k}", type="primary", on_click=act, args=(lambda s, i=c.id: Q.approve(
                s, i, day, override_reason=val(f"cov{k}", "")), "Approved ✅"))
        else:
            r = st.columns([1.5, 3, 1.3])
            r[0].text_input("Price paid", key=f"cbp{k}", placeholder="$")
            r[1].text_input("Note", key=f"cbn{k}", placeholder="e.g. Cash, picked up Sat, trailer included")
            r[2].button("Mark bought 🛥️", key=f"cbb{k}", type="primary", on_click=act, args=(lambda s, i=c.id: Q.mark_bought(
                s, i, val(f"cbp{k}", ""), day, val(f"cbn{k}", "")), "Welcome to the fleet 🛥️"))
        st.divider()
        r = st.columns([1.5, 3, 1])
        r[0].selectbox("Move to", ["contacted", "inspecting", "negotiating", "passed"], key=f"cst{k}")
        r[1].text_input("Reason (required to pass)", key=f"csr{k}")
        r[2].button("Update", key=f"csb{k}", on_click=act, args=(lambda s, i=c.id: Q.set_stage(
            s, i, val(f"cst{k}"), val(f"csr{k}", "")), "Stage updated"))
    with t4:
        st.caption(f"First seen {c.first_seen:%b} {c.first_seen.day} · last verified "
                   f"{c.last_verified:%b} {c.last_verified.day}" if c.last_verified else f"First seen {c.first_seen}")
        if c.price_history:
            st.dataframe([{"day": p.day, "price": f"${p.price:,.0f}", "note": p.note} for p in c.price_history],
                         hide_index=True, use_container_width=True)


def negotiate_tab(c, k: str) -> None:
    plan = Q.offer_plan(s, c, day)
    from lifeos.engines.negotiation import talk_track
    if plan.dealbreakers:
        st.error("🚫 **Walk away - don't negotiate.**  \n" + "  \n".join(f"• {d}" for d in plan.dealbreakers))
    elif plan.incomplete:
        st.warning("To work out your offers, first:  \n" + "  \n".join(f"• {d}" for d in plan.incomplete))
    elif plan.walk_away is not None and plan.great is None:
        st.warning("  \n".join(plan.notes))
    else:
        if plan.preliminary:
            st.info("**Preliminary numbers** - the inspection isn't done yet. Use them to decide if it's worth a "
                    "visit, but don't commit to a price until you've checked it in person.", icon="🔎")
        m = st.columns(3)
        m[0].metric("🔥 Great offer", f"${plan.great:,.0f}", help="Your opening number.")
        m[1].metric("🤝 Fair offer", f"${plan.fair:,.0f}", help="Where you'd like to land.")
        m[2].metric("🚫 Walk-away", f"${plan.walk_away:,.0f}", help="The most you'll pay. Don't say this out loud.")
        for n in plan.notes:
            st.caption(f"ℹ️ {n}")
        with st.expander("How these numbers were worked out"):
            st.markdown(f"**Starting point:** ${plan.reference:,.0f} - {plan.reference_basis}.")
            if plan.deductions:
                st.markdown("**Minus fixes from your inspection:**  \n" +
                            "  \n".join(f"• {t}: −${d:,.0f}" for t, d in plan.deductions))
            st.markdown(f"**What it's worth to you:** ${plan.value_to_you:,.0f}")
            st.markdown("**Walk-away = the lowest of:**  \n" +
                        "  \n".join(f"• {label}: ${v:,.0f}" for label, v in plan.caps))
            sig = "  \n".join(f"• {label}: +{pct * 100:.0f}%" for label, pct in plan.signals) or "• none"
            st.markdown(f"**Negotiating margin:** {plan.margin * 100:.0f}% (8% base + leverage, max 20%)  \n{sig}")
            st.caption("Great offer = walk-away minus the full margin · fair offer = minus half · rounded down to $25.")
        script = talk_track(c, plan)
        st.markdown("**What to say**")
        st.text_area("Talk track", value=script, key=f"tt{k}", height=230, label_visibility="collapsed")
        st.button("📨 Use as tomorrow's ask", key=f"ask{k}", on_click=act, args=(lambda s, i=c.id: DC_set_ask(
            s, c, plan, val(f"tt{k}", script)), "Saved as tomorrow's ask - it's on Today's evening check"))
    st.divider()
    st.markdown("**Offer log**")
    for e in Q.negotiation_log(s, c):
        st.caption(f"{e.at:%b %d}: {e.from_value} → {e.to_value}" + (f" · {e.reason}" if e.reason else ""))
    r = st.columns([1.2, 1.4, 1.2, 2.4, 1])
    r[0].text_input("Offered", key=f"of{k}", placeholder="$")
    r[1].selectbox("Their answer", list(Q.RESPONSES), key=f"or{k}",
                   format_func=lambda x: {"accepted": "✅ accepted", "countered": "↩️ countered", "declined": "✗ declined",
                                          "no_reply": "… no reply"}[x])
    r[2].text_input("Counter", key=f"oc{k}", placeholder="$")
    r[3].text_input("Note", key=f"on{k}", placeholder="what they said")
    r[4].button("Record", key=f"orb{k}", on_click=act, args=(lambda s, i=c.id: Q.record_offer(
        s, i, val(f"of{k}", ""), val(f"or{k}"), val(f"oc{k}", ""), val(f"on{k}", "")), "Offer recorded"))


def DC_set_ask(s_, c, plan, script: str):
    return D.set_ask(s_, for_date=day + dt.timedelta(days=1), today=day, who=f"Seller · {c.title}", area="price",
                     what=f"${plan.great:,.0f} (fallback ${plan.fair:,.0f}; walk away above ${plan.walk_away:,.0f})",
                     script=script)


def page_deals() -> None:
    st.title("🛥️ Deals")
    st.caption("Find, check and compare used equipment before you buy. Facts you don't know stay unknown, and a "
               "seller saying 'it runs' is never treated as proof.")
    cfg = Q.settings(s)
    owned = Q.fleet(s)
    evs = Q.ranked(s, day)
    c = st.columns(4)
    c[0].metric("Fleet", f"{len(owned)}/{cfg['fleet_target']}" if cfg["fleet_target"] else str(len(owned)))
    c[1].metric("Candidates", len(evs))
    c[2].metric("Ready to approve", sum(1 for e in evs if e.ready_for_approval and e.candidate.stage != "approved"))
    c[3].metric("Price ceiling", _dollars(cfg["max_price"]) if cfg["max_price"] else "not set")
    with st.expander("⚙️ Deal settings", expanded=cfg["max_price"] is None):
        st.caption("Stored only in your local database.")
        r = st.columns(3)
        r[0].text_input("Max purchase price per unit", value=str(cfg["max_price"] or ""), key="dq_max", placeholder="$")
        r[1].number_input("Fleet target", 0, 100, value=cfg["fleet_target"] or 0, key="dq_fleet")
        r[2].number_input("'High hours' warning above", 0, 5000, value=cfg["high_hours"], key="dq_hours")
        st.button("Save settings", key="dq_save", on_click=act, args=(lambda s: Q.save_settings(
            s, max_price=val("dq_max", ""), fleet_target=val("dq_fleet") or None, high_hours=val("dq_hours")), "Saved"))
    SS.setdefault("dq_view", "Best candidates")
    view = st.segmented_control("View", ["Best candidates", "Compare", "Fleet", "Passed"], key="dq_view",
                                label_visibility="collapsed") or "Best candidates"
    if view == "Best candidates":
        if not evs:
            st.caption("No candidates yet - add a listing below.")
        for i, ev in enumerate(evs, 1):
            with st.container(border=True):
                candidate_summary(ev, i)
                with st.expander("Open"):
                    candidate_detail(ev)
    elif view == "Compare":
        if evs:
            st.dataframe([{
                "rank": i, "listing": e.candidate.title, "stage": e.candidate.stage,
                "price": _dollars(AQ.working_price(e.candidate)), "year": str(e.candidate.year or "?"),
                "hours": str(e.candidate.engine_hours) if e.candidate.engine_hours is not None else "?",
                "ready to rent": e.cost.label, "gates": f"{e.gates_passed}/7",
                "risks": sum(f.level == "risk" for f in e.flags), "cautions": sum(f.level == "caution" for f in e.flags),
                "facts known": f"{e.facts_known}/{e.facts_total}", "next step": e.next_step,
            } for i, e in enumerate(evs, 1)], hide_index=True, use_container_width=True)
            st.caption("Ranked by red-flag risks first (fewest wins), then gates passed, then lowest ready-to-rent cost.")
        else:
            st.caption("Nothing to compare yet.")
    elif view == "Fleet":
        for u in owned:
            paid = f" · paid {_dollars(u.agreed_price)}" if u.agreed_price else ""
            st.markdown(f"🛥️ **{u.title}**{paid}")
        if not owned:
            st.caption("No units recorded - add the skis you already own so the count is right.")
        with st.form("own_add", clear_on_submit=True):
            r = st.columns([3, 1])
            r[0].text_input("Ski you already own", key="own_t", placeholder="e.g. 2017 Yamaha VX - unit 1")
            r[1].form_submit_button("Add to fleet", on_click=act, args=(lambda s: Q.add_owned(
                s, title=val("own_t", ""), today=day), "Added to fleet"))
    else:
        from lifeos.models import AcquisitionCandidate as _AC
        passed = list(s.scalars(select(_AC).where(_AC.stage == "passed")))
        for p_ in passed:
            st.markdown(f"— {p_.title} :gray[· asking {_dollars(p_.asking_price)}]")
        if not passed:
            st.caption("Nothing passed on yet.")
    with st.expander("➕ Add a listing"):
        with st.form("cand_add", clear_on_submit=True):
            r = st.columns([3, 1.2, 1.2])
            r[0].text_input("Listing", key="na_t", placeholder="e.g. 2019 Yamaha VX Cruiser - Kingston")
            r[1].text_input("Asking price", key="na_p", placeholder="$")
            r[2].text_input("Source", key="na_s", placeholder="Marketplace")
            r = st.columns([3, 1, 1])
            r[0].text_input("Link", key="na_u")
            r[1].text_input("Model", key="na_m", placeholder="VX Cruiser")
            r[2].number_input("Year (0 = unknown)", 0, 2100, value=0, key="na_y")
            st.text_area("What the seller said", key="na_ss", height=60)
            st.form_submit_button("Add listing", type="primary", on_click=act, args=(lambda s: Q.add_candidate(
                s, today=day, title=val("na_t", ""), asking_price=val("na_p", ""), source=val("na_s", ""),
                url=val("na_u", ""), model=val("na_m", ""), year=val("na_y") or None,
                seller_statements=val("na_ss", "")), "Listing added"))


# ================================================================== University
SRC_ICON = {"paper": "📄", "book": "📕", "guide": "🧭", "course": "🎬", "standard": "📐"}
LEVEL_TEXT = {"none": "not started", "introduced": "introduced", "practiced": "practiced", "recalled": "recalled",
              "applied": "applied", "mastered": "mastered"}


def source_line(key: str, checked: set, k: str) -> None:
    sr = UNI.source(key)
    cite = sr["title"]
    if sr.get("authors"):
        cite = f"{sr['authors']}{' (' + str(sr['year']) + ')' if sr.get('year') else ''}. *{sr['title']}*"
    if sr.get("venue"):
        cite += f". {sr['venue']}"
    link = f" · [open]({sr['url']})" if sr.get("url") else (" · find it on Google Scholar" if sr["type"] == "paper" else "")
    r = st.columns([0.9, 0.1])
    r[0].markdown(f"{SRC_ICON.get(sr['type'], '•')} {cite}{link}  \n:gray[{sr['level']} · {sr['why']}]")
    if key in checked:
        r[1].caption("✔ checked")
    else:
        r[1].button("✔", key=f"chk{k}{key}", help="I opened it and it's what it says", on_click=act,
                    args=(lambda s, kk=key: UNI.check_source(s, kk, day), "Marked as checked"))


def unit_panel(u: dict, checked: set) -> None:
    key = u["key"]
    k = key.replace("-", "")
    lvl = UNI.unit_level(s, key)
    prog = s.get(UnitProgressModel, key)
    if not prog or not prog.started_on:
        st.button("Start this unit", key=f"us{k}", type="primary", on_click=act, args=(
            lambda s: UNI.start_unit(s, key, day), "Unit started - its flashcards join your reviews"))
    t1, t2, t3, t4, t5, t6 = st.tabs(["📖 Learn", "🧠 Recall", "✅ Test", "🔨 Apply", "🃏 Cards", "📝 Notes"])
    with t1:
        st.markdown("**You'll be able to:**  \n" + "  \n".join(f"• {o}" for o in u["objectives"]))
        st.markdown(u["learn"])
        st.markdown("**Sources**")
        for sk in u["sources"]:
            source_line(sk, checked, k)
    with t2:
        st.caption("Close the sources. Write everything you remember in your own words - then check it against the key points.")
        st.text_area("What I remember", value=prog.recall_text if prog else "", key=f"rc{k}", height=120)
        st.markdown("**Key points - tick the ones you actually got:**")
        for i, kp in enumerate(u["key_points"]):
            st.checkbox(kp, value=bool(prog and kp in (prog.recall_hits or [])), key=f"rk{k}{i}")
        st.button("Save recall", key=f"rcs{k}", on_click=act, args=(lambda s: UNI.save_recall(
            s, key, val(f"rc{k}", ""), [kp for i, kp in enumerate(u["key_points"]) if val(f"rk{k}{i}")], day),
            "Recall saved"))
        if prog and prog.recall_on:
            st.caption(f"Last recall {prog.recall_on:%b %d}: {len(prog.recall_hits or [])}/{len(u['key_points'])} key points.")
    with t3:
        for qi, q in enumerate(u.get("quiz", [])):
            st.markdown(f"**Q{qi + 1}.** {q['q']}")
            if q.get("type") == "short":
                st.text_area("Your answer", key=f"qa{k}{qi}", height=80, label_visibility="collapsed")
                with st.expander("Check against the key points"):
                    for j, kp in enumerate(q["key_points"]):
                        st.checkbox(kp, key=f"qk{k}{qi}{j}")
                st.button("Submit", key=f"qs{k}{qi}", on_click=act, args=(lambda s, qi=qi, q=q: UNI.answer_quiz(
                    s, key, qi, day, text=val(f"qa{k}{qi}", ""),
                    hits=[kp for j, kp in enumerate(q["key_points"]) if val(f"qk{k}{qi}{j}")]), "Answer recorded"))
            else:
                st.radio("Answer", list(range(len(q["choices"]))), key=f"qr{k}{qi}", index=None,
                         format_func=lambda i, q=q: q["choices"][i], label_visibility="collapsed")
                st.button("Submit", key=f"qs{k}{qi}", on_click=quiz_submit, args=(key, qi, q, f"qr{k}{qi}"))
            fb = SS.get(f"fb{key}{qi}")
            if fb:
                (st.success if fb[0] else st.error)(fb[1])
    with t4:
        st.markdown(f"**Assignment:** {u['apply']}")
        st.markdown(f"**Done when:** {u.get('deliverable', '')}")
        if u.get("control"):
            st.caption(f"🛡️ Control it builds: {u['control']}")
        st.text_area("Evidence - what you did and where the proof is", value=prog.evidence if prog else "",
                     key=f"ev{k}", height=90)
        if u["mode"] == "studio":
            st.checkbox("It runs - tests pass / I used it for real", value=bool(prog and prog.evidence_runs), key=f"evr{k}")
        st.button("Save evidence", key=f"evs{k}", type="primary", on_click=act, args=(lambda s: UNI.save_evidence(
            s, key, val(f"ev{k}", ""), bool(val(f"evr{k}", u["mode"] != "studio")), day), "Evidence saved"))
    with t5:
        for c in UNI.cards_for(key):
            st.markdown(f"**{c['term']}** {'🔤' if c['kind'] == 'acronym' else ''} — {c['definition']}")
    with t6:
        st.text_area("Today's note on this unit", key=f"un{k}", height=90, label_visibility="collapsed",
                     placeholder="What clicked, what confused you, where you'd use it. #tags work.")
        st.button("Save note", key=f"uns{k}", on_click=act, args=(lambda s: NT.add_note(
            s, day, val(f"un{k}", ""), unit_key=key), "Note saved"))
        for n in NT.find(s, unit_keys=[key]):
            note_card(n, f"u{k}")


def note_label(n) -> str:
    bits = [f"{n.day:%a %b} {n.day.day}"]
    if n.unit_key:
        bits.append(f"{n.unit_key} · {UNI.unit(n.unit_key)['title'][:48]}")
    elif n.course_code:
        bits.append(n.course_code)
    if n.role_id:
        r = s.get(JB.JobRole, n.role_id)
        if r:
            bits.append(f"💼 {r.title} @ {r.company}")
    return " · ".join(bits)


def note_card(n, prefix: str) -> None:
    """One note, with edit and delete tucked into a popover."""
    with st.container(border=True):
        head = st.columns([6, 1])
        head[0].caption(note_label(n) + (" · " + " ".join(f"`#{x}`" for x in n.tags) if n.tags else ""))
        if n.title:
            head[0].markdown(f"**{n.title}**")
        head[0].markdown(n.body)
        with head[1].popover("✏️", use_container_width=True):
            st.text_area("Edit", value=n.body, key=f"ne{prefix}{n.id}", height=140)
            st.button("Save", key=f"nes{prefix}{n.id}", on_click=act, args=(
                lambda s, i=n.id: NT.edit_note(s, i, val(f"ne{prefix}{i}", "")), "Note updated"))
            st.button("Delete", key=f"ned{prefix}{n.id}", on_click=act, args=(
                lambda s, i=n.id: NT.delete_note(s, i), "Note deleted"))


def notes_view() -> None:
    """The University journal: today's notes first, then everything, searchable."""
    c = st.columns([3, 1])
    with c[0]:
        st.markdown(f"**📝 Notes for {day:%A, %b} {day.day}**")
        st.text_input("Title (optional)", key="nt_title", placeholder="e.g. DPA sub-processor terms")
        st.text_area("Notes", key="nt_body", height=160, label_visibility="collapsed",
                     placeholder="What you learned today, in your own words. Questions, examples, where you'd use it. "
                                 "#tags make it findable.")
        active = UNI.active_courses(day)
        cc = st.columns(3)
        course = cc[0].selectbox("Course", [None] + [co["code"] for co in UNI.courses()], key="nt_course",
                                 index=None, placeholder="Course (optional)",
                                 format_func=lambda x: "-" if x is None else f"{x} · {UNI.course(x)['title']}")
        units = UNI.units(course) if course else []
        cc[1].selectbox("Unit", [None] + [u["key"] for u in units], key="nt_unit", index=None,
                        placeholder="Unit (optional)", disabled=not units,
                        format_func=lambda x: "-" if x is None else f"{x} · {UNI.unit(x)['title'][:40]}")
        open_roles = JB.roles(s)
        cc[2].selectbox("Useful for a role", [None] + [r.id for r in open_roles], key="nt_role", index=None,
                        placeholder="Job role (optional)",
                        format_func=lambda i: "-" if i is None else next(f"💼 {r.title} @ {r.company}"
                                                                         for r in open_roles if r.id == i))
        st.button("Save note", type="primary", key="nt_save", on_click=act, args=(lambda s: NT.add_note(
            s, day, val("nt_body", ""), title=val("nt_title", ""), course_code=val("nt_course"),
            unit_key=val("nt_unit"), role_id=val("nt_role")), "Note saved"))
        if active and not course:
            st.caption("Tip: pick the course and unit - the note then shows up inside that unit too, and on any job "
                       "role linked to it.")
    with c[1]:
        st.metric("Days in a row", NT.streak(s, day))
        st.metric("Notes today", len(NT.find(s, day=day)))
        tags = NT.all_tags(s)
        if tags:
            st.caption("Tags: " + " · ".join(f"#{t} ({n})" for t, n in tags[:15]))
    st.divider()
    f = st.columns([2, 1, 1])
    q = f[0].text_input("Search notes", key="nt_q", placeholder="Search notes", label_visibility="collapsed")
    tag = f[1].selectbox("Tag", [None] + [t for t, _ in NT.all_tags(s)], key="nt_tag", index=None,
                         placeholder="Any tag", label_visibility="collapsed")
    only = f[2].selectbox("Course filter", [None] + [co["code"] for co in UNI.courses()], key="nt_fc", index=None,
                          placeholder="Any course", label_visibility="collapsed")
    rows = NT.find(s, query=q or "", tag=tag, course_code=only)
    if not rows:
        st.caption("No notes yet." if not (q or tag or only) else "No notes match.")
    last = None
    for n in rows[:100]:
        if n.day != last:
            st.markdown(f"##### {n.day:%A, %b} {n.day.day}" + (" · today" if n.day == day else ""))
            last = n.day
        note_card(n, "j")


def quiz_submit(unit_key: str, qi: int, q: dict, widget: str) -> None:
    choice = val(widget)
    try:
        with unit_of_work(_factory()) as s_:
            ok = UNI.answer_quiz(s_, unit_key, qi, day, choice=choice)
        SS[f"fb{unit_key}{qi}"] = (ok, ("✅ Correct. " if ok else f"❌ Not quite - answer: {q['choices'][q['answer']]}. ")
                                    + q["why"])
    except Exception as exc:  # validation errors shown inline
        SS[f"fb{unit_key}{qi}"] = (False, str(exc))


def review_session() -> None:
    minutes, low = UNI.review_budget(s, day)
    queue = UNI.due_cards(s, day, minutes, low)
    all_due = len(UNI.due_cards(s, day))
    if not queue:
        st.success("No cards due. Start a unit to add its cards, or come back tomorrow.")
        return
    st.caption(f"{len(queue)} card(s) today (~{max(1, len(queue) * LRN.SECONDS_PER_CARD // 60)} min)"
               + (f" · {all_due - len(queue)} more waiting - tight day, so only the most overdue" if all_due > len(queue) else ""))
    cur = queue[0]
    c = UNI.card(cur.card_key)
    with st.container(border=True):
        st.caption(f"{UNI.course(c['course'])['title']} · box {cur.box} · {'acronym' if c['kind'] == 'acronym' else 'definition'}")
        st.header(c["term"], anchor=False)
        if SS.get("uni_reveal") == cur.card_key:
            st.subheader(c["definition"], anchor=False)
            r = st.columns(2)
            r[0].button("✅ Knew it", key="rv_ok", type="primary", use_container_width=True, on_click=card_answer,
                        args=(cur.card_key, True))
            r[1].button("❌ Missed it", key="rv_no", use_container_width=True, on_click=card_answer,
                        args=(cur.card_key, False))
        else:
            st.caption("Say the answer out loud first.")
            st.button("Show answer", key="rv_show", on_click=lambda ck=cur.card_key: SS.__setitem__("uni_reveal", ck))


def card_answer(card_key: str, correct: bool) -> None:
    act(lambda s_: UNI.review_card(s_, card_key, correct, day), "✓" if correct else "Back to box 0 - see it tomorrow")
    SS.pop("uni_reveal", None)


def page_university() -> None:
    st.title("🎓 The Personal University")
    sem = UNI.current_semester(day)
    checked = UNI.checked_sources(s)
    minutes, low = UNI.review_budget(s, day)
    due_today = len(UNI.due_cards(s, day, minutes, low))
    c = st.columns(4)
    if sem:
        started = sem["start"] <= day
        c[0].metric(sem["code"], f"{max(0, (sem['end'] - day).days) // 7} wks left" if started else
                    f"starts {sem['start']:%b} {sem['start'].day}")
    c[1].metric("Cards due today", due_today)
    s1 = UNI.active_courses(day)
    reports = [UNI.course_report(s, co["code"], day) for co in s1]
    total_units = sum(len(r.levels) for r in reports)
    c[2].metric("Units applied", f"{sum(r.applied for r in reports)}/{total_units}" if total_units else "-")
    c[3].metric("Units mastered", f"{sum(r.mastered for r in reports)}/{total_units}" if total_units else "-")
    SS.setdefault("uni_view", "This semester")
    view = st.segmented_control("View", ["This semester", "Review", "Notes", "Catalog", "Library", "History", "Exams",
                                         "Hands-on", "Report card"], key="uni_view", label_visibility="collapsed") \
        or "This semester"
    if view == "This semester":
        if not s1:
            st.caption("No semester planned.")
            return
        if sem:
            st.caption(f"**{sem['name']}** · {sem['start']:%b %d} – {sem['end']:%b %d, %Y} · about {sem['weekly_hours']} "
                       f"hrs/week + {sem['daily_review_minutes']} min of cards a day. Rule of One: a hard skill, a human "
                       "skill, an anchor. Career sprints run alongside, for a set number of weeks.")
        for co, rep in zip(s1, reports):
            dept = next(d for d in UNI.departments() if d["code"] == co["dept"])
            with st.container(border=True):
                st.markdown(f"### {dept['icon']} {co['code']} · {co['title']}")
                if co.get("sprint"):
                    sp = co["sprint"]
                    st.markdown(f":orange-badge[🏁 Career sprint · {sp['start']:%b} {sp['start'].day} – "
                                f"{sp['end']:%b} {sp['end'].day}"
                                + (f" · {max(0, (sp['end'] - day).days)} days left]" if sp["start"] <= day else
                                   f" · starts in {(sp['start'] - day).days} days]"))
                st.caption(f"{co['role']} · {co['kind']} · ~{co['hours']} h/week · {co['outcome']}")
                st.markdown(" ".join(f"{LRN.LEVEL_ICON[lv]}" for lv in rep.levels.values()) +
                            f" :gray[· applied {rep.applied}/{len(rep.levels)} · mastered {rep.mastered}]")
                st.caption("⚪ not started · 🔹 introduced · 🔸 practiced · 🟡 recalled (7+ days later) · 🟢 applied · "
                           "🏅 mastered (applied + reviews 3+ weeks apart)")
                for u in UNI.units(co["code"]):
                    lv = rep.levels[u["key"]]
                    with st.expander(f"{LRN.LEVEL_ICON[lv]} Week{'s' if '-' in str(u['weeks']) else ''} {u['weeks']} · {u['title']} · {LEVEL_TEXT[lv]}"):
                        unit_panel(u, checked)
    elif view == "Review":
        review_session()
    elif view == "Notes":
        notes_view()
    elif view == "Catalog":
        for d in UNI.departments():
            cs = UNI.courses(dept=d["code"])
            with st.expander(f"{d['icon']} {d['name']} · {len(cs)} course(s)"):
                st.caption(d["outcome"])
                for co in cs:
                    when = (f"semester {co['semester']}" if co.get("semester") else
                            f"career sprint {co['sprint']['start']:%b} {co['sprint']['start'].day} – "
                            f"{co['sprint']['end']:%b} {co['sprint']['end'].day}" if co.get("sprint") else "not scheduled yet")
                    st.markdown(f"**{co['code']} · {co['title']}** :gray[· {co['kind']} · {when}]  \n{co['outcome']}")
                if d["code"] == "HIS":
                    st.caption("History has no limit - open inquiries anytime under **History**.")
    elif view == "Library":
        types = st.multiselect("Type", list(SRC_ICON), default=list(SRC_ICON), format_func=lambda x: f"{SRC_ICON[x]} {x}")
        st.caption("Papers are cited, not linked - find them on Google Scholar or through a library. Tick ✔ once you've "
                   "opened a source and confirmed it's what it says.")
        for sk, sr in UNI.content()["sources"].items():
            if sr["type"] in types:
                source_line(sk, checked, "lib")
        st.divider()
        st.markdown("**📚 Reading log** - three takeaways and one real action per book")
        from lifeos.models import ReadingEntry
        entries = list(s.scalars(select(ReadingEntry).order_by(ReadingEntry.id.desc())))
        for e in entries:
            if e.status == "done":
                st.markdown(f"✅ **{e.title}** :gray[· finished {e.finished_on:%b %d}]  \n" +
                            "  \n".join(f"• {x}" for x in e.takeaways) + f"  \n→ *{e.action}*")
            else:
                with st.expander(f"📖 Reading: {e.title}"):
                    for i in range(3):
                        st.text_input(f"Takeaway {i + 1}", key=f"tk{e.id}{i}")
                    st.text_input("One real action because of it", key=f"ta{e.id}")
                    st.button("Finish", key=f"tf{e.id}", on_click=act, args=(lambda s, i=e.id: UNI.finish_reading(
                        s, i, [val(f"tk{i}{j}", "") for j in range(3)], val(f"ta{i}", ""), day), "Logged 📚"))
        books = [k_ for k_, v in UNI.content()["sources"].items() if v["type"] in ("book", "paper")]
        r = st.columns([2, 2, 1])
        r[0].selectbox("From the library", [None] + books, key="rl_src",
                       format_func=lambda x: "- or type a title -" if x is None else UNI.source(x)["title"])
        r[1].text_input("Title", key="rl_t")
        r[2].button("Start reading", key="rl_b", on_click=act, args=(lambda s: UNI.start_reading(
            s, val("rl_t", ""), day, source_key=val("rl_src")), "Added to your reading log"))
    elif view == "History":
        from lifeos.models import Inquiry
        UNI.ensure_inquiries(s, day)
        s.commit()
        st.caption("No limit. Method: a question → at least two independent sources → your own conclusion → what would "
                   "change your mind.")
        seeds = {q["key"]: q for q in UNI.content()["inquiries"]}
        for i in s.scalars(select(Inquiry).order_by(Inquiry.status, Inquiry.id)):
            icon = "✅" if i.status == "concluded" else "🔎"
            with st.expander(f"{icon} {i.topic}: {i.question}"):
                seed = seeds.get(i.seed_key or "")
                if seed:
                    for sk in seed["sources"]:
                        source_line(sk, checked, f"h{i.id}")
                    if seed.get("note"):
                        st.caption(seed["note"])
                if i.status == "concluded":
                    st.markdown(f"**Sources:** {i.sources_used}  \n**My conclusion:** {i.conclusion}  \n"
                                f"**Would change my mind:** {i.would_change_mind}")
                else:
                    st.text_area("Sources used (one per line)", key=f"hs{i.id}", height=70)
                    st.text_area("My conclusion - my own argument", key=f"hc{i.id}", height=100)
                    st.text_input("What evidence would change my mind?", key=f"hw{i.id}")
                    st.button("Conclude", key=f"hb{i.id}", on_click=act, args=(lambda s, ii=i.id: UNI.conclude_inquiry(
                        s, ii, sources_used=val(f"hs{ii}", ""), conclusion=val(f"hc{ii}", ""),
                        would_change_mind=val(f"hw{ii}", ""), today=day), "Inquiry concluded"))
        with st.form("hadd", clear_on_submit=True):
            r = st.columns([1, 3])
            r[0].text_input("Topic", key="ha_t", placeholder="e.g. Haiti")
            r[1].text_input("Question", key="ha_q", placeholder="e.g. What did the US occupation of 1915-1934 change?")
            st.form_submit_button("Add inquiry", on_click=act, args=(lambda s: UNI.add_inquiry(
                s, val("ha_t", ""), val("ha_q", ""), day), "Inquiry added"))
    elif view == "Exams":
        from lifeos.models import ExamPlan as _EP
        for ex in UNI.exams():
            plan = s.get(_EP, ex["code"])
            hist = UNI.practice_history(s, ex["code"])
            when = (f"test {plan.test_date:%b %d, %Y} · {(plan.test_date - day).days} days" if plan and plan.test_date
                    else f"target {ex['target'] or 'not scheduled'}")
            with st.expander(f"{'⭐' if not ex.get('optional') else '◽'} {ex['name']} · {when}"):
                st.caption(f"{ex['provider']} · {ex['why']} · [official info]({ex['url']})")
                r = st.columns([1.4, 1.4, 1])
                r[0].date_input("Test date", value=(plan.test_date if plan and plan.test_date else day + dt.timedelta(days=120)),
                                key=f"ed{ex['code']}")
                r[1].text_input("Target score", value=plan.target_score if plan else "", key=f"et{ex['code']}")
                r[2].button("Save", key=f"eb{ex['code']}", on_click=act, args=(lambda s, cc=ex["code"]: UNI.set_exam(
                    s, cc, val(f"ed{cc}"), val(f"et{cc}", ""), day), "Exam plan saved"))
                r = st.columns([1, 1, 2, 1])
                r[0].text_input("Practice score", key=f"ps{ex['code']}")
                r[1].text_input("Out of", key=f"pm{ex['code']}")
                r[2].text_input("Note", key=f"pn{ex['code']}", placeholder="which test, weak areas")
                r[3].button("Log", key=f"pb{ex['code']}", on_click=act, args=(lambda s, cc=ex["code"]: UNI.log_practice(
                    s, cc, val(f"ps{cc}", ""), val(f"pm{cc}", ""), day, val(f"pn{cc}", "")), "Practice score logged"))
                if hist:
                    st.dataframe([{"day": h.day, "score": f"{h.score:g}" + (f" / {h.max_score:g}" if h.max_score else ""),
                                   "note": h.note} for h in hist], hide_index=True, use_container_width=True)
    elif view == "Hands-on":
        from lifeos.models import HandsOnRun as _HR
        for key, cl in UNI.content()["checklists"].items():
            runs = list(s.scalars(select(_HR).where(_HR.checklist_key == key)))
            with st.expander(f"🔧 {cl['title']}" + (f" · done {len(runs)}×" if runs else "")):
                st.warning(cl["safety"], icon="⚠️")
                for sk in cl["sources"]:
                    source_line(sk, checked, f"ho{key}")
                for i, step in enumerate(cl["steps"]):
                    st.checkbox(step, key=f"hs{key}{i}")
                st.text_input("Evidence (note / where the before-after photos are)", key=f"he{key}")
                st.button("Log as done", key=f"hl{key}", on_click=act, args=(lambda s, kk=key, n=len(cl["steps"]): UNI.log_hands_on(
                    s, kk, [i for i in range(n) if val(f"hs{kk}{i}")], val(f"he{kk}", ""), day), "Logged 🔧"))
    else:
        st.caption("Grades reflect demonstrated knowledge and real evidence - not hours spent.")
        for co, rep in zip(s1, reports):
            st.markdown(f"**{co['code']} · {co['title']}**")
            r = st.columns(4)
            r[0].metric("Applied", f"{rep.applied}/{len(rep.levels)}")
            r[1].metric("Mastered", f"{rep.mastered}/{len(rep.levels)}")
            r[2].metric("Quiz accuracy", f"{rep.quiz_correct}/{rep.quiz_total}" if rep.quiz_total else "-")
            r[3].metric("Card recall (30 days)", f"{rep.reviews_30d_correct}/{rep.reviews_30d}" if rep.reviews_30d else "-")
            st.caption(" · ".join(f"{LRN.LEVEL_ICON[v]} {UNI.unit(k_)['title'][:38]}" for k_, v in rep.levels.items()))


# ================================================================== Planner
PL_VIEWS = ["Agenda", "Week", "Month", "Weekly review"]


def _pl_anchor() -> dt.date:
    return SS.get("pl_anchor") or day


def _pl_view() -> str:
    return SS.get("pl_view") or "Agenda"


def pl_move(direction: int) -> None:
    a, v = _pl_anchor(), _pl_view()
    SS["pl_anchor"] = P.shift_month(a, direction) if v == "Month" else a + dt.timedelta(days=7 * direction)


def pl_today() -> None:
    SS["pl_anchor"] = day


def pl_open_day(d: dt.date) -> None:
    SS["pl_anchor"], SS["pl_view"] = d, "Agenda"


def go_weekly_review(week: dt.date) -> None:
    SS["nav"], SS["pl_view"], SS["pl_anchor"] = "Planner", "Weekly review", week


def _short(text: str, n: int = 34) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _day_label(d: dt.date) -> tuple[str, str]:
    if d == day:
        return "Today", f"{d:%a %B} {d.day}"
    if d == day + dt.timedelta(days=1):
        return "Tomorrow", f"{d:%a %B} {d.day}"
    if d == day - dt.timedelta(days=1):
        return "Yesterday", f"{d:%a %B} {d.day}"
    return f"{d:%a}", f"{d:%B} {d.day}"


def agenda_day(a) -> None:
    head, sub = _day_label(a.day)
    st.markdown(f"#### {head} &nbsp;:gray[{sub}]")
    if a.empty:
        st.caption("Nothing planned today" if a.day == day else "Nothing planned")
        return
    for b in a.carry_in:
        end = b.end.strftime("%-I:%M %p").replace(":00 ", " ").lower()
        st.markdown(f"{P.KINDS.get(b.kind, '•')} **until {end}** &nbsp; {b.title} :gray[· from last night]")
    for b in a.blocks:
        where = f" · {b.location}" if b.location else ""
        st.markdown(f"{P.KINDS.get(b.kind, '•')} **{b.time_label}** &nbsp; {b.title}:gray[{where}]")
    for dl in a.deadlines:
        st.markdown(f"🎯 **Deadline** &nbsp; {dl.title} :gray[· {TIER_LABELS[dl.tier]}]")
    for tk in a.tasks:
        if tk.protected:  # sleep, shifts: they're commitments, not to-dos
            continue
        done = tk.status == S.COMPLETED
        cols = st.columns([0.05, 0.85, 0.10])
        if done:
            cols[0].markdown("✅")
            cols[1].markdown(f"~~{tk.title}~~ :gray[· P{tk.priority}]")
            continue
        with cols[2].popover("✏️", help="Edit"):
            edit_form(tk, f"ag{a.day}{tk.id}")
        cols[0].button("○", key=f"ag{a.day}{tk.id}", help="Mark done", on_click=act, args=(
            lambda s, i=tk.id: T.transition(s, i, S.COMPLETED, today=day), "Completed ✓"))
        late = " · :red[overdue]" if tk.due_date and tk.due_date < a.day else ""
        due = f" · due {tk.due_date:%b %d}" if tk.due_date and not late else ""
        cols[1].markdown(f"{ICON.get(tk.pillar_code, '')} {tk.title} :gray[· P{tk.priority}{due}]{late}"
                         + (" ↻" if tk.is_recurring else ""))
    extras = []
    if a.routines:
        extras.append("↻ " + " · ".join(a.routines))
    if a.booked_minutes:
        extras.append(f"⏱️ {a.booked_minutes / 60:.1f} h on the calendar")
    if extras:
        st.caption("  \n".join(extras))


def planner_agenda() -> None:
    for a in P.agenda(s, _pl_anchor(), get_settings().planner.agenda_days, today=day):
        agenda_day(a)
        st.divider()


def planner_week() -> None:
    start = P.week_start(_pl_anchor())
    days = P.agenda(s, start, 7, today=day)
    cols = st.columns(7)
    for col, a in zip(cols, days):
        with col:
            mark = "🔵 " if a.day == day else ""
            st.markdown(f"**{mark}{a.day:%a} {a.day.day}**")
            if a.empty:
                st.caption("—")
            for b in a.blocks:
                st.caption(f"{P.KINDS.get(b.kind, '•')} {b.time_label if b.start else ''} {_short(b.title, 22)}")
            for dl in a.deadlines:
                st.caption(f"🎯 {_short(dl.title, 24)}")
            for tk in a.tasks:
                if tk.protected:
                    continue
                box = "✅" if tk.status == S.COMPLETED else ("🔴" if tk.priority == 1 else "○")
                st.caption(f"{box} {_short(tk.title, 24)}")
            if a.routines:
                st.caption(f"↻ {len(a.routines)} routine{'s' if len(a.routines) != 1 else ''}")
            if a.booked_minutes:
                st.caption(f"⏱️ {a.booked_minutes / 60:.1f}h")
            st.button("Open", key=f"wk{a.day}", on_click=pl_open_day, args=(a.day,))

    st.subheader("🗂️ Plan the week")
    pile = P.unscheduled_tasks(s)
    if not pile:
        st.caption("Every open task already has a day. 🙌")
        return
    st.caption("Tasks with no day yet. Give each one a day - planning ahead never counts as a slip.")
    week_days = [d for d in (start + dt.timedelta(days=i) for i in range(7)) if d >= day] or [day]
    for tk in pile[:15]:
        c = st.columns([5, 2, 1])
        due = f" · due {tk.due_date:%b %d}" if tk.due_date else ""
        c[0].markdown(f"{ICON.get(tk.pillar_code, '')} {tk.title} :gray[· P{tk.priority}{due}]")
        default = week_days.index(tk.due_date) if tk.due_date in week_days else 0
        c[1].selectbox("Day", week_days, index=default, key=f"pd{tk.id}", label_visibility="collapsed",
                       format_func=lambda d: f"{d:%a %b} {d.day}")
        c[2].button("Plan", key=f"pb{tk.id}", on_click=act, args=(
            lambda s, i=tk.id: P.plan_task_on(s, i, val(f"pd{i}"), today=day), "Planned"))
    if len(pile) > 15:
        st.caption(f"+ {len(pile) - 15} more in **All tasks**.")


def planner_month() -> None:
    a = _pl_anchor()
    grid = P.month_weeks(a.year, a.month)
    days = {x.day: x for x in P.agenda(s, grid[0][0], len(grid) * 7, today=day)}
    head = st.columns(7)
    for col, name in zip(head, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        col.markdown(f"**{name}**")
    for week in grid:
        cols = st.columns(7)
        for col, d in zip(cols, week):
            ag = days[d]
            with col, st.container(border=True):
                num = f"{d.day}"
                if d == day:
                    st.markdown(f"**:blue[● {num}]**")
                elif d.month != a.month:
                    st.caption(num)
                else:
                    st.markdown(f"**{num}**")
                lines = ([f"{P.KINDS.get(b.kind, '•')} {_short(b.title, 16)}" for b in ag.blocks]
                         + [f"🎯 {_short(x.title, 16)}" for x in ag.deadlines]
                         + [f"{'✅' if x.status == S.COMPLETED else '○'} {_short(x.title, 16)}" for x in ag.tasks
                            if not x.protected])
                for line in lines[:3]:
                    st.caption(line)
                if len(lines) > 3:
                    st.caption(f"+{len(lines) - 3} more")
                if d.month == a.month and lines:
                    st.button("›", key=f"mo{d}", on_click=pl_open_day, args=(d,), help="Open this day")


def planner_weekly_review() -> None:
    start = P.week_start(_pl_anchor())
    end = start + dt.timedelta(days=6)
    if start > day:
        st.info("That week hasn't started yet. Use ◀ to go back.")
        return
    w = W.week_summary(s, start, day)
    existing = W.get_review(s, start)
    st.markdown(f"#### {start:%b} {start.day} – {end:%b} {end.day}" + ("  ✅ reviewed" if existing else "")
                + ("  :gray[· in progress - only finished days are scored]" if start <= day <= end else ""))
    pct = lambda r: "n/a" if r is None else f"{r:.0%}"  # noqa: E731
    c = st.columns(5)
    c[0].metric("Completed", len(w.completed))
    c[1].metric("P1 done", pct(w.p1_rate), help=f"{w.p1_done} of {w.p1_total} critical commitments")
    c[2].metric("Routines", pct(w.routine_rate), help=f"{w.routine_done} of {w.routine_total}")
    c[3].metric("Key results met", f"{w.kr_met}/{w.kr_scored}" if w.kr_scored else "n/a")
    hrs = w.metrics()
    c[4].metric("Class + study", f"{hrs['class_hours'] + hrs['study_hours']:.1f} h",
                help=f"Class {hrs['class_hours']} h · study {hrs['study_hours']} h (from the calendar)")
    oc, ck = w.outcomes, w.checks
    c = st.columns(3)
    c[0].metric("Outcomes achieved", f"{oc.get('outcomes_achieved', 0)}/{oc.get('outcomes_total', 0)}"
                if oc.get("outcomes_total") else "n/a",
                help=f"partial {oc.get('outcomes_partial', 0)} · carried {oc.get('outcomes_carried', 0)}")
    c[1].metric("Evening checks", f"{ck.get('checks_done', 0)}/{ck.get('checks_possible', 0)}")
    c[2].metric("Asks", f"{ck.get('asks_got', 0)} won · {ck.get('asks_partial', 0)} partly",
                help=f"planned {ck.get('asks_planned', 0)} · no {ck.get('asks_no', 0)} · didn't ask {ck.get('asks_skipped', 0)}")
    if ck.get("gains"):
        st.caption("💲 What your asks got you: " + " · ".join(ck["gains"]))
    wm = M.week_money(s, start, end)
    if any(v for v in wm.values()):
        st.caption(f"💰 This week: set aside {M.fmt(wm['set_aside'])} · paid down {M.fmt(wm['paid_down'])} · "
                   f"tracked spending {M.fmt(wm['spent_tracked'])} · kept by not buying {M.fmt(wm['kept'])}")
    if w.rescheduled or w.overrides:
        st.caption(f"Rescheduled {w.rescheduled}× · priority overrides {w.overrides}×")
    open_outs = [o for o in O.outcomes_for_week(s, start) if o.status in O.OPEN]
    if open_outs:
        st.markdown("**🎯 Close out this week's outcomes** - nothing rolls over silently")
        for o in open_outs:
            r = st.columns([3, 1.3, 2, 0.9, 1.1])
            r[0].markdown(outcome_line(o))
            r[1].selectbox("As", ["achieved", "partial", "missed", "dropped"], key=f"wo_s{o.id}",
                           label_visibility="collapsed")
            r[2].text_input("Note", key=f"wo_n{o.id}", label_visibility="collapsed",
                            placeholder="evidence / what happened")
            r[3].button("Close", key=f"wo_c{o.id}", on_click=act, args=(
                lambda s, i=o.id: O.close_outcome(s, i, val(f"wo_s{i}"), val(f"wo_n{i}", "")), "Outcome closed"))
            r[4].button("Carry →", key=f"wo_r{o.id}", on_click=act, args=(
                lambda s, i=o.id: O.carry_outcome(s, i, val(f"wo_n{i}", "")), "Carried to next week"))

    left, right = st.columns(2)
    with left:
        st.markdown("**✅ Got done**")
        for x in w.completed[:12]:
            st.caption(f"✓ {x.title}")
        if not w.completed:
            st.caption("Nothing recorded this week.")
    with right:
        st.markdown("**↪️ Slipped**")
        for x in w.slipped[:12]:
            st.caption(f"○ {x.title} · P{x.priority}")
        for x in w.missed_routines[:6]:
            st.caption(f"↻ missed: {x.title} ({x.occurrence_date:%a})")
        if not (w.slipped or w.missed_routines):
            st.caption("Nothing slipped. 🔥")
    if w.key_results:
        st.markdown("**🎯 Key results this week**")
        for kr, status in w.key_results:
            st.caption(f"{STATUS_ICON[status.status]} {kr.title} · {STATUS_TEXT[status.status]}")
    ahead_tasks = W.tasks_due_between(s, end + dt.timedelta(days=1), end + dt.timedelta(days=14))
    if w.deadlines_ahead or ahead_tasks:
        st.markdown("**⏳ Coming up in the next two weeks**")
        for d in w.deadlines_ahead:
            st.caption(f"🎯 {d.date:%a %b} {d.date.day} · {d.title}")
        for x in ahead_tasks[:10]:
            st.caption(f"📌 {x.due_date:%a %b} {x.due_date.day} · {x.title} · P{x.priority}")

    focus = list(existing.focus) if existing else []
    with st.form(f"wr{start}"):
        st.markdown("**Reflect**")
        st.text_area("Wins - what went well?", value=existing.wins if existing else "", key=f"wr_w{start}")
        st.text_area("What slipped, and why? (design, not discipline: too big? wrong time? missing info?)",
                     value=existing.slipped if existing else "", key=f"wr_s{start}")
        st.text_area("Lessons - what will you change next week?", value=existing.lessons if existing else "",
                     key=f"wr_l{start}")
        st.select_slider("Energy this week", options=[1, 2, 3, 4, 5], key=f"wr_e{start}",
                         value=(existing.energy if existing and existing.energy else 3),
                         format_func=lambda n: {1: "1 😫", 2: "2 😕", 3: "3 😐", 4: "4 🙂", 5: "5 🔥"}[n])
        st.markdown("**Next week's outcomes** - up to 3 finish lines: what will be visibly different by next Sunday?")
        f = st.columns(3)
        for i in range(3):
            f[i].text_input(f"#{i + 1}", value=focus[i] if i < len(focus) else "", key=f"wr_f{i}{start}")
        st.form_submit_button("Save weekly review", type="primary", on_click=act, args=(
            lambda s: W.save_weekly_review(
                s, start, day, wins=val(f"wr_w{start}", ""), slipped=val(f"wr_s{start}", ""),
                lessons=val(f"wr_l{start}", ""), energy=val(f"wr_e{start}"),
                focus=[val(f"wr_f{i}{start}", "") for i in range(3)]), "Weekly review saved 🔥"))

    hist = W.history(s)
    if hist:
        st.markdown("**📈 Past weeks**")
        rate = lambda a, b: None if not b else round(100 * a / b)  # noqa: E731
        st.dataframe([{
            "week": f"{h.week_start:%b %d}", "completed": h.summary.get("completed"),
            "P1 %": rate(h.summary.get("p1_done", 0), h.summary.get("p1_total", 0)),
            "routines %": rate(h.summary.get("routine_done", 0), h.summary.get("routine_total", 0)),
            "KRs met": f"{h.summary.get('kr_met', 0)}/{h.summary.get('kr_scored', 0)}",
            "class+study h": (h.summary.get("class_hours", 0) or 0) + (h.summary.get("study_hours", 0) or 0),
            "energy": h.energy, "top 3": " · ".join(h.focus or []),
        } for h in hist], hide_index=True, use_container_width=True)


def planner_add_section() -> None:
    st.divider()
    kinds = list(P.KINDS)
    kind_fmt = lambda k: f"{P.KINDS[k]} {k}"  # noqa: E731
    pillar_opts = [None] + _pillar_options()
    pillar_fmt = lambda x: "-" if x is None else f"{ICON[x]} {x}"  # noqa: E731
    a, b, c = st.columns(3)
    with a.expander("➕ Add an event"):
        with st.form("ev_add", clear_on_submit=True):
            st.text_input("What", key="ev_t", placeholder="e.g. Office hours with Prof. Smith")
            st.date_input("Day", value=_pl_anchor(), key="ev_d")
            st.checkbox("All day", key="ev_all")
            x = st.columns(2)
            x[0].time_input("Start", value=dt.time(9), key="ev_s", step=900)
            x[1].time_input("End", value=dt.time(10), key="ev_e", step=900)
            st.selectbox("Kind", kinds, key="ev_k", format_func=kind_fmt)
            st.selectbox("Pillar", pillar_opts, key="ev_p", format_func=pillar_fmt)
            st.text_input("Where", key="ev_l")
            st.form_submit_button("Add event", type="primary", on_click=act, args=(lambda s: P.add_event(
                s, title=val("ev_t", ""), day=val("ev_d"), start_time=None if val("ev_all") else val("ev_s"),
                end_time=None if val("ev_all") else val("ev_e"), kind=val("ev_k"), pillar=val("ev_p"),
                location=val("ev_l", "")), "Event added"))
    with b.expander("🎓 Add a class schedule"):
        with st.form("ser_add", clear_on_submit=True):
            st.text_input("Class", key="se_t", placeholder="e.g. Contracts")
            st.multiselect("Days", ["MO", "TU", "WE", "TH", "FR", "SA", "SU"], key="se_days")
            x = st.columns(2)
            x[0].time_input("Start", value=dt.time(9), key="se_s", step=900)
            x[1].time_input("End", value=dt.time(10, 30), key="se_e", step=900)
            x = st.columns(2)
            x[0].date_input("First day", value=day, key="se_sd")
            x[1].date_input("Last day", value=day + dt.timedelta(days=100), key="se_ed")
            st.selectbox("Kind", kinds, key="se_k", format_func=kind_fmt)
            st.selectbox("Pillar", pillar_opts, index=pillar_opts.index("EDU"), key="se_p", format_func=pillar_fmt)
            st.text_input("Where", key="se_l")
            st.checkbox("Every other week", key="se_bi")
            st.form_submit_button("Add schedule", type="primary", on_click=act, args=(lambda s: P.add_series(
                s, title=val("se_t", ""), weekdays=val("se_days", []), start_date=val("se_sd"), end_date=val("se_ed"),
                start_time=val("se_s"), end_time=val("se_e"), kind=val("se_k"), pillar=val("se_p"),
                location=val("se_l", ""), biweekly=val("se_bi", False)), "Schedule added"))
    with c.expander("📥 Import a calendar (.ics)"):
        st.caption("Canvas: Calendar → Calendar Feed → open the link to download the .ics. Google Calendar: "
                   "Settings → Import & export → Export. Re-importing updates items instead of duplicating them.")
        st.file_uploader("Calendar file", type=["ics"], key="ics_file", label_visibility="collapsed")
        st.selectbox("Pillar for imported items", pillar_opts, index=pillar_opts.index("EDU"), key="ics_p",
                     format_func=pillar_fmt)
        st.button("Import calendar", key="ics_go", disabled=val("ics_file") is None, on_click=do_ics_import)

    with st.expander("🛠️ Manage calendar items"):
        a0 = P.week_start(_pl_anchor())
        items = [b for b in P.blocks_between(s, a0, a0 + dt.timedelta(days=27))]
        singles = [b for b in items if b.event_id]
        st.caption("One-off and imported items in the next four weeks shown:")
        for bl in singles[:40]:
            r = st.columns([5, 1.4, 1])
            r[0].markdown(f"{P.KINDS.get(bl.kind, '•')} {bl.day:%a %b} {bl.day.day} · {bl.time_label} · {bl.title}")
            if bl.task_id is None:
                r[1].button("Make it a task", key=f"et{bl.event_id}", on_click=act, args=(
                    lambda s, i=bl.event_id: P.event_to_task(s, i), "Task created from calendar item"))
            else:
                r[1].caption("✓ task")
            r[2].button("Delete", key=f"ed{bl.event_id}", on_click=act, args=(
                lambda s, i=bl.event_id: P.delete_event(s, i), "Deleted"))
        from lifeos.models import EventSeries
        series = list(s.scalars(select(EventSeries).where(EventSeries.active.is_(True))))
        if series:
            st.caption("Repeating schedules:")
        for ser in series:
            r = st.columns([5, 1.2, 1.2, 1])
            r[0].markdown(f"{P.KINDS.get(ser.kind, '•')} {ser.title} · {ser.frequency_label} · "
                          f"{ser.start_time:%-I:%M}-{ser.end_time:%-I:%M %p} · until {ser.end_date:%b %d}")
            r[1].date_input("Skip", value=_pl_anchor(), key=f"sk_s{ser.id}", label_visibility="collapsed")
            r[2].button("Skip that day", key=f"skb_s{ser.id}", on_click=act, args=(
                lambda s, i=ser.id: P.skip_series_day(s, i, val(f"sk_s{i}")), "Day skipped"))
            r[3].button("End", key=f"end_s{ser.id}", on_click=act, args=(
                lambda s, i=ser.id: P.end_series(s, i), "Schedule ended"))


def do_ics_import() -> None:
    from lifeos.services.ics_import import import_ics
    f = val("ics_file")
    try:
        text = f.getvalue().decode("utf-8", errors="replace")
    except Exception as exc:
        flash("error", f"Couldn't read that file: {exc}")
        return
    result = {}

    def run(s):
        result.update(import_ics(s, text, day, pillar=val("ics_p")))
    act(run, "Calendar imported")
    if result:
        flash("success", f"Calendar imported: {result['added']} new, {result['updated']} updated.")


# ------------------------------------------------------------------ jobs
def _new_role(s_) -> None:
    r = JB.create_role(s_, val("jr_company", ""), val("jr_title", ""), day, url=val("jr_url", ""),
                       posting=val("jr_posting", ""))
    n = JB.import_requirements(s_, r.id) if val("jr_import", True) else 0
    for code in val("jr_courses", []) or []:
        JB.link(s_, r.id, "course", code)
    SS["job_sel"] = r.id
    SS["job_view"] = "Roles"
    SS["jr_added"] = n


def role_header(rep) -> None:
    r = rep.role
    st.markdown(f"### {JP.STATUS_ICON[r.status]} {r.title} · {r.company}")
    bits = [f"**{r.status}**"]
    if r.applied_on:
        bits.append(f"applied {r.applied_on:%b} {r.applied_on.day}")
    if r.next_step:
        when = ""
        if rep.days_to_next is not None:
            d = rep.days_to_next
            when = " today" if d == 0 else (f" :red[{-d} day(s) overdue]" if d < 0 else f" in {d} day(s)")
        bits.append(f"next: {r.next_step}{when}")
    if r.url:
        bits.append(f"[posting]({r.url})")
    st.markdown(" · ".join(bits))
    problem = (r.strategy or {}).get("problem", "").strip()
    st.caption(f"🌐 **Solves:** {problem[:220]}{'…' if len(problem) > 220 else ''}" if problem else
               "🌐 Start with **Big picture**: what problem does this seat solve for the organization?")
    rd = rep.readiness
    pct = lambda x: "-" if x is None else f"{round(x * 100)}%"
    m = st.columns(4)
    m[0].metric("Readiness", pct(rd.overall), help="Average of requirements proven and study applied. "
                "Only evidence counts - a requirement you haven't rated counts as zero.")
    m[1].metric("Requirements proven", pct(rd.requirements),
                help=" · ".join(f"{JP.STRENGTH_ICON[k]} {k} {v}" for k, v in rd.counts.items()))
    m[2].metric("Linked study applied", pct(rd.study))
    open_tasks = [t for t in rep.tasks if t.status in OPEN_STATES]
    m[3].metric("Prep tasks open", len(open_tasks))


def role_tabs(rep) -> None:
    r = rep.role
    rid = r.id
    tabs = st.tabs(["🌐 Big picture", "🧭 Strategy", "✅ Requirements", "⚡ Automation", "🎓 Study & proof",
                    "🛠️ Tasks & stories", "📝 Notes", "⚙️ Status", "📄 Posting", "🕓 History"])
    tabs = list(tabs)
    big, auto = tabs.pop(0), tabs.pop(2)
    with big:
        st.caption("Understand the seat before you sell yourself for it: where it sits, the problem it exists to solve, "
                   "and how success is judged. Everything else (pitch, stories, automation ideas) should answer this.")
        for k_, label, prompt in JP.BIG_PICTURE:
            st.text_area(label, value=(r.strategy or {}).get(k_, ""), key=f"js{rid}{k_}", help=prompt,
                         height=120 if k_ == "problem" else 90, placeholder=prompt)
        st.button("Save big picture", type="primary", key=f"jbp{rid}", on_click=act, args=(lambda s: JB.save_strategy(
            s, rid, {k_: val(f"js{rid}{k_}", "") for k_ in JP.BIG_PICTURE_KEYS}), "Big picture saved"))
    with auto:
        automation_tab(rep)
    with tabs[0]:
        st.caption("Write the plan for THIS role. Each section has a prompt - hover the ⓘ.")
        for k_, label, prompt in JP.STRATEGY:
            st.text_area(label, value=(r.strategy or {}).get(k_, ""), key=f"js{rid}{k_}", help=prompt,
                         height=110 if k_ in ("needs", "proof", "gaps", "plan") else 80, placeholder=prompt)
        st.button("Save strategy", type="primary", key=f"jss{rid}", on_click=act, args=(lambda s: JB.save_strategy(
            s, rid, {k_: val(f"js{rid}{k_}", "") for k_ in JP.STRATEGY_KEYS}), "Strategy saved"))
    with tabs[1]:
        st.caption("What they want vs what you can prove. 🟢 strong (you have proof) · 🟡 partial · 🔴 gap · ⚪ not rated. "
                   "For a gap: link a unit or make a prep task, so it closes.")
        if r.posting.strip():
            st.button("Import requirements from the posting's bullet points", key=f"jri{rid}", on_click=act, args=(
                lambda s: JB.import_requirements(s, rid), "Requirements imported (duplicates skipped)"))
        for q in r.requirements:
            with st.container(border=True):
                c = st.columns([5, 2, 1])
                c[0].markdown(f"{JP.STRENGTH_ICON[q.strength]} **{q.text}**")
                c[1].selectbox("Strength", JP.STRENGTHS, index=JP.STRENGTHS.index(q.strength), key=f"jrs{q.id}",
                               label_visibility="collapsed", format_func=lambda x: f"{JP.STRENGTH_ICON[x]} {x}")
                c[2].button("✕", key=f"jrx{q.id}", help="Remove", on_click=act, args=(
                    lambda s, i=q.id: JB.remove_requirement(s, i), "Requirement removed"))
                c[0].text_input("Evidence", value=q.evidence, key=f"jre{q.id}", label_visibility="collapsed",
                                placeholder="Proof: what you did, numbers, a repo, a portfolio piece")
                c[1].button("Save", key=f"jrv{q.id}", on_click=act, args=(lambda s, i=q.id: JB.update_requirement(
                    s, i, strength=val(f"jrs{i}"), evidence=val(f"jre{i}", "")), "Saved"))
        c = st.columns([5, 1])
        c[0].text_input("Add a requirement", key=f"jrn{rid}", label_visibility="collapsed",
                        placeholder="Add a requirement in your own words")
        c[1].button("Add", key=f"jrna{rid}", on_click=act, args=(lambda s: JB.add_requirement(
            s, rid, val(f"jrn{rid}", "")), "Requirement added"))
    with tabs[2]:
        st.caption("Link University courses or units to this role. Their progress counts toward readiness, notes on "
                   "them show up here, and applied evidence becomes proof you can cite.")
        for ln in [x for x in r.links if x.kind in ("course", "unit")]:
            c = st.columns([6, 1])
            label = UNI.course(ln.ref)["title"] if ln.kind == "course" else UNI.unit(ln.ref)["title"]
            c[0].markdown(f"🎓 **{ln.ref}** · {label}")
            c[1].button("Unlink", key=f"jul{ln.id}", on_click=act, args=(lambda s, i=ln.id: JB.unlink(s, i), "Unlinked"))
        if rep.unit_levels:
            st.markdown(" ".join(f"{LRN.LEVEL_ICON[lv]}" for lv in rep.unit_levels.values()) +
                        f" :gray[· {sum(lv in ('applied', 'mastered') for lv in rep.unit_levels.values())}/"
                        f"{len(rep.unit_levels)} units applied]")
        sug = JB.suggestions(s, rid)
        if sug:
            st.markdown("**Suggested from your University** - the posting uses these terms:")
            for uk, terms in sug[:6]:
                c = st.columns([6, 1])
                c[0].markdown(f"{uk} · {UNI.unit(uk)['title']}  \n:gray[{', '.join(terms)}]")
                c[1].button("Link", key=f"jsu{rid}{uk}", on_click=act, args=(
                    lambda s, u_=uk: JB.link(s, rid, "unit", u_), f"Linked {uk}"))
        c = st.columns([5, 1])
        c[0].selectbox("Link a course", [co["code"] for co in UNI.courses()], key=f"jlc{rid}", index=None,
                       placeholder="Link a whole course", label_visibility="collapsed",
                       format_func=lambda x: f"{x} · {UNI.course(x)['title']}")
        c[1].button("Link", key=f"jlcb{rid}", on_click=act, args=(lambda s: JB.link(
            s, rid, "course", val(f"jlc{rid}") or ""), "Course linked"))
        if rep.proof_from_study:
            st.markdown("**Proof from your study** (applied units)")
            for uk, ev in rep.proof_from_study:
                st.markdown(f"- 🟢 {uk}: {ev}")
        st.markdown("**Repos and portfolio pieces**")
        for ln in rep.repos:
            c = st.columns([6, 1])
            c[0].markdown(f"🔗 {ln.ref}" + (f" - {ln.note}" if ln.note else ""))
            c[1].button("Unlink", key=f"jrl{ln.id}", on_click=act, args=(lambda s, i=ln.id: JB.unlink(s, i), "Unlinked"))
        c = st.columns([3, 3, 1])
        c[0].text_input("Repo or piece", key=f"jrp{rid}", label_visibility="collapsed",
                        placeholder="github.com/you/repo or 'NDA redline + memo'")
        c[1].text_input("Why it matters", key=f"jrpn{rid}", label_visibility="collapsed",
                        placeholder="What it proves for this role")
        c[2].button("Add", key=f"jrpa{rid}", on_click=act, args=(lambda s: JB.link(
            s, rid, "repo", val(f"jrp{rid}", ""), val(f"jrpn{rid}", "")), "Added"))
    with tabs[3]:
        st.markdown("**Prep tasks** - real tasks: they show on Today and in the Planner.")
        for t in rep.tasks:
            due = f" · due {t.due_date:%b} {t.due_date.day}" if t.due_date else ""
            done = t.status not in OPEN_STATES
            st.markdown(f"- {'✅' if done else '⬜'} {'~~' if done else ''}{t.title}{'~~' if done else ''} "
                        f":gray[P{t.priority} · {t.status}{due}]")
        c = st.columns([4, 2, 1, 1])
        c[0].text_input("Task", key=f"jt{rid}", label_visibility="collapsed",
                        placeholder="e.g. Timed NDA redline (45 min)")
        c[1].date_input("Due", value=None, key=f"jtd{rid}", label_visibility="collapsed")
        c[2].selectbox("Priority", [1, 2, 3], index=1, key=f"jtp{rid}", label_visibility="collapsed",
                       format_func=lambda x: f"P{x}")
        c[3].button("Add task", key=f"jta{rid}", on_click=act, args=(lambda s: JB.create_prep_task(
            s, rid, val(f"jt{rid}", ""), day, due=val(f"jtd{rid}"), priority=val(f"jtp{rid}", 2)), "Prep task added"))
        st.markdown("**Stories for this role**")
        for st_ in rep.stories:
            with st.expander(f"⭐ {st_.title}"):
                story_body(st_)
        mine = {x.id for x in rep.stories}
        others = [x for x in JB.stories(s) if x.id not in mine]
        if others:
            c = st.columns([5, 1])
            c[0].selectbox("Link a story", [x.id for x in others], key=f"jls{rid}", index=None,
                           placeholder="Link a story from your bank", label_visibility="collapsed",
                           format_func=lambda i: next(x.title for x in others if x.id == i))
            c[1].button("Link", key=f"jlsb{rid}", on_click=act, args=(lambda s: JB.link(
                s, rid, "story", str(val(f"jls{rid}") or "")), "Story linked"))
        else:
            st.caption("Add stories on the **Story bank** tab - write each once, reuse it for every role.")
    with tabs[4]:
        st.text_area("Job prep note", key=f"jn{rid}", height=110, label_visibility="collapsed",
                     placeholder="Recruiter call notes, research, interview debrief, what to say differently. #tags work.")
        st.button("Save note", key=f"jns{rid}", on_click=act, args=(lambda s: NT.add_note(
            s, day, val(f"jn{rid}", ""), role_id=rid), "Note saved"))
        st.caption("Includes your study notes on units linked to this role.")
        for n in rep.notes:
            note_card(n, f"r{rid}")
    with tabs[5]:
        c = st.columns([2, 3, 1])
        c[0].selectbox("Status", JP.STATUSES, index=JP.STATUSES.index(r.status), key=f"jst{rid}",
                       format_func=lambda x: f"{JP.STATUS_ICON[x]} {x}")
        c[1].text_input("Reason (required to close)", key=f"jstr{rid}")
        c[2].button("Update", key=f"jstb{rid}", on_click=act, args=(lambda s: JB.set_status(
            s, rid, val(f"jst{rid}"), day, val(f"jstr{rid}")), "Status updated"))
        c = st.columns([3, 2, 1])
        c[0].text_input("Next step", value=r.next_step, key=f"jnx{rid}", placeholder="e.g. Phone screen with recruiter")
        c[1].date_input("When", value=r.next_step_date, key=f"jnxd{rid}")
        c[2].button("Save", key=f"jnxb{rid}", on_click=act, args=(lambda s: JB.set_next_step(
            s, rid, val(f"jnx{rid}", ""), val(f"jnxd{rid}")), "Next step saved - it shows on Today"))
    with tabs[6]:
        c = st.columns(2)
        c[0].text_input("Company", value=r.company, key=f"jpc{rid}")
        c[1].text_input("Title", value=r.title, key=f"jpt{rid}")
        st.text_input("Link to the posting", value=r.url, key=f"jpu{rid}")
        st.text_area("Posting text (paste it - postings disappear)", value=r.posting, key=f"jpp{rid}", height=260)
        st.button("Save posting", key=f"jpb{rid}", on_click=act, args=(lambda s: JB.update_role(
            s, rid, company=val(f"jpc{rid}"), title=val(f"jpt{rid}"), url=val(f"jpu{rid}"),
            posting=val(f"jpp{rid}")), "Saved"))
    with tabs[7]:
        for e in reversed(r.events):
            arrow = f"{e.from_value} → {e.to_value}" if e.from_value and e.to_value else (e.to_value or e.from_value or "")
            st.markdown(f"- :gray[{e.at:%b %d %H:%M}] **{e.event_type.replace('_', ' ')}** {arrow}"
                        + (f" - _{e.reason}_" if e.reason else ""))


def automation_tab(rep) -> None:
    rid = rep.role.id
    st.caption("Ways you'd remove manual work in this seat. Rank by impact vs effort; build one quick win into a small "
               "demo so you can show it, not just say it. Use sample data only, never the employer's documents.")
    with st.expander("🔎 Questions that find automation"):
        for q in JP.AUTOMATION_PROMPTS:
            st.markdown(f"- {q}")
    for i in rep.ideas:
        with st.container(border=True):
            win = " :green-badge[⚡ quick win]" if JP.is_quick_win(i.impact, i.effort) else ""
            st.markdown(f"{JP.IDEA_ICON[i.status]} **{i.title}**{win} :gray[· impact {i.impact} · effort {i.effort}]")
            if i.pain or i.approach:
                st.markdown((f"**Manual today:** {i.pain}  \n" if i.pain else "") +
                            (f"**How:** {i.approach}" if i.approach else ""))
            if i.link:
                st.caption(f"🔗 {i.link}")
            with st.popover("Edit / progress"):
                st.text_input("Idea", value=i.title, key=f"ait{i.id}")
                st.text_area("Manual step it removes", value=i.pain, key=f"aip{i.id}", height=70)
                st.text_area("How you'd do it", value=i.approach, key=f"aia{i.id}", height=70)
                c = st.columns(3)
                c[0].selectbox("Impact", JP.IMPACTS, index=JP.IMPACTS.index(i.impact), key=f"aii{i.id}")
                c[1].selectbox("Effort", JP.EFFORTS, index=JP.EFFORTS.index(i.effort), key=f"aie{i.id}")
                c[2].selectbox("Status", JP.IDEA_STATUSES, index=JP.IDEA_STATUSES.index(i.status), key=f"ais{i.id}",
                               format_func=lambda x: f"{JP.IDEA_ICON[x]} {x}")
                st.text_input("Demo / repo / screenshot link", value=i.link, key=f"ail{i.id}")
                b = st.columns(3)
                b[0].button("Save", key=f"aiv{i.id}", type="primary", on_click=act, args=(
                    lambda s, x=i.id: JB.update_idea(s, x, title=val(f"ait{x}"), pain=val(f"aip{x}"),
                                                     approach=val(f"aia{x}"), impact=val(f"aii{x}"),
                                                     effort=val(f"aie{x}"), status=val(f"ais{x}"), link=val(f"ail{x}")),
                    "Idea saved"))
                b[1].button("→ Prep task", key=f"aik{i.id}", help="Create a task to build a demo of this",
                            on_click=act, args=(lambda s, x=i.id: JB.idea_to_task(s, x, day), "Demo task added - see Tasks"))
                b[2].button("Remove", key=f"aix{i.id}", on_click=act, args=(
                    lambda s, x=i.id: JB.remove_idea(s, x), "Idea removed"))
    st.markdown("**➕ New idea**")
    st.text_input("Idea", key=f"ain{rid}", placeholder="e.g. Auto-fill the contract tracker from signed agreements")
    c = st.columns(2)
    c[0].text_area("Manual step it removes", key=f"ainp{rid}", height=70,
                   placeholder="e.g. Someone retypes parties, dates and renewal terms into a spreadsheet")
    c[1].text_area("How you'd do it", key=f"aina{rid}", height=70,
                   placeholder="e.g. Ironclad repository fields + a weekly renewal report; or a Python extractor")
    c = st.columns([1, 1, 2])
    c[0].selectbox("Impact", JP.IMPACTS, index=0, key=f"aini{rid}")
    c[1].selectbox("Effort", JP.EFFORTS, index=0, key=f"aine{rid}")
    c[2].button("Add idea", key=f"ainb{rid}", on_click=act, args=(lambda s: JB.add_idea(
        s, rid, val(f"ain{rid}", ""), day, pain=val(f"ainp{rid}", ""), approach=val(f"aina{rid}", ""),
        impact=val(f"aini{rid}"), effort=val(f"aine{rid}")), "Idea added"))


def story_body(x) -> None:
    for lab, v in (("Situation", x.situation), ("Task", x.task), ("Action", x.action), ("Result", x.result)):
        if v:
            st.markdown(f"**{lab}:** {v}")
    if x.skills:
        st.caption("Shows: " + ", ".join(x.skills))


def story_form(x=None) -> None:
    p = f"sb{x.id}" if x else "sbn"
    st.text_input("Title", value=x.title if x else "", key=f"{p}t", placeholder="e.g. Got a vendor to accept a 24-hour breach notice")
    c = st.columns(2)
    c[0].text_area("Situation", value=x.situation if x else "", key=f"{p}s", height=90, placeholder="Context in 1-2 sentences")
    c[1].text_area("Task", value=x.task if x else "", key=f"{p}k", height=90, placeholder="What you were responsible for")
    c[0].text_area("Action", value=x.action if x else "", key=f"{p}a", height=110, placeholder="What YOU did - the steps, your judgment calls")
    c[1].text_area("Result", value=x.result if x else "", key=f"{p}r", height=110, placeholder="The outcome. Numbers if you have them; don't invent any.")
    st.text_input("Skills it shows (comma-separated)", value=", ".join(x.skills) if x else "", key=f"{p}x",
                  placeholder="negotiation, contract review, escalation judgment")
    fields = lambda: dict(title=val(f"{p}t", ""), situation=val(f"{p}s", ""), task=val(f"{p}k", ""),
                          action=val(f"{p}a", ""), result=val(f"{p}r", ""), skills=val(f"{p}x", "").split(","))
    if x:
        st.button("Save story", key=f"{p}b", on_click=act, args=(lambda s: JB.update_story(s, x.id, **fields()), "Story saved"))
    else:
        st.button("Add story", type="primary", key=f"{p}b", on_click=act, args=(
            lambda s: JB.add_story(s, today=day, **fields()), "Story added to your bank"))


def page_jobs() -> None:
    st.title("💼 Job Prep")
    open_roles = JB.roles(s)
    soon = JB.coming_up(s, day, 14)
    c = st.columns(4)
    c[0].metric("Open roles", len(open_roles))
    c[1].metric("Interviewing", sum(r.status == "interviewing" for r in open_roles))
    if soon:
        d = (soon[0].next_step_date - day).days
        c[2].metric("Next step", "today" if d == 0 else (f"{-d}d overdue" if d < 0 else f"in {d}d"),
                    help=f"{soon[0].next_step} · {soon[0].title} @ {soon[0].company}")
    else:
        c[2].metric("Next step", "-")
    c[3].metric("Stories in bank", len(JB.stories(s)))
    SS.setdefault("job_view", "Roles" if open_roles else "Add role")
    view = st.segmented_control("Jobs view", ["Roles", "Story bank", "Add role"], key="job_view",
                                label_visibility="collapsed") or "Roles"
    if view == "Add role":
        st.caption("Paste the posting - postings get taken down, and the bullet points become your requirements list.")
        c = st.columns(2)
        c[0].text_input("Company", key="jr_company")
        c[1].text_input("Job title", key="jr_title")
        st.text_input("Link to the posting (optional)", key="jr_url")
        st.text_area("Posting text", key="jr_posting", height=220)
        st.checkbox("Turn the posting's bullet points into requirements", value=True, key="jr_import")
        st.multiselect("Link University courses that prepare you", [co["code"] for co in UNI.courses()],
                       key="jr_courses", format_func=lambda x: f"{x} · {UNI.course(x)['title']}")
        st.button("Create role", type="primary", on_click=act, args=(_new_role, "Role created - write your strategy next"))
        return
    if view == "Story bank":
        st.caption("Interview stories in STAR form (Situation, Task, Action, Result). Write each once; link it to "
                   "every role where it answers a likely question.")
        with st.expander("➕ New story", expanded=not JB.stories(s)):
            story_form()
        for x in JB.stories(s):
            used = [r for r in JB.roles(s, include_closed=True) if str(x.id) in JB.refs(r, "story")]
            with st.expander(f"⭐ {x.title}" + (f" · used for {len(used)} role(s)" if used else "")):
                story_body(x)
                if used:
                    st.caption("Linked to: " + ", ".join(f"{r.title} @ {r.company}" for r in used))
                with st.popover("Edit story"):
                    story_form(x)
        return
    show_closed = st.toggle("Show closed roles", key="job_closed")
    rows = JB.roles(s, include_closed=show_closed)
    if not rows:
        st.info("No roles yet. Use **Add role** to start one.")
        return
    ids = [r.id for r in rows]
    if SS.get("job_sel") not in ids:
        SS["job_sel"] = ids[0]
    st.selectbox("Role", ids, key="job_sel", label_visibility="collapsed",
                 format_func=lambda i: next(f"{JP.STATUS_ICON[r.status]} {r.title} @ {r.company} · {r.status}"
                                            for r in rows if r.id == i))
    if SS.pop("jr_added", None):
        st.caption("Requirements came from the posting's bullet points - rate each one against your proof.")
    rep = JB.report(s, SS["job_sel"], day)
    with st.container(border=True):
        role_header(rep)
    role_tabs(rep)


# ------------------------------------------------------------------ studio
NEXT_LABEL = {"idea": "📋 Schedule it", "planned": "🎬 Mark shot", "shot": "✂️ Mark edited", "edited": "✅ Mark posted"}


def _date_bits(c) -> str:
    bits = []
    if c.brand:
        bits.append(f"{ST.BRAND_ICON[c.brand.status]} {c.brand.name}")
    if c.platform:
        bits.append(c.platform)
    if c.status == "posted" and c.posted_on:
        bits.append(f"posted {c.posted_on:%b} {c.posted_on.day}")
    else:
        if c.shoot_date:
            late = c.status in ("idea", "planned") and c.shoot_date < day
            bits.append((":red[" if late else "") + f"shoot {c.shoot_date:%a %b} {c.shoot_date.day}" + ("]" if late else ""))
        if c.post_date:
            late = c.post_date < day
            bits.append((":red[" if late else "") + f"post {c.post_date:%a %b} {c.post_date.day}" + ("]" if late else ""))
    return " · ".join(bits)


def content_card(c, prefix: str = "") -> None:
    cid, p = c.id, f"{prefix}sc{c.id}"
    with st.expander(f"{ST.CONTENT_ICON[c.status]} **{c.title}** · {_date_bits(c)}"):
        if c.plan:
            st.markdown(c.plan)
        if c.link:
            st.markdown(f"🔗 {c.link}")
        if c.status != "posted":
            nxt = ST.CONTENT_STATUSES[ST.CONTENT_STATUSES.index(c.status) + 1]
            if nxt == "posted":
                st.text_input("Link to the post", key=f"{p}lk", placeholder="Paste the post URL")
                if c.brand_id:
                    st.checkbox("Disclosed (#gifted / #ad or the paid-partnership label)", value=c.disclosed, key=f"{p}ds")
            st.button(NEXT_LABEL[c.status], key=f"{p}nx", type="primary", on_click=act, args=(
                lambda s: ST.advance(s, cid, day, link=val(f"{p}lk", ""), disclosed=val(f"{p}ds")),
                f"{c.title}: {nxt}" + (" - brand follow-up set" if nxt == "posted" and c.brand_id else "")))
        with st.popover("Edit"):
            st.text_input("Title", value=c.title, key=f"{p}t")
            bl = ST.brands(s)
            st.selectbox("Brand", [None] + [b.id for b in bl], key=f"{p}b",
                         index=([None] + [b.id for b in bl]).index(c.brand_id) if c.brand_id in [b.id for b in bl] else 0,
                         format_func=lambda i: "- none (own content)" if i is None else next(b.name for b in bl if b.id == i))
            cc = st.columns(2)
            cc[0].date_input("Shoot date", value=c.shoot_date, key=f"{p}sd")
            cc[1].date_input("Post date", value=c.post_date, key=f"{p}pd")
            st.text_input("Platform", value=c.platform, key=f"{p}pf")
            st.text_area("Concept & shot list", value=c.plan, key=f"{p}pl", height=120)
            st.text_input("Post link", value=c.link, key=f"{p}ln")
            st.button("Save", key=f"{p}sv", on_click=act, args=(lambda s: ST.update_content(
                s, cid, title=val(f"{p}t"), brand_id=val(f"{p}b"), shoot_date=val(f"{p}sd"), post_date=val(f"{p}pd"),
                platform=val(f"{p}pf"), plan=val(f"{p}pl"), link=val(f"{p}ln")), "Saved"))


def brand_card(b) -> None:
    bid = b.id
    pieces = [c for c in ST.content(s) if c.brand_id == bid]
    posted = sum(c.status == "posted" for c in pieces)
    with st.container(border=True):
        fu = ""
        if b.follow_up:
            d = (b.follow_up - day).days
            fu = (" · :red[follow up overdue]" if d < 0 else " · **follow up today**" if d == 0
                  else f" · follow up {b.follow_up:%a %b} {b.follow_up.day}")
        st.markdown(f"{ST.BRAND_ICON[b.status]} **{b.name}** · {b.status}{fu}  \n"
                    f":gray[{b.contact or ('no contact yet - find out who sent it' if b.status == 'gifted' else 'no contact yet')} · {len(pieces)} piece(s), {posted} posted]")
        if b.notes:
            st.caption(b.notes)
        due_now = bool(b.follow_up and b.follow_up <= day)
        c = st.columns([1, 1, 4]) if due_now else st.columns([1, 5])
        if due_now:
            c[0].button("Done +14d", key=f"bfd{bid}", help="Followed up - check in again in 14 days", on_click=act,
                        args=(lambda s: ST.update_brand(s, bid, follow_up=day + dt.timedelta(days=14)), "Next follow-up in 14 days"))
        with c[1 if due_now else 0].popover("Edit"):
            st.selectbox("Status", ST.BRAND_STATUSES, index=ST.BRAND_STATUSES.index(b.status), key=f"bst{bid}",
                         format_func=lambda x: f"{ST.BRAND_ICON[x]} {x}")
            st.date_input("Next follow-up", value=b.follow_up, key=f"bfu{bid}")
            st.text_input("Contact (person, email, PR agency)", value=b.contact, key=f"bct{bid}")
            st.text_area("Notes", value=b.notes, key=f"bnt{bid}", height=90)
            st.button("Save", key=f"bsv{bid}", on_click=act, args=(lambda s: ST.update_brand(
                s, bid, status=val(f"bst{bid}"), follow_up=val(f"bfu{bid}"), contact=val(f"bct{bid}", ""),
                notes=val(f"bnt{bid}", "")), "Brand saved"))


def page_studio() -> None:
    st.title("🎬 Studio")
    st.caption("Run it like a job: shoot what's planned, post on schedule, and turn every post into the next brand conversation.")
    sb = ST.scoreboard(s, day)
    m = st.columns(4)
    m[0].metric("Posted this month", sb["posted_this_month"])
    m[1].metric("In production", sb["in_production"])
    m[2].metric("Active brands", sb["brands_active"])
    m[3].metric("Paying brands", sb["paid_brands"])
    ag = ST.due(s, day)
    if ag:
        with st.container(border=True):
            st.markdown("**🔥 Needs you today**")
            for c in ag.shoots:
                st.markdown(f"- 🎬 Shoot: **{c.title}**" + (f" ({c.brand.name})" if c.brand else ""))
            for c in ag.posts:
                st.markdown(f"- 📤 Post: **{c.title}** - {c.status}")
            for b in ag.follow_ups:
                st.markdown(f"- 💬 Follow up: **{b.name}**" + (f" · {b.contact}" if b.contact else ""))
    SS.setdefault("studio_view", "Content")
    view = st.segmented_control("Studio view", ["Content", "Brands"], key="studio_view", label_visibility="collapsed") or "Content"
    if view == "Content":
        bl = ST.brands(s)
        with st.expander("➕ New piece"):
            st.text_input("Working title", key="sn_t", placeholder="e.g. Vendetta - 15s cinematic commercial")
            c = st.columns(3)
            c[0].selectbox("Brand", [None] + [b.id for b in bl], key="sn_b", index=0,
                           format_func=lambda i: "- none (own content)" if i is None else next(b.name for b in bl if b.id == i))
            c[1].date_input("Shoot date", value=None, key="sn_sd")
            c[2].date_input("Post date", value=None, key="sn_pd")
            st.text_input("Platform", key="sn_pf", placeholder="Instagram, TikTok, YouTube")
            st.text_area("Concept & shot list", key="sn_pl", height=100,
                         placeholder="Look, location, wardrobe, shots, hook. e.g. dark set, gold reflections, slow motion")
            st.button("Add piece", type="primary", key="sn_add", on_click=act, args=(lambda s: ST.add_content(
                s, val("sn_t", ""), day, brand_id=val("sn_b"), shoot_date=val("sn_sd"), post_date=val("sn_pd"),
                platform=val("sn_pf", ""), plan=val("sn_pl", "")), "Piece added"))
        rows = ST.content(s)
        if not rows:
            st.caption("Nothing in production yet. Add your first piece above.")
        for status, label in (("planned", "📋 Planned"), ("shot", "🎬 Shot - edit next"), ("edited", "✂️ Edited - post next"),
                              ("idea", "💡 Ideas"), ("posted", "✅ Posted")):
            group = [c for c in rows if c.status == status]
            if status == "posted":
                group = group[:10]
            if group:
                st.markdown(f"**{label}** ({len(group)})")
                for c in group:
                    content_card(c)
    else:
        with st.expander("➕ Add brand"):
            c = st.columns(2)
            c[0].text_input("Brand", key="bn_n", placeholder="e.g. Valentino Beauty")
            c[1].selectbox("Status", ST.BRAND_STATUSES, key="bn_s", format_func=lambda x: f"{ST.BRAND_ICON[x]} {x}")
            st.text_input("Contact (person, email, PR agency)", key="bn_c")
            st.date_input("Follow up on", value=None, key="bn_f")
            st.text_area("Notes", key="bn_nt", height=80, placeholder="What they sent, what they're launching next, what you pitched")
            st.button("Add brand", type="primary", key="bn_add", on_click=act, args=(lambda s: ST.add_brand(
                s, val("bn_n", ""), day, contact=val("bn_c", ""), status=val("bn_s"), follow_up=val("bn_f"),
                notes=val("bn_nt", "")), "Brand added"))
        st.caption("🎯 prospect · 🎁 gifted · 💬 talking · 💰 paid · 💤 paused. Posting brand content sets a follow-up "
                   f"{ST.FOLLOW_UP_AFTER_POST} days later - ask about upcoming launches and paid work.")
        for b in ST.brands(s):
            brand_card(b)


def page_planner() -> None:
    a, v = _pl_anchor(), _pl_view()
    top = st.columns([2.6, 0.55, 0.9, 0.55, 3.4])
    label = f"{a:%B %Y}" if v == "Month" else (f"Week of {P.week_start(a):%b} {P.week_start(a).day}"
                                                if v in ("Week", "Weekly review") else f"{a:%b %Y}")
    top[0].markdown(f"### 📅 {label}")
    top[1].button("◀", key="pl_prev", on_click=pl_move, args=(-1,), use_container_width=True)
    top[2].button("Today", key="pl_today", on_click=pl_today, use_container_width=True)
    top[3].button("▶", key="pl_next", on_click=pl_move, args=(1,), use_container_width=True)
    with top[4]:
        st.segmented_control("View", PL_VIEWS, key="pl_view", label_visibility="collapsed")
    due = W.review_due(s, day)
    if due and v != "Weekly review":
        st.info(f"🗓️ Weekly review time - week of {due:%b} {due.day}.", icon="🔥")
        st.button("Start weekly review", key="pl_wr", on_click=go_weekly_review, args=(due,))
    if v in ("Agenda", "Week"):
        outcome_manager()
    {"Agenda": planner_agenda, "Week": planner_week, "Month": planner_month,
     "Weekly review": planner_weekly_review}[_pl_view()]()
    planner_add_section()


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
    due = W.review_due(s, day)
    if due:
        st.info(f"🗓️ End of the week - time for your weekly review (week of {due:%b} {due.day}).", icon="🔥")
        st.button("Start weekly review", key="td_wr", on_click=go_weekly_review, args=(due,))
    yesterday_card(plan)
    outcomes_strip("today")
    if D.check_due(s, day):
        st.caption("🌙 Tomorrow isn't planned yet - the evening check is at the bottom of this page.")
    _m, _low = UNI.review_budget(s, day)
    _due = len(UNI.due_cards(s, day, _m, _low))
    if _due:
        st.caption(f"🧠 {_due} flashcard(s) due · ~{max(1, _due * LRN.SECONDS_PER_CARD // 60)} min - University → Review")
    for _d in DP.due_status(s, day):
        st.caption(f"💳 **{_d.fund.name}**: " + ("no payment for the last due date - pay the minimum now" if _d.missed
                   else f"minimum due {'today' if _d.days == 0 else f'in {_d.days} day(s)'}") + " - Money → Debt playbook")
    if DP.micro_due(s, day) and DP.plan(s).target:
        _t = DP.plan(s).target
        st.button(f"☕ Micro-pay {M.fmt(D_(DP.setting(s, 'micro_amount')))} → {_t.name}", key="td_micro",
                  on_click=act, args=(lambda s: DP.micro_pay(s, day), "Micro-payment logged ☕"))
    _ag = ST.due(s, day)
    if _ag:
        bits = [f"🎬 shoot {len(_ag.shoots)}" if _ag.shoots else "", f"📤 post {len(_ag.posts)}" if _ag.posts else "",
                f"💬 follow up {len(_ag.follow_ups)} brand(s)" if _ag.follow_ups else ""]
        st.caption("🎬 **Studio today:** " + " · ".join(b for b in bits if b) + " - Studio page")
    for r in JB.coming_up(s, day, 7):
        d = (r.next_step_date - day).days
        when = "today" if d == 0 else (f"{-d} day(s) overdue" if d < 0 else f"in {d} day(s)")
        st.caption(f"💼 **{r.next_step or 'Next step'}** {when} · {r.title} @ {r.company} - Jobs page")
    today_blocks = P.blocks_between(s, day, day)
    if today_blocks:
        st.caption("📅 Schedule: " + " · ".join(f"{P.KINDS.get(b.kind, '•')} {b.time_label} {b.title}"
                                               for b in today_blocks))
    road = [d for d in C.upcoming_deadlines(s, day) if d.tier != "vision"][:3]
    if road:
        st.caption("⏳ Countdown: " + " · ".join(
            f"{CD_ICON[d.countdown.state]} {d.title} - {d.countdown.label.split(' (')[0]}" for d in road))
    krs = C.active_key_results(s, day)
    if krs:
        with st.expander(f"🎯 Key results this period ({len(krs)})", expanded=True):
            for kr in krs:
                kr_row(kr)

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
    st.divider()
    with st.expander("🌙 Evening check - plan tomorrow in 3 questions", expanded=D.check_due(s, day)):
        daily_check_form()


def _kr_options() -> list[str | None]:
    return [None] + [kr.code for kr in s.scalars(select(KeyResult).where(KeyResult.status == "active")
                                                 .order_by(KeyResult.code))]


def _kr_label(code: str | None) -> str:
    if code is None:
        return "- not linked -"
    kr = s.scalar(select(KeyResult).where(KeyResult.code == code))
    return f"{kr.title}  ({kr.objective.title})"


def _pillar_options() -> list[str]:
    return [p.code for p in s.scalars(select(Pillar).order_by(Pillar.sort_order))]


def _goal_options() -> list[str | None]:
    return [None] + [g.code for g in s.scalars(select(Goal).order_by(Goal.code))]


def _goal_label(code: str | None) -> str:
    if code is None:
        return "- none -"
    title = s.scalar(select(Goal.title).where(Goal.code == code))
    return f"{code} · {title}" if title else code


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
        c[2].selectbox("Goal", _goal_options(), key="nt_goal", format_func=_goal_label)
        open_tasks = list(s.scalars(select(Task).where(Task.status.in_([str(x) for x in OPEN_STATES]))))
        st.multiselect("Can't start until these are done", [t.id for t in open_tasks], key="nt_deps",
                       format_func=lambda i: next(f"#{t.id} {t.title}" for t in open_tasks if t.id == i))
        st.selectbox("Drives which key result? ↑", _kr_options(), key="nt_kr", format_func=_kr_label)
        c = st.columns(2)
        c[0].checkbox("Requires evidence to complete", key="nt_ev")
        c[1].checkbox("Protected (never gated: sleep, shifts, caregiving)", key="nt_prot")
        st.text_area("Notes", key="nt_desc")
        st.form_submit_button("Add task", type="primary", on_click=act, args=(lambda s: T.create_task(
            s, title=val("nt_title", ""), pillar=val("nt_pillar"), priority=val("nt_prio"),
            description=val("nt_desc", ""), due_date=val("nt_due"), scheduled_date=val("nt_sched"),
            estimated_minutes=val("nt_est") or None, goal_code=val("nt_goal"), evidence_required=val("nt_ev"),
            protected=val("nt_prot"), depends_on=val("nt_deps", []), key_result_code=val("nt_kr")), "Task added"))
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
        st.selectbox("Drives which key result? ↑", _kr_options(), key="rt_kr", format_func=_kr_label)
        st.checkbox("Protected (never gated)", key="rt_prot")
        st.form_submit_button("Create routine", type="primary", on_click=act, args=(lambda s: R.create_template(
            s, title=val("rt_title", ""), pillar=val("rt_pillar"), priority=val("rt_prio"),
            frequency=val("rt_freq"), weekdays=val("rt_days") or None, month_day=val("rt_mday") or None,
            custom_rule=val("rt_custom") or None, start_date=val("rt_start"), end_date=val("rt_end"),
            estimated_minutes=val("rt_est") or None, missed_policy=val("rt_policy"), protected=val("rt_prot"),
            key_result_code=val("rt_kr")),
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
            if tpl.key_result:
                st.caption(f"↑ drives: {tpl.key_result.title}")
            with st.expander("✏️ Edit routine"):
                routine_edit_form(tpl)
            with st.expander("Link to a key result ↑"):
                krs = _kr_options()
                cur = tpl.key_result.code if tpl.key_result else None
                st.selectbox("Key result", krs, index=krs.index(cur) if cur in krs else 0, key=f"tk{tpl.id}",
                             format_func=_kr_label)
                st.button("Save link", key=f"tkb{tpl.id}", on_click=act, args=(
                    lambda s, i=tpl.id: C.link_template(s, i, val(f"tk{i}")), "Routine linked"))
            with st.expander("Skip a date"):
                st.date_input("Date to skip", key=f"sk{tpl.id}", value=day + dt.timedelta(days=1))
                st.button("Skip it", key=f"skb{tpl.id}", on_click=act, args=(
                    lambda s, i=tpl.id: R.add_exception(s, i, val(f"sk{i}")), "Date skipped"))


def _task_row(tk: Task, k: str, compact: bool = False) -> None:
    """One actionable line: ○ done · title · meta · ✏️ edit. Compact (board) puts the buttons under the title."""
    bits = [f"P{tk.priority}"]
    if tk.due_date:
        bits.append(f"due {tk.due_date:%b} {tk.due_date.day}")
    elif tk.scheduled_date:
        bits.append(f"on {tk.scheduled_date:%a %b} {tk.scheduled_date.day}")
    if tk.status in (S.WAITING, S.BLOCKED):
        bits.append(f"{tk.status}: {tk.status_reason or ''}".strip())
    if tk.status == S.IN_PROGRESS:
        bits.append("in progress")
    flag = "🔴 " if tk.priority == 1 else ""
    text = f"{flag}{tk.title}{' ↻' if tk.is_recurring else ''}  \n:gray[{' · '.join(bits)}]"
    done = dict(key=f"rd{k}{tk.id}", help="Mark done", on_click=act,
                args=(lambda s, i=tk.id: T.transition(s, i, S.COMPLETED, today=day), "Completed ✓"))
    if compact:
        st.markdown(text)
        b = st.columns(2)
        b[0].button("✓", use_container_width=True, **done)
        with b[1].popover("✏️", help="Edit", use_container_width=True):
            edit_form(tk, f"{k}{tk.id}")
        return
    cols = st.columns([0.05, 0.83, 0.12])
    cols[0].button("○", **done)
    cols[1].markdown(text)
    with cols[2].popover("✏️", help="Edit"):
        edit_form(tk, f"{k}{tk.id}")


def _pillar_header(g) -> str:
    name = s.get(Pillar, g.code).name if s.get(Pillar, g.code) else g.code
    parts = [f"{g.open_count} open"]
    if g.p1_count:
        parts.append(f"{g.p1_count} P1")
    if g.overdue_count:
        parts.append(f"{g.overdue_count} overdue")
    return f"{ICON.get(g.code, '')} {name} · " + " · ".join(parts)


def page_all() -> None:
    st.title("All tasks")
    SS.setdefault("all_view", "By pillar")
    view = st.segmented_control("View", ["By pillar", "Board", "Table"], key="all_view",
                                label_visibility="collapsed") or "By pillar"
    if view == "Table":
        return all_tasks_table()
    from lifeos.services.views import BUCKET_LABELS, BUCKETS, group_by_pillar
    tasks = [x for x in s.scalars(select(Task)) if visible(x)]
    codes = [c for c in _pillar_options() if not pillar_filter or c in pillar_filter]
    groups = group_by_pillar(tasks, codes, day)
    total = sum(g.open_count for g in groups)
    st.caption(f"{total} open tasks across {sum(1 for g in groups if g.open_count)} pillars · protected commitments "
               "(sleep, shifts) aren't listed · use the sidebar to show only routines or one-time tasks")
    if view == "By pillar":
        for g in groups:
            with st.expander(_pillar_header(g), expanded=g.urgent):
                if not g.open_count:
                    st.caption("Nothing open here.")
                for b in BUCKETS:
                    items = g.buckets[b]
                    if not items:
                        continue
                    st.markdown(f"**{BUCKET_LABELS[b]}** :gray[({len(items)})]")
                    for tk in items:
                        _task_row(tk, f"pg{b}")
        return
    cols = st.columns(len(groups) or 1)
    for col, g in zip(cols, groups):
        with col:
            st.markdown(f"**{ICON.get(g.code, '')} {s.get(Pillar, g.code).name}**")
            st.caption(" · ".join(x for x in [f"{g.open_count} open", f"{g.p1_count} P1" if g.p1_count else "",
                                              f"{g.overdue_count} overdue" if g.overdue_count else ""] if x))
            for b in BUCKETS:
                items = g.buckets[b]
                if not items:
                    continue
                with st.expander(f"{BUCKET_LABELS[b]} ({len(items)})", expanded=b in ("overdue", "this_week")):
                    for tk in items:
                        _task_row(tk, f"bd{b}", compact=True)


def all_tasks_table() -> None:
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


STATUS_ICON = {"met": "✅", "on_track": "🟢", "behind": "🟠", "missed": "🔴", "below": "🔵", "no_target": "📝",
               "no_data": "⚪", "not_started": "🗓️"}
STATUS_TEXT = {"met": "met", "on_track": "on track", "behind": "behind pace", "missed": "missed", "below": "not there yet",
               "no_target": "tracking", "no_data": "no reading yet", "not_started": "not started"}


def fmt_amount(value, unit: str = "") -> str:
    q = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"${q}" if unit.upper() in ("USD", "$") else (f"{q} {unit}".strip())


CD_ICON = {"on_clock": "⏳", "due_soon": "🟠", "due_today": "🔴", "overdue": "🔴", "not_started": "🗓️",
           "no_deadline": "⚪"}
SLOT_ICON = {"achieved": "✅", "missed": "❌", "active": "▶️", "planned": "🗓️", "unplanned": "⬜", "gap": "⚠️",
             "lead": "⏸️"}
SLOT_TEXT = {"achieved": "achieved", "missed": "missed", "active": "now", "planned": "planned",
             "unplanned": "not planned yet", "gap": "nothing was planned", "lead": "before this goal's first phase"}


def countdown_bar(entity: str, row) -> None:
    """Deadline + countdown + how much of the time window is already used."""
    cd = C.countdown_for(entity, row, day)
    moves = C.deadline_moves(s, entity, row.code)
    text = f"{CD_ICON[cd.state]} {cd.label}"
    if cd.deadline:
        text += f" · deadline {cd.deadline:%b %d, %Y} · {cd.elapsed_fraction:.0%} of the time used"
    if moves:
        text += f" · moved {moves}×"
    if cd.deadline:
        st.progress(cd.elapsed_fraction, text=text)
    else:
        st.caption(text + " - set one under **Change deadline**.")


def prefill_objective(goal_code: str, start: dt.date, end: dt.date | None = None) -> None:
    SS["o_g"], SS["o_s"], SS["o_e"] = goal_code, start, end
    flash("info", f"Objective form below is pre-filled for {goal_code} starting {start:%b %d}. Give it a title.")


def goal_timeline_row(g: Goal) -> None:
    slots = C.goal_timeline(g, day)
    if not slots:
        return
    cols = st.columns(len(slots))
    first_open = next((sl for sl in slots if sl.state == "unplanned"), None)
    for i, (col, sl) in enumerate(zip(cols, slots), 1):
        with col:
            mark = " ◀ now" if sl.current else ""
            st.caption(f"**Q{i}**{mark}  \n{sl.start:%b %d} - {sl.end:%b %d '%y}  \n"
                       f"{SLOT_ICON[sl.state]} {SLOT_TEXT[sl.state]}")
            if sl is first_open:
                st.button("Plan it", key=f"pq{g.code}{i}", on_click=prefill_objective,
                          args=(g.code, max(sl.start, day), sl.end))


def kr_row(kr: KeyResult) -> None:
    (lo, hi), status = C.kr_progress(s, kr, day)
    icon, word = STATUS_ICON[status.status], STATUS_TEXT[status.status]
    target = f"target {fmt_amount(kr.target, kr.unit)}" if kr.target > 0 else "track only"
    if status.status == "not_started":
        text = f"{icon} **{kr.title}** - {target} · starts {kr.objective.start_date:%b %d}"
    elif status.status == "no_data":
        text = f"{icon} **{kr.title}** - {target} · no reading yet"
    elif kr.measure == "level":
        value, when = C.kr_latest(kr, day)
        now = "no reading yet" if value is None else f"{fmt_amount(value, kr.unit)} (as of {when:%b %d})"
        goal = f" of {fmt_amount(kr.target, kr.unit)}" if kr.target > 0 else ""
        text = f"{icon} **{kr.title}** - {now}{goal} · {word}"
    else:
        period = "today" if kr.cadence == "daily" else f"week of {lo:%b %d}"
        goal = f" / {fmt_amount(kr.target, kr.unit)}" if kr.target > 0 else (f" {kr.unit}" if kr.unit and kr.unit.upper() != "USD" else "")
        text = f"{icon} **{kr.title}** - {fmt_amount(status.actual, kr.unit if kr.target > 0 else (kr.unit if kr.unit.upper() == 'USD' else ''))}{goal} {period} · {word}"
    st.progress(status.fraction, text=text)


def do_import() -> None:
    import yaml
    from lifeos.services.plan_import import import_plan
    f = val("plan_file")
    try:
        data = yaml.safe_load(f.getvalue().decode("utf-8"))
    except Exception as exc:  # malformed YAML
        flash("error", f"Couldn't read that file: {exc}")
        return

    def run(s):
        result = import_plan(s, data, day)
        SS["plan_warnings"] = result["warnings"]
        SS["import_summary"] = result["summary"]
    act(run, "Plan imported")
    if "import_summary" in SS and SS.get("flash", ("",))[0] == "success":
        sm = SS.pop("import_summary")
        flash("success", "Plan imported: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sm.items() if v))


def page_plan() -> None:
    st.title("Plan")
    ladder, summary = st.columns([1, 1.4])
    with ladder:
        st.markdown(
            "🔭 **Vision** · 3-5 years  \n&nbsp;&nbsp;&nbsp;↑  \n"
            "🎯 **Goals** · 6-12 months  \n&nbsp;&nbsp;&nbsp;↑  \n"
            "📅 **Objectives** · 3 months  \n&nbsp;&nbsp;&nbsp;↑  \n"
            "✅ **Key results** · daily / weekly  \n&nbsp;&nbsp;&nbsp;↑  \n"
            "_your tasks & routines_"
        )
        st.caption("Every arrow points up: each tier exists to serve the one above it.")
    gaps = C.cascade_gaps(s, day)
    unlinked = C.unlinked_open_tasks(s)
    with summary:
        by_tier = {tier: [g for g in gaps if g.tier == tier] for tier in ("vision", "goal", "objective", "key_result")}
        if not gaps:
            st.success("Every tier is connected. 👏")
        else:
            st.markdown(f"**🧭 {len(gaps)} gap(s) in the chain**")
            for tier, items in by_tier.items():
                if items:
                    st.caption(f"{TIER_LABELS[tier]}: {len(items)}")
            with st.expander("See all gaps"):
                for g in gaps:
                    st.markdown(f"- {g.message}")
        if unlinked:
            st.caption(f"{len(unlinked)} open task(s) aren't linked to a key result. Fine for chores; link the ones "
                       "that move a goal (task → More → Link ↑).")
        with st.expander("📥 Import a plan file"):
            st.caption("A .yaml plan (vision, goals, objectives, key results, tasks). Imported once, all or nothing. "
                       "Keep plan files in your data/ folder, never in Git.")
            st.file_uploader("Plan file", type=["yaml", "yml"], key="plan_file", label_visibility="collapsed")
            st.button("Import plan", key="plan_import", disabled=val("plan_file") is None, on_click=do_import)
    st.divider()

    for pillar in s.scalars(select(Pillar).order_by(Pillar.sort_order)):
        if pillar_filter and pillar.code not in pillar_filter:
            continue
        st.subheader(f"{ICON[pillar.code]} {pillar.name}")
        visions = list(s.scalars(select(Vision).where(Vision.pillar_code == pillar.code).order_by(Vision.code)))
        loose = list(s.scalars(select(Goal).where(Goal.pillar_code == pillar.code, Goal.vision_id.is_(None))
                               .order_by(Goal.code)))
        for v in visions:
            with st.container(border=True):
                st.markdown(f"🔭 **{v.title}**" + (f" · by {v.target_year}" if v.target_year else "")
                            + ("" if v.status == "active" else f" · _{v.status}_"))
                if v.description:
                    st.caption(v.description)
                countdown_bar("vision", v)
                for g in v.goals:
                    goal_block(g)
                if not v.goals:
                    st.caption("No goals feeding this vision yet.")
        if loose:
            st.caption("Goals not yet connected to a vision:")
            for g in loose:
                with st.container(border=True):
                    goal_block(g)
        if not visions and not loose:
            st.caption("Nothing planned here yet.")

    st.divider()
    st.subheader("Build the chain")
    tv, tg, to, tk, tc, ts = st.tabs(["Vision", "Goal", "Objective", "Key result", "Check in", "Close / change"])
    pillars = _pillar_options()
    with tv, st.form("f_vision", clear_on_submit=True):
        st.caption("3-5 years out: where do you want to be?")
        c = st.columns([1, 3, 1])
        c[0].selectbox("Pillar", pillars, key="v_p", format_func=lambda x: f"{ICON[x]} {x}")
        c[1].text_input("Vision", key="v_t", placeholder="e.g. Debt-free, 6 months of savings, own my home")
        c[2].number_input("By year", min_value=day.year, max_value=day.year + 15, value=day.year + 4, key="v_y")
        st.text_area("What it looks like", key="v_d")
        st.form_submit_button("Add vision", type="primary", on_click=act_warn, args=(lambda s: C.create_vision(
            s, pillar=val("v_p"), title=val("v_t", ""), description=val("v_d", ""), target_year=val("v_y"),
            today=day), "Vision added"))
    with tg, st.form("f_goal", clear_on_submit=True):
        st.caption("6-12 months out: what has to be true within a year?")
        c = st.columns([1, 3])
        c[0].selectbox("Pillar", pillars, key="g_p", format_func=lambda x: f"{ICON[x]} {x}")
        c[1].text_input("Goal", key="g_t")
        c = st.columns(2)
        c[0].date_input("Target date", value=day + dt.timedelta(days=270), key="g_d")
        c[1].selectbox("Serves vision ↑", [None] + [v.code for v in s.scalars(select(Vision).order_by(Vision.code))],
                       key="g_v", format_func=lambda x: "-" if x is None else f"{x} {s.scalar(select(Vision.title).where(Vision.code == x))}")
        st.text_input("How will you know it's achieved? (evidence)", key="g_e")
        st.form_submit_button("Add goal", type="primary", on_click=act_warn, args=(lambda s: C.create_goal(
            s, pillar=val("g_p"), title=val("g_t", ""), target_date=val("g_d"), vision_code=val("g_v"),
            progress_evidence=val("g_e", ""), today=day), "Goal added"))
        st.caption("To connect an existing goal to a vision, use **Close / change**.")
    goals = list(s.scalars(select(Goal).where(Goal.status == "active").order_by(Goal.code)))
    goal_label = {g.code: f"{g.code} {g.title}" for g in goals}
    with to, st.form("f_obj", clear_on_submit=True):
        st.caption("3 months: what must this quarter deliver toward the goal?")
        st.selectbox("Serves goal ↑", list(goal_label), key="o_g", format_func=goal_label.get)
        st.text_input("Objective", key="o_t", placeholder="e.g. Know exactly which years are unfiled and what I owe")
        c = st.columns(2)
        c[0].date_input("Starts", value=day, key="o_s")
        c[1].date_input("Ends (default: 3 months)", value=None, key="o_e")
        st.form_submit_button("Add objective", type="primary", on_click=act, args=(lambda s: C.create_objective(
            s, goal_code=val("o_g"), title=val("o_t", ""), start_date=val("o_s"), end_date=val("o_e")),
            "Objective added"))
    objs = list(s.scalars(select(Objective).where(Objective.status == "active").order_by(Objective.code)))
    obj_label = {o.code: f"{o.title} ({o.goal.code})" for o in objs}
    with tk, st.form("f_kr", clear_on_submit=True):
        st.caption("Daily/weekly: the measurable thing you do that moves the objective.")
        st.selectbox("Serves objective ↑", list(obj_label), key="k_o", format_func=obj_label.get)
        st.text_input("Key result", key="k_t", placeholder="e.g. Tax-paperwork sessions")
        c = st.columns(4)
        c[0].radio("Every", ["weekly", "daily"], key="k_c", horizontal=True)
        c[1].text_input("Target (blank = just track it)", value="", key="k_n")
        c[2].text_input("Unit (optional)", key="k_u", placeholder="sessions, $, pages")
        c[3].radio("Measured by", ["completions", "manual", "level"], key="k_m",
                   format_func=lambda m: {"completions": "Linked tasks done", "manual": "Amount I enter each period",
                                          "level": "Running total (latest reading)"}[m])
        st.form_submit_button("Add key result", type="primary", on_click=act, args=(lambda s: C.create_key_result(
            s, objective_code=val("k_o"), title=val("k_t", ""), cadence=val("k_c"), target=val("k_n", ""),
            unit=val("k_u", ""), measure=val("k_m")), "Key result added"))
    manual = [kr for kr in C.active_key_results(s, day) if kr.measure in ("manual", "level")]
    with tc, st.form("f_ci", clear_on_submit=True):
        if not manual:
            st.caption("Check-ins are for key results you enter numbers for. Only key results in an objective "
                       "that's running today show here.")
        st.caption("For running totals (skis ready, cash on hand), enter the current total, not the change.")
        st.selectbox("Key result", [kr.code for kr in manual], key="c_k", format_func=_kr_label)
        c = st.columns(2)
        c[0].date_input("Day", value=day, key="c_d")
        c[1].text_input("Amount", key="c_v")
        st.text_input("Note", key="c_n")
        st.form_submit_button("Record", type="primary", on_click=act, args=(lambda s: C.check_in(
            s, val("c_k"), val("c_d"), val("c_v", ""), val("c_n", "")), "Checked in"))
    with ts, st.form("f_status", clear_on_submit=True):
        c = st.columns(3)
        c[0].selectbox("Tier", list(C.STATUSES), key="x_t", format_func=lambda x: TIER_LABELS[x])
        c[1].text_input("Code", key="x_c", placeholder="e.g. FIN-G003-O1")
        c[2].text_input("New status", key="x_s", placeholder="achieved / missed / dropped / retired")
        st.text_input("What happened? (required)", key="x_r")
        st.form_submit_button("Update status", on_click=act, args=(lambda s: C.set_status(
            s, val("x_t"), val("x_c", "").strip(), val("x_s", "").strip(), val("x_r", "")), "Status updated"))
    with ts, st.form("f_deadline", clear_on_submit=True):
        st.caption("Change deadline - setting the first one is free; moving one needs a reason and is counted.")
        c = st.columns(3)
        c[0].selectbox("Tier", ["goal", "objective", "vision"], key="d_t", format_func=lambda x: TIER_LABELS[x])
        c[1].text_input("Code", key="d_c", placeholder="e.g. BUS-G001")
        c[2].date_input("New deadline (visions use the year)", value=day + dt.timedelta(days=180), key="d_d")
        st.text_input("Why is it moving?", key="d_r")
        st.form_submit_button("Save deadline", on_click=act, args=(lambda s: C.change_deadline(
            s, val("d_t"), val("d_c", "").strip(),
            val("d_d").year if val("d_t") == "vision" else val("d_d"), val("d_r", "")), "Deadline saved"))
    with ts, st.form("f_relink", clear_on_submit=True):
        st.caption("Connect a goal to a vision ↑")
        c = st.columns(2)
        c[0].selectbox("Goal", list(goal_label), key="r_g", format_func=goal_label.get)
        c[1].selectbox("Vision", [None] + [v.code for v in s.scalars(select(Vision).order_by(Vision.code))], key="r_v",
                       format_func=lambda x: "- none -" if x is None else f"{x} {s.scalar(select(Vision.title).where(Vision.code == x))}")
        st.form_submit_button("Connect", on_click=act, args=(lambda s: C.link_goal_to_vision(
            s, val("r_g"), val("r_v")), "Goal connected"))


TIER_ICON = {"vision": "🔭", "goal": "🎯", "objective": "📅"}
# Categorical slots 1-5 of the validated chart palette, in fixed pillar order (dataviz validator: PASS on #fbfaf7;
# three slots sit below 3:1 contrast, so every bar carries a visible text label and By pillar is the table view).
PILLAR_COLORS = {"BUS": "#2a78d6", "REL": "#eb6834", "ENV": "#1baf7a", "FIN": "#eda100", "EDU": "#e87ba4"}


def _deadline_line(d, show_pillar: bool = False) -> None:
    cd = d.countdown
    pill = f"{ICON.get(d.code.split('-')[0], '')} " if show_pillar else ""
    moved = f" · moved {d.moves}×" if d.moves else ""
    st.markdown(f"{pill}{TIER_ICON[d.tier]} **{d.title}** :gray[· {TIER_LABELS[d.tier].split(' (')[0]} · due "
                f"{d.date:%a %b} {d.date.day}, {d.date.year}{moved}]")
    st.progress(cd.elapsed_fraction, text=f"{CD_ICON[cd.state]} {cd.label.split(' (')[0]} · "
                                          f"{cd.elapsed_fraction:.0%} of the time used")


def timeline_by_pillar_view(groups) -> None:
    for g in groups:
        name = s.get(Pillar, g.code).name
        nxt = g.next_up(day)
        bits = [f"{len(g.deadlines)} deadline{'s' if len(g.deadlines) != 1 else ''}"]
        if g.overdue(day):
            bits.append(f"{len(g.overdue(day))} past due")
        if nxt:
            bits.append(f"next: {_short(nxt.title, 40)} ({nxt.countdown.label.split(' (')[0].lower()})")
        if g.undated_goals:
            bits.append(f"{len(g.undated_goals)} goal{'s' if len(g.undated_goals) != 1 else ''} without a date")
        urgent = bool(g.overdue(day) or g.due_within(day, 30))
        with st.expander(f"{ICON.get(g.code, '')} {name} · " + " · ".join(bits), expanded=urgent):
            if not g.deadlines and not g.undated_goals:
                st.caption("Nothing planned here yet - add a vision or goal on the **Plan** page.")
            for d in g.deadlines:
                _deadline_line(d)
            if g.undated_goals:
                st.markdown("**🗓️ Goals without a deadline** :gray[- give each a date so it counts down]")
                for goal in g.undated_goals:
                    r = st.columns([3, 1.4, 0.8])
                    r[0].markdown(f"🎯 {goal.code} · {goal.title}")
                    r[1].date_input("Deadline", value=day + dt.timedelta(days=270), key=f"ud{goal.id}",
                                    label_visibility="collapsed")
                    r[2].button("Set", key=f"udb{goal.id}", on_click=act, args=(
                        lambda s, c=goal.code, i=goal.id: C.change_deadline(s, "goal", c, val(f"ud{i}")),
                        "Deadline set - it's counting down"))


def timeline_roadmap_view(groups) -> None:
    from lifeos.services.views import quarter_label, quarter_start
    items = sorted((d for g in groups for d in g.deadlines), key=lambda d: d.date)
    past = [d for d in items if d.date < day]
    if past:
        with st.expander(f"🔴 Past deadline ({len(past)})", expanded=True):
            for d in past:
                _deadline_line(d, show_pillar=True)
    this_q = quarter_start(day)
    next_q = quarter_start(this_q + dt.timedelta(days=95))
    by_q: dict[dt.date, list] = {}
    for d in items:
        if d.date >= day:
            by_q.setdefault(quarter_start(d.date), []).append(d)
    if not by_q and not past:
        st.caption("No dated plans yet.")
    for q, ds in sorted(by_q.items()):
        icons = " ".join(sorted({ICON.get(d.code.split('-')[0], '') for d in ds}))
        with st.expander(f"{quarter_label(q)} · {len(ds)} deadline{'s' if len(ds) != 1 else ''} · {icons}",
                         expanded=q in (this_q, next_q)):
            month = None
            for d in ds:
                if d.date.month != month:
                    month = d.date.month
                    st.markdown(f"**{d.date:%B}**")
                _deadline_line(d, show_pillar=True)


def timeline_chart_view(groups) -> None:
    import altair as alt
    include_visions = st.checkbox("Include 3-5 year visions", value=False,
                                  help="Visions stretch the scale to years; leave off to see the next 12 months clearly.")
    rows = []
    for g in groups:
        for d in g.deadlines:
            if d.tier == "vision" and not include_visions:
                continue
            rows.append({"item": f"{TIER_ICON[d.tier]} {_short(d.title, 44)}", "pillar": s.get(Pillar, g.code).name,
                         "start": max(d.countdown.start, day - dt.timedelta(days=30)).isoformat(),
                         "deadline": d.date.isoformat(), "tier": TIER_LABELS[d.tier],
                         "left": d.countdown.label.split(" (")[0], "order": f"{g.code}-{d.date}"})
    if not rows:
        st.caption("No dated plans to chart yet.")
        return
    names = [s.get(Pillar, c).name for c in PILLAR_COLORS]
    color = alt.Color("pillar:N", scale=alt.Scale(domain=names, range=list(PILLAR_COLORS.values())),
                      legend=alt.Legend(orient="top", title=None))
    y = alt.Y("item:N", sort=alt.EncodingSortField("order"), title=None,
              axis=alt.Axis(labelLimit=320, labelColor="#1f2421", domain=False, ticks=False))
    bars = alt.Chart(alt.Data(values=rows)).mark_bar(size=14, cornerRadius=4).encode(
        x=alt.X("start:T", title=None, axis=alt.Axis(format="%b %Y", grid=True, gridColor="#ecebe6",
                                                     domain=False, labelColor="#6b6f6c")),
        x2="deadline:T", y=y, color=color,
        tooltip=[alt.Tooltip("item:N", title="Item"), alt.Tooltip("pillar:N", title="Pillar"),
                 alt.Tooltip("tier:N", title="Tier"), alt.Tooltip("deadline:T", title="Deadline", format="%b %d, %Y"),
                 alt.Tooltip("left:N", title="Time left")])
    today_rule = alt.Chart(alt.Data(values=[{"d": day.isoformat()}])).mark_rule(
        color="#1f2421", strokeWidth=2, strokeDash=[4, 3]).encode(x="d:T")
    today_text = alt.Chart(alt.Data(values=[{"d": day.isoformat(), "t": "today"}])).mark_text(
        align="left", dx=4, dy=-6, color="#6b6f6c", fontSize=11).encode(x="d:T", y=alt.value(0), text="t:N")
    st.altair_chart((bars + today_rule + today_text).properties(height=max(120, 30 * len(rows))).configure_view(
        stroke=None), use_container_width=True)
    st.caption("Each bar runs from when the item started to its deadline. Hover a bar for details. The same items, "
               "as text, are in **By pillar**.")


def page_timeline() -> None:
    from lifeos.services.views import timeline_by_pillar
    st.title("Timeline")
    st.caption("Visions, goals and objectives with deadlines. Deadlines only move with a reason, so nothing "
               "quietly resets. Task due dates live in the Planner and All tasks.")
    codes = [c for c in _pillar_options() if not pillar_filter or c in pillar_filter]
    groups = timeline_by_pillar(s, codes, day)
    every = sorted((d for g in groups for d in g.deadlines), key=lambda d: d.date)
    undated = sum(len(g.undated_goals) for g in groups)
    c = st.columns(4)
    c[0].metric("Next 30 days", sum(1 for d in every if 0 <= (d.date - day).days <= 30))
    c[1].metric("Next 12 months", sum(1 for d in every if 0 <= (d.date - day).days <= 365))
    c[2].metric("Goals without a date", undated)
    c[3].metric("Deadlines moved", sum(d.moves for d in every))
    past = [d for d in every if d.date < day]
    if past:
        st.error(f"{len(past)} commitment(s) past their deadline - close each out (achieved / missed) or move the "
                 "deadline with a reason.")
    soon = [d for d in every if day <= d.date][:4]
    if soon:
        st.markdown("**⏳ Coming up:** " + " · ".join(
            f"{ICON.get(d.code.split('-')[0], '')} {_short(d.title, 30)} :gray[({d.countdown.label.split(' (')[0]})]"
            for d in soon))
    SS.setdefault("tl_view", "By pillar")
    view = st.segmented_control("View", ["By pillar", "Roadmap", "Chart"], key="tl_view",
                                label_visibility="collapsed") or "By pillar"
    {"By pillar": timeline_by_pillar_view, "Roadmap": timeline_roadmap_view,
     "Chart": timeline_chart_view}[view](groups)


def goal_block(g: Goal) -> None:
    linked = [t for t in g.tasks if t.status != S.CANCELLED]
    tdate = f" · by {g.target_date:%b %Y}" if g.target_date else ""
    st.markdown(f"🎯 **{g.code} · {g.title}**{tdate}" + ("" if g.status == "active" else f" · _{g.status}_"))
    st.caption(f"Evidence of progress: {g.progress_evidence or ':red[not defined]'} · "
               f"Linked tasks done: {sum(t.status == S.COMPLETED for t in linked)}/{len(linked)} (activity, not achievement)")
    countdown_bar("goal", g)
    goal_timeline_row(g)
    for o in g.objectives:
        if o.status != "active" and o.end_date < day - dt.timedelta(days=120):
            continue
        h = C.objective_health(s, o, day)
        rate = "no finished periods yet" if h.rate is None else f"{h.rate:.0%} of KR periods met ({h.met}/{h.total})"
        st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ 📅 **{o.title}** · {o.start_date:%b %d} - {o.end_date:%b %d}"
                    f" · {o.status} · {rate}")
        if o.status == "active":
            cols = st.columns([0.06, 0.94])
            with cols[1]:
                countdown_bar("objective", o)
                left = (o.end_date - day).days
                has_next = any(n.start_date > o.end_date - dt.timedelta(days=7) and n.id != o.id
                               and n.status != "dropped" for n in g.objectives)
                if 0 <= left <= get_settings().cascade.next_objective_lead_days and not has_next and \
                        (g.target_date is None or o.end_date < g.target_date):
                    st.button("Plan next quarter →", key=f"nq{o.code}", on_click=prefill_objective,
                              args=(g.code, o.end_date + dt.timedelta(days=1)))
        for kr in o.key_results:
            if kr.status == "active":
                cols = st.columns([0.06, 0.94])
                with cols[1]:
                    kr_row(kr)


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


{"Today": page_today, "Planner": page_planner, "Plan": page_plan, "Timeline": page_timeline, "Money": page_money, "Deals": page_deals, "University": page_university, "Jobs": page_jobs, "Studio": page_studio, "Add": page_add, "Recurring": page_recurring, "All tasks": page_all,
 "Review": page_review}[page]()
s.close()
