"""Domain errors. Every rule violation raises; nothing fails silently."""

from __future__ import annotations


class LifeOSError(Exception):
    """Base class for all LIFE OS domain errors."""


class ValidationError(LifeOSError):
    """Input failed validation."""


class NotFound(LifeOSError):
    """Referenced record does not exist."""


class InvalidTransition(LifeOSError):
    """A task status change is not allowed by the state machine."""


class ReasonRequired(LifeOSError):
    """The action requires a documented reason."""


class DependencyError(LifeOSError):
    """Dependency rule violated (cycle, self-reference, unmet prerequisite)."""


class GateViolation(LifeOSError):
    """Priority Assurance Framework blocked the action."""

    def __init__(self, decision):  # decision: lifeos.engines.priority.GateDecision
        self.decision = decision
        super().__init__(decision.reason)
