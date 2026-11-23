"""INT-05 : every dashboard page renders without exceptions, and a button write goes through the services."""
import pytest
from streamlit.testing.v1 import AppTest

from lifeos.config import REPO_ROOT

APP = str(REPO_ROOT / "lifeos" / "dashboard" / "app.py")
PAGES = ["Today", "Planner", "Plan", "Timeline", "Add", "Recurring", "All tasks", "Review"]


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
