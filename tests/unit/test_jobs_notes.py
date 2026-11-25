"""JOB-* / NOTE-*: daily study notes, job prep roles, requirement matrix, links into the rest of LIFE OS."""
import datetime as dt

import pytest
from sqlalchemy import select

from lifeos.engines import jobprep as J
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import RoleEvent, TaskEvent
from lifeos.services import jobs as JB
from lifeos.services import notes as N
from lifeos.services import university as U

T = dt.date(2026, 12, 1)
POSTING = """Legal Operations Assistant
Responsibilities:
- Manage contract workflows in Ironclad from intake to signature
- Review NDAs, MSAs, DPAs and order forms and prepare redlines for attorney review
• Coordinate with Sales and Finance on deal timelines
1. Identify opportunities for process automation
- Ok
"""


# ---------------------------------------------------------------- notes
def test_note01_daily_note_ties_to_unit_course_and_tags(s):
    n = N.add_note(s, T, "Order of precedence decides conflicts #contracts #MSA", unit_key="LOPS201-U1")
    assert n.course_code == "LOPS201" and n.kind == "study" and n.tags == ["contracts", "msa"]
    assert [x.id for x in N.find(s, day=T)] == [n.id]
    assert N.find(s, course_code="LOPS201", tag="#msa") and not N.find(s, tag="python")
    assert N.find(s, query="precedence") and not N.find(s, query="terraform")
    with pytest.raises(ValidationError):
        N.add_note(s, T, "   ")
    with pytest.raises(ValidationError):
        N.add_note(s, T, "x", course_code="OWN101", unit_key="LOPS201-U1")
    with pytest.raises(NotFound):
        N.add_note(s, T, "x", unit_key="NOPE-U1")


def test_note02_edit_delete_streak_and_tag_counts(s):
    for d in (T - dt.timedelta(days=2), T - dt.timedelta(days=1), T):
        N.add_note(s, d, "studied #dpa")
    assert N.streak(s, T) == 3 and N.streak(s, T + dt.timedelta(days=1)) == 3 and N.streak(s, T + dt.timedelta(days=3)) == 0
    n = N.find(s, day=T)[0]
    N.edit_note(s, n.id, "rewrote it #gdpr")
    assert n.tags == ["gdpr"] and dict(N.all_tags(s)) == {"dpa": 2, "gdpr": 1}
    with pytest.raises(ValidationError):
        N.edit_note(s, n.id, "")
    N.delete_note(s, n.id)
    assert len(N.find(s)) == 2


# ---------------------------------------------------------------- engine
def test_job01_extract_requirements_and_tags():
    reqs = J.extract_requirements(POSTING)
    assert reqs == ["Manage contract workflows in Ironclad from intake to signature",
                    "Review NDAs, MSAs, DPAs and order forms and prepare redlines for attorney review",
                    "Coordinate with Sales and Finance on deal timelines",
                    "Identify opportunities for process automation"]
    assert J.tags_in("a #Tag and C# and #tag again, #x") == ["tag"]


def test_job02_readiness_uses_evidence_only():
    assert J.readiness([], []).overall is None  # unknown stays unknown
    r = J.readiness(["strong", "partial", "gap", "unknown"], ["applied", "introduced"])
    assert r.requirements == pytest.approx(0.375) and r.study == 0.5 and r.overall == pytest.approx(0.4375)


def test_job03_posting_matches_university_units():
    cards = list(U.content()["cards"].values())
    m = dict(J.match_units(POSTING, cards))
    assert "LOPS201-U1" in m and "MSA" in m["LOPS201-U1"]
    assert "LOPS201-U3" in m and "DPA" in m["LOPS201-U3"]
    assert "LOPS201-U4" in m  # "redline"
    assert not any(k.startswith("OWN101") for k in m)


