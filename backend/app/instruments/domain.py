"""Stable instrument identity and point-in-time reference-data contracts.

This module is the single source of truth for three deliberately separated
domain objects:

- :class:`InstrumentDisplay` carries the display information (trading code,
  names) that was valid at one market instant.  Every field except the
  stable ``instrument_id`` may be missing.
- :class:`InstrumentCodeMapping` records an evidenced, half-open
  ``[valid_from, valid_to)`` mapping between a stable instrument identity
  and a data-source code.  Mappings are immutable facts: they are never
  guessed from name similarity or from today's codes.
- :class:`InstrumentSpec` is a fully resolved, engine-consumable trading
  specification.  A spec is either complete or it does not exist; missing
  trading-critical fields must make the provider return ``None`` instead of
  producing a spec full of ``None`` placeholders.

All point-in-time queries explicitly separate two timestamps:

- ``effective_at`` — which market instant the content must be valid for;
- ``data_cutoff`` — the latest knowledge time (``known_at``) that this
  query is allowed to use.

Using a single ambiguous ``as_of`` for both meanings is forbidden by this
contract.  Trading-session details are referenced through
:class:`VersionedReference` instead of being inlined; concrete daily
sessions will later be resolved by calendar definitions and session facts.
Capability declarations (:class:`InstrumentCapabilities`) describe what an
instrument supports; enforcing them in matching or preflight checks is out
of scope for this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from app.backtesting.data.errors import (
    IdentityMappingConflictError,
    IdentityMappingIncompleteError,
    freeze_json,
)
from app.backtesting.domain import DomainValidationError, _aware_datetime


# ---------------------------------------------------------------------------
# Shared normalization helpers
# ---------------------------------------------------------------------------


def _optional_label(value: str | None, field_name: str) -> str | None:
    """Normalize an optional human-readable label; blank text means missing.

    Display fields legitimately may be unknown, but an empty or whitespace
    string is never a real value: it is canonicalized to ``None`` so callers
    can distinguish "unknown" from "present" with a plain identity check.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text when provided")
    normalized = value.strip()
    return normalized or None


def _required_label(value: str, field_name: str) -> str:
    """Require non-blank text for an identity-like string field."""

    normalized = _optional_label(value, field_name)
    if normalized is None:
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return normalized


def _strict_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Convert financial input to an exact ``Decimal`` while rejecting floats.

    Binary floats cannot represent most decimal fractions exactly, so they
    are rejected outright along with booleans (a ``bool`` is an ``int``
    subclass), NaN, and infinities.  ``Decimal``, ``int``, and decimal
    strings are accepted for ergonomic construction.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise DomainValidationError(
            f"{field_name} must be Decimal, int, or str; float is unsupported"
        )
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainValidationError(f"{field_name} must be a valid decimal") from exc
    if not normalized.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return normalized


def _positive_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a strictly positive exact decimal."""

    normalized = _strict_decimal(value, field_name)
    if normalized <= 0:
        raise DomainValidationError(f"{field_name} must be positive")
    return normalized


def _non_negative_int(value: int, field_name: str) -> int:
    """Require a plain non-negative integer; booleans are not integers here."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer")
    if value < 0:
        raise DomainValidationError(f"{field_name} must be non-negative")
    return value


def _is_representable(amount: Decimal, precision: int) -> bool:
    """Return whether ``amount`` has no digits beyond ``precision`` decimals."""

    scaled = amount.scaleb(precision)
    return scaled == scaled.to_integral_value()


# ---------------------------------------------------------------------------
# Versioned reference
# ---------------------------------------------------------------------------

# The canonical definition lives in the dependency-light ``references``
# module so that the backtesting data contract can import it without
# closing an instruments <-> backtesting import cycle.  It is re-exported
# here because every instrument-domain consumer historically imports it
# from this module.
from app.instruments.references import VersionedReference  # noqa: E402


# ---------------------------------------------------------------------------
# Point-in-time display information
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentDisplay:
    """Display fields valid at one market instant for a stable identity.

    ``instrument_id`` is mandatory and is the only association key used by
    results and mappings.  All display fields may be ``None`` when the data
    source does not provide them; a missing trading code is never copied
    into a name field to fake completeness.
    """

    instrument_id: UUID
    trading_code: str | None = None
    name: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        object.__setattr__(
            self, "trading_code", _optional_label(self.trading_code, "trading_code")
        )
        object.__setattr__(self, "name", _optional_label(self.name, "name"))
        object.__setattr__(
            self,
            "display_name",
            _optional_label(self.display_name, "display_name"),
        )


# ---------------------------------------------------------------------------
# Stable identity lifecycle and immutable identity facts
# ---------------------------------------------------------------------------


class InstrumentStatus(StrEnum):
    """Lifecycle state of a stable economic instrument identity.

    The status is deliberately independent from a source-code listing status.
    A code may be delisted while the economic identity remains useful for
    historical queries.  ``MERGED`` is terminal and points at the identity
    selected by an explicit, evidenced reconciliation operation.
    """

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    MERGED = "merged"
    # Synonyms preserve one persisted lifecycle value.
    RETIRED = "deprecated"
    INACTIVE = "deprecated"


IdentityStatus = InstrumentStatus
"""Backward-compatible alias for :class:`InstrumentStatus`."""
InstrumentLifecycleState = InstrumentStatus
"""Compatibility alias for lifecycle-oriented callers."""
InstrumentIdentityStatus = InstrumentStatus
"""Compatibility alias for the identity-row status enum."""


