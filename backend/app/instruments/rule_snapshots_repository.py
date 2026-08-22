"""Transaction-scoped persistence for run rule snapshots.

``write_bundle`` appends the run-level row and every instrument segment
row to the caller's session; the caller owns commit/rollback so both
layers land in the same transaction as run creation.  ``load_bundle``
rebuilds the immutable domain bundle from stored rows and re-verifies
the snapshot hash, guaranteeing that later edits anywhere else cannot go
unnoticed.
"""

from uuid import UUID, uuid4
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError
from app.instruments.domain import VersionedReference
from app.instruments.rule_snapshots import (
    InstrumentRuleSnapshotSegment,
    RunRuleSnapshotBundle,
)
from app.instruments.rules.contracts import canonical_payload
from app.instruments.rule_snapshots_models import (
    BacktestRunInstrumentRuleSnapshotRecord,
    BacktestRunRuleSnapshotRecord,
)

if TYPE_CHECKING:
    from app.instruments.rules.contracts import RulePackageDefinition


class RunRuleSnapshotRepository:
    """Write and reload frozen run rule snapshots."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def write_bundle(self, bundle: RunRuleSnapshotBundle) -> str:
        """Append one complete snapshot for an already-bound run.

        The bundle must carry a ``run_id`` (preflight produces unbound
        bundles; run creation binds and persists them in one transaction).
        Returns the verified ``snapshot_hash``.
        """

        if not isinstance(bundle, RunRuleSnapshotBundle):
            raise DomainValidationError("bundle must be a RunRuleSnapshotBundle")
        if bundle.run_id is None:
            raise DomainValidationError(
                "bundle.run_id must be bound before the snapshot is written"
            )
        existing = (
            self.session.execute(
                select(BacktestRunRuleSnapshotRecord.id).where(
                    BacktestRunRuleSnapshotRecord.run_id == bundle.run_id
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise DomainValidationError(
                "a rule snapshot already exists for run "
                f"{bundle.run_id}; snapshots are write-once"
            )
        exception_ref = bundle.exception_set_reference
        self.session.add(
            BacktestRunRuleSnapshotRecord(
                id=uuid4(),
                run_id=bundle.run_id,
                rule_package_key=bundle.rule_package_reference.key,
                rule_package_version=bundle.rule_package_reference.version,
                rule_package_semantic_hash=bundle.rule_package_semantic_hash,
                parser_revision=bundle.parser_revision,
                exception_set_key=(
                    exception_ref.key if exception_ref is not None else None
                ),
                exception_set_version=(
                    exception_ref.version if exception_ref is not None else None
                ),
                exception_set_hash=bundle.exception_set_hash,
                data_cutoff=bundle.data_cutoff,
                snapshot_hash=bundle.snapshot_hash,
            )
        )
        for segment in bundle.instrument_segments:
            normal_ref = segment.normal_fact_reference
            exception_fact_ref = segment.exception_fact_reference
            self.session.add(
                BacktestRunInstrumentRuleSnapshotRecord(
                    id=uuid4(),
                    run_id=bundle.run_id,
                    instrument_id=segment.instrument_id,
                    effective_from=segment.effective_from,
                    effective_to=segment.effective_to,
                    normal_fact_key=normal_ref.key,
                    normal_fact_version=normal_ref.version,
                    exception_fact_key=(
                        exception_fact_ref.key
                        if exception_fact_ref is not None
                        else None
                    ),
                    exception_fact_version=(
                        exception_fact_ref.version
                        if exception_fact_ref is not None
                        else None
                    ),
                    normalized_values=canonical_payload(
                        segment.normalized_values
                    ),
                    capability_declarations=canonical_payload(
                        segment.capability_declarations
                    ),
                    provenance=canonical_payload(segment.provenance),
                    resolution_hash=segment.resolution_hash,
                )
            )
        return bundle.snapshot_hash

    def load_bundle(
        self,
        run_id: UUID,
        *,
        rule_package_definition: "RulePackageDefinition | None" = None,
    ) -> RunRuleSnapshotBundle | None:
        """Reload the frozen snapshot of one run, or ``None`` if absent.

        ``normalized_values`` are stored as canonical JSON; pass the run's
        :class:`RulePackageDefinition` to restore them into the exact
        domain-normalized types (``Decimal``, ``VersionedReference``,
        tuples, declarations) the original ready resolution carried.
        Without the definition the canonical JSON form is returned as-is.
        """

        if not isinstance(run_id, UUID):
            raise DomainValidationError("run_id must be a UUID")
        row = (
            self.session.execute(
                select(BacktestRunRuleSnapshotRecord).where(
                    BacktestRunRuleSnapshotRecord.run_id == run_id
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        segment_rows = (
            self.session.execute(
                select(BacktestRunInstrumentRuleSnapshotRecord)
                .where(
                    BacktestRunInstrumentRuleSnapshotRecord.run_id == run_id
                )
                .order_by(
                    BacktestRunInstrumentRuleSnapshotRecord.instrument_id,
                    BacktestRunInstrumentRuleSnapshotRecord.effective_from,
                )
            )
            .scalars()
            .all()
        )
        try:
            segments = tuple(
                self._segment_from_row(segment_row, rule_package_definition)
                for segment_row in segment_rows
            )
            exception_ref = (
                VersionedReference(
                    key=row.exception_set_key, version=row.exception_set_version
                )
                if row.exception_set_key is not None
                else None
            )
            bundle = RunRuleSnapshotBundle(
                run_id=row.run_id,
                rule_package_reference=VersionedReference(
                    key=row.rule_package_key, version=row.rule_package_version
                ),
                rule_package_semantic_hash=row.rule_package_semantic_hash,
                parser_revision=row.parser_revision,
                exception_set_reference=exception_ref,
                exception_set_hash=row.exception_set_hash,
                data_cutoff=row.data_cutoff,
                instrument_segments=segments,
            )
        except DomainValidationError as exc:
            raise DomainValidationError(
                f"stored rule snapshot for run {run_id} violates the "
                f"domain contract: {exc}"
            ) from exc
        if bundle.snapshot_hash != row.snapshot_hash:
            raise DomainValidationError(
                f"stored rule snapshot for run {run_id} does not match its "
                "recorded snapshot_hash; persisted snapshots are immutable"
            )
        return bundle

    @staticmethod
    def _segment_from_row(
        segment_row, rule_package_definition: "RulePackageDefinition | None"
    ) -> InstrumentRuleSnapshotSegment:
        """Project one stored segment row into its immutable domain object.

        When the run's rule-package definition is supplied, the canonical
        JSON ``normalized_values`` are restored into domain types via the
        resolver's own per-field normalization; the recomputed snapshot
        hash is unaffected because hashing canonicalizes both forms to
        identical payloads.
        """

        if rule_package_definition is not None:
            from app.instruments.rules.resolver import (
                restore_normalized_values,
            )

            normalized_values = restore_normalized_values(
                rule_package_definition,
                dict(segment_row.normalized_values),
            )
        else:
            normalized_values = dict(segment_row.normalized_values)
        return InstrumentRuleSnapshotSegment(
            instrument_id=segment_row.instrument_id,
            effective_from=segment_row.effective_from,
            effective_to=segment_row.effective_to,
            normal_fact_reference=VersionedReference(
                key=segment_row.normal_fact_key,
                version=segment_row.normal_fact_version,
            ),
            exception_fact_reference=(
                VersionedReference(
                    key=segment_row.exception_fact_key,
                    version=segment_row.exception_fact_version,
                )
                if segment_row.exception_fact_key is not None
                else None
            ),
            normalized_values=normalized_values,
            capability_declarations=dict(
                segment_row.capability_declarations
            ),
            provenance=dict(segment_row.provenance),
            resolution_hash=segment_row.resolution_hash,
        )
