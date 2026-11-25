"""Job prep: roles you're preparing for, each with a strategy, a requirements-vs-evidence matrix, links into the
rest of LIFE OS (courses, units, tasks, stories, repos), notes and an append-only history."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.engines import jobprep as J
from lifeos.errors import NotFound, ReasonRequired, ValidationError
from lifeos.models import AutomationIdea, JobRole, RoleEvent, RoleLink, RoleRequirement, Story, Task, UnitProgress
from lifeos.services import notes as N
from lifeos.services import tasks as T
from lifeos.services import university as UNI


def _log(s: Session, role: JobRole, event: str, frm: str | None = None, to: str | None = None,
         reason: str | None = None) -> None:
    s.add(RoleEvent(role_id=role.id, event_type=event, from_value=frm, to_value=(to or None) and to[:300],
                    reason=reason))
    s.flush()


# ---------------------------------------------------------------- roles
def create_role(s: Session, company: str, title: str, today: dt.date, *, url: str = "", posting: str = "") -> JobRole:
    company, title = (company or "").strip(), (title or "").strip()
    if not company or not title:
        raise ValidationError("A role needs a company and a job title.")
    role = JobRole(company=company[:120], title=title[:160], url=(url or "").strip(), posting=posting or "",
                   created_on=today, strategy={})
    s.add(role)
    s.flush()
    _log(s, role, "created", to=f"{title} @ {company}")
    return role


def get_role(s: Session, role_id: int) -> JobRole:
    r = s.get(JobRole, role_id)
    if r is None:
        raise NotFound(f"Role {role_id} not found.")
    return r


def roles(s: Session, include_closed: bool = False) -> list[JobRole]:
    rows = list(s.scalars(select(JobRole)))
    if not include_closed:
        rows = [r for r in rows if r.status in J.OPEN_STATUSES]
    order = {st: i for i, st in enumerate(J.STATUSES)}
    return sorted(rows, key=lambda r: (r.next_step_date or dt.date.max, -order[r.status], r.id))


def update_role(s: Session, role_id: int, *, company: str | None = None, title: str | None = None,
                url: str | None = None, posting: str | None = None) -> JobRole:
    r = get_role(s, role_id)
    if company is not None and company.strip():
        r.company = company.strip()[:120]
    if title is not None and title.strip():
        r.title = title.strip()[:160]
    if url is not None:
        r.url = url.strip()
    if posting is not None:
        r.posting = posting
    s.flush()
    return r


def set_next_step(s: Session, role_id: int, step: str, when: dt.date | None) -> JobRole:
    r = get_role(s, role_id)
    old = f"{r.next_step} {r.next_step_date or ''}".strip()
    r.next_step, r.next_step_date = (step or "").strip()[:200], when
    new = f"{r.next_step} {r.next_step_date or ''}".strip()
    if old != new:
        _log(s, r, "next_step", old or None, new or None)
    return r


def set_status(s: Session, role_id: int, status: str, today: dt.date, reason: str | None = None) -> JobRole:
    r = get_role(s, role_id)
    if status not in J.STATUSES:
        raise ValidationError(f"Unknown status {status}.")
    if status == r.status:
        return r
    if status == "closed" and not (reason or "").strip():
        raise ReasonRequired("Closing a role needs a reason (rejected, withdrew, filled...) - it feeds your learning.")
    old = r.status
    r.status, r.status_reason = status, (reason or "").strip() or None
    if status == "applied" and r.applied_on is None:
        r.applied_on = today
    s.flush()
    _log(s, r, "status", old, status, r.status_reason)
    return r


def save_strategy(s: Session, role_id: int, sections: dict[str, str]) -> JobRole:
    r = get_role(s, role_id)
    unknown = set(sections) - set(J.SECTION_KEYS)
    if unknown:
        raise ValidationError(f"Unknown strategy section(s): {', '.join(sorted(unknown))}.")
    merged = dict(r.strategy or {})
    changed = [k for k, v in sections.items() if (v or "").strip() != (merged.get(k) or "").strip()]
    for k in changed:
        merged[k] = (sections[k] or "").strip()
    r.strategy = merged  # reassign so the JSON column is marked dirty
    s.flush()
    if changed:
        _log(s, r, "strategy", to=", ".join(changed))
    return r


# ---------------------------------------------------------------- requirements matrix
def add_requirement(s: Session, role_id: int, text: str, strength: str = "unknown", evidence: str = "") -> RoleRequirement:
    r = get_role(s, role_id)
    text = (text or "").strip()
    if not text:
        raise ValidationError("Write the requirement.")
    if strength not in J.STRENGTHS:
        raise ValidationError(f"Strength must be one of {', '.join(J.STRENGTHS)}.")
    req = RoleRequirement(role_id=r.id, text=text[:300], strength=strength, evidence=(evidence or "").strip(),
                          position=len(r.requirements))
    s.add(req)
    s.flush()
    s.expire(r, ["requirements"])
    return req


def import_requirements(s: Session, role_id: int) -> int:
    """Turn the posting's bullet lines into requirements (skipping ones already listed). Returns how many were added."""
    r = get_role(s, role_id)
    have = {q.text.lower() for q in r.requirements}
    added = 0
    for line in J.extract_requirements(r.posting):
        if line.lower() not in have:
            add_requirement(s, role_id, line)
            have.add(line.lower())
            added += 1
    if added:
        _log(s, r, "requirements_imported", to=str(added))
    return added