def _positive_version(value: int, field_name: str = "fact_version") -> int:
    """Validate an immutable fact version without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DomainValidationError(f"{field_name} must be a positive integer")
    return value


def _optional_uuid(value: UUID | None, field_name: str) -> UUID | None:
    """Validate an optional immutable fact/identity reference."""

    if value is None:
        return None
    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID when provided")
    return value


@dataclass(frozen=True, slots=True)
class InstrumentIdentityFact:
    """One immutable, point-in-time identity fact.

    Identity facts carry the asset protocol's identity-critical attributes;
    they do not derive exchange, currency, or calendar information from a
    source code.  ``exchange`` may be absent while an upstream fact is
    incomplete; formal ETF resolution must reject that identity rather than
    guessing a value.
    Corrections append a new version under the same ``logical_fact_key`` and
    reference the prior row through ``supersedes_fact_id``.  ``known_at`` is
    the only timestamp used for PIT visibility; ``observed_at`` remains an
    ingestion audit timestamp.
    """

    instrument_id: UUID
    fact_version: int
    asset_class: str
    currency: str
    calendar_id: str
    valid_from: date
    known_at: datetime
    observed_at: datetime
    evidence: str
    valid_to: date | None = None
    # Exchange is optional at ingestion time so incomplete historical facts
    # can be persisted and explicitly blocked by formal resolution.  ETF
    # specs require it; callers must never infer it from a source code.
    exchange: str | None = None
    fact_id: UUID | None = None
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        # ``None`` requests service-side generation.  Do not use truthiness
        # here: values such as ``False`` or ``""`` are invalid fact IDs and
        # must be rejected instead of silently replaced by a new UUID.
        fact_id = uuid4() if self.fact_id is None else _optional_uuid(
            self.fact_id, "fact_id"
        )
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(
            self, "fact_version", _positive_version(self.fact_version)
        )
        object.__setattr__(self, "asset_class", _required_label(self.asset_class, "asset_class"))
        exchange = _optional_label(self.exchange, "exchange")
        object.__setattr__(self, "exchange", exchange.upper() if exchange else None)
        object.__setattr__(self, "currency", _required_label(self.currency, "currency").upper())
        object.__setattr__(self, "calendar_id", _required_label(self.calendar_id, "calendar_id"))
        object.__setattr__(self, "evidence", _required_label(self.evidence, "evidence"))
        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise DomainValidationError("valid_from must be a calendar date")
        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise DomainValidationError("valid_to must be a calendar date")
            if self.valid_to <= self.valid_from:
                raise DomainValidationError("valid_to must be later than valid_from")
        object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "known_at"))
        object.__setattr__(self, "observed_at", _aware_datetime(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "logical_fact_key",
            _optional_label(self.logical_fact_key, "logical_fact_key")
            or f"instrument:{self.instrument_id}",
        )
        object.__setattr__(
            self,
            "supersedes_fact_id",
            _optional_uuid(self.supersedes_fact_id, "supersedes_fact_id"),
        )

    def covers(self, day: date) -> bool:
        """Return whether the fact is effective on a session date."""

        if not isinstance(day, date) or isinstance(day, datetime):
            raise DomainValidationError("day must be a calendar date")
        return self.valid_from <= day and (
            self.valid_to is None or day < self.valid_to
        )

# ---------------------------------------------------------------------------
# Evidenced PIT source-code mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentCodeMapping:
    """One evidenced source-code validity window for a stable instrument.

    The effective interval is half-open ``[valid_from, valid_to)`` at day
    granularity; ``valid_to=None`` means the mapping is still in force.
    ``source_code`` is the identifier assigned by the data source (for
    example ``510300.SH``) while ``trading_code`` is the user-facing display
    code (for example ``510300``).  ``known_at``/``observed_at`` must be
    timezone-aware because point-in-time visibility filtering depends on
    them.  ``evidence`` is mandatory: ``mapping_source`` only names the
    channel, while ``evidence`` is the concrete proof (announcement URL,
    filing reference, reviewed procedure id) without which the mapping
    must never be created.  Historical windows are never inferred from
    name similarity or current codes.
    """

    instrument_id: UUID
    source: str
    source_code: str
    trading_code: str
    valid_from: date
    mapping_source: str
    evidence: str
    known_at: datetime
    observed_at: datetime
    valid_to: date | None = None
    source_revision: str | None = None
    # ``fact_id`` is the immutable row id.  It is optional only for
    # compatibility with the original domain constructor; a server-side UUID
    # is generated when callers create a new fact.
    fact_id: UUID | None = None
    fact_version: int = 1
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        object.__setattr__(self, "source", _required_label(self.source, "source"))
        object.__setattr__(
            self, "source_code", _required_label(self.source_code, "source_code")
        )
        object.__setattr__(
            self, "trading_code", _required_label(self.trading_code, "trading_code")
        )
        object.__setattr__(
            self,
            "mapping_source",
            _required_label(self.mapping_source, "mapping_source"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _optional_label(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self, "evidence", _required_label(self.evidence, "evidence")
        )
        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise DomainValidationError("valid_from must be a calendar date")
        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise DomainValidationError("valid_to must be a calendar date")
            if self.valid_to <= self.valid_from:
                raise DomainValidationError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
        # Mapping visibility filtering compares these against data_cutoff, so
        # naive timestamps would silently shift what a query is allowed to see.
        object.__setattr__(
            self, "known_at", _aware_datetime(self.known_at, "known_at")
        )
        object.__setattr__(
            self,
            "observed_at",
            _aware_datetime(self.observed_at, "observed_at"),
        )
        # ``None`` requests service-side generation.  Falsey non-UUID values
        # are malformed immutable IDs and must not be silently regenerated.
        fact_id = uuid4() if self.fact_id is None else _optional_uuid(
            self.fact_id, "fact_id"
        )
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(
            self, "fact_version", _positive_version(self.fact_version)
        )
        # A revision chain must be identified by an explicit, immutable key.
        # For a new fact without a caller-supplied key, the generated fact id
        # is the only safe fallback; mutable fields such as source_code and
        # valid_from must never silently determine chain membership.
        supersedes_fact_id = _optional_uuid(
            self.supersedes_fact_id, "supersedes_fact_id"
        )
        object.__setattr__(self, "supersedes_fact_id", supersedes_fact_id)
        logical_fact_key = _optional_label(
            self.logical_fact_key, "logical_fact_key"
        )
        if logical_fact_key is None:
            if self.fact_version != 1 or supersedes_fact_id is not None:
                raise DomainValidationError(
                    "logical_fact_key must be explicit for mapping revisions"
                )
            logical_fact_key = f"mapping:{self.fact_id}"
        object.__setattr__(
            self,
            "logical_fact_key",
            logical_fact_key,
        )

    def covers(self, day: date) -> bool:
        """Return whether this mapping is effective on ``day`` (half-open)."""

        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


# The task-10 contract names this fact ``InstrumentCodeMappingFact``.  Keep
# one domain type and expose the terminology as an alias so persisted/API
# consumers can import either name without creating a second model.
InstrumentCodeMappingFact = InstrumentCodeMapping


def _normalize_mapping_error_detail(value: object) -> object:
    """Convert domain-native diagnostic values into stable JSON values.

    Mapping errors are raised by repository/domain code as well as by the PIT
    adapter.  Callers naturally include ``date``, ``datetime``, ``UUID``, or
    ``Decimal`` objects in those diagnostics, while the stable error base
    deliberately accepts JSON values only.  Normalize those domain values at
    this boundary instead of weakening the global error serializer.
    """

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_mapping_error_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_mapping_error_detail(item) for item in value]
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _normalize_mapping_error_detail(enum_value)
    return value


def _normalize_mapping_error_details(
    details: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Normalize one optional mapping-error diagnostic mapping."""

    if details is None:
        return None
    return {
        str(key): _normalize_mapping_error_detail(value)
        for key, value in details.items()
    }


