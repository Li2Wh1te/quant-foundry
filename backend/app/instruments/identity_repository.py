"""Append-only repositories for stable instrument identity facts.

The ingestion catalogue (notably ``etf_codes``) is a mutable current
snapshot.  This module is the authoritative historical read path: identity,
mapping, and display facts are appended with evidence and selected by their
effective session plus ``data_cutoff`` knowledge boundary.  No repository
method updates or deletes a fact row.

The repositories intentionally accept a SQLAlchemy ``Session``-like object.
That keeps the domain boundary easy to unit-test while production callers use
the normal PostgreSQL session and transaction supplied by the application.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.backtesting.data.errors import (
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
)
from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.data_ingestion.models.etf import EtfEntity
from app.instruments.domain import (
    AuthorityStatus,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentDisplayFact,
    InstrumentIdentityFact,
    InstrumentIdentityResolution,
    InstrumentStatus,
    MappingConflictError,
    MappingCoverageGapError,
    _identity_resolution_evidence_summary,
    _optional_label,
    _positive_version,
)
from app.instruments.models import (
    DisplayResolutionHead,
    Instrument,
    InstrumentCodeMappingRecord,
    InstrumentDisplayFactRecord,
    InstrumentIdentityFactRecord,
    MappingResolutionHead,
)
from app.instruments.repository import (
    InstrumentCodeMappingRepository,
    _date_range_value,
    _knowledge_range_value,
)

__all__ = [
    "DisplayFactConflictError",
    "DisplayResolutionRepository",
    "DisplayFactVersionExistsError",
    "IdentityFactConflictError",
    "IdentityFactVersionExistsError",
    "IdentityMergeEvidenceMissingError",
    "InstrumentDisplayFactRepository",
    "InstrumentIdentityFactRepository",
    "InstrumentIdentityRepository",
    "InstrumentIdentityService",
    "IdentityRepository",
    "IdentityFactRepository",
    "DisplayFactRepository",
    "MappingResolutionRepository",
    "normalize_identity_lookup_key",
    "migrate_existing_etf_identities",
    "resolve_instrument_identity",
]


class IdentityFactVersionExistsError(DomainValidationError):
    """An immutable identity fact version is already present."""


class DisplayFactVersionExistsError(DomainValidationError):
    """An immutable display fact version is already present."""


class IdentityFactConflictError(IdentityMappingConflictError):
    """More than one identity fact is authoritative for a requested day."""


class DisplayFactConflictError(IdentityMappingConflictError):
    """More than one display fact is authoritative for a requested day."""


class IdentityMergeEvidenceMissingError(IdentityMappingEvidenceMissingError):
    """An identity merge was requested without concrete evidence."""


def normalize_identity_lookup_key(source: str, source_code: str, effective_session: date) -> str:
    """Return the canonical lock key for an identity import.

    The normalization is used only for synchronization and matching.  The
    original source/source-code spelling remains in the immutable fact so an
    audit can reproduce exactly what the provider supplied.
    """

    if not isinstance(source, str) or not source.strip():
        raise DomainValidationError("source must be non-blank text")
    if not isinstance(source_code, str) or not source_code.strip():
        raise DomainValidationError("source_code must be non-blank text")
    if not isinstance(effective_session, date) or isinstance(effective_session, datetime):
        raise DomainValidationError("effective_session must be a calendar date")
    return (
        f"{source.strip().casefold()}|{source_code.strip().casefold()}|"
        f"{effective_session.isoformat()}"
    )


def _normalized_identifier(value: str, field_name: str) -> str:
    """Validate and trim a source identifier without changing its audit spelling."""

    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be non-blank text")
    return value.strip()


def _lookup_identifier(value: str) -> str:
    """Return the canonical comparison form for a source identifier."""

    return value.strip().casefold()


def _require_instrument_id(value: object) -> UUID:
    """Validate the stable identity key before issuing any repository query."""

    if not isinstance(value, UUID):
        raise DomainValidationError("instrument_id must be a UUID")
    return value


def _ensure_instrument_exists(session: Session, instrument_id: UUID) -> None:
    """Reject facts that do not point at a stable identity row.

    A few dependency-light repository tests use a session double without a
    ``get`` method, and a few SQLite tests create only the fact table.  In
    those intentionally incomplete harnesses the database foreign key remains
    the final guard; a production SQLAlchemy session always checks eagerly.
    """

    getter = getattr(session, "get", None)
    if not callable(getter):
        return
    try:
        row = getter(Instrument, instrument_id)
    except OperationalError as exc:
        # SQLite permits creating a fact table without its referenced table;
        # do not turn that test-fixture limitation into a domain failure.
        if "no such table" in str(exc).lower():
            return
        raise
    if row is None:
        raise DomainValidationError(
            f"instrument identity {instrument_id} does not exist"
        )


def _fact_chain(
    session: Session, record_type: type, logical_fact_key: str
) -> tuple[object, ...]:
    """Load one logical fact chain when the session exposes SQL execution."""

    execute = getattr(session, "execute", None)
    if not callable(execute):
        return ()
    result = execute(
        select(record_type).where(
            record_type.logical_fact_key == logical_fact_key
        )
    )
    return tuple(result.scalars().all())


def _visible_fact_rows(
    session: Session,
    record_type: type,
    instrument_id: UUID,
    cutoff: datetime,
) -> tuple[object, ...]:
    """Load all cutoff-visible revisions before effective-date folding.

    Effective-date predicates must be applied only after a logical revision
    chain is folded.  Otherwise a correction that changes its validity range
    can leave its predecessor looking authoritative for dates the correction
    explicitly removed.
    """

    return tuple(
        session.execute(
            select(record_type)
            .where(
                record_type.instrument_id == instrument_id,
                record_type.known_at <= cutoff,
            )
            .order_by(record_type.logical_fact_key, record_type.fact_version)
        )
        .scalars()
        .all()
    )


def _validate_fact_append(
    session: Session,
    fact: object,
    record_type: type,
    *,
    allow_historical: bool = False,
) -> tuple[object, ...]:
    """Validate identity, immutable id, and predecessor-chain invariants.

    The check is deliberately shared by identity and display facts.  A
    correction must append exactly the next version and point at the previous
    row in the same logical chain; this prevents a typo from silently creating
    an unrelated history branch.
    """

    instrument_id = getattr(fact, "instrument_id")
    _ensure_instrument_exists(session, instrument_id)
    logical_fact_key = getattr(fact, "logical_fact_key")
    chain = _fact_chain(session, record_type, logical_fact_key)

    execute = getattr(session, "execute", None)
    if callable(execute):
        duplicate_id = execute(
            select(record_type.id).where(record_type.id == getattr(fact, "fact_id"))
        ).scalars().first()
        if duplicate_id is not None:
            raise DomainValidationError(
                f"fact id {getattr(fact, 'fact_id')} already exists; facts are immutable"
            )

    fact_version = getattr(fact, "fact_version")
    if not chain:
        if fact_version != 1:
            raise DomainValidationError(
                "the first fact in a logical chain must have fact_version 1"
            )
        if getattr(fact, "supersedes_fact_id") is not None:
            raise DomainValidationError(
                "supersedes_fact_id must reference an existing fact in the same logical chain"
            )
        return chain

    latest = max(
        chain,
        key=lambda row: (
            getattr(row, "fact_version"),
            _stored_aware(getattr(row, "known_at"), "known_at"),
        ),
    )
    expected_version = getattr(latest, "fact_version") + 1
    if getattr(fact, "fact_version") != expected_version:
        raise DomainValidationError(
            f"fact_version must be {expected_version} after the current logical fact"
        )
    predecessor_id = getattr(fact, "supersedes_fact_id")
    if predecessor_id is None:
        raise DomainValidationError(
            "a revision must reference the preceding fact through supersedes_fact_id"
        )
    if predecessor_id != getattr(latest, "id"):
        raise DomainValidationError(
            "supersedes_fact_id must reference the latest fact in the same logical chain"
        )
    if getattr(latest, "instrument_id") != instrument_id:
        raise DomainValidationError(
            "supersedes_fact_id must belong to the same instrument identity"
        )
    latest_known_at = _stored_aware(getattr(latest, "known_at"), "known_at")
    fact_known_at = _stored_aware(getattr(fact, "known_at"), "known_at")
    if not allow_historical and fact_known_at <= latest_known_at:
        raise DomainValidationError(
            "ordinary fact appends require known_at to be strictly later "
            "than the latest fact; use append_historical_fact or "
            "append_reconstructed_fact for an explicitly reconstructed chain"
        )
    # Historical reconstruction is deliberately opt-in and still obeys the
    # exact next-version and predecessor checks above.  PIT readers use
    # known_at as the visibility boundary and fact_version as the deterministic
    # tie-breaker at one knowledge instant.
    return chain


def migrate_existing_etf_identities(session: Session) -> int:
    """Ensure every legacy ``etf_entities.id`` has the same generic identity.

    This compatibility helper mirrors migration ``20260822_03`` for
    installations that imported ETF rows before the generic identity table.
    It copies only the UUID and asset-class partition; it never fabricates a
    historical mapping or display fact from ``etf_codes``.
    """

    existing = set(session.scalars(select(Instrument.id)).all())
    etf_ids = tuple(session.scalars(select(EtfEntity.id)).all())
    missing = [identity_id for identity_id in etf_ids if identity_id not in existing]
    for identity_id in missing:
        session.add(
            Instrument(
                id=identity_id,
                asset_class="etf",
                status=InstrumentStatus.ACTIVE.value,
            )
        )
    return len(missing)


def _range_covers(fact: object, effective_at: datetime) -> bool:
    """Check a day-granularity fact interval against an aware instant."""

    day = effective_at.date()
    valid_from = getattr(fact, "valid_from")
    valid_to = getattr(fact, "valid_to")
    return valid_from <= day and (valid_to is None or day < valid_to)


def _stored_aware(value: datetime, field_name: str) -> datetime:
    """Restore SQLite's lost timezone marker for deterministic unit tests.

    PostgreSQL returns ``timestamptz`` values as aware datetimes.  SQLite's
    portable DateTime renderer drops that marker; treating such test-storage
    values as UTC keeps the domain contract strict without weakening it for
    production databases.
    """

    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        return value.replace(tzinfo=UTC)
    return _aware_datetime(value, field_name)


def _query_datetimes(
    effective_at: object, data_cutoff: object
) -> tuple[datetime, datetime]:
    """Validate the two independent PIT coordinates before querying.

    Keeping this check at the repository boundary prevents malformed strings,
    ``None`` values, and naive timestamps from surfacing as incidental
    ``AttributeError``/``TypeError`` exceptions in range folding.
    """

    if not isinstance(effective_at, datetime):
        raise DomainValidationError("effective_at must be a datetime")
    if not isinstance(data_cutoff, datetime):
        raise DomainValidationError("data_cutoff must be a datetime")
    return (
        _aware_datetime(effective_at, "effective_at"),
        _aware_datetime(data_cutoff, "data_cutoff"),
    )


def _identity_error_detail_value(value: object) -> object:
    """Render identity diagnostics as values accepted by stable errors."""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _identity_error_detail_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_identity_error_detail_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _identity_error_detail_value(enum_value)
    return repr(value)


def _identity_error_details(
    instrument_id: UUID,
    *,
    source: str,
    effective_at: datetime,
    data_cutoff: datetime,
    expected: object,
    actual: object,
    fact_version: object = None,
    source_code: object = None,
    **extra: object,
) -> dict[str, object]:
    """Build the common public fields for identity/PIT resolution errors."""

    effective = _aware_datetime(effective_at, "effective_at")
    cutoff = _aware_datetime(data_cutoff, "data_cutoff")
    details: dict[str, object] = {
        "instrument_id": str(instrument_id),
        "source": source,
        "source_code": source_code,
        "session_date": effective.date().isoformat(),
        "expected": expected,
        "actual": actual,
        "data_cutoff": cutoff.isoformat(),
        "fact_version": fact_version,
        "effective_at": effective.isoformat(),
    }
    # Keep the legacy aliases while making the canonical names above
    # available to callers that persist or query error details.
    details["session"] = details["session_date"]
    details["expected_value"] = details["expected"]
    details["actual_value"] = details["actual"]
    details.update(extra)
    return {
        str(key): _identity_error_detail_value(value)
        for key, value in details.items()
    }


def _require_fact_evidence(
    fact: object,
    *,
    instrument_id: UUID,
    source: str,
    effective_at: datetime,
    data_cutoff: datetime,
    description: str,
) -> None:
    """Reject a cutoff-visible fact that cannot prove its provenance.

    Domain constructors enforce this invariant for normal values, but the
    pure resolver also accepts provider materializations.  Rechecking at the
    resolution boundary keeps corrupted or hand-built values from becoming
    historical identity evidence.  The same public detail shape is used for
    identity, display, and source-code mapping facts.
    """

    evidence = getattr(fact, "evidence", None)
    if isinstance(evidence, str) and evidence.strip():
        return
    raise IdentityMappingEvidenceMissingError(
        f"the {description} covering the requested instant carries no evidence",
        details=_identity_error_details(
            instrument_id,
            source=source,
            source_code=getattr(fact, "source_code", None),
            effective_at=effective_at,
            data_cutoff=data_cutoff,
            expected="non-blank evidence",
            actual=evidence,
            fact_version=getattr(fact, "fact_version", None),
            fact_id=getattr(fact, "fact_id", None),
            logical_fact_key=getattr(fact, "logical_fact_key", None),
        ),
    )


def resolve_instrument_identity(
    instrument_id: UUID,
    *,
    effective_at: datetime,
    data_cutoff: datetime,
    identity_facts: Sequence[InstrumentIdentityFact] = (),
    display_facts: Sequence[InstrumentDisplayFact] = (),
    mappings: Sequence[InstrumentCodeMapping] = (),
    source: str | None = None,
) -> InstrumentIdentityResolution | None:
    """Resolve immutable in-memory facts with the same PIT rules as storage.

    This pure helper is useful to adapters and tests that already fetched
    facts in one transaction.  It never reads ``etf_codes`` or chooses a
    current snapshot as a fallback.
    """

    if not isinstance(instrument_id, UUID):
        raise DomainValidationError("instrument_id must be a UUID")
    effective = _aware_datetime(effective_at, "effective_at")
    cutoff = _aware_datetime(data_cutoff, "data_cutoff")
    for fact in (*identity_facts, *display_facts, *mappings):
        if getattr(fact, "instrument_id", instrument_id) != instrument_id:
            raise DomainValidationError(
                "all identity facts must belong to the requested instrument_id"
            )
    # A logical fact chain may contain each revision only once.  Repository
    # append paths enforce ``(logical_fact_key, fact_version)`` uniqueness;
    # the in-memory adapter must enforce the same invariant before the
    # latest-by-knowledge-time fold could silently hide a duplicate version.
    visible_identity_facts = tuple(
        fact
        for fact in identity_facts
        if _stored_aware(fact.known_at, "known_at") <= cutoff
    )
    for fact in visible_identity_facts:
        _require_fact_evidence(
            fact,
            instrument_id=instrument_id,
            source="identity_fact",
            effective_at=effective,
            data_cutoff=cutoff,
            description="identity fact",
        )
    _check_duplicate_fact_versions(
        visible_identity_facts,
        instrument_id=instrument_id,
        source="identity_fact",
        effective_at=effective,
        data_cutoff=cutoff,
        error_type=IdentityFactConflictError,
    )
    visible_display_facts = tuple(
        fact
        for fact in display_facts
        if _stored_aware(fact.known_at, "known_at") <= cutoff
    )
    for fact in visible_display_facts:
        _require_fact_evidence(
            fact,
            instrument_id=instrument_id,
            source=getattr(fact, "source", None) or "display_fact",
            effective_at=effective,
            data_cutoff=cutoff,
            description="display fact",
        )
    _check_duplicate_fact_versions(
        visible_display_facts,
        instrument_id=instrument_id,
        source="display_fact",
        effective_at=effective,
        data_cutoff=cutoff,
        error_type=DisplayFactConflictError,
    )
    identity_candidates = _visible_latest(
        visible_identity_facts, effective_at=effective, data_cutoff=cutoff
    )
    if len(identity_candidates) > 1:
        raise IdentityFactConflictError(
            "multiple identity facts cover the requested instant",
            details=_identity_error_details(
                instrument_id,
                source="identity_fact",
                effective_at=effective,
                data_cutoff=cutoff,
                expected="one identity fact",
                actual=len(identity_candidates),
                fact_versions=[
                    getattr(item, "fact_version") for item in identity_candidates
                ],
            ),
        )
    identity_fact = identity_candidates[0] if identity_candidates else None
    display_candidates = _visible_latest(
        visible_display_facts,
        effective_at=effective,
        data_cutoff=cutoff,
        authoritative_only=True,
    )
    display = None
    selected_display_fact: InstrumentDisplayFact | None = None
    if display_candidates:
        highest = max(getattr(item, "authority_rank") for item in display_candidates)
        leaders = [item for item in display_candidates if getattr(item, "authority_rank") == highest]
        if len(leaders) != 1:
            raise DisplayFactConflictError(
                "multiple authoritative display facts cover the requested instant",
                details=_identity_error_details(
                    instrument_id,
                    source="display_fact",
                    effective_at=effective,
                    data_cutoff=cutoff,
                    expected="one authoritative display fact",
                    actual=len(leaders),
                    fact_versions=[
                        getattr(item, "fact_version") for item in leaders
                    ],
                    sources=[getattr(item, "source", None) for item in leaders],
                ),
            )
        selected_display_fact = leaders[0]
        display = selected_display_fact.as_display()
    mapping_scope = (
        _lookup_identifier(_normalized_identifier(source, "source"))
        if source is not None
        else None
    )
    visible_mappings = tuple(
        fact
        for fact in mappings
        if (mapping_scope is None or _lookup_identifier(fact.source) == mapping_scope)
        and _stored_aware(fact.known_at, "known_at") <= cutoff
    )
    for fact in visible_mappings:
        _require_fact_evidence(
            fact,
            instrument_id=instrument_id,
            source=getattr(fact, "source", None) or source or "mapping",
            effective_at=effective,
            data_cutoff=cutoff,
            description="mapping fact",
        )
    _check_duplicate_fact_versions(
        visible_mappings,
        instrument_id=instrument_id,
        source=source or "mapping",
        effective_at=effective,
        data_cutoff=cutoff,
        error_type=IdentityMappingConflictError,
    )
    mapping_by_logical: dict[str, InstrumentCodeMapping] = {}
    for fact in visible_mappings:
        key = getattr(fact, "logical_fact_key", None) or f"fact:{fact.fact_id}"
        current = mapping_by_logical.get(key)
        if current is None or _fact_order(fact) > _fact_order(current):
            mapping_by_logical[key] = fact
    mapping_candidates = [
        fact for fact in mapping_by_logical.values() if fact.covers(effective.date())
    ]
    if len(mapping_candidates) > 1:
        raise IdentityMappingConflictError(
            "multiple mapping facts cover the requested instant",
            details=_identity_error_details(
                instrument_id,
                source=source or "mapping",
                effective_at=effective,
                data_cutoff=cutoff,
                expected="one mapping fact",
                actual=len(mapping_candidates),
                fact_version=None,
                source_code=None,
                source_codes=[item.source_code for item in mapping_candidates],
                fact_versions=[
                    getattr(item, "fact_version", None)
                    for item in mapping_candidates
                ],
            ),
        )
    mapping = mapping_candidates[0] if mapping_candidates else None
    if identity_fact is None and display is None and mapping is None:
        return None
    summary = _identity_resolution_evidence_summary(
        instrument_id=instrument_id,
        effective_at=effective,
        data_cutoff=cutoff,
        identity_fact=identity_fact,
        display_fact=selected_display_fact,
        mapping=mapping,
        source=source,
    )
    return InstrumentIdentityResolution(
        instrument_id=instrument_id,
        identity_fact=identity_fact,
        display=display,
        mapping=mapping,
        evidence_summary=summary,
    )


def _visible_latest(
    facts: Iterable[object],
    *,
    effective_at: datetime,
    data_cutoff: datetime,
    authoritative_only: bool = False,
) -> list[object]:
    """Select latest visible revision per logical fact key.

    Corrections use one logical key and a new fact version.  A query must see
    the latest revision known by its cutoff, while independent logical keys
    remain separate candidates and are checked for conflicts by the caller.
    """

    cutoff = _aware_datetime(data_cutoff, "data_cutoff")
    grouped: dict[str, object] = {}
    for fact in facts:
        known_at = _stored_aware(getattr(fact, "known_at"), "known_at")
        if known_at > cutoff:
            continue
        if authoritative_only:
            status = getattr(fact, "authority_status", None)
            if getattr(status, "value", status) != AuthorityStatus.AUTHORITATIVE.value:
                continue
        key = getattr(fact, "logical_fact_key", None) or (
            f"fact:{getattr(fact, 'fact_id', id(fact))}"
        )
        current = grouped.get(key)
        if current is None or _fact_order(fact) > _fact_order(current):
            grouped[key] = fact
    return [
        fact
        for fact in grouped.values()
        if _range_covers(fact, effective_at)
    ]


_FACT_VERSION_FIELDS = (
    "fact_id",
    "logical_fact_key",
    "fact_version",
    "instrument_id",
    "valid_from",
    "valid_to",
    "known_at",
    "observed_at",
    "supersedes_fact_id",
    "evidence",
    "source",
    "source_code",
    "trading_code",
    "mapping_source",
    "source_revision",
    "asset_class",
    "exchange",
    "currency",
    "calendar_id",
    "name",
    "display_name",
    "authority_rank",
    "authority_status",
)


def _fact_version_key(fact: object) -> tuple[str, object]:
    """Return the stable logical-chain/version key for one fact."""

    logical_key = getattr(fact, "logical_fact_key", None)
    if not isinstance(logical_key, str) or not logical_key.strip():
        fact_id = getattr(fact, "fact_id", None)
        logical_key = (
            f"fact:{fact_id}" if fact_id is not None else f"object:{id(fact)}"
        )
    else:
        logical_key = logical_key.strip()
    return logical_key, getattr(fact, "fact_version", 1)


def _fact_materialization_signature(fact: object) -> tuple[object, ...]:
    """Build a comparison signature for repeated materializations.

    A repository may hand the resolver the same persisted row more than once
    after joins or batching.  Repeating that exact row is harmless, while two
    different rows claiming the same logical version are an integrity error.
    """

    return tuple(
        _identity_error_detail_value(getattr(fact, field_name, None))
        for field_name in _FACT_VERSION_FIELDS
    )


def _same_fact_materialization(first: object, second: object) -> bool:
    """Return whether two values represent one persisted fact row."""

    first_id = getattr(first, "fact_id", None)
    second_id = getattr(second, "fact_id", None)
    if first_id is None or second_id is None:
        # Corrupted/legacy values without an immutable id can only be safely
        # repeated when they are literally the same object.  Equal-looking
        # independent values would otherwise create an unidentifiable fork.
        return first is second
    return first_id == second_id and (
        _fact_materialization_signature(first)
        == _fact_materialization_signature(second)
    )


def _check_duplicate_fact_versions(
    facts: Sequence[object],
    *,
    instrument_id: UUID,
    source: str,
    effective_at: datetime,
    data_cutoff: datetime,
    error_type: type[IdentityMappingConflictError],
) -> None:
    """Reject conflicting duplicate logical fact revisions before folding.

    The check runs after the caller's knowledge-cutoff filtering, so facts
    learned after the requested cutoff remain invisible just like they are in
    the database PIT query.  Exact repeated materializations of one fact id
    are deduplicated; every other duplicate version is a conflict.
    """

    grouped: dict[tuple[str, object], list[object]] = {}
    for fact in facts:
        grouped.setdefault(_fact_version_key(fact), []).append(fact)
    for (logical_key, fact_version), duplicates in grouped.items():
        if len(duplicates) < 2:
            continue
        first = duplicates[0]
        if all(_same_fact_materialization(first, item) for item in duplicates[1:]):
            continue
        raise error_type(
            "duplicate immutable fact version in one logical fact chain",
            details=_identity_error_details(
                instrument_id,
                source=source,
                effective_at=effective_at,
                data_cutoff=data_cutoff,
                expected="one immutable fact per logical_fact_key/fact_version",
                actual=len(duplicates),
                fact_version=fact_version,
                logical_fact_key=logical_key,
                fact_ids=[getattr(item, "fact_id", None) for item in duplicates],
                known_ats=[getattr(item, "known_at", None) for item in duplicates],
                fact_versions=[
                    getattr(item, "fact_version", None) for item in duplicates
                ],
            ),
        )


def _fact_order(fact: object) -> tuple[datetime, int]:
    """Return the PIT revision ordering: knowledge time, then version."""

    return (
        _stored_aware(getattr(fact, "known_at"), "known_at"),
        getattr(fact, "fact_version"),
    )


class InstrumentIdentityRepository:
    """Create and transition generic stable identities."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_identity(
        self,
        *,
        asset_class: str,
        # Kept as a keyword-only compatibility sentinel so old callers get a
        # stable domain error instead of accidentally taking ownership of an
        # identity UUID.  Existing ETF IDs are preserved only by the explicit
        # ``migrate_existing_etf_identities`` migration entry point below.
        instrument_id: UUID | None = None,
        status: InstrumentStatus = InstrumentStatus.ACTIVE,
    ) -> UUID:
        """Append one generic identity row and return its server UUID."""

        if not isinstance(asset_class, str) or not asset_class.strip():
            raise DomainValidationError("asset_class must be non-blank text")
        try:
            resolved_status = InstrumentStatus(getattr(status, "value", status))
        except ValueError as exc:
            raise DomainValidationError("status must be a valid InstrumentStatus") from exc
        if resolved_status is InstrumentStatus.MERGED:
            # A merged identity is terminal but must always point at an
            # explicitly reconciled target.  Creation has no target/evidence
            # context, so it cannot produce a valid merged row.
            raise DomainValidationError(
                "an identity cannot be created directly in the merged status"
            )
        if instrument_id is not None:
            raise DomainValidationError(
                "instrument_id is server-generated; use the explicit identity migration "
                "entry point when preserving a legacy UUID"
            )
        resolved_id = uuid4()
        self.session.add(
            Instrument(
                id=resolved_id,
                asset_class=asset_class.strip(),
                status=resolved_status.value,
            )
        )
        return resolved_id

    def get(self, instrument_id: UUID) -> Instrument | None:
        """Return one identity row without exposing a source snapshot."""

        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        return self.session.get(Instrument, instrument_id)

    def resolve_identity_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentIdentityFact | None:
        """Resolve immutable identity attributes for this stable ID."""

        return InstrumentIdentityFactRepository(self.session).resolve_identity_at(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    def resolve_display_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        """Resolve PIT display labels without reading the current catalogue."""

        return InstrumentDisplayFactRepository(self.session).resolve_display_at(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    def resolve_code_mappings(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[InstrumentCodeMapping, ...]:
        """Resolve evidenced PIT source-code segments for this stable ID."""

        return InstrumentCodeMappingRepository(self.session).resolve_code_mappings(
            instrument_id,
            source=source,
            start_date=start_date,
            end_date=end_date,
            data_cutoff=data_cutoff,
        )

    def transition_status(
        self,
        instrument_id: UUID,
        status: InstrumentStatus,
        *,
        merged_into_id: UUID | None = None,
    ) -> Instrument:
        """Apply a non-merge lifecycle transition to the mutable identity row.

        Merging is deliberately not a generic status change.  Callers must
        use :meth:`InstrumentIdentityService.merge_identities`, which records
        the evidence and audit row before changing the identity.  The one
        exception below is an already-merged row: repeating the same terminal
        transition remains an idempotent validation/read operation.
        """

        return self._transition_status(
            instrument_id,
            status,
            merged_into_id=merged_into_id,
            allow_evidenced_merge=False,
        )

    def _transition_status(
        self,
        instrument_id: UUID,
        status: InstrumentStatus,
        *,
        merged_into_id: UUID | None = None,
        allow_evidenced_merge: bool,
    ) -> Instrument:
        """Apply a lifecycle transition after the service-level merge gate.

        ``allow_evidenced_merge`` is private on purpose.  Only the service
        method that has validated and recorded merge evidence may set it;
        public lifecycle callers cannot bypass the evidence/audit boundary.
        """

        row = self.get(instrument_id)
        if row is None:
            raise KeyError(f"unknown instrument identity: {instrument_id}")
        try:
            target = InstrumentStatus(getattr(status, "value", status))
        except ValueError as exc:
            raise DomainValidationError("status must be a valid InstrumentStatus") from exc
        current = InstrumentStatus(row.status)
        allowed = {
            InstrumentStatus.ACTIVE: {
                InstrumentStatus.ACTIVE,
                InstrumentStatus.DEPRECATED,
                InstrumentStatus.MERGED,
            },
            InstrumentStatus.DEPRECATED: {
                InstrumentStatus.DEPRECATED,
                InstrumentStatus.ACTIVE,
                InstrumentStatus.MERGED,
            },
            InstrumentStatus.MERGED: {InstrumentStatus.MERGED},
        }
        if target not in allowed[current]:
            raise DomainValidationError(
                f"identity status cannot transition from {current.value} to {target.value}"
            )
        if target is InstrumentStatus.MERGED:
            if current is InstrumentStatus.MERGED:
                if (
                    row.merged_into_id is None
                    or merged_into_id != row.merged_into_id
                ):
                    raise DomainValidationError(
                        "merged identity cannot be redirected"
                    )
                # Repeating the same terminal transition is idempotent; do
                # not clear or rewrite the existing merge target.
                return row
            if not allow_evidenced_merge:
                raise DomainValidationError(
                    "identity merges require evidence; use merge_identities"
                )
            if merged_into_id is None or merged_into_id == instrument_id:
                raise DomainValidationError(
                    "a merged identity must reference a different target identity"
                )
            if self.get(merged_into_id) is None:
                raise KeyError(f"unknown merge target identity: {merged_into_id}")
        elif merged_into_id is not None:
            raise DomainValidationError(
                "merged_into_id is only valid for the merged status"
            )
        row.status = target.value
        row.merged_into_id = merged_into_id
        return row


class InstrumentIdentityFactRepository:
    """Append and resolve immutable identity facts under PIT semantics."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append_fact(self, fact: InstrumentIdentityFact) -> UUID:
        """Append one fact in ingestion order.

        Ordinary ingestion is intentionally stricter than the database's
        append-only constraint: knowledge time must advance strictly within a
        logical chain.  Historical replay callers must use the explicitly
        named reconstruction method below.
        """

        return self._append_fact(fact, allow_historical=False)

    def append_historical_fact(self, fact: InstrumentIdentityFact) -> UUID:
        """Append a deliberately reconstructed historical fact chain.

        This is the only public identity-fact path that permits a new fact's
        ``known_at`` to precede the current chain head.  Version continuity,
        predecessor identity, and append-only storage remain mandatory.
        """

        return self._append_fact(fact, allow_historical=True)

    # A second descriptive name is useful to backfill/import adapters while
    # keeping the ordinary ``append_fact`` API safe by default.
    append_reconstructed_fact = append_historical_fact

    def _append_fact(
        self, fact: InstrumentIdentityFact, *, allow_historical: bool
    ) -> UUID:
        """Validate and persist one identity fact without updating a row."""

        if not isinstance(fact, InstrumentIdentityFact):
            raise DomainValidationError("fact must be an InstrumentIdentityFact")
        existing_chain = _fact_chain(
            self.session, InstrumentIdentityFactRecord, fact.logical_fact_key
        )
        if any(
            getattr(row, "fact_version") == fact.fact_version
            for row in existing_chain
        ):
            raise IdentityFactVersionExistsError(
                f"identity fact {fact.logical_fact_key}@{fact.fact_version} already exists"
            )
        chain = _validate_fact_append(
            self.session,
            fact,
            InstrumentIdentityFactRecord,
            allow_historical=allow_historical,
        )
        existing = next(
            (
                row
                for row in chain
                if getattr(row, "fact_version") == fact.fact_version
            ),
            None,
        )
        if existing is not None:
            raise IdentityFactVersionExistsError(
                f"identity fact {fact.logical_fact_key}@{fact.fact_version} already exists"
            )
        self.session.add(
            InstrumentIdentityFactRecord(
                id=fact.fact_id,
                instrument_id=fact.instrument_id,
                fact_version=fact.fact_version,
                logical_fact_key=fact.logical_fact_key,
                supersedes_fact_id=fact.supersedes_fact_id,
                asset_class=fact.asset_class,
                exchange=getattr(fact, "exchange", None),
                currency=fact.currency,
                calendar_id=fact.calendar_id,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                effective_range=_date_range_value(
                    fact.valid_from, fact.valid_to, session=self.session
                ),
                knowledge_range=_knowledge_range_value(
                    fact.known_at, session=self.session
                ),
                known_at=fact.known_at,
                observed_at=fact.observed_at,
                evidence=fact.evidence,
            )
        )
        return fact.fact_id  # type: ignore[return-value]

    def list_facts(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> tuple[InstrumentIdentityFact, ...]:
        """Return all visible effective facts for audit/conflict checking."""

        instrument_id = _require_instrument_id(instrument_id)
        effective, cutoff = _query_datetimes(effective_at, data_cutoff)
        rows = _visible_fact_rows(
            self.session,
            InstrumentIdentityFactRecord,
            instrument_id,
            cutoff,
        )
        # Keep this public listing effective-date bounded while ensuring the
        # revision fold happened against every cutoff-visible row.
        return tuple(
            fact
            for fact in (
                _identity_to_domain(
                    row,
                    effective_at=effective,
                    data_cutoff=cutoff,
                )
                for row in rows
            )
            if fact.covers(effective.date())
        )

    def resolve(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentIdentityFact | None:
        """Resolve one unique identity fact or block on authoritative conflict."""

        instrument_id = _require_instrument_id(instrument_id)
        effective, cutoff = _query_datetimes(effective_at, data_cutoff)
        try:
            listed = tuple(
                _identity_to_domain(
                    row,
                    effective_at=effective,
                    data_cutoff=cutoff,
                )
                for row in _visible_fact_rows(
                    self.session,
                    InstrumentIdentityFactRecord,
                    instrument_id,
                    cutoff,
                )
            )
        except DomainValidationError as exc:
            if "evidence" in str(exc).lower():
                raise IdentityMappingEvidenceMissingError(
                    "the identity fact covering the requested instant carries no evidence",
                    details=_identity_error_details(
                        instrument_id,
                        source="identity_fact",
                        effective_at=effective,
                        data_cutoff=cutoff,
                        expected="non-blank evidence",
                        actual="blank or missing evidence",
                        fact_version=None,
                        reason=str(exc),
                    ),
                ) from exc
            raise
        facts = _visible_latest(
            listed,
            effective_at=effective,
            data_cutoff=cutoff,
        )
        if not facts:
            return None
        # Independent logical keys represent competing identity assertions;
        # choosing one by ingestion time would make a replay non-deterministic.
        if len(facts) > 1:
            raise IdentityFactConflictError(
                f"multiple identity facts cover {instrument_id} at {effective_at.date()}",
                details=_identity_error_details(
                    instrument_id,
                    source="identity_fact",
                    effective_at=effective,
                    data_cutoff=cutoff,
                    expected="one identity fact",
                    actual=len(facts),
                    fact_version=None,
                    fact_versions=[getattr(fact, "fact_version") for fact in facts],
                    ),
            )
        return facts[0]  # type: ignore[return-value]

    def resolve_identity_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentIdentityFact | None:
        """Resolve the identity fact at separate effective/knowledge times.

        The explicit name is the stable PIT query boundary exposed to
        adapters.  Keep ``resolve`` as the implementation so existing
        callers and the new contract share one validation path.
        """

        return self.resolve(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )


class InstrumentDisplayFactRepository:
    """Append and resolve point-in-time display facts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append_fact(self, fact: InstrumentDisplayFact) -> UUID:
        """Append one display fact in strictly increasing knowledge order."""

        return self._append_fact(fact, allow_historical=False)

    def append_historical_fact(self, fact: InstrumentDisplayFact) -> UUID:
        """Append a deliberately reconstructed historical display fact."""

        return self._append_fact(fact, allow_historical=True)

    append_reconstructed_fact = append_historical_fact

    def _append_fact(
        self, fact: InstrumentDisplayFact, *, allow_historical: bool
    ) -> UUID:
        """Validate and persist one display fact without updating a row."""

        if not isinstance(fact, InstrumentDisplayFact):
            raise DomainValidationError("fact must be an InstrumentDisplayFact")
        existing_chain = _fact_chain(
            self.session, InstrumentDisplayFactRecord, fact.logical_fact_key
        )
        if any(
            getattr(row, "fact_version") == fact.fact_version
            for row in existing_chain
        ):
            raise DisplayFactVersionExistsError(
                f"display fact {fact.logical_fact_key}@{fact.fact_version} already exists"
            )
        chain = _validate_fact_append(
            self.session,
            fact,
            InstrumentDisplayFactRecord,
            allow_historical=allow_historical,
        )
        existing = next(
            (
                row
                for row in chain
                if getattr(row, "fact_version") == fact.fact_version
            ),
            None,
        )
        if existing is not None:
            raise DisplayFactVersionExistsError(
                f"display fact {fact.logical_fact_key}@{fact.fact_version} already exists"
            )
        self.session.add(
            InstrumentDisplayFactRecord(
                id=fact.fact_id,
                instrument_id=fact.instrument_id,
                fact_version=fact.fact_version,
                logical_fact_key=fact.logical_fact_key,
                supersedes_fact_id=fact.supersedes_fact_id,
                trading_code=fact.trading_code,
                name=fact.name,
                display_name=fact.display_name,
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                source=fact.source,
                source_revision=fact.source_revision,
                known_at=fact.known_at,
                observed_at=fact.observed_at,
                evidence=fact.evidence,
                authority_rank=fact.authority_rank,
                authority_status=fact.authority_status.value,
                effective_range=_date_range_value(
                    fact.valid_from, fact.valid_to, session=self.session
                ),
                knowledge_range=_knowledge_range_value(
                    fact.known_at, session=self.session
                ),
            )
        )
        return fact.fact_id  # type: ignore[return-value]

    def list_facts(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> tuple[InstrumentDisplayFact, ...]:
        """Return visible display facts, including unresolved review rows."""

        instrument_id = _require_instrument_id(instrument_id)
        effective, cutoff = _query_datetimes(effective_at, data_cutoff)
        rows = _visible_fact_rows(
            self.session,
            InstrumentDisplayFactRecord,
            instrument_id,
            cutoff,
        )
        # Pending/rejected rows are retained in the audit listing; only the
        # resolver applies the authoritative-only policy.
        return tuple(
            fact
            for fact in (
                _display_to_domain(
                    row,
                    effective_at=effective,
                    data_cutoff=cutoff,
                )
                for row in rows
            )
            if fact.covers(effective.date())
        )

    def resolve_display_fact(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplayFact | None:
        """Resolve the selected authoritative display fact.

        Returning the fact (rather than only its display projection) lets the
        identity-resolution snapshot retain source, revision, evidence, and
        PIT interval provenance.
        """

        instrument_id = _require_instrument_id(instrument_id)
        # Validate both query coordinates before entering the fact-read error
        # translation below.  Otherwise a malformed timestamp can leave the
        # normalized locals undefined and mask the domain error with an
        # ``UnboundLocalError`` while constructing diagnostic details.
        effective, cutoff = _query_datetimes(effective_at, data_cutoff)
        try:
            listed = tuple(
                _display_to_domain(
                    row,
                    effective_at=effective,
                    data_cutoff=cutoff,
                )
                for row in _visible_fact_rows(
                    self.session,
                    InstrumentDisplayFactRecord,
                    instrument_id,
                    cutoff,
                )
            )
        except DomainValidationError as exc:
            if "evidence" in str(exc).lower():
                raise IdentityMappingEvidenceMissingError(
                    "the display fact covering the requested instant carries no evidence",
                    details=_identity_error_details(
                        instrument_id,
                        source="display_fact",
                        effective_at=effective,
                        data_cutoff=cutoff,
                        expected="non-blank evidence",
                        actual="blank or missing evidence",
                        fact_version=None,
                        reason=str(exc),
                    ),
                ) from exc
            raise IdentityMappingConflictError(
                "the stored display fact cannot be uniquely resolved",
                details=_identity_error_details(
                    instrument_id,
                    source="display_fact",
                    effective_at=effective,
                    data_cutoff=cutoff,
                    expected="valid stored display fact",
                    actual=str(exc),
                    fact_version=None,
                    reason=str(exc),
                ),
            ) from exc
        facts = _visible_latest(
            listed,
            effective_at=effective,
            data_cutoff=cutoff,
            authoritative_only=True,
        )
        if not facts:
            return None
        # Highest authority rank is a deliberate policy choice only when it
        # yields one fact.  A tie is a conflict, not a reason to use
        # observed_at or whichever SQL row happened to arrive first.
        highest = max(getattr(fact, "authority_rank") for fact in facts)
        leaders = [fact for fact in facts if getattr(fact, "authority_rank") == highest]
        if len(leaders) != 1:
            raise DisplayFactConflictError(
                f"multiple authoritative display facts cover {instrument_id} at "
                f"{effective_at.date()}",
                details=_identity_error_details(
                    instrument_id,
                    source="display_fact",
                    effective_at=effective,
                    data_cutoff=cutoff,
                    expected="one authoritative display fact",
                    actual=len(leaders),
                    fact_version=None,
                    fact_versions=[
                        getattr(fact, "fact_version") for fact in leaders
                    ],
                    sources=[getattr(fact, "source", None) for fact in leaders],
                ),
            )
        return leaders[0]  # type: ignore[return-value]

    def resolve_display(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        """Resolve authoritative labels without consulting ``etf_codes``."""

        fact = self.resolve_display_fact(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )
        return fact.as_display() if fact is not None else None

    def resolve_display_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        """Resolve display labels at one market instant and cutoff."""

        return self.resolve_display(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )


class InstrumentIdentityService:
    """Coordinate identity imports, explicit merges, and PIT resolution."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.identities = InstrumentIdentityRepository(session)
        self.identity_facts = InstrumentIdentityFactRepository(session)
        self.display_facts = InstrumentDisplayFactRepository(session)

    def get_or_create(
        self,
        *,
        source: str,
        source_code: str,
        effective_session: date,
        asset_class: str,
        currency: str,
        calendar_id: str,
        exchange: str | None = None,
        mapping_source: str,
        evidence: str,
        known_at: datetime,
        observed_at: datetime,
        trading_code: str,
        valid_to: date | None = None,
        source_revision: str | None = None,
    ) -> UUID:
        """Get-or-create an identity under a normalized import lock.

        PostgreSQL callers execute this inside their transaction.  The
        ``FOR UPDATE`` lookup serializes an existing key; the unique fact
        version/lookup constraints serialize concurrent first imports.  An
        integrity race is retried as a read and never creates a replacement
        UUID.
        """

        normalized_source = _normalized_identifier(source, "source")
        normalized_source_code = _normalized_identifier(source_code, "source_code")
        normalized_trading_code = _normalized_identifier(trading_code, "trading_code")
        if not isinstance(effective_session, date) or isinstance(
            effective_session, datetime
        ):
            raise DomainValidationError("effective_session must be a calendar date")
        key = normalize_identity_lookup_key(
            normalized_source, normalized_source_code, effective_session
        )
        self._acquire_import_lock(key)
        mapping_repo = InstrumentCodeMappingRepository(self.session)
        # ``with_for_update`` is important for PostgreSQL and harmless for
        # SQLite/fake sessions used by unit tests.
        statement = select(InstrumentCodeMappingRecord).where(
            func.lower(func.trim(InstrumentCodeMappingRecord.source))
            == _lookup_identifier(normalized_source),
            func.lower(func.trim(InstrumentCodeMappingRecord.source_code))
            == _lookup_identifier(normalized_source_code),
            InstrumentCodeMappingRecord.valid_from <= effective_session,
            (
                InstrumentCodeMappingRecord.valid_to.is_(None)
                | (InstrumentCodeMappingRecord.valid_to > effective_session)
            ),
        ).with_for_update()
        existing_rows = self.session.execute(statement).scalars().all()
        if len(existing_rows) > 1:
            raise IdentityMappingConflictError(
                "source code has multiple identities at the import session",
                details={
                    "instrument_id": None,
                    "source": normalized_source,
                    "source_code": normalized_source_code,
                    "session_date": effective_session.isoformat(),
                    "session": effective_session.isoformat(),
                    "expected": "one identity",
                    "actual": len(existing_rows),
                    "data_cutoff": None,
                    "fact_version": None,
                },
            )
        if existing_rows:
            return existing_rows[0].instrument_id

        resolved_id: UUID | None = None
        try:
            nested = getattr(self.session, "begin_nested", None)
            if not callable(nested):
                raise DomainValidationError(
                    "get_or_create requires a SQLAlchemy session with savepoints"
                )
            with nested():
                # Keep identity creation inside the savepoint.  SQLAlchemy
                # flushes pending objects when entering ``begin_nested``;
                # creating the identity before that point would leave an
                # orphan row behind if the mapping/fact insert lost a unique
                # race and the savepoint had to roll back.
                resolved_id = self.identities.create_identity(asset_class=asset_class)
                mapping = InstrumentCodeMapping(
                    instrument_id=resolved_id,
                    source=normalized_source,
                    source_code=normalized_source_code,
                    trading_code=normalized_trading_code,
                    valid_from=effective_session,
                    valid_to=valid_to,
                    mapping_source=mapping_source,
                    evidence=evidence,
                    known_at=known_at,
                    observed_at=observed_at,
                    source_revision=source_revision,
                    logical_fact_key=key,
                )
                mapping_repo.add_mapping(mapping)
                # Identity facts are separate from mappings and must carry the
                # explicit asset/currency/calendar facts supplied by the caller.
                self.identity_facts.append_fact(
                    InstrumentIdentityFact(
                        instrument_id=resolved_id,
                        fact_version=1,
                        asset_class=asset_class,
                        exchange=exchange,
                        currency=currency,
                        calendar_id=calendar_id,
                        valid_from=effective_session,
                        valid_to=valid_to,
                        known_at=known_at,
                        observed_at=observed_at,
                        evidence=evidence,
                        logical_fact_key=f"identity:{resolved_id}",
                    )
                )
                # Force uniqueness/FK checks inside the savepoint.  Otherwise
                # an IntegrityError could occur at the caller's later commit,
                # after the retry opportunity has already passed.
                self.session.flush()
        except IntegrityError:
            # ``begin_nested`` rolled back only the failed import, leaving the
            # caller's outer transaction usable.  Another transaction may
            # have won the normalized import key; never create a replacement.
            winner = self.session.execute(statement).scalars().first()
            if winner is not None:
                return winner.instrument_id
            raise
        if resolved_id is None:  # pragma: no cover - defensive type narrowing
            raise DomainValidationError("identity import did not produce an identity")
        return resolved_id

    # Explicit names used by ingestion adapters; aliases keep one code path.
    get_or_create_identity = get_or_create

    def _acquire_import_lock(self, key: str) -> None:
        """Serialize first imports by normalized source/code/session key.

        PostgreSQL advisory locks are transaction-scoped and work even when
        no mapping row exists yet (the case where ``FOR UPDATE`` cannot lock
        anything).  SQLite and lightweight test sessions simply skip this
        database-specific primitive; their unique constraints still guard
        persisted duplicates.
        """

        bind = getattr(self.session, "bind", None)
        if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
            return
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lookup_key, 0))"),
            {"lookup_key": key},
        )

    def merge_identities(
        self,
        source_instrument_id: UUID,
        target_instrument_id: UUID,
        *,
        evidence: str | None,
        mapping_source: str = "identity_reconciliation",
        actor: str | None = None,
    ) -> bool:
        """Merge only with concrete evidence and retain every attempt audit.

        Rejection audits are written to the caller's current transaction and
        are flushed before the domain error is raised.  The repository never
        commits or rolls back that transaction on the caller's behalf; callers
        that want a rejected attempt retained must commit after handling the
        exception, while a deliberate rollback also rolls back its audit.
        """

        from app.instruments.models import InstrumentIdentityMergeAuditRecord

        valid_mapping_source = _optional_label(mapping_source, "mapping_source")
        if valid_mapping_source is None:
            raise DomainValidationError("mapping_source must be non-blank text")
        valid_actor = _optional_label(actor, "actor")
        valid_evidence = _optional_label(evidence, "evidence")
        old = self.identities.get(source_instrument_id)
        target = self.identities.get(target_instrument_id)
        if old is None or target is None:
            raise KeyError("both source and target identities must exist")

        def record_rejection(reason: str) -> None:
            """Persist a rejected attempt before raising its domain error."""

            self.session.add(
                InstrumentIdentityMergeAuditRecord(
                    id=uuid4(),
                    source_instrument_id=source_instrument_id,
                    target_instrument_id=target_instrument_id,
                    mapping_source=valid_mapping_source,
                    evidence=None,
                    actor=valid_actor,
                    outcome="rejected",
                    reason=reason,
                )
            )
            # Make the audit visible to the caller before the exception is
            # raised.  Transaction ownership stays with the caller so this
            # method cannot unexpectedly commit unrelated work.
            self.session.flush()

        if source_instrument_id == target_instrument_id:
            record_rejection("self_merge")
            raise DomainValidationError("an identity cannot merge into itself")
        if not valid_evidence:
            record_rejection("evidence_missing")
            raise IdentityMergeEvidenceMissingError(
                "identity merge requires concrete evidence",
                details={
                    "instrument_id": _identity_error_detail_value(
                        source_instrument_id
                    ),
                    "source": valid_mapping_source,
                    "source_code": None,
                    "session_date": None,
                    "expected": "non-blank evidence",
                    "actual": _identity_error_detail_value(evidence),
                    "data_cutoff": None,
                    "fact_version": None,
                    "target_instrument_id": _identity_error_detail_value(
                        target_instrument_id
                    ),
                },
            )
        if InstrumentStatus(old.status) is InstrumentStatus.MERGED:
            if old.merged_into_id == target_instrument_id:
                return False
            record_rejection("source_already_merged")
            raise DomainValidationError("merged identity cannot be redirected")
        if InstrumentStatus(target.status) is InstrumentStatus.MERGED:
            record_rejection("target_already_merged")
            raise DomainValidationError("a merge target must not already be merged")

        # A merge points at one canonical identity and is never allowed to
        # introduce a cycle through an existing redirect chain.  Follow the
        # chain with a visited set so even corrupt legacy data terminates.
        cursor = target
        visited: set[UUID] = set()
        while cursor.merged_into_id is not None:
            if cursor.id in visited:
                record_rejection("merge_chain_cycle")
                raise DomainValidationError("existing identity merge chain contains a cycle")
            visited.add(cursor.id)
            if cursor.merged_into_id == source_instrument_id:
                record_rejection("merge_cycle")
                raise DomainValidationError("identity merge would create a cycle")
            next_identity = self.identities.get(cursor.merged_into_id)
            if next_identity is None:
                record_rejection("merge_target_missing")
                raise DomainValidationError("existing merge target identity is missing")
            cursor = next_identity
        self.session.add(
            InstrumentIdentityMergeAuditRecord(
                id=uuid4(),
                source_instrument_id=source_instrument_id,
                target_instrument_id=target_instrument_id,
                mapping_source=valid_mapping_source,
                evidence=valid_evidence,
                actor=valid_actor,
                outcome="accepted",
                reason=None,
            )
        )
        self.identities._transition_status(
            source_instrument_id,
            InstrumentStatus.MERGED,
            merged_into_id=target_instrument_id,
            allow_evidenced_merge=True,
        )
        return True

    merge_identity = merge_identities
    merge = merge_identities

    def resolve(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
        source: str | None = None,
    ) -> InstrumentIdentityResolution | None:
        """Resolve identity/display facts and optional source mapping."""

        identity_fact = self.identity_facts.resolve(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )
        display_fact = self.display_facts.resolve_display_fact(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )
        display = display_fact.as_display() if display_fact is not None else None
        mapping = None
        if source is not None:
            mappings = InstrumentCodeMappingRepository(self.session).resolve_code_mappings(
                instrument_id,
                source=source,
                start_date=effective_at.date(),
                end_date=effective_at.date(),
                data_cutoff=data_cutoff,
            )
            if mappings:
                mapping = mappings[0]
        if identity_fact is None and display is None and mapping is None:
            return None
        summary = _identity_resolution_evidence_summary(
            instrument_id=instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
            identity_fact=identity_fact,
            display_fact=display_fact,
            mapping=mapping,
            source=source,
        )
        return InstrumentIdentityResolution(
            instrument_id=instrument_id,
            identity_fact=identity_fact,
            display=display,
            mapping=mapping,
            evidence_summary=MappingProxyType(summary),
        )

    def resolve_display(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        """Implement ``InstrumentDisplayProvider`` for result snapshots."""

        return self.display_facts.resolve_display(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    # Explicit PIT names are part of the public service boundary.  They keep
    # effective-at and data-cutoff semantics visible at call sites while
    # retaining the existing compact aliases above for compatibility.
    def resolve_identity_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentIdentityFact | None:
        return self.identity_facts.resolve_identity_at(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    def resolve_display_at(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentDisplay | None:
        return self.display_facts.resolve_display_at(
            instrument_id,
            effective_at=effective_at,
            data_cutoff=data_cutoff,
        )

    def resolve_code_mappings(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[InstrumentCodeMapping, ...]:
        return InstrumentCodeMappingRepository(self.session).resolve_code_mappings(
            instrument_id,
            source=source,
            start_date=start_date,
            end_date=end_date,
            data_cutoff=data_cutoff,
        )


class MappingResolutionRepository:
    """Rebuildable mapping-head helper.

    Heads are an index, not a second fact source.  Rebuilding them from the
    append-only mapping rows is always safe; this helper intentionally does
    not expose update/delete methods for mapping facts themselves.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def rebuild(self, *, instrument_id: UUID | None = None) -> int:
        """Recreate deterministic head rows from each logical fact chain.

        Fact rows keep an open-ended ``[known_at, infinity)`` knowledge range.
        Heads are the disposable resolution index, so each ordered revision
        receives the finite interval ``[known_at, next_known_at)`` and the
        final revision remains open-ended.  Recomputing these ranges on every
        rebuild avoids mutating immutable fact rows when a later revision is
        appended.
        """

        # Heads are disposable indexes.  Replace only the requested identity's
        # index so a scoped rebuild cannot leave stale pointers or touch other
        # instruments.  Fact tables are never part of this delete operation.
        delete_statement = delete(MappingResolutionHead)
        if instrument_id is not None:
            delete_statement = delete_statement.where(
                MappingResolutionHead.instrument_id == instrument_id
            )
        self.session.execute(delete_statement)
        query = select(InstrumentCodeMappingRecord)
        if instrument_id is not None:
            query = query.where(InstrumentCodeMappingRecord.instrument_id == instrument_id)
        rows = self.session.execute(query).scalars().all()
        chains: dict[str, list[InstrumentCodeMappingRecord]] = {}
        for row in rows:
            logical_key = getattr(row, "logical_fact_key", None) or f"legacy:{row.id}"
            chains.setdefault(logical_key, []).append(row)
        count = 0
        for logical_key, chain in chains.items():
            # A head has one pointer per knowledge instant.  Historical
            # reconstruction may contain multiple versions at that instant;
            # retain the highest version as the visible row for that instant.
            by_knowledge: dict[datetime, InstrumentCodeMappingRecord] = {}
            for row in chain:
                knowledge_from = _stored_aware(row.known_at, "knowledge_from")
                current = by_knowledge.get(knowledge_from)
                if current is None or (
                    getattr(row, "fact_version", 1),
                    str(row.id),
                ) > (
                    getattr(current, "fact_version", 1),
                    str(current.id),
                ):
                    by_knowledge[knowledge_from] = row
            ordered = sorted(by_knowledge.items())
            for index, (knowledge_from, row) in enumerate(ordered):
                next_known_at = (
                    ordered[index + 1][0] if index + 1 < len(ordered) else None
                )
                effective_range = _date_range_value(
                    row.valid_from, row.valid_to, session=self.session
                )
                knowledge_range = _knowledge_range_value(
                    knowledge_from, next_known_at, session=self.session
                )
                self.session.add(
                    MappingResolutionHead(
                        id=uuid4(),
                        logical_fact_key=logical_key,
                        knowledge_from=knowledge_from,
                        fact_id=row.id,
                        instrument_id=row.instrument_id,
                        source=row.source,
                        source_code=row.source_code,
                        effective_range=effective_range,
                        knowledge_range=knowledge_range,
                    )
                )
                count += 1
        return count


class DisplayResolutionRepository:
    """Rebuildable index for authoritative display-fact revisions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def rebuild(self, *, instrument_id: UUID | None = None) -> int:
        """Recreate display heads with finite knowledge intervals per chain."""

        delete_statement = delete(DisplayResolutionHead)
        if instrument_id is not None:
            delete_statement = delete_statement.where(
                DisplayResolutionHead.instrument_id == instrument_id
            )
        self.session.execute(delete_statement)
        query = select(InstrumentDisplayFactRecord)
        if instrument_id is not None:
            query = query.where(InstrumentDisplayFactRecord.instrument_id == instrument_id)
        rows = tuple(
            row
            for row in self.session.execute(query).scalars().all()
            if getattr(row, "authority_status", AuthorityStatus.AUTHORITATIVE.value)
            == AuthorityStatus.AUTHORITATIVE.value
        )
        chains: dict[str, list[InstrumentDisplayFactRecord]] = {}
        for row in rows:
            logical_key = getattr(row, "logical_fact_key", None) or f"legacy:{row.id}"
            chains.setdefault(logical_key, []).append(row)
        count = 0
        for logical_key, chain in chains.items():
            by_knowledge: dict[datetime, InstrumentDisplayFactRecord] = {}
            for row in chain:
                knowledge_from = _stored_aware(row.known_at, "knowledge_from")
                current = by_knowledge.get(knowledge_from)
                if current is None or (
                    getattr(row, "fact_version", 1),
                    str(row.id),
                ) > (
                    getattr(current, "fact_version", 1),
                    str(current.id),
                ):
                    by_knowledge[knowledge_from] = row
            ordered = sorted(by_knowledge.items())
            for index, (knowledge_from, row) in enumerate(ordered):
                next_known_at = (
                    ordered[index + 1][0] if index + 1 < len(ordered) else None
                )
                effective_range = _date_range_value(
                    row.valid_from, row.valid_to, session=self.session
                )
                knowledge_range = _knowledge_range_value(
                    knowledge_from, next_known_at, session=self.session
                )
                self.session.add(
                    DisplayResolutionHead(
                        id=uuid4(),
                        logical_fact_key=logical_key,
                        knowledge_from=knowledge_from,
                        fact_id=row.id,
                        instrument_id=row.instrument_id,
                        authority_rank=row.authority_rank,
                        effective_range=effective_range,
                        knowledge_range=knowledge_range,
                    )
                )
                count += 1
        return count


# Short aliases are intentionally exported after the concrete classes below;
# they make the persistence boundary discoverable without introducing another
# implementation or a second source of truth.
IdentityRepository = InstrumentIdentityRepository
IdentityFactRepository = InstrumentIdentityFactRepository
DisplayFactRepository = InstrumentDisplayFactRepository


def _stored_evidence_error_details(
    row: object,
    *,
    source: object = None,
    source_code: object = None,
    effective_at: datetime | None = None,
    data_cutoff: datetime | None = None,
) -> dict[str, object]:
    """Build stable diagnostics when a stored fact lacks evidence.

    Identity facts intentionally do not carry source provenance.  Display
    facts may pass their source explicitly because they retain that metadata;
    keeping this value as an explicit argument prevents the helper from
    reaching across fact boundaries.
    """

    instrument_id = getattr(row, "instrument_id", None)
    session_date = (
        effective_at.date()
        if isinstance(effective_at, datetime)
        else getattr(row, "valid_from", None)
    )
    return {
        "instrument_id": _identity_error_detail_value(instrument_id),
        "source": _identity_error_detail_value(source),
        "source_code": _identity_error_detail_value(source_code),
        "session_date": _identity_error_detail_value(session_date),
        "expected": "non-blank evidence",
        "actual": _identity_error_detail_value(getattr(row, "evidence", None)),
        "data_cutoff": _identity_error_detail_value(data_cutoff),
        "fact_version": _identity_error_detail_value(
            getattr(row, "fact_version", None)
        ),
    }


def _identity_to_domain(
    row: InstrumentIdentityFactRecord,
    *,
    effective_at: datetime | None = None,
    data_cutoff: datetime | None = None,
) -> InstrumentIdentityFact:
    """Project and revalidate one stored identity fact."""

    evidence = getattr(row, "evidence", None)
    if not isinstance(evidence, str) or not evidence.strip():
        raise IdentityMappingEvidenceMissingError(
            "stored identity fact carries no evidence",
            details=_stored_evidence_error_details(
                row,
                effective_at=effective_at,
                data_cutoff=data_cutoff,
            ),
        )
    try:
        return InstrumentIdentityFact(
            fact_id=row.id,
            instrument_id=row.instrument_id,
            fact_version=row.fact_version,
            logical_fact_key=row.logical_fact_key,
            supersedes_fact_id=row.supersedes_fact_id,
            asset_class=row.asset_class,
            exchange=getattr(row, "exchange", None),
            currency=row.currency,
            calendar_id=row.calendar_id,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            known_at=_stored_aware(row.known_at, "known_at"),
            observed_at=_stored_aware(row.observed_at, "observed_at"),
            evidence=row.evidence,
        )
    except DomainValidationError as exc:
        if "evidence" in str(exc).lower():
            raise IdentityMappingEvidenceMissingError(
                "stored identity fact carries no evidence",
                details=_stored_evidence_error_details(
                    row,
                    effective_at=effective_at,
                    data_cutoff=data_cutoff,
                ),
            ) from exc
        raise DomainValidationError(
            f"stored identity fact {getattr(row, 'id', None)} violates the domain contract: {exc}"
        ) from exc


def _display_to_domain(
    row: InstrumentDisplayFactRecord,
    *,
    effective_at: datetime | None = None,
    data_cutoff: datetime | None = None,
) -> InstrumentDisplayFact:
    """Project and revalidate one stored display fact."""

    evidence = getattr(row, "evidence", None)
    if not isinstance(evidence, str) or not evidence.strip():
        raise IdentityMappingEvidenceMissingError(
            "stored display fact carries no evidence",
            details=_stored_evidence_error_details(
                row,
                source=getattr(row, "source", None),
                effective_at=effective_at,
                data_cutoff=data_cutoff,
            ),
        )
    try:
        return InstrumentDisplayFact(
            fact_id=row.id,
            instrument_id=row.instrument_id,
            fact_version=row.fact_version,
            logical_fact_key=row.logical_fact_key,
            supersedes_fact_id=row.supersedes_fact_id,
            trading_code=row.trading_code,
            name=row.name,
            display_name=row.display_name,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            source=row.source,
            source_revision=row.source_revision,
            known_at=_stored_aware(row.known_at, "known_at"),
            observed_at=_stored_aware(row.observed_at, "observed_at"),
            evidence=row.evidence,
            authority_rank=row.authority_rank,
            authority_status=AuthorityStatus(row.authority_status),
        )
    except (DomainValidationError, ValueError) as exc:
        if "evidence" in str(exc).lower():
            raise IdentityMappingEvidenceMissingError(
                "stored display fact carries no evidence",
                details=_stored_evidence_error_details(
                    row,
                    source=getattr(row, "source", None),
                    effective_at=effective_at,
                    data_cutoff=data_cutoff,
                ),
            ) from exc
        raise DomainValidationError(
            f"stored display fact {getattr(row, 'id', None)} violates the domain contract: {exc}"
        ) from exc
