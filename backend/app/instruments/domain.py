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
from typing import Protocol, Sequence
from uuid import UUID

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

    def covers(self, day: date) -> bool:
        """Return whether this mapping is effective on ``day`` (half-open)."""

        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


class MappingConflictError(DomainValidationError):
    """Raised when overlapping mappings cover the same validity instants."""


class MappingCoverageGapError(DomainValidationError):
    """Raised when mappings leave requested window dates uncovered."""


def order_mapping_segments(
    mappings: Sequence[InstrumentCodeMapping],
    *,
    start_date: date,
    end_date: date,
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
            f"[{start_date}, {end_date}]"
        )
    first, last = segments[0], segments[-1]
    if first.valid_from > start_date:
        raise MappingCoverageGapError(
            "instrument code mappings start at "
            f"{first.valid_from}, leaving [{start_date}, {first.valid_from}) "
            "of the requested window uncovered"
        )
    if last.valid_to is not None and last.valid_to <= end_date:
        raise MappingCoverageGapError(
            "instrument code mappings end at "
            f"{last.valid_to}, leaving [{last.valid_to}, {end_date}] "
            "of the requested window uncovered"
        )
    for earlier, later in zip(segments, segments[1:]):
        if earlier.valid_to is None or earlier.valid_to > later.valid_from:
            raise MappingConflictError(
                "overlapping instrument code mappings for "
                f"{earlier.source_code!r} ({earlier.valid_from}..{earlier.valid_to}) "
                f"and {later.source_code!r} ({later.valid_from}..{later.valid_to})"
            )
        if earlier.valid_to < later.valid_from:
            raise MappingCoverageGapError(
                "instrument code mappings leave uncovered dates between "
                f"{earlier.valid_to} and {later.valid_from} "
                f"for instrument {earlier.instrument_id}"
            )
    return tuple(segments)


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


# ---------------------------------------------------------------------------
# Fully resolved trading specification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """A fully resolved, engine-consumable trading specification.

    Every trading-critical field is required: there are intentionally no
    default values, so a "half-complete" spec cannot be constructed by
    accident.  Only the nested display fields may be missing.  When a
    provider cannot assemble all of these facts for the requested instant
    it must return ``None`` (unresolvable) rather than degrade into Nones.
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
    valid_from: datetime
    valid_to: datetime | None
    capabilities: InstrumentCapabilities

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
    "CorporateActionRequirement",
    "InstrumentCapabilities",
    "InstrumentCodeMapping",
    "InstrumentCodeMappingProvider",
    "InstrumentDisplay",
    "InstrumentDisplayProvider",
    "InstrumentSpec",
    "InstrumentSpecProvider",
    "MappingConflictError",
    "MappingCoverageGapError",
    "VersionedReference",
    "order_mapping_segments",
]