class MappingConflictError(IdentityMappingConflictError):
    """Raised when overlapping mappings cover the same validity instants."""

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(
            message,
            details=_normalize_mapping_error_details(details),
        )


class MappingCoverageGapError(IdentityMappingIncompleteError):
    """Raised when mappings leave requested window dates uncovered."""

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(
            message,
            details=_normalize_mapping_error_details(details),
        )


def order_mapping_segments(
    mappings: Sequence[InstrumentCodeMapping],
    *,
    start_date: date,
    end_date: date,
    data_cutoff: datetime | None = None,
    instrument_id: UUID | None = None,
    source: str | None = None,
) -> tuple[InstrumentCodeMapping, ...]:
    """Sort mappings by ``valid_from`` and verify full window coverage.

    The segments must jointly cover every day of the inclusive
    ``[start_date, end_date]`` request window:

    - an empty result is a coverage gap (nothing is known for the window);
    - the first segment must start on or before ``start_date``;
    - the last segment must still be open or end after ``end_date``;
    - adjacent segments must chain exactly (``valid_to == next.valid_from``).

    Overlapping evidence raises :class:`MappingConflictError`; any
    uncovered date raises :class:`MappingCoverageGapError`.  Nothing is
    ever silently repaired with today's codes.  All mappings must belong
    to one ``instrument_id``/``source`` pair; mixing identities is a
    caller bug and rejected outright.
    """

    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise DomainValidationError("start_date must be a calendar date")
    if not isinstance(end_date, date) or isinstance(end_date, datetime):
        raise DomainValidationError("end_date must be a calendar date")
    if start_date > end_date:
        raise DomainValidationError("start_date cannot be after end_date")
    if data_cutoff is not None:
        data_cutoff = _aware_datetime(data_cutoff, "data_cutoff")

    def detail_value(value: object) -> object:
        """Render domain values into the JSON scalar types used by errors."""

        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {str(key): detail_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [detail_value(item) for item in value]
        if value is None or type(value) in (str, bool, int, float):
            return value
        enum_value = getattr(value, "value", None)
        return detail_value(enum_value) if enum_value is not None else repr(value)

    def error_details(
        *,
        source_code: object = None,
        session_date: object = None,
        expected: object = None,
        actual: object = None,
        **extra: object,
    ) -> dict[str, object]:
        """Build a stable diagnostic shape for mapping coverage failures."""

        first = mappings[0] if mappings else None
        resolved_instrument = (
            instrument_id
            if instrument_id is not None
            else getattr(first, "instrument_id", None)
        )
        resolved_source = (
            source if source is not None else getattr(first, "source", None)
        )
        result: dict[str, object] = {
            "instrument_id": detail_value(resolved_instrument),
            "source": detail_value(resolved_source),
            "source_code": detail_value(source_code),
            "session_date": detail_value(session_date),
            "expected": detail_value(expected),
            "actual": detail_value(actual),
            "data_cutoff": detail_value(data_cutoff),
            "fact_version": detail_value(
                getattr(first, "fact_version", None) if first is not None else None
            ),
        }
        if session_date is not None:
            result["session"] = detail_value(session_date)
        result.update({key: detail_value(value) for key, value in extra.items()})
        return result

    segments = sorted(mappings, key=lambda mapping: mapping.valid_from)
    identities = {(mapping.instrument_id, mapping.source) for mapping in segments}
    if len(identities) > 1:
        raise DomainValidationError(
            "mappings must belong to a single instrument_id/source pair; "
            f"got {sorted(identities)}"
        )
    if not segments:
        raise MappingCoverageGapError(
            "no instrument code mappings cover the requested window "
            f"[{start_date}, {end_date}]",
            details=error_details(
                session_date=start_date,
                expected="at least one mapping segment",
                actual=0,
            ),
        )
    first, last = segments[0], segments[-1]
    if first.valid_from > start_date:
        raise MappingCoverageGapError(
            "instrument code mappings start at "
            f"{first.valid_from}, leaving [{start_date}, {first.valid_from}) "
            "of the requested window uncovered",
            details=error_details(
                source_code=first.source_code,
                session_date=start_date,
                expected=f"<= {start_date.isoformat()}",
                actual=first.valid_from,
                first_valid_from=first.valid_from,
            ),
        )
    if last.valid_to is not None and last.valid_to <= end_date:
        raise MappingCoverageGapError(
            "instrument code mappings end at "
            f"{last.valid_to}, leaving [{last.valid_to}, {end_date}] "
            "of the requested window uncovered",
            details=error_details(
                source_code=last.source_code,
                session_date=end_date,
                expected=f"> {end_date.isoformat()}",
                actual=last.valid_to,
                last_valid_to=last.valid_to,
            ),
        )
    for earlier, later in zip(segments, segments[1:]):
        if earlier.valid_to is None or earlier.valid_to > later.valid_from:
            raise MappingConflictError(
                "overlapping instrument code mappings for "
                f"{earlier.source_code!r} ({earlier.valid_from}..{earlier.valid_to}) "
                f"and {later.source_code!r} ({later.valid_from}..{later.valid_to})",
                details=error_details(
                    source_code=earlier.source_code,
                    session_date=later.valid_from,
                    expected="one covering mapping",
                    actual=2,
                    earlier_source_code=earlier.source_code,
                    later_source_code=later.source_code,
                    earlier_valid_from=earlier.valid_from,
                    earlier_valid_to=earlier.valid_to,
                    later_valid_from=later.valid_from,
                    later_valid_to=later.valid_to,
                    fact_versions=[
                        getattr(earlier, "fact_version", None),
                        getattr(later, "fact_version", None),
                    ],
                ),
            )
        if earlier.valid_to < later.valid_from:
            raise MappingCoverageGapError(
                "instrument code mappings leave uncovered dates between "
                f"{earlier.valid_to} and {later.valid_from} "
                f"for instrument {earlier.instrument_id}",
                details=error_details(
                    source_code=earlier.source_code,
                    session_date=earlier.valid_to,
                    expected=earlier.valid_to,
                    actual=later.valid_from,
                    earlier_source_code=earlier.source_code,
                    later_source_code=later.source_code,
                    earlier_valid_to=earlier.valid_to,
                    later_valid_from=later.valid_from,
                    fact_versions=[
                        getattr(earlier, "fact_version", None),
                        getattr(later, "fact_version", None),
                    ],
                ),
            )
    return tuple(segments)


# ---------------------------------------------------------------------------
# Versioned display facts and identity resolution
# ---------------------------------------------------------------------------


class AuthorityStatus(StrEnum):
    """Review state of a display fact.

    Only ``AUTHORITATIVE`` facts are eligible for formal PIT resolution.
    Pending and rejected rows remain useful audit evidence but can never be
    selected merely because they are newer.
    """

    AUTHORITATIVE = "authoritative"
    PENDING = "pending"
    REJECTED = "rejected"


DisplayAuthorityStatus = AuthorityStatus
"""Compatibility alias for callers using the longer enum name."""


@dataclass(frozen=True, slots=True)
class InstrumentDisplayFact:
    """One immutable, evidenced display identity fact.

    Display labels are intentionally nullable.  A source can know the
    stable identity while not publishing one of the labels; resolution then
    returns ``None`` for that field instead of reading the current ETF
    snapshot as a historical fallback.
    """

    instrument_id: UUID
    fact_version: int
    valid_from: date
    known_at: datetime
    observed_at: datetime
    source: str
    evidence: str
    trading_code: str | None = None
    name: str | None = None
    display_name: str | None = None
    valid_to: date | None = None
    source_revision: str | None = None
    authority_rank: int = 0
    authority_status: AuthorityStatus = AuthorityStatus.AUTHORITATIVE
    fact_id: UUID | None = None
    logical_fact_key: str | None = None
    supersedes_fact_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        # ``None`` requests service-side generation.  Preserve validation for
        # every other value, including falsey values such as ``False``/"".
        fact_id = uuid4() if self.fact_id is None else _optional_uuid(
            self.fact_id, "fact_id"
        )
        object.__setattr__(self, "fact_id", fact_id)
        object.__setattr__(self, "fact_version", _positive_version(self.fact_version))
        if not isinstance(self.valid_from, date) or isinstance(self.valid_from, datetime):
            raise DomainValidationError("valid_from must be a calendar date")
        if self.valid_to is not None:
            if not isinstance(self.valid_to, date) or isinstance(self.valid_to, datetime):
                raise DomainValidationError("valid_to must be a calendar date")
            if self.valid_to <= self.valid_from:
                raise DomainValidationError("valid_to must be later than valid_from")
        for field_name in ("trading_code", "name", "display_name"):
            object.__setattr__(
                self,
                field_name,
                _optional_label(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "source", _required_label(self.source, "source"))
        object.__setattr__(self, "evidence", _required_label(self.evidence, "evidence"))
        object.__setattr__(self, "source_revision", _optional_label(self.source_revision, "source_revision"))
        if isinstance(self.authority_rank, bool) or not isinstance(self.authority_rank, int):
            raise DomainValidationError("authority_rank must be an integer")
        if self.authority_rank < 0:
            raise DomainValidationError("authority_rank must be non-negative")
        try:
            status = AuthorityStatus(getattr(self.authority_status, "value", self.authority_status))
        except ValueError as exc:
            raise DomainValidationError("authority_status must be valid") from exc
        object.__setattr__(self, "authority_status", status)
        object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "known_at"))
        object.__setattr__(self, "observed_at", _aware_datetime(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "logical_fact_key",
            _optional_label(self.logical_fact_key, "logical_fact_key")
            or f"display:{self.instrument_id}",
        )
        object.__setattr__(
            self,
            "supersedes_fact_id",
            _optional_uuid(self.supersedes_fact_id, "supersedes_fact_id"),
        )

    def covers(self, day: date) -> bool:
        """Return whether this display fact is effective on ``day``."""

        if not isinstance(day, date) or isinstance(day, datetime):
            raise DomainValidationError("day must be a calendar date")
        return self.valid_from <= day and (
            self.valid_to is None or day < self.valid_to
        )

    def as_display(self) -> InstrumentDisplay:
        """Project the immutable fact to the historical display DTO."""

        return InstrumentDisplay(
            instrument_id=self.instrument_id,
            trading_code=self.trading_code,
            name=self.name,
            display_name=self.display_name,
        )


def _identity_fact_evidence_summary(
    fact: object | None,
    *,
    kind: str,
) -> dict[str, object] | None:
    """Project one selected fact into a JSON-safe evidence summary.

    Resolution objects are immutable snapshots.  Their evidence therefore
    needs to retain the provenance and both PIT axes of every selected fact,
    rather than reducing a source to a boolean such as ``display_present``.
    ``kind`` is intentionally explicit so mapping/display-specific fields are
    included without coupling the helper to ORM records.
    """

    if fact is None:
        return None
    fields: dict[str, object] = {
        "fact_id": getattr(fact, "fact_id", None),
        "fact_version": getattr(fact, "fact_version", None),
        "logical_fact_key": getattr(fact, "logical_fact_key", None),
        "instrument_id": getattr(fact, "instrument_id", None),
        "evidence": getattr(fact, "evidence", None),
        "valid_from": getattr(fact, "valid_from", None),
        "valid_to": getattr(fact, "valid_to", None),
        "known_at": getattr(fact, "known_at", None),
        "observed_at": getattr(fact, "observed_at", None),
    }
    if kind == "identity":
        fields.update(
            {
                "asset_class": getattr(fact, "asset_class", None),
                "exchange": getattr(fact, "exchange", None),
                "currency": getattr(fact, "currency", None),
                "calendar_id": getattr(fact, "calendar_id", None),
            }
        )
    elif kind == "mapping":
        fields.update(
            {
                "source": getattr(fact, "source", None),
                "source_revision": getattr(fact, "source_revision", None),
                "source_code": getattr(fact, "source_code", None),
                "trading_code": getattr(fact, "trading_code", None),
                "mapping_source": getattr(fact, "mapping_source", None),
            }
        )
    elif kind == "display":
        fields.update(
            {
                "source": getattr(fact, "source", None),
                "source_revision": getattr(fact, "source_revision", None),
                "trading_code": getattr(fact, "trading_code", None),
                "name": getattr(fact, "name", None),
                "display_name": getattr(fact, "display_name", None),
                "authority_rank": getattr(fact, "authority_rank", None),
                "authority_status": getattr(fact, "authority_status", None),
            }
        )
    else:  # pragma: no cover - private helper misuse guard
        raise ValueError(f"unknown identity fact kind: {kind}")
    return {
        str(key): _normalize_mapping_error_detail(value)
        for key, value in fields.items()
    }


def _identity_resolution_evidence_summary(
    *,
    instrument_id: UUID,
    effective_at: datetime,
    data_cutoff: datetime,
    identity_fact: InstrumentIdentityFact | None = None,
    display_fact: InstrumentDisplayFact | None = None,
    mapping: InstrumentCodeMapping | None = None,
    source: str | None = None,
) -> dict[str, object]:
    """Build the shared evidence summary used by pure and repository paths."""

    effective = _aware_datetime(effective_at, "effective_at")
    cutoff = _aware_datetime(data_cutoff, "data_cutoff")
    identity_summary = _identity_fact_evidence_summary(
        identity_fact, kind="identity"
    )
    display_summary = _identity_fact_evidence_summary(display_fact, kind="display")
    mapping_summary = _identity_fact_evidence_summary(mapping, kind="mapping")
    summary: dict[str, object] = {
        "instrument_id": str(instrument_id),
        "effective_at": effective.isoformat(),
        "data_cutoff": cutoff.isoformat(),
        "source": source,
        "identity_fact": identity_summary,
        "display_fact": display_summary,
        "mapping_fact": mapping_summary,
    }
    # Flat aliases preserve the original result shape for existing consumers;
    # the nested summaries above are the canonical provenance representation.
    for prefix, selected in (
        ("identity", identity_summary),
        ("display", display_summary),
        ("mapping", mapping_summary),
    ):
        if selected is None:
            continue
        for field_name, value in selected.items():
            summary[f"{prefix}_{field_name}"] = value
    summary["display_present"] = display_summary is not None
    return summary


@dataclass(frozen=True, slots=True)
class InstrumentIdentityResolution:
    """PIT identity and display result used by result/candidate boundaries."""

    instrument_id: UUID
    identity_fact: InstrumentIdentityFact | None = None
    display: InstrumentDisplay | None = None
    mapping: InstrumentCodeMapping | None = None
    evidence_summary: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if self.identity_fact is not None:
            if not isinstance(self.identity_fact, InstrumentIdentityFact):
                raise DomainValidationError("identity_fact must be an InstrumentIdentityFact")
            if self.identity_fact.instrument_id != self.instrument_id:
                raise DomainValidationError("identity_fact.instrument_id must match instrument_id")
        if self.display is not None:
            if not isinstance(self.display, InstrumentDisplay):
                raise DomainValidationError("display must be an InstrumentDisplay")
            if self.display.instrument_id != self.instrument_id:
                raise DomainValidationError("display.instrument_id must match instrument_id")
        if self.mapping is not None:
            if not isinstance(self.mapping, InstrumentCodeMapping):
                raise DomainValidationError("mapping must be an InstrumentCodeMapping")
            if self.mapping.instrument_id != self.instrument_id:
                raise DomainValidationError("mapping.instrument_id must match instrument_id")
        if not isinstance(self.evidence_summary, Mapping):
            raise DomainValidationError("evidence_summary must be a mapping")
        # Keep resolution evidence detached from mutable repository values at
        # every nesting level.  Evidence is persisted/serialized as JSON, so
        # rejecting non-JSON values here also prevents a partially frozen
        # object from reaching a result snapshot.
        try:
            frozen = freeze_json(self.evidence_summary, "evidence_summary")
        except ValueError as exc:
            raise DomainValidationError(
                f"evidence_summary must contain only JSON values: {exc}"
            ) from exc
        if not isinstance(frozen, Mapping):
            raise DomainValidationError("evidence_summary must be a mapping")
        object.__setattr__(self, "evidence_summary", frozen)

    @property
    def asset_class(self) -> str | None:
        """Resolved asset class, or ``None`` when its identity fact is absent."""

        return self.identity_fact.asset_class if self.identity_fact else None

    @property
    def exchange(self) -> str | None:
        """Resolved exchange without deriving it from the source code."""

        return self.identity_fact.exchange if self.identity_fact else None

    @property
    def currency(self) -> str | None:
        """Resolved currency without guessing from a source code."""

        return self.identity_fact.currency if self.identity_fact else None

    @property
    def calendar_id(self) -> str | None:
        """Resolved calendar reference without deriving a market timezone."""

        return self.identity_fact.calendar_id if self.identity_fact else None

    @property
    def trading_code(self) -> str | None:
        """Historical display code, if a display fact supplied one."""

        return self.display.trading_code if self.display else None

    @property
    def name(self) -> str | None:
        """Historical name, if a display fact supplied one."""

        return self.display.name if self.display else None

    @property
    def display_name(self) -> str | None:
        """Historical display name, if a display fact supplied one."""

        return self.display.display_name if self.display else None


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


class CorporateActionRequirement(StrEnum):
    """Whether corporate actions must be processed before trading."""

    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class InstrumentCapabilities:
    """Explicit capability facts for one instrument.

    Collections are stored as ``frozenset`` so the declaration cannot be
    mutated after construction.  Empty position-side or order-type sets are
    meaningless and rejected.  This class only *declares* capabilities;
    gating execution on them happens elsewhere and is out of scope here.
    """

    position_sides: frozenset[str]
    order_types: frozenset[str]
    margin_supported: bool
    corporate_action_requirement: CorporateActionRequirement

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "position_sides",
            _frozen_capability_set(self.position_sides, "position_sides"),
        )
        object.__setattr__(
            self,
            "order_types",
            _frozen_capability_set(self.order_types, "order_types"),
        )
        if not isinstance(self.margin_supported, bool):
            raise DomainValidationError("margin_supported must be a boolean")
        try:
            requirement = CorporateActionRequirement(
                getattr(
                    self.corporate_action_requirement,
                    "value",
                    self.corporate_action_requirement,
                )
            )
        except ValueError as exc:
            allowed = [member.value for member in CorporateActionRequirement]
            raise DomainValidationError(
                f"corporate_action_requirement must be one of {allowed}"
            ) from exc
        object.__setattr__(
            self, "corporate_action_requirement", requirement
        )


