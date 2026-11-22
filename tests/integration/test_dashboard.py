"""INT-05 : every dashboard page renders without exceptions, and a button write goes through the services."""
import pytest
from streamlit.testing.v1 import AppTest

from lifeos.config import REPO_ROOT

APP = str(REPO_ROOT / "lifeos" / "dashboard" / "app.py")
PAGES = ["Today", "Plan", "Timeline", "Add", "Recurring", "All tasks", "Review"]


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
