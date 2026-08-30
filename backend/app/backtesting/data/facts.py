"""Generic fact envelopes returned by chunk sessions.

These are the minimal strongly typed facts every provider must return for
the corresponding query.  Stable instrument identity reuses the canonical
``app.instruments.domain`` objects (:class:`InstrumentSpec`,
:class:`InstrumentCodeMapping`, :class:`InstrumentDisplay`); this module
never defines a second truth source for them.

Money-like values (prices, amounts, quantities, factors) are finite
``Decimal`` instances: binary floats, booleans, NaN, and infinities are
rejected at construction.  Every fact carries evidence via
:class:`FactEvidence`; asset-specific extensions live under a versioned
``schema`` reference inside deep-frozen ``attributes``, which the engine
must not depend on directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from app.backtesting.data.errors import (
    CoverageFactInvalidError,
    ProviderContractViolationError,
    freeze_json,
)
from app.backtesting.data.requests import (
    ContractRef,
    DataCapability,
    PriceBasis,
    QualityStatus,
)
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import (
    InstrumentCodeMapping,
    InstrumentCodeMappingFact,
    InstrumentDisplay,
    InstrumentSpec,
)

__all__ = [
    "AdjustedSeriesPoint",
    "CoverageApplicability",
    "DataCoverageApplicability",
    "DataCoverageFact",
    "Bar",
    "BarFact",
    "ClosePriceFact",
    "CorporateAction",
    "DataPoint",
    "FactEvidence",
    "InstrumentCodeMapping",
    "InstrumentCodeMappingFact",
    "InstrumentDisplay",
    "InstrumentSpec",
    "PitRateSnapshotQuery",
    "PitRateFact",
    "Tick",
    "TradingRule",
    "TradingStatus",
]


# ---------------------------------------------------------------------------
# Validation helpers (provider output is validated on construction)
# ---------------------------------------------------------------------------


def _finite_decimal(value: object, field_name: str) -> Decimal:
    """Normalize one money-like value to an exact finite ``Decimal``.

    Floats and booleans are rejected outright because they cannot represent
    decimal fractions exactly.  ``Decimal``, ``int``, and decimal strings
    are accepted; NaN and infinities never pass.
    """

    if isinstance(value, bool) or isinstance(value, float) or not isinstance(
        value, (Decimal, int, str)
    ):
        raise ProviderContractViolationError(
            f"{field_name} must be Decimal, int, or str; float is unsupported"
        )
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderContractViolationError(
            f"{field_name} is not a valid decimal"
        ) from exc
    if not normalized.is_finite():
        raise ProviderContractViolationError(f"{field_name} must be finite")
    return normalized


def _positive_decimal(value: object, field_name: str) -> Decimal:
    """A finite decimal strictly greater than zero."""

    normalized = _finite_decimal(value, field_name)
    if normalized <= 0:
        raise ProviderContractViolationError(f"{field_name} must be positive")
    return normalized


def _non_negative_decimal(value: object, field_name: str) -> Decimal:
    """A finite decimal greater than or equal to zero."""

    normalized = _finite_decimal(value, field_name)
    if normalized < 0:
        raise ProviderContractViolationError(f"{field_name} must not be negative")
    return normalized


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject full datetimes."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise ProviderContractViolationError(f"{field_name} must be a datetime.date")
    return value


def _non_blank_text(value: object, field_name: str) -> str:
    """Require non-blank plain text on a returned fact."""

    if not isinstance(value, str) or not value.strip():
        raise ProviderContractViolationError(f"{field_name} must be non-blank text")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    """Normalize optional non-blank text; blank means missing."""

    if value is None:
        return None
    return _non_blank_text(value, field_name)


def _require_uuid(value: object, field_name: str) -> UUID:
    """Require the stable UUID identity key."""

    if not isinstance(value, UUID):
        raise ProviderContractViolationError(
            f"{field_name} must be a UUID (stable instrument identity)"
        )
    return value


def _frozen_attributes(
    value: Mapping[str, object] | None, field_name: str
) -> Mapping[str, object]:
    """Deep-freeze extension attributes, accepting JSON values only."""

    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ProviderContractViolationError(f"{field_name} must be a mapping")
    try:
        frozen = freeze_json(dict(value), field_name)
    except ValueError as exc:
        raise ProviderContractViolationError(str(exc)) from exc
    assert isinstance(frozen, MappingProxyType)
    return frozen


def _validated_schema(value: ContractRef | None, field_name: str) -> ContractRef | None:
    """Validate the optional versioned schema reference of a fact."""

    if value is None:
        return None
    if not isinstance(value, ContractRef):
        raise ProviderContractViolationError(f"{field_name} must be a ContractRef")
    return value


_MACHINE_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/-]*$")
_MACHINE_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_SENSITIVE_DETAIL_KEY_PARTS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "api_token",
        "authorization",
        "credential",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)


class CoverageApplicability(StrEnum):
    """Whether one coverage dimension is required by the frozen rule."""

    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


# This longer spelling is useful to callers that want to distinguish the
# coverage declaration from similarly named calendar declarations.  It is an
# alias, not a second enum or a second source of contract semantics.
DataCoverageApplicability = CoverageApplicability


def _safe_contract_value(value: object) -> object:
    """Convert validation context to JSON-safe, non-sensitive primitives."""

    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, ContractRef):
        return {"key": value.key, "version": value.version}
    if isinstance(value, Mapping):
        return {
            str(key): _safe_contract_value(item)
            for key, item in value.items()
            if isinstance(key, (str, int, float, bool))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_contract_value(item) for item in value]
    return type(value).__name__


def _coverage_invalid(message: str, **details: object) -> CoverageFactInvalidError:
    """Build one JSON-safe immutable-fact error with machine context."""

    return CoverageFactInvalidError(
        message,
        details={key: _safe_contract_value(value) for key, value in details.items()},
    )


def _coverage_rule_ref(
    value: ContractRef | tuple[str, int] | str | None,
    field_name: str,
) -> ContractRef | None:
    """Normalize an optional exact ``key@version`` validation reference.

    The public contract uses :class:`ContractRef`; accepting the compact
    ``key@version`` form keeps JSON adapters ergonomic while still rejecting
    an unpinned ``latest`` reference.  No mutable mapping or arbitrary object
    can enter a fact through this compatibility path.
    """

    if value is None:
        return None
    if isinstance(value, ContractRef):
        reference = value
    elif isinstance(value, str):
        text = value.strip()
        if "@" not in text:
            raise _coverage_invalid(
                f"{field_name} must include an exact key@version reference",
                field=field_name,
                actual=value,
                expected="key@version",
            )
        key, separator, version_text = text.rpartition("@")
        if not separator or not key.strip() or not version_text.isdigit():
            raise _coverage_invalid(
                f"{field_name} must include an exact key@version reference",
                field=field_name,
                actual=value,
                expected="key@version",
            )
        try:
            reference = ContractRef(key=key.strip(), version=int(version_text))
        except (TypeError, ValueError) as exc:
            raise _coverage_invalid(
                f"{field_name} is not a valid versioned reference",
                field=field_name,
                actual=value,
                expected="key@version",
            ) from exc
    elif isinstance(value, Mapping) and set(value) == {"key", "version"}:
        key = value["key"]
        version = value["version"]
        try:
            reference = ContractRef(key=key, version=version)
        except (TypeError, ValueError) as exc:
            raise _coverage_invalid(
                f"{field_name} is not a valid versioned reference",
                field=field_name,
                actual={"key": key, "version": version},
                expected="key@version",
            ) from exc
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        key, version = value
        try:
            reference = ContractRef(key=key, version=version)
        except (TypeError, ValueError) as exc:
            raise _coverage_invalid(
                f"{field_name} is not a valid versioned reference",
                field=field_name,
                actual=list(value),
                expected="key@version",
            ) from exc
    else:
        raise _coverage_invalid(
            f"{field_name} must be a ContractRef or exact key@version value",
            field=field_name,
            actual=type(value).__name__,
            expected="ContractRef",
        )
    if reference.key.strip().lower() == "latest":
        raise _coverage_invalid(
            f"{field_name} must not use the unpinned latest reference",
            field=field_name,
            actual=reference.key,
            expected="an exact versioned key",
        )
    return reference


def _coverage_field(value: object) -> str:
    """Require an ASCII machine field name, never a display label."""

    if type(value) is not str or not value.strip() or not _MACHINE_FIELD_RE.fullmatch(value.strip()):
        raise _coverage_invalid(
            "field must be a non-blank ASCII machine field name",
            field=value if type(value) is str else None,
            actual=value if type(value) in (str, int, float, bool) else type(value).__name__,
            expected="[A-Za-z_][A-Za-z0-9_.:/-]*",
        )
    return value.strip()


def _coverage_applicability(value: CoverageApplicability | str) -> CoverageApplicability:
    """Normalize only the two explicitly declared applicability values."""

    if isinstance(value, CoverageApplicability):
        return value
    if isinstance(value, str):
        try:
            return CoverageApplicability(value.strip())
        except ValueError:
            pass
    raise _coverage_invalid(
        "applicability must be required or not_applicable",
        field="applicability",
        actual=value if type(value) is str else type(value).__name__,
        expected=[CoverageApplicability.REQUIRED.value, CoverageApplicability.NOT_APPLICABLE.value],
    )


def _contains_sensitive_detail_key(value: object) -> str | None:
    """Find credential-shaped keys before JSON freezing detail payloads."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                normalized = key.strip().lower().replace("-", "_")
                pieces = set(normalized.split("."))
                if (
                    normalized in _SENSITIVE_DETAIL_KEY_PARTS
                    or pieces & _SENSITIVE_DETAIL_KEY_PARTS
                    or any(
                        part in normalized
                        for part in (
                            "token",
                            "secret",
                            "password",
                            "credential",
                            "authorization",
                            "api_key",
                            "private_key",
                        )
                    )
                ):
                    return key
            nested = _contains_sensitive_detail_key(item)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _contains_sensitive_detail_key(item)
            if nested is not None:
                return nested
    return None


