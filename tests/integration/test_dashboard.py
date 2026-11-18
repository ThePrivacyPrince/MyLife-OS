"""INT-05 : every dashboard page renders without exceptions, and a button write goes through the services."""
import pytest
from streamlit.testing.v1 import AppTest

from lifeos.config import REPO_ROOT

APP = str(REPO_ROOT / "lifeos" / "dashboard" / "app.py")
PAGES = ["Today", "Add", "Recurring", "All tasks", "Goals", "Review"]


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
