"""Recurring-task schedule rules (RFC 5545 RRULE via python-dateutil). Pure functions."""

from __future__ import annotations

import datetime as dt
from typing import Iterable

from dateutil.rrule import rrulestr

from lifeos.errors import ValidationError

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQUENCIES = ("daily", "weekdays", "weekly", "biweekly", "monthly", "custom")


def build_rule(
    frequency: str,
    *,
    weekdays: Iterable[str] | None = None,
    month_day: int | None = None,
    custom: str | None = None,
) -> str:
    """Build an RRULE string from a friendly frequency name."""
    days = [d.upper()[:2] for d in (weekdays or [])]
    bad = [d for d in days if d not in WEEKDAY_CODES]
    if bad:
        raise ValidationError(f"Unknown weekday code(s): {bad}. Use {WEEKDAY_CODES}.")
    byday = f";BYDAY={','.join(days)}" if days else ""
    match frequency:
        case "daily":
            rule = "FREQ=DAILY"
        case "weekdays":
            if not days:
                raise ValidationError("'weekdays' frequency needs at least one weekday.")
            rule = f"FREQ=WEEKLY{byday}"
        case "weekly":
            rule = f"FREQ=WEEKLY{byday}"
        case "biweekly":
            rule = f"FREQ=WEEKLY;INTERVAL=2{byday}"
        case "monthly":
            rule = "FREQ=MONTHLY" + (f";BYMONTHDAY={month_day}" if month_day else "")
        case "custom":
            if not custom:
                raise ValidationError("Custom frequency needs an RRULE string.")
            rule = custom.removeprefix("RRULE:")
        case _:
            raise ValidationError(f"Unknown frequency '{frequency}'. Use one of {FREQUENCIES}.")
    validate_rule(rule, dt.date.today())
    return rule


def validate_rule(rule: str, start: dt.date) -> None:
    if "COUNT" in rule.upper() and "UNTIL" in rule.upper():
        raise ValidationError("RRULE cannot have both COUNT and UNTIL.")
    try:
        rrulestr(rule, dtstart=_as_dt(start))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid recurrence rule '{rule}': {exc}") from exc


def occurrence_dates(
    rule: str,
    start: dt.date,
    window_start: dt.date,
    window_end: dt.date,
    *,
    end_date: dt.date | None = None,
    exceptions: Iterable[dt.date] = (),
) -> list[dt.date]:
    """Dates the rule produces within [window_start, window_end], honoring end_date and exceptions."""
    lo = max(start, window_start)
    hi = min(window_end, end_date) if end_date else window_end
    if hi < lo:
        return []
    excluded = set(exceptions)
    rr = rrulestr(rule, dtstart=_as_dt(start))
    return [d.date() for d in rr.between(_as_dt(lo), _as_dt(hi), inc=True) if d.date() not in excluded]


def _as_dt(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day)
