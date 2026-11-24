"""Personal University: course content from curriculum/*.yaml (general education), progress in the database."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import REPO_ROOT, get_settings
from lifeos.engines import learning as L
from lifeos.errors import NotFound, ValidationError
from lifeos.models import (CardReview, CardState, ExamPlan, HandsOnRun, Inquiry, PracticeScore, QuizAttempt,
                           ReadingEntry, SourceCheck, UnitProgress)

CURRICULUM = REPO_ROOT / "curriculum"


# ---------------------------------------------------------------- content
@lru_cache(maxsize=1)
def _load(stamp: float) -> dict:
    cat = yaml.safe_load((CURRICULUM / "catalog.yaml").read_text())
    src = yaml.safe_load((CURRICULUM / "sources.yaml").read_text())["sources"]
    units, cards = {}, {}
    for f in sorted((CURRICULUM / "courses").glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        for u in data.get("units", []):
            u["course"] = data["course"]
            units[u["key"]] = u
        for c in data.get("cards", []):
            c["course"] = data["course"]
            c["key"] = f"{data['course']}:{c['term']}"
            cards[c["key"]] = c
    hist = yaml.safe_load((CURRICULUM / "history.yaml").read_text())["inquiries"]
    hands = yaml.safe_load((CURRICULUM / "hands_on.yaml").read_text())["checklists"]
    return {"catalog": cat, "sources": {s["key"]: s for s in src}, "units": units, "cards": cards,
            "inquiries": hist, "checklists": {c["key"]: c for c in hands}}


def content() -> dict:
    stamp = max(p.stat().st_mtime for p in CURRICULUM.rglob("*.yaml"))
    return _load(stamp)


def departments() -> list[dict]:
    return content()["catalog"]["departments"]


def courses(dept: str | None = None, semester: str | None = None) -> list[dict]:
    out = content()["catalog"]["courses"]
    return [c for c in out if (dept is None or c["dept"] == dept) and (semester is None or c.get("semester") == semester)]


def course(code: str) -> dict:
    for c in content()["catalog"]["courses"]:
        if c["code"] == code:
            return c
    raise NotFound(f"Course {code} not found.")


def units(course_code: str) -> list[dict]:
    return [u for u in content()["units"].values() if u["course"] == course_code]


def unit(unit_key: str) -> dict:
    u = content()["units"].get(unit_key)
    if u is None:
        raise NotFound(f"Unit {unit_key} not found.")
    return u


def cards_for(unit_key: str) -> list[dict]:
    return [c for c in content()["cards"].values() if c["unit"] == unit_key]


def source(key: str) -> dict:
    return content()["sources"][key]


def semesters() -> list[dict]:
    return content()["catalog"]["semesters"]


def current_semester(today: dt.date) -> dict | None:
    sems = sorted(semesters(), key=lambda x: x["start"])
    for sm in sems:
        if sm["start"] <= today <= sm["end"]:
            return sm
    upcoming = [sm for sm in sems if sm["start"] > today]
    return upcoming[0] if upcoming else None


def exams() -> list[dict]:
    return content()["catalog"]["exams"]


# ---------------------------------------------------------------- progress
def progress(s: Session, unit_key: str) -> UnitProgress:
    unit(unit_key)
    return s.get(UnitProgress, unit_key) or UnitProgress(unit_key=unit_key)


def start_unit(s: Session, unit_key: str, today: dt.date) -> UnitProgress:
    p = progress(s, unit_key)
    if p.started_on is None:
        p.started_on = today
        s.add(p)
        for c in cards_for(unit_key):
            if s.get(CardState, c["key"]) is None:
                s.add(CardState(card_key=c["key"], unit_key=unit_key, box=0, due=today))
    s.flush()
    return p


def save_recall(s: Session, unit_key: str, text: str, hits: list[str], today: dt.date) -> UnitProgress:
    """Write what you remember WITHOUT looking, then tick the key points you actually got."""
    if not text or len(text.strip()) < 20:
        raise ValidationError("Write what you remember in your own words first (a few sentences).")
    u = unit(unit_key)
    bad = [h for h in hits if h not in u.get("key_points", [])]
    if bad:
        raise ValidationError("Unknown key point.")
    p = start_unit(s, unit_key, today)
    p.recall_text, p.recall_hits, p.recall_on = text.strip(), list(hits), today
    s.flush()
    return p


def answer_quiz(s: Session, unit_key: str, q_index: int, today: dt.date, *, choice: int | None = None,
                text: str = "", hits: list[str] | None = None) -> bool:
    u = unit(unit_key)
    qs = u.get("quiz", [])
    if not 0 <= q_index < len(qs):
        raise NotFound("No such question.")
    q = qs[q_index]
    start_unit(s, unit_key, today)
    if q.get("type") == "short":
        if not text or len(text.strip()) < 15:
            raise ValidationError("Answer in your own words first.")
        need = q["key_points"]
        got = [h for h in (hits or []) if h in need]
        correct = len(got) * 2 >= len(need)  # at least half the key points, honestly self-checked
        ans = text.strip()
    else:
        if choice is None or not 0 <= choice < len(q["choices"]):
            raise ValidationError("Pick an answer.")
        correct = choice == q["answer"]
        ans = q["choices"][choice]
    s.add(QuizAttempt(unit_key=unit_key, q_index=q_index, day=today, correct=correct, answer=ans))
    s.flush()
    return correct


def save_evidence(s: Session, unit_key: str, evidence: str, runs: bool, today: dt.date) -> UnitProgress:
    """Apply step. For studio units you pass when the thing runs - say what it is and where."""
    u = unit(unit_key)
    if not evidence or len(evidence.strip()) < 15:
        raise ValidationError("Describe what you did and where the proof is (file, link, logged ask, screenshot).")
    if u.get("mode") == "studio" and not runs:
        raise ValidationError("Studio units pass when the thing runs - confirm it works (tests pass / used for real).")
    p = start_unit(s, unit_key, today)
    p.evidence, p.evidence_runs, p.applied_on = evidence.strip(), bool(runs), today
    s.flush()
    return p


def review_card(s: Session, card_key: str, correct: bool, today: dt.date) -> CardState:
    st_ = s.get(CardState, card_key)
    if st_ is None:
        raise NotFound("That card isn't in your reviews yet - start its unit first.")
    st_.box, st_.due = L.next_review(st_.box, correct, today)
    st_.reviews += 1
    st_.lapses += 0 if correct else 1
    st_.last_reviewed = today
    s.add(CardReview(card_key=card_key, unit_key=st_.unit_key, day=today, correct=correct))
    s.flush()
    return st_


def due_cards(s: Session, today: dt.date, minutes: int | None = None, low_capacity: bool = False) -> list[CardState]:
    rows = list(s.scalars(select(CardState).where(CardState.due <= today)))
    rows.sort(key=lambda c: (c.due, c.box, c.card_key))  # most overdue, least known first
    if minutes is None:
        return rows
    return rows[:L.daily_limit(minutes, low_capacity)]


def card(card_key: str) -> dict:
    c = content()["cards"].get(card_key)
    if c is None:
        raise NotFound("Card not found.")
    return c


def unit_level(s: Session, unit_key: str) -> str:
    p = s.get(UnitProgress, unit_key)
    quiz = list(s.scalars(select(QuizAttempt).where(QuizAttempt.unit_key == unit_key)))
    reviews = list(s.scalars(select(CardReview).where(CardReview.unit_key == unit_key)))
    correct_days = tuple(a.day for a in quiz if a.correct) + tuple(r.day for r in reviews if r.correct)
    ev = L.UnitEvidence(first_activity=p.started_on if p else None, attempted=bool(quiz or reviews),
                        correct_days=correct_days, applied=bool(p and p.applied_on))
    return L.mastery(ev)


@dataclass
class CourseReport:
    code: str
    levels: dict = field(default_factory=dict)  # unit_key -> level
    quiz_total: int = 0
    quiz_correct: int = 0
    cards_active: int = 0
    cards_due: int = 0
    reviews_30d: int = 0
    reviews_30d_correct: int = 0

    @property
    def mastered(self) -> int:
        return sum(1 for v in self.levels.values() if v == "mastered")

    @property
    def applied(self) -> int:
        return sum(1 for v in self.levels.values() if v in ("applied", "mastered"))


def course_report(s: Session, code: str, today: dt.date) -> CourseReport:
    r = CourseReport(code)
    keys = [u["key"] for u in units(code)]
    for k in keys:
        r.levels[k] = unit_level(s, k)
    if keys:
        qa = list(s.scalars(select(QuizAttempt).where(QuizAttempt.unit_key.in_(keys))))
        r.quiz_total, r.quiz_correct = len(qa), sum(a.correct for a in qa)
        cs = list(s.scalars(select(CardState).where(CardState.unit_key.in_(keys))))
        r.cards_active, r.cards_due = len(cs), sum(1 for c in cs if c.due <= today)
        since = today - dt.timedelta(days=30)
        rv = list(s.scalars(select(CardReview).where(CardReview.unit_key.in_(keys), CardReview.day > since)))
        r.reviews_30d, r.reviews_30d_correct = len(rv), sum(x.correct for x in rv)
    return r


# ---------------------------------------------------------------- library, inquiries, exams, hands-on
def check_source(s: Session, key: str, today: dt.date, note: str = "") -> None:
    source(key)
    row = s.get(SourceCheck, key) or SourceCheck(source_key=key, checked_on=today)
    row.checked_on, row.note = today, note.strip()
    s.add(row)
    s.flush()


def checked_sources(s: Session) -> set[str]:
    return {r.source_key for r in s.scalars(select(SourceCheck))}


def start_reading(s: Session, title: str, today: dt.date, source_key: str | None = None) -> ReadingEntry:
    if source_key:
        title = title or source(source_key)["title"]
    if not title or not title.strip():
        raise ValidationError("What are you reading?")
    e = ReadingEntry(title=title.strip(), source_key=source_key, started_on=today, takeaways=[])
    s.add(e)
    s.flush()
    return e


def finish_reading(s: Session, entry_id: int, takeaways: list[str], action: str, today: dt.date) -> ReadingEntry:
    e = s.get(ReadingEntry, entry_id)
    if e is None:
        raise NotFound("Reading entry not found.")
    tk = [t.strip() for t in takeaways if t and t.strip()]
    if len(tk) < 3:
        raise ValidationError("Write your three main takeaways.")
    if not action or not action.strip():
        raise ValidationError("Name one real action you'll take because of it.")
    e.takeaways, e.action, e.status, e.finished_on = tk[:3], action.strip(), "done", today
    s.flush()
    return e


def ensure_inquiries(s: Session, today: dt.date) -> None:
    for q in content()["inquiries"]:
        if s.scalar(select(Inquiry).where(Inquiry.seed_key == q["key"])) is None:
            s.add(Inquiry(seed_key=q["key"], topic=q["topic"], question=q["question"], created_on=today))
    s.flush()


def add_inquiry(s: Session, topic: str, question: str, today: dt.date) -> Inquiry:
    if not (topic.strip() and question.strip()):
        raise ValidationError("Give it a topic and a real question.")
    i = Inquiry(topic=topic.strip(), question=question.strip(), created_on=today)
    s.add(i)
    s.flush()
    return i


def conclude_inquiry(s: Session, inquiry_id: int, *, sources_used: str, conclusion: str, would_change_mind: str,
                     today: dt.date) -> Inquiry:
    i = s.get(Inquiry, inquiry_id)
    if i is None:
        raise NotFound("Inquiry not found.")
    srcs = [x for x in sources_used.replace(";", "\n").splitlines() if x.strip()]
    if len(srcs) < 2:
        raise ValidationError("Use at least two independent sources (one per line).")
    if len(conclusion.strip()) < 40:
        raise ValidationError("Write your conclusion in a few sentences - your own argument.")
    if not would_change_mind.strip():
        raise ValidationError("What evidence would change your mind?")
    i.sources_used, i.conclusion, i.would_change_mind = sources_used.strip(), conclusion.strip(), would_change_mind.strip()
    i.status, i.concluded_on = "concluded", today
    s.flush()
    return i


def set_exam(s: Session, code: str, test_date: dt.date | None, target_score: str, today: dt.date) -> ExamPlan:
    if code not in {e["code"] for e in exams()}:
        raise NotFound("Unknown exam.")
    if test_date and test_date < today:
        raise ValidationError("Test date is in the past.")
    row = s.get(ExamPlan, code) or ExamPlan(code=code)
    row.test_date, row.target_score = test_date, (target_score or "").strip()
    s.add(row)
    s.flush()
    return row


def log_practice(s: Session, code: str, score, max_score, today: dt.date, note: str = "") -> PracticeScore:
    from lifeos.services.money import money
    if code not in {e["code"] for e in exams()}:
        raise NotFound("Unknown exam.")
    sc = money(score)
    mx = money(max_score) if max_score not in (None, "") else None
    if mx is not None and sc > mx:
        raise ValidationError("Score can't be above the maximum.")
    row = PracticeScore(exam_code=code, day=today, score=sc, max_score=mx, note=note.strip())
    s.add(row)
    s.flush()
    return row


def practice_history(s: Session, code: str) -> list[PracticeScore]:
    return list(s.scalars(select(PracticeScore).where(PracticeScore.exam_code == code).order_by(PracticeScore.day)))


def log_hands_on(s: Session, key: str, steps_done: list[int], evidence: str, today: dt.date) -> HandsOnRun:
    cl = content()["checklists"].get(key)
    if cl is None:
        raise NotFound("Checklist not found.")
    if sorted(set(steps_done)) != list(range(len(cl["steps"]))):
        raise ValidationError("Tick every step (or finish them) before logging it as done.")
    if not evidence.strip():
        raise ValidationError("Add evidence - a note or where the before/after photos are.")
    row = HandsOnRun(checklist_key=key, day=today, steps_done=sorted(set(steps_done)), evidence=evidence.strip())
    s.add(row)
    s.flush()
    return row


def review_budget(s: Session, today: dt.date) -> tuple[int, bool]:
    """(minutes, low_capacity) for today's card queue - respects overnight / practice days."""
    from lifeos.services.planner_cal import capacity_for
    sem = current_semester(today)
    minutes = sem.get("daily_review_minutes", 10) if sem else 10
    return minutes, capacity_for(s, today).available < 60
