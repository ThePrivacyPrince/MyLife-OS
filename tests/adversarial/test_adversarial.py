"""ADV-* : hostile, malformed and edge-case inputs."""
import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

from lifeos.enums import TaskStatus as S
from lifeos.errors import GateViolation, InvalidTransition, ValidationError
from lifeos.models import Task
from lifeos.services import recurring as R
from lifeos.services import tasks as T


def test_adv01_sql_and_markup_in_titles_stored_literally(s, mk):
    nasty = "'); DROP TABLE tasks; -- <script>alert(1)</script>"
    t = mk(nasty)
    s.commit()
    assert s.get(Task, t.id).title == nasty
    assert s.execute(text("select count(*) from tasks")).scalar() == 1


@pytest.mark.parametrize("rule", ["FREQ=DAILY;COUNT=3;UNTIL=20270101", "garbage", "FREQ=SECONDLY;BYSETPOS=-999x"])
def test_adv02_malformed_recurrence_rules_rejected(s, today, rule):
    with pytest.raises(ValidationError):
        R.create_template(s, title="x", pillar="ENV", priority=2, frequency="custom", custom_rule=rule,
                          start_date=today)


def test_adv03_whitespace_override_cannot_bypass_gate(s, mk, today):
    mk("Critical", priority=1)
    t = mk("Optional", priority=3)
    with pytest.raises(GateViolation):
        T.transition(s, t.id, S.IN_PROGRESS, override_reason="\t \n", today=today)


def test_adv04_cannot_jump_from_cancelled_to_completed(s, mk, today):
    t = mk()
    T.transition(s, t.id, S.CANCELLED, reason="nope", today=today)
    with pytest.raises(InvalidTransition):
        T.transition(s, t.id, S.COMPLETED, today=today)


def test_adv05_database_constraints_backstop_the_services(s, today):
    s.add(Task(title="x", pillar_code="ENV", priority=7))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    s.add(Task(title="x", pillar_code="NOPE", priority=1))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()


def test_adv06_duplicate_occurrence_rejected_by_db(s, today):
    tpl = R.create_template(s, title="x", pillar="ENV", priority=2, frequency="daily", start_date=today)
    R.generate_occurrences(s, today)
    s.add(Task(title="x", pillar_code="ENV", priority=2, kind="occurrence", template_id=tpl.id,
               occurrence_date=today))
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()


def test_adv07_conflicting_dates_rejected(mk, today):
    with pytest.raises(ValidationError):
        mk(scheduled_date=today + dt.timedelta(days=5), due_date=today)


def test_adv08_unknown_status_rejected(s, mk, today):
    t = mk()
    with pytest.raises(ValueError):
        T.transition(s, t.id, "done-ish", today=today)
