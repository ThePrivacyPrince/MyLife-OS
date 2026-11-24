"""Learning rules - pure functions: spaced review (Leitner boxes) and evidence-based mastery.

Mastery is earned, never granted for opening a lesson:
  introduced -> practiced -> recalled (correct 7+ days after you started) -> applied (real evidence)
  -> mastered (applied + recalled + two correct reviews at least 21 days apart).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

INTERVALS = (1, 3, 7, 14, 30, 60, 120)   # days until the next review, by box
SECONDS_PER_CARD = 30
RECALL_DELAY_DAYS = 7
SPACING_DAYS = 21
LEVELS = ("none", "introduced", "practiced", "recalled", "applied", "mastered")
LEVEL_ICON = {"none": "⚪", "introduced": "🔹", "practiced": "🔸", "recalled": "🟡", "applied": "🟢", "mastered": "🏅"}


def next_review(box: int, correct: bool, today: dt.date) -> tuple[int, dt.date]:
    """Correct: move up a box (longer gap). Missed: back to box 0, see it tomorrow."""
    if not correct:
        return 0, today + dt.timedelta(days=INTERVALS[0])
    nb = min(box + 1, len(INTERVALS) - 1)
    return nb, today + dt.timedelta(days=INTERVALS[nb])


def daily_limit(minutes: int, low_capacity: bool) -> int:
    """How many cards fit the daily budget; on a tight day only the most overdue few."""
    n = max(1, (minutes * 60) // SECONDS_PER_CARD)
    return min(n, 8) if low_capacity else n


@dataclass(frozen=True)
class UnitEvidence:
    first_activity: dt.date | None
    attempted: bool                       # any quiz answer or card review
    correct_days: tuple[dt.date, ...]     # days with a correct quiz answer or card review
    applied: bool                         # real-world evidence saved


def mastery(ev: UnitEvidence) -> str:
    if ev.first_activity is None and not ev.attempted and not ev.applied:
        return "none"
    recalled = ev.first_activity is not None and any(
        (d - ev.first_activity).days >= RECALL_DELAY_DAYS for d in ev.correct_days)
    spaced = False
    days = sorted(set(ev.correct_days))
    if len(days) >= 2:
        spaced = (days[-1] - days[0]).days >= SPACING_DAYS
    if ev.applied and recalled and spaced:
        return "mastered"
    if ev.applied:
        return "applied"
    if recalled:
        return "recalled"
    if ev.attempted:
        return "practiced"
    return "introduced"
