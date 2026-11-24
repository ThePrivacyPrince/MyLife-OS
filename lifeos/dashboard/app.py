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
from lifeos.services import cascade as C
from lifeos.services import planner_cal as P
from lifeos.services import task_edit as E
from lifeos.services import daily_check as D
from lifeos.services import outcomes as O
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
PAGES = ["Today", "Planner", "Plan", "Timeline", "Add", "Recurring", "All tasks", "Review"]
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


{"Today": page_today, "Planner": page_planner, "Plan": page_plan, "Timeline": page_timeline, "Add": page_add, "Recurring": page_recurring, "All tasks": page_all,
 "Review": page_review}[page]()
s.close()
