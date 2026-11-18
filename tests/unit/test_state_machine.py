"""SM-* : task state machine."""
import itertools

import pytest
from hypothesis import given, strategies as st

from lifeos.engines.state_machine import REASON_REQUIRED_TARGETS, TRANSITIONS, requires_reason, validate_transition
from lifeos.enums import TaskStatus as S
from lifeos.errors import InvalidTransition, ReasonRequired

ALL = list(S)


@pytest.mark.parametrize("current,target", list(itertools.product(ALL, ALL)))
def test_sm01_transition_table_is_enforced(current, target):
    if target in TRANSITIONS[current]:
        validate_transition(current, target, "because")
    else:
        with pytest.raises(InvalidTransition):
            validate_transition(current, target, "because")


@pytest.mark.parametrize("target", sorted(REASON_REQUIRED_TARGETS))
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_sm02_reason_required(target, reason):
    current = S.PENDING if target != S.MISSED else S.PENDING
    with pytest.raises(ReasonRequired):
        validate_transition(current, target, reason)


@pytest.mark.parametrize("closed", [S.COMPLETED, S.CANCELLED, S.MISSED])
def test_sm03_reopen_requires_reason(closed):
    assert requires_reason(closed, S.PENDING)
    with pytest.raises(ReasonRequired):
        validate_transition(closed, S.PENDING, None)


def test_sm04_completion_needs_no_reason():
    validate_transition(S.PENDING, S.COMPLETED, None)


@given(st.lists(st.sampled_from(ALL), min_size=1, max_size=30))
def test_sm05_random_walks_only_reach_valid_states(path):
    state = S.PENDING
    for target in path:
        try:
            validate_transition(state, target, "r")
        except InvalidTransition:
            assert target not in TRANSITIONS[state]
            continue
        assert target in TRANSITIONS[state]
        state = target
    assert state in S
