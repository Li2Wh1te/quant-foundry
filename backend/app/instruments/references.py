"""Dependency-free versioned reference values shared across domains.

This module deliberately imports nothing heavier than the stdlib-only leaf
``app.backtesting.domain``.  The generic backtesting data contract
(``app.backtesting.data``) names its versioned policies through
:class:`VersionedReference`, so the type must live below the heavy reverse
dependencies -- keeping it here (together with the lazy exports of
``app.backtesting.data``) is what breaks the ``instruments`` <->
``backtesting`` import cycle.  Nothing in this file may grow an import of
the data contract or of other instrument modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backtesting.domain import DomainValidationError

__all__ = [
    "VersionedReference",
]


def _required_label(value: object, field_name: str) -> str:
    """Require non-blank plain text for one reference field."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return value


@dataclass(frozen=True, slots=True)
class VersionedReference:
    """An immutable pointer to a versioned definition owned by another domain.

    Instrument specs reference trading-session templates this way instead of
    embedding concrete session times; the generic data contract uses the
    same shape for its frozen policy keys (chunk policy, calendar-axis
    policy, token contracts).
    """

    key: str
    version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _required_label(self.key, "key"))
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise DomainValidationError("version must be an integer")
        if self.version < 1:
            raise DomainValidationError("version must be a positive integer")