def update_requirement(s: Session, req_id: int, *, strength: str | None = None, evidence: str | None = None,
                       text: str | None = None) -> RoleRequirement:
    q = s.get(RoleRequirement, req_id)
    if q is None:
        raise NotFound(f"Requirement {req_id} not found.")
    if strength is not None:
        if strength not in J.STRENGTHS:
            raise ValidationError(f"Strength must be one of {', '.join(J.STRENGTHS)}.")
        q.strength = strength
    if evidence is not None:
        q.evidence = evidence.strip()
    if text is not None and text.strip():
        q.text = text.strip()[:300]
    s.flush()
    return q


def remove_requirement(s: Session, req_id: int) -> None:
    q = s.get(RoleRequirement, req_id)
    if q is None:
        raise NotFound(f"Requirement {req_id} not found.")
    role = q.role
    s.delete(q)
    s.flush()
    s.expire(role, ["requirements"])


# ---------------------------------------------------------------- links into the rest of LIFE OS
def _check_ref(s: Session, kind: str, ref: str) -> None:
    if kind == "course":
        UNI.course(ref)
    elif kind == "unit":
        UNI.unit(ref)
    elif kind == "task":
        T.get_task(s, int(ref))
    elif kind == "story":
        if s.get(Story, int(ref)) is None:
            raise NotFound(f"Story {ref} not found.")
    elif kind == "repo":
        if not ref.strip():
            raise ValidationError("Give the repo or portfolio piece a name or link.")
    else:
        raise ValidationError(f"Can't link a {kind}.")


def link(s: Session, role_id: int, kind: str, ref: str, note: str = "") -> RoleLink:
    r = get_role(s, role_id)
    ref = str(ref).strip()
    _check_ref(s, kind, ref)
    existing = s.scalar(select(RoleLink).where(RoleLink.role_id == r.id, RoleLink.kind == kind, RoleLink.ref == ref))
    if existing:
        return existing
    ln = RoleLink(role_id=r.id, kind=kind, ref=ref, note=(note or "").strip())
    s.add(ln)
    s.flush()
    s.expire(r, ["links"])
    _log(s, r, "linked", to=f"{kind}:{ref}")
    return ln


def unlink(s: Session, link_id: int) -> None:
    ln = s.get(RoleLink, link_id)
    if ln is None:
        raise NotFound(f"Link {link_id} not found.")
    r = ln.role
    _log(s, r, "unlinked", frm=f"{ln.kind}:{ln.ref}")
    s.delete(ln)
    s.flush()
    s.expire(r, ["links"])


def refs(role: JobRole, kind: str) -> list[str]:
    return [ln.ref for ln in role.links if ln.kind == kind]


def linked_units(role: JobRole) -> list[str]:
    """Units linked directly, plus every unit of each linked course."""
    keys = list(refs(role, "unit"))
    for code in refs(role, "course"):
        keys += [u["key"] for u in UNI.units(code)]
    return list(dict.fromkeys(keys))


def create_prep_task(s: Session, role_id: int, title: str, today: dt.date, *, due: dt.date | None = None,
                     pillar: str = "EDU", priority: int = 2) -> Task:
    """A normal LIFE OS task (with its audit trail), tagged to this role so it shows on the role page."""
    r = get_role(s, role_id)
    task = T.create_task(s, title=title, pillar=pillar, priority=priority, due_date=due, scheduled_date=today,
                         description=f"Job prep: {r.title} @ {r.company}")
    link(s, r.id, "task", str(task.id))
    return task


def suggestions(s: Session, role_id: int) -> list[tuple[str, list[str]]]:
    """University units that match the posting's vocabulary and aren't linked yet."""
    r = get_role(s, role_id)
    have = set(linked_units(r))
    cards = list(UNI.content()["cards"].values())
    return [(k, terms) for k, terms in J.match_units(r.posting, cards) if k not in have]


# ---------------------------------------------------------------- automation ideas
def _check_idea_fields(impact: str, effort: str, status: str) -> None:
    if impact not in J.IMPACTS or effort not in J.EFFORTS or status not in J.IDEA_STATUSES:
        raise ValidationError("Impact, effort or status isn't one of the allowed values.")


def add_idea(s: Session, role_id: int, title: str, today: dt.date, *, pain: str = "", approach: str = "",
             impact: str = "medium", effort: str = "medium") -> AutomationIdea:
    r = get_role(s, role_id)
    title = (title or "").strip()
    if not title:
        raise ValidationError("Name the idea in a few words.")
    _check_idea_fields(impact, effort, "idea")
    idea = AutomationIdea(role_id=r.id, title=title[:200], pain=(pain or "").strip(), approach=(approach or "").strip(),
                          impact=impact, effort=effort, created_on=today)
    s.add(idea)
    s.flush()
    _log(s, r, "idea_added", to=idea.title)
    return idea


