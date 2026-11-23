"""Import calendar files (.ics) - e.g. a Canvas calendar feed or a Google Calendar export.

The file is treated as data only. Events are copied into LIFE OS (deduplicated by UID + day), repeating
events are expanded inside a window, and times are converted to your local timezone.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from lifeos.config import get_settings
from lifeos.errors import ValidationError
from lifeos.models import CalendarEvent

MAX_EVENTS = 2000


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_prop(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _to_local(value: str, params: dict, tz: ZoneInfo) -> tuple[dt.datetime, bool]:
    """Returns (naive local datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        return dt.datetime.strptime(value[:8], "%Y%m%d"), True
    if value.endswith("Z"):
        aware = dt.datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        return aware.astimezone(tz).replace(tzinfo=None), False
    naive = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if "TZID" in params:
        try:
            return naive.replace(tzinfo=ZoneInfo(params["TZID"])).astimezone(tz).replace(tzinfo=None), False
        except Exception:
            pass  # unknown TZID: treat as local
    return naive, False


def guess_kind(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ("exam", "midterm", "final ", "finals", "quiz")):
        return "exam"
    if any(w in t for w in ("due", "assignment", "deadline", "submit", "memo", "brief")):
        return "deadline"
    if any(w in t for w in ("class", "lecture", "seminar", "section", "office hours")):
        return "class"
    return "other"


def parse_ics(text: str, today: dt.date) -> list[dict]:
    cfg = get_settings()
    tz = cfg.tz
    lo = dt.datetime.combine(today - dt.timedelta(days=cfg.planner.ics_days_back), dt.time.min)
    hi = dt.datetime.combine(today + dt.timedelta(days=cfg.planner.ics_days_ahead), dt.time.max)
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ValidationError("That doesn't look like a calendar (.ics) file.")
    events, cur = [], None
    for line in _unfold(text):
        if not line.strip():
            continue
        name, params, value = _parse_prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"exdates": []}
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None:
            if name in ("DTSTART", "DTEND"):
                cur[name] = _to_local(value, params, tz)
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "STATUS"):
                cur[name] = _unescape(value) if name not in ("RRULE",) else value
            elif name == "EXDATE":
                cur["exdates"] += [_to_local(v, params, tz)[0].date() for v in value.split(",") if v]
    out: list[dict] = []
    for ev in events:
        if "DTSTART" not in ev or ev.get("STATUS", "").upper() == "CANCELLED":
            continue
        start, all_day = ev["DTSTART"]
        end = ev.get("DTEND", (start, all_day))[0]
        duration = end - start
        starts = [start]
        if ev.get("RRULE"):
            try:
                rule = rrulestr(ev["RRULE"], dtstart=start)
                starts = list(rule.between(lo, hi, inc=True))[:MAX_EVENTS]
            except (ValueError, TypeError):
                starts = [start]
        for st in starts:
            if not (lo <= st <= hi) or st.date() in ev["exdates"]:
                continue
            title = (ev.get("SUMMARY") or "(no title)").strip()[:200]
            item = {"title": title, "day": st.date(), "uid": (ev.get("UID") or f"{title}@{st.isoformat()}")[:300],
                    "location": (ev.get("LOCATION") or "")[:200], "notes": (ev.get("DESCRIPTION") or "")[:2000],
                    "kind": guess_kind(title), "start": None, "end": None}
            if not all_day and duration > dt.timedelta(0) and (st + duration).date() == st.date():
                item["start"], item["end"] = st.time(), (st + duration).time()
            elif not all_day and duration == dt.timedelta(0):
                item["start"] = None  # zero-length item (e.g. an assignment due time): show as all-day marker
                item["notes"] = (f"Due {st:%-I:%M %p}. " + item["notes"]).strip()
            out.append(item)
            if len(out) >= MAX_EVENTS:
                return out
    return out


def import_ics(s: Session, text: str, today: dt.date, pillar: str | None = None) -> dict:
    items = parse_ics(text, today)
    added = updated = 0
    for it in items:
        row = s.scalar(select(CalendarEvent).where(CalendarEvent.source == "ics",
                                                   CalendarEvent.source_uid == it["uid"],
                                                   CalendarEvent.day == it["day"]))
        if row is None:
            row = CalendarEvent(source="ics", source_uid=it["uid"], day=it["day"], pillar_code=pillar)
            s.add(row)
            added += 1
        else:
            updated += 1
        row.title, row.location, row.notes, row.kind = it["title"], it["location"], it["notes"], it["kind"]
        row.start_time, row.end_time = it["start"], it["end"]
    s.flush()
    return {"added": added, "updated": updated}
