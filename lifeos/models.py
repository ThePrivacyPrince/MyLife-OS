"""Database schema (SQLAlchemy 2.0). Milestone 1 entities.

All timestamps are naive UTC. Calendar fields (scheduled/due dates) are the user's local dates.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from lifeos.config import utcnow


class Base(DeclarativeBase):
    pass


class Pillar(Base):
    __tablename__ = "pillars"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(Text, default="")
    subdomains: Mapped[list] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. FIN-G001
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    pillar_code: Mapped[str] = mapped_column(ForeignKey("pillars.code"))
    secondary_pillars: Mapped[list] = mapped_column(JSON, default=list)
    target_metric: Mapped[str | None] = mapped_column(String(200))
    baseline: Mapped[str | None] = mapped_column(String(200))
    target_value: Mapped[str | None] = mapped_column(String(200))
    current_value: Mapped[str | None] = mapped_column(String(200))  # unknown stays None, never invented
    progress_evidence: Mapped[str] = mapped_column(Text, default="")  # how progress is demonstrated
    target_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    tasks: Mapped[list["Task"]] = relationship(back_populates="goal")


class RecurringTemplate(Base):
    """A schedule that generates independent TaskOccurrence rows (Task.kind == 'occurrence')."""

    __tablename__ = "recurring_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    pillar_code: Mapped[str] = mapped_column(ForeignKey("pillars.code"))
    priority: Mapped[int] = mapped_column(Integer)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    rule: Mapped[str] = mapped_column(String(200))  # RRULE
    frequency_label: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    exceptions: Mapped[list] = mapped_column(JSON, default=list)  # ISO dates to skip
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    missed_policy: Mapped[str] = mapped_column(String(16), default="mark_missed")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_required: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    generated_through: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    occurrences: Mapped[list["Task"]] = relationship(back_populates="template")

    __table_args__ = (CheckConstraint("priority BETWEEN 1 AND 3", name="ck_template_priority"),)


class Task(Base):
    """One unified task table so one-time tasks and recurring occurrences share one priority queue."""

    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    pillar_code: Mapped[str] = mapped_column(ForeignKey("pillars.code"))
    priority: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="one_time")
    template_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_templates.id"))
    occurrence_date: Mapped[dt.date | None] = mapped_column(Date)
    scheduled_date: Mapped[dt.date | None] = mapped_column(Date)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    status_reason: Mapped[str | None] = mapped_column(Text)
    follow_up_date: Mapped[dt.date | None] = mapped_column(Date)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    evidence_required: Mapped[bool] = mapped_column(Boolean, default=False)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    goal: Mapped[Goal | None] = relationship(back_populates="tasks")
    template: Mapped[RecurringTemplate | None] = relationship(back_populates="occurrences")
    events: Mapped[list["TaskEvent"]] = relationship(back_populates="task", order_by="TaskEvent.id")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="task", order_by="Evidence.id")
    prerequisites: Mapped[list["TaskDependency"]] = relationship(
        foreign_keys="TaskDependency.task_id", back_populates="task"
    )

    __table_args__ = (
        UniqueConstraint("template_id", "occurrence_date", name="uq_occurrence_per_day"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="ck_task_priority"),
    )

    @property
    def effective_date(self) -> dt.date | None:
        return self.scheduled_date or self.due_date

    @property
    def is_recurring(self) -> bool:
        return self.kind == "occurrence"


class TaskDependency(Base):
    __tablename__ = "task_dependencies"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    depends_on_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[Task] = relationship(foreign_keys=[task_id], back_populates="prerequisites")
    depends_on: Mapped[Task] = relationship(foreign_keys=[depends_on_id])

    __table_args__ = (UniqueConstraint("task_id", "depends_on_id", name="uq_dependency"),)


class TaskEvent(Base):
    """Append-only audit trail. Never updated or deleted."""

    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    from_value: Mapped[str | None] = mapped_column(String(200))
    to_value: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(32), default="user")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[Task] = relationship(back_populates="events")


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    description: Mapped[str] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(String(500))
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[Task] = relationship(back_populates="evidence")


class DayReview(Base):
    __tablename__ = "day_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True)
    note: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
