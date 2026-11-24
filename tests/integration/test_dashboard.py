"""INT-05 : every dashboard page renders without exceptions, and a button write goes through the services."""
import pytest
from streamlit.testing.v1 import AppTest

from lifeos.config import REPO_ROOT

APP = str(REPO_ROOT / "lifeos" / "dashboard" / "app.py")
PAGES = ["Today", "Planner", "Plan", "Timeline", "Money", "Deals", "Add", "Recurring", "All tasks", "Review"]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEOS_DB", str(tmp_path / "ui.db"))
    from lifeos.cli import main
    main(["seed"])


@pytest.mark.parametrize("page", PAGES)
def test_int05_pages_render(seeded_db, page):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_int06_done_button_completes_task(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    done = [b for b in at.button if b.label == "Done ✓" and not b.disabled]
    assert done, "the P1 task should have an enabled Done button"
    done[0].click().run()
    assert not at.exception
    assert any("Completed" in s.value for s in at.success)


def test_int07_build_the_whole_chain_from_the_plan_page(seeded_db):
    """Vision -> goal link -> objective -> key result, all through the UI forms."""
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Plan").run()
    at.selectbox(key="v_p").set_value("FIN")
    at.text_input(key="v_t").set_value("Debt-free with real savings")
    [b for b in at.button if b.label == "Add vision"][0].click().run()
    assert not at.exception and any("Vision added" in x.value for x in at.success)

    at.selectbox(key="r_g").set_value("FIN-G003")
    at.selectbox(key="r_v").set_value("FIN-V1")
    [b for b in at.button if b.label == "Connect"][0].click().run()
    assert any("Goal connected" in x.value for x in at.success)

    at.selectbox(key="o_g").set_value("FIN-G003")
    at.text_input(key="o_t").set_value("Know exactly what I owe")
    [b for b in at.button if b.label == "Add objective"][0].click().run()
    assert any("Objective added" in x.value for x in at.success)

    at.selectbox(key="k_o").set_value("FIN-G003-O1")
    at.text_input(key="k_t").set_value("Tax paperwork sessions")
    [b for b in at.button if b.label == "Add key result"][0].click().run()
    assert any("Key result added" in x.value for x in at.success), [e.value for e in at.error]
    assert not at.exception
    assert any("Tax paperwork sessions" in str(p.value) or "Tax paperwork sessions" in (p.proto.text or "")
               for p in at.get("progress"))


def test_int08_deadline_form_and_timeline(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Plan").run()
    at.selectbox(key="d_t").set_value("goal")
    at.text_input(key="d_c").set_value("FIN-G003")
    [b for b in at.button if b.label == "Save deadline"][0].click().run()
    assert any("Deadline saved" in x.value for x in at.success), [e.value for e in at.error]
    # moving it again without a reason is refused, visibly
    at.selectbox(key="d_t").set_value("goal")
    at.text_input(key="d_c").set_value("FIN-G003")
    [b for b in at.button if b.label == "Save deadline"][0].click().run()
    assert any("needs a reason" in e.value for e in at.error)
    at.sidebar.radio[0].set_value("Timeline").run()
    assert not at.exception
    assert any("Tax compliance" in m.value for m in at.markdown)


def test_int09_level_and_tracking_key_results_render(seeded_db):
    import datetime as dt
    from lifeos.config import get_settings
    from lifeos.db import make_engine, session_factory, unit_of_work
    from lifeos.services import cascade as C
    today = get_settings().today()
    with unit_of_work(session_factory(make_engine())) as s:
        o = C.create_objective(s, goal_code="BUS-G001", title="Buy units", start_date=today)
        lvl = C.create_key_result(s, objective_code=o.code, title="Units ready", cadence="weekly", target=6,
                                  measure="level", unit="units")
        C.create_key_result(s, objective_code=o.code, title="Cash added", cadence="weekly", target=None,
                            measure="manual", unit="USD")
        C.check_in(s, lvl.code, today, 4)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    texts = " ".join(str(p.proto.text) for p in at.get("progress"))
    assert "4 units (as of" in texts and "of 6 units" in texts and "tracking" in texts



@pytest.mark.parametrize("view", ["Agenda", "Week", "Month", "Weekly review"])
def test_int10_planner_views_render(seeded_db, view):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Planner").run()
    at.session_state["pl_view"] = view
    at.run()
    assert not at.exception, at.exception
    for label in ("◀", "Today", "▶"):
        [b for b in at.button if b.label == label][0].click().run()
        assert not at.exception


def test_int11_add_class_schedule_and_save_weekly_review(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Planner").run()
    at.text_input(key="se_t").set_value("Contracts")
    at.multiselect(key="se_days").set_value(["MO", "TU", "WE", "TH", "FR", "SA", "SU"])
    [b for b in at.button if b.label == "Add schedule"][0].click().run()
    assert any("Schedule added" in x.value for x in at.success), [e.value for e in at.error]
    assert any("Contracts" in m.value for m in at.markdown)
    at.session_state["pl_view"] = "Weekly review"
    at.run()
    [b for b in at.button if b.label == "Save weekly review"][0].click().run()
    assert not at.exception
    assert any("Weekly review saved" in x.value for x in at.success), [e.value for e in at.error]



def test_int12_edit_task_from_the_card(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    k = next(w.key for w in at.text_input if w.key and w.key.startswith("e_t"))
    suffix = k[len("e_t"):]
    at.text_input(key=k).set_value("Finish RXO onboarding (all forms)")
    at.number_input(key=f"e_m{suffix}").set_value(90)
    [b for b in at.button if b.label == "Save changes"][0].click().run()
    assert not at.exception
    assert any("Task updated" in x.value for x in at.success), [e.value for e in at.error]
    assert any("Finish RXO onboarding (all forms)" in m.value for m in at.markdown)


def test_int13_priority_edit_without_reason_is_refused_visibly(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    k = next(w.key for w in at.radio if w.key and w.key.startswith("e_pr"))
    at.radio(key=k).set_value(3)
    [b for b in at.button if b.label == "Save changes"][0].click().run()
    assert any("needs a reason" in e.value for e in at.error)


def test_int14_edit_routine(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Recurring").run()
    k = next(w.key for w in at.text_input if w.key and w.key.startswith("re_t"))
    at.text_input(key=k).set_value("Bedroom reset (10 min)")
    btn = [b for b in at.button if b.label == "Save routine"]
    idx = [w.key for w in at.text_input if w.key and w.key.startswith("re_t")].index(k)
    btn[idx].click().run()
    assert not at.exception
    assert any("Routine updated" in x.value for x in at.success), [e.value for e in at.error]



def test_int15_outcome_then_evening_check_then_yesterday_card(seeded_db):
    import datetime as dt
    from lifeos.config import get_settings
    from lifeos.db import make_engine, session_factory, unit_of_work
    from lifeos.services import outcomes as O
    today = get_settings().today()
    with unit_of_work(session_factory(make_engine())) as s:
        O.add_outcome(s, any_day=today, title="First pay date confirmed in writing", pillar="FIN")
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert any("First pay date confirmed in writing" in m.value for m in at.markdown)
    k = str(today + dt.timedelta(days=1))
    at.text_input(key=f"d_n{k}").set_value("Call RXO payroll about first pay date")
    at.text_input(key=f"d_w{k}").set_value("VX seller")
    at.text_input(key=f"d_q{k}").set_value("$300 off with trailer")
    at.text_area(key=f"d_s{k}").set_value("I can pick it up Saturday with cash. Would you take $4,500?")
    at.number_input(key=f"d_h{k}").set_value(9.0)
    [b for b in at.button if b.label == "Save the plan"][0].click().run()
    assert not at.exception
    assert any("plan saved" in x.value for x in at.success), [e.value for e in at.error]
    with unit_of_work(session_factory(make_engine())) as s:
        from lifeos.services import daily_check as D
        c = D.get_check(s, today + dt.timedelta(days=1))
        assert c.progress_task.title == "Call RXO payroll about first pay date"
        assert c.awake_minutes == 540 and c.ask_area == "price"


def test_int16_planner_outcome_manager_and_review_closeout(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Planner").run()
    at.text_input(key="oa_t").set_value("Stipend amount confirmed in writing")
    [b for b in at.button if b.label == "Add outcome"][0].click().run()
    assert any("Outcome added" in x.value for x in at.success), [e.value for e in at.error]
    at.session_state["pl_view"] = "Weekly review"
    at.run()
    assert not at.exception
    close = [b for b in at.button if b.label == "Close"]
    close[0].click().run()
    assert any("needs evidence" in e.value for e in at.error), "achieved without evidence is refused"


@pytest.mark.parametrize("view", ["By pillar", "Board", "Table"])
def test_int17_all_tasks_views(seeded_db, view):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("All tasks").run()
    at.session_state["all_view"] = view
    at.run()
    assert not at.exception, at.exception
    if view != "Table":
        labels = [e.label for e in at.expander]
        assert any(l.startswith("💼 Business") for l in labels) or view == "Board"
        done = [b for b in at.button if b.label in ("○", "✓")]
        done[0].click().run()
        assert not at.exception


@pytest.mark.parametrize("view", ["By pillar", "Roadmap", "Chart"])
def test_int18_timeline_views(seeded_db, view):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Timeline").run()
    at.session_state["tl_view"] = view
    at.run()
    assert not at.exception, at.exception


def test_int19_set_a_deadline_from_the_timeline(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Timeline").run()
    [b for b in at.button if b.label == "Set"][0].click().run()
    assert not at.exception
    assert any("Deadline set" in x.value for x in at.success), [e.value for e in at.error]



def test_int20_money_add_fund_set_aside_and_before_you_buy(seeded_db):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Money").run()
    assert not at.exception
    at.text_input(key="afnholiday").set_value("Christmas 2026")
    at.text_input(key="aftholiday").set_value("300")
    [b for b in at.button if b.label == "Add"][1].click().run()
    assert any("Added" in x.value for x in at.success), [e.value for e in at.error]
    amt = next(w.key for w in at.text_input if w.key and w.key.startswith("amtfd"))
    at.text_input(key=amt).set_value("40")
    [b for b in at.button if b.label == "Set aside"][0].click().run()
    assert any("Set aside" in x.value for x in at.success), [e.value for e in at.error]
    assert any("$40 of $300 saved" in str(p.proto.text) for p in at.get("progress"))
    at.text_input(key="byb_i").set_value("Sneakers")
    at.text_input(key="byb_p").set_value("180")
    [b for b in at.button if b.label == "Check it"][0].click().run()
    [b for b in at.button if b.label == "Skip it - keep the money"][0].click().run()
    assert any("Kept $180" in x.value for x in at.success)



@pytest.mark.parametrize("view", ["Best candidates", "Compare", "Fleet", "Passed"])
def test_int21_deals_views_and_add_listing(seeded_db, view):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Deals").run()
    at.text_input(key="na_t").set_value("2019 Yamaha VX Cruiser - Kingston")
    at.text_input(key="na_p").set_value("4800")
    [b for b in at.button if b.label == "Add listing"][0].click().run()
    assert any("Listing added" in x.value for x in at.success), [e.value for e in at.error]
    at.session_state["dq_view"] = view
    at.run()
    assert not at.exception, at.exception