def _frozen_capability_set(value: frozenset[str], field_name: str) -> frozenset[str]:
    """Validate and freeze one capability collection; emptiness is rejected."""

    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise DomainValidationError(f"{field_name} must be a collection of strings")
    members = tuple(value)
    normalized = []
    for member in members:
        label = _optional_label(member, f"{field_name} member")
        if label is None:
            raise DomainValidationError(f"{field_name} must contain non-blank text")
        normalized.append(label)
    frozen = frozenset(normalized)
    if len(frozen) != len(normalized):
        raise DomainValidationError(f"{field_name} must not contain duplicates")
    if not frozen:
        raise DomainValidationError(f"{field_name} must not be empty")
    return frozen


def _freeze_spec_value(value: Any, field_name: str) -> Any:
    """Freeze a rule/calendar projection without inventing a value.

    Rule facts are normalized by the rule resolver into immutable domain
    objects (``VersionedReference``/``StrategyRuleDeclaration``), while
    calendar providers may return nested mappings or session tuples.  This
    boundary accepts those already-resolved values and recursively freezes
    standard containers so a spec cannot be mutated through an alias.
    """

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise DomainValidationError(f"{field_name} keys must be non-blank text")
            normalized_key = key.strip()
            if normalized_key in frozen:
                raise DomainValidationError(
                    f"{field_name} keys must be unique after normalization"
                )
            frozen[normalized_key] = _freeze_spec_value(
                item, f"{field_name}[{key!r}]"
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_spec_value(item, field_name) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_spec_value(item, field_name) for item in value)
    return value


