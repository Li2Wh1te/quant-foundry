"""Instrument identity and point-in-time display specification.

The backtesting result layer never links rows by trading code or name: the
only stable association key is ``instrument_id``.  Display fields (code,
name, display name) are only valid at a query-time instant, so they are
resolved through the :class:`InstrumentSpecProvider` protocol when results
are written and then frozen into each result row.  Trading rules and
capability facts belong to the data foundation and are intentionally not
part of this minimal contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.backtesting.domain import DomainValidationError


def _optional_label(value: str | None, field_name: str) -> str | None:
    """Normalize an optional human-readable label; blank text means missing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text when provided")
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Query-time-valid description of one tradable instrument.

    ``instrument_id`` is mandatory and stable across trading-code changes.
    All display fields may be ``None`` for asset protocols that do not
    provide them; an empty display field must never be replaced by the
    trading code.
    """

    instrument_id: UUID
    asset_class: str
    trading_code: str | None = None
    name: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        asset_class = _optional_label(self.asset_class, "asset_class")
        if asset_class is None:
            raise DomainValidationError("asset_class must be non-blank text")
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(
            self, "trading_code", _optional_label(self.trading_code, "trading_code")
        )
        object.__setattr__(self, "name", _optional_label(self.name, "name"))
        object.__setattr__(
            self,
            "display_name",
            _optional_label(self.display_name, "display_name"),
        )


class InstrumentSpecProvider(Protocol):
    """Structural source of query-time-valid instrument descriptions.

    Implementations may be backed by the ORM catalogue, a market-data
    client, or an in-memory fake in tests; result-writing code depends only
    on this protocol.
    """

    def resolve(
        self,
        instrument_id: UUID,
        *,
        as_of: datetime,
    ) -> InstrumentSpec | None:
        """Return the spec valid at ``as_of``, or ``None`` when unknown."""
        ...


__all__ = ["InstrumentSpec", "InstrumentSpecProvider"]
