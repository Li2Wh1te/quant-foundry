"""Immutable contracts for versioned instrument rule packages.

This module is intentionally free of PostgreSQL, SQLAlchemy, FastAPI,
Tushare, and any concrete data-source client.  It only describes *how*
instrument rule facts are declared, referenced, selected, and summarized:

- :class:`RulePackageDefinition` freezes the field contract, capability
  schema, settlement classes, exception policy, and fixed parse order of
  one ``key + version`` rule package.
- :class:`RuleExceptionSetDefinition` / :class:`RuleExceptionEntry`
  carry named exceptions as *references to facts*, never production
  values.
- :class:`RuleFactCandidate` is the replaceable read-only input boundary
  that a future fact table (or an in-memory Phase 1 fixture) adapts into.
- :class:`RulePackageResolution` / :class:`RulePackageIssue` are the
  structured outcomes: a resolution is either ``ready`` or ``blocked``
  with machine-readable issue codes; missing facts never raise ordinary
  exceptions for callers to guess about.

All objects are frozen after construction and normalize themselves so
that equal inputs always produce byte-stable hash inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping
from types import MappingProxyType
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import (
    VersionedReference,
    _required_label,
)


# ---------------------------------------------------------------------------
# Canonical serialization helpers
# ---------------------------------------------------------------------------


def canonical_decimal_string(value: Decimal) -> str:
    """Return a stable decimal string without trailing zeros or exponents."""

    normalized = value.normalize()
    if normalized == 0:
        # Decimal("0.000").normalize() keeps an exponent ("0"); force plain form.
        return "0"
    return format(normalized, "f")


def canonical_payload(value: Any) -> Any:
    """Recursively convert domain values into JSON-stable primitives.

    Sets become sorted arrays, :class:`Decimal` becomes its canonical
    decimal string, :class:`VersionedReference` becomes ``{key, version}``,
    and timestamps/UUIDs become strings.  Nothing time-, order-, or
    memory-dependent (object reprs, dict iteration order, primary keys)
    survives this conversion.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return canonical_decimal_string(value)
    if isinstance(value, VersionedReference):
        return {"key": value.key, "version": value.version}
    if isinstance(value, StrategyRuleDeclaration):
        return {
            "statements": sorted(canonical_payload(s) for s in value.statements)
        }
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [canonical_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(canonical_payload(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): canonical_payload(item) for key, item in value.items()}
    if isinstance(value, (str, int)):
        return value
    raise DomainValidationError(
        f"value of type {type(value).__name__} is not canonically serializable"
    )


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize a canonical payload to deterministic JSON bytes."""

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def stable_hash(payload: Any) -> str:
    """Return the SHA-256 hex digest of a canonical payload."""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def reference_display(reference: VersionedReference | None) -> str | None:
    """Human/machine display form ``key@version`` used in issue details."""

    if reference is None:
        return None
    return f"{reference.key}@{reference.version}"


def deep_freeze(value: Any) -> Any:
    """Recursively convert a structure into immutable equivalents.

    ``MappingProxyType`` only freezes the outer mapping, so nested dicts
    and lists inside fields, normalized values, and issue details would
    stay mutable and could be edited after the semantic hash was
    computed.  This helper freezes every level: mappings become proxies,
    lists/tuples become tuples, and sets become frozensets.  Domain
    objects (``Decimal``, ``VersionedReference``, frozen dataclasses) are
    already immutable and pass through unchanged.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


# ---------------------------------------------------------------------------
# Enums and small vocabularies
# ---------------------------------------------------------------------------


class ParseMode(StrEnum):
    """Explicit resolution mode; gates fixture-sourced facts.

    ``formal`` is the only production mode and rejects every fact flagged
    ``fixture_only``.  The two acceptance modes exist so Phase 1 can run
    entirely on in-memory fixtures without polluting engine constants.
    """

    PHASE1_FIXTURE = "phase1_fixture@1"
    INTERNAL_LINK_ACCEPTANCE = "internal_link_acceptance@1"
    FORMAL = "formal@1"


class FactQualityStatus(StrEnum):
    """Quality flag carried by each fact candidate."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class ResolutionStatus(StrEnum):
    """Terminal status of one rule-package resolution."""

    READY = "ready"
    BLOCKED = "blocked"


class RuleFieldType(StrEnum):
    """Machine-readable value type of one contracted rule field."""

    POSITIVE_DECIMAL = "positive_decimal"
    NON_NEGATIVE_INT = "non_negative_int"
    VERSIONED_REFERENCE = "versioned_reference"
    STRATEGY_RULE = "strategy_rule"
    SETTLEMENT_CLASS = "settlement_class"
    STRING_SET = "string_set"
    CURRENCY_CODE = "currency_code"
    TRADING_STATUS_APPLICABILITY = "trading_status_applicability"


class TradingStatusRequirement(StrEnum):
    """Whether a trading-status dimension requires point-in-time facts.

    ``not_applicable`` is an explicit declaration that a constraint does
    not apply; it must never be inferred from a missing key.
    """

    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


#: Capability dimensions that v1 requires to be declared explicitly.
CAPABILITY_DIMENSIONS: tuple[str, ...] = (
    "suspension",
    "opening_availability",
    "price_limit_tradability",
)


class RulePackageIssueCode(StrEnum):
    """Stable machine codes for structured blocked results."""

    RULE_REQUIRED_FIELD_MISSING = "RULE_REQUIRED_FIELD_MISSING"
    RULE_FIELD_INVALID = "RULE_FIELD_INVALID"
    RULE_FIELD_CONFLICT = "RULE_FIELD_CONFLICT"
    RULE_PACKAGE_MISMATCH = "RULE_PACKAGE_MISMATCH"
    RULE_EXCEPTION_FACT_MISSING = "RULE_EXCEPTION_FACT_MISSING"
    RULE_EXCEPTION_TARGET_MISMATCH = "RULE_EXCEPTION_TARGET_MISMATCH"
    RULE_EXCEPTION_INTERVAL_CONFLICT = "RULE_EXCEPTION_INTERVAL_CONFLICT"
    RULE_CAPABILITY_DECLARATION_MISSING = "RULE_CAPABILITY_DECLARATION_MISSING"
    RULE_SETTLEMENT_UNKNOWN = "RULE_SETTLEMENT_UNKNOWN"
    RULE_SETTLEMENT_UNSUPPORTED = "RULE_SETTLEMENT_UNSUPPORTED"
    RULE_FIXTURE_SOURCE_FORBIDDEN = "RULE_FIXTURE_SOURCE_FORBIDDEN"
    RULE_FACT_NOT_COMPLETE = "RULE_FACT_NOT_COMPLETE"


# ---------------------------------------------------------------------------
# Strategy-style strong-typed declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyRuleDeclaration:
    """Strong-typed inline statement of strategy-rule semantics.

    Fields such as ``sellable_rule`` accept either a
    :class:`VersionedReference` pointing at a versioned rule definition or
    one of these declarations carrying explicit semantic statements.  A
    declaration with no statements would silently mean "anything" and is
    rejected; statements are deduplicated and sorted for stable hashing.
    """

    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.statements, (str, bytes)) or not hasattr(
            self.statements, "__iter__"
        ):
            raise DomainValidationError("statements must be a collection of strings")
        normalized: list[str] = []
        for statement in self.statements:
            if not isinstance(statement, str):
                raise DomainValidationError("statements must contain strings")
            label = _required_label(statement, "statement")
            normalized.append(label)
        unique = tuple(sorted(set(normalized)))
        if not unique:
            raise DomainValidationError("statements must not be empty")
        object.__setattr__(self, "statements", unique)


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleFieldDefinition:
    """One contracted field of a rule package.

    v1 deliberately has no ``default_value``: every production value must
    come from a fact.  Cross-field constraints (precision representability,
    lot multiples, mandatory ``market`` order type) are validated by the
    resolver, not encoded here.
    """

    name: str
    value_type: RuleFieldType
    required: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_label(self.name, "name"))
        if not isinstance(self.value_type, RuleFieldType):
            raise DomainValidationError("value_type must be a RuleFieldType")
        if not isinstance(self.required, bool):
            raise DomainValidationError("required must be a boolean")


# ---------------------------------------------------------------------------
# Rule package definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleExceptionPolicy:
    """How named exceptions may match instruments in this package.

    ``allowed_match_keys`` documents the only sanctioned matching keys;
    v1 accepts stable ``instrument_id`` plus an explicit validity interval
    and nothing else — trading codes, names, exchanges, and wildcards are
    forbidden matching keys.
    """

    allowed_match_keys: tuple[str, ...]
    carries_production_values: bool = False

    def __post_init__(self) -> None:
        keys = tuple(self.allowed_match_keys)
        if not keys or any(not isinstance(key, str) or not key for key in keys):
            raise DomainValidationError(
                "allowed_match_keys must be a non-empty tuple of strings"
            )
        object.__setattr__(self, "allowed_match_keys", keys)
        if not isinstance(self.carries_production_values, bool):
            raise DomainValidationError(
                "carries_production_values must be a boolean"
            )
        if self.carries_production_values:
            raise DomainValidationError(
                "exception policy must never carry production values"
            )


@dataclass(frozen=True, slots=True)
class RulePackageDefinition:
    """Immutable definition of one ``key + version`` rule package.

    Construction computes the package's own ``semantic_hash`` over the
    full structure, so any later change to fields, settlement classes, or
    parse order necessarily changes identity — new behavior means a new
    package version, never an in-place edit of v1.
    """

    reference: VersionedReference
    supported_asset_classes: frozenset[str]
    field_definitions: tuple[RuleFieldDefinition, ...]
    capability_schema: tuple[str, ...]
    known_settlement_rule_classes: frozenset[str]
    formal_settlement_rule_classes: frozenset[str]
    exception_policy: RuleExceptionPolicy
    parse_order: tuple[str, ...]
    semantic_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference, VersionedReference):
            raise DomainValidationError("reference must be a VersionedReference")
        asset_classes = frozenset(self.supported_asset_classes)
        if not asset_classes or any(
            not isinstance(item, str) or not item.strip()
            for item in asset_classes
        ):
            raise DomainValidationError(
                "supported_asset_classes must be a non-empty set of labels"
            )
        object.__setattr__(self, "supported_asset_classes", asset_classes)

        names: list[str] = []
        for field_definition in self.field_definitions:
            if not isinstance(field_definition, RuleFieldDefinition):
                raise DomainValidationError(
                    "field_definitions must contain RuleFieldDefinition instances"
                )
            names.append(field_definition.name)
        if len(set(names)) != len(names):
            raise DomainValidationError(
                f"duplicate field names in package definition: {names}"
            )
        if not names:
            raise DomainValidationError("field_definitions must not be empty")

        schema = tuple(self.capability_schema)
        if len(set(schema)) != len(schema) or not schema:
            raise DomainValidationError(
                "capability_schema must be a non-empty tuple of unique dimensions"
            )
        object.__setattr__(self, "capability_schema", schema)

        known = frozenset(self.known_settlement_rule_classes)
        formal = frozenset(self.formal_settlement_rule_classes)
        if not known:
            raise DomainValidationError(
                "known_settlement_rule_classes must not be empty"
            )
        if not formal <= known:
            raise DomainValidationError(
                "formal_settlement_rule_classes must be a subset of known classes"
            )
        object.__setattr__(self, "known_settlement_rule_classes", known)
        object.__setattr__(self, "formal_settlement_rule_classes", formal)

        if not isinstance(self.exception_policy, RuleExceptionPolicy):
            raise DomainValidationError(
                "exception_policy must be a RuleExceptionPolicy"
            )

        parse_order = tuple(self.parse_order)
        if not parse_order or any(
            not isinstance(step, str) or not step.strip() for step in parse_order
        ):
            raise DomainValidationError(
                "parse_order must be a non-empty tuple of step identifiers"
            )
        if len(set(parse_order)) != len(parse_order):
            raise DomainValidationError("parse_order steps must be unique")
        object.__setattr__(self, "parse_order", parse_order)

        object.__setattr__(
            self, "semantic_hash", self._compute_semantic_hash()
        )

    def _compute_semantic_hash(self) -> str:
        payload = canonical_payload(
            {
                "kind": "rule_package_definition",
                "reference": self.reference,
                "supported_asset_classes": self.supported_asset_classes,
                "field_definitions": [
                    {
                        "name": field_definition.name,
                        "value_type": field_definition.value_type,
                        "required": field_definition.required,
                    }
                    for field_definition in self.field_definitions
                ],
                "capability_schema": self.capability_schema,
                "known_settlement_rule_classes": (
                    self.known_settlement_rule_classes
                ),
                "formal_settlement_rule_classes": (
                    self.formal_settlement_rule_classes
                ),
                "exception_policy": {
                    "allowed_match_keys": (
                        self.exception_policy.allowed_match_keys
                    ),
                    "carries_production_values": (
                        self.exception_policy.carries_production_values
                    ),
                },
                "parse_order": self.parse_order,
            }
        )
        return stable_hash(payload)

    def field_names(self) -> tuple[str, ...]:
        """Return contracted field names in definition order."""

        return tuple(field.name for field in self.field_definitions)

    def field_by_name(self, name: str) -> RuleFieldDefinition | None:
        """Return the definition of one contracted field, if present."""

        for field_definition in self.field_definitions:
            if field_definition.name == name:
                return field_definition
        return None


# ---------------------------------------------------------------------------
# Named exceptions
# ---------------------------------------------------------------------------


def _validate_interval(
    valid_from: date, valid_to: date | None, *, prefix: str
) -> None:
    """Validate a half-open ``[valid_from, valid_to)`` calendar interval."""

    if not isinstance(valid_from, date) or isinstance(valid_from, datetime):
        raise DomainValidationError(f"{prefix}valid_from must be a calendar date")
    if valid_to is None:
        return
    if not isinstance(valid_to, date) or isinstance(valid_to, datetime):
        raise DomainValidationError(f"{prefix}valid_to must be a calendar date")
    if valid_to <= valid_from:
        raise DomainValidationError(
            f"{prefix}valid_to must be later than valid_from (half-open interval)"
        )


@dataclass(frozen=True, slots=True)
class RuleExceptionEntry:
    """One named exception: an instrument routed to alternate facts.

    The entry stores only a reference to independently sourced facts plus
    an explicit half-open validity interval.  Production numbers have no
    place here by construction.
    """

    instrument_id: UUID
    exception_fact_ref: VersionedReference
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        if not isinstance(self.exception_fact_ref, VersionedReference):
            raise DomainValidationError(
                "exception_fact_ref must be a VersionedReference"
            )
        _validate_interval(self.valid_from, self.valid_to, prefix="")

    def covers(self, day: date) -> bool:
        """Return whether this entry is effective on ``day`` (half-open)."""

        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


@dataclass(frozen=True, slots=True)
class RuleExceptionSetDefinition:
    """A versioned set of named exceptions bound to exactly one package.

    Every entry routes through ``exception_fact_ref`` to facts owned by an
    independent provider.  Overlapping intervals for the same instrument
    are detected by the resolver and surface as structured blocks, not as
    constructor errors, so bad data can be reported per resolution.
    """

    reference: VersionedReference
    package_reference: VersionedReference
    entries: tuple[RuleExceptionEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reference, VersionedReference):
            raise DomainValidationError("reference must be a VersionedReference")
        if not isinstance(self.package_reference, VersionedReference):
            raise DomainValidationError(
                "package_reference must be a VersionedReference"
            )
        entries = tuple(self.entries)
        for entry in entries:
            if not isinstance(entry, RuleExceptionEntry):
                raise DomainValidationError(
                    "entries must contain RuleExceptionEntry instances"
                )
        object.__setattr__(self, "entries", entries)


# ---------------------------------------------------------------------------
# Fact input boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleFactCandidate:
    """One read-only candidate fact supplied by a replaceable provider.

    This is *not* a database model: a future ``instrument_rule_facts``
    table task will adapt rows into these immutable candidates.  Raw field
    values stay uninterpreted here; the resolver owns all validation so
    that missing or invalid facts become structured blocks instead of
    constructor exceptions.
    """

    instrument_id: UUID
    package_reference: VersionedReference
    source: str
    source_revision: str | None
    known_at: datetime
    observed_at: datetime
    quality_status: FactQualityStatus
    fixture_only: bool
    fields: Mapping[str, Any]
    exception_fact_ref: VersionedReference | None = None
    valid_from: date | None = None
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        if not isinstance(self.package_reference, VersionedReference):
            raise DomainValidationError(
                "package_reference must be a VersionedReference"
            )
        if self.exception_fact_ref is not None and not isinstance(
            self.exception_fact_ref, VersionedReference
        ):
            raise DomainValidationError(
                "exception_fact_ref must be a VersionedReference when provided"
            )
        object.__setattr__(self, "source", _required_label(self.source, "source"))
        _validate_interval(self.valid_from, self.valid_to, prefix="")
        if not isinstance(self.quality_status, FactQualityStatus):
            raise DomainValidationError(
                "quality_status must be a FactQualityStatus"
            )
        if not isinstance(self.fixture_only, bool):
            raise DomainValidationError("fixture_only must be a boolean")
        if not isinstance(self.fields, Mapping):
            raise DomainValidationError("fields must be a mapping")
        # Raw values may contain nested dicts/lists; freeze every level so
        # candidates cannot be edited after construction.
        object.__setattr__(self, "fields", deep_freeze(self.fields))
        # Visibility filtering compares these against data_cutoff, so naive
        # timestamps would silently change what a query is allowed to see.
        object.__setattr__(
            self, "known_at", _aware_datetime(self.known_at, "known_at")
        )
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "observed_at")
        )

    def covers(self, day: date) -> bool:
        """Return whether this fact is effective on ``day`` (half-open).

        Facts without an explicit validity window are treated as always
        applicable; providers that model validity must set the bounds.
        """

        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day >= self.valid_to:
            return False
        return True

    def summary(
        self,
        *,
        exception_set_reference: VersionedReference | None = None,
    ) -> "ResolvedFactSummary":
        """Return the stable provenance summary used in resolutions."""

        return ResolvedFactSummary(
            source=self.source,
            source_revision=self.source_revision,
            exception_fact_ref=self.exception_fact_ref,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            exception_set_reference=exception_set_reference,
        )


# ---------------------------------------------------------------------------
# Resolution results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedFactSummary:
    """Stable provenance record of one contributing fact candidate.

    ``exception_set_reference`` records *which versioned exception set*
    routed to this fact, so two exception-set versions pointing at the
    same fact reference remain distinguishable in audits and hashes.
    """

    source: str
    source_revision: str | None
    exception_fact_ref: VersionedReference | None
    valid_from: date | None
    valid_to: date | None
    exception_set_reference: VersionedReference | None = None


@dataclass(frozen=True, slots=True)
class RulePackageIssue:
    """One structured reason a resolution ended up blocked.

    ``code`` is a stable machine value, ``message`` is concise Chinese for
    operators, and ``details`` holds JSON-serializable, non-sensitive
    context.  Messages never participate in the semantic hash.
    """

    code: RulePackageIssueCode
    message: str
    field: str | None = None
    instrument_id: UUID | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, RulePackageIssueCode):
            raise DomainValidationError("code must be a RulePackageIssueCode")
        if not isinstance(self.message, str) or not self.message.strip():
            raise DomainValidationError("message must be non-blank text")
        if self.details is not None:
            if not isinstance(self.details, Mapping):
                raise DomainValidationError("details must be a mapping")
            # Fail fast on non-serializable details instead of breaking the
            # log/query pipeline later, then freeze every nesting level.
            canonical_payload(self.details)
            object.__setattr__(self, "details", deep_freeze(self.details))


@dataclass(frozen=True, slots=True)
class RulePackageResolution:
    """Immutable outcome of resolving rules for one instrument instant.

    A ``ready`` resolution carries fully normalized production values and
    is the only status downstream engines may consume.  A ``blocked``
    resolution never exposes half-finished rule objects: its normalized
    values are empty and its issues carry machine-readable codes.

    ``exception_set_reference`` is the single exception-set version that
    routed the resolution (exactly one match); ``exception_set_references``
    lists, in stable order, every exception-set version that produced a
    covering entry for the instrument — including all participants of an
    interval conflict — so blocked runs remain auditable.
    """

    status: ResolutionStatus
    package_reference: VersionedReference
    parse_order: tuple[str, ...]
    parser_revision: str
    semantic_hash: str
    exception_reference: VersionedReference | None = None
    exception_set_reference: VersionedReference | None = None
    exception_set_references: tuple[VersionedReference, ...] = ()
    selected_facts: tuple[ResolvedFactSummary, ...] = ()
    normalized_values: Mapping[str, Any] = MappingProxyType({})
    capability_declarations: Mapping[str, str] = MappingProxyType({})
    issues: tuple[RulePackageIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResolutionStatus):
            raise DomainValidationError("status must be a ResolutionStatus")
        if not isinstance(self.package_reference, VersionedReference):
            raise DomainValidationError(
                "package_reference must be a VersionedReference"
            )
        issues = tuple(self.issues)
        for issue in issues:
            if not isinstance(issue, RulePackageIssue):
                raise DomainValidationError(
                    "issues must contain RulePackageIssue instances"
                )
        object.__setattr__(self, "issues", issues)

        # Validate and canonicalize the exception-set participants before
        # freezing: the container itself must truly be iterable (probed
        # with iter(), since a bogus __iter__ attribute would pass a
        # hasattr check), every element must be a VersionedReference, and
        # the tuple is deduplicated and sorted by (key, version) so the
        # audit trail cannot depend on caller-supplied order.
        if isinstance(self.exception_set_references, (str, bytes)):
            raise DomainValidationError(
                "exception_set_references must be an iterable of "
                "VersionedReference instances"
            )
        try:
            candidate_references = tuple(self.exception_set_references)
        except TypeError as exc:
            raise DomainValidationError(
                "exception_set_references must be an iterable of "
                "VersionedReference instances"
            ) from exc
        seen_exception_sets: dict[tuple[str, int], VersionedReference] = {}
        for reference in candidate_references:
            if not isinstance(reference, VersionedReference):
                raise DomainValidationError(
                    "exception_set_references must contain "
                    "VersionedReference instances"
                )
            seen_exception_sets[(reference.key, reference.version)] = reference
        object.__setattr__(
            self,
            "exception_set_references",
            tuple(
                sorted(
                    seen_exception_sets.values(),
                    key=lambda item: (item.key, item.version),
                )
            ),
        )

        # Reject non-mapping structures with a domain error instead of an
        # accidental AttributeError (or a silently frozen wrong type) from
        # deep_freeze downstream.
        if not isinstance(self.normalized_values, Mapping):
            raise DomainValidationError("normalized_values must be a mapping")
        for key in self.normalized_values:
            if not isinstance(key, str) or not key.strip():
                raise DomainValidationError(
                    "normalized_values keys must be non-blank field-name strings"
                )
        if not isinstance(self.capability_declarations, Mapping):
            raise DomainValidationError(
                "capability_declarations must be a mapping"
            )
        for key, value in self.capability_declarations.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise DomainValidationError(
                    "capability_declarations must map dimension strings "
                    "to requirement strings"
                )
        # Deep-freeze the value-bearing structures: a nested dict mutated
        # after hashing would silently invalidate the semantic hash.
        object.__setattr__(
            self, "normalized_values", deep_freeze(self.normalized_values)
        )
        object.__setattr__(
            self,
            "capability_declarations",
            deep_freeze(self.capability_declarations),
        )
        if self.status is ResolutionStatus.BLOCKED:
            if self.normalized_values or self.capability_declarations:
                raise DomainValidationError(
                    "a blocked resolution must not expose normalized values"
                )
        else:
            if issues:
                raise DomainValidationError(
                    "a ready resolution must not carry issues"
                )
