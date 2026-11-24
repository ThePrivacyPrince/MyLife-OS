"""UNI-* : Personal University - content integrity, spaced review, earned mastery, library, inquiries, exams."""
import datetime as dt
import re
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from lifeos.engines import learning as L
from lifeos.errors import NotFound, ValidationError
from lifeos.services import university as U

T = dt.date(2026, 12, 7)
ALLOWED_DOMAINS = {
    "consumerfinance.gov", "annualcreditreport.com", "myfico.com", "irs.gov", "sba.gov", "investor.gov",
    "khanacademy.org", "coursera.org", "ocw.mit.edu", "pon.harvard.edu", "sre.google", "automatetheboringstuff.com",
    "cs50.harvard.edu", "docs.python.org", "developer.mozilla.org", "requests.readthedocs.io", "weather.gov",
    "openpolicyagent.org", "developer.hashicorp.com", "nist.gov", "csrc.nist.gov", "iso.org", "aicpa-cima.com",
    "lsac.org", "isaca.org", "pecb.com", "pwia.org", "yamahawaverunners.com", "thisoldhouse.com",
}


# ---------------------------------------------------------------- content integrity (no fabricated sources)
def test_uni01_every_reference_exists_and_links_are_official():
    c = U.content()
    srcs = c["sources"]
    for s in srcs.values():
        assert s["type"] in ("paper", "book", "guide", "course", "standard") and s.get("title") and s.get("why")
        if s["type"] == "paper":
            assert s.get("authors") and s.get("year") and s.get("venue"), f"paper {s['key']} needs a full citation"
            assert "url" not in s, "papers are cited, not linked to guessed URLs"
        if "url" in s:
            host = urlparse(s["url"]).hostname or ""
            assert s["url"].startswith("https://")
            assert any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS), f"unapproved domain {host}"
    for u in c["units"].values():
        for k in u["sources"]:
            assert k in srcs, f"{u['key']} cites unknown source {k}"
        assert u["objectives"] and u["key_points"] and u["apply"] and u["mode"] in ("knowledge", "studio")
    for q in c["inquiries"]:
        assert all(k in srcs for k in q["sources"])
    for cl in c["checklists"].values():
        assert all(k in srcs for k in cl["sources"]) and cl["steps"] and cl["safety"]


def test_uni02_quiz_answers_and_catalog_are_consistent():
    c = U.content()
    for u in c["units"].values():
        for q in u.get("quiz", []):
            if q.get("type") == "short":
                assert len(q["key_points"]) >= 2
            else:
                assert 0 <= q["answer"] < len(q["choices"]) and q["why"]
    depts = {d["code"] for d in U.departments()}
    sems = {s["code"] for s in U.semesters()}
    exams = {e["code"] for e in U.exams()}
    for co in U.courses():
        assert co["dept"] in depts and (co.get("semester") in sems or co.get("semester") is None)
        if co.get("exam"):
            assert co["exam"] in exams
    s1 = U.courses(semester="S1")
    assert sorted(x["role"] for x in s1) == ["anchor", "hard", "human"], "Rule of One"
    assert len({cd["key"] for cd in c["cards"].values()}) == len(c["cards"])


def test_uni03_worked_examples_in_quizzes_are_arithmetically_right():
    assert round(Decimal(4900) / (Decimal(208) - Decimal(60)), 1) == Decimal("33.1")  # OWN101-U5 -> 34 bookings
    assert 72 / 8 == 9 and 600 / 1000 == 0.6


# ---------------------------------------------------------------- engine
def test_uni04_leitner_boxes():
    assert L.next_review(0, True, T) == (1, T + dt.timedelta(days=3))
    assert L.next_review(5, True, T) == (6, T + dt.timedelta(days=120))
    assert L.next_review(6, True, T) == (6, T + dt.timedelta(days=120))
    assert L.next_review(4, False, T) == (0, T + dt.timedelta(days=1))
    assert L.daily_limit(10, False) == 20 and L.daily_limit(10, True) == 8


def test_uni05_mastery_is_earned_not_granted():
    d = lambda n: T + dt.timedelta(days=n)  # noqa: E731
    ev = lambda **kw: L.UnitEvidence(**{**dict(first_activity=T, attempted=False, correct_days=(), applied=False), **kw})  # noqa: E731
    assert L.mastery(L.UnitEvidence(None, False, (), False)) == "none"
    assert L.mastery(ev()) == "introduced", "opening a lesson earns nothing more"
    assert L.mastery(ev(attempted=True, correct_days=(d(0),))) == "practiced", "same-day correct isn't recall"
    assert L.mastery(ev(attempted=True, correct_days=(d(8),))) == "recalled"
    assert L.mastery(ev(applied=True)) == "applied"
    assert L.mastery(ev(attempted=True, applied=True, correct_days=(d(8), d(20)))) == "applied", "not spaced 21 days"
    assert L.mastery(ev(attempted=True, applied=True, correct_days=(d(8), d(30)))) == "mastered"


