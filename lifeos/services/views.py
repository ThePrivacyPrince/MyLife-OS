"""Task views: group open work by pillar (domain), then by when it needs to happen."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable

from lifeos.enums import TaskStatus as S
from lifeos.models import Task

BUCKETS = ("overdue", "this_week", "later", "no_date", "waiting")
BUCKET_LABELS = {"overdue": "🔴 Overdue", "this_week": "📅 This week", "later": "🗓️ Later",
                 "no_date": "📥 No date", "waiting": "⏳ Waiting / blocked"}
WORKABLE = (S.PENDING, S.IN_PROGRESS)


def bucket_of(t: Task, today: dt.date) -> str | None:
    """Which section an open task belongs in. None for closed tasks."""
    if t.status in (S.WAITING, S.BLOCKED):
        return "waiting"
    if t.status == S.DEFERRED:
        return "later"
    if t.status not in WORKABLE:
        return None
    if t.due_date and t.due_date < today:
        return "overdue"
    when = t.effective_date
    if when is None:
        return "no_date"
    week_end = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=6)
    return "this_week" if when <= week_end else "later"


@dataclass
class PillarGroup:
    code: str
    buckets: dict[str, list[Task]] = field(default_factory=lambda: {b: [] for b in BUCKETS})

    @property
    def open_count(self) -> int:
        return sum(len(v) for v in self.buckets.values())

    @property
    def p1_count(self) -> int:
        return sum(1 for v in self.buckets.values() for t in v if t.priority == 1)

    @property
    def overdue_count(self) -> int:
        return len(self.buckets["overdue"])

    @property
    def urgent(self) -> bool:
        return self.overdue_count > 0 or any(t.priority == 1 for t in self.buckets["this_week"])


def group_by_pillar(tasks: Iterable[Task], pillar_codes: list[str], today: dt.date) -> list[PillarGroup]:
    groups = {c: PillarGroup(c) for c in pillar_codes}
    for t in tasks:
        b = bucket_of(t, today)
        if b is None or t.protected:
            continue
        groups.setdefault(t.pillar_code, PillarGroup(t.pillar_code)).buckets[b].append(t)
    for g in groups.values():
        for b, items in g.buckets.items():
            items.sort(key=lambda t: (t.priority, t.due_date or dt.date.max, t.effective_date or dt.date.max, t.id))
    return [groups[c] for c in pillar_codes if c in groups] + \
        [g for c, g in groups.items() if c not in pillar_codes]


# ---------------------------------------------------------------- timeline by pillar
def quarter_label(d: dt.date) -> str:
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def quarter_start(d: dt.date) -> dt.date:
    return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


@dataclass
class PillarTimeline:
    code: str
    deadlines: list = field(default_factory=list)      # cascade Deadline objects, soonest first
    undated_goals: list = field(default_factory=list)  # active goals with no deadline yet

    def next_up(self, today: dt.date):
        upcoming = [d for d in self.deadlines if d.date >= today]
        return upcoming[0] if upcoming else None

    def overdue(self, today: dt.date) -> list:
        return [d for d in self.deadlines if d.date < today]

    def due_within(self, today: dt.date, days: int) -> list:
        return [d for d in self.deadlines if today <= d.date <= today + dt.timedelta(days=days)]


def pillar_of_code(code: str) -> str:
    return code.split("-", 1)[0]


def timeline_by_pillar(s, pillar_codes: list[str], today: dt.date) -> list[PillarTimeline]:
    from sqlalchemy import select
    from lifeos.models import Goal
    from lifeos.services.cascade import upcoming_deadlines
    out = {c: PillarTimeline(c) for c in pillar_codes}
    for d in upcoming_deadlines(s, today):
        p = pillar_of_code(d.code)
        if p in out:
            out[p].deadlines.append(d)
    for g in s.scalars(select(Goal).where(Goal.status == "active", Goal.target_date.is_(None)).order_by(Goal.code)):
        if g.pillar_code in out:
            out[g.pillar_code].undated_goals.append(g)
    return [out[c] for c in pillar_codes]
