"""Only the visibility boundary required by shared named-calendar storage."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.backtest_service.config_values import _aware_datetime
from app.data_contracts.errors import DataCutoffExceededError, InvalidDataRequestError

def derive_cutoff_local_date(data_cutoff: datetime, timezone_name: str) -> date:
    """Derive a market-local cutoff date from a timezone-aware instant."""

    if not isinstance(data_cutoff, datetime) or data_cutoff.tzinfo is None or data_cutoff.utcoffset() is None:
        raise InvalidDataRequestError("data_cutoff must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidDataRequestError("timezone must be a resolvable IANA timezone") from exc
    return data_cutoff.astimezone(zone).date()


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject full datetimes."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidDataRequestError(f"{field_name} must be a datetime.date")
    return value


@dataclass(frozen=True, slots=True)
class QueryBoundary:
    """The point-in-time visibility boundary carried by market-fact queries.

    ``data_cutoff`` is mandatory: no query may read facts recorded after it,
    and overflow fails instead of silently trimming.  Strict historical
    cognition additionally supplies ``knowledge_as_of``, which must not be
    later than ``data_cutoff``.  Both timestamps must be timezone-aware.

    Date-window queries need an explicit rule for the cutoff day itself,
    because a bare ``data_cutoff`` instant cannot tell whether the day's
    facts are already complete.  ``include_cutoff_day`` encodes that
    decision: the default ``False`` means the cutoff day counts as
    incomplete and any date window touching it fails; ``True`` means the
    caller has established that the whole cutoff day is readable.
    """

    data_cutoff: datetime
    knowledge_as_of: datetime | None = None
    include_cutoff_day: bool = False

    def __post_init__(self) -> None:
        cutoff = _aware_datetime(self.data_cutoff, "data_cutoff")
        object.__setattr__(self, "data_cutoff", cutoff)
        if not isinstance(self.include_cutoff_day, bool):
            raise InvalidDataRequestError(
                "include_cutoff_day must be a boolean"
            )
        if self.knowledge_as_of is not None:
            known = _aware_datetime(self.knowledge_as_of, "knowledge_as_of")
            if known > cutoff:
                raise InvalidDataRequestError(
                    "knowledge_as_of must not be later than data_cutoff"
                )
            object.__setattr__(self, "knowledge_as_of", known)

    def derive_cutoff_local_date(self, timezone_name: str) -> date:
        """Derive the frozen cutoff date in one confirmed IANA timezone."""

        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidDataRequestError(
                "timezone must be a resolvable IANA timezone"
            ) from exc
        return self.data_cutoff.astimezone(zone).date()

    @property
    def cutoff_date(self) -> date:
        """Legacy UTC-surface date retained only for diagnostics."""

        return self.data_cutoff.date()

    def require_not_past_cutoff(
        self,
        day: date,
        timezone_or_field: str,
        field_name: str | None = None,
    ) -> None:
        """Reject a date beyond the boundary instead of trimming it.

        A date strictly after ``data_cutoff``'s day always fails.  The
        cutoff day itself fails unless ``include_cutoff_day`` declares the
        whole day complete.
        """

        if field_name is None:
            timezone_name = None
            resolved_field_name = timezone_or_field
            cutoff_date = self.cutoff_date
        else:
            timezone_name = timezone_or_field
            resolved_field_name = field_name
            cutoff_date = self.derive_cutoff_local_date(timezone_name)
        day = _plain_date(day, resolved_field_name)
        details = {
            "requested": day.isoformat(),
            "data_cutoff": self.data_cutoff.isoformat(),
            "include_cutoff_day": self.include_cutoff_day,
            "timezone": timezone_name,
            "cutoff_local_date": cutoff_date.isoformat(),
        }
        if day > cutoff_date:
            raise DataCutoffExceededError(
                f"{resolved_field_name} {day.isoformat()} is later than data_cutoff local date {cutoff_date.isoformat()}",
                details=details,
            )
        if day == cutoff_date and not self.include_cutoff_day:
            raise DataCutoffExceededError(
                f"{resolved_field_name} {day.isoformat()} touches the incomplete cutoff day",
                details=details,
            )

    def require_instant_not_past_cutoff(
        self, instant: datetime, field_name: str
    ) -> None:
        """Reject an aware instant later than ``data_cutoff``."""

        instant = _aware_datetime(instant, field_name)
        if instant > self.data_cutoff:
            raise DataCutoffExceededError(
                f"{field_name} {instant.isoformat()} is later than data_cutoff "
                f"{self.data_cutoff.isoformat()}",
                details={
                    "requested": instant.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                },
            )
