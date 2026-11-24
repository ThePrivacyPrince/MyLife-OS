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
