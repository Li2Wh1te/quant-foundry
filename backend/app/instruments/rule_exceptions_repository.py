"""PostgreSQL persistence for named instrument rule exception sets.

Sets are loaded strictly by the exact ``set_key + set_version`` pair —
there is no latest-version fallback.  The stored ``content_hash`` is
recomputed from the loaded entries on every read so a drifted set fails
loudly instead of silently routing instruments with stale exceptions.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import VersionedReference, _required_label
from app.instruments.rule_exceptions_models import (
    InstrumentRuleExceptionEntryRecord,
    InstrumentRuleExceptionSetRecord,
)
from app.instruments.rules.contracts import (
    FactQualityStatus,
    RuleExceptionEntry,
    RuleExceptionSetDefinition,
    exception_set_content_hash,
)


class RuleExceptionSetVersionExistsError(DomainValidationError):
    """Raised when a set is appended under an existing set version."""


class RuleExceptionSetContentDriftError(DomainValidationError):
    """Raised when a stored set's content hash no longer matches its rows."""


@dataclass(frozen=True, slots=True)
class PersistedExceptionSet:
    """A loaded exception set plus its full provenance metadata.

    Construction validates every field so a gateway cannot smuggle in an
    unknown quality status or naive timestamps that would silently shift
    point-in-time visibility.
    """

    definition: RuleExceptionSetDefinition
    source: str
    source_revision: str | None
    known_at: datetime
    observed_at: datetime
    quality_status: FactQualityStatus
    fixture_only: bool
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.definition, RuleExceptionSetDefinition):
            raise DomainValidationError(
                "definition must be a RuleExceptionSetDefinition"
            )
        object.__setattr__(self, "source", _required_label(self.source, "source"))
        object.__setattr__(
            self, "known_at", _aware_datetime(self.known_at, "known_at")
        )
        object.__setattr__(
            self, "observed_at", _aware_datetime(self.observed_at, "observed_at")
        )
        if not isinstance(self.quality_status, FactQualityStatus):
            raise DomainValidationError(
                "quality_status must be a FactQualityStatus"
            )
        if not isinstance(self.fixture_only, bool):
            raise DomainValidationError("fixture_only must be a boolean")
        if not isinstance(self.content_hash, str) or not self.content_hash.strip():
            raise DomainValidationError("content_hash must be non-blank text")


