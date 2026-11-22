"""The planning cascade - pure rules.

    3-5 years      VISION        where I want to be
        ^
    6-12 months    GOALS         what has to be true within a year
        ^
    3 months       OBJECTIVES    what this quarter must deliver
        ^
    daily/weekly   KEY RESULTS   the measurable things I do every day/week
        ^
                   tasks & routines (linked to a key result)

Every arrow points UP: each tier exists to serve the one above it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from lifeos.errors import ValidationError

TIERS = ("vision", "goal", "objective", "key_result")
TIER_LABELS = {
    "vision": "Vision (3-5 years)",
    "goal": "Goal (6-12 months)",
    "objective": "Objective (3 months)",
    "key_result": "Key result (daily/weekly)",
}
CADENCES = ("daily", "weekly")
MEASURES = ("completions", "manual")


def months_between(a: dt.date, b: dt.date) -> float:
    return (b - a).days / 30.44


def vision_horizon_warnings(today: dt.date, target_year: int | None, min_years: int, max_years: int) -> list[str]:
    if target_year is None:
        return ["No target year set - a vision is easier to plan toward with a horizon (3-5 years)."]
    years = target_year - today.year
    if years < min_years:
        return [f"Target year {target_year} is only {years} year(s) out - that is closer to a goal than a "
                f"{min_years}-{max_years} year vision."]
    if years > max_years:
        return [f"Target year {target_year} is {years} years out - beyond the {min_years}-{max_years} year vision horizon."]
    return []


def goal_horizon_warnings(today: dt.date, target_date: dt.date | None, min_months: int, max_months: int) -> list[str]:
    if target_date is None:
        return ["No target date - goals on the board are 6-12 months out."]
    if target_date <= today:
        return ["Target date is not in the future."]
    m = months_between(today, target_date)
    if m < min_months:
        return [f"Target is about {m:.0f} month(s) away - that's objective-sized (3 months). "
                "Consider making it an objective under a bigger goal."]
    if m > max_months:
        return [f"Target is about {m:.0f} months away - beyond a 6-12 month goal. Consider splitting it, "
                "or treating the long-range part as part of your vision."]
    return []


def validate_objective_window(start: dt.date, end: dt.date, max_days: int) -> None:
    if end < start:
        raise ValidationError("Objective ends before it starts.")
    if (end - start).days > max_days:
        raise ValidationError(f"Objectives cover about 3 months (max {max_days} days). "
                              "Anything longer belongs at the goal level.")


def period_bounds(cadence: str, day: dt.date) -> tuple[dt.date, dt.date]:
    """Inclusive bounds of the daily or weekly (Monday-Sunday) period containing `day`."""
    if cadence == "daily":
        return day, day
    if cadence == "weekly":
        start = day - dt.timedelta(days=day.weekday())
        return start, start + dt.timedelta(days=6)
    raise ValidationError(f"Unknown cadence '{cadence}'. Use {CADENCES}.")


def periods_in(cadence: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """All periods overlapping [start, end], in order."""
    out, cursor = [], start
    while cursor <= end:
        lo, hi = period_bounds(cadence, cursor)
        out.append((lo, hi))
        cursor = hi + dt.timedelta(days=1)
    return out


@dataclass(frozen=True)
class KRStatus:
    actual: Decimal
    target: Decimal
    status: str  # met | on_track | behind | missed | no_target

    @property
    def fraction(self) -> float:
        if self.target <= 0:
            return 0.0
        return float(min(self.actual / self.target, Decimal(1)))


def kr_status(actual: Decimal, target: Decimal, period: tuple[dt.date, dt.date], today: dt.date) -> KRStatus:
    """Met if the period's target is reached; otherwise compare against how much of the period has elapsed."""
    if target <= 0:
        return KRStatus(actual, target, "no_target")
    if actual >= target:
        return KRStatus(actual, target, "met")
    lo, hi = period
    if today > hi:
        return KRStatus(actual, target, "missed")
    days = (hi - lo).days + 1
    finished_days = (today - lo).days  # today isn't over yet, so it doesn't count against you
    expected = target * Decimal(finished_days) / Decimal(days)
    # Daily KRs and the first part of a week can't be "behind" until the period is over.
    if days == 1 or actual >= expected:
        return KRStatus(actual, target, "on_track")
    return KRStatus(actual, target, "behind")


def parse_amount(value) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except Exception as exc:
        raise ValidationError(f"'{value}' is not a number.") from exc
    if not d.is_finite():
        raise ValidationError("Amount must be a finite number.")
    return d
