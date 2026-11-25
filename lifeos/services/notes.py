"""Notes: a daily study journal and job-prep notes. A note can point at a course, a unit and a role at once,
so it appears in each of those places. #hashtags in the text become tags."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lifeos.engines.jobprep import tags_in
from lifeos.errors import NotFound, ValidationError
from lifeos.models import JobRole, Note
from lifeos.services import university as UNI


def _check_refs(s: Session, course_code: str | None, unit_key: str | None, role_id: int | None) -> str | None:
    if unit_key:
        u = UNI.unit(unit_key)  # raises NotFound
        if course_code and course_code != u["course"]:
            raise ValidationError(f"{unit_key} belongs to {u['course']}, not {course_code}.")
        course_code = u["course"]
    if course_code:
        UNI.course(course_code)
    if role_id is not None and s.get(JobRole, role_id) is None:
        raise NotFound(f"Role {role_id} not found.")
    return course_code


def add_note(s: Session, day: dt.date, body: str, *, title: str = "", course_code: str | None = None,
             unit_key: str | None = None, role_id: int | None = None) -> Note:
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write something first - an empty note isn't saved.")
    course_code = _check_refs(s, course_code or None, unit_key or None, role_id)
    note = Note(day=day, body=body, title=(title or "").strip()[:200], course_code=course_code,
                unit_key=unit_key or None, role_id=role_id,
                kind="job" if role_id is not None and not course_code else "study", tags=tags_in(body))
    s.add(note)
    s.flush()
    return note


def get_note(s: Session, note_id: int) -> Note:
    n = s.get(Note, note_id)
    if n is None:
        raise NotFound(f"Note {note_id} not found.")
    return n


def edit_note(s: Session, note_id: int, body: str, title: str | None = None) -> Note:
    n = get_note(s, note_id)
    body = (body or "").strip()
    if not body:
        raise ValidationError("A note can't be empty. Delete it instead.")
    n.body, n.tags = body, tags_in(body)
    if title is not None:
        n.title = title.strip()[:200]
    s.flush()
    return n


def delete_note(s: Session, note_id: int) -> None:
    s.delete(get_note(s, note_id))
    s.flush()


def find(s: Session, *, day: dt.date | None = None, start: dt.date | None = None, end: dt.date | None = None,
         course_code: str | None = None, unit_keys: list[str] | None = None, role_id: int | None = None,
         kind: str | None = None, query: str = "", tag: str | None = None) -> list[Note]:
    """Newest first. unit_keys and role_id combine with OR (a role's page shows its own notes plus notes on
    the units linked to it)."""
    q = select(Note)
    if day:
        q = q.where(Note.day == day)
    if start:
        q = q.where(Note.day >= start)
    if end:
        q = q.where(Note.day <= end)
    if course_code:
        q = q.where(Note.course_code == course_code)
    if kind:
        q = q.where(Note.kind == kind)
    ors = []
    if unit_keys:
        ors.append(Note.unit_key.in_(unit_keys))
    if role_id is not None:
        ors.append(Note.role_id == role_id)
    if ors:
        q = q.where(or_(*ors))
    if query.strip():
        like = f"%{query.strip()}%"
        q = q.where(or_(Note.body.ilike(like), Note.title.ilike(like)))
    rows = list(s.scalars(q.order_by(Note.day.desc(), Note.id.desc())))
    if tag:
        rows = [n for n in rows if tag.lower().lstrip("#") in (n.tags or [])]
    return rows


def all_tags(s: Session) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for tags in s.scalars(select(Note.tags)):
        for t in tags or []:
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def streak(s: Session, today: dt.date) -> int:
    """Consecutive days with at least one study note, ending today (or yesterday, if today has none yet)."""
    days = set(s.scalars(select(Note.day).where(Note.kind == "study")))
    d = today if today in days else today - dt.timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n