def _coverage_details(value: Mapping[str, object] | None) -> Mapping[str, object]:
    """Deep-freeze auditable JSON details and reject credential material."""

    details = {} if value is None else value
    if not isinstance(details, Mapping):
        raise _coverage_invalid(
            "details must be a JSON mapping",
            field="details",
            actual=type(details).__name__,
            expected="JSON object",
        )
    sensitive_key = _contains_sensitive_detail_key(details)
    if sensitive_key is not None:
        raise _coverage_invalid(
            "details must not contain credentials or access tokens",
            field="details",
            actual=sensitive_key,
            expected="non-sensitive audit context",
        )
    try:
        frozen = freeze_json(dict(details), "details")
    except ValueError as exc:
        raise _coverage_invalid(
            "details must contain JSON-safe values",
            field="details",
            actual=type(details).__name__,
            expected="JSON object",
        ) from exc
    assert isinstance(frozen, MappingProxyType)
    return frozen


def _coverage_issue_codes(value: Sequence[str] | None) -> tuple[str, ...]:
    """Validate and deterministically normalize machine issue identifiers."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise _coverage_invalid(
            "issue_codes must be an iterable of machine codes",
            field="issue_codes",
            actual=type(value).__name__,
            expected="sequence[str]",
        )
    try:
        raw_codes = tuple(value)
    except TypeError as exc:
        raise _coverage_invalid(
            "issue_codes must be an iterable of machine codes",
            field="issue_codes",
            actual=type(value).__name__,
            expected="sequence[str]",
        ) from exc
    normalized: set[str] = set()
    for code in raw_codes:
        if type(code) is not str or not _MACHINE_CODE_RE.fullmatch(code.strip()):
            raise _coverage_invalid(
                "issue_codes entries must be non-blank machine codes",
                field="issue_codes",
                actual=code if type(code) is str else type(code).__name__,
                expected="[A-Za-z][A-Za-z0-9_.:-]*",
            )
        normalized.add(code.strip())
    return tuple(sorted(normalized))


def _thaw_json(value: object) -> object:
    """Return a JSON-safe mutable projection for wire-style dictionaries."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DataCoverageFact:
    """One immutable, auditable coverage assertion.

    The fact is intentionally an envelope rather than a source-specific
    table.  Its logical identity contains only the stable instrument/date,
    capability, field, and exact validation-rule reference.  ``details`` is
    limited to JSON values and is recursively frozen so a provider cannot
    mutate the evidence after aggregation.  A missing fact is represented by
    the aggregator as ``unavailable``; callers must not manufacture an empty
    fact or infer ``not_applicable`` from absence.
    """

    instrument_id: UUID
    session_date: date
    capability: DataCapability
    field: str
    validation_rule: ContractRef | tuple[str, int] | str | None = None
    applicability: CoverageApplicability | str = CoverageApplicability.REQUIRED
    quality_status: QualityStatus = QualityStatus.UNAVAILABLE
    evidence: FactEvidence | None = None
    details: Mapping[str, object] = MappingProxyType({})
    issue_codes: Sequence[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise _coverage_invalid(
                "instrument_id must be a UUID",
                field="instrument_id",
                actual=type(self.instrument_id).__name__,
                expected="UUID",
            )
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise _coverage_invalid(
                "session_date must be a calendar date",
                field="session_date",
                actual=type(self.session_date).__name__,
                expected="date",
            )
        if not isinstance(self.capability, DataCapability):
            raise _coverage_invalid(
                "capability must be a DataCapability",
                field="capability",
                actual=type(self.capability).__name__,
                expected="DataCapability",
            )
        object.__setattr__(self, "field", _coverage_field(self.field))
        object.__setattr__(
            self,
            "validation_rule",
            _coverage_rule_ref(self.validation_rule, "validation_rule"),
        )
        applicability = _coverage_applicability(self.applicability)
        object.__setattr__(self, "applicability", applicability)
        if not isinstance(self.quality_status, QualityStatus):
            raise _coverage_invalid(
                "quality_status must be a QualityStatus",
                field="quality_status",
                actual=type(self.quality_status).__name__,
                expected="QualityStatus",
            )
        if self.evidence is not None and not isinstance(self.evidence, FactEvidence):
            raise _coverage_invalid(
                "evidence must be FactEvidence or None",
                field="evidence",
                actual=type(self.evidence).__name__,
                expected="FactEvidence",
            )
        # A complete assertion is meaningful only with complete source
        # evidence.  This prevents a provider from using a positive status as
        # a shortcut around the source's own quality result.
        if self.quality_status is QualityStatus.COMPLETE and (
            self.evidence is None
            or self.evidence.quality_status is not QualityStatus.COMPLETE
        ):
            raise _coverage_invalid(
                "complete coverage requires complete audit evidence",
                field="evidence",
                actual=(
                    None
                    if self.evidence is None
                    else self.evidence.quality_status.value
                ),
                expected=QualityStatus.COMPLETE.value,
            )
        # ``not_applicable`` is a positive rule declaration, not a fallback
        # for a missing source row.  It therefore needs the rule reference
        # that made the declaration and has no partial/invalid quality.
        if applicability is CoverageApplicability.NOT_APPLICABLE:
            if self.validation_rule is None:
                raise _coverage_invalid(
                    "not_applicable coverage requires an explicit validation rule",
                    field="validation_rule",
                    actual=None,
                    expected="exact key@version",
                )
            if self.quality_status is not QualityStatus.COMPLETE:
                raise _coverage_invalid(
                    "not_applicable coverage must have complete quality",
                    field="quality_status",
                    actual=self.quality_status.value,
                    expected=QualityStatus.COMPLETE.value,
                )
        # An unavailable assertion explicitly says that coverage cannot be
        # proven.  A source evidence object, when present, must not contradict
        # that meaning by claiming complete/partial/invalid quality.
        if (
            self.quality_status is QualityStatus.UNAVAILABLE
            and self.evidence is not None
            and self.evidence.quality_status is not QualityStatus.UNAVAILABLE
        ):
            raise _coverage_invalid(
                "unavailable coverage cannot carry non-unavailable evidence",
                field="evidence.quality_status",
                actual=self.evidence.quality_status.value,
                expected=QualityStatus.UNAVAILABLE.value,
            )
        frozen_details = _coverage_details(self.details)
        if self.quality_status is QualityStatus.INVALID and not frozen_details:
            raise _coverage_invalid(
                "invalid coverage requires auditable failure details",
                field="details",
                actual="empty",
                expected="raw value and/or failed-rule context",
            )
        object.__setattr__(self, "details", frozen_details)
        object.__setattr__(self, "issue_codes", _coverage_issue_codes(self.issue_codes))

    @property
    def logical_key(
        self,
    ) -> tuple[UUID, date, DataCapability, str, tuple[str, int] | None]:
        """Return the stable logical identity used for de-duplication."""

        rule = self.validation_rule
        rule_key = None if rule is None else (rule.key, rule.version)
        return (
            self.instrument_id,
            self.session_date,
            self.capability,
            self.field,
            rule_key,
        )

    @property
    def fact_key(
        self,
    ) -> tuple[UUID, date, DataCapability, str, tuple[str, int] | None]:
        """Compatibility alias for callers that name the identity fact key."""

        return self.logical_key

    @property
    def normalized_logical_key(self) -> tuple[str, str, str, str, tuple[str, int] | None]:
        """Return the JSON-safe key projection used by report aggregation."""

        instrument_id, session_date, capability, field, rule = self.logical_key
        return (
            str(instrument_id),
            session_date.isoformat(),
            capability.value,
            field,
            rule,
        )

    def __hash__(self) -> int:
        """Hash only the stable logical key, never mutable-source metadata."""

        return hash(self.logical_key)

    def machine_content(self) -> dict[str, object]:
        """Return hash/comparison content without object identity metadata."""

        rule = self.validation_rule
        evidence = self.evidence
        return {
            "instrument_id": str(self.instrument_id),
            "session_date": self.session_date,
            "capability": self.capability,
            "field": self.field,
            "validation_rule": (
                None
                if rule is None
                else {"key": rule.key, "version": rule.version}
            ),
            "applicability": self.applicability,
            "quality_status": self.quality_status,
            "evidence": (
                None
                if evidence is None
                else {
                    "source": evidence.source,
                    "observed_at": evidence.observed_at,
                    "known_at": evidence.known_at,
                    "quality_status": evidence.quality_status,
                    "source_revision": evidence.source_revision,
                }
            ),
            "details": self.details,
            "issue_codes": list(self.issue_codes),
        }

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe wire projection of this coverage fact."""

        payload = self.machine_content()
        return _thaw_json(payload)  # type: ignore[return-value]

    def to_dict(self) -> dict[str, object]:
        """Compatibility alias for serializers using the common ``to_dict`` name."""

        return self.as_dict()


@dataclass(frozen=True, slots=True)
class FactEvidence:
    """Provenance and quality evidence attached to one returned fact.

    ``known_at`` records when this content became known to the source under
    strict PIT semantics; ``None`` means no strict knowledge-time evidence
    exists.  ``observed_at`` records when the provider read the fact.  Both
    timestamps must be timezone-aware.
    """

    source: str
    observed_at: datetime
    quality_status: QualityStatus
    known_at: datetime | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ProviderContractViolationError("source must be non-blank text")
        object.__setattr__(self, "source", self.source.strip())
        for name in ("observed_at", "known_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, datetime)
                or
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ProviderContractViolationError(
                    f"{name} must be timezone-aware"
                )
        if not isinstance(self.quality_status, QualityStatus):
            raise ProviderContractViolationError(
                "quality_status must be a QualityStatus"
            )
        if self.source_revision is not None:
            object.__setattr__(
                self,
                "source_revision",
                _optional_text(self.source_revision, "source_revision"),
            )

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _non_blank_text(self.source, "source"))
        if not isinstance(self.quality_status, QualityStatus):
            raise ProviderContractViolationError(
                "quality_status must be a QualityStatus"
            )
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "observed_at")
        )
        if self.known_at is not None:
            object.__setattr__(
                self, "known_at", _aware_datetime(self.known_at, "known_at")
            )
        object.__setattr__(
            self, "source_revision", _optional_text(self.source_revision, "source_revision")
        )


@dataclass(frozen=True, slots=True)
class TradingRule:
    """One trading-rule fact valid over a half-open date interval.

    ``rule_class`` is the strong-typed rule category (for example a
    settlement-rule class); the owning package is referenced through its
    versioned :class:`ContractRef`.  Unknown asset-specific payloads belong
    in ``attributes`` under ``schema``, never in free-form core fields.
    """

    instrument_id: UUID
    rule_class: str
    rule_package: ContractRef
    valid_from: date
    evidence: FactEvidence
    valid_to: date | None = None
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(self, "rule_class", _non_blank_text(self.rule_class, "rule_class"))
        if not isinstance(self.rule_package, ContractRef):
            raise ProviderContractViolationError("rule_package must be a ContractRef")
        start = _plain_date(self.valid_from, "valid_from")
        object.__setattr__(self, "valid_from", start)
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= start:
                raise ProviderContractViolationError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
            object.__setattr__(self, "valid_to", end)
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )


@dataclass(frozen=True, slots=True)
class TradingStatus:
    """One trading-status fact valid over a half-open date interval.

    ``status`` is the machine status identifier declared by the rule
    package (for example tradable versus suspended); it is never guessed
    from price presence.
    """

    instrument_id: UUID
    status: str
    valid_from: date
    evidence: FactEvidence
    valid_to: date | None = None
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(self, "status", _non_blank_text(self.status, "status"))
        start = _plain_date(self.valid_from, "valid_from")
        object.__setattr__(self, "valid_from", start)
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= start:
                raise ProviderContractViolationError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
            object.__setattr__(self, "valid_to", end)
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )


@dataclass(frozen=True, slots=True)
class Bar:
    """One immutable OHLCV bar keyed by the stable instrument id.

    The base never silently repairs data: zero or negative prices and
    quantities are preserved as *raw facts* when the attached evidence
    declares a non-``complete`` quality status, so downstream coverage and
    preflight can mark them invalid instead of losing the audit trail.
    Only facts declared ``complete`` must be consumable, i.e. strictly
    positive prices and non-negative volume/amount.  Floats, booleans,
    NaN, and infinities are rejected in every case.
    """

    instrument_id: UUID
    trade_date: date
    frequency: str
    open: Decimal | int | str | None = None
    high: Decimal | int | str | None = None
    low: Decimal | int | str | None = None
    close: Decimal | int | str | None = None
    volume: Decimal | int | str | None = None
    amount: Decimal | int | str | None = None
    price_basis: PriceBasis = PriceBasis.RAW
    evidence: FactEvidence | None = None
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})
    validation_rule_version: ContractRef | str | None = None
    open_interest: Decimal | int | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        day = _plain_date(self.trade_date, "trade_date")
        object.__setattr__(self, "trade_date", day)
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        if not isinstance(self.price_basis, PriceBasis):
            raise ProviderContractViolationError("price_basis must be a PriceBasis")
        # Complete bars require usable OHLC; non-complete facts retain all
        # source values (including missing and illegal numbers) for audit.
        consumable = self.evidence.quality_status is QualityStatus.COMPLETE
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            normalized = (
                _positive_decimal(value, name)
                if consumable
                else (None if value is None else _finite_decimal(value, name))
            )
            object.__setattr__(self, name, normalized)
        if consumable:
            # A complete fact is safe for downstream consumption only when
            # its OHLC interval is internally coherent.  Invalid source
            # values use a non-complete quality and are preserved above.
            if self.high < self.low:
                raise ProviderContractViolationError("high must not be below low")
            if self.open < self.low or self.open > self.high:
                raise ProviderContractViolationError(
                    "open must be within the [low, high] range"
                )
            if self.close < self.low or self.close > self.high:
                raise ProviderContractViolationError(
                    "close must be within the [low, high] range"
                )
        for name in ("volume", "amount"):
            value = getattr(self, name)
            # Missing volume/turnover is not a business-invalid condition;
            # never turn it into a fabricated zero.
            normalized = (
                None
                if value is None
                else (
                    _non_negative_decimal(value, name)
                    if consumable
                    else _finite_decimal(value, name)
                )
            )
            object.__setattr__(self, name, normalized)
        if self.open_interest is not None:
            object.__setattr__(
                self,
                "open_interest",
                _finite_decimal(self.open_interest, "open_interest"),
            )
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )
        rule_version = self.validation_rule_version
        if rule_version is not None:
            if isinstance(rule_version, ContractRef):
                pass
            elif isinstance(rule_version, str) and rule_version.strip():
                object.__setattr__(
                    self, "validation_rule_version", rule_version.strip()
                )
            else:
                raise ProviderContractViolationError(
                    "validation_rule_version must be a ContractRef or non-blank text"
                )

    @property
    def turnover(self) -> Decimal | None:
        """Alias for the sole persisted turnover/amount value."""

        return self.amount

    @property
    def validation_status(self) -> QualityStatus:
        """Quality status exposed under the Bar protocol's field name."""

        return self.evidence.quality_status

    @property
    def metadata(self) -> Mapping[str, object]:
        """Audit metadata alias; ``attributes`` is the sole storage."""

        return self.attributes


# Semantic alias required by the task package; this is not a second model.
BarFact = Bar


@dataclass(frozen=True, slots=True)
class Tick:
    """One tick observation bounded by an aware instant.

    First-version providers may declare ticks unsupported; the envelope
    exists so the read contract is already stable when they do.
    """

    instrument_id: UUID
    traded_at: datetime
    price: Decimal | int | str
    quantity: Decimal | int | str
    evidence: FactEvidence
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(
            self, "traded_at", _aware_datetime(self.traded_at, "traded_at")
        )
        object.__setattr__(self, "price", _positive_decimal(self.price, "price"))
        object.__setattr__(
            self, "quantity", _non_negative_decimal(self.quantity, "quantity")
        )
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )


