"""Lossless display fields bound to a stable instrument identity."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from app.backtest_service.config_values import DomainValidationError, _aware_datetime
from app.instruments.domain import InstrumentDisplay, InstrumentDisplayProvider

def _uuid(value: UUID, field_name: str) -> UUID:
    """Require a real UUID; identity fields are never free-form strings."""

    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    """Normalize optional text; blank strings are treated as missing."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text when provided")
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class InstrumentDisplaySnapshot:
    """Point-in-time display fields frozen when a result row is written.

    ``instrument_id`` is the only association key.  The display fields are
    optional for every asset protocol and are never used to substitute the
    stable identity.  Historical queries read these frozen values; they must
    not be refreshed from today's catalogue.
    """

    instrument_id: UUID
    event_trading_code: str | None = None
    event_name: str | None = None
    event_display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _uuid(self.instrument_id, "instrument_id"))
        object.__setattr__(
            self,
            "event_trading_code",
            _optional_text(self.event_trading_code, "event_trading_code"),
        )
        object.__setattr__(
            self, "event_name", _optional_text(self.event_name, "event_name")
        )
        object.__setattr__(
            self,
            "event_display_name",
            _optional_text(self.event_display_name, "event_display_name"),
        )

    @classmethod
    def from_display(cls, display: InstrumentDisplay) -> "InstrumentDisplaySnapshot":
        """Freeze the display fields of a point-in-time-valid display object."""

        return cls(
            instrument_id=display.instrument_id,
            event_trading_code=display.trading_code,
            event_name=display.name,
            event_display_name=display.display_name,
        )

    def require_matching_instrument(self, instrument_id: UUID, field_name: str) -> None:
        """Reject snapshots belonging to a different instrument."""

        if self.instrument_id != instrument_id:
            raise DomainValidationError(
                f"{field_name} snapshot instrument_id must match the row instrument_id"
            )


def resolve_display_snapshot(
    provider: InstrumentDisplayProvider,
    instrument_id: UUID,
    *,
    effective_at: datetime,
    data_cutoff: datetime,
) -> InstrumentDisplaySnapshot:
    """Resolve display fields from a provider for one market instant.

    ``effective_at`` selects the market instant the display info must be
    valid at; ``data_cutoff`` limits the knowledge the provider may use.
    The two timestamps are intentionally separate parameters — never merge
    them back into one ambiguous ``as_of``.  A missing display is not an
    error: asset protocols may not expose display fields, so the snapshot
    simply stays empty while ``instrument_id`` keeps carrying the identity.
    Result repositories depend on the caller (or this helper) rather than
    on any concrete market-data client.
    """

    _aware_datetime(effective_at, "effective_at")
    _aware_datetime(data_cutoff, "data_cutoff")
    display = provider.resolve_display(
        _uuid(instrument_id, "instrument_id"),
        effective_at=effective_at,
        data_cutoff=data_cutoff,
    )
    if display is None:
        return InstrumentDisplaySnapshot(instrument_id=instrument_id)
    if display.instrument_id != instrument_id:
        raise DomainValidationError("provider returned a mismatched instrument_id")
    return InstrumentDisplaySnapshot.from_display(display)