# ---------------------------------------------------------------- service
def test_job04_role_pipeline_is_audited_and_close_needs_reason(s):
    r = JB.create_role(s, "Acme", "Legal Operations Assistant", T, posting=POSTING)
    JB.set_status(s, r.id, "applied", T)
    assert r.applied_on == T
    with pytest.raises(ReasonRequired):
        JB.set_status(s, r.id, "closed", T)
    with pytest.raises(ValidationError):
        JB.set_status(s, r.id, "ghosted", T)
    JB.set_next_step(s, r.id, "Phone screen", T + dt.timedelta(days=3))
    JB.save_strategy(s, r.id, {"pitch": "GRC analyst who reviews vendor contracts", "needs": ""})
    JB.save_strategy(s, r.id, {"pitch": "GRC analyst who reviews vendor contracts"})  # no change -> no event
    with pytest.raises(ValidationError):
        JB.save_strategy(s, r.id, {"salary_hack": "x"})
    kinds = [e.event_type for e in s.scalars(select(RoleEvent).where(RoleEvent.role_id == r.id))]
    assert kinds == ["created", "status", "next_step", "strategy"]
    assert [x.id for x in JB.coming_up(s, T)] == [r.id]
    JB.set_status(s, r.id, "closed", T, "Position filled internally")
    assert JB.roles(s) == [] and JB.roles(s, include_closed=True)[0].status_reason == "Position filled internally"


def test_job05_requirements_matrix_from_posting(s):
    r = JB.create_role(s, "Acme", "Legal Ops", T, posting=POSTING)
    assert JB.import_requirements(s, r.id) == 4 and JB.import_requirements(s, r.id) == 0
    q = r.requirements[1]
    JB.update_requirement(s, q.id, strength="strong", evidence="Redlined vendor MSAs and addenda, ~7-day turnaround")
    JB.update_requirement(s, r.requirements[0].id, strength="gap")
    with pytest.raises(ValidationError):
        JB.update_requirement(s, q.id, strength="amazing")
    rep = JB.report(s, r.id, T)
    assert rep.readiness.counts["strong"] == 1 and rep.readiness.counts["gap"] == 1
    assert rep.readiness.requirements == pytest.approx(0.25) and rep.readiness.study is None
    JB.remove_requirement(s, r.requirements[3].id)
    assert len(r.requirements) == 3


def test_job06_links_tasks_stories_and_study_flow_into_the_role(s):
    r = JB.create_role(s, "Acme", "Legal Ops", T, posting=POSTING)
    assert any(k == "LOPS201-U3" for k, _ in JB.suggestions(s, r.id))
    JB.link(s, r.id, "course", "LOPS201")
    JB.link(s, r.id, "course", "LOPS201")  # idempotent
    assert not any(k.startswith("LOPS201") for k, _ in JB.suggestions(s, r.id))
    with pytest.raises(NotFound):
        JB.link(s, r.id, "course", "NOPE999")
    JB.link(s, r.id, "repo", "github.com/ThePrivacyPrince/tprm-clause-engine", "Playbook + clause scanner")
    task = JB.create_prep_task(s, r.id, "Timed NDA redline (45 min)", T, due=T + dt.timedelta(days=2))
    assert task.description.startswith("Job prep: Legal Ops @ Acme")
    assert s.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).first() is not None  # audited like any task
    st = JB.add_story(s, "Pushed a vendor to a 24-hour breach notice", T, result="Clause accepted", skills=["negotiation", " "])
    assert st.skills == ["negotiation"]
    JB.link(s, r.id, "story", str(st.id))
    # study notes on a linked unit and job notes on the role both appear on the role page
    N.add_note(s, T, "Art. 28(3) has 8 required terms", unit_key="LOPS201-U3")
    N.add_note(s, T, "Recruiter said volume is ~40 contracts/week", role_id=r.id)
    N.add_note(s, T, "Unrelated money note", unit_key="OWN101-U1")
    U.start_unit(s, "LOPS201-U3", T)
    U.save_evidence(s, "LOPS201-U3", "DPA checklist mapped to Art. 28(3) + SOC 2, in portfolio repo", True, T)
    rep = JB.report(s, r.id, T)
    assert len(rep.unit_levels) == 6 and rep.unit_levels["LOPS201-U3"] == "applied"
    assert rep.readiness.study == pytest.approx(1 / 6)
    assert {n.body[:6] for n in rep.notes} == {"Art. 2", "Recrui"}
    assert [t.id for t in rep.tasks] == [task.id] and rep.stories[0].id == st.id and len(rep.repos) == 1
    assert rep.proof_from_study[0][0] == "LOPS201-U3"
    ln = next(x for x in r.links if x.kind == "repo")
    JB.unlink(s, ln.id)
    assert not [x for x in r.links if x.kind == "repo"]