@dataclass(frozen=True, slots=True)
class DataPoint:
    """One generic named-series value observed on one calendar date.

    ``series`` identifies the requested value series; ``unit`` optionally
    names the measurement unit.  Values may be any finite sign.
    """

    instrument_id: UUID
    series: str
    point_date: date
    value: Decimal | int | str
    evidence: FactEvidence
    unit: str | None = None
    frequency: str | None = None
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(self, "series", _non_blank_text(self.series, "series"))
        day = _plain_date(self.point_date, "point_date")
        object.__setattr__(self, "point_date", day)
        object.__setattr__(self, "value", _finite_decimal(self.value, "value"))
        object.__setattr__(self, "unit", _optional_text(self.unit, "unit"))
        object.__setattr__(
            self, "frequency", _optional_text(self.frequency, "frequency")
        )
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )


@dataclass(frozen=True, slots=True)
class AdjustedSeriesPoint:
    """One adjustment-factor point for an explicitly chosen price basis.

    The factor is a strictly positive finite decimal; the basis must state
    exactly which series (raw/qfq/hfq) the factor applies to.
    """

    instrument_id: UUID
    point_date: date
    price_basis: PriceBasis
    adj_factor: Decimal | int | str
    evidence: FactEvidence
    # Optional source coordinates keep the normalization relation auditable
    # without making the generic point depend on an ingestion ORM row.
    source_code: str | None = None
    source_trade_date: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        day = _plain_date(self.point_date, "point_date")
        object.__setattr__(self, "point_date", day)
        if not isinstance(self.price_basis, PriceBasis):
            raise ProviderContractViolationError("price_basis must be a PriceBasis")
        object.__setattr__(
            self, "adj_factor", _positive_decimal(self.adj_factor, "adj_factor")
        )
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        if self.source_code is not None:
            object.__setattr__(
                self, "source_code", _optional_text(self.source_code, "source_code")
            )
        if self.source_trade_date is not None:
            object.__setattr__(
                self,
                "source_trade_date",
                _plain_date(self.source_trade_date, "source_trade_date"),
            )
            if self.source_trade_date != self.point_date:
                raise ProviderContractViolationError(
                    "source_trade_date must equal normalized point_date"
                )

    @property
    def effective_date(self) -> date:
        """Normalized effective date alias used by adjustment readers."""

        return self.point_date


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One corporate-action fact with its ex-date and typed action kind."""

    instrument_id: UUID
    action_type: str
    ex_date: date
    evidence: FactEvidence
    schema: ContractRef | None = None
    attributes: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(
            self, "action_type", _non_blank_text(self.action_type, "action_type")
        )
        day = _plain_date(self.ex_date, "ex_date")
        object.__setattr__(self, "ex_date", day)
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        object.__setattr__(
            self, "attributes", _frozen_attributes(self.attributes, "attributes")
        )


@dataclass(frozen=True, slots=True)
class ClosePriceFact:
    """One raw close-price valuation mark with its PIT evidence.

    The analyzer subsystem consumes these facts to freeze the initial
    equity snapshot (E0) and end-of-day valuations; the evidence's
    ``known_at``/``observed_at`` timestamps are the only accepted proof
    that the mark was strictly point-in-time available.
    """

    instrument_id: UUID
    session_date: date
    close_price: Decimal | int | str
    evidence: FactEvidence
    currency: str = "CNY"
    schema: ContractRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument_id", _require_uuid(self.instrument_id, "instrument_id")
        )
        object.__setattr__(
            self, "session_date", _plain_date(self.session_date, "session_date")
        )
        object.__setattr__(
            self, "close_price", _positive_decimal(self.close_price, "close_price")
        )
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ProviderContractViolationError("currency must be non-blank text")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))


@dataclass(frozen=True, slots=True)
class PitRateSnapshotQuery:
    """Half-open session range a frozen risk-free rate snapshot must cover.

    ``expected_sessions`` names the official sessions the caller needs;
    missing sessions become deterministic ``missing_ranges`` in the frozen
    snapshot instead of being forward-filled.
    """

    start_session: date
    end_session: date
    expected_sessions: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "start_session", _plain_date(self.start_session, "start_session")
        )
        object.__setattr__(
            self, "end_session", _plain_date(self.end_session, "end_session")
        )
        if self.end_session < self.start_session:
            raise ProviderContractViolationError(
                "end_session must not precede start_session"
            )
        expected = tuple(
            _plain_date(day, "expected_sessions entry")
            for day in self.expected_sessions
        )
        object.__setattr__(self, "expected_sessions", expected)


@dataclass(frozen=True, slots=True)
class PitRateFact:
    """One daily risk-free rate value with its source and PIT evidence."""

    session_date: date
    rate: Decimal | int | str
    evidence: FactEvidence
    # The source-declared cutoff is separate from ``known_at``.  ``known_at``
    # proves strict PIT provenance; this field states the actual cutoff used
    # by the query and is the boundary checked against the session open.
    data_cutoff_at: datetime
    schema: ContractRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_date", _plain_date(self.session_date, "session_date")
        )
        object.__setattr__(self, "rate", _finite_decimal(self.rate, "rate"))
        if not isinstance(self.evidence, FactEvidence):
            raise ProviderContractViolationError("evidence must be a FactEvidence")
        object.__setattr__(self, "schema", _validated_schema(self.schema, "schema"))
        if (
            not isinstance(self.data_cutoff_at, datetime)
            or self.data_cutoff_at.tzinfo is None
            or self.data_cutoff_at.utcoffset() is None
        ):
            raise ProviderContractViolationError(
                "data_cutoff_at must be timezone-aware"
            )
