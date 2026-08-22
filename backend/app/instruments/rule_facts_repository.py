"""PostgreSQL persistence for versioned instrument rule facts.

The repository is append-only: ``append_fact`` creates a new
``fact_key + fact_version`` row and no update path exists.  Queries
enforce point-in-time visibility strictly (``known_at <= data_cutoff``),
use half-open ``[valid_from, valid_to)`` windows, and never fall back to
a "latest" version or fill historical gaps with current facts.  Coverage
gaps, conflicts, and incomplete quality surface in the returned
candidates so upper layers can convert them into stable preflight
issues.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import VersionedReference
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.instruments.rules.contracts import (
    FactQualityStatus,
    RuleFactCandidate,
    canonical_payload,
    rule_fact_content_hash,
)


class RuleFactVersionExistsError(DomainValidationError):
    """Raised when a fact is appended under an existing fact version.

    Facts are immutable and append-only: correcting history means writing
    a new ``fact_version``, never reusing or overwriting one.
    """


class RuleFactsRepository:
    """Append and point-in-time query instrument rule facts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append_fact(self, fact: RuleFactCandidate) -> VersionedReference:
        """Append one immutable fact row and return its versioned reference.

        The candidate is validated domain-side before it reaches SQL.  A
        duplicate ``fact_key + fact_version`` raises
        :class:`RuleFactVersionExistsError` instead of being overwritten;
        the caller is expected to publish the correction as a new version.
        """

        if not isinstance(fact, RuleFactCandidate):
            raise DomainValidationError("fact must be a RuleFactCandidate")
        # The stored hash must be exactly the canonical content hash of
        # the row being appended: reads recompute it for drift detection,
        # so an inconsistent candidate would make its own rows unreadable.
        expected_hash = rule_fact_content_hash(
            fact_reference=fact.fact_reference,
            instrument_id=fact.instrument_id,
            package_reference=fact.package_reference,
            exception_fact_ref=fact.exception_fact_ref,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            fields=fact.fields,
            source=fact.source,
            source_revision=fact.source_revision,
            known_at=fact.known_at,
            observed_at=fact.observed_at,
            quality_status=fact.quality_status,
            fixture_only=fact.fixture_only,
        )
        if fact.content_hash != expected_hash:
            raise DomainValidationError(
                "candidate content_hash does not match the fact content; "
                "compute it with rule_fact_content_hash()"
            )
        existing = (
            self.session.execute(
                select(InstrumentRuleFactRecord.id).where(
                    InstrumentRuleFactRecord.fact_key == fact.fact_reference.key,
                    InstrumentRuleFactRecord.fact_version
                    == fact.fact_reference.version,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise RuleFactVersionExistsError(
                "instrument rule fact already exists for "
                f"{fact.fact_reference.key}@{fact.fact_reference.version}; "
                "append a new fact_version instead of overwriting history"
            )
        exception_ref = fact.exception_fact_ref
        self.session.add(
            InstrumentRuleFactRecord(
                id=uuid4(),
                fact_key=fact.fact_reference.key,
                fact_version=fact.fact_reference.version,
                instrument_id=fact.instrument_id,
                rule_package_key=fact.package_reference.key,
                rule_package_version=fact.package_reference.version,
                rule_exception_key=(
                    exception_ref.key if exception_ref is not None else None
                ),
                rule_exception_version=(
                    exception_ref.version if exception_ref is not None else None
                ),
                valid_from=fact.valid_from,
                valid_to=fact.valid_to,
                # The candidate deep-freezes nested structures into
                # mappingproxy/tuple, which the JSONB serializer cannot
                # encode; canonical_payload converts every level back to
                # JSON-native types (and canonical decimal strings).
                fields=canonical_payload(fact.fields),
                source=fact.source,
                source_revision=fact.source_revision,
                known_at=fact.known_at,
                observed_at=fact.observed_at,
                quality_status=fact.quality_status.value,
                fixture_only=fact.fixture_only,
                content_hash=expected_hash,
            )
        )
        return fact.fact_reference

    def get_fact(
        self,
        fact_reference: VersionedReference,
        *,
        data_cutoff: datetime,
    ) -> RuleFactCandidate | None:
        """Return the exact fact row, or ``None`` when invisible/missing.

        Only the exact ``fact_key + fact_version`` pair matches; there is
        intentionally no latest-version fallback.  Rows learned after
        ``data_cutoff`` are invisible to this query.
        """

        if not isinstance(fact_reference, VersionedReference):
            raise DomainValidationError(
                "fact_reference must be a VersionedReference"
            )
        cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        row = (
            self.session.execute(
                select(InstrumentRuleFactRecord).where(
                    InstrumentRuleFactRecord.fact_key == fact_reference.key,
                    InstrumentRuleFactRecord.fact_version
                    == fact_reference.version,
                    InstrumentRuleFactRecord.known_at <= cutoff,
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return _to_domain(row)

    def list_facts(
        self,
        instrument_id: UUID,
        package_reference: VersionedReference,
        *,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[RuleFactCandidate, ...]:
        """Return facts of one instrument/package intersecting the window.

        A fact intersects ``[start_date, end_date]`` when its half-open
        validity range covers at least one day of the window.  Only rows
        with ``known_at <= data_cutoff`` participate.  The result is
        ordered by ``(valid_from, fact_version)``; overlapping candidates
        and coverage gaps are returned as-is so the preflight layer can
        raise structured conflicts instead of this repository silently
        picking a winner.
        """

        _aware_datetime(data_cutoff, "data_cutoff")
        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if not isinstance(package_reference, VersionedReference):
            raise DomainValidationError(
                "package_reference must be a VersionedReference"
            )
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise DomainValidationError("start_date must be a calendar date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise DomainValidationError("end_date must be a calendar date")
        if start_date > end_date:
            raise DomainValidationError("start_date cannot be after end_date")

        rows = (
            self.session.execute(
                select(InstrumentRuleFactRecord)
                .where(
                    InstrumentRuleFactRecord.instrument_id == instrument_id,
                    InstrumentRuleFactRecord.rule_package_key
                    == package_reference.key,
                    InstrumentRuleFactRecord.rule_package_version
                    == package_reference.version,
                    # Half-open window intersection.
                    InstrumentRuleFactRecord.valid_from <= end_date,
                    (
                        InstrumentRuleFactRecord.valid_to.is_(None)
                        | (InstrumentRuleFactRecord.valid_to > start_date)
                    ),
                    # Knowledge-time visibility boundary.
                    InstrumentRuleFactRecord.known_at <= data_cutoff,
                )
                .order_by(
                    InstrumentRuleFactRecord.valid_from,
                    InstrumentRuleFactRecord.fact_version,
                )
            )
            .scalars()
            .all()
        )
        return tuple(_to_domain(row) for row in rows)


def _to_domain(row: InstrumentRuleFactRecord) -> RuleFactCandidate:
    """Project one ORM row into its immutable domain candidate.

    Validation runs again here so corrupted or hand-edited rows fail
    loudly at query time instead of leaking invalid facts into callers.
    The stored ``content_hash`` is recomputed from the projected content:
    a row whose fields were edited without republishing a new version is
    rejected as drifted.
    """

    try:
        exception_fact_ref = None
        if row.rule_exception_key is not None:
            if row.rule_exception_version is None:
                raise DomainValidationError(
                    "rule_exception_key without rule_exception_version"
                )
            exception_fact_ref = VersionedReference(
                key=row.rule_exception_key, version=row.rule_exception_version
            )
        candidate = RuleFactCandidate(
            fact_reference=VersionedReference(
                key=row.fact_key, version=row.fact_version
            ),
            instrument_id=row.instrument_id,
            package_reference=VersionedReference(
                key=row.rule_package_key, version=row.rule_package_version
            ),
            source=row.source,
            source_revision=row.source_revision,
            known_at=row.known_at,
            observed_at=row.observed_at,
            quality_status=FactQualityStatus(row.quality_status),
            fixture_only=row.fixture_only,
            content_hash=row.content_hash,
            fields=dict(row.fields),
            exception_fact_ref=exception_fact_ref,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
        )
        recomputed = rule_fact_content_hash(
            fact_reference=candidate.fact_reference,
            instrument_id=candidate.instrument_id,
            package_reference=candidate.package_reference,
            exception_fact_ref=candidate.exception_fact_ref,
            valid_from=candidate.valid_from,
            valid_to=candidate.valid_to,
            fields=candidate.fields,
            source=candidate.source,
            source_revision=candidate.source_revision,
            known_at=candidate.known_at,
            observed_at=candidate.observed_at,
            quality_status=candidate.quality_status,
            fixture_only=candidate.fixture_only,
        )
        if recomputed != row.content_hash:
            raise DomainValidationError(
                f"stored instrument rule fact {row.fact_key}@{row.fact_version} "
                "content does not match its content_hash; facts are "
                "append-only and must be republished as a new version "
                "instead of being edited"
            )
        return candidate
    except DomainValidationError as exc:
        raise DomainValidationError(
            f"stored instrument rule fact {row.fact_key}@{row.fact_version} "
            f"violates the domain contract: {exc}"
        ) from exc
