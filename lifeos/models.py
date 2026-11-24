"""Database schema (SQLAlchemy 2.0). Milestone 1 entities.

All timestamps are naive UTC. Calendar fields (scheduled/due dates) are the user's local dates.
"""

from __future__ import annotations

import datetime as dt

from decimal import Decimal

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from lifeos.config import utcnow


class Base(DeclarativeBase):
    pass


class DecimalText(TypeDecorator):
    """Exact decimals stored as text. SQLite has no decimal type, and floats are never allowed for amounts."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


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

    vision_id: Mapped[int | None] = mapped_column(ForeignKey("visions.id"))

    tasks: Mapped[list["Task"]] = relationship(back_populates="goal")
    vision: Mapped["Vision | None"] = relationship(back_populates="goals")
    objectives: Mapped[list["Objective"]] = relationship(back_populates="goal", order_by="Objective.start_date")


class Vision(Base):
    """Tier 1 - where I want to be in 3-5 years."""

    __tablename__ = "visions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. FIN-V1
    pillar_code: Mapped[str] = mapped_column(ForeignKey("pillars.code"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    target_year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    goals: Mapped[list[Goal]] = relationship(back_populates="vision", order_by="Goal.code")


class Objective(Base):
    """Tier 3 - what this quarter (about 3 months) must deliver toward a goal."""

    __tablename__ = "objectives"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True)  # e.g. FIN-G003-O1
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|achieved|missed|dropped
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    goal: Mapped[Goal] = relationship(back_populates="objectives")
    key_results: Mapped[list["KeyResult"]] = relationship(back_populates="objective", order_by="KeyResult.id")


class KeyResult(Base):
    """Tier 4 - a measurable daily/weekly result that drives an objective."""

    __tablename__ = "key_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(56), unique=True)  # e.g. FIN-G003-O1-KR1
    objective_id: Mapped[int] = mapped_column(ForeignKey("objectives.id"))
    title: Mapped[str] = mapped_column(String(200))
    cadence: Mapped[str] = mapped_column(String(8))           # daily | weekly
    measure: Mapped[str] = mapped_column(String(16))          # completions (of linked tasks) | manual (check-ins)
    target: Mapped[Decimal] = mapped_column(DecimalText)      # per period
    unit: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | retired
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    objective: Mapped[Objective] = relationship(back_populates="key_results")
    tasks: Mapped[list["Task"]] = relationship(back_populates="key_result")
    templates: Mapped[list["RecurringTemplate"]] = relationship(back_populates="key_result")
    checkins: Mapped[list["KeyResultCheckIn"]] = relationship(back_populates="key_result",
                                                              order_by="KeyResultCheckIn.day")


class KeyResultCheckIn(Base):
    __tablename__ = "key_result_checkins"
    id: Mapped[int] = mapped_column(primary_key=True)
    key_result_id: Mapped[int] = mapped_column(ForeignKey("key_results.id"))
    day: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(DecimalText)
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    key_result: Mapped[KeyResult] = relationship(back_populates="checkins")


class PlanEvent(Base):
    """Append-only audit trail for the planning cascade (visions, goals, objectives, key results)."""

    __tablename__ = "plan_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_code: Mapped[str] = mapped_column(String(56))
    event_type: Mapped[str] = mapped_column(String(32))
    from_value: Mapped[str | None] = mapped_column(String(200))
    to_value: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(32), default="user")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


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
    key_result_id: Mapped[int | None] = mapped_column(ForeignKey("key_results.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    occurrences: Mapped[list["Task"]] = relationship(back_populates="template")
    key_result: Mapped[KeyResult | None] = relationship(back_populates="templates")

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
    key_result_id: Mapped[int | None] = mapped_column(ForeignKey("key_results.id"))

    goal: Mapped[Goal | None] = relationship(back_populates="tasks")
    key_result: Mapped[KeyResult | None] = relationship(back_populates="tasks")
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


class CalendarEvent(Base):
    """A one-off block on the calendar: an appointment, exam, study block, or an imported calendar item."""

    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    day: Mapped[dt.date] = mapped_column(Date)
    start_time: Mapped[dt.time | None] = mapped_column(Time)  # None = all day
    end_time: Mapped[dt.time | None] = mapped_column(Time)
    kind: Mapped[str] = mapped_column(String(16), default="other")
    pillar_code: Mapped[str | None] = mapped_column(ForeignKey("pillars.code"))
    location: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | ics
    source_uid: Mapped[str | None] = mapped_column(String(300))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))  # set when turned into a task
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("source", "source_uid", "day", name="uq_imported_event"),)


class EventSeries(Base):
    """A repeating block, e.g. a class every Mon/Wed 9:00-10:30 for the semester. Expanded when read."""

    __tablename__ = "event_series"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="class")
    pillar_code: Mapped[str | None] = mapped_column(ForeignKey("pillars.code"))
    rule: Mapped[str] = mapped_column(String(200))
    frequency_label: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    location: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    exceptions: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WeeklyReview(Base):
    __tablename__ = "weekly_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[dt.date] = mapped_column(Date, unique=True)  # Monday
    wins: Mapped[str] = mapped_column(Text, default="")
    slipped: Mapped[str] = mapped_column(Text, default="")
    lessons: Mapped[str] = mapped_column(Text, default="")
    energy: Mapped[int | None] = mapped_column(Integer)  # 1-5
    focus: Mapped[list] = mapped_column(JSON, default=list)  # top 3 for next week
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # metrics snapshot
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WeeklyOutcome(Base):
    """A finish line for the week: something visibly different by Sunday, proven by evidence."""

    __tablename__ = "weekly_outcomes"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[dt.date] = mapped_column(Date)  # Monday
    title: Mapped[str] = mapped_column(String(200))  # the finish line
    pillar_code: Mapped[str | None] = mapped_column(ForeignKey("pillars.code"))
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    next_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    evidence: Mapped[str] = mapped_column(Text, default="")
    follow_up_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")
    # open | waiting | achieved | partial | missed | dropped | carried
    close_note: Mapped[str] = mapped_column(Text, default="")
    carried_from_id: Mapped[int | None] = mapped_column(ForeignKey("weekly_outcomes.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    next_task: Mapped["Task | None"] = relationship(foreign_keys=[next_task_id])
    goal: Mapped["Goal | None"] = relationship(foreign_keys=[goal_id])


class DailyCheck(Base):
    """The evening check: three questions that plan tomorrow (progress, one ask, capacity)."""

    __tablename__ = "daily_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    for_date: Mapped[dt.date] = mapped_column(Date, unique=True)  # the day being planned
    # 1. Progress
    progress_outcome_id: Mapped[int | None] = mapped_column(ForeignKey("weekly_outcomes.id"))
    progress_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    # 2. Negotiation - one ask (lives here, not in the task queue: it's a message, not gated work)
    no_ask: Mapped[bool] = mapped_column(Boolean, default=False)
    ask_who: Mapped[str] = mapped_column(String(200), default="")
    ask_area: Mapped[str] = mapped_column(String(16), default="")  # price | timing | terms | scope
    ask_what: Mapped[str] = mapped_column(Text, default="")
    ask_script: Mapped[str] = mapped_column(Text, default="")
    ask_result: Mapped[str | None] = mapped_column(String(16))  # got | partial | no | not_asked
    ask_gain: Mapped[str] = mapped_column(Text, default="")
    ask_result_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # 3. Capacity
    protect_note: Mapped[str] = mapped_column(Text, default="")
    awake_minutes: Mapped[int | None] = mapped_column(Integer)
    bump_task_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    progress_outcome: Mapped["WeeklyOutcome | None"] = relationship(foreign_keys=[progress_outcome_id])
    progress_task: Mapped["Task | None"] = relationship(foreign_keys=[progress_task_id])


class Fund(Base):
    """A money bucket: something you're saving for, a debt you're paying down, or spending you're tracking.

    Money is tracked, not moved: entries record what you actually set aside, paid or spent elsewhere."""

    __tablename__ = "funds"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(8))          # save | debt | spend
    space: Mapped[str] = mapped_column(String(16))        # family | holiday | wants | reserves | owed
    target: Mapped[Decimal | None] = mapped_column(DecimalText)  # save: goal · debt: amount owed · spend: monthly budget
    deadline: Mapped[dt.date | None] = mapped_column(Date)
    counterparty: Mapped[str] = mapped_column(String(120), default="")  # who you owe
    kept_where: Mapped[str] = mapped_column(String(120), default="")    # e.g. "Savings account", "envelope"
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="active")   # active | done | dropped
    paid_price: Mapped[Decimal | None] = mapped_column(DecimalText)     # what a want actually cost
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    entries: Mapped[list["FundEntry"]] = relationship(back_populates="fund", order_by="FundEntry.id")


class FundEntry(Base):
    """Append-only money log for a fund."""

    __tablename__ = "fund_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"))
    day: Mapped[dt.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(12))  # add | withdraw | payment | charge | spend | purchase
    amount: Mapped[Decimal] = mapped_column(DecimalText)  # always positive; kind says which way it moves
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    fund: Mapped[Fund] = relationship(back_populates="entries")


class ImpulseCheck(Base):
    """'Before you buy': an item you thought about, its price, and what you decided."""

    __tablename__ = "impulse_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[dt.date] = mapped_column(Date)
    item: Mapped[str] = mapped_column(String(160))
    price: Mapped[Decimal] = mapped_column(DecimalText)
    decision: Mapped[str] = mapped_column(String(12))  # skip | wishlist | buy
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class UserSetting(Base):
    """Small personal settings kept only in your local database (never in Git), e.g. take-home hourly rate."""

    __tablename__ = "user_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AcquisitionCandidate(Base):
    """A used asset under consideration (e.g. a jet ski). Unknown facts stay None / 'unknown' - never guessed."""

    __tablename__ = "acquisition_candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(60), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    manufacturer: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    year: Mapped[int | None] = mapped_column(Integer)
    asking_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    location: Mapped[str] = mapped_column(String(120), default="")
    engine_hours: Mapped[int | None] = mapped_column(Integer)
    hull_condition: Mapped[str] = mapped_column(String(12), default="unknown")        # unknown|good|fair|poor
    mechanical_condition: Mapped[str] = mapped_column(String(12), default="unknown")  # as stated / observed
    title_status: Mapped[str] = mapped_column(String(12), default="unknown")          # unknown|clean|lien|missing|salvage
    title_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    trailer_included: Mapped[str] = mapped_column(String(8), default="unknown")       # unknown|yes|no
    seller_statements: Mapped[str] = mapped_column(Text, default="")
    est_repairs: Mapped[Decimal | None] = mapped_column(DecimalText)
    transport_cost: Mapped[Decimal | None] = mapped_column(DecimalText)
    prep_cost: Mapped[Decimal | None] = mapped_column(DecimalText)  # registration, safety gear, rental-ready work
    rental_suitable: Mapped[str] = mapped_column(String(8), default="unknown")        # unknown|yes|no
    market_value: Mapped[Decimal | None] = mapped_column(DecimalText)  # what similar units really sell for (your research)
    cash_ready: Mapped[bool] = mapped_column(Boolean, default=False)   # you can pay cash and pick up quickly
    rental_note: Mapped[str] = mapped_column(Text, default="")
    inspection: Mapped[dict] = mapped_column(JSON, default=dict)  # item -> {"result": pass|fail|na, "note": ""}
    funding_fund_id: Mapped[int | None] = mapped_column(ForeignKey("funds.id"))
    stage: Mapped[str] = mapped_column(String(12), default="found")
    # found | contacted | inspecting | negotiating | approved | owned | passed
    offer: Mapped[Decimal | None] = mapped_column(DecimalText)
    agreed_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    first_seen: Mapped[dt.date] = mapped_column(Date)
    last_verified: Mapped[dt.date | None] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    price_history: Mapped[list["CandidatePrice"]] = relationship(back_populates="candidate",
                                                                 order_by="CandidatePrice.day")
    funding_fund: Mapped["Fund | None"] = relationship(foreign_keys=[funding_fund_id])


class CandidatePrice(Base):
    __tablename__ = "candidate_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("acquisition_candidates.id"))
    day: Mapped[dt.date] = mapped_column(Date)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    note: Mapped[str] = mapped_column(Text, default="")

    candidate: Mapped[AcquisitionCandidate] = relationship(back_populates="price_history")


# ---------------------------------------------------------------- Personal University (progress only - content is YAML)
class UnitProgress(Base):
    __tablename__ = "uni_unit_progress"
    unit_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    started_on: Mapped[dt.date | None] = mapped_column(Date)
    recall_text: Mapped[str] = mapped_column(Text, default="")
    recall_hits: Mapped[list] = mapped_column(JSON, default=list)
    recall_on: Mapped[dt.date | None] = mapped_column(Date)
    evidence: Mapped[str] = mapped_column(Text, default="")
    evidence_runs: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_on: Mapped[dt.date | None] = mapped_column(Date)


class QuizAttempt(Base):
    __tablename__ = "uni_quiz_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_key: Mapped[str] = mapped_column(String(40))
    q_index: Mapped[int] = mapped_column(Integer)
    day: Mapped[dt.date] = mapped_column(Date)
    correct: Mapped[bool] = mapped_column(Boolean)
    answer: Mapped[str] = mapped_column(Text, default="")


class CardState(Base):
    __tablename__ = "uni_card_state"
    card_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    unit_key: Mapped[str] = mapped_column(String(40))
    box: Mapped[int] = mapped_column(Integer, default=0)
    due: Mapped[dt.date] = mapped_column(Date)
    reviews: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    last_reviewed: Mapped[dt.date | None] = mapped_column(Date)


class CardReview(Base):
    __tablename__ = "uni_card_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    card_key: Mapped[str] = mapped_column(String(200))
    unit_key: Mapped[str] = mapped_column(String(40))
    day: Mapped[dt.date] = mapped_column(Date)
    correct: Mapped[bool] = mapped_column(Boolean)


class SourceCheck(Base):
    __tablename__ = "uni_source_checks"
    source_key: Mapped[str] = mapped_column(String(60), primary_key=True)
    checked_on: Mapped[dt.date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, default="")


class ReadingEntry(Base):
    __tablename__ = "uni_reading_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str | None] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="reading")  # reading | done
    takeaways: Mapped[list] = mapped_column(JSON, default=list)
    action: Mapped[str] = mapped_column(Text, default="")
    started_on: Mapped[dt.date] = mapped_column(Date)
    finished_on: Mapped[dt.date | None] = mapped_column(Date)


class Inquiry(Base):
    __tablename__ = "uni_inquiries"
    id: Mapped[int] = mapped_column(primary_key=True)
    seed_key: Mapped[str | None] = mapped_column(String(40), unique=True)
    topic: Mapped[str] = mapped_column(String(160))
    question: Mapped[str] = mapped_column(Text)
    sources_used: Mapped[str] = mapped_column(Text, default="")
    conclusion: Mapped[str] = mapped_column(Text, default="")
    would_change_mind: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | concluded
    created_on: Mapped[dt.date] = mapped_column(Date)
    concluded_on: Mapped[dt.date | None] = mapped_column(Date)


class ExamPlan(Base):
    __tablename__ = "uni_exam_plans"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    test_date: Mapped[dt.date | None] = mapped_column(Date)
    target_score: Mapped[str] = mapped_column(String(40), default="")


class PracticeScore(Base):
    __tablename__ = "uni_practice_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_code: Mapped[str] = mapped_column(String(20))
    day: Mapped[dt.date] = mapped_column(Date)
    score: Mapped[Decimal] = mapped_column(DecimalText)
    max_score: Mapped[Decimal | None] = mapped_column(DecimalText)
    note: Mapped[str] = mapped_column(Text, default="")


class HandsOnRun(Base):
    __tablename__ = "uni_hands_on"
    id: Mapped[int] = mapped_column(primary_key=True)
    checklist_key: Mapped[str] = mapped_column(String(40))
    day: Mapped[dt.date] = mapped_column(Date)
    steps_done: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[str] = mapped_column(Text, default="")
