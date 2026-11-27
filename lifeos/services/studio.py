"""Studio: content creation run like a job. Brands (who, status, follow-up) and content (planned -> shot ->
edited -> posted). Kept deliberately small."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.errors import NotFound, ValidationError
from lifeos.models import Brand, ContentPiece, StudioEvent

BRAND_STATUSES = ["prospect", "gifted", "talking", "paid", "paused"]
BRAND_ICON = {"prospect": "🎯", "gifted": "🎁", "talking": "💬", "paid": "💰", "paused": "💤"}
CONTENT_STATUSES = ["idea", "planned", "shot", "edited", "posted"]
CONTENT_ICON = {"idea": "💡", "planned": "📋", "shot": "🎬", "edited": "✂️", "posted": "✅"}
FOLLOW_UP_AFTER_POST = 3  # days: after brand content goes live, check in while it's fresh


def _log(s: Session, entity: str, eid: int, event: str, frm=None, to=None) -> None:
    s.add(StudioEvent(entity=entity, entity_id=eid, event_type=event,
                      from_value=None if frm is None else str(frm), to_value=None if to is None else str(to)))
    s.flush()


# ---------------------------------------------------------------- brands
def add_brand(s: Session, name: str, today: dt.date, *, contact: str = "", status: str = "prospect",
              follow_up: dt.date | None = None, notes: str = "") -> Brand:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Brand name is required.")
    if status not in BRAND_STATUSES:
        raise ValidationError(f"Status must be one of {', '.join(BRAND_STATUSES)}.")
    if s.scalar(select(Brand).where(Brand.name.ilike(name))):
        raise ValidationError(f"{name} is already in your brands.")
    b = Brand(name=name[:120], contact=(contact or "").strip(), status=status, follow_up=follow_up,
              notes=(notes or "").strip(), created_on=today)
    s.add(b)
    s.flush()
    _log(s, "brand", b.id, "created", to=status)
    return b


def get_brand(s: Session, brand_id: int) -> Brand:
    b = s.get(Brand, brand_id)
    if b is None:
        raise NotFound(f"Brand {brand_id} not found.")
    return b


def update_brand(s: Session, brand_id: int, *, status: str | None = None, contact: str | None = None,
                 follow_up: dt.date | None | str = "keep", notes: str | None = None) -> Brand:
    b = get_brand(s, brand_id)
    if status is not None and status != b.status:
        if status not in BRAND_STATUSES:
            raise ValidationError(f"Status must be one of {', '.join(BRAND_STATUSES)}.")
        _log(s, "brand", b.id, "status", b.status, status)
        b.status = status
    if follow_up != "keep" and follow_up != b.follow_up:
        _log(s, "brand", b.id, "follow_up", b.follow_up, follow_up)
        b.follow_up = follow_up
    if contact is not None:
        b.contact = contact.strip()
    if notes is not None:
        b.notes = notes.strip()
    s.flush()
    return b


def brands(s: Session) -> list[Brand]:
    rows = list(s.scalars(select(Brand)))
    return sorted(rows, key=lambda b: (b.status == "paused", b.follow_up or dt.date.max, b.name.lower()))


# ---------------------------------------------------------------- content
def add_content(s: Session, title: str, today: dt.date, *, brand_id: int | None = None, shoot_date=None,
                post_date=None, platform: str = "", plan: str = "") -> ContentPiece:
    title = (title or "").strip()
    if not title:
        raise ValidationError("Give the piece a working title.")
    if brand_id is not None:
        get_brand(s, brand_id)
    if shoot_date and post_date and post_date < shoot_date:
        raise ValidationError("The post date is before the shoot date.")
    c = ContentPiece(title=title[:200], brand_id=brand_id, shoot_date=shoot_date, post_date=post_date,
                     platform=(platform or "").strip(), plan=(plan or "").strip(), created_on=today,
                     status="planned" if shoot_date else "idea")
    s.add(c)
    s.flush()
    _log(s, "content", c.id, "created", to=c.status)
    return c


def get_content(s: Session, cid: int) -> ContentPiece:
    c = s.get(ContentPiece, cid)
    if c is None:
        raise NotFound(f"Content {cid} not found.")
    return c


def update_content(s: Session, cid: int, **fields) -> ContentPiece:
    c = get_content(s, cid)
    for k in ("title", "platform", "plan", "link"):
        if fields.get(k) is not None:
            setattr(c, k, fields[k].strip())
    if not c.title:
        raise ValidationError("A piece needs a title.")
    for k in ("shoot_date", "post_date"):
        if k in fields and fields[k] != getattr(c, k):
            _log(s, "content", c.id, k, getattr(c, k), fields[k])
            setattr(c, k, fields[k])
    if "brand_id" in fields:
        if fields["brand_id"] is not None:
            get_brand(s, fields["brand_id"])
        c.brand_id = fields["brand_id"]
    if fields.get("disclosed") is not None:
        c.disclosed = bool(fields["disclosed"])
    if c.shoot_date and c.post_date and c.post_date < c.shoot_date:
        raise ValidationError("The post date is before the shoot date.")
    s.flush()
    return c


def advance(s: Session, cid: int, today: dt.date, *, link: str = "", disclosed: bool | None = None) -> ContentPiece:
    """Move a piece one step forward. Posting brand content needs the gift/ad disclosure confirmed; posting
    sets a brand follow-up a few days out if none is set - so every post leads to the next conversation."""
    c = get_content(s, cid)
    if c.status == "posted":
        raise ValidationError("Already posted.")
    nxt = CONTENT_STATUSES[CONTENT_STATUSES.index(c.status) + 1]
    if disclosed is not None:
        c.disclosed = bool(disclosed)
    if nxt == "posted":
        if c.brand_id and not c.disclosed:
            raise ValidationError("Confirm the disclosure first (#gifted / #ad or the platform's paid-partnership "
                                  "label) - brand content must say so.")
        c.posted_on = today
        if link.strip():
            c.link = link.strip()
        if c.brand and c.brand.follow_up is None and c.brand.status != "paused":
            update_brand(s, c.brand.id, follow_up=today + dt.timedelta(days=FOLLOW_UP_AFTER_POST))
    _log(s, "content", c.id, "status", c.status, nxt)
    c.status = nxt
    s.flush()
    return c


def content(s: Session, include_posted: bool = True) -> list[ContentPiece]:
    rows = list(s.scalars(select(ContentPiece)))
    if not include_posted:
        rows = [c for c in rows if c.status != "posted"]
    key_date = lambda c: c.shoot_date if c.status in ("idea", "planned") else (c.post_date or c.posted_on)
    return sorted(rows, key=lambda c: (c.status == "posted", key_date(c) or dt.date.max, c.id))


def history(s: Session, entity: str, eid: int) -> list[StudioEvent]:
    return list(s.scalars(select(StudioEvent).where(StudioEvent.entity == entity, StudioEvent.entity_id == eid)
                          .order_by(StudioEvent.id)))


# ---------------------------------------------------------------- what needs you
@dataclass
class Agenda:
    shoots: list[ContentPiece]      # planned, shoot date today or past
    posts: list[ContentPiece]       # not posted, post date today or past
    follow_ups: list[Brand]         # follow-up date today or past

    def __bool__(self) -> bool:
        return bool(self.shoots or self.posts or self.follow_ups)


def due(s: Session, today: dt.date) -> Agenda:
    cs = content(s, include_posted=False)
    return Agenda(
        shoots=[c for c in cs if c.status in ("idea", "planned") and c.shoot_date and c.shoot_date <= today],
        posts=[c for c in cs if c.post_date and c.post_date <= today],
        follow_ups=[b for b in brands(s) if b.status != "paused" and b.follow_up and b.follow_up <= today],
    )


def scoreboard(s: Session, today: dt.date) -> dict:
    """This month's numbers - only counts of things that actually happened."""
    start = today.replace(day=1)
    rows = content(s)
    return {
        "posted_this_month": sum(1 for c in rows if c.posted_on and c.posted_on >= start),
        "in_production": sum(1 for c in rows if c.status in ("planned", "shot", "edited")),
        "brands_active": sum(1 for b in brands(s) if b.status in ("gifted", "talking", "paid")),
        "paid_brands": sum(1 for b in brands(s) if b.status == "paid"),
    }
