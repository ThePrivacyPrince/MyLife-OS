"""Countdowns and timelines - pure rules.

A deadline is a commitment: it counts down every day, and it can only move with a recorded reason.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def vision_deadline(target_year: int | None) -> dt.date | None:
    return dt.date(target_year, 12, 31) if target_year else None


def humanize_days(days: int) -> str:
    days = abs(days)
    if days < 60:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 730:
        months, rem = divmod(days, 30)
        return f"{months} months" + (f", {rem} day{'s' if rem != 1 else ''}" if rem and months < 6 else "")
    years, rem = divmod(days, 365)
    months = rem // 30
    return f"{years} years" + (f", {months} month{'s' if months != 1 else ''}" if months else "")


@dataclass(frozen=True)
class Countdown:
    start: dt.date
    deadline: dt.date | None
    today: dt.date
    due_soon_days: int = 14

    @property
    def days_left(self) -> int | None:
        return None if self.deadline is None else (self.deadline - self.today).days

    @property
    def total_days(self) -> int | None:
        return None if self.deadline is None else max((self.deadline - self.start).days, 1)

    @property
    def elapsed_fraction(self) -> float:
        """Share of the time window already used (0..1)."""
        if self.deadline is None or self.today <= self.start:
            return 0.0
        return min((self.today - self.start).days / self.total_days, 1.0)

    @property
    def state(self) -> str:
        if self.deadline is None:
            return "no_deadline"
        if self.today < self.start:
            return "not_started"
        left = self.days_left
        if left < 0:
            return "overdue"
        if left == 0:
            return "due_today"
        if left <= self.due_soon_days:
            return "due_soon"
        return "on_clock"

    @property
    def label(self) -> str:
        match self.state:
            case "no_deadline":
                return "No deadline set"
            case "not_started":
                return f"Starts in {humanize_days((self.start - self.today).days)}"
            case "overdue":
                return f"{humanize_days(self.days_left)} past deadline"
            case "due_today":
                return "Due today"
            case _:
                return f"{humanize_days(self.days_left)} left ({self.days_left} days)"


def countdown(start: dt.date, deadline: dt.date | None, today: dt.date, due_soon_days: int = 14) -> Countdown:
    return Countdown(min(start, deadline) if deadline else start, deadline, today, due_soon_days)


def quarter_slots(start: dt.date, deadline: dt.date, length_days: int = 91) -> list[tuple[dt.date, dt.date]]:
    """Split [start, deadline] into back-to-back ~3-month slots; the last one ends on the deadline."""
    if deadline < start:
        return []
    slots, lo = [], start
    while lo <= deadline:
        hi = min(lo + dt.timedelta(days=length_days - 1), deadline)
        # Don't leave a stub of under 3 weeks at the end - fold it into the last slot.
        if (deadline - hi).days < 21:
            hi = deadline
        slots.append((lo, hi))
        lo = hi + dt.timedelta(days=1)
    return slots