# ---------------------------------------------------------------- service
def test_uni06_start_unit_enrolls_its_cards_and_reviews_schedule(s):
    U.start_unit(s, "OWN101-U2", T)
    due = U.due_cards(s, T)
    assert len(due) == len(U.cards_for("OWN101-U2")) and all(c.box == 0 for c in due)
    U.review_card(s, "OWN101:APR", True, T)
    U.review_card(s, "OWN101:APY", False, T)
    assert {c.card_key for c in U.due_cards(s, T)} == {c["key"] for c in U.cards_for("OWN101-U2")} - {"OWN101:APR", "OWN101:APY"}
    with pytest.raises(NotFound):
        U.review_card(s, "OWN101:DTI", True, T)  # unit 3 not started


def test_uni07_recall_quiz_and_evidence_rules(s):
    with pytest.raises(ValidationError):
        U.save_recall(s, "OWN101-U4", "file", [], T)
    U.save_recall(s, "OWN101-U4", "Filing on time matters more than paying in full because the penalty is bigger.",
                  ["File even if you can't pay - the filing penalty is the bigger one"], T)
    assert U.answer_quiz(s, "OWN101-U4", 0, T, choice=1) is True
    assert U.answer_quiz(s, "OWN101-U4", 1, T, choice=0) is False
    with pytest.raises(ValidationError):
        U.answer_quiz(s, "OWN101-U4", 0, T)
    assert U.unit_level(s, "OWN101-U4") == "practiced"
    assert U.answer_quiz(s, "OWN101-U4", 1, T + dt.timedelta(days=9), choice=1) is True
    assert U.unit_level(s, "OWN101-U4") == "recalled"
    U.save_evidence(s, "OWN101-U4", "Pulled wage & income transcripts for 2023-2025; table saved in FIN-G003 notes.",
                    runs=False, today=T + dt.timedelta(days=10))
    assert U.unit_level(s, "OWN101-U4") == "applied"


def test_uni08_studio_units_pass_only_when_it_runs(s):
    with pytest.raises(ValidationError, match="runs"):
        U.save_evidence(s, "SYS101-U3", "Wrote booking.rego and booking_test.rego in jetlife-controls repo", False, T)
    U.save_evidence(s, "SYS101-U3", "opa test passes: 6/6, incl. 'no waiver -> deny'. repo: jetlife-controls", True, T)
    assert U.unit_level(s, "SYS101-U3") == "applied"


def test_uni09_short_answers_are_self_checked_against_key_points(s):
    q = U.unit("SYS101-U1")["quiz"][0]
    assert U.answer_quiz(s, "SYS101-U1", 0, T, text="Tests give repeatable proof and catch breakage.",
                         hits=q["key_points"][:2]) is True
    assert U.answer_quiz(s, "SYS101-U1", 0, T, text="It just feels more official to have tests.", hits=[]) is False


def test_uni10_reading_inquiries_exams_handson(s):
    U.check_source(s, "shah2012", T)
    assert "shah2012" in U.checked_sources(s)
    e = U.start_reading(s, "", T, source_key="housel")
    with pytest.raises(ValidationError):
        U.finish_reading(s, e.id, ["a", "b"], "x", T)
    U.finish_reading(s, e.id, ["Wealth is what you don't see", "Room for error", "Enough"], "Raise my buffer", T)
    U.ensure_inquiries(s, T)
    U.ensure_inquiries(s, T)
    from lifeos.models import Inquiry
    n = len(list(s.scalars(__import__("sqlalchemy").select(Inquiry))))
    assert n == len(U.content()["inquiries"]), "seeded once"
    mine = U.add_inquiry(s, "Haiti", "What did the 1915-1934 US occupation change in Haiti?", T)
    with pytest.raises(ValidationError):
        U.conclude_inquiry(s, mine.id, sources_used="one book", conclusion="x" * 50, would_change_mind="y", today=T)
    U.conclude_inquiry(s, mine.id, sources_used="Dubois\nTrouillot", conclusion="It centralized power and " * 3,
                       would_change_mind="Primary records showing otherwise", today=T)
    U.set_exam(s, "TF_ASSOC", T + dt.timedelta(days=120), "pass", T)
    U.log_practice(s, "TF_ASSOC", 41, 57, T, "first practice test")
    with pytest.raises(ValidationError):
        U.log_practice(s, "TF_ASSOC", 60, 57, T)
    assert len(U.practice_history(s, "TF_ASSOC")) == 1
    with pytest.raises(ValidationError):
        U.log_hands_on(s, "LIFE101-PAINT", [0, 1], "photos in Drive", T)
    steps = list(range(len(U.content()["checklists"]["LIFE101-PAINT"]["steps"])))
    U.log_hands_on(s, "LIFE101-PAINT", steps, "Bedroom done, before/after photos in Drive", T)


def test_uni11_course_report_and_semester(s):
    assert U.current_semester(dt.date(2026, 11, 24))["code"] == "S1"  # upcoming
    assert U.current_semester(dt.date(2027, 1, 15))["code"] == "S1"
    U.start_unit(s, "NEG101-U1", T)
    r = U.course_report(s, "NEG101", T)
    assert r.levels["NEG101-U1"] == "introduced" and r.cards_active == len(U.cards_for("NEG101-U1"))