def _rule_declaration(value: Any, field_name: str) -> Any:
    """Validate one strategy rule projection without creating a new type.

    Importing ``StrategyRuleDeclaration`` at module load time would create a
    cycle because the rule-contract module imports ``VersionedReference``
    from this module.  A local import keeps the dependency one-way while
    retaining an exact type check at construction time.
    """

    from app.instruments.rules.contracts import StrategyRuleDeclaration

    if not isinstance(value, (VersionedReference, StrategyRuleDeclaration)):
        raise DomainValidationError(
            f"{field_name} must be a VersionedReference or StrategyRuleDeclaration"
        )
    return value


def _trading_status_policy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize explicit required/not-applicable status declarations."""

    from app.instruments.rules.contracts import (
        CAPABILITY_DIMENSIONS,
        TradingStatusRequirement,
    )

    if not isinstance(value, Mapping):
        raise DomainValidationError("trading_status_policy must be a mapping")
    normalized: dict[str, TradingStatusRequirement] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.strip():
            raise DomainValidationError(
                "trading_status_policy keys must be non-blank text"
            )
        try:
            normalized_key = key.strip()
            if normalized_key in normalized:
                raise DomainValidationError(
                    "trading_status_policy keys must be unique after normalization"
                )
            normalized[normalized_key] = TradingStatusRequirement(
                getattr(raw, "value", raw)
            )
        except ValueError as exc:
            allowed = [member.value for member in TradingStatusRequirement]
            raise DomainValidationError(
                f"trading_status_policy values must be one of {allowed}"
            ) from exc
    required_dimensions = set(CAPABILITY_DIMENSIONS)
    if set(normalized) != required_dimensions:
        missing = sorted(required_dimensions - set(normalized))
        extra = sorted(set(normalized) - required_dimensions)
        raise DomainValidationError(
            "trading_status_policy must declare every status dimension "
            f"(missing={missing}, extra={extra})"
        )
    return MappingProxyType({key: normalized[key] for key in sorted(normalized)})


# ---------------------------------------------------------------------------
# Fully resolved trading specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """A fully resolved, engine-consumable trading specification.

    Every trading-critical field is required: there are intentionally no
    default values, so a "half-complete" spec cannot be constructed by
    accident.  Only the nested display fields may be missing.  Rule values,
    capability declarations, calendar hours, and package/exception pointers
    are captured from the resolved facts and frozen at this boundary.  When
    a provider cannot assemble all of these facts for the requested instant it
    must return ``None`` (unresolvable) rather than degrade into Nones.
    """

    instrument_id: UUID
    display: InstrumentDisplay
    asset_class: str
    exchange: str
    currency: str
    calendar_id: str
    price_precision: int
    quantity_precision: int
    price_tick: Decimal | int | str
    lot_size: Decimal | int | str
    minimum_order_quantity: Decimal | int | str
    contract_multiplier: Decimal | int | str
    trading_session_template: VersionedReference
    trading_hours: Any
    settlement_rule_class: str
    sellable_rule: Any
    fee_categories: frozenset[str]
    trading_status_policy: Mapping[str, Any]
    order_types: frozenset[str]
    price_limit_rule: Any
    cash_availability_rule: Any
    position_availability_rule: Any
    capabilities: InstrumentCapabilities
    rule_package_reference: VersionedReference
    valid_from: datetime
    valid_to: datetime | None
    # No exception is a valid resolved outcome; the default only represents
    # that absence and never supplies production rule values.
    rule_exception_reference: VersionedReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        if not isinstance(self.display, InstrumentDisplay):
            raise DomainValidationError("display must be an InstrumentDisplay")
        if self.display.instrument_id != self.instrument_id:
            raise DomainValidationError(
                "display.instrument_id must equal the spec instrument_id"
            )
        object.__setattr__(
            self, "asset_class", _required_label(self.asset_class, "asset_class")
        )
        object.__setattr__(
            self, "exchange", _required_label(self.exchange, "exchange")
        )
        # Currencies are canonicalized to upper case for consistent joins,
        # but no ISO-4217 length rule is enforced here.
        currency = _required_label(self.currency, "currency").upper()
        object.__setattr__(self, "currency", currency)
        object.__setattr__(
            self, "calendar_id", _required_label(self.calendar_id, "calendar_id")
        )
        price_precision = _non_negative_int(
            self.price_precision, "price_precision"
        )
        quantity_precision = _non_negative_int(
            self.quantity_precision, "quantity_precision"
        )
        object.__setattr__(self, "price_precision", price_precision)
        object.__setattr__(self, "quantity_precision", quantity_precision)

        price_tick = _positive_decimal(self.price_tick, "price_tick")
        lot_size = _positive_decimal(self.lot_size, "lot_size")
        minimum_order_quantity = _positive_decimal(
            self.minimum_order_quantity, "minimum_order_quantity"
        )
        contract_multiplier = _positive_decimal(
            self.contract_multiplier, "contract_multiplier"
        )
        if not _is_representable(price_tick, price_precision):
            raise DomainValidationError(
                "price_tick must be exactly representable with price_precision"
            )
        for name, amount in (
            ("lot_size", lot_size),
            ("minimum_order_quantity", minimum_order_quantity),
        ):
            if not _is_representable(amount, quantity_precision):
                raise DomainValidationError(
                    f"{name} must be exactly representable with quantity_precision"
                )
        if minimum_order_quantity % lot_size != 0:
            raise DomainValidationError(
                "minimum_order_quantity must be an integer multiple of lot_size"
            )
        object.__setattr__(self, "price_tick", price_tick)
        object.__setattr__(self, "lot_size", lot_size)
        object.__setattr__(self, "minimum_order_quantity", minimum_order_quantity)
        object.__setattr__(self, "contract_multiplier", contract_multiplier)

        if not isinstance(self.trading_session_template, VersionedReference):
            raise DomainValidationError(
                "trading_session_template must be a VersionedReference"
            )
        if self.trading_hours is None:
            raise DomainValidationError(
                "trading_hours must be resolved by the calendar provider"
            )
        object.__setattr__(
            self,
            "trading_hours",
            _freeze_spec_value(self.trading_hours, "trading_hours"),
        )
        object.__setattr__(
            self,
            "settlement_rule_class",
            _required_label(self.settlement_rule_class, "settlement_rule_class"),
        )
        for field_name in (
            "sellable_rule",
            "price_limit_rule",
            "cash_availability_rule",
            "position_availability_rule",
        ):
            object.__setattr__(
                self,
                field_name,
                _rule_declaration(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "fee_categories",
            _frozen_capability_set(self.fee_categories, "fee_categories"),
        )
        object.__setattr__(
            self,
            "order_types",
            _frozen_capability_set(self.order_types, "order_types"),
        )
        object.__setattr__(
            self,
            "trading_status_policy",
            _trading_status_policy(self.trading_status_policy),
        )
        if not isinstance(self.rule_package_reference, VersionedReference):
            raise DomainValidationError(
                "rule_package_reference must be a VersionedReference"
            )
        if self.rule_exception_reference is not None and not isinstance(
            self.rule_exception_reference, VersionedReference
        ):
            raise DomainValidationError(
                "rule_exception_reference must be a VersionedReference when provided"
            )
        object.__setattr__(
            self,
            "valid_from",
            _aware_datetime(self.valid_from, "valid_from"),
        )
        if self.valid_to is not None:
            valid_to = _aware_datetime(self.valid_to, "valid_to")
            if valid_to <= self.valid_from:
                raise DomainValidationError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
            object.__setattr__(self, "valid_to", valid_to)
        if not isinstance(self.capabilities, InstrumentCapabilities):
            raise DomainValidationError(
                "capabilities must be an InstrumentCapabilities instance"
            )


# ---------------------------------------------------------------------------
# Point-in-time provider protocols
# ---------------------------------------------------------------------------


class InstrumentDisplayProvider(Protocol):
    """Structural source of point-in-time-valid display information."""

    def resolve_display(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        """Return display info valid at ``effective_at`` or ``None``.

        Only knowledge recorded up to ``data_cutoff`` (``known_at``) may be
        used; later corrections must stay invisible to this query.
        """
        ...


class InstrumentSpecProvider(Protocol):
    """Structural source of fully resolved trading specifications."""

    def resolve_spec(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentSpec | None:
        """Return the complete spec valid at ``effective_at`` or ``None``.

        Returning ``None`` is the only sanctioned way to express "cannot be
        resolved"; specs with placeholder ``None`` trading fields violate
        this contract.
        """
        ...


class InstrumentCodeMappingProvider(Protocol):
    """Structural source of evidenced PIT source-code mappings."""

    def resolve_code_mappings(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> Sequence[InstrumentCodeMapping]:
        """Return the mappings intersecting ``[start_date, end_date]``.

        Results must be ordered by ``valid_from``.  Mappings learned after
        ``data_cutoff`` are invisible.  Coverage gaps and overlapping
        conflicts surface as explicit domain errors instead of being
        repaired with the currently valid code.
        """
        ...


__all__ = [
    "AuthorityStatus",
    "CorporateActionRequirement",
    "DisplayAuthorityStatus",
    "IdentityStatus",
    "InstrumentCapabilities",
    "InstrumentCodeMapping",
    "InstrumentCodeMappingFact",
    "InstrumentCodeMappingProvider",
    "InstrumentDisplay",
    "InstrumentDisplayFact",
    "InstrumentDisplayProvider",
    "InstrumentIdentityFact",
    "InstrumentIdentityResolution",
    "InstrumentLifecycleState",
    "InstrumentIdentityStatus",
    "InstrumentStatus",
    "InstrumentSpec",
    "InstrumentSpecProvider",
    "MappingConflictError",
    "MappingCoverageGapError",
    "VersionedReference",
    "order_mapping_segments",
]