class RuleExceptionSetsRepository:
    """Append and exact-version load named exception sets."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append_exception_set(
        self,
        definition: RuleExceptionSetDefinition,
        *,
        source: str,
        known_at: datetime,
        observed_at: datetime,
        quality_status: FactQualityStatus = FactQualityStatus.COMPLETE,
        fixture_only: bool = False,
        source_revision: str | None = None,
        content_hash: str | None = None,
    ) -> VersionedReference:
        """Append one immutable exception-set version and its entries.

        ``content_hash`` defaults to the canonical order-independent hash
        of the definition; entries are written in the same transaction as
        the set row.  A duplicate ``set_key + set_version`` is rejected.
        """

        if not isinstance(definition, RuleExceptionSetDefinition):
            raise DomainValidationError(
                "definition must be a RuleExceptionSetDefinition"
            )
        recomputed = exception_set_content_hash(definition)
        if content_hash is not None and content_hash != recomputed:
            # A wrong caller-supplied hash would make every later read
            # fail its drift check; reject it at the write boundary.
            raise DomainValidationError(
                "content_hash does not match the exception set content; "
                "compute it with exception_set_content_hash()"
            )
        resolved_hash = recomputed
        existing = (
            self.session.execute(
                select(InstrumentRuleExceptionSetRecord.id).where(
                    InstrumentRuleExceptionSetRecord.set_key
                    == definition.reference.key,
                    InstrumentRuleExceptionSetRecord.set_version
                    == definition.reference.version,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise RuleExceptionSetVersionExistsError(
                "instrument rule exception set already exists for "
                f"{definition.reference.key}@{definition.reference.version}; "
                "append a new set_version instead of overwriting history"
            )
        self.session.add(
            InstrumentRuleExceptionSetRecord(
                id=uuid4(),
                set_key=definition.reference.key,
                set_version=definition.reference.version,
                rule_package_key=definition.package_reference.key,
                rule_package_version=definition.package_reference.version,
                source=source,
                source_revision=source_revision,
                known_at=_aware_datetime(known_at, "known_at"),
                observed_at=_aware_datetime(observed_at, "observed_at"),
                quality_status=quality_status.value,
                fixture_only=fixture_only,
                content_hash=resolved_hash,
            )
        )
        for entry in definition.entries:
            self.session.add(
                InstrumentRuleExceptionEntryRecord(
                    id=uuid4(),
                    set_key=definition.reference.key,
                    set_version=definition.reference.version,
                    instrument_id=entry.instrument_id,
                    exception_fact_key=entry.exception_fact_ref.key,
                    exception_fact_version=entry.exception_fact_ref.version,
                    valid_from=entry.valid_from,
                    valid_to=entry.valid_to,
                )
            )
        return definition.reference

    def load_exception_set(
        self,
        set_reference: VersionedReference,
        *,
        data_cutoff: datetime | None = None,
    ) -> PersistedExceptionSet | None:
        """Return the exact set version with entries, or ``None``.

        Only the exact ``set_key + set_version`` matches; sets learned
        after ``data_cutoff`` (when provided) are invisible.  The stored
        content hash must equal the recomputed hash of the loaded entries.
        """

        if not isinstance(set_reference, VersionedReference):
            raise DomainValidationError(
                "set_reference must be a VersionedReference"
            )
        cutoff = (
            _aware_datetime(data_cutoff, "data_cutoff")
            if data_cutoff is not None
            else None
        )
        conditions = [
            InstrumentRuleExceptionSetRecord.set_key == set_reference.key,
            InstrumentRuleExceptionSetRecord.set_version
            == set_reference.version,
        ]
        if cutoff is not None:
            conditions.append(
                InstrumentRuleExceptionSetRecord.known_at <= cutoff
            )
        row = (
            self.session.execute(
                select(InstrumentRuleExceptionSetRecord).where(*conditions)
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        entry_rows = (
            self.session.execute(
                select(InstrumentRuleExceptionEntryRecord)
                .where(
                    InstrumentRuleExceptionEntryRecord.set_key == row.set_key,
                    InstrumentRuleExceptionEntryRecord.set_version
                    == row.set_version,
                )
                .order_by(
                    InstrumentRuleExceptionEntryRecord.instrument_id,
                    InstrumentRuleExceptionEntryRecord.valid_from,
                    InstrumentRuleExceptionEntryRecord.exception_fact_key,
                    InstrumentRuleExceptionEntryRecord.exception_fact_version,
                )
            )
            .scalars()
            .all()
        )
        try:
            definition = RuleExceptionSetDefinition(
                reference=VersionedReference(
                    key=row.set_key, version=row.set_version
                ),
                package_reference=VersionedReference(
                    key=row.rule_package_key, version=row.rule_package_version
                ),
                entries=tuple(
                    RuleExceptionEntry(
                        instrument_id=entry_row.instrument_id,
                        exception_fact_ref=VersionedReference(
                            key=entry_row.exception_fact_key,
                            version=entry_row.exception_fact_version,
                        ),
                        valid_from=entry_row.valid_from,
                        valid_to=entry_row.valid_to,
                    )
                    for entry_row in entry_rows
                ),
            )
        except DomainValidationError as exc:
            raise DomainValidationError(
                f"stored instrument rule exception set "
                f"{row.set_key}@{row.set_version} violates the domain "
                f"contract: {exc}"
            ) from exc
        recomputed = exception_set_content_hash(definition)
        if recomputed != row.content_hash:
            raise RuleExceptionSetContentDriftError(
                f"stored instrument rule exception set "
                f"{row.set_key}@{row.set_version} content hash does not "
                "match its entries; the set must be republished as a new "
                "version instead of being edited in place"
            )
        return PersistedExceptionSet(
            definition=definition,
            source=row.source,
            source_revision=row.source_revision,
            known_at=row.known_at,
            observed_at=row.observed_at,
            quality_status=FactQualityStatus(row.quality_status),
            fixture_only=row.fixture_only,
            content_hash=row.content_hash,
        )