def get_idea(s: Session, idea_id: int) -> AutomationIdea:
    i = s.get(AutomationIdea, idea_id)
    if i is None:
        raise NotFound(f"Idea {idea_id} not found.")
    return i


def update_idea(s: Session, idea_id: int, **fields) -> AutomationIdea:
    i = get_idea(s, idea_id)
    old_status = i.status
    pick = lambda k: fields[k] if fields.get(k) is not None else getattr(i, k)
    _check_idea_fields(pick("impact"), pick("effort"), pick("status"))  # validate before touching the row
    if fields.get("title") is not None and not fields["title"].strip():
        raise ValidationError("An idea needs a name.")
    for k in ("title", "pain", "approach", "link"):
        if fields.get(k) is not None:
            setattr(i, k, fields[k].strip())
    i.impact, i.effort, i.status = pick("impact"), pick("effort"), pick("status")
    s.flush()
    if i.status != old_status:
        _log(s, get_role(s, i.role_id), "idea_status", old_status, f"{i.status}: {i.title}")
    return i


def remove_idea(s: Session, idea_id: int) -> None:
    i = get_idea(s, idea_id)
    _log(s, get_role(s, i.role_id), "idea_removed", frm=i.title)
    s.delete(i)
    s.flush()


def ideas(s: Session, role_id: int) -> list[AutomationIdea]:
    """Best first: quick wins and high impact-for-effort at the top; parked ideas last."""
    rows = list(s.scalars(select(AutomationIdea).where(AutomationIdea.role_id == role_id)))
    return sorted(rows, key=lambda i: (i.status == "parked", -J.idea_score(i.impact, i.effort), i.id))


def idea_to_task(s: Session, idea_id: int, today: dt.date, due: dt.date | None = None) -> Task:
    """Turn an idea into a prep task to build a small demo - something to show, not just say, in the interview."""
    i = get_idea(s, idea_id)
    return create_prep_task(s, i.role_id, f"Build a demo: {i.title}"[:200], today, due=due, priority=2)


# ---------------------------------------------------------------- stories
def add_story(s: Session, title: str, today: dt.date, *, situation: str = "", task: str = "", action: str = "",
              result: str = "", skills: list[str] | None = None) -> Story:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Give the story a short title.")
    st = Story(title=title[:200], situation=situation.strip(), task=task.strip(), action=action.strip(),
               result=result.strip(), skills=[x.strip() for x in (skills or []) if x.strip()], created_on=today)
    s.add(st)
    s.flush()
    return st


def update_story(s: Session, story_id: int, **fields) -> Story:
    st = s.get(Story, story_id)
    if st is None:
        raise NotFound(f"Story {story_id} not found.")
    for k in ("title", "situation", "task", "action", "result"):
        if fields.get(k) is not None:
            setattr(st, k, fields[k].strip())
    if fields.get("skills") is not None:
        st.skills = [x.strip() for x in fields["skills"] if x.strip()]
    if not st.title:
        raise ValidationError("A story needs a title.")
    s.flush()
    return st


def stories(s: Session) -> list[Story]:
    return list(s.scalars(select(Story).order_by(Story.title)))


# ---------------------------------------------------------------- report
@dataclass
class RoleReport:
    role: JobRole
    readiness: J.Readiness
    unit_levels: dict[str, str]
    tasks: list[Task]
    stories: list[Story]
    repos: list[RoleLink]
    notes: list
    ideas: list = field(default_factory=list)
    proof_from_study: list[tuple[str, str]] = field(default_factory=list)  # (unit_key, evidence)
    days_to_next: int | None = None


def report(s: Session, role_id: int, today: dt.date) -> RoleReport:
    r = get_role(s, role_id)
    units = linked_units(r)
    levels = {k: UNI.unit_level(s, k) for k in units}
    tasks = [t for t in (s.get(Task, int(x)) for x in refs(r, "task")) if t is not None]
    sts = [x for x in (s.get(Story, int(i)) for i in refs(r, "story")) if x is not None]
    proof = []
    for k in units:
        p = s.get(UnitProgress, k)
        if p and p.evidence and p.applied_on:
            proof.append((k, p.evidence))
    return RoleReport(
        role=r, readiness=J.readiness([q.strength for q in r.requirements], list(levels.values())),
        unit_levels=levels, tasks=tasks, stories=sts, repos=[ln for ln in r.links if ln.kind == "repo"],
        notes=N.find(s, role_id=r.id, unit_keys=units), ideas=ideas(s, r.id), proof_from_study=proof,
        days_to_next=(r.next_step_date - today).days if r.next_step_date else None,
    )


def coming_up(s: Session, today: dt.date, days: int = 14) -> list[JobRole]:
    """Open roles with a next step dated within `days` (overdue ones included)."""
    return [r for r in roles(s) if r.next_step_date and r.next_step_date <= today + dt.timedelta(days=days)]
